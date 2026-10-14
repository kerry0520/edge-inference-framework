"""Recheck saved fixed-input manifests and every float; no device required."""
import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from compare_mnn_logits import read_dump, compare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    args = parser.parse_args()
    args.summary = args.summary.resolve()
    source = json.loads(args.summary.read_text(encoding="utf-8-sig"))
    assert source["result"] == "pass" and source["remote_cleaned"]
    directory = args.summary.with_name(args.summary.name.replace("-summary.json", ""))
    rows = []
    for case in source["cases"]:
        length, seed = case["prompt_length"], case["seed"]
        baseline = read_dump(directory / f"baseline-p{length}-cold.bin")
        candidate_states = []
        for row in case["runs"]:
            name = f"{row['variant']}-p{length}-{row['phase']}"
            dump = directory / (name + ".bin")
            values = read_dump(dump)
            hash_value = hashlib.sha256(dump.read_bytes()).hexdigest()
            assert hash_value == row["candidate_sha256"]
            observed = re.findall(r"stage=(\d+) logits=(\d+) input_count=(\d+) context_tokens=(\d+) ids=([\d,]+)", (directory / (name + ".log")).read_text())
            assert len(observed) == 5
            for stage, shape in enumerate(observed):
                count = length if stage == 0 else 1
                ids = [16 + (seed * 53 + stage * 97 + i * 37) % 1000 for i in range(count)]
                assert [int(x) for x in shape[:4]] == [stage, 151936, count, length + stage]
                assert [int(x) for x in shape[4].strip(",").split(",")] == ids
            comparison = compare(baseline, values, source["atol"], source["rtol"])
            nonfinite = sum(not math.isfinite(v) for stage in values for v in stage)
            expected_failure = row["variant"] == "baseline" and row["phase"] == "after"
            assert (comparison["outside_tolerance"] > 0) == expected_failure
            if not expected_failure:
                assert nonfinite == 0 and all(x["max_abs_diff"] == 0 for x in comparison["stages"])
            if row["variant"] == "candidate":
                candidate_states.append(row["cache_state"])
            rows.append({"name": name, "observed_fixed_inputs_verified": True, "sha256": hash_value,
                         "nonfinite": nonfinite, "outside_tolerance": comparison["outside_tolerance"],
                         "max_abs_diff": max(x["max_abs_diff"] for x in comparison["stages"]),
                         "logits": sum(len(x) for x in values), "expected_failure": expected_failure})
        assert len(candidate_states) == 4 and len(set(candidate_states)) == 1
    report = {"result": "pass", "scope": "offline full dump and observed shape/input verification",
              "source_summary": str(args.summary.relative_to(ROOT)), "runs": rows,
              "runs_checked": len(rows), "values_checked": sum(row["logits"] for row in rows),
              "candidate_cache_unchanged": True, "raw_bins_are_local_gitignored": True}
    target = args.summary.with_name(args.summary.name.replace("-summary.json", "-recheck.json"))
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
