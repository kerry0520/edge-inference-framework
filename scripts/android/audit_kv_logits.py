"""Audit archived sampler dumps, including actual input and trajectory identity."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from compare_kv_logits import compare, load, VOCAB


def require(condition, message):
    if not condition:
        raise ValueError(message)


def ids(log, name):
    matches = re.findall(r"^" + re.escape(name) + r"=([0-9,]+)$", log, re.M)
    require(len(matches) == 1, "Missing or ambiguous " + name)
    return [int(v) for v in matches[0].split(",") if v]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def audit(directory):
    summary = read(directory / "summary.json")
    require(summary["model_unchanged"] is True, "Model identity changed")
    require(read(directory / "model-before.json") == read(directory / "model-after.json"), "Model files differ")
    expected_names = {p + "-" + t + "-" + m for p in ("low", "high") for t in ("natural", "force") for m in ("candidate", "cold")}
    require({s["name"] for s in summary["stages"]} == expected_names and len(summary["stages"]) == 8, "Unexpected diagnostic stages")
    dump_count = 0
    for stage in summary["stages"]:
        for entry in stage["files"]:
            data = (directory / Path(entry["path"])).read_bytes()
            require(len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"], "Dump identity mismatch")
            if Path(entry["path"]).suffix == ".f32":
                load(directory / Path(entry["path"]))
                dump_count += 1
    require(dump_count == 32, "Missing vocabulary rows")
    results = []
    for precision in ("low", "high"):
        for trajectory in ("natural", "force"):
            left = directory / (precision + "-" + trajectory + "-candidate")
            right = directory / (precision + "-" + trajectory + "-cold")
            left_log = (left / "stdout.log").read_text(encoding="utf-8")
            right_log = (right / "stdout.log").read_text(encoding="utf-8")
            require(re.findall(r"^DEVICE_DIAG_EXIT=(\d+)$", left_log, re.M) == ["0"] and re.findall(r"^DEVICE_DIAG_EXIT=(\d+)$", right_log, re.M) == ["0"], "Device exit unconfirmed")
            first_left = read(left / "pulled/sample-0.json")
            first_right = read(right / "pulled/sample-0.json")
            require(first_left["all_seq_len"] == first_right["all_seq_len"], "Initial absolute positions differ")
            full_left = ids(left_log, "FULL_TOKENS")
            full_right = ids(right_log, "COLD_FULL_TOKENS")
            require(full_left == full_right and len(full_left) == first_left["all_seq_len"], "Actual token input differs")
            require(ids(left_log, "FIRST_TOKENS") == ids(right_log, "FIRST_TOKENS"), "Warm history differs")
            count = 4 if trajectory == "force" else 1
            previous_left, previous_right = [], []
            for index in range(count):
                lm = read(left / ("pulled/sample-" + str(index) + ".json"))
                rm = read(right / ("pulled/sample-" + str(index) + ".json"))
                require(lm["sample_index"] == rm["sample_index"] == index, "Wrong sample index")
                require(lm["all_seq_len"] == rm["all_seq_len"] == len(full_left) + index, "Wrong absolute position")
                require(lm["gen_seq_len"] == rm["gen_seq_len"], "Decode step differs")
                require(lm["vocab_size"] == rm["vocab_size"] == VOCAB and lm["shape"] == rm["shape"] == [1, 1, VOCAB], "Wrong logits shape")
                require(lm["tensor_size"] == rm["tensor_size"] == VOCAB and lm["offset"] == rm["offset"] == lm["size_argument"] == rm["size_argument"] == 0, "Wrong vocabulary slice")
                require(previous_left == previous_right, "Input trajectories diverged")
                math = compare(left / ("pulled/sample-" + str(index) + ".f32"), right / ("pulled/sample-" + str(index) + ".f32"))
                require(math["left_top5"][0]["id"] == lm["natural_token"] and math["right_top5"][0]["id"] == rm["natural_token"], "Natural greedy differs from raw argmax")
                expected_forced = trajectory == "force" and index < 3
                require(lm["forced"] is expected_forced and rm["forced"] is expected_forced, "Unexpected forcing mode")
                if expected_forced:
                    token = summary["teacher_force_tokens"][index]
                    require(lm["returned_token"] == rm["returned_token"] == token, "Wrong common returned token")
                else:
                    require(lm["returned_token"] == lm["natural_token"] and rm["returned_token"] == rm["natural_token"], "Natural result overridden")
                results.append({"precision": precision, "trajectory": trajectory, "sample": index,
                                "absolute_position": lm["all_seq_len"], "same_input_tokens": True,
                                "left_meta": lm, "right_meta": rm, "comparison": math})
                previous_left.append(lm["returned_token"])
                previous_right.append(rm["returned_token"])
    return {"audit_pass": True, "finite_vocabulary_rows": dump_count, "comparisons": results, "fresh_phone_execution": False,
            "scope": "Same-input final vocabulary logits; not internal KV/layer correctness or performance proof"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.record_dir.resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
