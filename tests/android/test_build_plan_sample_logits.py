"""Prepare-only fixture tests; every compiler/archive/source file is synthetic."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/android"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("sample_logits_build", SCRIPTS / "build_plan_sample_logits.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sample-logits-prepare-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

        def file(relative):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
            return path

        self.file = file
        self.source = self.root / "source"
        self.worker = self.root / "worker"
        harness = file("tests/android/plan_sample_logits.cpp")
        self.library = file("archive/libMNN.a")
        self.compiler = file("toolchain/compiler.cmd")
        clang, linker = file("toolchain/clang.exe"), file("toolchain/ld.lld.exe")
        source_file = file("source/example.cpp")
        boundary = file("source/public-boundary.hpp")
        file("worker/prefill_trunk_reuse.hpp")
        helper = file("apps/android/prefill_trunk_reuse.hpp")
        (self.worker / "prefill_trunk_reuse.hpp").write_bytes(helper.read_bytes())
        recipe_file = file("scripts/android/recipe.py")
        file("configs/android/mnn-source-build/lock.json")
        source_sha = {"example.cpp": audit.sha(source_file)}
        client_sha = {"apps/android/prefill_trunk_reuse.hpp": audit.sha(helper)}
        self.patch_recipe = patch.multiple(audit.recipe, SOURCE_SHA=source_sha, CLIENT_SHA=client_sha,
                                           PATCHES={}, OPTIONS={}, __file__=str(recipe_file))
        self.patch_recipe.start(); self.addCleanup(self.patch_recipe.stop)
        self.patch_audit = patch.multiple(audit, ROOT=self.root, HARNESS=harness,
                                         BOUNDARY_SOURCE_SHA={"public-boundary.hpp": audit.sha(boundary)})
        self.patch_audit.start(); self.addCleanup(self.patch_audit.stop)
        self.prepared_path = file("records/prepared.json")
        prepared = {"source_commit": audit.recipe.COMMIT, "source_sha256": source_sha,
                    "input_sha256": client_sha, "options": {}, "recipe_sha256": audit.sha(recipe_file),
                    "recipe_lock_sha256": audit.sha(self.root / "configs/android/mnn-source-build/lock.json"),
                    "source_directory": str(self.source), "worker_directory": str(self.worker),
                    "dependency_directory": str(self.root / "deps"), "build_directory": str(self.root / "build"),
                    "compiler": {"path": str(self.compiler), "sha256": audit.sha(self.compiler)},
                    "tools": {"clang.exe": {"sha256": audit.sha(clang)}, "ld.lld.exe": {"sha256": audit.sha(linker)}}}
        self.prepared_path.write_text(json.dumps(prepared), encoding="utf-8")
        self.build_path = file("records/build.json")
        build = {"prepared_sha256": audit.sha(self.prepared_path), "source_commit": audit.recipe.COMMIT,
                 "diagnostic": False, "compilation_performed": True, "actual_exit": 0,
                 "actual_exits": {"configure": 0, "compile": 0, "link": 0, "archive-members": 0},
                 "archive_member_count": 1, "library_sha256": audit.sha(self.library),
                 "library_path": str(self.library), "commands": {"link": ["DO-NOT-EXECUTE"]}}
        self.build_path.write_text(json.dumps(build), encoding="utf-8")
        self.args = argparse.Namespace(
            prepared=str(self.prepared_path), expected_prepared_sha256=audit.sha(self.prepared_path),
            build=str(self.build_path), expected_build_sha256=audit.sha(self.build_path),
            library=str(self.library), expected_library_sha256=audit.sha(self.library),
            compiler=str(self.compiler), expected_compiler_sha256=audit.sha(self.compiler),
            expected_harness_sha256=audit.sha(harness), output=str(self.root / "diagnostic"))

    def test_prepare_only_reconstructs_command(self):
        result = audit.prepare(self.args)
        self.assertFalse(result["compilation_performed"])
        self.assertEqual(result["native_tool_commands_started"], 0)
        self.assertIn(str(self.library), result["command"])
        self.assertNotIn("DO-NOT-EXECUTE", result["command"])
        self.assertFalse(Path(result["binary_path"]).exists())
        self.assertEqual(audit.sha(Path(self.args.output) / "plan_sample_logits.cpp"), self.args.expected_harness_sha256)

    def test_changed_boundary_rejected_before_output(self):
        (self.source / "public-boundary.hpp").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "frozen input differs"):
            audit.prepare(self.args)
        self.assertFalse(Path(self.args.output).exists())

    def test_external_hash_and_tool_overlap_rejected(self):
        self.args.expected_build_sha256 = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA differs"):
            audit.prepare(self.args)
        self.args.expected_build_sha256 = audit.sha(self.build_path)
        self.args.output = str(self.compiler.parent / "new-output")
        with self.assertRaisesRegex(ValueError, "overlaps"):
            audit.prepare(self.args)
        self.assertFalse(Path(self.args.output).exists())

    def test_existing_output_and_duplicate_keys_rejected(self):
        Path(self.args.output).mkdir()
        with self.assertRaisesRegex(ValueError, "fresh ASCII"):
            audit.prepare(self.args)
        self.build_path.write_text('{"actual_exit":0,"actual_exit":0}', encoding="utf-8")
        self.args.expected_build_sha256 = audit.sha(self.build_path)
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            audit.prepare(self.args)


if __name__ == "__main__":
    unittest.main()
