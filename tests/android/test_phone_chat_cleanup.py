"""Offline failure injection for bounded ownership checks and transaction cleanup."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"scripts/android"))
import phone_chat_cleanup as cleanup
import phone_chat_session as full
import phone_chat_kv_session as kv
from test_phone_chat_session import frames as full_frames
from test_phone_chat_kv_session import frames as kv_frames
from test_phone_stream_session import FakeConnection


class Launcher:
    def __init__(self, finished=False): self.finished=finished; self.waits=[]
    def poll(self): return 0 if self.finished else None
    def wait(self, timeout):
        self.waits.append(timeout)
        if not self.finished: raise subprocess.TimeoutExpired("adb",timeout)
        return 0
    def terminate(self): self.finished=True
    def kill(self): self.finished=True


class Connection(FakeConnection):
    def settimeout(self, value): self.timeout=value


class CleanupTests(unittest.TestCase):
    def make(self, module, folder, factory):
        s=module.Session("offline",Path(folder));s.record["ready"]={"pid":123,"port":4444}
        s.socket=Connection(factory);return s

    def test_interrupt_after_visible_chunk_does_not_commit_or_resubmit(self):
        for module,factory in ((full,full_frames),(kv,kv_frames)):
            with self.subTest(module=module.__name__),tempfile.TemporaryDirectory() as d:
                sent=[]
                s=self.make(module,d,lambda request: (sent.append(request),factory(request))[1])
                def interrupt(text): raise KeyboardInterrupt()
                with self.assertRaises(KeyboardInterrupt): s.ask("first",on_text=interrupt)
                self.assertEqual(s.history,[]);self.assertEqual(s.record["requests"],[])
                original=json.dumps(s.record["pending_request"],sort_keys=True)
                for action in (lambda:s.ask("retry"),s.new):
                    with self.assertRaisesRegex(RuntimeError,"会话已中断"): action()
                self.assertEqual(len(sent),1)
                self.assertEqual(original,json.dumps(s.record["pending_request"],sort_keys=True))
                self.assertEqual(s.record["pending_request"]["error_type"],"KeyboardInterrupt")

    def test_nonzero_exit_retains_stdout_and_forward_failure_still_persists(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make(full,d,full_frames);s.transport_uncertain=True;s.socket.close();s.socket=None
            s.launcher=Launcher(True);s.launch_file=(s.directory/"launcher.log").open("wb")
            s.launch_file.write(("OWNED_EXIT_"+s.id+"=6\n").encode());s.launch_file.flush()
            s.forward=(5555,4444);calls=[]
            class Adb:
                def __init__(self,session): pass
                def shell(self,command): calls.append(command);return b"ABSENT\n"
                def call(self,*args,**kwargs):
                    calls.append(args)
                    if args[:2]==("forward","--list"): raise RuntimeError("offline")
                    return b"logcat"
                def pull_verified(self,remote,destination): Path(destination).write_bytes(b"exit6");return {"bytes":5}
            with patch.object(cleanup,"CleanupAdb",Adb):
                with self.assertRaises(RuntimeError):s.close()
                count=len(calls)
                with self.assertRaises(RuntimeError):s.close()
                self.assertEqual(count,len(calls))
            record=json.loads((s.directory/"session.json").read_text(encoding="utf-8"))
            self.assertEqual(record["worker_exit"],6)
            self.assertEqual((s.directory/"worker-stdout.log").read_bytes(),b"exit6")
            self.assertTrue(record["cleanup"]["worker_absence_confirmed"])
            self.assertTrue(any(e["step"]=="owned_forward_remove" for e in record["cleanup"]["errors"]))

    def test_signal_refuses_wrong_identity_and_matches_starttime_in_single_command(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make(full,d,full_frames)
            class Wrong:
                def shell(self,command): return b"/other\n00\n999\n"
            with self.assertRaisesRegex(RuntimeError,"拒绝信号"):cleanup.signal_owned(s,Wrong(),"TERM")
            s.record["worker_identity"]={"pid":123,"cwd":s.remote,"cmdline_hex":cleanup.expected_command(s),"starttime":"999"}
            commands=[]
            class Good:
                def shell(self,command):commands.append(command);return b"SIGNAL_EXIT=0\n"
            cleanup.signal_owned(s,Good(),"TERM")
            self.assertEqual(len(commands),1)
            for needle in ("/proc/$p/cwd","/proc/$p/cmdline","/proc/$p/stat","999","kill -TERM $p",s.remote):
                self.assertIn(needle,commands[0])
            self.assertIn(cleanup.expected_command(s),commands[0])

    def test_forward_reassigned_to_other_owner_is_not_removed(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make(full,d,full_frames);s.socket=None;s.record.pop("ready");s.forward=(5555,4444)
            calls=[]
            class Adb:
                def __init__(self,session):pass
                def call(self,*args,**kwargs):calls.append(args);return b"someone-else tcp:5555 tcp:4444\n"
            with patch.object(cleanup,"CleanupAdb",Adb):s.close();s.close()
            self.assertEqual(calls,[("forward","--list")]);self.assertFalse(s.record["owned_forward_removed"])

    def test_cleanup_adb_timeout_preserves_partial_raw_and_real_unknown_exit(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make(full,d,full_frames)
            with patch.object(cleanup.subprocess,"run",side_effect=subprocess.TimeoutExpired("adb",5,output=b"partial",stderr=b"late")) as run:
                with self.assertRaises(subprocess.TimeoutExpired):cleanup.CleanupAdb(s).shell("safe")
            self.assertEqual(run.call_args.kwargs["timeout"],5)
            folder=next((s.directory/"cleanup-evidence").iterdir())
            self.assertEqual((folder/"stdout.bin").read_bytes(),b"partial")
            self.assertEqual((folder/"stderr.bin").read_bytes(),b"late")
            self.assertIsNone(json.loads((folder/"execution.json").read_text())["returncode"])

    def test_term_cleanup_confirms_resources_without_inventing_native_success(self):
        for code in (141,143):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as d:
                s=self.make(kv,d,kv_frames);s.transport_uncertain=True;s.socket=None
                s.record["pending_request"]={"state":"stream_unconfirmed","partial_bytes_hex":"41"}
                s.launcher=Launcher();s.launch_file=(s.directory/"launcher.log").open("wb")
                s.forward=(5555,4444);commands=[];removed=[]
                class Adb:
                    def __init__(self,session):pass
                    def shell(self,command):
                        commands.append(command)
                        if "kill -TERM" in command:
                            s.launcher.finished=True
                            s.launch_file.write(("OWNED_EXIT_"+s.id+"="+str(code)+"\n").encode());s.launch_file.flush()
                            return b"SIGNAL_EXIT=0\n"
                        if "readlink" in command:
                            return (s.remote+"\n"+cleanup.expected_command(s)+"\n999\n").encode()
                        return b"ABSENT\n" if s.launcher.finished else b"PRESENT\n"
                    def call(self,*args,**kwargs):
                        if args[:2]==("forward","--remove"):removed.append(True);return b""
                        if args[:2]==("forward","--list"):
                            return b"" if removed else b"offline tcp:5555 tcp:4444\n"
                        return b""
                    def pull_verified(self,remote,destination):Path(destination).write_bytes(b"stopped");return {"bytes":7}
                with patch.object(cleanup,"CleanupAdb",Adb):s.close();s.close()
                self.assertEqual(s.record["status"],"closed_with_unconfirmed_request")
                self.assertEqual(s.record["worker_exit"],code)
                self.assertTrue(s.record["cleanup"]["resource_cleanup_confirmed"])
                self.assertEqual(s.record["cleanup"]["request_outcome"],"unconfirmed_session_discarded")
                self.assertEqual(s.record["pending_request"]["partial_bytes_hex"],"41")
                self.assertTrue(s.record["cleanup"]["errors"][0]["recovered"])
                self.assertEqual(s.launcher.waits,[3,3])
                self.assertEqual(sum("kill -TERM" in command for command in commands),1)

    def test_dead_local_launcher_does_not_hide_live_owned_worker_before_ready(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make(full,d,full_frames);s.socket=None;s.record.pop("ready");s.transport_uncertain=True
            s.launcher=Launcher(True);s.launch_file=(s.directory/"launcher.log").open("wb")
            s.launch_file.write(("OWNED_PID_"+s.id+"=123\n").encode());s.launch_file.flush()
            signaled=[]
            class Adb:
                def __init__(self,session):pass
                def shell(self,command):
                    if "kill -TERM" in command:signaled.append(command);return b"SIGNAL_EXIT=0\n"
                    if "readlink" in command:return (s.remote+"\n"+cleanup.expected_command(s)+"\n1234\n").encode()
                    return b"ABSENT\n" if signaled else b"PRESENT\n"
                def pull_verified(self,remote,destination):Path(destination).write_bytes(b"stopped");return {"bytes":7}
            with patch.object(cleanup,"CleanupAdb",Adb):s.close()
            self.assertEqual(len(signaled),1)
            self.assertEqual(s.record["worker_identity"]["starttime"],"1234")
            self.assertIsNone(s.record["worker_exit"])
            self.assertTrue(s.record["cleanup"]["resource_cleanup_confirmed"])
            self.assertEqual(s.record["status"],"closed_with_unconfirmed_request")

    def test_socket_close_error_does_not_replace_keyboard_interrupt(self):
        for module,factory in ((full,full_frames),(kv,kv_frames)):
            with self.subTest(module=module.__name__),tempfile.TemporaryDirectory() as d:
                s=self.make(module,d,factory)
                def bad_close():raise RuntimeError("close failed")
                s.socket.close=bad_close
                def interrupt(text):raise KeyboardInterrupt("primary")
                with self.assertRaisesRegex(KeyboardInterrupt,"primary"):s.ask("x",on_text=interrupt)
                self.assertEqual(s.record["pending_request"]["error_type"],"KeyboardInterrupt")
                self.assertEqual(s.record["pending_request"]["socket_close_error"]["error"],"close failed")

    def test_late_launcher_poll_records_actual_exit_after_wait_timeout(self):
        for actual_exit in (0,7):
            with self.subTest(actual_exit=actual_exit),tempfile.TemporaryDirectory() as d:
                s=self.make(full,d,full_frames);s.transport_uncertain=True;s.socket=None
                class LateLauncher(Launcher):
                    def poll(self):return actual_exit if self.finished else None
                    def wait(self,timeout):
                        self.waits.append(timeout)
                        self.finished=True
                        raise subprocess.TimeoutExpired("adb",timeout)
                s.launcher=LateLauncher();s.launch_file=(s.directory/"launcher.log").open("wb")
                s.launch_file.write(("OWNED_EXIT_"+s.id+"=6\n").encode());s.launch_file.flush()
                s.record["model_before"]={"same":"model"}
                class Adb:
                    def __init__(self,session):pass
                    def shell(self,command):return b"ABSENT\n"
                    def call(self,*args,**kwargs):return b""
                    def pull_verified(self,remote,destination):Path(destination).write_bytes(b"exit6");return {"bytes":5}
                def collect_model(adb):
                    # Model collection follows the wait/poll race; final record
                    # must retain the real poll exit, including a nonzero one.
                    s.launcher.finished=True
                    return {"same":"model"}
                with patch.object(cleanup,"CleanupAdb",Adb),patch.object(full,"model_identity",collect_model):s.close()
                self.assertEqual(s.record["adb_launcher_exit"],actual_exit)
                self.assertEqual(s.record["worker_exit"],6)
                self.assertTrue(s.record["cleanup"]["resource_cleanup_confirmed"])
                saved=json.loads((s.directory/"session.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["adb_launcher_exit"],actual_exit)


class CleanupLogTransferTests(unittest.TestCase):
    def transfer(self, directory, replies, destination=None):
        session = SimpleNamespace(directory=Path(directory),
                                  adb=SimpleNamespace(exe="never-run-adb.exe", serial="offline", commands=[]))
        client = cleanup.CleanupAdb(session)
        target = Path(directory) / "worker-stdout.log" if destination is None else destination
        return session, client, target

    def test_exec_out_preserves_non_utf8_bytes_and_three_hashes(self):
        raw = b"\xff\x00\x80\r\nlog\r\n"
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            session, client, target = self.transfer(directory, [])
            replies = [subprocess.CompletedProcess([], 0, (digest + "  /remote/log\n").encode(), b""),
                       subprocess.CompletedProcess([], 0, raw, b""),
                       subprocess.CompletedProcess([], 0, (digest + "  /remote/log\n").encode(), b"")]
            with patch.object(cleanup.subprocess, "run", side_effect=replies):
                result = client.pull_verified("/remote/log", target)
            self.assertEqual(target.read_bytes(), raw)
            self.assertEqual(result, {"remote": "/remote/log", "sha256": digest, "bytes": len(raw)})
            self.assertEqual(session.adb.commands[1], ["exec-out", "cat", "/remote/log"])
            evidence = list((Path(directory) / "cleanup-evidence").iterdir())
            self.assertEqual(len(evidence), 3)
            self.assertTrue(any((entry / "stdout.bin").read_bytes() == raw for entry in evidence))

    def test_hash_change_empty_or_local_mismatch_rejects_and_keeps_evidence(self):
        raw = b"actual\xff"
        digest = hashlib.sha256(raw).hexdigest()
        for payload, after in ((raw, "0" * 64), (b"", digest), (b"other", digest)):
            with self.subTest(payload=payload, after=after), tempfile.TemporaryDirectory() as directory:
                session, client, target = self.transfer(directory, [])
                replies = [subprocess.CompletedProcess([], 0, (digest + " log\n").encode(), b""),
                           subprocess.CompletedProcess([], 0, payload, b""),
                           subprocess.CompletedProcess([], 0, (after + " log\n").encode(), b"")]
                with patch.object(cleanup.subprocess, "run", side_effect=replies):
                    with self.assertRaisesRegex(RuntimeError, "退出日志采集为空/身份不匹配"):
                        client.pull_verified("/remote/log", target)
                self.assertFalse(target.exists())
                self.assertEqual(len(list((Path(directory) / "cleanup-evidence").iterdir())), 3)

    def test_nonzero_exec_out_keeps_raw_and_actual_exit(self):
        digest = hashlib.sha256(b"complete").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            session, client, target = self.transfer(directory, [])
            replies = [subprocess.CompletedProcess([], 0, (digest + " log\n").encode(), b""),
                       subprocess.CompletedProcess([], 7, b"partial\xff", b"read failed")]
            with patch.object(cleanup.subprocess, "run", side_effect=replies):
                with self.assertRaisesRegex(RuntimeError, "cleanup ADB退出7"):
                    client.pull_verified("/remote/log", target)
            self.assertFalse(target.exists())
            evidence = list((Path(directory) / "cleanup-evidence").iterdir())
            failed = [entry for entry in evidence if json.loads((entry / "execution.json").read_text())["returncode"] == 7]
            self.assertEqual(len(failed), 1)
            self.assertEqual((failed[0] / "stdout.bin").read_bytes(), b"partial\xff")
            self.assertEqual((failed[0] / "stderr.bin").read_bytes(), b"read failed")
            self.assertEqual(len(session.adb.commands), 2)

    def test_existing_destination_rejected_before_adb(self):
        with tempfile.TemporaryDirectory() as directory:
            session, client, target = self.transfer(directory, [])
            target.write_bytes(b"original")
            with patch.object(cleanup.subprocess, "run") as run:
                with self.assertRaises(FileExistsError):
                    client.pull_verified("/remote/log", target)
                run.assert_not_called()
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(session.adb.commands, [])


if __name__=="__main__":unittest.main()
