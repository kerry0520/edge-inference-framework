"""Offline identity/build/deployment/exit binding check for formal response evidence."""
import argparse
import json
from pathlib import Path
from recheck_prefill_response_elision import actual_exit, events, require, sha

ROOT = Path(__file__).resolve().parents[2]


def remote_hashes(path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    require(lines, "zero SHA matches")
    pairs = [line.split() for line in lines]
    require(all(len(x) == 2 and len(x[0]) == 64 for x in pairs), "invalid remote SHA record")
    require(len({x[1] for x in pairs}) == len(pairs), "duplicate remote SHA paths")
    return {x[1]: x[0] for x in pairs}


def check(directory):
    binding = json.loads((directory/"bindings.json").read_text(encoding="utf-8-sig"))
    require(binding["device"]["serial"] == "1e7c759e" and
            binding["device"]["model"] == "M2011K2C", "wrong declared device")
    identity = (directory/"commands/identity-stdout.log").read_text().splitlines()
    require(identity == ["M2011K2C", "1e7c759e", "venus"], "actual device identity differs")
    commands = list((directory/"commands").glob("*-exit.json"))
    require(commands and all(json.loads(p.read_text())["actual_exit"] == 0 for p in commands),
            "actual ADB command exit failed or unknown")
    model_before = remote_hashes(directory/"commands/model-identity-before-stdout.log")
    model_after = remote_hashes(directory/"commands/model-identity-after-stdout.log")
    historical = remote_hashes(ROOT/"results/android/prefill-head-elision-20261013/device-forward-v2/run/commands/model-identity-before-stdout.log")
    require(len(model_before) == 4 and model_before == model_after == historical and
            model_before == {e["remote"]: e["sha256"] for e in binding["assets"]},
            "fixed graph/weight/config/tokenizer identity changed")
    deployed_before = remote_hashes(directory/"commands/deployed-binding-before-stdout.log")
    deployed_after = remote_hashes(directory/"commands/deployed-binding-after-stdout.log")
    require(len(deployed_before) == 3 and deployed_before == deployed_after,
            "deployed worker/config/fixture changed")
    expected = {"worker": binding["binary_sha256"], "config.json": sha(directory/"config.json"),
                "fixture.json": sha(directory/"fixture.json")}
    require({Path(k).name: v for k, v in deployed_before.items()} == expected,
            "local vs deployed identities differ")
    require(binding["fixture_sha256"] == expected["fixture.json"] and
            binding["config_sha256"] == expected["config.json"], "fixture/config declaration differs")
    fixture = json.loads((directory/"fixture.json").read_text(encoding="utf-8-sig"))
    require(len(fixture["cases"]) == 7 and binding["approved_requests"] == 14,
            "approved request budget differs")
    require([len(c["input_ids"]) for c in fixture["cases"][:5]] == [31, 32, 33, 64, 65] and
            [c["max_new_tokens"] for c in fixture["cases"]] == [2, 2, 2, 2, 2, 16, 32],
            "fixed boundary/budget fixture differs")
    model_config = json.loads((directory/"config.json").read_text())
    require(model_config["backend_type"] == "cpu" and model_config["thread_num"] == 4 and
            model_config["precision"] == "low" and model_config["memory"] == "low" and
            model_config["chunk"] == 32 and model_config["attention_mode"] == 8 and
            model_config["dynamic_option"] == 0 and not model_config["async"] and
            not model_config["use_mmap"] and not model_config["use_cached_mmap"],
            "fixed diagnostic config differs")
    manifest = Path(binding["build_manifest"])
    require(sha(manifest) == binding["build_manifest_sha256"], "build manifest changed")
    build = json.loads(manifest.read_text())
    require(sha(build["binary_path"]) == build["binary_sha256"] == binding["binary_sha256"] and
            sha(build["candidate_library_path"]) == build["candidate_library_sha256"] == binding["library_sha256"],
            "built binary/library changed")
    for name, key in (("prefill_response_worker.cpp", "worker_sha256"),
                      ("prefill_response_elision.hpp", "helper_sha256")):
        require(sha(manifest.parent/"source"/name) == build[key] == binding[key],
                "worker/helper source binding differs")
    require(len(binding["runs"]) == 2 and {r["mode"] for r in binding["runs"]} == {"baseline", "candidate"},
            "route binding coverage")
    total_files = 0
    for run in binding["runs"]:
        route = directory/run["mode"]
        require(run["directory"] == run["mode"] and run["requests"] == 7 and
                run["native_exit"] == run["wrapper_exit"] == actual_exit(route/"native-exit.code") ==
                actual_exit(route/"wrapper-exit.code") == 0, "native/wrapper real exits differ")
        records = events(route/"stdout.log")
        exports = [e for e in records if e["event"] == "response_logits"]
        files = json.loads((route/"logits-manifest.json").read_text())
        require(len(files) == run["logits_files"] == len(exports) > 0 and
                len({e["path"] for e in files}) == len(files), "logits SHA manifest count differs")
        require(len([e for e in records if e["event"] == "response_forward_begin"]) == run["forwards"],
                "native forward count binding differs")
        remote = remote_hashes(directory/f'commands/{run["mode"]}-remote-logits-sha-stdout.log')
        for item in files:
            require(item["path"] == Path(item["remote"]).name and item["bytes"] == 607744 and
                    item["float_count"] == 151936 and (route/item["path"]).stat().st_size == item["bytes"] and
                    sha(route/item["path"]) == item["sha256"] == remote[item["remote"]],
                    "full logits remote/local SHA binding differs")
        require({e["file"] for e in exports} == {e["path"] for e in files}, "native capture file coverage differs")
        total_files += len(files)
    return {"passed": True, "model_replay": False, "device": binding["device"],
            "actual_command_exit_matches": len(commands), "remote_model_sha_matches": 4,
            "remote_complete_logit_sha_matches": total_files, "requests": 14,
            "binary_sha256": binding["binary_sha256"], "library_sha256": binding["library_sha256"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        result = check(args.run_dir)
    except (ValueError, KeyError, OSError, TypeError, json.JSONDecodeError) as e:
        result = {"passed": False, "model_replay": False, "error": str(e)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
