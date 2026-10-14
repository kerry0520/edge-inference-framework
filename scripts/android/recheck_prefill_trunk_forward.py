"""Independent, offline native-evidence reader; never connects or replays a model."""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import sys

VOCAB = 151936
SIZES = (32, 32, 29, 1, 1)
PRIORS = (0, 32, 64, 93, 94)
ATTENTION_NAMES = {f"Attention/Reshape_{8 + 9 * i}_output_0" for i in range(28)}
FIXTURE_SHA256 = "0222b78e0603687cd52bd2d5a498998e7b49dd80f9d7c5ae23cc03d5ee61ef32"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def actual_exit(path):
    raw = path.read_text().strip()
    return json.loads(raw)["actual_exit"] if raw.startswith("{") else int(raw)


def events(path):
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith('{"event":'):
            result.append(json.loads(line))
    require(result, "no native JSON events")
    return result


def load_gate(records, pooled_full=False):
    summaries = [r for r in records if r.get("event") == "summary"]
    require(len(summaries) == 1, "one load summary required")
    s = summaries[0]
    require(s.get("passed") is True and s.get("full_loaded") is True and
            s.get("trunk_loaded") is True and s.get("model_forward_count") == 0,
            "load summary failed or not zero-forward")
    expected = s["expected_meta"]
    require(type(expected) is int and expected > 0, "invalid meta identity")
    tables = {}
    for phase in ("full-load", "trunk-load"):
        cs = [r for r in records if r.get("event") == "clone" and r.get("phase") == phase]
        require(len(cs) == (28 if phase == "full-load" and not pooled_full else 56), f"{phase}: wrong clone-chain count")
        table = {}
        chains = {}
        for c in cs:
            name = c["name"]
            require(name, f"{phase}: empty name")
            chains.setdefault(name, []).append(c)
            if phase == "full-load" and not pooled_full:
                require(name not in table, "full-load duplicate name")
            for field in ("source_execution", "target_execution", "source_manager", "target_manager"):
                require(type(c[field]) is int and c[field] > 0, f"{phase}: null {field}")
            require(c["source_execution"] != c["target_execution"], "clone execution aliases source")
            require(c["source_meta"] == c["target_meta"] == expected, "clone meta differs")
            require(c["source_manager"] == c["target_manager"], "clone manager not shared")
            require(c["source_kv_length"] == c["target_kv_length"] == 0, "nonzero load KV length")
            require(c["shared_layer_index"] < 0, "unexpected cross-layer KV sharing")
            table[name] = c
        require(len({c["target_manager"] for c in cs}) == 28, "cross-layer manager alias")
        # Intermediate pre-rearrange executions are destroyed in the per-op loop:
        # allocators may reuse their addresses. Only retained final targets must differ.
        require(len({c["target_execution"] for c in table.values()}) == 28, "retained clone execution alias")
        require(len(chains) == 28, "clone layer coverage")
        require(set(chains) == ATTENTION_NAMES, "fixed model Attention names differ")
        if phase == "full-load" and pooled_full:
            decode_table = {}
            for name, chain in chains.items():
                require(len(chain) == 2 and chain[1]["source_execution"] == chain[0]["target_execution"],
                        "broken full prefill-to-decode clone chain")
                require(chain[0]["target_manager"] == chain[1]["target_manager"], "decode clone manager differs")
                table[name] = chain[0]
                decode_table[name] = chain[1]
            require(len({c["target_execution"] for c in cs}) == 56, "full retained pool execution alias")
            tables["full-decode"] = decode_table
        if phase == "trunk-load":
            for name, chain in chains.items():
                require(len(chain) == 2, "two-link trunk clone chain required")
                base = tables["full-load"].get(name)
                require(base is not None and chain[0]["source_execution"] == base["target_execution"] and
                        chain[1]["source_execution"] == chain[0]["target_execution"],
                        "broken full-to-trunk clone chain")
                require(all(c["source_manager"] == base["target_manager"] for c in chain),
                        "intermediate trunk manager differs")
        tables[phase] = table
    require(tables["full-load"].keys() == tables["trunk-load"].keys(), "clone name sets differ")
    attempts = [r for r in records if r.get("event") == "bridge_attempt"]
    require(len(attempts) == 28 and len({r["name"] for r in attempts}) == 28,
            "28 distinct bridge attempts required")
    require({r["name"] for r in attempts} == set(tables["full-load"]) and
            all(r.get("success") is True for r in attempts), "bridge attempt failed")
    for name, c in tables["trunk-load"].items():
        require(c["source_manager"] == tables["full-load"][name]["target_manager"],
                "full/trunk manager mismatch")
    for field in ("full_attention_names", "trunk_attention_names", "bridge_attempts", "unique_managers"):
        require(s[field] == 28, "summary count mismatch")
    return expected, tables


def read_route(directory, mode, fixture):
    require(actual_exit(directory / "native-exit.code") == 0, "native exit not zero")
    require(actual_exit(directory / "wrapper-exit.code") == 0, "wrapper exit not zero")
    records = events(directory / "stdout.log")
    meta, tables = load_gate(records, pooled_full=True)
    steps = [r for r in records if r.get("event") == "step"]
    positions = [r for r in records if r.get("event") == "positions"]
    inputs = [r for r in records if r.get("event") == "step_input"]
    lengths = [r for r in records if r.get("event") == "kv_length"]
    executed = [r for r in records if r.get("event") == "attention_execute"]
    require(len(steps) == len(positions) == len(inputs) == 5 and len(lengths) == len(executed) == 280,
            "missing/extra steps, position records, or 28-layer before/after records")
    outputs = []
    for i, (size, prior) in enumerate(zip(SIZES, PRIORS)):
        route = "trunk" if mode == "candidate" and i < 2 else "full"
        one = [r for r in steps if r["step"] == i]
        require(len(one) == 1, "duplicate/missing step")
        s = one[0]
        require(s["route"] == route and s["passed"] is True and s["tokens"] == size,
                "wrong route or failed step")
        require(s["phase"] == ("decode" if i >= 3 else "prefill_last" if i == 2 else "prefill_nonlast"), "wrong phase")
        head = 0 if route == "trunk" else 1
        require(s["head_before_count"] == s["head_after_count"] == head, "head execution count")
        require(s["attention_names"] == 28 and s["nonfinite_count"] == 0, "attention/output failure")
        require(s["meta_before"] == prior and s["meta_after"] == prior + size, "meta sync failure")
        require(s["output_float_count"] == (size * 1024 if route == "trunk" else VOCAB), "output dimensions")
        ps = [r for r in positions if r["step"] == i]
        require(len(ps) == 1 and ps[0]["values"] == list(range(prior, prior + size)), "position IDs differ")
        ins = [r for r in inputs if r["step"] == i]
        expected_ids = fixture["input_ids"][prior:prior+size] if i < 3 else [fixture["decode_ids"][i-3]]
        require(len(ins) == 1 and ins[0]["meta"] == meta and ins[0]["input_ids"] == expected_ids,
                "actual fixed input IDs/meta mismatch")
        for boundary in ("before", "after"):
            expected_length = prior if boundary == "before" else prior + size
            for kind, pool in (("length", lengths), ("execute", executed)):
                group = [r for r in pool if r["step"] == i and r["boundary"] == boundary]
                require(len(group) == 28 and len({r["name"] for r in group}) == 28, f"{kind} layer coverage")
                require({r["name"] for r in group} == set(tables["full-load"]), "executed layer names differ")
                for r in group:
                    key = "trunk-load" if route == "trunk" else "full-decode" if i >= 3 else "full-load"
                    c = tables[key][r["name"]]
                    require(r["manager"] == c["target_manager"], "actual execution manager mismatch")
                    if kind == "length":
                        require(r["expected"] == r["actual"] == expected_length, "KV boundary length")
                    else:
                        require(r["route"] == route and r["execution"] == c["target_execution"] and r["meta"] == meta,
                                "actual Execution/meta/route differs from clone gate")
                        require(r["kv_length"] == expected_length, "actual onExecute KV length")
                        require(r["error_code"] == (-1 if boundary == "before" else 0), "actual onExecute error")
                        require(r["meta_previous"] == prior and r["meta_add"] == size and
                                r["meta_remove"] == r["meta_n_reserve"] == r["meta_file_flag"] == 0,
                                "actual onExecute meta mutation")
        if i >= 2:
            path = directory / (Path(s["logits_file"]).name)
            raw = path.read_bytes()
            require(len(raw) == VOCAB * 4, "incomplete logits file")
            values = array.array("f"); values.frombytes(raw)
            if sys.byteorder != "little": values.byteswap()
            require(all(math.isfinite(v) for v in values), "nonfinite raw logits")
            outputs.append((path, values))
    fs = [r for r in records if r.get("event") == "forward_summary"]
    require(len(fs) == 1 and fs[0] == {"event":"forward_summary", "mode":mode,
            "prefill_steps":3, "decode_steps":2, "compared_vocab_outputs":3, "passed":True}, "forward summary mismatch")
    return outputs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--load-log", type=Path)
    p.add_argument("--native-exit", type=Path)
    p.add_argument("--run-dir", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = {"passed": False, "model_replay": False}
    try:
        if args.load_log:
            require(args.native_exit is not None and actual_exit(args.native_exit) == 0, "native exit not zero")
            load_gate(events(args.load_log))
            result.update(load_layer_matches=28, model_forward_count=0, native_exit=0,
                          stdout_sha256=sha(args.load_log))
        else:
            require(args.run_dir is not None, "run directory required")
            require(sha(args.run_dir / "fixture.json") == FIXTURE_SHA256, "fixed fixture SHA differs")
            fixture = json.loads((args.run_dir / "fixture.json").read_text(encoding="utf-8"))
            require(len(fixture["input_ids"]) == 93 and len(fixture["decode_ids"]) == 2 and
                    all(type(v) is int and 0 <= v < VOCAB for v in fixture["input_ids"] + fixture["decode_ids"]), "invalid fixed fixture")
            baseline = read_route(args.run_dir / "baseline", "baseline", fixture)
            candidate = read_route(args.run_dir / "candidate", "candidate", fixture)
            pairs=[]
            for (a, av), (b, bv) in zip(baseline, candidate):
                errors=[abs(x-y) for x,y in zip(av,bv)]
                require(all(e <= 0.0001 + 0.0001 * abs(x) for e,x in zip(errors,av)), "complete logits tolerance exceeded")
                pairs.append({"baseline_sha256":sha(a), "candidate_sha256":sha(b),
                              "float_matches":VOCAB, "max_abs_error":max(errors),
                              "rms_error":math.sqrt(sum(e*e for e in errors)/VOCAB),
                              "byte_identical":a.read_bytes()==b.read_bytes()})
            result.update(routes=2, steps=10, actual_attention_boundary_records=560,
                          logits_pairs=pairs, atol=0.0001, rtol=0.0001)
        result["passed"] = True
    except (ValueError, KeyError, OSError, TypeError, json.JSONDecodeError) as e:
        result["error"] = str(e)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
