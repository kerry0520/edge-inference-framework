"""Compare two raw little-endian float32 vocabulary rows; never runs a model."""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import sys


VOCAB = 151936


def load(path):
    raw = Path(path).read_bytes()
    if len(raw) != VOCAB * 4:
        raise ValueError("Expected one complete 151936-float vocabulary row")
    values = array.array("f")
    if values.itemsize != 4:
        raise ValueError("Host float storage is not 32-bit")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Non-finite logits")
    return values, hashlib.sha256(raw).hexdigest()


def top(values, count=5):
    ids = sorted(range(len(values)), key=lambda i: (-values[i], i))[:count]
    return [{"id": i, "score": values[i]} for i in ids]


def compare(left_path, right_path):
    left, left_sha = load(left_path)
    right, right_sha = load(right_path)
    delta = [float(a) - float(b) for a, b in zip(left, right)]
    avg = math.fsum(delta) / len(delta)
    left_top, right_top = top(left), top(right)
    a, b = left_top[0]["id"], right_top[0]["id"]
    return {"count": VOCAB, "dtype": "little-endian float32",
            "left_sha256": left_sha, "right_sha256": right_sha,
            "equal_values": sum(a == b for a, b in zip(left, right)),
            "max_abs_difference": max(abs(v) for v in delta),
            "rmse": math.sqrt(math.fsum(v*v for v in delta) / len(delta)),
            "mean_left_minus_right": avg,
            "centered_rmse": math.sqrt(math.fsum((v-avg)**2 for v in delta) / len(delta)),
            "left_top5": left_top, "right_top5": right_top,
            "left_top2_margin": left_top[0]["score"] - left_top[1]["score"],
            "right_top2_margin": right_top[0]["score"] - right_top[1]["score"],
            "argmax_equal": a == b,
            "cross_candidate_scores": [{"id": i, "left": left[i], "right": right[i]} for i in sorted({a, b})],
            "scope": "Raw score comparison only; caller must verify identical token trajectory and absolute position"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True)
    parser.add_argument("--right", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("Output exists; preserve previous evidence")
    result = compare(args.left, args.right)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
