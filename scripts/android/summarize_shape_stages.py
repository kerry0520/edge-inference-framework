"""Summarize selected W4/W8 decode shapes from PhoneShapeStages logs.

Fixed experiment: llm_bench p32/g8, n-repeat=1. It performs seven tuning
passes and one warm-up request. The 29th plane=32, 1024->2048 record marks
the measured request. The first W8 output-layer record in that request
belongs to prefill and is excluded from decode aggregation.
"""

import argparse
import json
import re
import statistics
from pathlib import Path


PATTERN = re.compile(
    r"\[PhoneShapeStages\] plane=(\d+) ic=(\d+) oc=(\d+) bits=(\d+) "
    r"split=([^ ]+) pre_us=([\d.]+) parallel_us=([\d.]+) post_us=([\d.]+)"
)


def median(values):
    return round(statistics.median(values), 3)


def summarize(path):
    records = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = PATTERN.search(line)
        if match:
            plane, ic, oc, bits, split, pre, parallel, post = match.groups()
            records.append(
                dict(
                    plane=int(plane), ic=int(ic), oc=int(oc), bits=int(bits),
                    split=split, pre_us=float(pre), parallel_us=float(parallel),
                    post_us=float(post),
                )
            )
    p32_indices = [i for i, r in enumerate(records) if r["plane"] == 32 and r["oc"] == 2048]
    if len(p32_indices) != 56:
        raise ValueError(f"Expected 56 W4 p32 records in {path}, got {len(p32_indices)}")
    measured = records[p32_indices[28]:]
    expected = {(32, 2048): 28, (1, 2048): 224, (1, 3072): 448, (1, 151936): 9}
    for shape, count in expected.items():
        actual = sum((r["plane"], r["oc"]) == shape for r in measured)
        if actual != count:
            raise ValueError(f"Unexpected {shape} count in {path}: {actual}, expected {count}")
    if len(measured) != sum(expected.values()):
        raise ValueError(f"Unexpected extra records in {path}")

    first_w8_index = next(i for i, r in enumerate(measured) if r["oc"] == 151936)
    decode = measured[first_w8_index + 1:]
    if any(r["plane"] != 1 for r in decode):
        raise ValueError(f"Non-decode shape after first W8 record in {path}")
    result = {"file": str(path), "raw_count": len(records), "measured_count": len(measured)}
    groups = (("w4_1024x2048", 2048, 4, 224),
              ("w4_1024x3072", 3072, 4, 448),
              ("w8_1024x151936", 151936, 8, 8))
    for name, oc, bits, expected_count in groups:
        subset = [r for r in decode if r["oc"] == oc]
        if len(subset) != expected_count or any(r["bits"] != bits for r in subset):
            raise ValueError(f"Unexpected {name} count/bitwidth in {path}")
        durations = [r["pre_us"] + r["parallel_us"] + r["post_us"] for r in subset]
        result[name] = {
            "calls": len(subset),
            "pre_median_us": median([r["pre_us"] for r in subset]),
            "parallel_median_us": median([r["parallel_us"] for r in subset]),
            "total_median_us": median(durations),
            "sum_ms": round(sum(durations) / 1000, 3),
        }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    runs = [summarize(path) for path in args.logs]
    payload = {
        "selection": "last measured request, excluding tuning/warm-up and its first W8 prefill call",
        "runs": runs,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
