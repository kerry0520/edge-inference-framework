"""Read-only, independent verification of PR cache-manifest test and model evidence."""
import hashlib
import json
from pathlib import Path
import re
import struct

from audit_query_scale_candidate import archive

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def suite(directory, name, expected_passed, expected_failed):
    log = (directory / (name + ".log")).read_text(encoding="utf-8")
    matches = re.findall(r"^TEST_CASE=(\{[^\n]+\})", log, re.M)
    assert len(matches) == 1
    counts = json.loads(matches[0])
    assert (counts["passed"], counts["failed"]) == (expected_passed, expected_failed)
    command = read(directory / (name + ".command.json"))
    assert command["exit_code"] == (0 if expected_failed == 0 else 2)
    return {"test": name, "passed": expected_passed, "failed": expected_failed, "exit": command["exit_code"]}


def audit():
    phase = ROOT / "results/android/pr-cache-manifest-20261008"
    validation = phase / "validation-v2"
    source_identity = read(validation / "source-identity.json")
    source = ROOT / "build/mnn-cache-pr-20261002"
    assert all(digest(source / name) == expected for name, expected in source_identity.items())
    tests = [suite(validation, "candidate-core-cpu-mmap_cache", 5, 0),
             suite(validation, "old-pr-core-cpu-mmap_cache", 2, 2)]
    related = phase / "related-tests"
    for name, passed in (("core-file_utils", 1), ("core-buffer_allocator", 1),
                         ("core-cpu_copy_lifetime", 1), ("engine-backend", 3), ("core-cpu-mmap_cache", 5)):
        tests.append(suite(related, name, passed, 0))
    replacement = read(validation / "archive-replace.command.json")["args"]
    candidate = Path(replacement[2])
    baseline = Path(read(validation / "old-archive-members.command.json")["args"][-1])
    before, after = archive(baseline), archive(candidate)
    assert len(before) == len(after) == 462 and before.keys() == after.keys()
    changed = sorted(name for name in before if before[name] != after[name])
    assert changed == ["BufferAllocator.cpp.o", "CPUBackend.cpp.o"]
    model = phase / "model-smoke-v2"
    summary = read(model / "summary.json")
    assert summary["source_identity"] == source_identity and summary["library_sha256"] == digest(candidate)
    assert summary["model_before"] == summary["model_after"]
    assert digest(model / "llm_demo") == summary["binary_sha256"] == summary["remote_binary_sha256"]
    answers = []
    for row in summary["requests"]:
        name = row["phase"]
        assert read(model / (name + ".result.json"))["exit"] == row["exit"] == 0
        log = (model / (name + ".log")).read_text(encoding="utf-8")
        answer = log.split("prompt file is ", 1)[1].split("\n", 1)[1].strip()
        assert answer == row["answer"] and re.search(r"17\s*\+\s*25\s*=\s*42", answer)
        answers.append(answer)
    assert len(answers) == 6 and len(set(answers)) == 1
    reference = read(model / "cold.snapshot.json")
    for name in ("cold", "warm", "uncached", "cached-again", "recovery", "recovery-warm"):
        snapshot = read(model / (name + ".snapshot.json"))
        raw = (model / (name + ".sync.bin")).read_bytes()
        assert len(raw) >= 24 and len(raw) % 8 == 0
        words = struct.unpack("<" + "Q" * (len(raw) // 8), raw)
        assert words[:3] == (0x4d4e4e4d4d415031, 1, len(words) - 3)
        assert snapshot["manifest"]["sizes"] == list(words[3:])
        assert snapshot == reference
        data = [meta for filename, meta in snapshot["files"].items() if not filename.endswith("sync.static")]
        assert len(data) == words[2] == summary["real_model_data_file_count"] == 1
        assert data[0]["size"] == words[3] == 1073741824
    assert summary["damage_index"] == 0 and not summary["later_file_coverage"]
    assert read(model / "truncate-cache.result.json")["exit"] == 0
    assert (model / "truncate-cache.log").read_text().strip().endswith(".static 1")
    return {"pass": True, "source_hashes_match": True, "tests": tests, "changed_library_members": changed,
            "library_sha256": digest(candidate), "model_requests": len(answers), "model_answers_equal": True,
            "cache_snapshots_equal": True, "real_model_cache_file_count": 1,
            "scope": "Limited Android regressions and single-file real-model recovery. Old invalid-marker test stops before mutation loop. No full suite, logits, checksum validity, model identity validation, or concurrency guarantee."}


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, indent=2))
