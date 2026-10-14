"""Read formal response native evidence offline; never connect or replay a model."""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import sys

VOCAB = 151936
ATTENTION_NAMES = {f"Attention/Reshape_{8 + 9 * i}_output_0" for i in range(28)}
STOP_IDS = {151643, 151645}  # Fixed tokenizer: 26 special IDs followed by two stop IDs.


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def actual_exit(path):
    raw = path.read_text(encoding="utf-8-sig").strip()
    return json.loads(raw)["actual_exit"] if raw.startswith("{") else int(raw)


def events(path):
    result = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith('{"event":'):
            result.append(json.loads(line))
    require(result, "zero native JSON event matches")
    return result


def logits(directory, event):
    require(event["count"] == VOCAB, "not complete vocabulary")
    path = directory / Path(event["path"]).name
    require(path.name == f'output-step{event["step"]}.f32', "unexpected logits name")
    raw = path.read_bytes()
    require(len(raw) == 4 * VOCAB, "incomplete f32 bytes")
    values = array.array("f")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    require(all(math.isfinite(x) for x in values), "nonfinite logits")
    return path, values


def verify_forward(records, begin, previous, expected_role, expected_seq, context, remove, managers):
    step = begin["step"]
    require(begin["role"] == expected_role and begin["seq"] == expected_seq,
            "outer chunk role or length differs from plan")
    require(begin["context"] == context and begin["meta_previous"] == previous and
            begin["meta_remove"] == remove, "forward begin context/reset differs")
    meta = begin["meta"]
    require(type(meta) is int and meta > 0, "null meta")
    positions = [e for e in records if e.get("event") == "response_positions" and e.get("step") == step]
    require(len(positions) == 1 and positions[0]["values"] == list(range(context, context+expected_seq)),
            "actual input positions differ")
    end = [e for e in records if e.get("event") == "response_forward_end" and e.get("step") == step]
    after = previous - remove + expected_seq
    require(len(end) == 1 and end[0]["ok"] is True and
            end[0]["role"] == expected_role and end[0]["meta_previous"] == after,
            "forward failed or meta synced incorrectly")
    for boundary in ("before", "after"):
        attn = [e for e in records if e.get("event") == "response_attention" and
                e.get("step") == step and e.get("boundary") == boundary]
        require(len(attn) == 28 and {e["name"] for e in attn} == ATTENTION_NAMES,
                "28 distinct real Attention boundaries required")
        require(len({e["manager"] for e in attn}) == 28, "cross-layer manager alias")
        for a in attn:
            require(type(a["execution"]) is int and a["execution"] > 0 and
                    type(a["manager"]) is int and a["manager"] > 0, "null Execution/manager")
            old = managers.setdefault(a["name"], a["manager"])
            require(old == a["manager"], "KV manager changed within Llm instance")
            require(a["meta"] == meta and a["meta_previous"] == previous and
                    a["meta_add"] == expected_seq and a["meta_remove"] == remove and
                    a["meta_n_reserve"] == a["meta_file_flag"] == 0, "Attention meta differs")
            require(a["kv_length"] == (previous if boundary == "before" else after),
                    "Attention real KV length differs")
            require(a["error_code"] == (-1 if boundary == "before" else 0), "Attention execution failed")
    return after


def compare(a, b):
    ap, av = a
    bp, bv = b
    errors = [abs(x-y) for x, y in zip(av, bv)]
    require(len(errors) == VOCAB and
            all(e <= .0001 + .0001 * abs(x) for x, e in zip(av, errors)),
            "complete logits tolerance exceeded")
    return {"baseline_sha256": sha(ap), "candidate_sha256": sha(bp),
            "float_matches": VOCAB, "byte_identical": ap.read_bytes() == bp.read_bytes(),
            "max_abs_error": max(errors), "rms_error": math.sqrt(sum(e*e for e in errors)/VOCAB)}


def read_route(directory, mode, fixture):
    require(actual_exit(directory/"native-exit.code") == 0 and
            actual_exit(directory/"wrapper-exit.code") == 0, "actual native/wrapper exit nonzero")
    records = events(directory/"stdout.log")
    cases = fixture["cases"]
    require(cases and len({c["name"] for c in cases}) == len(cases), "empty/duplicate fixture cases")
    rb = [i for i, e in enumerate(records) if e["event"] == "request_begin"]
    re = [i for i, e in enumerate(records) if e["event"] == "request_end"]
    require(len(rb) == len(re) == len(cases), "request event coverage")
    completions = [e for e in records if e["event"] == "worker_complete"]
    require(completions == [{"event": "worker_complete", "requests": len(cases), "ok": True}],
            "worker incomplete or failed")
    previous = 0
    step_index = 0
    manager_table = {}
    route_meta = None
    all_outputs = set()
    result = []
    for j, case in enumerate(cases):
        require(rb[j] < re[j] and (j == 0 or rb[j] > re[j-1]), "overlapping requests")
        segment = records[rb[j]:re[j]+1]
        begin, end = segment[0], segment[-1]
        require(begin["request_index"] == end["request_index"] == j and
                begin["name"] == end["name"] == case["name"] and begin["mode"] == mode,
                "request fixture identity")
        ids = begin["input_ids"]
        require(ids and all(type(x) is int and 0 <= x < VOCAB for x in ids), "invalid input IDs")
        if "input_ids" in case:
            require(ids == case["input_ids"] and begin["source_entry"] == "response(token IDs)",
                    "fixed token fixture differs")
        else:
            require(case.get("prompt") and begin["source_entry"] == "response(string)",
                    "text fixture entry differs")
        require(begin["max_new_tokens"] == case["max_new_tokens"] and
                begin["first_step"] == step_index, "request budget or step continuity differs")
        resets = [e for e in segment if e["event"] == "request_reset"]
        require(resets == [{"event": "request_reset", "request_index": j, "context_all": 0}],
                "explicit request reset missing")
        output_ids = end["output_ids"]
        require(output_ids and all(type(x) is int and 0 <= x < VOCAB for x in output_ids), "invalid output IDs")
        require(end["ok"] is True and end["text"] == end["stream_text"], "response output or status failed")
        eos = end["status"] == "normal_finished"
        if eos:
            require(end["status_code"] == 1 and output_ids[-1] in STOP_IDS and
                    not (set(output_ids[:-1]) & STOP_IDS), "EOS status/token mismatch")
        else:
            require(end["status"] == "max_tokens_finished" and end["status_code"] == 2 and
                    len(output_ids) == case["max_new_tokens"] and not (set(output_ids) & STOP_IDS),
                    "max_new_tokens status/token mismatch")
        require(len(output_ids) <= case["max_new_tokens"], "output budget exceeded")
        decode_count = len(output_ids) - int(eos)
        sizes = [min(32, len(ids)-k) for k in range(0, len(ids), 32)]
        expected_steps = len(sizes) + decode_count
        forwards = [e for e in segment if e["event"] == "response_forward_begin"]
        require(len(forwards) == expected_steps and
                [e["step"] for e in forwards] == list(range(step_index, step_index+expected_steps)) and
                end["last_step_exclusive"] == step_index+expected_steps, "real forward count differs")
        route = [e for e in segment if e["event"] == "response_route"]
        if len(ids) > 32:
            require(len(route) == 1 and route[0]["requested"] == (mode == "candidate") and
                    route[0]["eligible"] == route[0]["prepared"] == (mode == "candidate"),
                    "opt-in preparation/fallback differs")
        else:
            require(not route, "short prompt unexpectedly created trunk")
        clones = [e for e in segment if e["event"] == "response_clone"]
        authorizations = [e for e in segment if e["event"] == "response_authorization"]
        grouped = {}
        if mode == "candidate" and len(ids) > 32:
            require(authorizations and all(e["passed"] is True and e["reason"] == "authorized" and
                    e["backend_type"] in (0, 13) and e["expected_base"] == e["actual_base"] and
                    type(e["actual_base"]) is int and e["actual_base"] > 0 for e in authorizations),
                    "request-local CPU base authorization missing or mismatched")
            require(len(clones) == 56 and {e["name"] for e in clones} == ATTENTION_NAMES,
                    "trunk clone chain coverage")
            grouped = {n: [e for e in clones if e["name"] == n] for n in ATTENTION_NAMES}
            require(len({g[-1]["target_execution"] for g in grouped.values()}) == 28,
                    "retained trunk Execution aliases")
            for name, chain in grouped.items():
                require(len(chain) == 2 and chain[1]["source_execution"] == chain[0]["target_execution"],
                        "trunk two-link clone chain broken")
                for c in chain:
                    require(c["source_execution"] != c["target_execution"] and
                            c["source_manager"] == c["target_manager"] and
                            c["source_kv_length"] == c["target_kv_length"] == previous and
                            c["shared_layer_index"] < 0, "trunk clone does not preserve old KV")
                    require(c["target_manager"] == manager_table.get(name, c["target_manager"]),
                            "trunk manager differs from preceding full/decode")
        else:
            require(not clones and not authorizations, "unexpected trunk cloning/authorization")
        outputs = []
        context = 0
        for k, f in enumerate(forwards):
            prefill = k < len(sizes)
            role = "decode" if not prefill else ("trunk" if mode == "candidate" and k < len(sizes)-1 else "full")
            seq = sizes[k] if prefill else 1
            remove = previous if k == 0 else 0
            if route_meta is None:
                route_meta = f["meta"]
            require(f["meta"] == route_meta, "KVMeta changed between ordinary responses")
            previous = verify_forward(segment, f, previous, role, seq, context, remove, manager_table)
            if grouped:
                for name, chain in grouped.items():
                    require(all(c["meta"] == route_meta for c in chain), "trunk clone KVMeta differs")
                    actual = next(e for e in segment if e["event"] == "response_attention" and
                                  e["step"] == f["step"] and e["name"] == name and e["boundary"] == "before")
                    require(actual["manager"] == chain[-1]["target_manager"], "actual trunk/full manager differs")
                    if role == "trunk":
                        require(actual["execution"] == chain[-1]["target_execution"],
                                "actual trunk Execution differs from prepared clone")
            heads = [e for e in segment if e["event"] == "response_head" and e["step"] == f["step"]]
            require(len(heads) == (0 if role == "trunk" else 2) and
                    (not heads or [e["boundary"] for e in heads] == ["before", "after"]),
                    "actual vocabulary head count differs")
            captures = [e for e in segment if e["event"] == "response_logits" and e["step"] == f["step"]]
            require(len(captures) == (0 if role == "trunk" else 1), "trunk published logits or full omitted them")
            if captures:
                out = logits(directory, captures[0])
                all_outputs.add(out[0].name)
                if not prefill or k == len(sizes)-1:
                    outputs.append(out)
            context += seq
        require(end["all_seq_len"] == previous == len(ids)+decode_count,
                "response context/KV final length")
        require(len(outputs) == 1+decode_count, "lastprefill/decode capture count")
        for token, (_, values) in zip(output_ids, outputs):
            require(token == max(range(VOCAB), key=values.__getitem__), "greedy token differs from complete logits")
        result.append({"name": case["name"], "input_ids": ids, "output_ids": output_ids,
                       "text": end["text"], "status": end["status"], "steps": expected_steps,
                       "outputs": outputs, "eos": eos})
        step_index += expected_steps
    require({p.name for p in directory.glob("*.f32")} == all_outputs,
            "unbound extra/missing complete logits file")
    require(sum(e["event"] == "response_attention" for e in records) == step_index*56,
            "unbound Attention events outside request/step")
    for name in ("response_forward_begin", "response_forward_end", "response_positions"):
        require(sum(e["event"] == name for e in records) == step_index,
                f"unbound/missing {name} events outside request/step")
    require(sum(e["event"] == "response_logits" for e in records) == len(all_outputs),
            "unbound/missing logits events outside request/step")
    return result, step_index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = {"passed": False, "model_replay": False}
    try:
        fixture_path = args.run_dir/"fixture.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8-sig"))
        baseline, n = read_route(args.run_dir/"baseline", "baseline", fixture)
        candidate, m = read_route(args.run_dir/"candidate", "candidate", fixture)
        require(n == m and len(baseline) == len(candidate), "route coverage differs")
        requests = []
        for a, b in zip(baseline, candidate):
            require(all(a[k] == b[k] for k in ("name", "input_ids", "output_ids", "text", "status", "steps", "eos")),
                    "formal response token/text/status differs")
            require(len(a["outputs"]) == len(b["outputs"]), "full output pair count differs")
            pairs = [compare(x, y) for x, y in zip(a["outputs"], b["outputs"])]
            requests.append({k: a[k] for k in ("name", "input_ids", "output_ids", "text", "status", "steps", "eos")}
                            | {"logits_pairs": pairs})
        result.update(passed=True, fixture_sha256=sha(fixture_path), routes=2,
                      requests=requests, forwards=2*n, actual_attention_boundary_matches=112*n,
                      complete_logit_pairs=sum(len(r["logits_pairs"]) for r in requests),
                      stop_ids=sorted(STOP_IDS), atol=.0001, rtol=.0001)
    except (ValueError, KeyError, OSError, TypeError, json.JSONDecodeError) as e:
        result["error"] = str(e)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
