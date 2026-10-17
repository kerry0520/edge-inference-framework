"""Small synthetic headers only: no real native executable or device evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_source_candidate as candidate


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = b"\xb7\x00"
        payloads = {"binary": header, "library": b"!<arch>\nfixture only",
                    "worker": b"fixture source", "helper": b"fixture helper"}
        self.paths = {}
        for role, content in payloads.items():
            path = self.base / role
            path.write_bytes(content)
            self.paths[role] = path
        fixed = {candidate.WORKER: candidate.sha(self.paths["worker"]),
                 candidate.HELPER: candidate.sha(self.paths["helper"])}
        self.fixed = patch.object(candidate, "CLIENT_SHA", fixed)
        self.fixed.start()
        self.addCleanup(self.fixed.stop)
        self.manifest = self.base / "build.json"

    def document(self, historical=False):
        fields = {**candidate.COMMON, **(candidate.HISTORICAL if historical else candidate.MODERN)}
        data = {key: {str: "fixture", int: 0, bool: False, dict: {}}[kind] for key, kind in fields.items()}
        data.update(source_commit=candidate.COMMIT, diagnostic=False, actual_exit=0,
                    runtime_validation="pending", source_path=candidate.WORKER,
                    helper_path=candidate.HELPER)
        for role, key in candidate.ROLES.items():
            data[key] = candidate.sha(self.paths[role])
        data.update(binary_path=str(self.paths["binary"]), library_path=str(self.paths["library"]))
        if historical:
            data.update(source_build_prepared_sha256="a" * 64)
        else:
            data.update(prepared_sha256="a" * 64, archive_member_count=1,
                        actual_exits={key: 0 for key in ("configure", "compile", "link", "archive-members")})
        return data

    def write(self, data):
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        return candidate.sha(self.manifest)

    def prepare(self, data=None, label="output"):
        expected = self.write(self.document() if data is None else data)
        return candidate.prepare(self.manifest, expected, self.base / label, **self.paths)

    def check_saved(self, result):
        return candidate.inspect(result["candidate_path"], result["candidate_sha256"])

    def test_modern_positive(self):
        result = self.prepare()
        self.assertEqual(self.check_saved(result), result)
        self.assertFalse(result["accepted_profile"])
        self.assertEqual(result["build_proof"], "not_verified")
        self.assertEqual(list((self.base / "output").iterdir()), [Path(result["candidate_path"])])

    def test_historical_positive(self):
        result = self.prepare(self.document(True))
        self.assertEqual(self.check_saved(result)["reported_receipt_kind"], "historical_linkfix_receipt")

    def test_prepare_returns_canonical_candidate_path(self):
        # Existing lexical aliases must return the same path as inspect.
        (self.base / "existing parent").mkdir()
        result = self.prepare(label="existing parent/../canonical output")
        self.assertEqual(result["candidate_path"], str((self.base / "canonical output/candidate.json").resolve()))
        self.assertEqual(self.check_saved(result), result)

    def test_output_reparse_parent_rejected_before_creation(self):
        expected = self.write(self.document())
        original_lstat = Path.lstat

        def reparse_parent(path, *args, **kwargs):
            if path == self.base:
                from types import SimpleNamespace
                return SimpleNamespace(st_file_attributes=0x400, st_mode=original_lstat(path).st_mode)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", reparse_parent):
            with self.assertRaisesRegex(ValueError, "output parent"):
                candidate.prepare(self.manifest, expected, self.base / "blocked", **self.paths)
        self.assertFalse((self.base / "blocked").exists())

    def test_wrong_receipt_shapes(self):
        for mutation in (lambda d: d.pop("prepared_sha256"), lambda d: d.update(exit=0),
                         lambda d: d.update(unknown=True)):
            data = self.document()
            mutation(data)
            with self.assertRaises(ValueError):
                self.prepare(data)

    def test_wrong_fixed_declarations(self):
        for key, value in (("source_commit", "b" * 40), ("source_sha256", "a" * 64),
                           ("helper_path", "wrong"), ("diagnostic", True),
                           ("runtime_validation", "accepted")):
            data = self.document()
            data[key] = value
            with self.assertRaises(ValueError):
                self.prepare(data)

    def test_wrong_json_types(self):
        for key, value in (("diagnostic", 0), ("actual_exit", False),
                           ("archive_member_count", True), ("device_commands", False)):
            data = self.document()
            data[key] = value
            with self.assertRaises(ValueError):
                self.prepare(data)

    def test_nested_exit_bool(self):
        data = self.document()
        data["actual_exits"]["compile"] = False
        with self.assertRaises(ValueError):
            self.prepare(data)

    def test_bad_declared_hash(self):
        data = self.document()
        data["binary_sha256"] = "bad"
        with self.assertRaises(ValueError):
            self.prepare(data)

    def test_bad_headers(self):
        for role in ("binary", "library"):
            original = self.paths[role].read_bytes()
            self.paths[role].write_bytes(b"not a valid header")
            with self.assertRaises(ValueError):
                self.prepare()
            self.paths[role].write_bytes(original)

    def test_wrong_architecture(self):
        content = bytearray(self.paths["binary"].read_bytes())
        content[18] = 62
        self.paths["binary"].write_bytes(content)
        with self.assertRaises(ValueError):
            self.prepare()

    def test_wrong_external_manifest_hash(self):
        self.write(self.document())
        with self.assertRaises(ValueError):
            candidate.prepare(self.manifest, "0" * 64, self.base / "output", **self.paths)

    def test_wrong_external_candidate_hash(self):
        result = self.prepare()
        with self.assertRaises(ValueError):
            candidate.inspect(result["candidate_path"], "0" * 64)

    def test_state_forgery_even_with_updated_hash(self):
        result = self.prepare()
        path = Path(result["candidate_path"])
        original = json.loads(path.read_text(encoding="utf-8"))
        for key, value in (("accepted_profile", True), ("device_commands", False),
                           ("status", "accepted"), ("build_proof", "verified")):
            changed = copy.deepcopy(original)
            changed[key] = value
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(ValueError):
                candidate.inspect(path, candidate.sha(path))

    def test_asset_drift(self):
        result = self.prepare()
        for path in self.paths.values():
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.assertRaises(ValueError):
                self.check_saved(result)
            path.write_bytes(original)

    def test_manifest_drift(self):
        result = self.prepare()
        self.manifest.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.check_saved(result)

    def test_existing_output_preserved(self):
        self.prepare()
        original = (self.base / "output/candidate.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual((self.base / "output/candidate.json").read_bytes(), original)

    def test_duplicate_keys(self):
        self.manifest.write_text('{"x":1,"x":2}', encoding="utf-8")
        with self.assertRaises(ValueError):
            candidate.read_pinned(self.manifest, candidate.sha(self.manifest))


if __name__ == "__main__":
    unittest.main()
