"""Independently recompute output equality from archived live/cold requests."""
import argparse
import json
import re
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def first_difference(left, right):
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if a != b:
            return {"index": index, "candidate": a, "cold": b}
    return None


def audit(directory, precisions=("low", "high"), dynamic_option=0):
    rows = []
    for precision in precisions:
        for repeat in range(2):
            group = directory / (precision + "-" + str(repeat))
            cfg = read(group / "full-config.json")
            require(cfg["precision"] == precision and cfg.get("chunk", 0) == 0, "Wrong cold profile")
            require(cfg.get("dynamic_option", 0) == dynamic_option, "Wrong cold quantization option")
            folders = list((group / "sessions").iterdir())
            require(len(folders) == 1, "Unexpected session count")
            session = read(folders[0] / "session.json")
            live_cfg = read(folders[0] / "config.json")
            require(live_cfg["precision"] == precision and live_cfg.get("chunk", 0) == 0, "Wrong live profile")
            require(live_cfg.get("dynamic_option", 0) == dynamic_option, "Wrong live quantization option")
            require(session["status"] == "closed" and session["worker_exit"] == session["adb_launcher_exit"] == 0, "Unconfirmed close")
            require(session["model_before"] == session["model_after"] and bool(session["model_before"]), "Model changed")
            require(session["owned_forward_removed"] is True, "Forward cleanup missing")
            require(len(session["requests"]) == 2, "Missing requests")
            for turn, item in enumerate(session["requests"]):
                request = read(group / ("full-request-" + str(turn) + ".json"))
                require(request == item["request"], "Cold and live inputs differ")
                cold_log = (group / ("full-" + str(turn) + ".log")).read_text(encoding="utf-8")
                cold = [json.loads(line) for line in cold_log.splitlines() if line.startswith('{"pid":')]
                require(len(cold) == 1, "Ambiguous cold response")
                cold = cold[0]
                exit_log = cold_log
                if not re.search(r"^DEVICE_COLD_EXIT=", cold_log, re.M):
                    exit_log = (directory / "cold-exit-supplement" / (precision + "-" + str(repeat) + "-" + str(turn) + ".log")).read_text(encoding="utf-8")
                    repeated = [json.loads(line) for line in exit_log.splitlines() if line.startswith('{"pid":')]
                    require(len(repeated) == 1 and repeated[0]["id"] == request["id"], "Wrong supplementary cold request")
                    require(all(repeated[0][k] == cold[k] for k in ("tokens", "text", "status", "status_code")), "Cold rerun differs")
                require(re.findall(r"^DEVICE_COLD_EXIT=(\d+)$", exit_log, re.M) == ["0"], "Cold device exit unconfirmed")
                live = item["response"]
                require(live == read(group / ("candidate-" + str(turn) + ".json")), "Live snapshot differs")
                require(live["id"] == cold["id"] == request["id"], "Wrong request identity")
                require(live["ok"] is True and cold["ok"] is True, "Failed generation")
                require(live["message_count"] == cold["message_count"] == len(request["messages"]), "Wrong history size")
                require(cold["input_tokens"] == live["input_tokens"] == len(live["full_tokens"]), "Different full input")
                kept = len(live["full_tokens"]) - live["prompt_tokens"]
                require(kept >= 0 and live["before_history_tokens"][:kept] == live["full_tokens"][:kept], "Invalid retained prefix")
                rows.append({"precision": precision, "repeat": repeat, "turn": turn,
                             "matches": {k: live[k] == cold[k] for k in ("tokens", "text", "status", "status_code")},
                             "first_token_difference": first_difference(live["tokens"], cold["tokens"]),
                             "retained_tokens": kept})
    return {"audit_pass": True, "comparisons": rows, "fresh_phone_execution": False,
            "scope": "Archived request and output equality; not a logits/KV numerical or performance proof"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--precisions", choices=("low", "high"), nargs="+", default=["low", "high"])
    parser.add_argument("--dynamic-option", choices=(0, 2), type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(audit(args.record_dir.resolve(), args.precisions, args.dynamic_option), ensure_ascii=False))


if __name__ == "__main__":
    main()
