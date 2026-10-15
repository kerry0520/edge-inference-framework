"""Safety and frozen-input checks for the dedicated clean Android build."""
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import build_mnn_source as recipe


def archive(name, data=b"content", *, kind=tarfile.REGTYPE, target=""):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as stream:
        member = tarfile.TarInfo(name)
        member.type, member.linkname = kind, target
        member.size = len(data) if kind == tarfile.REGTYPE else 0
        stream.addfile(member, io.BytesIO(data) if member.size else None)
    return buffer.getvalue()


class RecipeTests(unittest.TestCase):
    def test_fixed_assets_match_lock(self):
        lock = json.loads((recipe.ROOT / "configs/android/mnn-source-build/lock.json").read_text())
        self.assertEqual(lock["patches"], recipe.PATCHES)
        self.assertEqual(lock["sources"], recipe.SOURCE_SHA)
        self.assertEqual(lock["options"], recipe.OPTIONS)
        for path, expected in {**recipe.PATCHES, **recipe.CLIENT_SHA}.items():
            self.assertEqual(recipe.sha(recipe.ROOT / path), expected)

    def test_archive_unsafe_members_fail_without_write(self):
        cases = [("../escape", tarfile.REGTYPE), ("/abs", tarfile.REGTYPE),
                 ("C:/windows", tarfile.REGTYPE), ("folder\\escape", tarfile.REGTYPE),
                 ("link", tarfile.SYMTYPE), ("link", tarfile.LNKTYPE)]
        with tempfile.TemporaryDirectory() as temp:
            for name, kind in cases:
                with self.subTest(name=name, kind=kind), self.assertRaises(ValueError):
                    recipe.extract(archive(name, kind=kind, target="../outside"), temp)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_regular_archive_extracts_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            recipe.extract(archive("dep/source.c"), temp)
            self.assertEqual((Path(temp) / "dep/source.c").read_bytes(), b"content")
            with self.assertRaises(FileExistsError):
                recipe.extract(archive("dep/source.c", b"changed"), temp)
            self.assertEqual((Path(temp) / "dep/source.c").read_bytes(), b"content")

    def test_git_link_is_plain_text_and_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            recipe.extract(archive("docs/link", kind=tarfile.SYMTYPE, target="../../outside"), temp, git_archive=True)
            path = Path(temp) / "docs/link"
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_bytes(), b"../../outside")

    def test_failed_compile_keeps_live_log_and_actual_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RuntimeError):
                recipe.compile_logged(temp, [sys.executable, "-c", "import sys;print('diagnostic');sys.exit(3)"])
            self.assertIn(b"diagnostic", (Path(temp) / "compile-stdout.log").read_bytes())
            self.assertEqual(json.loads((Path(temp) / "compile-command.json").read_text())["actual_exit"], 3)

    def test_existing_output_rejected_before_subprocess(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(recipe.subprocess, "run") as proc:
            with self.assertRaises(ValueError):
                recipe.prepare(temp, Path(temp) / "records", "repo", "ndk", "cmake", "ninja", "tar")
            proc.assert_not_called()

    def test_wrong_plan_digest_rejected_before_subprocess(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(recipe.subprocess, "run") as proc:
            path = Path(temp) / "plan.json"
            path.write_text("{}")
            with self.assertRaises(ValueError):
                recipe.build(path, "0" * 64)
            proc.assert_not_called()

    def test_retry_uses_new_objects_and_frozen_inputs(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(recipe.subprocess, "run") as proc:
            root = Path(temp)
            old = root / "old"
            old.mkdir()
            for name in ("source", "deps", "worker", "cmake-build"):
                (old / name).mkdir()
            (old / "cmake-build/old.o").write_bytes(b"old")
            plan = {"output": str(old), "commands": {
                "configure": ["cmake", "-S", str(old / "source"), "-B", str(old / "cmake-build")],
                "compile": ["cmake", "--build", str(old / "cmake-build")],
                "link": ["clang", str(old / "worker/phone_chat_plan_worker.cpp"), str(old / "cmake-build/libMNN.a"), "-o", str(old / "worker/phone_chat_plan_worker")]}}
            p = root / "parent.json"
            p.write_text(json.dumps(plan))
            result = recipe.prepare_retry(p, recipe.sha(p), root / "retry", root / "records")
            self.assertEqual(result["source_directory"], str(old / "source"))
            self.assertFalse(Path(result["build_directory"]).exists())
            self.assertNotIn(str(old / "cmake-build/libMNN.a"), result["commands"]["link"])
            self.assertIn(str(old / "worker/phone_chat_plan_worker.cpp"), result["commands"]["link"])
            self.assertEqual(result["environment"]["TMP"], str(root / "retry/tmp"))
            self.assertEqual((old / "cmake-build/old.o").read_bytes(), b"old")
            with self.assertRaises(ValueError):
                recipe.prepare_retry(p, recipe.sha(p), root / "retry", root / "other")
            proc.assert_not_called()

    def test_rebuild_and_source_tamper_rejected_before_commands(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(recipe.subprocess, "run") as proc:
            root = Path(temp)
            (root / "source").mkdir()
            (root / "deps").mkdir()
            (root / "source/code.cpp").write_text("old")
            plan = {"output": str(root), "records": str(root), "recipe_sha256": recipe.sha(recipe.__file__), "recipe_lock_path": str(recipe.ROOT / "configs/android/mnn-source-build/lock.json"), "recipe_lock_sha256": recipe.sha(recipe.ROOT / "configs/android/mnn-source-build/lock.json"),
                    "prepared_source_tree_sha256": recipe.tree_hash(root / "source"),
                    "prepared_dependency_tree_sha256": recipe.tree_hash(root / "deps")}
            path = root / "prepared.json"
            path.write_text(json.dumps(plan))
            (root / "source/code.cpp").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "source tree changed"):
                recipe.build(path, recipe.sha(path))
            (root / "cmake-build").mkdir()
            with self.assertRaisesRegex(ValueError, "fresh build"):
                recipe.build(path, recipe.sha(path))
            proc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
