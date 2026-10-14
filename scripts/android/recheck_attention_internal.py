"""Independent raw-array recheck of a bounded Attention scaling intervention."""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sys


def require(value, message):
    if not value:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load(path, count):
    raw = path.read_bytes()
    require(len(raw) == count*4, "Unexpected float32 dump length: " + str(path))
    result = array.array("f")
    require(result.itemsize == 4, "Host float is not 32-bit")
    result.frombytes(raw)
    if sys.byteorder != "little":
        result.byteswap()
    require(all(math.isfinite(v) for v in result), "Non-finite dump")
    return result


def stats(left, right):
    require(len(left) == len(right) and len(left) > 0, "Array lengths differ")
    differences = [float(a)-float(b) for a, b in zip(left, right)]
    return {"total": len(left), "different": sum(v != 0 for v in differences),
            "max_abs": max(abs(v) for v in differences),
            "rmse": math.sqrt(math.fsum(v*v for v in differences)/len(differences)),
            "aligned_bytes_equal": left.tobytes() == right.tobytes()}


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def token_ids(log, key):
    matches = re.findall(r"^"+key+r"=([0-9,]+)$", log, re.M)
    require(len(matches) == 1, "Missing token identity: " + key)
    return [int(v) for v in matches[0].split(",") if v]


def audit(directory, reference, shared=False):
    left = directory / "low-candidate/pulled"
    right = directory / "low-cold/pulled"
    lm, rm = read(left / "attention.json"), read(right / "attention.json")
    for meta, seq, offset in ((lm, 25, 22), (rm, 47, 0)):
        require(meta["active_attention_call_index"] == 1 and meta["seq_len"] == seq and meta["kv_valid_offset"] == offset, "Wrong Attention/query position")
        require(all(meta[k] == v for k, v in {"kv_len": 47, "heads": 16, "kv_heads": 8, "head_dim": 128, "bytes": 2, "pack": 8, "eP": 12, "lP": 1, "hP": 16, "attention_option": 8, "key_quant": 0, "value_quant": 0, "block_kv": 64}.items()) and meta["flash"] is True, "Unsupported Attention layout")
    require(lm["previous"]-lm["remove"] == 22 and lm["add"] == 25 and rm["previous"] == rm["remove"] == 0 and rm["add"] == 47, "Cache bounds differ")
    full = []
    for mode in ("candidate", "cold"):
        log = (directory / ("low-"+mode) / "stdout.log").read_text(encoding="utf-8")
        require(re.findall(r"^DEVICE_DIAG_EXIT=(\d+)$", log, re.M) == ["0"], "Device exit unconfirmed")
        full.append(token_ids(log, "COLD_FULL_TOKENS" if mode == "cold" else "FULL_TOKENS"))
    require(full[0] == full[1] and len(full[0]) == 47, "Actual complete input differs")
    require(read(directory/"model-before.json") == read(directory/"model-after.json"), "Model changed")
    results = {}
    queries = []
    for name, heads, left_rows, offset in (("q",16,25,22),("k-input",8,25,22),("v-input",8,25,22),("effective-k",8,47,0),("effective-v",8,47,0)):
        stride = heads*128
        lv = load(left / ("att-"+name+"-f32.bin"), left_rows*stride)
        rv = load(right / ("att-"+name+"-f32.bin"), 47*stride)
        results[name] = stats(lv, rv[offset*stride:])
        require(results[name]["aligned_bytes_equal"], "Different original Q/K/V or effective cache")
        if name == "q":
            queries = [lv, rv]
    extrema = []
    for query, meta in zip(queries, (lm, rm)):
        peak = max(abs(v) for v in query)
        natural = meta.get("natural_q_scale", meta["q_scale"])
        require(f32(1/peak) == natural, "Natural Q scale inconsistent with actual full-range maximum")
        require(0 < meta["q_scale"] <= natural, "Intervention is not the conservative scale")
        extrema.append({"absmax": peak, "natural_q_scale": natural, "used_q_scale": meta["q_scale"],
                        "used_scale_fp16": struct.unpack("<e", struct.pack("<e", meta["q_scale"]))[0]})
    require((left/"att-mask-raw.bin").read_bytes() == (right/"att-mask-raw.bin").read_bytes() == b"\0\0", "Different scalar mask")
    for name, columns in (("scaled-q",128),("b0-qk-before",47),("b0-qk-masked",47),("b0-softmax",47),("b0-pv",128),("final-pv",128),("b0-running-max",1),("b0-running-sum",1)):
        if not (left / ("att-h0-"+name+"-f32.bin")).exists():
            continue
        all_left, all_right = array.array("f"), array.array("f")
        for head in range(16):
            lv = load(left / ("att-h"+str(head)+"-"+name+"-f32.bin"), 25*columns)
            rv = load(right / ("att-h"+str(head)+"-"+name+"-f32.bin"), 47*columns)[22*columns:]
            if name == "b0-softmax":
                # Flash weights are unnormalized; compare only visible keys.
                for row in range(25):
                    end = 23+row
                    all_left.extend(lv[row*47:row*47+end])
                    all_right.extend(rv[row*47:row*47+end])
            else:
                all_left.extend(lv)
                all_right.extend(rv)
        results[name] = stats(all_left, all_right)
    if shared:
        require(lm["q_scale"] == rm["q_scale"] and lm["mScale"] == rm["mScale"], "Shared scale compensation differs")
        for name in ("scaled-q","b0-qk-before","b0-qk-masked","b0-softmax","b0-pv","final-pv","b0-running-max","b0-running-sum"):
            require(results[name]["aligned_bytes_equal"], "Shared scaling did not close target boundary: " + name)
    else:
        require(lm["q_scale"] != rm["q_scale"] and results["scaled-q"]["different"] > 0, "Expected observational scaling divergence absent")
    observer = {}
    for mode in ("candidate", "cold"):
        observer[mode] = [(directory / ("low-"+mode) / "pulled" / ("sample-"+str(i)+".f32")).read_bytes() == (reference / ("low-"+mode) / "pulled" / ("sample-"+str(i)+".f32")).read_bytes() for i in range(4)]
    require(all(observer["cold"]) and (shared or all(observer["candidate"])), "Observer cold/original score preservation failed")
    return {"audit_pass": True, "same_47_input_ids": full[0], "metadata": [lm, rm],
            "query_extrema": extrema, "comparisons": results, "observer_matches_reference": observer,
            "shared_scale_intervention": shared, "fresh_phone_execution": False,
            "scope": "Target second Attention only; qk-masked is scaled before implicit Flash mask; exp weights unnormalized; no full-model repair proof"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--reference", type=Path, default=Path("results/android/kv-layer-diagnosis-20261007/run-4"))
    parser.add_argument("--shared", action="store_true")
    args = parser.parse_args()
    print(json.dumps(audit(args.record_dir, args.reference, args.shared), ensure_ascii=False))


if __name__ == "__main__":
    main()
