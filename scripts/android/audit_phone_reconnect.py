"""Read archived lifecycle evidence without ADB, model files or build products."""
import argparse
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(directory):
    connection = read(directory / "connection.json")
    require(connection["phone_online"] is True, "No verified online phone")
    require(connection["device"]["hardware_serial"] == "1e7c759e", "Wrong device")
    require(connection["device"]["model"] == "M2011K2C", "Wrong model")
    checked = 0
    for mode in ("full", "kv"):
        folders = sorted((directory / mode / "sessions").iterdir())
        require(len(folders) == 2, "Expected two independent sessions per mode")
        sizes = []
        for folder in folders:
            session = read(folder / "session.json")
            requests = session["requests"]
            sizes.append(len(requests))
            require(session["status"] == "closed", "Session not closed")
            require(session["worker_exit"] == session["adb_launcher_exit"] == 0, "Nonzero exit")
            require(session["owned_forward_removed"] is True, "Forward cleanup unconfirmed")
            require(bool(session["model_before"]) and session["model_before"] == session["model_after"], "Model changed")
            require(session["ready"]["load_count"] == 1, "Unexpected load identity")
            history = []
            for index, item in enumerate(requests):
                if index == 2:
                    resets = session["conversation_resets"]
                    require(len(resets) == 1, "Missing reset acknowledgement")
                    reply = resets[0]["reply"]
                    require(reply["ok"] is True and reply["event"] == "new", "Invalid reset")
                    require(reply["pid"] == session["ready"]["pid"], "Wrong reset worker")
                    require(all(type(reply[k]) is int and reply[k] == 0 for k in
                                ("committed_message_count", "reset_history", "reset_output", "reset_all_seq", "reset_gen_seq")), "Old state after reset")
                    history = []
                req, response = item["request"], item["response"]
                require(req["session_token"] == session["session_id"], "Wrong session token")
                require(req["messages"][:-1] == history and req["messages"][-1]["role"] == "user", "Wrong history")
                require(response["message_count"] == len(req["messages"]), "Wrong input count")
                require(response["committed_message_count"] == len(req["messages"]) + 1, "Wrong committed count")
                require(response["ok"] is True and response["status"] in ("normal_finished", "max_tokens_finished"), "Incomplete response")
                require(response["id"] == req["id"] and response["pid"] == session["ready"]["pid"] and response["load_count"] == 1, "Response identity mismatch")
                frames = [f["message"] for f in item["frames"]]
                require(frames and frames[-1] == response, "Final frame mismatch")
                chunks = frames[:-1]
                require(len(chunks) == response["chunk_count"], "Missing chunks")
                for seq, frame in enumerate(chunks):
                    require(frame["event"] == "chunk" and frame["seq"] == seq and frame["id"] == req["id"] and frame["pid"] == response["pid"] and frame["load_count"] == 1, "Invalid chunk")
                require(b"".join(bytes.fromhex(f["bytes_hex"]) for f in chunks).decode("utf-8") == response["text"], "Stream differs from text")
                if mode == "kv" and not history:
                    require(response["reset_applied"] is True and response["reused_tokens"] == 0, "Fresh request reused stale KV")
                history = req["messages"] + [{"role": "assistant", "content": response["text"]}]
                checked += 1
            require(session["conversation"] == history, "Final local history mismatch")
        require(sorted(sizes) == [1, 3], "Incomplete lifecycle workload")
    return {"pass": True, "archived_requests": checked, "sessions": 4,
            "fresh_phone_execution": False,
            "scope": "Read raw archived protocol and lifecycle state; no automatic interrupted-request recovery or speed proof"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.record_dir.resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
