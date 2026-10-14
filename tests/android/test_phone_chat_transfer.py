"""Offline regressions for slow uploads, partial timeouts and empty ADB pulls."""
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
from phone_chat_transfer import SessionAdb


class TransferTests(unittest.TestCase):
    def test_chinese_push_uses_verified_ascii_stage_and_cleans_owned_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "中文worker"
            source.write_bytes(b"unchanged binary")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            stages = []
            def run(argv, **kwargs):
                if "push" in argv:
                    upload = Path(argv[-2])
                    self.assertTrue(str(upload).isascii())
                    self.assertEqual(upload.read_bytes(), source.read_bytes())
                    self.assertEqual(upload.resolve().parent.parent, Path(tempfile.gettempdir()).resolve())
                    self.assertTrue(upload.parent.name.startswith("phone-chat-push-"))
                    stages.append(upload.parent)
                    return subprocess.CompletedProcess(argv, 0, b"uploaded", b"")
                return subprocess.CompletedProcess(argv, 0, (digest + " /owned/worker").encode(), b"")
            with patch("phone_chat_transfer.subprocess.run", side_effect=run):
                self.assertEqual(SessionAdb("offline", folder).push_verified(source, "/owned/worker")["sha256"], digest)
            self.assertEqual(source.read_bytes(), b"unchanged binary")
            self.assertFalse(stages[0].exists())

    def test_nonzero_adb_is_command_failure_with_original_output(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("phone_chat_transfer.subprocess.run", return_value=
                       subprocess.CompletedProcess([], 7, b"out", b"connection lost")):
                with self.assertRaisesRegex(RuntimeError, "ADB命令失败"):
                    SessionAdb("offline", folder).call("pull", "/owned/log", "stage")
            evidence = next((Path(folder) / "adb-evidence").iterdir())
            record = json.loads((evidence / "failure.json").read_text())
            self.assertEqual(record["kind"], "adb_command_failure")
            self.assertEqual(record["returncode"], 7)
            self.assertEqual((evidence / "stderr.bin").read_bytes(), b"connection lost")

    def test_push_sha_mismatch_does_not_accept_deployment(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "worker"
            file.write_bytes(b"binary")
            adb = SessionAdb("offline", folder)
            with patch.object(adb, "call", return_value=b""), patch.object(adb, "shell", return_value=b"wrong /owned/worker"):
                with self.assertRaisesRegex(RuntimeError, "部署SHA核对失败"):
                    adb.push_verified(file, "/owned/worker")

    def test_upload_budget_sha_and_stdin(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "worker"
            file.write_bytes(b"binary")
            digest = hashlib.sha256(b"binary").hexdigest()
            adb = SessionAdb("offline", folder)
            with patch("phone_chat_transfer.subprocess.run", side_effect=[
                subprocess.CompletedProcess([], 0, b"uploaded", b""),
                subprocess.CompletedProcess([], 0, (digest + "  /owned/worker\n").encode(), b"")]) as run:
                self.assertEqual(adb.push_verified(file, "/owned/worker")["sha256"], digest)
            self.assertEqual(run.call_args_list[0].kwargs["timeout"], 180)
            self.assertTrue(all(call.kwargs["stdin"] == subprocess.DEVNULL for call in run.call_args_list))

    def test_timeout_keeps_raw_bytes_and_propagates_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            adb = SessionAdb("offline", folder)
            error = subprocess.TimeoutExpired("adb", 180, output=b"partial\xff", stderr=b"error\x00")
            with patch("phone_chat_transfer.subprocess.run", side_effect=error) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    adb.call("push", "worker", "/owned/worker", timeout=180)
            self.assertEqual(run.call_count, 1)
            evidence = next((Path(folder) / "adb-evidence").iterdir())
            self.assertEqual((evidence / "stdout.bin").read_bytes(), b"partial\xff")
            self.assertEqual((evidence / "stderr.bin").read_bytes(), b"error\x00")
            self.assertEqual(json.loads((evidence / "failure.json").read_text())["kind"], "adb_timeout")
            self.assertEqual(json.loads((evidence / "failure.json").read_text())["argv"][-5:],
                             ["-s", "offline", "push", "worker", "/owned/worker"])

    def pull_case(self, payload, expected, timeout=False):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        adb = SessionAdb("offline", folder.name)
        destination = Path(folder.name) / "中文.log"
        stages = []
        def call(*args, **kwargs):
            if args[0] == "shell":
                return (hashlib.sha256(expected).hexdigest() + "  /owned/log\n").encode()
            staged = Path(args[2])
            stages.append(staged.parent)
            self.assertTrue(str(staged).isascii())
            self.assertEqual(kwargs["timeout"], 180)
            staged.write_bytes(payload)
            if timeout:
                raise subprocess.TimeoutExpired("adb", 180, output=b"partial pull")
            return b"pulled"
        with patch.object(adb, "call", side_effect=call):
            if timeout or not payload or payload != expected:
                with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                    adb.pull_verified("/owned/log", destination)
                self.assertFalse(destination.exists())
                self.assertEqual((stages[0] / "worker-stdout.log").read_bytes(), payload)
                self.addCleanup(shutil.rmtree, stages[0])
            else:
                result = adb.pull_verified("/owned/log", destination)
                self.assertEqual(destination.read_bytes(), payload)
                self.assertEqual(result["bytes"], len(payload))
                self.assertFalse(stages[0].exists())
        return adb, destination

    def test_verified_pull_to_chinese_path(self):
        self.pull_case(b"ready\n", b"ready\n")

    def test_empty_pull_is_collection_failure(self):
        self.pull_case(b"", b"ready\n")

    def test_wrong_bytes_are_collection_failure(self):
        self.pull_case(b"partial", b"ready\n")

    def test_pull_timeout_keeps_partial_stage(self):
        self.pull_case(b"partial", b"ready\n", timeout=True)

    def test_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "中文.log"
            destination.write_bytes(b"original")
            with patch.object(SessionAdb, "call") as call:
                with self.assertRaises(FileExistsError):
                    SessionAdb("offline", folder).pull_verified("/owned/log", destination)
                call.assert_not_called()
            self.assertEqual(destination.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
