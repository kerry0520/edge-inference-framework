"""Summarize ABBA workset probe runs, checking exact output assertions succeeded."""

import argparse
import json
import re
import statistics
from pathlib import Path


HEADER = re.compile(r"groups=(\d+) bytes_per_group=(\d+) tile_bytes=(\d+) stream_bytes=(\d+) threads=(\d+) checksum=(\d+) nonzero_bytes=(\d+)")
ROW = re.compile(r"repeat=(\d+) mode=(hot|stream) elapsed_us=([\d.]+) checksum=(\d+)")


def rounded_median(values):
    return round(statistics.median(values), 3)


def summarize(path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header = HEADER.fullmatch(lines[0])
    if not header:
        raise ValueError(f"Missing probe header: {path}")
    groups, bytes_per_group, tile_bytes, stream_bytes, threads, checksum, nonzero_bytes = map(int, header.groups())
    if nonzero_bytes <= 0:
        raise ValueError(f"Trivial all-zero output in {path}")
    rows = []
    for line in lines[1:]:
        match = ROW.fullmatch(line)
        if not match:
            raise ValueError(f"Unexpected probe line in {path}: {line}")
        repeat, mode, elapsed, row_checksum = match.groups()
        if int(row_checksum) != checksum:
            raise ValueError(f"Checksum mismatch in {path}")
        rows.append((int(repeat), mode, float(elapsed)))
    if len(rows) != 20:
        raise ValueError(f"Expected 20 samples in {path}, got {len(rows)}")
    pairs = []
    for repeat in range(5):
        sample = rows[repeat * 4:(repeat + 1) * 4]
        if [(r, mode) for r, mode, _ in sample] != [(repeat, mode) for mode in ("hot", "stream", "stream", "hot")]:
            raise ValueError(f"Broken ABBA order in {path}, repeat {repeat}")
        pairs.extend([sample[1][2] / sample[0][2], sample[2][2] / sample[3][2]])
    hot = [elapsed for _, mode, elapsed in rows if mode == "hot"]
    stream = [elapsed for _, mode, elapsed in rows if mode == "stream"]
    return {
        "file": str(path), "threads": threads, "groups": groups,
        "bytes_per_group": bytes_per_group, "tile_bytes": tile_bytes,
        "stream_bytes": stream_bytes, "checksum": checksum,
        "nonzero_bytes": nonzero_bytes,
        "hot_median_us": rounded_median(hot),
        "stream_median_us": rounded_median(stream),
        "paired_stream_over_hot_median": round(statistics.median(pairs), 3),
        "paired_stream_over_hot_min": round(min(pairs), 3),
        "paired_stream_over_hot_max": round(max(pairs), 3),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    runs = [summarize(path) for path in args.logs]
    payload = {"runs": runs, "comparison": "same ARMv8.2 W8 kernel and output; hot reuses 576 KiB weight tile, stream traverses 167 MiB"}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
