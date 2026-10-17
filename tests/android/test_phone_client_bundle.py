"""Offline bundle fixtures; identities patched only in test, no native/ADB execution."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_client_bundle as bundle
import phone_plan_profile as profile


class ClientBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.runtime = self.base / "输入运行包 空格"
        self.runtime.mkdir()
        self.payload = {"binary": b"fixture ELF never executed", "worker": b"fixture source", "helper": b"fixture helper"}
        identity = deepcopy(profile.IDENTITY)
        for role, key in (("binary", "binary_sha256"), ("worker", "source_sha256"), ("helper", "helper_sha256")):
            identity[key] = hashlib.sha256(self.payload[role]).hexdigest()
        pin = patch.object(profile, "IDENTITY", identity)
        pin.start(); self.addCleanup(pin.stop)
        for name in ("run", "Popen"):
            guard = patch.object(subprocess, name, side_effect=AssertionError("native execution forbidden"))
            guard.start(); self.addCleanup(guard.stop)
        for role, data in self.payload.items():
            (self.runtime / profile.FILES[role]).write_bytes(data)
        for name, doc in (("profile.json", profile.profile_document()),
                          (profile.FILES["runtime_build"], profile.runtime_document()),
                          (profile.FILES["provenance"], profile.provenance_document())):
            (self.runtime / name).write_text(json.dumps(doc), encoding="utf-8")
        self.output = self.base / "输出客户端 空格"

    def export(self):
        return bundle.export_bundle(self.runtime / "profile.json", self.output)

    def inspect(self, pin):
        return bundle.inspect_bundle(self.output, pin)

    def rewrite_manifest(self, mutate):
        path = self.output / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")
        return bundle.digest(path)

    def test_roundtrip_exact_modules_and_runtime_no_process(self):
        result = self.export()
        self.assertEqual(result["client_modules"], 13)
        self.assertEqual(result["runtime_files"], 6)
        self.assertEqual(self.inspect(result["manifest_sha256"])["status"], "verified")
        self.assertEqual(self.inspect(result["manifest_sha256"])["device_commands"], 0)

    def test_move_package_unicode_space(self):
        result = self.export()
        self.output = self.output.rename(self.base / "移动客户端 空格")
        self.assertEqual(self.inspect(result["manifest_sha256"])["status"], "verified")

    def test_wrong_external_pin_rejected(self):
        self.export()
        for pin in ("0" * 64, "A" * 64, "short"):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                self.inspect(pin)

    def test_tampered_client_rejected(self):
        result = self.export()
        (self.output / "scripts/android/phone_chat.py").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.inspect(result["manifest_sha256"])

    def test_runtime_resigning_cannot_admit_new_binary(self):
        self.export()
        target = self.output / "runtime/phone_chat_plan_worker"
        target.write_bytes(b"unknown ELF")
        pin = self.rewrite_manifest(lambda d: d["files"]["runtime/phone_chat_plan_worker"].update(bundle.record(target)))
        with self.assertRaises(ValueError):
            self.inspect(pin)

    def test_missing_module_rejected(self):
        result = self.export()
        (self.output / "scripts/android/phone_chat.py").unlink()
        with self.assertRaises(ValueError):
            self.inspect(result["manifest_sha256"])

    def test_extra_python_or_runtime_rejected(self):
        result = self.export()
        for rel in ("scripts/android/unknown.py", "runtime/unknown.json", "extra.txt"):
            path = self.output / rel
            path.write_bytes(b"extra")
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                self.inspect(result["manifest_sha256"])
            path.unlink()

    def test_operational_results_and_cache_allowed(self):
        result = self.export()
        for rel in ("scripts/android/__pycache__/phone_chat.cpython-311.pyc", "results/android/session/answer.json"):
            path = self.output / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"untrusted runtime output")
        self.assertEqual(self.inspect(result["manifest_sha256"])["status"], "verified")

    def test_manifest_path_escape_rejected_even_with_new_pin(self):
        self.export()
        pin = self.rewrite_manifest(lambda d: d["files"].update({"../outside": {"role": "client", "size": 1, "sha256": "0" * 64}}))
        with self.assertRaises(ValueError):
            self.inspect(pin)

    def test_manifest_duplicate_json_rejected(self):
        self.export()
        path = self.output / "manifest.json"
        path.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "重复"):
            self.inspect(bundle.digest(path))

    def test_runtime_duplicate_json_rejected_before_output(self):
        path = self.runtime / "profile.json"
        original = path.read_text(encoding="utf-8")
        path.write_text('{"schema_version":1,' + original[1:], encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "重复"):
            self.export()
        self.assertFalse(self.output.exists())

    def test_output_existing_or_missing_parent_rejected(self):
        self.output.mkdir()
        with self.assertRaises(ValueError):
            self.export()
        self.output = self.base / "missing" / "new"
        with self.assertRaises(ValueError):
            self.export()

    def test_output_nested_input_or_ancestor_rejected(self):
        for output in (self.runtime / "new", self.runtime.parent):
            self.output = output
            with self.assertRaises(ValueError):
                self.export()

    def test_output_protected_source_rejected(self):
        self.output = Path(bundle.__file__).parent / "never-create-unit-fixture"
        with self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_input_changed_during_copy_no_manifest(self):
        real = bundle.record
        count = {}
        target = self.runtime / "phone_chat_plan_worker"
        def record(path):
            path = Path(path)
            count[path] = count.get(path, 0) + 1
            if path == target and count[path] == 2:
                target.write_bytes(b"changed during copy")
            return real(path)
        with patch.object(bundle, "record", side_effect=record), self.assertRaises(ValueError):
            self.export()
        self.assertFalse((self.output / "manifest.json").exists())

    def test_reparse_attribute_rejected(self):
        target = self.runtime / "profile.json"
        real = Path.lstat
        class Reparse:
            st_mode = 0o100644
            st_file_attributes = 0x400
        def fake(path, *a, **kw):
            return Reparse() if path == target else real(path, *a, **kw)
        with patch.object(Path, "lstat", fake), self.assertRaisesRegex(ValueError, "reparse"):
            bundle.no_links(target)

    def test_manifest_type_confusion_rejected(self):
        self.export()
        pin = self.rewrite_manifest(lambda d: d["files"]["README.md"].update(size=True))
        with self.assertRaises(ValueError):
            self.inspect(pin)

    def test_all_three_profiles_export_and_inspect_with_fixture_identities(self):
        for index, (selected, attribute) in enumerate(((profile.PROFILE_ID, "IDENTITY"),
                (profile.SOURCE_PROFILE_ID, "SOURCE_IDENTITY"), (profile.FRESH_PROFILE_ID, "FRESH_IDENTITY"))):
            identity = deepcopy(getattr(profile, attribute))
            for role, key in (("binary", "binary_sha256"), ("worker", "source_sha256"), ("helper", "helper_sha256")):
                identity[key] = hashlib.sha256(self.payload[role]).hexdigest()
            with self.subTest(selected=selected), patch.object(profile, attribute, identity):
                for name, doc in (("profile.json", profile.profile_document(selected)),
                        (profile.FILES["runtime_build"], profile.runtime_document(selected)),
                        (profile.FILES["provenance"], profile.provenance_document(selected))):
                    (self.runtime / name).write_text(json.dumps(doc), encoding="utf-8")
                self.output = self.base / ("profile-" + str(index))
                result = self.export()
                self.assertEqual(self.inspect(result["manifest_sha256"])["profile_id"], selected)

    def test_all_three_existing_profile_registries_remain_fixed(self):
        self.assertEqual(len({profile.PROFILE_ID, profile.SOURCE_PROFILE_ID, profile.FRESH_PROFILE_ID}), 3)
        self.assertEqual(set(bundle.RUNTIME), {"profile.json", *profile.FILES.values()})
        with self.assertRaises(ValueError):
            profile._identity("new-self-signed-profile")


if __name__ == "__main__":
    unittest.main()
