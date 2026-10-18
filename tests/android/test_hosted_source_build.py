"""Small mocked tests only: no dependency downloads or native builds."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SOURCE = Path(__file__).resolve().parents[2] / "scripts/android/hosted_source_build.py"
spec = importlib.util.spec_from_file_location("hosted_source_build_under_test", SOURCE)
hosted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted)


class Response(io.BytesIO):
    def geturl(self):
        return "https://example.invalid/asset"


class HostedSourceBuildTests(unittest.TestCase):
    def test_default_plan_makes_no_process_network_or_directory_attempt(self):
        with patch.object(hosted.subprocess, "Popen", side_effect=AssertionError("process attempted")):
            with patch.object(hosted.urllib.request, "urlopen", side_effect=AssertionError("network attempted")):
                with patch.object(Path, "mkdir", side_effect=AssertionError("mkdir attempted")):
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(hosted.main([]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "offline_plan")
        self.assertEqual([result[k] for k in ("network_requests", "subprocesses", "directories_created")], [0, 0, 0])
        self.assertFalse(result["production_admitted"])
        self.assertEqual(result["runtime_validation"], "pending")

    def test_download_good_size_and_digest(self):
        data = b"an explicit dependency"
        spec = {"url": "https://example.invalid/a", "size": len(data),
                "algorithm": "sha256", "digest": hashlib.sha256(data).hexdigest()}
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "asset"
            result = hosted.download(spec, target, opener=lambda *a, **k: Response(data))
            self.assertEqual(result["bytes"], len(data))
            self.assertEqual(target.read_bytes(), data)

    def test_download_rejects_wrong_digest_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "asset"
            spec = {"url": "https://example.invalid/a", "size": 3,
                    "algorithm": "sha256", "digest": "0" * 64}
            with self.assertRaisesRegex(ValueError, "mismatch"):
                hosted.download(spec, target, opener=lambda *a, **k: Response(b"abc"))
            self.assertEqual(target.read_bytes(), b"abc")
            self.assertTrue(Path(str(target) + ".observed.json").is_file())

    def test_download_rejects_truncated_and_oversized(self):
        for data, size in ((b"abc", 4), (b"abc", 2)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as name:
                spec = {"url": "https://example.invalid/a", "size": size,
                        "algorithm": "sha256", "digest": hashlib.sha256(data).hexdigest()}
                with self.assertRaises(ValueError):
                    hosted.download(spec, Path(name) / "asset", opener=lambda *a, **k: Response(data))

    def test_download_failure_prevents_extract_process_and_later_downloads(self):
        manifest = hosted.load_manifest()
        with tempfile.TemporaryDirectory() as name:
            work = Path(name) / "new"
            with patch.object(hosted, "download", side_effect=ValueError("digest mismatch")) as fetch:
                with patch.object(hosted, "unzip") as extract, patch.object(hosted, "command") as process:
                    with self.assertRaises(ValueError):
                        hosted.execute(manifest, work, {"steps": []})
            self.assertEqual(fetch.call_count, 1)
            extract.assert_not_called()
            process.assert_not_called()

    def test_zip_rejects_traversal_absolute_backslash_colon_and_links_before_extract(self):
        for bad in ("../escaped", "/absolute", "a\\bad", "C:bad", "link", "NUL", "a.", "a/../b"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as name:
                archive, destination = Path(name) / "in.zip", Path(name) / "out"
                with zipfile.ZipFile(archive, "w") as z:
                    z.writestr("first-safe.txt", b"must not extract even this")
                    # ZipInfo normalizes backslashes on Windows; inject the raw filename afterwards.
                    info = zipfile.ZipInfo("aXbad" if bad == "a\\bad" else bad)
                    if bad == "link":
                        info.create_system = 3
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(info, b"bad")
                if bad == "a\\bad":
                    archive.write_bytes(archive.read_bytes().replace(b"aXbad", b"a\\bad"))
                with self.assertRaises(ValueError):
                    hosted.unzip(archive, destination)
                self.assertFalse(destination.exists())

    def test_zip_rejects_case_collisions(self):
        with tempfile.TemporaryDirectory() as name:
            archive = Path(name) / "in.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("a", b"1")
                z.writestr("A", b"2")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                hosted.unzip(archive, Path(name) / "out")

    def test_zip_extracts_small_regular_file(self):
        with tempfile.TemporaryDirectory() as name:
            archive, destination = Path(name) / "in.zip", Path(name) / "out"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("sub/a.txt", b"hello")
            hosted.unzip(archive, destination)
            self.assertEqual((destination / "sub/a.txt").read_bytes(), b"hello")

    def test_child_nonzero_saves_actual_exit_and_stops(self):
        class FailedChild:
            def wait(self):
                return 7

        steps = []
        with tempfile.TemporaryDirectory() as name:
            records = Path(name)
            def fake_popen(argv, **kwargs):
                kwargs["stderr"].write(b"compiler failed")
                return FailedChild()
            with self.assertRaisesRegex(RuntimeError, "child failed"):
                hosted.command(records, "build", ["fake", "build"], steps, popen=fake_popen)
            record = json.loads((records / "build-command.json").read_text())
            self.assertEqual(record["actual_exit"], 7)
            self.assertEqual((records / "build-stderr.log").read_bytes(), b"compiler failed")
        self.assertEqual(steps, [{"step": "build", "actual_exit": 7}])

    def test_launch_failure_never_fabricates_exit_zero(self):
        steps = []
        with tempfile.TemporaryDirectory() as name:
            records = Path(name)
            with self.assertRaises(OSError):
                hosted.command(records, "build", ["fake"], steps,
                               popen=lambda *a, **k: (_ for _ in ()).throw(OSError("launch failed")))
            self.assertIsNone(steps[0]["actual_exit"])
            self.assertFalse((records / "build-command.json").exists())
            self.assertIsNone(json.loads((records / "build-started.json").read_text())["actual_exit"])

    def test_failed_build_never_calls_candidate_or_audit(self):
        manifest = hosted.load_manifest()
        manifest["downloads"]["ninja"]["executable_sha256"] = "fake"
        calls = []
        def fake_unzip(archive, destination):
            destination.mkdir()
            if destination.name == "ndk":
                ndk = destination / manifest["downloads"]["ndk"]["directory"]
                ndk.mkdir()
                (ndk / "source.properties").write_text("Pkg.Revision = 30.0.16248370\n")
        def fake_command(records, label, argv, steps):
            calls.append(label)
            if label == "version-cmake":
                return b"cmake version 4.1.1\n"
            if label == "version-ninja":
                return b"1.12.1\n"
            if label.startswith("version-"):
                return b"version-test\n"
            if label == "git-identity":
                return (hosted.COMMIT + "\n").encode()
            if label == "prepare":
                records.parent.joinpath("recipe-records").mkdir()
                records.parent.joinpath("recipe-records/prepared.json").write_text("{}")
            if label == "build":
                raise RuntimeError("build child actual 7")
            return b""
        with tempfile.TemporaryDirectory() as name:
            with patch.object(hosted, "download", return_value={}), patch.object(hosted, "unzip", side_effect=fake_unzip):
                with patch.object(hosted, "sha", return_value="fake"), patch.object(hosted, "command", side_effect=fake_command):
                    with self.assertRaisesRegex(RuntimeError, "actual 7"):
                        hosted.execute(manifest, Path(name) / "new", {"steps": []})
        self.assertEqual(calls[-1], "build")
        self.assertNotIn("candidate", calls)
        self.assertNotIn("audit", calls)

    def test_success_audit_still_pending_and_rejects_admission_or_false_counts(self):
        audit = {"status": "build_audit_passed", "candidate_status": "candidate_pending",
                 "production_admitted": False, "runtime_validation": "pending", "device_commands": 0,
                 "model_requests": 0, "actual_object_count": 565, "archive_member_count": 565,
                 "actual_kleidi_object_count": 101, "native_tool_commands_started": 4}
        hosted.check_audit(audit)
        for key, bad in (("production_admitted", True), ("runtime_validation", "passed"),
                         ("actual_object_count", 0), ("device_commands", False),
                         ("actual_kleidi_object_count", 100)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                hosted.check_audit({**audit, key: bad})

    def test_manifest_distinguishes_official_and_measured_checksums(self):
        manifest = hosted.load_manifest()
        self.assertEqual(manifest["downloads"]["ndk"]["algorithm"], "sha1")
        self.assertIn("Locally measured", manifest["downloads"]["ninja"]["digest_source"])
        self.assertIn("distinct", manifest["tool_combination"])

    def test_failure_tail_exposes_real_inner_error_with_bounded_bytes(self):
        with tempfile.TemporaryDirectory() as name:
            work = Path(name)
            (work / "records").mkdir()
            (work / "recipe-records").mkdir()
            (work / "records/build-stderr.log").write_bytes(b"see preserved compiler output")
            (work / "recipe-records/compile-command.json").write_text('{"actual_exit": 7}')
            (work / "recipe-records/compile-stderr.log").write_bytes(b"discard-me" * 2000 + b"real compiler error")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                hosted.print_failure_tails(work, [{"step": "build", "actual_exit": 1}])
            text = out.getvalue()
            self.assertIn("real compiler error", text)
            self.assertIn("HOSTED_FAILURE_TAIL compile stderr", text)
            self.assertLess(len(text), 8500)
            self.assertNotIn("hosted_build_audit_passed", text)

    def test_failure_tail_ignores_successful_inner_phase(self):
        with tempfile.TemporaryDirectory() as name:
            work = Path(name)
            (work / "recipe-records").mkdir()
            (work / "recipe-records/compile-command.json").write_text('{"actual_exit": 0}')
            (work / "recipe-records/compile-stderr.log").write_bytes(b"not an error")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                hosted.print_failure_tails(work, [{"step": "build", "actual_exit": 1}])
            self.assertNotIn("not an error", out.getvalue())


if __name__ == "__main__":
    unittest.main()
