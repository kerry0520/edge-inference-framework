"""Supplement existing diagnostic cold records with explicit Android process exits."""
import argparse
import json
from pathlib import Path
import shutil
from phone_kv_prefix_session import SessionAdb, save, sha


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", required=True)
    p.add_argument("--record-dir", type=Path, required=True)
    args = p.parse_args()
    root = args.record_dir.resolve()
    output = root / "cold-exit-supplement"
    output.mkdir(exist_ok=False)
    shutil.copyfile(__file__, output / Path(__file__).name)
    source = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    adb = SessionAdb(args.serial)
    device = adb.select()
    assert device["hardware_serial"] == "1e7c759e"
    checks = []
    for item in source["sessions"]:
        group = root / (item["precision"] + "-" + str(item["repeat"]))
        session = json.loads((Path(item["directory"]) / "session.json").read_text(encoding="utf-8"))
        remote = session["remote"]
        for turn in range(2):
            sentinel = "DEVICE_COLD_EXIT="
            command = ("cd " + remote + "; ./full_worker full-config.json --cold full-request-" + str(turn)
                       + ".json; code=$?; echo " + sentinel + "$code")
            raw = adb.shell(command, timeout=900)
            name = item["precision"] + "-" + str(item["repeat"]) + "-" + str(turn)
            (output / (name + ".log")).write_bytes(raw)
            lines = raw.decode().splitlines()
            exits = [line[len(sentinel):] for line in lines if line.startswith(sentinel)]
            assert exits == ["0"], exits
            response = [json.loads(line) for line in lines if line.startswith('{"pid":')]
            assert len(response) == 1
            previous = [json.loads(line) for line in (group / ("full-" + str(turn) + ".log")).read_text(encoding="utf-8").splitlines() if line.startswith('{"pid":')]
            assert len(previous) == 1
            matches = {key: previous[0][key] == response[0][key] for key in ("tokens", "text", "status", "status_code")}
            assert all(matches.values()), matches
            checks.append({"precision": item["precision"], "repeat": item["repeat"], "turn": turn,
                           "device_exit": 0, "original_cold_matches": matches, "raw_sha256": sha(output / (name + ".log"))})
            save(output / "progress.json", checks)
            print(name + " exit0 original-cold-equal", flush=True)
    save(output / "summary.json", {"device": device, "checks": checks, "adb_commands": adb.commands,
                                   "scope": "Eight repeated cold executions, no additional live session; original run-1 retained unchanged"})


if __name__ == "__main__":
    main()
