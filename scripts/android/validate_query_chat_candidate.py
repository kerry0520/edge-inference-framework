"""Fresh candidate chat lifecycle acceptance, never resume uncertain requests."""
import argparse
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import traceback
from phone_chat_query_session import Session, ROOT, LIBRARY_SHA
from phone_chat_kv_session import ContextBudgetExceeded, save, sha


NAME = "请记住我的名字是小林，只回复：记住了。"
RECALL = "我的名字是什么？只回答名字。"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def cold(s, record, index, directory):
    request_path = directory / ("cold-request-%d.json" % index)
    save(request_path, record["request"])
    remote = s.remote + "/cold-request-%d.json" % index
    request_identity = s.adb.push_verified(request_path, remote)
    marker = "COLD_EXIT_%d=" % index
    command = "cd " + s.remote + "; ./phone_chat_kv_worker config.json --cold " + remote + "; code=$?; echo " + marker + "$code; exit $code"
    args = [str(s.adb.exe), "-s", s.adb.serial, "shell", command]
    p = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=900)
    (directory / ("cold-%d.stdout.log" % index)).write_bytes(p.stdout)
    (directory / ("cold-%d.stderr.log" % index)).write_bytes(p.stderr)
    codes = re.findall(re.escape(marker) + r"(\d+)", p.stdout.decode("utf-8", "replace"))
    evidence = {"args": args, "adb_exit": p.returncode, "remote_exit": int(codes[0]) if len(codes) == 1 else None,
                "request_deployment": request_identity, "worker_source_sha256": s.record["build"]["source_sha256"],
                "worker_binary_sha256": s.record["build"]["binary_sha256"], "library_sha256": LIBRARY_SHA}
    save(directory / ("cold-%d.command.json" % index), evidence)
    require(p.returncode == 0 and evidence["remote_exit"] == 0, "Cold process failed or actual exit missing")
    finals = []
    for line in p.stdout.decode("utf-8").splitlines():
        if line.startswith("{"):
            value = json.loads(line)
            if value.get("event") == "final":
                finals.append(value)
    require(len(finals) == 1, "Expected one cold final")
    result = finals[0]
    require(result.get("ok") is True, "Cold final is not successful")
    fields = ("text", "tokens", "status", "status_code", "input_tokens")
    matches = {key: result.get(key) == record["response"].get(key) for key in fields}
    if "full_input_ids" in record["response"]:
        matches["full_input_ids"] = result.get("full_input_ids") == record["response"]["full_input_ids"]
    save(directory / ("cold-%d.comparison.json" % index), {"final": result, "matches": matches,
         "input_ids_observed": "full_input_ids" in record["response"], "all_logits_observed": False})
    require(all(matches.values()), "Cold vs live response mismatch")
    return result


def run(serial, directory):
    directory.mkdir(parents=True, exist_ok=False)
    for path in (Path(__file__), ROOT / "scripts/android/phone_chat_query_session.py",
                 ROOT / "scripts/android/phone_chat_kv_session.py", Session.worker_source, Session.manifest_path):
        shutil.copyfile(path, directory / path.name)
    s = Session(serial, directory / "sessions")
    summary = {"status": "running", "device_requests_completed": 0, "cold_processes_completed": 0,
               "all_logits_observed": False, "scope": "Fixed Qwen3/Mi11 CPU low attention_mode8 chunk0 candidate"}
    save(directory / "progress.json", summary)
    # ADB pull uses an ASCII destination; preserve the original contents locally afterwards.
    original_call = s.adb.call
    def ascii_call(*args, **kwargs):
        if args and args[0] == "pull" and len(args) == 3:
            with tempfile.TemporaryDirectory(prefix="query-chat-pull-") as temporary:
                target = Path(temporary) / "payload"
                data = original_call("pull", args[1], str(target), **kwargs)
                shutil.copyfile(target, Path(args[2]))
                return data
        return original_call(*args, **kwargs)
    s.adb.call = ascii_call
    closed = False
    try:
        s.start()
        require(s.record["build"]["library_sha256"] == LIBRARY_SHA, "Wrong library")
        require(json.loads((s.directory / "config.json").read_text())["attention_mode"] == 8, "Wrong attention mode")
        initial_pid = s.record["ready"]["pid"]
        binary_before = s.adb.shell("sha256sum " + s.remote + "/phone_chat_kv_worker").decode().split()[0]
        require(binary_before == s.record["build"]["binary_sha256"], "Deployment identity mismatch")
        def ask(question, budget=64, compare=True):
            result = s.ask(question, budget)
            record = s.record["requests"][-1]
            summary["device_requests_completed"] += 1
            require(result["pid"] == initial_pid and result["load_count"] == 1, "Worker identity changed")
            if compare:
                cold(s, record, summary["cold_processes_completed"], directory)
                summary["cold_processes_completed"] += 1
            save(directory / "progress.json", summary)
            return result
        first = ask(NAME)
        second = ask(RECALL)
        require(second["reused_tokens"] > 0 and second["prompt_tokens"] < second["input_tokens"], "No actual prefix reuse")
        third = ask("只回答我的名字。", 1)
        summary["budget1_actual_status"] = third["status"]
        before_history = copy.deepcopy(s.history)
        before_requests = len(s.record["requests"])
        s.max_context_tokens = 1
        try:
            s.ask("这题必须被预算拒绝", 64)
        except ContextBudgetExceeded:
            pass
        else:
            raise RuntimeError("Budget rejection was accepted")
        finally:
            s.max_context_tokens = 2048
        require(s.history == before_history and len(s.record["requests"]) == before_requests and s.socket is not None,
                "Budget rejection changed committed history or closed session")
        summary["budget_rejection_preserved_history"] = True
        ask("现在只回答我的名字。", 32)
        s.new()
        reset = s.record["conversation_resets"][-1]["reply"]
        require(all(reset[key] == 0 for key in ("reset_history", "reset_output", "reset_all_seq", "reset_gen_seq")), "Incomplete new reset")
        fresh = ask(NAME)
        require(fresh["reused_tokens"] == 0 and fresh["message_count"] == 1, "New reused old history")
        fields = ["text", "tokens", "status", "status_code", "input_tokens"]
        if "full_input_ids" in first:
            fields.append("full_input_ids")
        summary["new_vs_initial"] = {key: first.get(key) == fresh.get(key) for key in fields}
        require(all(summary["new_vs_initial"].values()), "New A differs from initial A")
        binary_after = s.adb.shell("sha256sum " + s.remote + "/phone_chat_kv_worker").decode().split()[0]
        require(binary_after == binary_before and sha(Path(s.record["build"]["library_path"])) == LIBRARY_SHA, "Artifacts changed")
        s.close()
        closed = True
        require(s.record["worker_exit"] == 0 and s.record["adb_launcher_exit"] == 0 and s.record.get("owned_forward_removed") is True, "Shutdown/forward cleanup not confirmed")
        stops = [json.loads(line) for line in (s.directory / "worker-stdout.log").read_text(encoding="utf-8").splitlines() if line.startswith('{"event":"stopped"')]
        require(len(stops) == 1 and stops[0]["pid"] == initial_pid and stops[0]["exit"] == 0, "Actual stopped event missing")
        require(summary["cold_processes_completed"] >= 3 and summary["device_requests_completed"] > 0, "Empty acceptance")
        summary.update(status="passed", worker_exit=s.record["worker_exit"], adb_launcher_exit=s.record["adb_launcher_exit"],
                       owned_forward_removed=True, model_unchanged=s.record["model_before"] == s.record["model_after"],
                       input_ids_observed="full_input_ids" in first, session_directory=str(s.directory),
                       binary_sha256=binary_after, library_sha256=LIBRARY_SHA)
        require(summary["model_unchanged"], "Model identity changed")
        save(directory / "summary.json", summary)
    except BaseException as error:
        summary.update(status="failed", error=str(error), traceback=traceback.format_exc())
        if not closed:
            try:
                s.close()
            except BaseException as close_error:
                summary["close_error"] = str(close_error)
        summary["worker_exit"] = s.record.get("worker_exit")
        save(directory / "failed-summary.json", summary)
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", required=True)
    p.add_argument("--record-dir", type=Path, default=ROOT / "results/android/query-scale-followup-20261011/chat-device-v1")
    args = p.parse_args()
    run(args.serial, args.record_dir)


if __name__ == "__main__":
    main()
