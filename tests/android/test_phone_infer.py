"""Offline CLI/protocol/device/failure tests; no phone model execution."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("phone_infer", ROOT / "scripts/android/phone_infer.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.owner = tempfile.TemporaryDirectory(prefix="phone-entry-tests-")
        self.work = Path(self.owner.name).resolve()
        self.addCleanup(self.owner.cleanup)

    def quiet(self):
        return contextlib.redirect_stderr(io.StringIO())

    def test_argument_and_single_line_validation(self):
        for argv in (["--prompt", "x", "--max-new-tokens", "0"], ["--prompt", "x", "--max-new-tokens", "513"],
                     ["--prompt", "x", "--prompt-file", "x"], ["--prompt", "x", "--cache-mode", "wrong"]):
            with self.quiet(), self.assertRaises(SystemExit):
                entry.parse_args(argv)
        for text in ("", " ", "one\ntwo", "one\rnext"):
            with self.assertRaises(ValueError):
                entry.read_prompt(entry.parse_args(["--prompt", text]))
        question = "  中文与 $() ; & 字符  "
        for end in ("", "\n", "\r\n"):
            file = self.work / "question.txt"
            file.write_bytes((question + end).encode())
            actual, data = entry.read_prompt(entry.parse_args(["--prompt-file", str(file)]))
            self.assertEqual(actual, question)
            self.assertEqual(data, file.read_bytes())
        file.write_bytes(b"one\ntwo\n")
        with self.assertRaises(ValueError):
            entry.read_prompt(entry.parse_args(["--prompt-file", str(file)]))

    def test_answer_protocol_preserves_model_lines(self):
        body = "PID=123\nDEVICE_EXIT=0\nconfig path is imaginary\n text with spaces  \n"
        raw = ("config path is cfg\r\nprompt file is question\r\n" + body.replace("\n", "\r\n")).encode()
        self.assertEqual(entry.extract_answer(raw, "cfg", "question"), body)
        for raw in (b"config path is cfg\n", b"config path is cfg\nprompt file is wrong\nanswer", b"config path is cfg\nprompt file is question\n  "):
            with self.assertRaises(RuntimeError):
                entry.extract_answer(raw, "cfg", "question")

    def test_windows_devices_connected_path(self):
        paired = "adb-" + entry.EXPECTED_HARDWARE_SERIAL + "-fixture._adb-tls-connect._tcp"
        original = "List of devices attached\n192.168.5.30:40139\tdevice\n" + paired + "\tdevice\n"
        windows = original.replace("\n", "\r\n").encode()
        calls = []
        adb = entry.Adb()
        def fake(*args, **kwargs):
            calls.append(args)
            if args == ("devices",): return windows
            if args == ("shell", "getprop ro.product.model"): return b"M2011K2C\r\n"
            if args == ("shell", "getprop ro.serialno"): return (entry.EXPECTED_HARDWARE_SERIAL + "\r\n").encode()
            raise AssertionError("Unexpected mDNS/connect: " + repr(args))
        adb.call = fake
        result = adb.select()
        self.assertEqual(result["hardware_serial"], entry.EXPECTED_HARDWARE_SERIAL)
        self.assertEqual(result["serial"], paired)
        self.assertFalse(any(c[0] in ("mdns", "connect") for c in calls))

    def test_same_hardware_changed_wireless_port_reuses_identity(self):
        records = self.work / "requests"
        records.mkdir()
        entry.write_json(self.work / "installation-model-identity-v2.json", {"hardware_serial": "same", "model": entry.EXPECTED_MODEL,
            "initial_connection_serial": "192.168.5.30:11111", "binary_sha256": entry.BINARY_SHA, "files": {"recorded": "digest"}})
        class Fake:
            serial = "192.168.5.30:22222"
            def shell(self, command, **kwargs):
                return (entry.BINARY_SHA + " remote").encode() if command.startswith("sha256sum") else b""
        record = {"device": {"hardware_serial": "same"}}
        with patch.object(entry, "RECORDS", records), patch.object(entry, "model_identity", side_effect=AssertionError("No full rehash")):
            entry.install(Fake(), Path("unused"), record)
        self.assertEqual(record["model_identity"]["verification"], "recorded_at_installation")

    def test_prepare_only_never_calls_adb(self):
        records = self.work / "requests"
        with patch.object(entry, "RECORDS", records), patch.object(entry, "verify_binary", return_value=Path("unused")),              patch.object(entry.Adb, "call", side_effect=AssertionError("prepare contacted phone")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(entry.main(["--prompt", "offline question", "--prepare-only"]), 0)
        record = json.loads(next(records.glob("*/request.json")).read_text())
        self.assertFalse(record["phone_run"])
        self.assertEqual(record["status"], "prepared_offline")
        self.assertEqual(record["adb_commands"], [])

    def test_interrupt_marks_unknown_before_unlock(self):
        records = self.work / "requests"
        observed = []
        original_write = entry.write_json
        def observe_write(path, value):
            if Path(path).name == "uncertain-request.json":
                observed.append((self.work / "active-request.lock").exists())
            original_write(path, value)
        def interrupt(adb, binary, directory, record, args):
            record.update(phone_run_attempted=True, phone_run=None)
            raise KeyboardInterrupt()
        with patch.object(entry, "RECORDS", records), patch.object(entry, "verify_binary", return_value=Path("unused")), patch.object(entry, "execute", side_effect=interrupt), patch.object(entry, "write_json", side_effect=observe_write), self.quiet():
            self.assertEqual(entry.main(["--prompt", "interrupted"]), 130)
        self.assertEqual(observed, [True])
        record = json.loads(next(records.glob("*/request.json")).read_text())
        self.assertEqual(record["status"], "interrupted")
        self.assertIsNone(record["phone_run"])

    def test_uncertain_submission_blocks_next_execution(self):
        records = self.work / "requests"
        def timeout(adb, binary, directory, record, args):
            record.update(phone_run_attempted=True, phone_run=None)
            raise subprocess.TimeoutExpired("model command", 900)
        with patch.object(entry, "RECORDS", records), patch.object(entry, "verify_binary", return_value=Path("unused")),              patch.object(entry, "execute", side_effect=timeout) as executed, self.quiet():
            self.assertEqual(entry.main(["--prompt", "first"]), 1)
            self.assertTrue((self.work / "uncertain-request.json").exists())
            self.assertEqual(entry.main(["--prompt", "second"]), 1)
            self.assertEqual(executed.call_count, 1)
        first = next(r for r in (json.loads(p.read_text()) for p in records.glob("*/request.json")) if r["question"] == "first")
        self.assertIsNone(first["phone_run"])
        self.assertFalse((self.work / "active-request.lock").exists())


if __name__ == "__main__":
    unittest.main()
