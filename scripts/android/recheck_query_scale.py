"""Independent read-only audit of per-Query scale and same-input sampler dumps."""
import argparse
import json
import math
from pathlib import Path
import re

from recheck_attention_internal import require, load, stats, f32, token_ids


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def scales(directory):
    result = {}
    for path in directory.glob("scale-*-h*.json"):
        match = re.fullmatch(r"scale-(\d+)-h(\d+)\.json", path.name)
        require(match is not None, "Unexpected scale filename")
        index, head = map(int, match.groups())
        meta = read(path)
        require(meta["head"] == head and 0 <= head < 16, "Head identity differs")
        key = (index // 16, head)
        require(key not in result, "Duplicate layer/head scale record")
        require(all(len(meta[k]) == meta["seq_len"] for k in ("absmax", "q_scale", "score_scale")), "Scale length mismatch")
        require(meta["seq_len"] > 1 and meta["kv_len"] >= meta["seq_len"], "Invalid sequence bounds")
        require(math.isfinite(meta["base_scale"]) and meta["base_scale"] > 0, "Invalid base scale")
        for maximum, scale, score in zip(meta["absmax"], meta["q_scale"], meta["score_scale"]):
            require(all(math.isfinite(v) for v in (maximum, scale, score)), "Non-finite scale metadata")
            require(maximum >= 0 and 0 < scale <= 1, "Invalid Q scale")
            expected = f32(1 / maximum) if maximum > 1 else 1.0
            require(scale == expected and score == f32(meta["base_scale"] / scale), "Scale or compensation formula differs")
            require(maximum * scale <= 1.000001, "Scaling exceeds original float bound")
        result[key] = meta
    require(set(result) == {(layer, head) for layer in range(28) for head in range(16)}, "Expected all 28 layers and 16 heads")
    return result


def audit(directory):
    summary = read(directory / "summary.json")
    require(summary["fresh_phone_execution"] and summary["model_unchanged"] and summary["production_unchanged"], "Execution identity incomplete")
    require(read(directory / "model-before.json") == read(directory / "model-after.json"), "Model changed")
    inputs, paths, logs = [], [], []
    for mode in ("candidate", "cold"):
        stage = directory / ("low-" + mode)
        log = (stage / "stdout.log").read_text(encoding="utf-8")
        require(re.findall(r"^DEVICE_DIAG_EXIT=(\d+)$", log, re.M) == ["0"], "Device exit unconfirmed")
        full = token_ids(log, "FULL_TOKENS")
        if mode == "cold":
            require(token_ids(log, "COLD_FULL_TOKENS") == full, "Cold input IDs differ")
        inputs.append(full)
        paths.append(stage / "pulled")
        logs.append(log)
    require(inputs[0] == inputs[1] and inputs[0], "Different actual full inputs")
    require(token_ids(logs[0], "FIRST_TOKENS") == token_ids(logs[1], "FIRST_TOKENS"), "Warm history differs")
    left, right = map(scales, paths)
    suffix, full_len = left[(0, 0)]["seq_len"], len(inputs[0])
    start = full_len - suffix
    require(start > 0, "No retained prefix")
    comparisons = []
    for key in sorted(left):
        a, b = left[key], right[key]
        require(a["seq_len"] == suffix and b["seq_len"] == full_len and a["kv_len"] == b["kv_len"] == full_len, "Layer cache/query lengths differ")
        require(a["base_scale"] == b["base_scale"], "Base scales differ")
        comparisons.append({"layer": key[0], "head": key[1], **{k + "_equal": a[k] == b[k][start:] for k in ("absmax", "q_scale", "score_scale")}})
    samples = []
    previous = []
    natural = summary.get("natural", False)
    sample_count = len(list(paths[0].glob("sample-*.json")))
    require(0 < sample_count == len(list(paths[1].glob("sample-*.json"))) <= summary.get("max_new_tokens", 4), "Candidate sample counts differ")
    for i in range(sample_count):
        metas = [read(p / f"sample-{i}.json") for p in paths]
        for meta, expected_prompt in zip(metas, (suffix, full_len)):
            require(meta["sample_index"] == i and meta["shape"] == [1, 1, 151936] and meta["tensor_size"] == 151936, "Sampler shape differs")
            require(meta["all_seq_len"] == full_len + i and meta["prompt_len"] == expected_prompt and meta["gen_seq_len"] == i, "Sampler position differs")
            require(meta["offset"] == meta["size_argument"] == 0, "Sampler slice differs")
        require(metas[0]["returned_token"] == metas[1]["returned_token"], "Decode inputs diverge")
        require(all(m["forced"] == (not natural and i < 3) for m in metas), "Unexpected forcing mode")
        result = stats(*[load(p / f"sample-{i}.f32", 151936) for p in paths])
        if natural:
            require(all(m["natural_token"] == m["returned_token"] for m in metas), "Natural candidate generation altered")
        previous.append(metas[0]["returned_token"])
        samples.append({"step": i, "natural_tokens": [m["natural_token"] for m in metas], "returned_tokens": [m["returned_token"] for m in metas],
                        "returned_matches_natural": all(m["natural_token"] == m["returned_token"] for m in metas), **result})
    require(all(token_ids(log, "SECOND_TOKENS") == previous for log in logs), "Returned output IDs disagree")
    baseline = None
    if (directory / "baseline-low-candidate").exists():
        baseline_paths = []
        for mode in ("candidate", "cold"):
            stage = directory / ("baseline-low-" + mode)
            log = (stage / "stdout.log").read_text(encoding="utf-8")
            require(re.findall(r"^DEVICE_DIAG_EXIT=(\d+)$", log, re.M) == ["0"], "Baseline exit unconfirmed")
            require(token_ids(log, "FULL_TOKENS") == inputs[0], "Baseline actual full inputs differ")
            if mode == "cold":
                require(token_ids(log, "COLD_FULL_TOKENS") == inputs[0], "Baseline cold input differs")
            baseline_paths.append(stage / "pulled")
        baseline = []
        baseline_count = min(len(list(p.glob("sample-*.json"))) for p in baseline_paths)
        require(baseline_count > 0, "Baseline samples missing")
        for i in range(baseline_count):
            metas = [read(p / f"sample-{i}.json") for p in baseline_paths]
            for meta, prompt in zip(metas, (suffix, full_len)):
                require(meta["sample_index"] == i and meta["all_seq_len"] == full_len + i and meta["prompt_len"] == prompt and meta["gen_seq_len"] == i, "Baseline sample position differs")
                require(meta["shape"] == [1, 1, 151936] and meta["tensor_size"] == 151936 and meta["offset"] == meta["size_argument"] == 0, "Baseline sample slice differs")
            require(all(m["forced"] == (not natural and i < 3) for m in metas), "Baseline forcing mode differs")
            if not natural and i < 3:
                require(all(m["forced"] for m in metas) and metas[0]["returned_token"] == previous[i], "Baseline teacher forcing differs")
                require(metas[0]["returned_token"] == metas[1]["returned_token"], "Baseline forced inputs diverge")
            if natural:
                require(all(m["natural_token"] == m["returned_token"] for m in metas), "Baseline natural generation altered")
            result = stats(*[load(p / f"sample-{i}.f32", 151936) for p in baseline_paths])
            baseline.append({"step": i, "natural_tokens": [m["natural_token"] for m in metas], **result})
            if metas[0]["returned_token"] != metas[1]["returned_token"]:
                break  # Subsequent inputs diverge; never compare different trajectories as same-input logits.
    return {"audit_pass": True, "actual_full_ids_equal": True, "full_length": full_len, "suffix_length": suffix, "retained_prefix": start,
            "layer_head_count": len(comparisons), "row_scale_comparisons": comparisons, "logit_comparisons": samples,
            "all_aligned_row_scales_equal": all(all(v for k, v in row.items() if k.endswith("_equal")) for row in comparisons),
            "all_logits_bytes_equal": all(row["aligned_bytes_equal"] for row in samples),
            "baseline_logit_comparisons": baseline,
            "natural_generation": natural, "candidate_output_tokens": previous,
            "scope": "Recorded all-layer scale metadata and captured same-input final logits; finite dump values. Baseline stops comparing after different returned IDs. Does not prove internal cache bytes, all models, speed, or generation beyond captured steps."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.record_dir.resolve()), ensure_ascii=False, indent=2))
