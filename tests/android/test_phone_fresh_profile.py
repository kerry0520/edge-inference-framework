"""Offline fixed-fresh admission tests; fixtures are never executable/device evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_plan_profile as profile
import phone_chat_plan as plan


class FreshProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.stage = self.root / profile.FRESH_STAGE
        self.assets = self.base / "assets"
        self.assets.mkdir()
        self.identity = deepcopy(profile.FRESH_IDENTITY)
        self.original_defaults = (profile.profile_document(), profile.runtime_document(),
                                  profile.provenance_document(), profile.profile_document(profile.SOURCE_PROFILE_ID))
        self.paths = {"binary": self.assets / "worker", "library": self.assets / "libMNN.a",
                      "built_worker": self.assets / "snapshot/phone_chat_plan_worker.cpp",
                      "built_helper": self.assets / "snapshot/prefill_trunk_reuse.hpp",
                      "worker": self.root / "apps/android/phone_chat_plan_worker.cpp",
                      "helper": self.root / "apps/android/prefill_trunk_reuse.hpp"}
        for role, path in self.paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("nonexecutable fixture " + role.removeprefix("built_")).encode())
            key = {"binary": "binary_sha256", "library": "library_sha256", "worker": "source_sha256",
                   "helper": "helper_sha256", "built_worker": "source_sha256", "built_helper": "helper_sha256"}[role]
            self.identity[key] = profile.sha(path)
        self.manifest = self.stage / profile.FRESH_MANIFEST
        self.documents = {
            profile.FRESH_MANIFEST: {key: self.identity[key] for key in
                ("source_commit", "binary_sha256", "library_sha256", "source_sha256", "helper_sha256")},
            "build-v3/prepared.json": {"source_commit": self.identity["source_commit"],
                "worker_directory": str(self.assets / "snapshot"), "input_sha256": {
                    "apps/android/phone_chat_plan_worker.cpp": self.identity["source_sha256"],
                    "apps/android/prefill_trunk_reuse.hpp": self.identity["helper_sha256"]}},
            "review-built-v3/build-review.json": {"accepted": True, "checks": {"fixture_review": True},
                "actual_compiled_object_count": 565, "kleidi_actual_object_count": 101, "archive_member_count": 565,
                "binary_sha256": self.identity["binary_sha256"], "library_sha256": self.identity["library_sha256"]},
            "review-device-v2/raw-acceptance.json": {"accepted": True, "native_sessions": 1, "requests": 3,
                "new": 2, "chunks": 12, "actual_forwards": 21, "native_exit": 0, "runner_exit": 0,
                "production_admitted": False, "full": 15, "trunk": 6, "decode_included_in_full": 12,
                "independent_of_candidate_verifier": True, "cleanup_commands_exit0": 11, "worker_absence_raw": 2,
                "binary_sha256": self.identity["binary_sha256"]},
            "root-review-v1/acceptance.json": {"accepted": True, "native_sessions": 1, "requests": 3,
                "new": 2, "chunks": 12, "actual_forwards": 21, "native_exit": 0, "runner_exit": 0,
                "production_admitted": False, "actual_compiled_object_count": 565,
                "kleidi_actual_object_count": 101, "archive_member_count": 565,
                "build_and_generation_chain_verified": True, "post_build_inputs_unchanged": True,
                "root_direct_session_review": True, "full_logits_verified": False,
                "stable_performance_claim": False, "candidate_status": "candidate_pending"}}
        self.documents[profile.FRESH_MANIFEST].update(actual_exit=0, compilation_performed=True,
            archive_member_count=565, diagnostic=False, source_path="apps/android/phone_chat_plan_worker.cpp",
            helper_path="apps/android/prefill_trunk_reuse.hpp", binary_path=str(self.paths["binary"]),
            library_path=str(self.paths["library"]), actual_exits={"configure": 0, "compile": 0, "link": 0,
                                                                  "archive-members": 0})
        self.evidence = {}
        for name, value in (("FRESH_IDENTITY", self.identity), ("FRESH_EVIDENCE", self.evidence)):
            mocked = patch.object(profile, name, value)
            mocked.start(); self.addCleanup(mocked.stop)
        self.sync()

    def sync(self):
        # Deliberately re-pin test fixtures to exercise semantics beyond SHA checks.
        for relative, document in self.documents.items():
            if relative != profile.FRESH_MANIFEST:
                self.write(self.stage / relative, document)
                self.evidence[relative] = profile.sha(self.stage / relative)
        self.documents[profile.FRESH_MANIFEST]["prepared_sha256"] = self.evidence["build-v3/prepared.json"]
        self.write(self.manifest, self.documents[profile.FRESH_MANIFEST])
        self.evidence[profile.FRESH_MANIFEST] = profile.sha(self.manifest)
        for key, relative in (("source_build_manifest_sha256", profile.FRESH_MANIFEST),
                              ("prepared_sha256", "build-v3/prepared.json"),
                              ("build_review_sha256", "review-built-v3/build-review.json"),
                              ("generation_review_sha256", "review-device-v2/raw-acceptance.json"),
                              ("root_review_sha256", "root-review-v1/acceptance.json")):
            self.identity[key] = self.evidence[relative]

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf8")

    def resolve(self, **kwargs):
        return profile.resolve_fresh_build(self.manifest, self.root, **kwargs)

    def test_export_and_relocated_runtime_need_no_historical_inputs(self):
        output = self.base / "导出 包"
        with patch.object(subprocess, "run", side_effect=AssertionError("no execution")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("no execution")):
            result = profile.export_fresh_bundle(output, self.manifest, self.root)
            self.assertEqual(result["profile_id"], profile.FRESH_PROFILE_ID)
            self.assertFalse(list(output.glob("*.a")))
            self.root.rename(self.base / "retained project")
            self.assets.rename(self.base / "retained assets")
            with patch.object(profile, "resolve_fresh_build", side_effect=AssertionError("history consulted")):
                moved = self.base / "搬移 中文 空格"; output.rename(moved)
                adb = self.base / "adb"; adb.write_bytes(b"fixture")
                session = plan.PlanCacheSession(record_parent=self.base / "sessions",
                    profile_path=moved / "profile.json", adb_path=adb)
                self.assertEqual(session.binary_path, moved / profile.FILES["binary"])
                self.assertEqual(session.adb.commands, [])

    def test_old_documents_unchanged(self):
        self.assertEqual(self.original_defaults, (profile.profile_document(), profile.runtime_document(),
            profile.provenance_document(), profile.profile_document(profile.SOURCE_PROFILE_ID)))

    def test_all_five_evidence_tamper_fail_before_output(self):
        for relative in self.evidence:
            with self.subTest(relative=relative):
                path = self.stage / relative; original = path.read_bytes()
                path.write_bytes(original + b" ")
                output = self.base / "must not exist"
                with self.assertRaises(ValueError): profile.export_fresh_bundle(output, self.manifest, self.root)
                self.assertFalse(output.exists()); path.write_bytes(original)

    def test_semantic_types_and_failures_rejected_even_test_repinned(self):
        mutations = [(profile.FRESH_MANIFEST, "actual_exit", False),
                     (profile.FRESH_MANIFEST, "actual_exits", {"configure": 0, "compile": 1, "link": 0, "archive-members": 0}),
                     (profile.FRESH_MANIFEST, "compilation_performed", 1),
                     ("review-built-v3/build-review.json", "checks", {"bad": 1}),
                     ("review-built-v3/build-review.json", "actual_compiled_object_count", 565.0),
                     ("review-device-v2/raw-acceptance.json", "native_exit", 1),
                     ("review-device-v2/raw-acceptance.json", "requests", True),
                     ("root-review-v1/acceptance.json", "accepted", 1),
                     ("root-review-v1/acceptance.json", "production_admitted", True)]
        for relative, key, bad in mutations:
            with self.subTest(relative=relative, key=key):
                original = self.documents[relative][key]
                self.documents[relative][key] = bad; self.sync()
                with self.assertRaises(ValueError): self.resolve()
                self.documents[relative][key] = original; self.sync()

    def test_six_actual_inputs_rechecked_and_library_mutation_retains_failed_export(self):
        for role, path in self.paths.items():
            with self.subTest(role=role):
                original = path.read_bytes(); path.write_bytes(b"tampered")
                with self.assertRaises(ValueError): self.resolve()
                path.write_bytes(original)
        real_save = profile.save
        def mutate(path, data):
            real_save(path, data)
            if Path(path).name == profile.FILES["provenance"]:
                self.paths["library"].write_bytes(b"changed")
        output = self.base / "retained failed export"
        with patch.object(profile, "save", mutate), self.assertRaises(ValueError):
            profile.export_fresh_bundle(output, self.manifest, self.root)
        self.assertTrue((output / profile.FILES["provenance"]).is_file())
        self.assertFalse((output / "profile.json").exists())
        with self.assertRaises(ValueError): profile.resolve_profile(output / "profile.json")
        self.assertFalse((output / "export-receipt.json").exists())
        with self.assertRaises(FileExistsError): profile.export_fresh_bundle(output, self.manifest, self.root)

    def test_relocation_does_not_consult_original_asset_or_evidence_paths(self):
        moved = self.base / "搬移 记录"; self.stage.rename(moved)
        self.stage = moved; self.manifest = moved / profile.FRESH_MANIFEST
        assets = self.base / "搬移 资产"; self.assets.rename(assets)
        mapping = assets / "map.json"
        self.write(mapping, {"binary": "worker", "library": "libMNN.a", "source_inputs_directory": "snapshot"})
        result = self.resolve(records_root=moved, artifact_map=mapping)
        self.assertEqual(result["inputs"]["binary"], str((assets / "worker").resolve()))
        self.assertEqual(result["identity"], self.identity)
        self.write(mapping, {"binary": "../outside", "library": "libMNN.a", "source_inputs_directory": "snapshot"})
        with self.assertRaises(ValueError): self.resolve(records_root=moved, artifact_map=mapping)

    def test_unregistered_candidate_or_self_signed_profile_not_admitted(self):
        output = self.base / "valid"
        profile.export_fresh_bundle(output, self.manifest, self.root)
        path = output / "profile.json"; document = json.loads(path.read_text())
        for mutate in (lambda d: d.update(profile_id="candidate_pending"),
                       lambda d: d["identity"].update(binary_sha256="0" * 64),
                       lambda d: d.update(schema_version=True)):
            edited = deepcopy(document); mutate(edited); self.write(path, edited)
            with self.assertRaises(ValueError): profile.resolve_profile(path)

    def test_cli_forwards_fresh_gate_and_relocation(self):
        with patch.object(profile, "export_fresh_bundle", return_value={}) as export, patch("builtins.print"):
            self.assertEqual(profile.main(["export-fresh", "--source-build", "manifest", "--output", "output",
                "--records-root", "records", "--artifact-map", "map"]), 0)
        export.assert_called_once_with("output", "manifest", records_root="records", artifact_map="map")

    def test_explicit_mapping_rejects_link_components_before_asset_read(self):
        mapping = self.assets / "map.json"
        self.write(mapping, {"binary": "worker", "library": "libMNN.a", "source_inputs_directory": "snapshot"})
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: Path(path) == self.assets or original(path)):
            with self.assertRaisesRegex(ValueError, "链接"):
                self.resolve(artifact_map=mapping)

    def test_wrong_manifest_location_and_missing_or_extra_map_key_rejected(self):
        wrong = self.stage / "same-bytes.json"; wrong.write_bytes(self.manifest.read_bytes())
        with self.assertRaises(ValueError): profile.resolve_fresh_build(wrong, self.root)
        mapping = self.assets / "map.json"
        for data in ({}, {"binary": "worker", "library": "libMNN.a", "source_inputs_directory": "snapshot",
                          "accepted": True}):
            self.write(mapping, data)
            with self.assertRaises(ValueError): self.resolve(artifact_map=mapping)


if __name__ == "__main__": unittest.main()
