"""Offline discovery/identity regression tests. Every ADB action is mocked."""
import importlib.util
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("phone_discovery_under_test", ROOT / "scripts/android/phone_infer.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
TARGET = entry.EXPECTED_HARDWARE_SERIAL
MODEL = entry.EXPECTED_MODEL


class DiscoveryTests(unittest.TestCase):
    def adb(self, devices="", mdns="", identities=None, errors=None, serial=None):
        adb = entry.Adb(serial)
        adb.observed = []
        identities = identities or {}
        errors = errors or {}
        def call(*args, **kwargs):
            adb.observed.append((adb.serial, args, kwargs))
            if args == ("devices",):
                return devices.encode()
            if args == ("mdns", "services"):
                return mdns.encode()
            if args[0] == "connect":
                if args[1] in errors:
                    raise errors[args[1]]
                return b"already connected"
            if args[0] == "shell":
                if adb.serial in errors:
                    raise errors[adb.serial]
                model, hardware, boot = identities.get(adb.serial, ("other", "other", ""))
                return {"getprop ro.product.model": model, "getprop ro.serialno": hardware,
                        "getprop ro.boot.serialno": boot}[args[1]].encode()
            raise AssertionError(args)
        adb.call = call
        return adb

    def target(self):
        return MODEL, TARGET, ""

    def test_changed_private_ip_from_mdns(self):
        endpoint = "192.168.5.4:42205"
        adb = self.adb("List of devices attached\r\n", "adb-x _adb-tls-connect._tcp " + endpoint + "\r\n",
                       {endpoint: self.target()})
        self.assertEqual(adb.select()["serial"], endpoint)
        self.assertIn(("connect", endpoint), [a for _, a, _ in adb.observed])

    def test_usb_selected_without_mdns(self):
        adb = self.adb(TARGET + "\tdevice product:p model:m\r\n", identities={TARGET: self.target()})
        self.assertEqual(adb.select()["serial"], TARGET)
        self.assertNotIn(("mdns", "services"), [a for _, a, _ in adb.observed])

    def test_service_alias_and_usb_priority(self):
        alias = "adb-" + TARGET + "-abc._adb-tls-connect._tcp"
        adb = self.adb(alias + " device\n" + TARGET + " device\n",
                       identities={TARGET: self.target(), alias: self.target()})
        self.assertEqual(adb.select()["serial"], TARGET)
        self.assertEqual(sum(a == ("shell", "getprop ro.serialno") for _, a, _ in adb.observed), 1)

    def test_verified_service_does_not_probe_lower_priority_ip(self):
        alias = "adb-" + TARGET + "-abc._adb-tls-connect._tcp"
        endpoint = "192.168.5.4:12345"
        adb = self.adb(alias + " device\n" + endpoint + " device", identities={alias: self.target()},
                       errors={endpoint: subprocess.TimeoutExpired("adb", 5)})
        self.assertEqual(adb.select()["serial"], alias)
        self.assertFalse(any(s == endpoint for s, _, _ in adb.observed))

    def test_only_online_devices_probed(self):
        adb = self.adb("wrong unauthorized\nold offline\n192.168.5.4:2222 device\n",
                       identities={"192.168.5.4:2222": self.target()})
        self.assertEqual(adb.select()["hardware_serial"], TARGET)
        self.assertFalse(any(s in ("wrong", "old") for s, _, _ in adb.observed))

    def test_same_model_other_hardware_rejected(self):
        adb = self.adb("usb-other device", identities={"usb-other": (MODEL, "different", "")})
        with self.assertRaises(RuntimeError):
            adb.select()
        self.assertIsNone(adb.serial)

    def test_service_name_does_not_grant_trust(self):
        alias = "adb-" + TARGET + "-fake._adb-tls-connect._tcp"
        adb = self.adb(alias + " device", identities={alias: (MODEL, "impostor", "")})
        with self.assertRaises(RuntimeError):
            adb.select()
        self.assertIsNone(adb.serial)

    def test_wrong_model_rejected_without_hardware_read(self):
        adb = self.adb(TARGET + " device", identities={TARGET: ("wrong", TARGET, "")})
        with self.assertRaises(RuntimeError):
            adb.select()
        self.assertFalse(any(a == ("shell", "getprop ro.serialno") for _, a, _ in adb.observed))

    def test_duplicate_mdns_connected_once(self):
        endpoint = "10.1.2.3:12345"
        line = "adb-x _adb-tls-connect._tcp " + endpoint + "\n"
        adb = self.adb(mdns=line * 3, identities={endpoint: self.target()})
        self.assertEqual(adb.select()["serial"], endpoint)
        self.assertEqual(sum(a[0] == "connect" for _, a, _ in adb.observed), 1)

    def test_invalid_public_and_pairing_advertisements_ignored(self):
        lines = ["x _adb-tls-connect._tcp " + value for value in
                 ("8.8.8.8:5555", "127.0.0.1:123", "192.168.1.2:0", "192.168.1.2:65536",
                  "192.168.001.2:123", "192.168.5.4:not-port", "[::1]:5555")]
        lines.append("x _adb-tls-pairing._tcp 192.168.5.4:12345")
        adb = self.adb(mdns="\n".join(lines))
        with self.assertRaises(RuntimeError):
            adb.select()
        self.assertFalse(any(a[0] in ("connect", "shell") for _, a, _ in adb.observed))

    def test_bad_port_timeout_then_valid_port(self):
        bad, good = "192.168.5.4:11111", "192.168.5.4:22222"
        adb = self.adb(mdns="x _adb-tls-connect._tcp " + bad + "\ny _adb-tls-connect._tcp " + good,
                       identities={good: self.target()}, errors={bad: subprocess.TimeoutExpired("adb", 5)})
        self.assertEqual(adb.select()["serial"], good)
        self.assertTrue(all(k.get("timeout") == 5 for _, _, k in adb.observed))

    def test_probe_limit_eight(self):
        adb = self.adb("\n".join("10.0.0.%d:1234 device" % i for i in range(1, 15)))
        with self.assertRaises(RuntimeError):
            adb.select()
        self.assertEqual(sum(a[0] == "shell" for _, a, _ in adb.observed), 8)
        self.assertIsNone(adb.serial)

    def test_equal_priority_target_endpoints_ambiguous(self):
        one, two = "192.168.5.4:11111", "192.168.5.4:22222"
        adb = self.adb(one + " device\n" + two + " device", identities={one: self.target(), two: self.target()})
        with self.assertRaisesRegex(RuntimeError, "歧义"):
            adb.select()
        self.assertIsNone(adb.serial)

    def test_boot_serial_fallback(self):
        adb = self.adb(TARGET + " device", identities={TARGET: (MODEL, "", TARGET)})
        self.assertEqual(adb.select()["hardware_serial"], TARGET)

    def test_explicit_other_hardware_keeps_contract(self):
        adb = self.adb(serial="usb-other", identities={"usb-other": (MODEL, "other", "")})
        self.assertEqual(adb.select()["hardware_serial"], "other")
        self.assertFalse(any(a[0] in ("devices", "mdns", "connect") for _, a, _ in adb.observed))

    def test_explicit_failure_retains_requested_serial(self):
        adb = self.adb(serial="bad")
        with self.assertRaises(RuntimeError):
            adb.select()
        self.assertEqual(adb.serial, "bad")

    def test_no_repeat_shell_for_duplicate_device_rows(self):
        adb = self.adb((TARGET + " device\n") * 2, identities={TARGET: self.target()})
        adb.select()
        self.assertEqual(sum(a[0] == "shell" for _, a, _ in adb.observed), 2)

    def test_existing_failed_endpoint_not_retried_in_mdns(self):
        endpoint = "192.168.5.4:1234"
        adb = self.adb(endpoint + " device", "x _adb-tls-connect._tcp " + endpoint)
        with self.assertRaises(RuntimeError):
            adb.select()
        self.assertFalse(any(a[0] == "connect" for _, a, _ in adb.observed))

    def test_arbitrary_usb_can_be_verified(self):
        adb = self.adb("usb-new device", identities={"usb-new": self.target()})
        self.assertEqual(adb.select()["serial"], "usb-new")


if __name__ == "__main__":
    unittest.main()
