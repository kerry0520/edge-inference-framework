"""Bounded, evidence-preserving cleanup of one chat session; never replay requests."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
import uuid

from phone_chat_transfer import ascii_stage, remove_stage

ADB_TIMEOUT = 5
WAIT_TIMEOUT = 3


class CleanupAdb:
    def __init__(self, session):
        self.session = session

    def call(self, *args, targeted=True, check=True, timeout=ADB_TIMEOUT):
        s = self.session
        argv = [str(s.adb.exe)]
        if targeted and s.adb.serial:
            argv += ["-s", s.adb.serial]
        argv += list(args)
        s.adb.commands.append(list(args))
        folder = s.directory / "cleanup-evidence" / uuid.uuid4().hex
        folder.mkdir(parents=True, exist_ok=False)
        item = {"argv": argv, "timeout_seconds": min(timeout, ADB_TIMEOUT)}
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=item["timeout_seconds"])
            stdout, stderr = result.stdout, result.stderr
            item["returncode"] = result.returncode
        except (Exception, KeyboardInterrupt) as error:
            stdout, stderr = getattr(error, "stdout", None) or b"", getattr(error, "stderr", None) or b""
            item.update(returncode=None, error_type=type(error).__name__, error=str(error))
            raise
        finally:
            (folder / "stdout.bin").write_bytes(stdout)
            (folder / "stderr.bin").write_bytes(stderr)
            (folder / "execution.json").write_text(json.dumps(item, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        if check and result.returncode:
            raise RuntimeError("cleanup ADB退出%d: %s" % (result.returncode, stderr.decode("utf-8", "replace")))
        return stdout

    def shell(self, command, check=True, timeout=ADB_TIMEOUT):
        return self.call("shell", command, check=check, timeout=timeout)

    def pull_verified(self, remote, destination):
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError("保留已有采集原件: " + str(destination))
        stage, parent = ascii_stage("phone-chat-cleanup-")
        staged = stage / "stdout.log"
        # Failed stage is deliberately retained and its location recorded.
        self.session.record["cleanup"]["collection_stage"] = str(stage)
        digest = self.shell("sha256sum " + shlex.quote(remote)).decode().split()[0]
        self.call("pull", remote, str(staged))
        raw = staged.read_bytes()
        if not raw or hashlib.sha256(raw).hexdigest() != digest:
            raise RuntimeError("退出日志采集为空/身份不匹配")
        with destination.open("xb") as output:
            output.write(raw)
        remove_stage(stage, parent, "phone-chat-cleanup-")
        return {"remote": remote, "sha256": digest, "bytes": len(raw)}


def expected_command(session):
    worker = "phone_chat_kv_worker" if session.record["history_mode"] != "full_recompute" else "phone_chat_worker"
    return ("./"+worker+"\0config.json\0--serve\0"+session.id+"\0").encode().hex()


def owned_pid(session):
    pid = session.record.get("ready", {}).get("pid")
    if pid is None and (session.directory/"launcher.log").exists():
        markers = re.findall(r"OWNED_PID_"+session.id+r"=(\d+)",
                             (session.directory/"launcher.log").read_text(encoding="utf-8", errors="replace"))
        if len(markers) == 1:
            pid = int(markers[0])
            session.record["owned_launcher_pid"] = pid
    return pid


def capture_identity(session, adb=None):
    """Record identity only after exact cwd/argv checks; PID alone never authorizes kill."""
    pid = owned_pid(session)
    if type(pid) is not int or pid <= 1:
        return None
    adb = adb or CleanupAdb(session)
    command = ("p="+str(pid)+"; if [ ! -d /proc/$p ]; then echo ABSENT; exit 0; fi; "
               "readlink /proc/$p/cwd; od -An -tx1 /proc/$p/cmdline | tr -d ' \\n'; echo; "
               "awk '{print $22}' /proc/$p/stat")
    raw = adb.shell(command).decode("utf-8", "replace").strip().splitlines()
    if raw == ["ABSENT"]:
        return None
    if len(raw) != 3 or raw[0] != session.remote or raw[1] != expected_command(session) or not raw[2].isdigit():
        raise RuntimeError("worker cwd/argv/session身份不匹配；拒绝信号")
    identity = {"pid": pid, "cwd": raw[0], "cmdline_hex": raw[1], "starttime": raw[2]}
    session.record["worker_identity"] = identity
    return identity


def signal_owned(session, adb, signal):
    identity = session.record.get("worker_identity")
    if not identity:
        identity = capture_identity(session, adb)
    if not identity:
        return "ABSENT"
    # Recheck all identity fields in the same shell immediately before signaling.
    command = ("p=%d; if [ ! -d /proc/$p ]; then echo ABSENT; exit 0; fi; " % identity["pid"] +
               "cwd=$(readlink /proc/$p/cwd); argv=$(od -An -tx1 /proc/$p/cmdline | tr -d ' \\n'); "
               "stamp=$(awk '{print $22}' /proc/$p/stat); "
               "if [ \"$cwd\" = %s ] && [ \"$argv\" = %s ] && [ \"$stamp\" = %s ]; then " %
               tuple(shlex.quote(identity[k]) for k in ("cwd", "cmdline_hex", "starttime")) +
               "kill -%s $p; code=$?; echo SIGNAL_EXIT=$code; exit $code; " % signal +
               "else echo IDENTITY_MISMATCH; exit 42; fi")
    return adb.shell(command).decode("utf-8", "replace").strip()


def close_session(session, exchange, model_identity):
    s = session
    if getattr(s, "_close_finished", False):
        if getattr(s, "_close_error", None):
            raise RuntimeError(s._close_error)
        return
    cleanup = s.record.setdefault("cleanup", {"started_at_unix": time.time(), "errors": []})
    adb = CleanupAdb(s)

    def attempt(label, action):
        try:
            return action()
        except (Exception, KeyboardInterrupt) as error:
            cleanup["errors"].append({"step": label, "error_type": type(error).__name__, "error": str(error)})
            return None

    def shutdown():
        if s.transport_uncertain:
            return  # Single-accept worker: no reconnect, cancel frame, or request replay.
        if not s.socket and s.record.get("ready"):
            capture_identity(s, adb)
            if not s.record.get("worker_identity"):
                return
            if not s.forward:
                port = s.record["ready"]["port"]
                local = int(adb.call("forward", "tcp:0", "tcp:"+str(port)).decode().strip())
                s.forward = (local, port)
            s.socket = socket.create_connection(("127.0.0.1", s.forward[0]), timeout=3)
        if s.socket:
            s.socket.settimeout(3)
            reply = exchange(s.socket, {"session_token": s.id, "command": "shutdown"})
            if reply.get("event") != "shutdown" or reply.get("pid") != s.record["ready"]["pid"] or reply.get("ok") is not True:
                raise RuntimeError("shutdown响应身份不匹配")
            s.record["shutdown"] = reply

    def close_socket():
        if s.socket:
            connection, s.socket = s.socket, None
            connection.close()

    def wait_launcher():
        if s.launcher:
            s.record["adb_launcher_exit"] = s.launcher.wait(timeout=WAIT_TIMEOUT)

    def stopped():
        pid = owned_pid(s)
        if type(pid) is not int or pid <= 1:
            return s.launcher is None
        raw = adb.shell("if [ -d /proc/%d ]; then echo PRESENT; else echo ABSENT; fi" % pid).decode().strip()
        cleanup["worker_absence_confirmed"] = raw == "ABSENT"
        return raw == "ABSENT"

    try:
        attempt("shutdown", shutdown)
        attempt("socket_close", close_socket)
        attempt("launcher_wait", wait_launcher)
        # The local ADB process may exit while its remote child survives.
        # Independently inspect and stop the precisely owned remote process.
        worker_stopped = attempt("worker_absence_before_fallback", stopped)
        if worker_stopped is not True:
            for signal in ("TERM", "KILL"):
                result = attempt("owned_signal_"+signal, lambda: signal_owned(s, adb, signal))
                cleanup["signal_"+signal] = result
                attempt("launcher_wait_after_"+signal, wait_launcher)
                if attempt("worker_absence_after_"+signal, stopped) is True:
                    break
        if s.launcher:
            if s.launcher.poll() is None:
                attempt("local_launcher_terminate", s.launcher.terminate)
                attempt("local_launcher_wait", wait_launcher)
                if s.launcher.poll() is None:
                    attempt("local_launcher_kill", s.launcher.kill)
                    attempt("local_launcher_final_wait", wait_launcher)
        if s.launcher:
            attempt("launch_file_close", lambda: s.launch_file.close())
            def collect_exit():
                raw = (s.directory/"launcher.log").read_text(encoding="utf-8", errors="replace")
                matches = re.findall(r"OWNED_EXIT_"+s.id+r"=(\d+)", raw)
                s.record["worker_exit"] = int(matches[-1]) if matches else None
                # Never treat an ADB launcher exit or successful kill as native exit 0.
            attempt("native_exit_record", collect_exit)
            result = attempt("stdout_collection", lambda: adb.pull_verified(s.remote+"/worker-stdout.log", s.directory/"worker-stdout.log"))
            if result: s.record["stdout_collection"] = result
            if s.record.get("ready"):
                def collect_logcat():
                    raw = adb.call("logcat", "-d", "-v", "threadtime", "--pid="+str(s.record["ready"]["pid"]))
                    with (s.directory/"pid-logcat.log").open("xb") as output: output.write(raw)
                attempt("pid_logcat", collect_logcat)
            attempt("worker_absence", stopped)
        if "model_before" in s.record:
            def check_model():
                s.record["model_after"] = model_identity(adb)
                if s.record["model_after"] != s.record["model_before"]:
                    raise RuntimeError("模型身份改变")
            attempt("model_identity", check_model)
    finally:
        attempt("socket_close_final", close_socket)
        if s.forward:
            def remove_forward():
                local, remote = s.forward
                mapping = adb.call("forward", "--list", targeted=False).decode().splitlines()
                expected = [s.adb.serial, "tcp:"+str(local), "tcp:"+str(remote)]
                if any(line.split() == expected for line in mapping):
                    adb.call("forward", "--remove", "tcp:"+str(local))
                    s.record["owned_forward_removed"] = True
                    # An acknowledged removal alone is not a fresh absence check.
                    mapping = adb.call("forward", "--list", targeted=False).decode().splitlines()
                else: s.record["owned_forward_removed"] = False
                cleanup["owned_forward_absence_confirmed"] = not any(line.split() == expected for line in mapping)
            attempt("owned_forward_remove", remove_forward)
        else:
            cleanup["owned_forward_absence_confirmed"] = True
        s._close_finished = True
        # A timed-out wait can race with the child exiting later during evidence
        # collection. Record the actual final poll value; never invent exit 0.
        launcher_exit = s.launcher.poll() if s.launcher else None
        if launcher_exit is not None:
            s.record["adb_launcher_exit"] = launcher_exit
        # Keep the request error/partial trace and local conversation unchanged.
        unconfirmed = s.transport_uncertain or bool(s.record.get("pending_request"))
        cleanup["request_outcome"] = "unconfirmed_session_discarded" if unconfirmed else "confirmed"
        needs_worker_check = bool(s.launcher or s.record.get("ready") or s.record.get("owned_launcher_pid"))
        resources = cleanup.get("owned_forward_absence_confirmed", False) and (
            not s.launcher or launcher_exit is not None) and (
            not needs_worker_check or cleanup.get("worker_absence_confirmed") is True)
        cleanup["resource_cleanup_confirmed"] = bool(resources)
        cleanup["native_exit"] = s.record.get("worker_exit")
        # Waiting can time out before the bounded fallback succeeds. Preserve it as
        # evidence, but distinguish recovery from an unconfirmed process lifetime.
        for error in cleanup["errors"]:
            if error["step"].startswith("launcher_wait"):
                error["recovered"] = bool(resources)
        evidence_errors = [error for error in cleanup["errors"] if not error.get("recovered")]
        cleanup["evidence_collection_complete"] = not evidence_errors
        native_ok = not s.launcher or s.record.get("worker_exit") == 0
        if not resources:
            s.record["status"] = "shutdown_unconfirmed"
        elif unconfirmed:
            s.record["status"] = "closed_with_unconfirmed_request"
        elif not native_ok or evidence_errors:
            s.record["status"] = "stopped_with_error"
        else:
            s.record["status"] = "closed"
        cleanup["finished_at_unix"] = time.time()
        clean = resources and not evidence_errors and (unconfirmed or native_ok)
        s._close_error = None if clean else "会话收尾存在失败/退出未确认；见cleanup原件"
        s.persist()
    if s._close_error:
        raise RuntimeError(s._close_error)
