"""Reject malformed copies of evidence without changing the original records."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from recheck_query_scale import audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--copies-dir", type=Path, required=True)
    args = parser.parse_args()
    original = args.record_dir.resolve()
    copies = args.copies_dir.resolve()
    require_root = Path(__file__).resolve().parents[2] / "build"
    if not copies.is_relative_to(require_root.resolve()):
        raise ValueError("Copies must stay inside this project's build directory")
    copies.mkdir(parents=True, exist_ok=False)
    tracked = {}
    checks = []
    for case in ("scale-formula", "truncated-logits", "cold-input-id"):
        destination = copies / case
        destination.mkdir()
        for name in ("summary.json", "model-before.json", "model-after.json"):
            shutil.copyfile(original / name, destination / name)
        for mode in ("candidate", "cold"):
            src = original / ("low-" + mode)
            dst = destination / ("low-" + mode)
            (dst / "pulled").mkdir(parents=True)
            shutil.copyfile(src / "stdout.log", dst / "stdout.log")
            for file in (src / "pulled").iterdir():
                if file.name.startswith(("scale-", "sample-")):
                    shutil.copyfile(file, dst / "pulled" / file.name)
        if case == "scale-formula":
            relative = Path("low-candidate/pulled/scale-0-h0.json")
            path = destination / relative
            record = json.loads(path.read_text())
            record["q_scale"][0] = 1.0
            path.write_text(json.dumps(record), encoding="utf-8")
        elif case == "truncated-logits":
            relative = Path("low-candidate/pulled/sample-0.f32")
            path = destination / relative
            path.write_bytes(path.read_bytes()[:-4])
        else:
            relative = Path("low-cold/stdout.log")
            path = destination / relative
            path.write_text(path.read_text().replace("COLD_FULL_TOKENS=151644,", "COLD_FULL_TOKENS=151645,", 1), encoding="utf-8")
        tracked[relative.as_posix()] = hashlib.sha256((original / relative).read_bytes()).hexdigest()
        try:
            audit(destination)
        except ValueError as error:
            checks.append({"case": case, "rejected": True, "reason": str(error)})
        else:
            raise AssertionError("Malformed copy accepted: " + case)
    assert all(hashlib.sha256((original / relative).read_bytes()).hexdigest() == digest for relative, digest in tracked.items())
    print(json.dumps({"pass": True, "checks": checks, "original_mutation_targets_unchanged": True,
                      "scope": "Three offline malformed copies; no new phone requests."}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
