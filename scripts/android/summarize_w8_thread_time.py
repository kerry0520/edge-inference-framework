"""Summarize the fixed p32/g8/t4 PhoneW8Thread Android experiment.

Each process emits 7 tuning, 9 warm-up, and 9 measured W8 calls. The first
call of the measured request scores the prefill output, so decode is the last
eight calls. Times are instrumented and do not measure an unmodified build.
"""

import argparse
import json
import re
import statistics
from pathlib import Path


FIELDS = ("pre_us", "parallel_us", "post_us", "max_thread_us", "max_gemm_us",
          "gemm0_us", "gemm1_us", "gemm2_us", "gemm3_us")
PATTERN = re.compile(r"\[PhoneW8Thread\] " + " ".join(fr"{field}=([\d.]+)" for field in FIELDS))


def median(values):
    return round(statistics.median(values), 3)


def summarize(path):
    records = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = PATTERN.search(line)
        if match:
            records.append(dict(zip(FIELDS, map(float, match.groups()))))
    if len(records) != 25:
        raise ValueError(f"Expected 25 W8 calls in {path}, got {len(records)}")
    decode = records[-8:]
    summary = {field + "_median": median([row[field] for row in decode]) for field in FIELDS}
    summary["parallel_minus_max_thread_us_median"] = median(
        [row["parallel_us"] - row["max_thread_us"] for row in decode]
    )
    summary["parallel_minus_max_gemm_us_median"] = median(
        [row["parallel_us"] - row["max_gemm_us"] for row in decode]
    )
    return {"file": str(path), "raw_count": len(records), "decode_count": len(decode), **summary}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    runs = [summarize(path) for path in args.logs]
    payload = {
        "selection": "last 8 W8 calls of p32/g8/t4 request; exclude 7 tuning, 9 warm-up, and measured prefill call",
        "units": "microseconds",
        "runs": runs,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
