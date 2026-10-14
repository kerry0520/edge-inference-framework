"""Validate and summarize six on-device W8 dual-accumulator probe logs.

This reads saved logs only. Parse success is not a performance acceptance.
"""
import argparse
import json
import math
import re
import statistics
from pathlib import Path

ORDER = ("original", "candidate", "candidate", "original")
THREADS = (1, 4, 4, 1, 1, 4)
LINE = re.compile(
    r"repeat=(\d+) mode=(hot|stream) variant=(original|candidate) "
    r"threads=(\d+) elapsed_us=([\d.]+) byte_exact=1"
)


def read_run(folder, index, threads):
    path = folder / f"run{index}-t{threads}.log"
    text = path.read_text(encoding="utf-8-sig")
    if len(re.findall(r"^correctness cases=1008 byte_exact=1 scope=synthetic_original_vs_candidate$", text, re.M)) != 1:
        raise ValueError(f"Missing or duplicate actual correctness count: {path}")
    records = []
    for line in text.splitlines():
        if line.startswith("repeat="):
            match = LINE.fullmatch(line)
            if not match:
                raise ValueError(f"Malformed timing: {line}")
            repeat, mode, variant, observed_threads, elapsed = match.groups()
            elapsed = float(elapsed)
            if not math.isfinite(elapsed) or elapsed <= 0 or int(observed_threads) != threads:
                raise ValueError(f"Invalid timing or thread setting: {line}")
            records.append((int(repeat), mode, variant, elapsed))
    expected = [(repeat, mode, variant) for mode in ("hot", "stream") for repeat in range(5) for variant in ORDER]
    if [row[:3] for row in records] != expected:
        raise ValueError(f"Incomplete or changed experiment order: {path}")
    before = folder / f"run{index}-before.txt"
    after = folder / f"run{index}-after.txt"
    if not before.is_file() or not after.is_file():
        raise ValueError(f"Missing phone-state records: {path}")
    modes = {}
    for mode_index, mode in enumerate(("hot", "stream")):
        rows = records[mode_index * 20:(mode_index + 1) * 20]
        blocks = []
        for start in range(0, 20, 4):
            block = rows[start:start + 4]
            original = (block[0][3] + block[3][3]) / 2
            candidate = (block[1][3] + block[2][3]) / 2
            blocks.append({
                "repeat": block[0][0], "original_us": original,
                "candidate_us": candidate, "candidate_over_original": candidate / original,
                "latency_reduction_percent": 100 * (1 - candidate / original),
            })
        modes[mode] = {
            "original_median_us": statistics.median(row[3] for row in rows if row[2] == "original"),
            "candidate_median_us": statistics.median(row[3] for row in rows if row[2] == "candidate"),
            "median_block_latency_reduction_percent": statistics.median(b["latency_reduction_percent"] for b in blocks),
            "positive_blocks": sum(b["latency_reduction_percent"] > 0 for b in blocks),
            "blocks": blocks,
        }
    return {"run": index, "threads": threads, "log": str(path), "synthetic_cases_passed": 1008, "modes": modes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    runs = [read_run(args.folder, index, threads) for index, threads in enumerate(THREADS, 1)]
    groups = []
    for threads in (1, 4):
        for mode in ("hot", "stream"):
            selected = [run["modes"][mode] for run in runs if run["threads"] == threads]
            reductions = [m["median_block_latency_reduction_percent"] for m in selected]
            groups.append({
                "threads": threads, "mode": mode, "independent_processes": len(selected),
                "per_process_median_latency_reduction_percent": reductions,
                "all_process_medians_positive": all(value > 0 for value in reductions),
            })
    result = {
        "evidence_parse_result": "pass", "runs": runs, "groups": groups,
        "scope": "Synthetic kernel comparison; includes probe thread creation/wait; not model-logits or whole-model acceptance.",
        "interpretation": "Positive latency reduction means lower candidate time. ABBA edge/center averages are descriptive; no causal confidence or automatic adoption.",
        "state_coverage": "Before/after phone-state records only; no runtime frequency sampling in this probe entry.",
    }
    output = args.output or args.folder / "parsed-summary.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"evidence_parse_result": "pass", "groups": groups}, indent=2))


if __name__ == "__main__":
    main()
