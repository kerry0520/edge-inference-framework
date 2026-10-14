"""Summarize fixed Android W8 ABBA benchmark with per-run CPU state."""

import argparse
import csv
import json
import re
from pathlib import Path


ORDER = ("original", "candidate", "candidate", "original") * 2
REVERSE_ORDER = ("candidate", "original", "original", "candidate") * 2
POLICIES = ("p4", "p7")


def read_state(path):
    text = path.read_text(encoding="utf-8-sig")
    temperature = re.search(r"temperature:\s*(\d+)", text)
    if not temperature:
        raise ValueError(f"No battery temperature in {path}")
    cooling = {}
    for name in ("thermal-cpufreq-4", "thermal-cpufreq-7", "thermal-devfreq-0"):
        match = re.search(r"mValue=(\d+).*mName=" + name, text)
        if not match:
            raise ValueError(f"No {name} in {path}")
        cooling[name] = int(match.group(1))
    caps = {}
    for policy in (0, 4, 7):
        match = re.search(fr"policy{policy} cpuinfo_max=(\d+) scaling_max=(\d+)", text)
        if not match:
            raise ValueError(f"No policy{policy} cap in {path}")
        caps[f"p{policy}"] = {"hardware_max_khz": int(match.group(1)),
                               "allowed_max_khz": int(match.group(2))}
    return {"battery_tenths_c": int(temperature.group(1)), "cooling": cooling,
            "caps": caps}


def summarize_run(prefix, variant):
    bench = json.loads(Path(str(prefix) + ".json").read_text(encoding="utf-8"))
    if bench["threads"] != 4 or bench["results"][0]["prompt_len"] != 32 or bench["results"][0]["generate_len"] != 64:
        raise ValueError(f"Unexpected benchmark conditions: {prefix}")
    with Path(str(prefix) + "-frequencies.csv").open(encoding="utf-8-sig", newline="") as handle:
        freq = list(csv.DictReader(handle))
    if len(freq) < 5:
        raise ValueError(f"Too few frequency samples: {prefix}")
    before = read_state(Path(str(prefix) + "-before.txt"))
    after = read_state(Path(str(prefix) + "-after.txt"))
    for policy in POLICIES:
        expected = before["caps"][policy]["allowed_max_khz"]
        if any(int(row[f"{policy}_max"]) != expected for row in freq):
            raise ValueError(f"Changing cap for {policy}: {prefix}")
    return {"variant": variant, "prefix": str(prefix),
            "decode_tps": round(bench["results"][0]["decode_tps"], 3),
            "prefill_tps": round(bench["results"][0]["prefill_tps"], 3),
            "frequency_samples": len(freq), "before": before, "after": after,
            "frequency": {policy: {
                "min_khz": min(int(row[f"{policy}_cur"]) for row in freq),
                "max_khz": max(int(row[f"{policy}_cur"]) for row in freq),
            } for policy in POLICIES}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default="results/android/w8-state-20260929-g64")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reverse", action="store_true")
    args = parser.parse_args()
    order = REVERSE_ORDER if args.reverse else ORDER
    runs = [summarize_run(Path(f"{args.prefix}-r{run}-{variant}"), variant)
            for run, variant in enumerate(order, 1)]
    blocks = []
    for start in (0, 4):
        edge = (runs[start]["decode_tps"] + runs[start + 3]["decode_tps"]) / 2
        center = (runs[start + 1]["decode_tps"] + runs[start + 2]["decode_tps"]) / 2
        original = center if args.reverse else edge
        candidate = edge if args.reverse else center
        blocks.append({"original_mean_tps": round(original, 3),
                       "candidate_mean_tps": round(candidate, 3),
                       "candidate_vs_original_percent": round(100 * (candidate / original - 1), 3)})
    payload = {"order": order, "runs": runs, "blocks": blocks,
               "note": "Pinned to CPUs 4-7; 0.1 s frequency polling; edge/center means are descriptive, not a strict pair estimate."}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
