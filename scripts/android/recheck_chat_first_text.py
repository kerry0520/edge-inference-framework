"""Read-only independent audit of saved CLI receive frames, without producer imports.

Frames are saved decoded receive messages, not an independent TCP capture.
Timestamps share the recorded request monotonic origin, not a native token clock.
"""
import argparse
import codecs
import hashlib
import json
import math
from pathlib import Path
import re


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replay(record):
    def clock(x):
        require(type(x) in (int, float) and math.isfinite(x) and x >= 0, "invalid clock")
        return x
    end = clock(record["elapsed_seconds"])
    frames = record["frames"]
    require(isinstance(frames, list) and frames, "missing frames")
    response = record["response"]
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    first = None
    prior = 0
    output = bytearray()
    chunks = 0
    for i, item in enumerate(frames):
        at = clock(item["elapsed_seconds"])
        require(prior <= at <= end, "clock order/bounds")
        prior = at
        frame = item["message"]
        require(frame["id"] == response["id"], "request identity")
        if frame["event"] == "chunk":
            require(type(frame["seq"]) is int and frame["seq"] == chunks, "chunk order")
            encoded = frame["bytes_hex"]
            require(isinstance(encoded, str) and re.fullmatch(r"(?:[0-9a-f]{2})*", encoded) is not None, "hex")
            raw = bytes.fromhex(encoded)
            output.extend(raw)
            shown = decoder.decode(raw, final=False)
            if shown and first is None:
                first = at
            chunks += 1
        else:
            require(frame["event"] == "final" and i == len(frames)-1 and frame == response, "final identity")
            flushed = decoder.decode(b"", final=True)
            if flushed and first is None:
                first = at
            require(type(frame["chunk_count"]) is int and frame["chunk_count"] == chunks, "chunk count")
            require(bytes(output).decode("utf-8") == frame["text"], "text differs from chunks")
            if "chunk_bytes_sha256" in record:
                require(hashlib.sha256(output).hexdigest() == record["chunk_bytes_sha256"], "output hash")
            return {"client_first_text_seconds": first, "client_final_frame_seconds": at,
                    "client_tail_seconds": None if first is None else at-first,
                    "client_elapsed_seconds": end}
    raise RuntimeError("missing final")


def audit(directory):
    before = read(directory/"source-before.json")
    require(before == read(directory/"source-after.json"), "client changed during run")
    for key, digest in before.items():
        require(sha(directory/"source"/Path(key.replace("\\", "/")).name) == digest, "frozen source hash")
    assets = read(directory/"assets-before.json")
    require(assets == read(directory/"assets-after.json") and len(assets) == 6, "worker/library assets changed")
    completion = read(directory/"completion.json")
    require(completion["pass"] is True and completion["model_requests"] == 4, "four completed requests required")
    rows = []
    for mode in ("full", "kv"):
        for kind, budget in (("short", 8), ("long", 64)):
            stage = directory/(mode+"-"+kind)
            require(read(stage/"execution.json")["actual_exit"] == 0, "actual CLI exit")
            d = stage/"session"
            s = read(d/"session.json")
            require(s["status"] == "closed" and s["worker_exit"] == s["adb_launcher_exit"] == 0, "actual native/launcher exit")
            require(s["device"]["hardware_serial"] == "1e7c759e", "phone identity")
            require(s["model_before"] == s["model_after"] and len(s["model_before"]) == 5, "model identity changed")
            require(s["build"]["binary_sha256"] == s["binary_deployed"]["sha256"], "deployed binary identity")
            asset = next(v for k,v in assets.items() if k.replace("\\", "/").endswith("/"+mode+"_worker"))
            require(s["build"]["binary_sha256"] == asset, "worker differs from phase asset manifest")
            for field, name in (("client_sha256", "phone_chat_session.py" if mode == "full" else "phone_chat_kv_session.py"),
                                ("metrics_client_sha256", "phone_chat_metrics.py"),
                                ("transfer_helper_sha256", "phone_chat_transfer.py"),
                                ("cleanup_helper_sha256", "phone_chat_cleanup.py")):
                digest = next(v for k,v in before.items() if k.replace("\\", "/").endswith("/"+name))
                require(s[field] == digest, "session client/source manifest differs")
            require(len(s["requests"]) == 1, "stats caused extra model request")
            c = s["cleanup"]
            require(all(c.get(k) is True for k in ("resource_cleanup_confirmed", "evidence_collection_complete", "worker_absence_confirmed", "owned_forward_absence_confirmed")), "cleanup incomplete")
            raw = (d/"worker-stdout.log").read_bytes()
            require(sha(d/"worker-stdout.log") == s["stdout_collection"]["sha256"], "native stdout hash")
            events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.startswith("{")]
            require([x.get("event") for x in events] == ["ready", "stopped"], "native lifetime")
            require(events[0] == s["ready"] and events[1]["pid"] == s["ready"]["pid"] and events[1]["exit"] == 0, "native lifetime identity")
            require(re.findall(rb"OWNED_EXIT_"+s["session_id"].encode()+rb"=(\d+)", (d/"launcher.log").read_bytes()) == [b"0"], "native exit marker")
            record = s["requests"][0]
            r = record["response"]
            require(record == read(d/(r["id"]+".json")), "request record mismatch")
            require(r["ok"] is True and r["status"] in ("normal_finished", "max_tokens_finished") and r["tokens"] and r["text"], "response unsuccessful")
            require(record["request"]["max_new_tokens"] == budget, "budget fixture")
            require(s["conversation"] == record["request"]["messages"] + [{"role":"assistant", "content":r["text"]}], "history commit")
            timing = replay(record)
            stats = record["metrics_summary"]
            require("client_text_timing_error" not in stats and stats == s["last_stats"], "summary incomplete")
            require(all(stats[k] == v for k,v in timing.items()), "summary differs from independent replay")
            require(stats["request_id"] == r["id"] and all(stats[k] == v for k,v in r["metrics"].items()), "native counters altered")
            display = (stage/"stdout.bin").read_bytes().decode("utf-8")
            require(display.count("[统计: 最近已确认请求]") == 2, "stats repeat count")
            first = timing["client_first_text_seconds"]
            tail = timing["client_tail_seconds"]
            require(first is not None and tail is not None, "no first text in nonempty fixture")
            for phrase in ("首段非空文字等待 %.3f ms" % (first*1000), "首段到final帧 %.3f ms" % (tail*1000),
                           "请求区间 %.3f ms" % (timing["client_elapsed_seconds"]*1000)):
                require(display.count(phrase) == 2, "printed timing mismatch")
            require(any(x["message"].get("bytes_hex") for x in record["frames"]), "nonempty receive chunk required")
            rows.append({"mode":mode, "kind":kind, "request_id":r["id"], **timing,
                         "input_tokens":r["input_tokens"], "output_tokens":len(r["tokens"]),
                         "record_sha256":sha(d/(r["id"]+".json"))})
    require(len(set(row["request_id"] for row in rows)) == 4, "duplicate request IDs")
    for mode in ("full", "kv"):
        short, long = [r for r in rows if r["mode"] == mode]
        require(long["output_tokens"] > short["output_tokens"], "long output fixture did not extend generation")
    return {"pass":True, "model_requests_checked":len(rows), "stats_displays_checked":8,
            "timings":rows, "scope":"Saved receive JSON frames, not independently captured TCP or native token TTFT"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    print(json.dumps(audit(parser.parse_args().directory), ensure_ascii=False, indent=2))
