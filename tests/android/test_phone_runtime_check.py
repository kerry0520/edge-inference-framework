"""Readiness checks use tiny nonexecutable fixtures and mocked ADB only."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_plan_profile as profile
import phone_runtime_check as readiness


class RuntimeReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.payload = {"binary": b"not executable", "worker": b"unit worker", "helper": b"unit helper"}
        identity = deepcopy(profile.SOURCE_IDENTITY)
        for role, key in (("binary", "binary_sha256"), ("worker", "source_sha256"),
                          ("helper", "helper_sha256")):
            identity[key] = hashlib.sha256(self.payload[role]).hexdigest()
        fixture = patch.object(profile, "SOURCE_IDENTITY", identity)
        fixture.start()
        self.addCleanup(fixture.stop)
        bundle = self.base / "运行包 空格"
        bundle.mkdir()
        for role, data in self.payload.items():
            (bundle / profile.FILES[role]).write_bytes(data)
        for name, document in (("profile.json", profile.profile_document(profile.SOURCE_PROFILE_ID)),
                               (profile.FILES["runtime_build"], profile.runtime_document(profile.SOURCE_PROFILE_ID)),
                               (profile.FILES["provenance"], profile.provenance_document(profile.SOURCE_PROFILE_ID))):
            (bundle / name).write_text(json.dumps(document), encoding="utf8")
        self.profile_path = bundle / "profile.json"
        self.adb = self.base / "adb fixture"
        self.adb.write_bytes(b"never executed")
        self.calls = []
        self.transport = "fixture-transport"

    def check(self, **kwargs):
        return readiness.check_runtime(self.profile_path, adb_path=self.adb, **kwargs)

    def respond(self, argv, **kwargs):
        """Only inventory/property/file-existence reads may reach mocked ADB."""
        self.calls.append((argv, kwargs))
        self.assertEqual(Path(argv[0]), self.adb)
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertTrue(kwargs.get("capture_output"))
        self.assertGreater(kwargs.get("timeout", 0), 0)
        args = argv[1:]
        if args[:1] == ["devices"]:
            output = "List of devices attached\nfixture-transport\tdevice product:venus model:M2011K2C\n"
        else:
            self.assertEqual(args[:2], ["-s", self.transport])
            self.assertEqual(args[2], "shell")
            command = " ".join(args[3:])
            if command == "getprop ro.product.model":
                output = profile.LIMITS["device_model"] + "\n"
            elif command in ("getprop ro.serialno", "getprop ro.boot.serialno"):
                output = profile.LIMITS["hardware_serial"] + "\n"
            else:
                self.assertIn("test", command)
                self.assertIn(profile.LIMITS["model_directory"], command)
                for forbidden in ("push ", "forward ", "chmod ", "mkdir ", "--serve", "kill "):
                    self.assertNotIn(forbidden, command)
                output = ""
        return subprocess.CompletedProcess(argv, 0, output.encode(), b"")

    def test_offline_profile_is_verified_without_adb_or_native_execution(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("ADB forbidden")) as run, \
                patch.object(subprocess, "Popen", side_effect=AssertionError("model forbidden")) as popen:
            report = self.check()
        self.assertTrue(report["accepted"])
        self.assertEqual(report["offline"]["status"], "passed")
        self.assertEqual(report["device"]["status"], "not_run")
        self.assertEqual(report["commands"], [])
        run.assert_not_called()
        popen.assert_not_called()

    def test_tampered_bundle_fails_before_device_calls(self):
        (self.profile_path.parent / profile.FILES["binary"]).write_bytes(b"tampered")
        with patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen:
            report = self.check(device=True, serial="fixture-transport")
        self.assertFalse(report["accepted"])
        self.assertEqual(report["offline"]["status"], "failed")
        self.assertEqual(report["device"]["status"], "not_run")
        self.assertTrue(report["errors"])
        run.assert_not_called()
        popen.assert_not_called()

    def test_existing_output_is_not_overwritten_and_does_not_run_checks(self):
        output = self.base / "existing.json"
        sentinel = b"preserve original evidence\n"
        output.write_bytes(sentinel)
        with patch.object(readiness, "check_runtime") as check, \
                patch.object(subprocess, "run") as run, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                result = readiness.main(["--plan-profile", str(self.profile_path), "--output", str(output)])
            except SystemExit as error:
                result = error.code
        self.assertNotEqual(result, 0)
        check.assert_not_called()
        run.assert_not_called()
        self.assertEqual(output.read_bytes(), sentinel)

    def test_explicit_device_passes_only_readonly_checks_and_records_actual_exits(self):
        with patch.object(subprocess, "run", side_effect=self.respond), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("native launch forbidden")) as popen:
            report = self.check(device=True, serial="fixture-transport")
        self.assertTrue(report["accepted"])
        self.assertEqual(report["device"]["status"], "passed")
        self.assertEqual(report["device"]["identity"]["hardware_serial"], profile.LIMITS["hardware_serial"])
        self.assertGreater(len(report["commands"]), 0)
        self.assertTrue(all(command["actual_exit"] == 0 for command in report["commands"]))
        self.assertEqual(len(report["device"]["assets"]), 5)
        popen.assert_not_called()

    def test_device_model_and_hardware_serial_mismatch_reject_before_assets(self):
        for property_name in ("ro.product.model", "ro.serialno"):
            with self.subTest(property_name=property_name):
                self.calls.clear()
                def wrong_identity(argv, **kwargs):
                    response = self.respond(argv, **kwargs)
                    if " ".join(argv).endswith("getprop " + property_name):
                        return subprocess.CompletedProcess(argv, 0, b"wrong-device\n", b"")
                    return response
                with patch.object(subprocess, "run", side_effect=wrong_identity):
                    report = self.check(device=True, serial="fixture-transport")
                self.assertFalse(report["accepted"])
                self.assertEqual(report["device"]["status"], "failed")
                self.assertTrue(report["errors"])
                self.assertFalse(any("test " in " ".join(argv) for argv, _ in self.calls))

    def test_adb_command_failure_is_not_model_failure_or_success(self):
        failure = subprocess.CompletedProcess(["adb"], 23, b"", b"transport error")
        with patch.object(subprocess, "run", return_value=failure), patch.object(subprocess, "Popen") as popen:
            report = self.check(device=True, serial="fixture-transport")
        self.assertFalse(report["accepted"])
        self.assertEqual(report["device"]["status"], "failed")
        self.assertEqual(report["commands"][0]["actual_exit"], 23)
        self.assertTrue(report["errors"])
        popen.assert_not_called()

    def test_adb_timeout_has_no_invented_zero_exit(self):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(["adb"], 15)), \
                patch.object(subprocess, "Popen") as popen:
            report = self.check(device=True, serial="fixture-transport")
        self.assertFalse(report["accepted"])
        self.assertEqual(report["device"]["status"], "failed")
        self.assertIsNone(report["commands"][0]["actual_exit"])
        self.assertTrue(report["commands"][0]["timed_out"])
        self.assertTrue(report["errors"])
        popen.assert_not_called()

    def test_missing_model_asset_rejects_without_native_launch(self):
        def missing_asset(argv, **kwargs):
            response = self.respond(argv, **kwargs)
            if "test " in " ".join(argv):
                return subprocess.CompletedProcess(argv, 1, b"", b"model asset missing")
            return response
        with patch.object(subprocess, "run", side_effect=missing_asset), patch.object(subprocess, "Popen") as popen:
            report = self.check(device=True, serial="fixture-transport")
        self.assertFalse(report["accepted"])
        self.assertEqual(report["device"]["status"], "failed")
        self.assertTrue(report["errors"])
        self.assertTrue(any(command["actual_exit"] == 1 for command in report["commands"]))
        popen.assert_not_called()

    def test_cli_serializes_offline_device_not_run_without_claiming_device_pass(self):
        output = self.base / "new.json"
        with patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = readiness.main(["--plan-profile", str(self.profile_path), "--adb-path", str(self.adb),
                                     "--output", str(output)])
        self.assertEqual(result, 0)
        report = json.loads(output.read_text(encoding="utf8"))
        self.assertEqual(report["offline"]["status"], "passed")
        self.assertEqual(report["device"]["status"], "not_run")
        self.assertTrue(report["accepted"])
        run.assert_not_called()
        popen.assert_not_called()

    def test_missing_adb_file_and_client_import_fail_offline_without_execution(self):
        with patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen:
            report = readiness.check_runtime(self.profile_path, adb_path=self.base / "missing-adb", device=True)
            self.assertFalse(report["accepted"])
            self.assertEqual(report["offline"]["status"], "failed")
            self.assertEqual(report["device"]["status"], "not_run")
            with patch.object(readiness.importlib, "import_module", side_effect=ImportError("missing client")):
                report = self.check(device=True)
            self.assertFalse(report["accepted"])
            self.assertEqual(report["errors"][0]["stage"], "offline")
            self.assertEqual(report["device"]["status"], "not_run")
        run.assert_not_called()
        popen.assert_not_called()

    def test_cli_serial_without_device_is_rejected_before_any_check(self):
        with patch.object(readiness, "check_runtime") as check, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                readiness.main(["--plan-profile", str(self.profile_path), "--serial", "fixture-transport"])
        self.assertNotEqual(error.exception.code, 0)
        check.assert_not_called()

    def test_failed_device_report_is_saved_with_nonzero_cli_exit(self):
        output = self.base / "device-failure.json"
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(["adb"], 15)), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = readiness.main(["--plan-profile", str(self.profile_path), "--adb-path", str(self.adb),
                                     "--device", "--serial", "fixture-transport", "--output", str(output)])
        self.assertNotEqual(result, 0)
        report = json.loads(output.read_text(encoding="utf8"))
        self.assertFalse(report["accepted"])
        self.assertEqual(report["offline"]["status"], "passed")
        self.assertEqual(report["device"]["status"], "failed")
        self.assertIsNone(report["commands"][0]["actual_exit"])

    def test_explicit_current_address_connects_without_mdns_then_verifies_identity(self):
        self.transport = "192.168.5.30:45678"
        def explicit_connect(argv, **kwargs):
            if argv[1:2] == ["connect"]:
                self.calls.append((argv, kwargs))
                self.assertEqual(argv[1:], ["connect", self.transport])
                return subprocess.CompletedProcess(argv, 0, b"connected", b"")
            return self.respond(argv, **kwargs)
        with patch.object(subprocess, "run", side_effect=explicit_connect):
            report = self.check(device=True, serial=self.transport)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["device"]["identity"]["serial"], self.transport)
        self.assertEqual(sum(argv[1:2] == ["connect"] for argv, _ in self.calls), 1)
        self.assertFalse(any(argv[1:2] == ["mdns"] for argv, _ in self.calls))
        self.assertTrue(any("getprop ro.serialno" in " ".join(argv) for argv, _ in self.calls))

    def test_paired_service_preferred_to_same_phone_ip_but_identity_still_checked(self):
        self.transport = "adb-" + profile.LIMITS["hardware_serial"] + "-fixture._adb-tls-connect._tcp"
        def paired_inventory(argv, **kwargs):
            if argv[1:2] == ["devices"]:
                self.calls.append((argv, kwargs))
                inventory = ("List of devices attached\n192.168.5.30:45678 device\n" +
                             self.transport + " device\n")
                return subprocess.CompletedProcess(argv, 0, inventory.encode(), b"")
            return self.respond(argv, **kwargs)
        with patch.object(subprocess, "run", side_effect=paired_inventory):
            report = self.check(device=True)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["device"]["identity"]["serial"], self.transport)
        self.assertTrue(any("getprop ro.product.model" in " ".join(argv) for argv, _ in self.calls))
        self.assertTrue(any("getprop ro.serialno" in " ".join(argv) for argv, _ in self.calls))
        self.assertFalse(any(argv[1:2] in (["connect"], ["mdns"]) for argv, _ in self.calls))

    def test_new_ip_online_is_selected_and_identity_read_once(self):
        self.transport = "192.168.5.4:40895"
        def new_inventory(argv, **kwargs):
            if argv[1:2] == ["devices"]:
                self.calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0,
                    ("List of devices attached\n" + self.transport + " device\n").encode(), b"")
            return self.respond(argv, **kwargs)
        with patch.object(subprocess, "run", side_effect=new_inventory):
            report = self.check(device=True)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["device"]["identity"]["serial"], self.transport)
        self.assertEqual(sum("getprop ro.serialno" in " ".join(argv) for argv, _ in self.calls), 1)
        self.assertFalse(any(argv[1:2] in (["connect"], ["mdns"]) for argv, _ in self.calls))

    def test_new_ip_mdns_is_connected_before_property_checks(self):
        self.transport = "192.168.5.4:40895"
        def discovered(argv, **kwargs):
            if argv[1:2] == ["devices"]:
                self.calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, b"List of devices attached\n", b"")
            if argv[1:2] == ["mdns"]:
                self.calls.append((argv, kwargs))
                text = "paired-name _adb-tls-connect._tcp " + self.transport + "\n"
                return subprocess.CompletedProcess(argv, 0, text.encode(), b"")
            if argv[1:2] == ["connect"]:
                self.calls.append((argv, kwargs))
                self.assertEqual(argv[2], self.transport)
                return subprocess.CompletedProcess(argv, 0, b"connected\n", b"")
            return self.respond(argv, **kwargs)
        with patch.object(subprocess, "run", side_effect=discovered):
            report = self.check(device=True)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["device"]["identity"]["serial"], self.transport)
        self.assertEqual(sum(argv[1:2] == ["connect"] for argv, _ in self.calls), 1)
        self.assertTrue(all(row["actual_exit"] == 0 for row in report["commands"]))

    def test_automatic_same_model_wrong_hardware_never_reads_model_assets(self):
        self.transport = "192.168.5.4:40895"
        def wrong_phone(argv, **kwargs):
            if argv[1:2] == ["devices"]:
                self.calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, (self.transport + " device\n").encode(), b"")
            if argv[1:2] == ["mdns"]:
                self.calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if " ".join(argv).endswith("getprop ro.serialno"):
                self.calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, b"different-hardware\n", b"")
            return self.respond(argv, **kwargs)
        with patch.object(subprocess, "run", side_effect=wrong_phone):
            report = self.check(device=True)
        self.assertFalse(report["accepted"])
        self.assertFalse(any("test -f" in " ".join(argv) for argv, _ in self.calls))


if __name__ == "__main__":
    unittest.main()
