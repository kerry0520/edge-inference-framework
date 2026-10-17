"""Replay synthetic original LLVM evidence without any current native execution."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_source_build_audit as audit
import phone_source_build_audit_replay as replay
import test_phone_source_build_audit as fixture


class ReplayTest(unittest.TestCase):
    setUp = fixture.AuditTest.setUp
    write = staticmethod(fixture.AuditTest.write)
    json = fixture.AuditTest.json
    persist = fixture.AuditTest.persist
    native = fixture.AuditTest.native
    run_audit = fixture.AuditTest.run_audit

    def original(self):
        _, original, _ = self.run_audit()
        return original, audit.candidate.sha(original / "receipt.json")

    def run_replay(self, original, pin=None):
        receipt = original / "receipt.json"
        pin = pin or audit.candidate.sha(receipt)
        _, document = replay.inspect_receipt(receipt, pin)
        output = replay.new_output(self.base / ("replay-" + str(len(list(self.base.glob("replay-*"))))), receipt, document)
        with patch.object(audit.subprocess, "run", side_effect=AssertionError("native execution forbidden")) as run:
            result = replay.replay(receipt, pin, output, root=self.root)
        run.assert_not_called()
        return result, output

    def reseal(self, original):
        receipt = json.loads((original / "receipt.json").read_text())
        receipt["files"] = replay.bindings(original)
        self.json(original / "receipt.json", receipt)

    def test_success_recomputes_objects_and_has_no_native_receipt(self):
        original, pin = self.original()
        result, output = self.run_replay(original, pin)
        self.assertEqual(result["actual_object_count"], 8)
        self.assertEqual(result["original_native_commands"], 4)
        self.assertEqual(result["current_native_commands"], 0)
        self.assertFalse(result["production_admitted"])
        self.assertFalse((output / "receipt.json").exists())
        self.assertFalse((output / "elf-header-command.json").exists())
        trace = replay.load(output / "elf-header-replayed-command.json")
        self.assertFalse(trace["current_native_execution"])
        self.assertEqual(trace["original_actual_exit"], 0)

    def test_wrong_external_pin_rejected(self):
        original, _ = self.original()
        with self.assertRaises(ValueError):
            self.run_replay(original, "0" * 64)

    def test_tool_output_tamper_rejected(self):
        original, _ = self.original()
        (original / "elf-header-stdout.log").write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            self.run_replay(original)

    def test_resealed_invalid_elf_is_not_accepted_as_passed(self):
        original, _ = self.original()
        (original / "elf-header-stdout.log").write_bytes(b"bad")
        self.reseal(original)
        with self.assertRaisesRegex(ValueError, "ELF64"):
            self.run_replay(original)

    def test_resealed_false_derived_rejected(self):
        original, _ = self.original()
        derived = replay.load(original / "derived.json")
        derived["actual_object_count"] = 999
        self.json(original / "derived.json", derived)
        self.reseal(original)
        with self.assertRaisesRegex(ValueError, "derived"):
            self.run_replay(original)

    def test_resealed_float_derived_count_rejected(self):
        original, _ = self.original()
        derived = replay.load(original / "derived.json")
        derived["actual_object_count"] = 8.0
        self.json(original / "derived.json", derived)
        self.reseal(original)
        with self.assertRaisesRegex(ValueError, "derived"):
            self.run_replay(original)

    def test_resealed_integer_false_audit_rejected(self):
        original, _ = self.original()
        result = replay.load(original / "audit.json")
        result["production_admitted"] = 0
        self.json(original / "audit.json", result)
        self.reseal(original)
        with self.assertRaisesRegex(ValueError, "audit result"):
            self.run_replay(original)

    def test_resealed_float_audit_count_rejected(self):
        original, _ = self.original()
        result = replay.load(original / "audit.json")
        result["actual_object_count"] = 8.0
        self.json(original / "audit.json", result)
        self.reseal(original)
        with self.assertRaisesRegex(ValueError, "audit result"):
            self.run_replay(original)

    def test_resealed_float_original_native_count_rejected(self):
        original, _ = self.original()
        result = replay.load(original / "audit.json")
        result["native_tool_commands_started"] = 4.0
        self.json(original / "audit.json", result)
        self.reseal(original)
        with self.assertRaisesRegex(ValueError, "original audit"):
            self.run_replay(original)

    def test_nonzero_original_exit_rejected(self):
        original, _ = self.original()
        command = replay.load(original / "elf-header-command.json")
        command["actual_exit"] = 1
        self.json(original / "elf-header-command.json", command)
        self.reseal(original)
        with self.assertRaisesRegex(ValueError, "command differs or failed"):
            self.run_replay(original)

    def test_original_timeout_rejected(self):
        original, _ = self.original()
        command = replay.load(original / "elf-header-command.json")
        command["timed_out"] = True
        self.json(original / "elf-header-command.json", command)
        self.reseal(original)
        with self.assertRaisesRegex(ValueError, "command differs or failed"):
            self.run_replay(original)

    def test_external_actual_object_mutation_rejected(self):
        original, _ = self.original()
        (self.build / self.object_names[0]).write_bytes(b"changed object")
        with self.assertRaisesRegex(ValueError, "external input changed"):
            self.run_replay(original)

    def test_resealed_argv_mutation_rejected(self):
        original, _ = self.original()
        command = replay.load(original / "elf-header-command.json")
        command["arguments"][1] = "-d"
        self.json(original / "elf-header-command.json", command)
        self.reseal(original)
        with self.assertRaisesRegex(ValueError, "command differs or failed"):
            self.run_replay(original)

    def test_extra_or_traversal_inventory_rejected(self):
        original, _ = self.original()
        for name in ("extra", "../outside"):
            receipt = replay.load(original / "receipt.json")
            receipt["files"][name] = {"sha256": "0" * 64, "size": 0}
            self.json(original / "receipt.json", receipt)
            with self.assertRaisesRegex(ValueError, "inventory"):
                self.run_replay(original)

    def test_duplicate_receipt_keys_rejected(self):
        original, _ = self.original()
        receipt = original / "receipt.json"
        receipt.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.run_replay(original)

    def test_old_or_overlapping_output_rejected(self):
        original, pin = self.original()
        _, receipt = replay.inspect_receipt(original / "receipt.json", pin)
        for output in (original, original / "child", self.build / "child", self.base):
            with self.assertRaises(ValueError):
                replay.new_output(output, original / "receipt.json", receipt)

    def test_original_input_path_link_rejected(self):
        original, _ = self.original()
        target = self.build / self.object_names[0]
        real_lstat = Path.lstat
        class Attributes:
            st_file_attributes = 0x400
        def lstat(path, *args, **kwargs):
            return Attributes() if path == target else real_lstat(path, *args, **kwargs)
        with patch.object(Path, "is_symlink", return_value=False), patch.object(Path, "lstat", lstat):
            with self.assertRaisesRegex(ValueError, "reparse"):
                self.run_replay(original)

    def test_cli_failure_preserves_original(self):
        original, pin = self.original()
        before = replay.bindings(original)
        with patch.object(audit.subprocess, "run", side_effect=AssertionError("native forbidden")):
            code = replay.main(["--receipt", str(original / "receipt.json"), "--expected-receipt-sha256", pin,
                                "--output", str(original)])
        self.assertEqual(code, 1)
        self.assertEqual(replay.bindings(original), before)


if __name__ == "__main__":
    unittest.main()
