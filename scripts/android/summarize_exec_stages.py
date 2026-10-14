"""Summarize the final measured request in llm_bench PhoneExecStages logs.

llm_bench performs tuning and a warm-up request before its measured request.
The final contiguous plane=32 block starts that measured request for this
fixed p32/g8 experiment. Keep the selection explicit and validate its shape.
"""

import argparse
import json
import re
import statistics
from pathlib import Path


PATTERN = re.compile(
    r"\[PhoneExecStages\] plane=(\d+) split=([^ ]+) "
    r"pre_us=([\d.]+) parallel_us=([\d.]+) post_us=([\d.]+)"
)


def median(values):
    return round(statistics.median(values), 3)


def summarize(path):
    records = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = PATTERN.search(line)
        if match:
            plane, split, pre, parallel, post = match.groups()
            records.append(
                dict(
                    plane=int(plane),
                    split=split,
                    pre_us=float(pre),
                    parallel_us=float(parallel),
                    post_us=float(post),
                )
            )
    if not records:
        raise ValueError(f"No stage records in {path}")

    last_p32 = max(i for i, item in enumerate(records) if item["plane"] == 32)
    first_p32 = last_p32
    while first_p32 > 0 and records[first_p32 - 1]["plane"] == 32:
        first_p32 -= 1
    measured = records[first_p32:]
    p32 = [item for item in measured if item["plane"] == 32]
    p1 = [item for item in measured if item["plane"] == 1]
    if len(p32) != 28 or len(p1) != 224 or len(measured) != 252:
        raise ValueError(
            f"Unexpected measured shape in {path}: plane32={len(p32)}, plane1={len(p1)}"
        )
    if any(item["split"] != "output-channel" for item in measured):
        raise ValueError(f"Unexpected split mode in {path}")

    result = {"file": str(path), "raw_record_count": len(records), "measured_record_count": len(measured)}
    for plane, group in ((32, p32), (1, p1)):
        times = [item["pre_us"] + item["parallel_us"] + item["post_us"] for item in group]
        result[f"plane_{plane}"] = {
            "count": len(group),
            "split": "output-channel",
            "pre_median_us": median([item["pre_us"] for item in group]),
            "parallel_median_us": median([item["parallel_us"] for item in group]),
            "post_median_us": median([item["post_us"] for item in group]),
            "total_median_us": median(times),
            "parallel_share_median_pct": median(
                [100 * item["parallel_us"] / total for item, total in zip(group, times)]
            ),
        }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs = [summarize(path) for path in args.logs]
    result = {"selection": "last p32 block plus following p1 blocks; tuning and warm-up excluded", "runs": runs}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
