"""On-device full-logit cache regression; uses isolated trial directories only."""
import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from compare_mnn_logits import read_dump, compare


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial")
    args = parser.parse_args()
    adb = ROOT / "tools/android/platform-tools/adb.exe"
    devices = subprocess.check_output([str(adb), "devices"], text=True)
    serial = args.serial or next(line.split()[0] for line in devices.splitlines() if "\tdevice" in line)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = "cache-logits-" + stamp
    output = ROOT / "results/android" / stem
    output.mkdir()
    remote = "/data/local/tmp/MNN/" + stem
    summary = {"result": "running", "serial": serial, "remote_trial": remote,
               "source_commit": "024a946b0b8fcf87c8a418229fadd4cd7858ffba",
               "driver": "diagnostic only; unchanged production libMNN.a; fixed tokens, no sampling",
               "atol": 1e-4, "rtol": 1e-5, "cases": [], "commands": [], "remote_cleaned": False}

    def run(*arguments):
        summary["commands"].append(list(arguments))
        return subprocess.check_output([str(adb), "-s", serial, *arguments], text=True, encoding="utf-8", errors="replace")

    def shell(command):
        return run("shell", command)

    def save():
        (ROOT / "results/android" / (stem + "-summary.json")).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    def comparison(reference, candidate):
        left, right = read_dump(reference), read_dump(candidate)
        report = compare(left, right, 1e-4, 1e-5)
        report["reference_nonfinite"] = sum(not math.isfinite(x) for row in left for x in row)
        report["candidate_nonfinite"] = sum(not math.isfinite(x) for row in right for x in row)
        report["compared_values"] = sum(len(row) for row in left)
        report["reference_sha256"], report["candidate_sha256"] = sha(reference), sha(candidate)
        report["all_values_finite"] = not (report["reference_nonfinite"] or report["candidate_nonfinite"])
        return report

    try:
        shell("mkdir " + remote)
        summary["builds"] = {}
        for variant in ("baseline", "candidate"):
            build_dir = ROOT / "build/mnn-cache-logits-024a946"
            binary = build_dir / ("cache_logits_" + variant)
            build = json.loads((build_dir / (variant + "-build.json")).read_text(encoding="utf-8-sig"))
            summary["builds"][variant] = build
            for filename in (variant + "-build.json", variant + "-compile.log"):
                (output / filename).write_bytes((build_dir / filename).read_bytes())
            run("push", str(binary), remote + "/" + variant)
            phone_hash = shell("sha256sum " + remote + "/" + variant).split()[0]
            assert phone_hash == sha(binary) == build["binary_sha256"]
            build["phone_sha256"] = phone_hash
            shell("chmod 755 " + remote + "/" + variant)
        for length, seed in ((7, 3), (129, 11)):
            case = {"prompt_length": length, "seed": seed, "stages": 5, "thread_num": 4, "runs": []}
            summary["cases"].append(case)
            for variant in ("baseline", "candidate"):
                trial = f"{remote}/{variant}-p{length}"
                shell("mkdir " + trial)
                paths = {}
                for phase, cached in (("cold", True), ("warm", True), ("uncached", False), ("after", True)):
                    config = json.loads((ROOT / "data/android_configs/config-phone-greedy-mmap-cached.json").read_text())
                    config.update(base_dir="/data/local/tmp/MNN/Qwen3-0.6B-MNN-modelscope/", tmp_path=trial + "/weights", use_cached_mmap=cached)
                    name = f"{variant}-p{length}-{phase}"
                    config_file = output / (name + ".json")
                    config_file.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
                    run("push", str(config_file), trial + "/config.json")
                    log = shell(f"cd {trial} && {remote}/{variant} {trial}/config.json {trial}/logits.bin {length} {seed}")
                    (output / (name + ".log")).write_text(log, encoding="utf-8")
                    observed = re.findall(r"stage=(\d+) logits=(\d+) input_count=(\d+) context_tokens=(\d+) ids=([\d,]+)", log)
                    assert len(observed) == 5, "missing observed forward shapes"
                    for stage, row in enumerate(observed):
                        count = length if stage == 0 else 1
                        ids = [16 + (seed * 53 + stage * 97 + i * 37) % 1000 for i in range(count)]
                        assert [int(x) for x in row[:4]] == [stage, 151936, count, length + stage]
                        assert [int(x) for x in row[4].strip(",").split(",")] == ids
                    dump = output / (name + ".bin")
                    run("pull", trial + "/logits.bin", str(dump))
                    paths[phase] = dump
                    state = shell("find " + trial + "/weights -type f -exec sha256sum {} \\;")
                    (output / (name + "-cache-state.log")).write_text(state, encoding="utf-8")
                    report = comparison(paths["cold"], dump)
                    report.update(variant=variant, phase=phase, config_path=trial + "/config.json", dump=str(dump.relative_to(ROOT)), cache_state=state)
                    report["observed_inputs_verified"] = True
                    case["runs"].append(report)
                    if variant == "candidate" or phase != "after":
                        assert report["outside_tolerance"] == 0 and report["all_values_finite"], name
                    else:
                        assert report["outside_tolerance"] > 0, "baseline failure not reproduced"
                    save()
                if variant == "baseline":
                    reference = paths["cold"]
                else:
                    case["baseline_vs_candidate_cold"] = comparison(reference, paths["cold"])
                    assert case["baseline_vs_candidate_cold"]["outside_tolerance"] == 0
                shell("find " + trial + "/weights -type f -delete")
                shell("rmdir " + trial + "/weights && rm " + trial + "/config.json " + trial + "/logits.bin && rmdir " + trial)
        summary["result"] = "pass"
    except Exception as error:
        summary["result"] = "failed"
        summary["error"] = repr(error)
        raise
    finally:
        save()
        # Exact isolated trial prefix; never touches model or main cache.
        remaining = shell("find " + remote + " -mindepth 1 -maxdepth 1 -type d")
        if not remaining.strip():
            shell("rm -f " + remote + "/baseline " + remote + "/candidate && rmdir " + remote)
            summary["remote_cleaned"] = True
            save()
        print(str(ROOT / "results/android" / (stem + "-summary.json")), flush=True)


if __name__ == "__main__":
    main()
