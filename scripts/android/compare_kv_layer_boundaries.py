"""Read plain-host tensors and align suffix positions with a full-prefill slice."""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import sys


def require(value, message):
    if not value:
        raise ValueError(message)


def values(directory, meta):
    require(meta["copied"] is True and meta["code"] == 2 and meta["lanes"] == 1 and meta["host_bits"] == 32, "Unsupported host tensor")
    raw = (directory / meta["file"]).read_bytes()
    require(len(raw) == meta["host_bytes"] == math.prod(meta["host_shape"]) * 4, "Tensor bytes disagree with plain host shape")
    result = array.array("f")
    require(result.itemsize == 4, "Host float is not 32-bit")
    result.frombytes(raw)
    if sys.byteorder != "little":
        result.byteswap()
    require(all(math.isfinite(v) for v in result), "Non-finite tensor")
    return result, hashlib.sha256(raw).hexdigest()


def audit(directory, last_op=50, right_directory=None, left_length=25, right_length=47, left_start=0, right_start=22, count=25, precisions=("low", "high")):
    right_directory = right_directory or directory
    require(0 <= left_start and left_start + count <= left_length and 0 <= right_start and right_start + count <= right_length and count > 0, "Invalid comparison interval")
    results = []
    for precision in precisions:
        left = directory / (precision + "-candidate") / "pulled"
        right = right_directory / (precision + "-cold") / "pulled"
        ls = [json.loads(v) for v in (left / "ops.jsonl").read_text().splitlines()]
        rs = [json.loads(v) for v in (right / "ops.jsonl").read_text().splitlines()]
        require(len(ls) == len(rs), "Different operation counts")
        checked = []
        skipped = []
        for lo, ro in zip(ls, rs):
            require((lo["index"], lo["name"], lo["type"]) == (ro["index"], ro["name"], ro["type"]), "Different operation identities")
            if lo["index"] > last_op:
                continue
            require(len(lo["outputs"]) == len(ro["outputs"]), "Different output counts")
            for number, (lm, rm) in enumerate(zip(lo["outputs"], ro["outputs"])):
                key = {"op_index": lo["index"], "name": lo["name"], "type": lo["type"], "output": number}
                if not lm.get("file") or not rm.get("file"):
                    skipped.append(dict(key, reason="No captured file"))
                    continue
                lshape, rshape = lm["host_shape"], rm["host_shape"]
                require(lm["host_format"] == rm["host_format"], "Host layouts differ")
                require(len(lshape) == len(rshape), "Tensor ranks differ")
                axes = [i for i, (a, b) in enumerate(zip(lshape, rshape)) if a != b]
                if len(axes) != 1 or (lshape[axes[0]], rshape[axes[0]]) != (left_length, right_length):
                    skipped.append(dict(key, reason="No unique sequence axis matching declared lengths", left_shape=lshape, right_shape=rshape))
                    continue
                axis = axes[0]
                lv, lsha = values(left, lm)
                rv, rsha = values(right, rm)
                inner, outer = math.prod(lshape[axis+1:]), math.prod(lshape[:axis])
                differences = []
                left_bytes, right_bytes = [], []
                for batch in range(outer):
                    left_offset = (batch * left_length + left_start) * inner
                    right_offset = (batch * right_length + right_start) * inner
                    differences.extend(float(lv[left_offset+i]) - float(rv[right_offset+i]) for i in range(count*inner))
                    left_bytes.append(lv[left_offset:left_offset+count*inner].tobytes())
                    right_bytes.append(rv[right_offset:right_offset+count*inner].tobytes())
                left_aligned_sha = hashlib.sha256(b"".join(left_bytes)).hexdigest()
                right_aligned_sha = hashlib.sha256(b"".join(right_bytes)).hexdigest()
                checked.append(dict(key, left_shape=lshape, right_shape=rshape, sequence_axis=axis,
                                    left_local_positions=[left_start, left_start+count-1], right_local_positions=[right_start, right_start+count-1], compared_values=len(differences),
                                    equal_values=sum(v == 0 for v in differences),
                                    max_abs_difference=max(abs(v) for v in differences),
                                    rmse=math.sqrt(math.fsum(v*v for v in differences)/len(differences)),
                                    aligned_bytes_equal=left_aligned_sha == right_aligned_sha,
                                    left_aligned_sha256=left_aligned_sha, right_aligned_sha256=right_aligned_sha,
                                    left_sha256=lsha, right_sha256=rsha))
        require(checked and checked[0]["op_index"] == 0, "Initial input boundary missing")
        different = [row for row in checked if row["max_abs_difference"] != 0]
        results.append({"precision": precision, "checked": checked, "skipped": skipped,
                        "first_observed_difference": different[0] if different else None})
    return {"audit_pass": True, "last_operation_in_scope": last_op, "profiles": results,
            "interval": {"left_length": left_length, "right_length": right_length, "left_start": left_start, "right_start": right_start, "count": count},
            "scope": "Only captured, uniquely aligned float32 host outputs; caller must verify actual token identity and absolute positions"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--last-op", type=int, default=50)
    parser.add_argument("--right-record-dir", type=Path)
    parser.add_argument("--left-length", type=int, default=25)
    parser.add_argument("--right-length", type=int, default=47)
    parser.add_argument("--left-start", type=int, default=0)
    parser.add_argument("--right-start", type=int, default=22)
    parser.add_argument("--count", type=int, default=25)
    parser.add_argument("--precisions", choices=("low", "high"), nargs="+", default=["low", "high"])
    args = parser.parse_args()
    print(json.dumps(audit(args.record_dir.resolve(), args.last_op, args.right_record_dir, args.left_length, args.right_length, args.left_start, args.right_start, args.count, args.precisions), ensure_ascii=False))


if __name__ == "__main__":
    main()
