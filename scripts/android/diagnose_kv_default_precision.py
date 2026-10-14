"""Bounded diagnostic: actual-token-prefix candidate versus fresh full-history CPU references.

Changes precision only between profiles; never installs into the accepted release.
Output equality is diagnostic evidence, not a proof of a particular kernel or KV tensor.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

import phone_kv_prefix_session as candidate


def divergence(left, right):
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return {"index": index, "candidate": a, "cold": b}
    if len(left) != len(right):
        index = min(len(left), len(right))
        return {"index": index, "candidate": left[index] if index < len(left) else None,
                "cold": right[index] if index < len(right) else None}
    return None


class ProfileAdb(candidate.SessionAdb):
    precision = "low"
    dynamic_option = None

    def push_verified(self, file, remote):
        if Path(file).name == "config.json":
            config = json.loads(Path(file).read_text(encoding="utf-8"))
            config.update(precision=self.precision, chunk=0)
            if self.dynamic_option is not None:
                config["dynamic_option"] = self.dynamic_option
            candidate.save(file, config)
        return super().push_verified(file, remote)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial")
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--precisions", nargs="+", choices=("low", "high"), default=["low", "high"])
    parser.add_argument("--repeats", type=int, choices=(1, 2), default=2)
    parser.add_argument("--dynamic-option", type=int, choices=(2,))
    args = parser.parse_args()
    directory = args.record_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    candidate.SessionAdb = ProfileAdb
    ProfileAdb.dynamic_option = args.dynamic_option
    sources = ["scripts/android/diagnose_kv_default_precision.py",
               "scripts/android/phone_kv_prefix_session.py", "apps/android/phone_kv_prefix_worker.cpp",
               "apps/android/phone_chat_worker.cpp", "results/android/kv-prefix-fix-20261007/build.json",
               "results/android/kv-prefix-fix-20261007/core-build.json"]
    source_hashes = {}
    for relative in sources:
        source = candidate.ROOT / relative
        target = directory / "snapshots" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        source_hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    comparisons, sessions = [], []
    questions = ("请记住我的名字是小林，只回复：记住了。", "我的名字是什么？只回答名字。")
    full_binary = candidate.ROOT / "build/chat-session-20261007/phone_chat_worker"
    summary = {"scope": "default chunk0 actual-prefix candidate, low/high configuration intervention; no kernel/logit/KV tensor proof",
               "source_hashes": source_hashes, "full_binary_sha256": candidate.sha(full_binary),
               "precisions": args.precisions, "repeats": args.repeats, "dynamic_option": args.dynamic_option,
               "comparisons": comparisons, "sessions": sessions, "fresh_phone_execution": True}
    try:
        for precision in args.precisions:
            ProfileAdb.precision = precision
            for repeat in range(args.repeats):
                group = directory / (precision + "-" + str(repeat))
                group.mkdir()
                session = candidate.Session(args.serial, group / "sessions")
                try:
                    session.start()
                    session.adb.push_verified(full_binary, session.remote + "/full_worker")
                    session.adb.shell("chmod 755 " + session.remote + "/full_worker")
                    config = json.loads((session.directory / "config.json").read_text(encoding="utf-8"))
                    config.update(reuse_kv=False, prompt_cache=False)
                    candidate.save(group / "full-config.json", config)
                    session.adb.push_verified(group / "full-config.json", session.remote + "/full-config.json")
                    for turn, question in enumerate(questions):
                        live = session.ask(question, 64)
                        request = session.record["requests"][-1]["request"]
                        request_path = group / ("full-request-" + str(turn) + ".json")
                        candidate.save(request_path, request)
                        remote = session.remote + "/full-request-" + str(turn) + ".json"
                        session.adb.push_verified(request_path, remote)
                        raw = session.adb.shell("cd " + session.remote + "; ./full_worker full-config.json --cold " + remote + "; code=$?; echo DEVICE_COLD_EXIT=$code", timeout=900)
                        (group / ("full-" + str(turn) + ".log")).write_bytes(raw)
                        cold = [json.loads(line) for line in raw.decode().splitlines() if line.startswith('{"pid":')]
                        device_exits = [line.split("=", 1)[1] for line in raw.decode().splitlines() if line.startswith("DEVICE_COLD_EXIT=")]
                        assert device_exits == ["0"], device_exits
                        assert len(cold) == 1
                        cold = cold[0]
                        candidate.save(group / ("candidate-" + str(turn) + ".json"), live)
                        matches = {key: live[key] == cold[key] for key in ("tokens", "text", "status", "status_code")}
                        full = live["full_tokens"]
                        actual = full[-live["prompt_tokens"]:]
                        before = live["before_history_tokens"]
                        kept = len(full) - len(actual)
                        comparisons.append({"precision": precision, "repeat": repeat, "turn": turn,
                            "cold_device_exit": 0, "dynamic_option": args.dynamic_option,
                            "session_directory": str(session.directory), "request": request, "matches": matches,
                            "first_token_difference": divergence(live["tokens"], cold["tokens"]),
                            "candidate_text": live["text"], "cold_text": cold["text"],
                            "full_tokens": full, "actual_input_tokens_inferred_from_prompt_len": actual,
                            "retained_prefix_length": kept, "retained_prefix_matches_previous_history": before[:kept] == full[:kept],
                            "before_all_seq": live["before_all_seq"], "after_all_seq": live["after_all_seq"],
                            "actual_vector_scope": "derived full suffix from public prompt_len; no KV tensor measurement"})
                        candidate.save(directory / "progress.json", comparisons)
                        print(json.dumps({"precision": precision, "repeat": repeat, "turn": turn,
                                          "matches": matches, "first_token_difference": comparisons[-1]["first_token_difference"]}), flush=True)
                finally:
                    session.close()
                    sessions.append({"precision": precision, "repeat": repeat, "directory": str(session.directory),
                                     "worker_exit": session.record.get("worker_exit"),
                                     "model_unchanged": session.record.get("model_before") == session.record.get("model_after"),
                                     "owned_forward_removed": session.record.get("owned_forward_removed"),
                                     "device": session.record.get("device")})
                    candidate.save(directory / "summary.json", summary)
        summary["status"] = "diagnostic_completed"
    except BaseException as error:
        summary.update(status="failed_retained", error=repr(error))
        raise
    finally:
        candidate.save(directory / "summary.json", summary)


if __name__ == "__main__":
    main()
