"""Compile only the independent streaming public-API worker, preserving accepted core/library/binary artifacts."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]
ALIAS = Path("C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006")
MANIFEST_SHA = "21dedf6c803a8aa7fa4d48d52f545123ab64c6e23eb114e723faf184477ebfec"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-tag", default="replay-" + uuid.uuid4().hex)
    args = parser.parse_args()
    if not args.record_tag.replace("-", "").replace("_", "").isalnum() or not args.record_tag.isascii():
        parser.error("record-tag requires ASCII letters, digits, hyphens or underscores")
    records = ROOT / "results/android/kv-chat-20261007/compile-replays" / args.record_tag
    records.mkdir(parents=True, exist_ok=False)
    manifest = ROOT / "build/framework-integration-20261006/build.json"
    if sha(manifest) != MANIFEST_SHA:
        raise RuntimeError("Integration manifest identity mismatch")
    config = json.loads(manifest.read_text(encoding="utf-8-sig"))
    repo = ROOT / "build/mnn-android-source"
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    if head != "d407447ed56c4121a11ccbd266dc184ca1ead0c2":
        raise RuntimeError("Fixed source HEAD mismatch")
    library = ALIAS / "build/framework-integration-20261006/libMNN-candidate.a"
    expected = next(r["sha256"] for r in config["artifacts"] if r["name"] == "libMNN-candidate.a")
    if sha(library) != expected:
        raise RuntimeError("Accepted library identity mismatch")
    flags = list(next(r["arguments"] for r in config["drivers"] if r["kind"] == "demo"))
    flags += ["-I" + str(ALIAS / "build/mnn-android-source/transformers/llm/engine/src")]
    source = ALIAS / "apps/android/phone_kv_chat_worker.cpp"
    build = ALIAS / "build/kv-chat-20261007"
    build.mkdir(exist_ok=True)
    replay = build / args.record_tag
    replay.mkdir(exist_ok=False)
    binary = replay / "phone_kv_chat_worker"
    command = [config["compiler"], *flags, "-fPIE", "-pie", "-static-libstdc++", str(source), "-Wl,--whole-archive", str(library), "-Wl,--no-whole-archive", "-pthread", "-llog", "-landroid", "-lm", "-latomic", "-o", str(binary)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True)
    (records / "stdout.log").write_bytes(result.stdout)
    (records / "stderr.log").write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError("Worker compile failed; records preserved")
    if sha(library) != expected:
        raise RuntimeError("Library changed")
    destination = build / "phone_kv_chat_worker"
    if destination.exists():
        if sha(destination) != sha(binary):
            raise RuntimeError("Rebuild differs from accepted worker; preserved separately, no overwrite")
    else:
        shutil.copy(binary, destination)
    (records / "build.json").write_text(json.dumps({"source_commit": head, "source_sha256": sha(source), "library_sha256": expected, "binary_sha256": sha(binary), "arguments": command, "exit": result.returncode, "core_not_recompiled": True, "existing_worker_not_overwritten": True}, indent=2))
    accepted = ROOT / "results/android/kv-chat-20261007/build.json"
    if not accepted.exists():
        accepted.write_text((records / "build.json").read_text())
    else:
        previous = json.loads(accepted.read_text())
        if previous["binary_sha256"] != sha(binary) or previous["source_sha256"] != sha(source):
            raise RuntimeError("Accepted build manifest differs; new record preserved without replacement")
    print(binary)


if __name__ == "__main__":
    main()
