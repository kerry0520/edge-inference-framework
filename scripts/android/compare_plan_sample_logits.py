"""Compare sampled vocabulary rows from two isolated diagnostic runs, offline."""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import sys

VOCAB = 151936
MODEL = "/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/"
MODEL_FILES = {MODEL + name for name in
               ("llm.mnn", "llm.mnn.weight", "llm_config.json", "tokenizer.txt", "tokenizer-android-lf.txt")}


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def load_cases(path, expected):
    require(sha(path) == expected, "case manifest hash differs")
    data = read_json(path)
    require(type(data.get("schema_version")) is int and data["schema_version"] == 1 and type(data.get("cases")) is list
            and 1 <= len(data["cases"]) <= 16, "case schema/count")
    seen = set()
    for case in data["cases"]:
        require(type(case.get("id")) is str and 1 <= len(case["id"].encode("utf-8")) <= 128
                and "\0" not in case["id"] and case["id"] not in seen, "case ID")
        seen.add(case["id"])
        ids = case.get("input_ids")
        require(type(ids) is list and 1 <= len(ids) <= 384
                and all(integer(v) and v < VOCAB for v in ids), "case input IDs")
        require(type(case.get("max_new_tokens")) is int and 1 <= case["max_new_tokens"] <= 8,
                "case output budget")
    return data["cases"]


def load_run(directory, mode, cases, case_sha):
    directory = Path(directory)
    receipt_path = directory / "run.json"
    receipt = read_json(receipt_path)
    require(receipt.get("mode") == mode and receipt.get("cases_sha256") == case_sha, "run case/mode binding")
    for key in ("adb_actual_exit", "native_actual_exit"):
        require(type(receipt.get(key)) is int and receipt[key] == 0, "run did not exit zero: " + key)
    require(receipt.get("model_before") == receipt.get("model_after")
            and type(receipt.get("model_before")) is dict and set(receipt["model_before"]) == MODEL_FILES
            and all(type(v) is str and len(v) == 64 and all(c in "0123456789abcdef" for c in v)
                    for v in receipt["model_before"].values()),
            "model identities differ/missing")
    for key in ("binary_sha256", "library_sha256"):
        value = receipt.get(key)
        require(type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value),
                "invalid identity " + key)
    bindings = receipt.get("files")
    require(type(bindings) is dict and set(bindings) == {"logits.f32", "steps.jsonl", "requests.jsonl"},
            "raw files binding")
    for name, digest in bindings.items():
        require(sha(directory / name) == digest, "raw hash differs: " + name)
    def rows(name):
        return [json.loads(line) for line in (directory / name).read_text(encoding="utf-8").splitlines() if line]
    requests, steps = rows("requests.jsonl"), rows("steps.jsonl")
    require(len(requests) == len(cases), "request count differs")
    cursor, position, groups, retained = 0, 0, [], False
    for index, (case, response) in enumerate(zip(cases, requests)):
        require(type(response.get("request")) is int and response["request"] == index
                and response.get("ok") is True and response.get("mode") == mode and response.get("id") == case["id"]
                and response.get("input_ids") == case["input_ids"]
                and type(response.get("input_tokens")) is int and response["input_tokens"] == len(case["input_ids"]),
                "request identity/input")
        tokens = response.get("output_ids")
        require(type(tokens) is list and 1 <= len(tokens) <= case["max_new_tokens"]
                and all(integer(t) and t < VOCAB for t in tokens), "output IDs")
        status = response.get("status")
        require(status in ("normal_finished", "max_tokens_finished")
                and type(response.get("status_code")) is int
                and response["status_code"] == (1 if status == "normal_finished" else 2), "request status")
        require(status != "max_tokens_finished" or len(tokens) == case["max_new_tokens"], "MAX sample count")
        require(type(response.get("sample_count")) is int and response["sample_count"] == len(tokens),
                "sample count differs from emitted tokens")
        require(type(response.get("text")) is str, "response text")
        plan = response.get("plan_cache")
        require(type(plan) is dict, "missing execution route")
        eligible = mode == "candidate" and len(case["input_ids"]) > 32
        decode = len(tokens) - (1 if status == "normal_finished" else 0)
        blocks = (len(case["input_ids"]) + 31) // 32
        expected_plan = {"requested": mode == "candidate", "eligible": eligible,
                         "prepared": eligible, "cache_hit": eligible and retained,
                         "trunk_count": blocks - 1 if eligible else 0,
                         "full_count": (1 if eligible else blocks) + decode,
                         "decode_count": decode}
        for name, value in expected_plan.items():
            require(type(plan.get(name)) is type(value) and plan[name] == value, "execution route " + name)
        retained = retained or eligible
        selected = steps[position:position + len(tokens)]
        require(len(selected) == len(tokens), "missing sampled vocabulary rows")
        for step, (token, row) in enumerate(zip(tokens, selected)):
            require(type(row.get("request")) is int and row["request"] == index and row.get("id") == case["id"]
                    and type(row.get("step")) is int and row["step"] == step
                    and type(row.get("selected_token")) is int and row["selected_token"] == token,
                    "sample sequence/token")
            require(type(row.get("selected_is_stop")) is bool
                    and row["selected_is_stop"] == (status == "normal_finished" and step == len(tokens) - 1),
                    "sample stop/status mismatch")
            require(type(row.get("count")) is int and row["count"] == VOCAB
                    and type(row.get("file_float_offset")) is int and row["file_float_offset"] == cursor
                    and type(row.get("offset_bytes")) is int and row["offset_bytes"] == cursor * 4,
                    "sample size/file offsets")
            shape = row.get("shape")
            require(type(shape) is list and shape and all(integer(d, 1) for d in shape), "tensor shape")
            require(type(row.get("source_float_count")) is int and row["source_float_count"] == math.prod(shape),
                    "source tensor count")
            require(type(row.get("gen_seq_len")) is int and row["gen_seq_len"] == step
                    and type(row.get("all_seq_len")) is int and row["all_seq_len"] == len(case["input_ids"]) + step,
                    "sample context sequence")
            offset, size = row.get("offset"), row.get("size")
            require(integer(offset) and integer(size) and type(row.get("slice_applied")) is bool,
                    "sample slice fields")
            sliced = bool(offset and size)
            require(row["slice_applied"] == sliced and shape[-1] == VOCAB
                    and ((size == VOCAB and offset + size <= math.prod(shape)) if sliced
                         else math.prod(shape) == VOCAB), "sample slice semantics")
            cursor += VOCAB
        position += len(tokens)
        groups.append((response, selected))
    require(position == len(steps) and cursor > 0
            and (directory / "logits.f32").stat().st_size == cursor * 4, "extra/truncated dump rows")
    return receipt, groups


def compare(cases_path, expected_sha, baseline, candidate, *, atol=1e-4, rtol=1e-5):
    require(all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in (atol, rtol)),
            "tolerances must be finite nonnegative numbers")
    require(sys.byteorder == "little" and array.array("f").itemsize == 4, "requires little-endian float32 host")
    cases = load_cases(cases_path, expected_sha)
    before = {str(Path(d) / n): sha(Path(d) / n) for d in (baseline, candidate)
              for n in ("run.json", "logits.f32", "steps.jsonl", "requests.jsonl")}
    br, bg = load_run(baseline, "baseline", cases, expected_sha)
    cr, cg = load_run(candidate, "candidate", cases, expected_sha)
    require(all(br[k] == cr[k] for k in ("binary_sha256", "library_sha256", "model_before")),
            "runs differ in binary/library/model identity")
    count, equal, maximum, outside = 0, True, 0.0, 0
    rows = []
    with (Path(baseline) / "logits.f32").open("rb") as bs, (Path(candidate) / "logits.f32").open("rb") as cs:
        for case, (breq, bsteps), (creq, csteps) in zip(cases, bg, cg):
            require(all(breq[k] == creq[k] for k in ("input_ids", "output_ids", "text", "status", "status_code")),
                    "generation differs for " + case["id"])
            require(len(bsteps) == len(csteps), "sampled step count differs")
            for step in range(len(bsteps)):
                bb, cb = bs.read(VOCAB * 4), cs.read(VOCAB * 4)
                bv, cv = array.array("f"), array.array("f")
                bv.frombytes(bb); cv.frombytes(cb)
                require(len(bv) == len(cv) == VOCAB, "short sampled row")
                require(all(math.isfinite(v) for v in bv) and all(math.isfinite(v) for v in cv), "nonfinite score")
                require(max(range(VOCAB), key=bv.__getitem__) == bsteps[step]["selected_token"]
                        and max(range(VOCAB), key=cv.__getitem__) == csteps[step]["selected_token"],
                        "sample row differs from greedy selected token")
                diffs = [abs(b - c) for b, c in zip(bv, cv)]
                bad = sum(d > atol + rtol * abs(b) for b, d in zip(bv, diffs))
                maximum = max(maximum, max(diffs)); outside += bad; count += 1; equal = equal and bb == cb
                rows.append({"id": case["id"], "step": step, "count": VOCAB,
                             "bitwise_equal": bb == cb, "max_abs_error": max(diffs), "outside_tolerance": bad})
    require(sha(cases_path) == expected_sha and all(sha(p) == pin for p, pin in before.items()),
            "comparison input changed")
    require(count > 0 and outside == 0, "sampled logits differ outside tolerance")
    return {"accepted": True, "requests_per_arm": len(cases), "compared_rows": count,
            "scores_per_row": VOCAB, "all_bitwise_equal": equal, "max_abs_error": maximum,
            "outside_tolerance": outside, "atol": atol, "rtol": rtol, "rows": rows,
            "production_elf_direct_logits_verified": False, "performance_claim": False,
            "scope": "Sampled vocabulary rows of isolated sample-observer ELF linked with bound library; not all intermediate outputs."}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("cases", "expected-cases-sha256", "baseline", "candidate", "output"):
        p.add_argument("--" + name, required=True)
    a = p.parse_args(argv)
    out = Path(a.output)
    require(not out.exists(), "output exists; preserve previous evidence")
    try:
        result = compare(a.cases, a.expected_cases_sha256, a.baseline, a.candidate)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"accepted": False, "error": str(error), "model_execution_performed": False}
    with out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
