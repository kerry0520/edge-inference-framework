"""Independent offline source-runtime contract tests.

Small nonexecutable fixture bytes replace identities only inside these tests.
No device, model, compiler, or large production binary is used here.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_plan_profile as profile
import phone_chat_plan as plan
import phone_chat_session as full


class SourceBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.payload = {"binary": b"source-built unit fixture, never executable",
                        "worker": b"unit worker", "helper": b"unit helper",
                        "library": b"unit archive, never linked"}
        self.identity = deepcopy(profile.SOURCE_IDENTITY)
        for role, key in (("binary", "binary_sha256"), ("worker", "source_sha256"),
                          ("helper", "helper_sha256"), ("library", "library_sha256")):
            self.identity[key] = hashlib.sha256(self.payload[role]).hexdigest()
        fixture = patch.object(profile, "SOURCE_IDENTITY", self.identity)
        fixture.start(); self.addCleanup(fixture.stop)

    def bundle(self, label="bundle", selected=None):
        selected = selected or profile.SOURCE_PROFILE_ID
        base = self.base / label
        base.mkdir()
        for role in ("binary", "worker", "helper"):
            (base / profile.FILES[role]).write_bytes(self.payload[role])
        for name, document in (("profile.json", profile.profile_document(selected)),
                               (profile.FILES["runtime_build"], profile.runtime_document(selected)),
                               (profile.FILES["provenance"], profile.provenance_document(selected))):
            self.write(base / name, document)
        return base / "profile.json"

    @staticmethod
    def write(path, document):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf8")

    def edit(self, path, mutate):
        document = json.loads(path.read_text(encoding="utf8"))
        mutate(document)
        self.write(path, document)

    def reject_before_adb(self, path):
        with patch.object(full, "SessionAdb") as adb, \
                patch.object(subprocess, "run") as run, \
                patch.object(subprocess, "Popen") as launch:
            with self.assertRaises(ValueError):
                plan.PlanCacheSession(profile_path=path, adb_path="unavailable")
            adb.assert_not_called(); run.assert_not_called(); launch.assert_not_called()

    def test_relocated_source_bundle_constructs_without_any_old_build_reads(self):
        initial = self.bundle("old prefix")
        moved = self.base / "新前缀 空格" / "runtime"
        moved.parent.mkdir()
        shutil.move(str(initial.parent), moved)
        chosen = moved / "profile.json"
        adb = self.base / "adb fixture"
        adb.write_bytes(b"never executed")
        real_bytes, real_text = Path.read_bytes, Path.read_text
        def guard(path):
            target = Path(path).resolve()
            if (target.is_relative_to(profile.ROOT / "build") or
                    target.is_relative_to(profile.ROOT / "results") or target.suffix in (".a", ".lib")):
                raise AssertionError("historical build dependency: " + str(target))
        def read_bytes(path, *args, **kwargs):
            guard(path); return real_bytes(path, *args, **kwargs)
        def read_text(path, *args, **kwargs):
            guard(path); return real_text(path, *args, **kwargs)
        with patch.object(Path, "read_bytes", read_bytes), patch.object(Path, "read_text", read_text), \
                patch.object(plan, "resolve_plan_build", side_effect=AssertionError("legacy resolver")), \
                patch.object(subprocess, "run", side_effect=AssertionError("execution forbidden")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("execution forbidden")):
            result = profile.resolve_profile(chosen)
            session = plan.PlanCacheSession("offline", self.base / "sessions", profile_path=chosen, adb_path=adb)
            self.assertEqual(result["profile_id"], profile.SOURCE_PROFILE_ID)
            self.assertEqual(session.profile, result)
            self.assertEqual(session.binary_path, moved / profile.FILES["binary"])
            self.assertEqual(session.adb.commands, [])

    def test_zero_argument_documents_remain_old_contract(self):
        expected = {"schema_version": 1, "profile_id": profile.PROFILE_ID,
                    "identity": dict(profile.IDENTITY), "files": dict(profile.FILES), "limits": dict(profile.LIMITS)}
        self.assertEqual(profile.profile_document(), expected)
        self.assertEqual(profile.profile_document(), profile.profile_document(profile.PROFILE_ID))
        self.assertEqual(profile.runtime_document(), profile.runtime_document(profile.PROFILE_ID))
        self.assertEqual(profile.provenance_document(), profile.provenance_document(profile.PROFILE_ID))
        self.assertEqual(profile.IDENTITY["binary_sha256"], "ee5ed8d620e00550ada713d3ffce25354c763bda5d34a23897c40315dc2894b5")
        self.assertNotEqual(profile.IDENTITY["binary_sha256"], profile.SOURCE_IDENTITY["binary_sha256"])

    def test_edited_payload_and_self_signed_identity_fail_before_adb(self):
        for role, key in (("binary", "binary_sha256"), ("worker", "source_sha256"), ("helper", "helper_sha256")):
            for resign in (False, True):
                with self.subTest(role=role, resign=resign):
                    path = self.bundle(role + str(resign))
                    target = path.parent / profile.FILES[role]
                    target.write_bytes(b"modified")
                    if resign:
                        self.edit(path, lambda d: d["identity"].update({key: profile.sha(target)}))
                    self.reject_before_adb(path)

    def test_typed_metadata_cross_profile_and_extra_fields_fail_before_adb(self):
        mutations = [lambda d: d.update(schema_version=True), lambda d: d.update(schema_version=1.0),
                     lambda d: d.update(profile_id=profile.PROFILE_ID), lambda d: d.update(profile_id="unregistered"),
                     lambda d: d.update(unsigned="extra"), lambda d: d["limits"].update(chunk=True),
                     lambda d: d["limits"].update(max_context_tokens=512.0),
                     lambda d: d["identity"].update(source_commit="different")]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                path = self.bundle("meta" + str(index)); self.edit(path, mutate)
                self.reject_before_adb(path)
        for role in ("runtime_build", "provenance"):
            with self.subTest(role=role):
                path = self.bundle(role)
                self.edit(path.parent / profile.FILES[role], lambda d: d.update(schema_version=True))
                self.reject_before_adb(path)

    def test_missing_bundle_assets_rejected_before_adb(self):
        for role in profile.FILES:
            with self.subTest(role=role):
                path = self.bundle("missing" + role)
                (path.parent / profile.FILES[role]).rename(path.parent / "retained-original")
                self.reject_before_adb(path)

    def test_source_bundle_rechecked_before_start(self):
        path = self.bundle()
        adb = self.base / "adb"; adb.write_bytes(b"fixture")
        with patch.object(full, "SessionAdb") as factory:
            factory.return_value.commands = []
            session = plan.PlanCacheSession(record_parent=self.base / "sessions", profile_path=path, adb_path=adb)
            (path.parent / profile.FILES["binary"]).write_bytes(b"after constructor")
            with self.assertRaises(ValueError): session.start()
            factory.return_value.select.assert_not_called()

    def source_fixture(self, label="source"):
        root = self.base / label
        stage = root / profile.SOURCE_STAGE
        paths = {"binary": root / "products/worker", "library": root / "products/libMNN.a",
                 "worker": root / "apps/android/phone_chat_plan_worker.cpp",
                 "helper": root / "apps/android/prefill_trunk_reuse.hpp",
                 "built_worker": root / "snapshot/phone_chat_plan_worker.cpp",
                 "built_helper": root / "snapshot/prefill_trunk_reuse.hpp"}
        for role, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.payload[role.removeprefix("built_")])
        build = {key: self.identity[key] for key in ("source_commit", "binary_sha256", "library_sha256",
                                                    "source_sha256", "helper_sha256")}
        build.update(actual_exit=0, exit=0, core_build_actual_exit=0, link_actual_exit=0,
                     core_library_built_from_fresh_objects=True, original_failed_link_preserved=True,
                     variant="full_plan", chunk=32, metrics_version=1, plan_cache_version=1, diagnostic=False,
                     source_path="apps/android/phone_chat_plan_worker.cpp", helper_path="apps/android/prefill_trunk_reuse.hpp",
                     binary_path=str(paths["binary"]), library_path=str(paths["library"]),
                     source_inputs_directory=str(root / "snapshot"))
        documents = {profile.SOURCE_MANIFEST: build,
                     "build-review-v4/build-review.json": {
                         "accepted": True, "checks": {"unit_review": True}, "actual_compiled_object_count": 565,
                         "archive_member_count": 565, "kleidi_actual_object_count": 101,
                         "binary_sha256": self.identity["binary_sha256"], "library_sha256": self.identity["library_sha256"]},
                     "root-phone-acceptance-v1.json": {"accepted": True, "matched_pairs": 3, "successful_requests": 6,
                         "native_processes": 2, "actual_forwards": 62, "full": 54, "trunk": 8, "decode_included_in_full": 48}}
        for relative, document in documents.items(): self.write(stage / relative, document)
        evidence = {relative: profile.sha(stage / relative) for relative in documents}
        mocked = patch.object(profile, "SOURCE_EVIDENCE", evidence)
        mocked.start(); self.addCleanup(mocked.stop)
        self.sync_fixture_evidence(stage)
        return root, stage / profile.SOURCE_MANIFEST, paths

    def sync_fixture_evidence(self, stage):
        # Simulate a deliberately pinned *test* registry, not trust in a supplied manifest.
        for relative in profile.SOURCE_EVIDENCE:
            profile.SOURCE_EVIDENCE[relative] = profile.sha(stage / relative)
        for key, relative in (("source_build_manifest_sha256", profile.SOURCE_MANIFEST),
                              ("build_review_sha256", "build-review-v4/build-review.json"),
                              ("generation_review_sha256", "root-phone-acceptance-v1.json")):
            self.identity[key] = profile.SOURCE_EVIDENCE[relative]

    def test_export_source_accepted_gate_then_runtime_without_build_evidence(self):
        root, manifest, _ = self.source_fixture()
        output = self.base / "export source"
        with patch.object(plan, "resolve_plan_build", side_effect=AssertionError("wrong exporter")), \
                patch.object(subprocess, "run", side_effect=AssertionError("no execution")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("no execution")):
            result = profile.export_source_bundle(output, manifest, root=root)
            receipt = json.loads((output / "export-receipt.json").read_text())
            self.assertEqual(receipt["source_before"], receipt["source_after"])
            self.assertEqual(receipt["model_requests"], 0)
            self.assertIs(receipt["deployment_performed"], False)
            self.assertFalse(any(path.suffix == ".a" for path in output.rglob("*")))
            # Renaming preserves fixture evidence but removes it from original paths.
            root.rename(self.base / "retained-original-build")
            with patch.object(profile, "resolve_source_build", side_effect=AssertionError("runtime consulted build")):
                self.assertEqual(profile.resolve_profile(output / "profile.json"), result)
        with self.assertRaises(FileExistsError): profile.export_source_bundle(output, manifest, root=root)

    def test_unpinned_or_forged_source_evidence_rejected_without_creating_output(self):
        root, manifest, _ = self.source_fixture()
        stage = root / profile.SOURCE_STAGE
        self.edit(stage / "root-phone-acceptance-v1.json", lambda d: d.update(successful_requests=999))
        output = self.base / "must not exist"
        with self.assertRaises(ValueError): profile.export_source_bundle(output, manifest, root=root)
        self.assertFalse(output.exists())
        # Even when only the evidence SHA registry is changed, the fixed identity rejects it.
        profile.SOURCE_EVIDENCE["root-phone-acceptance-v1.json"] = profile.sha(stage / "root-phone-acceptance-v1.json")
        with self.assertRaises(ValueError): profile.resolve_source_build(manifest, root=root)

    def test_source_actual_inputs_and_built_snapshots_tamper_rejected(self):
        root, manifest, paths = self.source_fixture()
        for role, path in paths.items():
            with self.subTest(role=role):
                original = path.read_bytes()
                path.write_bytes(b"tampered input")
                with self.assertRaises(ValueError): profile.resolve_source_build(manifest, root=root)
                path.write_bytes(original)
        self.assertEqual(profile.resolve_source_build(manifest, root=root)["identity"], self.identity)

    def test_source_gate_semantics_check_types_even_with_test_pinned_bytes(self):
        root, manifest, _ = self.source_fixture()
        stage = root / profile.SOURCE_STAGE
        mutations = [(profile.SOURCE_MANIFEST, lambda d: d.update(actual_exit=False)),
                     (profile.SOURCE_MANIFEST, lambda d: d.update(link_actual_exit=0.0)),
                     (profile.SOURCE_MANIFEST, lambda d: d.update(source_commit="other")),
                     (profile.SOURCE_MANIFEST, lambda d: d.update(core_library_built_from_fresh_objects=1)),
                     ("build-review-v4/build-review.json", lambda d: d.update(accepted=1)),
                     ("build-review-v4/build-review.json", lambda d: d.update(actual_compiled_object_count=565.0)),
                     ("build-review-v4/build-review.json", lambda d: d.update(checks={"unit_review": 1})),
                     ("root-phone-acceptance-v1.json", lambda d: d.update(successful_requests=6.0)),
                     ("root-phone-acceptance-v1.json", lambda d: d.update(matched_pairs=True))]
        for relative, mutate in mutations:
            with self.subTest(relative=relative, mutate=mutate):
                path = stage / relative; original = path.read_bytes()
                self.edit(path, mutate); self.sync_fixture_evidence(stage)
                with self.assertRaises(ValueError): profile.resolve_source_build(manifest, root=root)
                path.write_bytes(original); self.sync_fixture_evidence(stage)

    def test_other_source_manifest_rejected_and_failed_export_keeps_original(self):
        root, manifest, paths = self.source_fixture()
        another = root / "same-content-build.json"; another.write_bytes(manifest.read_bytes())
        with self.assertRaises(ValueError): profile.resolve_source_build(another, root=root)
        real_save = profile.save
        def change_during_export(path, data):
            real_save(path, data)
            if Path(path).name == "profile.json": paths["library"].write_bytes(b"changed after export started")
        output = self.base / "preserved failed export"
        with patch.object(profile, "save", change_during_export):
            with self.assertRaises(ValueError): profile.export_source_bundle(output, manifest, root=root)
        self.assertTrue((output / "profile.json").exists())
        self.assertFalse((output / "export-receipt.json").exists())
        with self.assertRaises(FileExistsError): profile.export_source_bundle(output, manifest, root=root)

    def relocated_fixture(self):
        root, manifest, paths = self.source_fixture()
        moved = self.base / "搬移 证据"
        manifest.parents[2].rename(moved)
        assets = self.base / "搬移 资产"
        assets.mkdir()
        paths["binary"].rename(assets / "worker")
        paths["library"].rename(assets / "libMNN.a")
        paths["built_worker"].parent.rename(assets / "snapshot")
        mapping = assets / "map.json"
        self.write(mapping, {"binary": "worker", "library": "libMNN.a",
                             "source_inputs_directory": "snapshot"})
        return root, moved, moved / profile.SOURCE_MANIFEST, mapping

    def test_explicit_relocation_preserves_evidence_and_never_reads_original_paths(self):
        root, records, manifest, mapping = self.relocated_fixture()
        before = {rel: (records / rel).read_bytes() for rel in profile.SOURCE_EVIDENCE}
        actual_bytes, actual_text = Path.read_bytes, Path.read_text
        def guard(path):
            path = Path(path).absolute()
            if any(path.is_relative_to(root / old) for old in (profile.SOURCE_STAGE, "products", "snapshot")):
                raise AssertionError("old path read: " + str(path))
        def read_bytes(path, *args, **kwargs):
            guard(path); return actual_bytes(path, *args, **kwargs)
        def read_text(path, *args, **kwargs):
            guard(path); return actual_text(path, *args, **kwargs)
        with patch.object(Path, "read_bytes", read_bytes), patch.object(Path, "read_text", read_text), \
                patch.object(subprocess, "run", side_effect=AssertionError("no execution")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("no execution")):
            result = profile.export_source_bundle(self.base / "导出 小型测试包", manifest, root,
                                                  records_root=records, artifact_map=mapping)
            self.assertEqual(result["profile_id"], profile.SOURCE_PROFILE_ID)
        self.assertEqual(before, {rel: (records / rel).read_bytes() for rel in profile.SOURCE_EVIDENCE})

    def test_map_requires_exact_keys_and_nonempty_strings(self):
        root, records, manifest, mapping = self.relocated_fixture()
        valid = json.loads(mapping.read_text())
        bad = [{}, {**valid, "trusted": True}, {**valid, "binary": ""},
               {**valid, "library": 1}, {**valid, "source_inputs_directory": "worker"}]
        for document in bad:
            with self.subTest(document=document):
                self.write(mapping, document)
                with self.assertRaises(ValueError):
                    profile.resolve_source_build(manifest, root, records_root=records, artifact_map=mapping)

    def test_relative_map_escape_rejected_but_explicit_absolute_input_allowed(self):
        root, records, manifest, mapping = self.relocated_fixture()
        document = json.loads(mapping.read_text())
        external = self.base / "external-worker"
        external.write_bytes(self.payload["binary"])
        self.write(mapping, {**document, "binary": "../external-worker"})
        with self.assertRaises(ValueError):
            profile.resolve_source_build(manifest, root, records_root=records, artifact_map=mapping)
        self.write(mapping, {**document, "binary": str(external)})
        result = profile.resolve_source_build(manifest, root, records_root=records, artifact_map=mapping)
        self.assertEqual(result["inputs"]["binary"], str(external.resolve()))

    def test_relocated_assets_and_evidence_still_require_pinned_bytes(self):
        root, records, manifest, mapping = self.relocated_fixture()
        for target in (mapping.parent / "worker", records / "root-phone-acceptance-v1.json"):
            original = target.read_bytes()
            target.write_bytes(b"wrong identity")
            with self.assertRaises(ValueError):
                profile.resolve_source_build(manifest, root, records_root=records, artifact_map=mapping)
            target.write_bytes(original)

    def test_pending_new_build_and_wrong_records_location_cannot_be_admitted(self):
        root, records, manifest, mapping = self.relocated_fixture()
        wrong = records / "same-build.json"
        wrong.write_bytes(manifest.read_bytes())
        with self.assertRaises(ValueError):
            profile.resolve_source_build(wrong, root, records_root=records, artifact_map=mapping)
        self.write(manifest, {"runtime_validation": "pending", "compilation_performed": True,
                              "actual_exit": 0, "binary_sha256": self.identity["binary_sha256"]})
        with self.assertRaises(ValueError):
            profile.resolve_source_build(manifest, root, records_root=records, artifact_map=mapping)

    def test_relocated_map_rechecked_during_export(self):
        root, records, manifest, mapping = self.relocated_fixture()
        save = profile.save
        def mutate(path, data):
            save(path, data)
            if Path(path).name == "profile.json":
                mapping.write_text(mapping.read_text() + " ", encoding="utf8")
        output = self.base / "retained failed relocation"
        with patch.object(profile, "save", mutate):
            with self.assertRaises(ValueError):
                profile.export_source_bundle(output, manifest, root, records_root=records, artifact_map=mapping)
        self.assertTrue(output.exists())
        self.assertFalse((output / "export-receipt.json").exists())

    def test_link_component_is_rejected_before_reading_mapped_assets(self):
        root, records, manifest, mapping = self.relocated_fixture()
        actual = Path.is_symlink
        def linked(path):
            return Path(path) == mapping.parent or actual(path)
        # Exercise the ancestor policy without requiring OS symlink privileges.
        with patch.object(Path, "is_symlink", linked):
            with self.assertRaisesRegex(ValueError, "链接"):
                profile.resolve_source_build(manifest, root, records_root=records, artifact_map=mapping)

    def test_export_cli_forwards_explicit_relocation_options(self):
        root, records, manifest, mapping = self.relocated_fixture()
        with patch.object(profile, "export_source_bundle", return_value={}) as export, \
                patch("builtins.print"):
            self.assertEqual(profile.main(["export-source", "--source-build", str(manifest),
                "--output", "new-output", "--records-root", str(records), "--artifact-map", str(mapping)]), 0)
        export.assert_called_once_with("new-output", str(manifest), records_root=str(records),
                                       artifact_map=str(mapping))

    def test_default_preserves_historical_intermediate_junction_compatibility(self):
        root, manifest, _ = self.source_fixture()
        original = Path.lstat
        def reparse(path, *args, **kwargs):
            if Path(path) == root / "products":
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", reparse):
            self.assertEqual(profile.resolve_source_build(manifest, root)["identity"], self.identity)

    def test_explicit_map_rejects_ancestor_reparse_using_lstat_not_following_stat(self):
        root, records, manifest, mapping = self.relocated_fixture()
        original = Path.lstat
        def reparse(path, *args, **kwargs):
            if Path(path) == mapping.parent:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", reparse):
            with self.assertRaisesRegex(ValueError, "链接"):
                profile.resolve_source_build(manifest, root, records_root=records, artifact_map=mapping)


if __name__ == "__main__":
    unittest.main()
