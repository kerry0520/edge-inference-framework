"""Summarize two ABBA blocks of original/candidate Android llm_bench runs."""

import argparse
import json
from pathlib import Path


ORDER = ("original", "candidate", "candidate", "original") * 2


def summarize(prefix, prompt, generation):
    values = []
    files = []
    for run, variant in enumerate(ORDER, 1):
        path = Path(f"{prefix}-r{run}-{variant}.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = payload["results"][0]
        if payload["threads"] != 4 or result["prompt_len"] != prompt or result["generate_len"] != generation:
            raise ValueError(f"Unexpected conditions: {path}")
        values.append(result["decode_tps"])
        files.append(str(path))
    blocks = []
    for start in (0, 4):
        original_mean = (values[start] + values[start + 3]) / 2
        candidate_mean = (values[start + 1] + values[start + 2]) / 2
        blocks.append({"original_edge_mean_tps": round(original_mean, 3),
                       "candidate_center_mean_tps": round(candidate_mean, 3),
                       "candidate_vs_original_percent": round(100 * (candidate_mean / original_mean - 1), 3)})
    return {"prompt": prompt, "generation": generation, "files": files,
            "original_tps": [round(values[i], 3) for i in (0, 3, 4, 7)],
            "candidate_tps": [round(values[i], 3) for i in (1, 2, 5, 6)],
            "blocks": blocks,
            "all_values_tps": [round(value, 3) for value in values]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = {"order": ORDER, "experiments": [
        summarize("results/android/w8-prefetch512-bench-20260928", 32, 128),
        summarize("results/android/w8-prefetch512-short-20260928", 32, 32),
    ]}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
