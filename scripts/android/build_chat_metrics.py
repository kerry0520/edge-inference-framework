"""Build new metrics-only public-API workers; never replace accepted workers or libraries."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ALIAS = Path("C:/Users/32065/AppData/Local/Temp/mnn-android-project-32065-20261006")
COMMIT = "d407447ed56c4121a11ccbd266dc184ca1ead0c2"
BASE_SHA = "ef6397477900b868fc3b4f55b1f95e8b342f110310555705e1ed274b18c90b36"
GUARD_SHA = "5c2d58ee4b2774ec59d0537fad550ffe3109244110fef4b6b6c1017877ca01cf"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-tag", default="worker-build-v1")
    args = parser.parse_args()
    if not args.record_tag.isascii() or not args.record_tag.replace("-", "").replace("_", "").isalnum():
        parser.error("record-tag requires ASCII letters, digits, hyphens or underscores")
    records = ROOT / "results/android/chat-metrics-20261012" / args.record_tag
    records.mkdir(parents=True, exist_ok=False)
    build = ALIAS / "build/chat-metrics-20261012"
    # Fresh evidence and outputs are mandatory; a replay uses its own child directory.
    if args.record_tag != "worker-build-v1":
        build = build / args.record_tag
    build.mkdir(parents=True, exist_ok=False)
    head = subprocess.check_output(["git", "-C", str(ROOT / "build/mnn-android-source"), "rev-parse", "HEAD"]).decode().strip()
    if head != COMMIT:
        raise RuntimeError("Fixed public source HEAD mismatch")
    original_manifest = ROOT / "results/android/chat-session-20261007/build.json"
    original = json.loads(original_manifest.read_text(encoding="utf-8-sig"))
    old_args = original["arguments"]
    source_index = next(i for i, item in enumerate(old_args) if item.endswith("phone_chat_worker.cpp"))
    prefix = old_args[:source_index]
    suffix = old_args[source_index + 1:]
    base = ALIAS / "build/framework-integration-20261006/libMNN-candidate.a"
    guard = ALIAS / "build/kv-chat-delivery-20261007/libMNN-candidate.a"
    immutable = [base, guard, ROOT / "apps/android/phone_chat_worker.cpp", ROOT / "apps/android/phone_chat_kv_worker.cpp",
                 ROOT / "build/chat-session-20261007/phone_chat_worker",
                 ROOT / "build/kv-chat-delivery-20261007/delivery-clean/phone_chat_kv_worker"]
    before = {str(path): sha(path) for path in immutable}
    if sha(base) != BASE_SHA or sha(guard) != GUARD_SHA:
        raise RuntimeError("Accepted libraries differ")
    header = ROOT / "build/mnn-android-source/transformers/llm/engine/include/llm/llm.hpp"
    generation = ROOT / "build/mnn-android-source/transformers/llm/engine/src/speculative_decoding/generate.cpp"
    common = {"source_commit": head, "public_header_sha256": sha(header), "public_ar_generation_sha256": sha(generation),
              "prior_worker_manifest_sha256": sha(original_manifest), "core_not_recompiled": True,
              "metrics_version": 1, "existing_worker_not_overwritten": True}
    variants = [("full", "phone_chat_metrics_worker.cpp", base, BASE_SHA, 0),
                ("kv", "phone_chat_kv_metrics_worker.cpp", guard, GUARD_SHA, 32),
                ("full32", "phone_chat_metrics_worker.cpp", guard, GUARD_SHA, 32)]
    summaries = []
    for name, source_name, library, library_sha, chunk in variants:
        source = ALIAS / "apps/android" / source_name
        binary = build / (name + "_worker")
        adapted = list(suffix)
        archive_index = adapted.index("-Wl,--whole-archive") + 1
        adapted[archive_index] = str(library)
        adapted[adapted.index("-o") + 1] = str(binary)
        command = [*prefix, str(source), *adapted]
        save(records / (name + "-command.json"), command)
        result = subprocess.run(command, cwd=ROOT, capture_output=True, stdin=subprocess.DEVNULL)
        (records / (name + "-stdout.log")).write_bytes(result.stdout)
        (records / (name + "-stderr.log")).write_bytes(result.stderr)
        (records / (name + "-exit.code")).write_text(str(result.returncode) + "\n")
        entry = {**common, "variant": name, "source_path": "apps/android/" + source_name,
                 "source_sha256": sha(source), "library_sha256": library_sha, "arguments": command,
                 "binary_path": str(binary), "chunk": chunk, "exit": result.returncode,
                 "binary_sha256": sha(binary) if binary.exists() else None}
        save(records / (name + "-build.json"), entry)
        summaries.append(entry)
        if result.returncode:
            raise RuntimeError(name + " worker compile failed; new evidence preserved")
    after = {str(path): sha(path) for path in immutable}
    if before != after:
        raise RuntimeError("Accepted source/library/worker changed")
    save(records / "immutable-identities.json", {"before": before, "after": after, "unchanged": True})
    save(records / "summary.json", {"ok": True, "variant_count": 3, "model_requests": 0, "builds": summaries})
    print(json.dumps({"ok": True, "records": str(records), "variants": [row[0] for row in variants]}))


if __name__ == "__main__":
    main()
