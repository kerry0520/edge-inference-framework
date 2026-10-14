"""Summarize simpleperf address CSV for the fixed unstripped Android bench.

The address ranges are from llvm-nm on llm_bench_w8_prof_symbols built on
2026-09-28. They are valid only for that exact binary, not arbitrary builds.
"""

import argparse
import csv
import json
from pathlib import Path


DSO = "/data/local/tmp/MNN/llm_bench_w8_prof_symbols"
RANGES = {
    "w8_fp16_kernel": (0x277380, 0x2780C0),
    "w8_single_position_dot_loop": (0x277F78, 0x277F90),
    "w8_single_position_post": (0x277F90, 0x27804C),
    "w4_fp16_kernel": (0x2787C0, 0x279660),
}


def summarize(path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Overhead,Shared Object,"))
    counts = {key: 0 for key in RANGES}
    total = 0
    for row in csv.DictReader(lines[start:]):
        count = int(row["EventCount"])
        total += count
        if row["Shared Object"] != DSO:
            continue
        address = int(row["VaddrInFile"], 16)
        for key, (low, high) in RANGES.items():
            if low <= address < high:
                counts[key] += count
    return {
        "file": str(path),
        "event": path.name.split("-")[2],
        "sampled_event_total": total,
        "ranges": {key: {"count": count, "share_all_events_percent": round(100 * count / total, 3)}
                   for key, count in counts.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_files", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = {"binary": "llm_bench_w8_prof_symbols", "ranges_hex": {
        key: [hex(low), hex(high)] for key, (low, high) in RANGES.items()},
        "profiles": [summarize(path) for path in args.csv_files]}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
