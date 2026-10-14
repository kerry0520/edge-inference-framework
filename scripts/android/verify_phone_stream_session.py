"""Two real inputs: original resident API versus streaming API, same fixed library/model."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from phone_session import Session, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial")
    parser.add_argument("--record-tag", default="acceptance-" + uuid.uuid4().hex)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.record_tag):
        parser.error("record-tag must contain only ASCII letters/digits/hyphens/underscores")
    out = ROOT / "results/android/stream-session-20261006/acceptance" / args.record_tag
    out.mkdir(parents=True, exist_ok=False)
    prompts = ["2+10", "请用中文介绍你自己，限一句。"]
    sources = ["apps/android/phone_stream_worker.cpp", "scripts/android/phone_stream_session.py",
               "scripts/android/verify_phone_stream_session.py", "scripts/android/compile_phone_stream_worker.py",
               "scripts/android/phone_session.py", "apps/android/phone_worker.cpp"]
    hashes = {}
    for source in sources:
        path = ROOT / source
        hashes[source] = hashlib.sha256(path.read_bytes()).hexdigest()
        shutil.copyfile(path, out / ("executed-" + path.name))
    baseline = Session(args.serial, out / "baseline")
    baseline.start()
    try:
        references = [baseline.ask(prompt, 64) for prompt in prompts]
    finally:
        baseline.close()
    argv = [sys.executable, str(ROOT / "scripts/android/phone_stream_session.py")]
    if args.serial:
        argv += ["--serial", args.serial]
    result = subprocess.run(argv, input="\n".join(prompts + ["/exit"]) + "\n", text=True,
                            encoding="utf-8", capture_output=True, timeout=900)
    (out / "interactive-stdout.log").write_text(result.stdout, encoding="utf-8")
    (out / "interactive-stderr.log").write_text(result.stderr, encoding="utf-8")
    (out / "interactive-exit.txt").write_text(str(result.returncode) + "\n")
    if result.returncode:
        raise RuntimeError("stream CLI failed; raw preserved")
    match = re.search(r"会话记录: (.+)", result.stderr)
    if not match:
        raise RuntimeError("stream session path missing")
    stream_path = Path(match[1].strip())
    stream = json.loads((stream_path / "session.json").read_text(encoding="utf-8"))
    if stream["status"] != "closed" or stream["worker_exit"] != 0 or not stream["owned_forward_removed"]:
        raise RuntimeError("stream shutdown not verified")
    if len(stream["requests"]) != len(prompts):
        raise RuntimeError("wrong response count")
    pairs = []
    for prompt, reference, entry in zip(prompts, references, stream["requests"]):
        actual = entry["response"]
        if entry["request"]["prompt"] != prompt:
            raise RuntimeError("prompt changed")
        for key in ("text", "tokens", "status", "status_code", "prompt_tokens", "after_all_seq"):
            if reference[key] != actual[key]:
                raise RuntimeError("baseline versus streaming mismatch: " + key)
        chunks = [frame["message"] for frame in entry["frames"] if frame["message"]["event"] == "chunk"]
        raw = b"".join(bytes.fromhex(frame["bytes_hex"]) for frame in chunks)
        if not chunks or raw != actual["text"].encode("utf-8"):
            raise RuntimeError("chunk bytes mismatch")
        if actual["pid"] != stream["ready"]["pid"] or actual["load_count"] != 1:
            raise RuntimeError("stream object identity changed")
        pairs.append({"prompt": prompt, "text": actual["text"], "tokens": actual["tokens"],
                      "status": actual["status"], "chunk_count": len(chunks), "all_equal": True})
    if baseline.record["model_before"] != stream["model_before"] or stream["model_before"] != stream["model_after"]:
        raise RuntimeError("model resource identity mismatch")
    if baseline.record["config_deployed"]["sha256"] != stream["config_deployed"]["sha256"]:
        raise RuntimeError("config identity mismatch")
    for source, digest in hashes.items():
        if hashlib.sha256((ROOT / source).read_bytes()).hexdigest() != digest:
            raise RuntimeError("source changed during execution")
    summary = {"pass": True, "prompts": prompts, "max_new_tokens": 64, "pairs": pairs,
               "baseline_session": str(baseline.directory), "stream_session": str(stream_path),
               "baseline_pid": baseline.record["ready"]["pid"], "stream_pid": stream["ready"]["pid"],
               "source_sha256": hashes, "cli_exit": result.returncode,
               "scope": "streamed response bytes/tokens/state equivalence; no performance or mmap-cache claim"}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
