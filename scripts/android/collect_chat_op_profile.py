"""Fresh four-request callback transparency experiment; no retry or resume."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from collect_chat_metrics import ROOT, run_session, save
from phone_chat_op_profile import profile_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "build-manifest.json").write_bytes(args.manifest.read_bytes())
    build_evidence = output / "build-evidence"
    build_evidence.mkdir()
    for name in ("command.json", "exit.code", "stdout.log", "stderr.log", "summary.json", "immutable-identities.json"):
        (build_evidence / name).write_bytes((args.manifest.parent / name).read_bytes())
    binary_bytes = args.binary.read_bytes()
    save(output / "binary-identity.json", {
        "path": str(args.binary.resolve().relative_to(ROOT)),
        "sha256": hashlib.sha256(binary_bytes).hexdigest(), "size": len(binary_bytes)})
    sources = [Path(__file__), Path(__file__).with_name("phone_chat_op_profile.py"),
               Path(__file__).with_name("collect_chat_metrics.py"),
               Path(__file__).with_name("phone_chat_metrics.py"),
               Path(__file__).with_name("phone_chat_session.py"),
               Path(__file__).with_name("phone_chat_transfer.py"),
               Path(__file__).with_name("phone_chat_cleanup.py"),
               Path(__file__).with_name("phone_infer.py"), args.source.resolve()]
    snapshot = output / "source"
    snapshot.mkdir()
    identities = {}
    for source in sources:
        data = source.read_bytes()
        (snapshot / source.name).write_bytes(data)
        identities[str(source.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
    save(output / "source-identity.json", identities)
    turns = [
        {"question": "Background: " + "The garden has green trees and quiet paths. " * 8 +
                     "Reply with only the word Hello.", "budget": 8},
        {"question": "Again reply with only the word Hello.", "budget": 8},
    ]
    save(output / "scenario.json", turns)
    save(output / "run.json", {
        "phase": "public_callback_transparency", "order": ["00-off", "01-on"],
        "max_requests": 4, "serial": args.serial, "callback_scope": "whole response",
        "binary_path": str(args.binary.resolve().relative_to(ROOT)),
        "manifest_path": str(args.manifest.resolve().relative_to(ROOT)),
        "worker_source": str(args.source.resolve().relative_to(ROOT)),
        "collector_sha256": identities[str(Path(__file__).relative_to(ROOT))],
        "rule": "No reconnect, replay or resubmit after unknown request outcome",
    })
    for label, enabled in (("00-off", False), ("01-on", True)):
        cls = profile_session(args.binary.resolve(), args.manifest.resolve(), args.source.resolve(), enabled)
        run_session(output / label, cls, args.serial, turns)
    after = {str(source.relative_to(ROOT)): hashlib.sha256(source.read_bytes()).hexdigest()
             for source in sources}
    save(output / "source-identity-after.json", after)
    if after != identities:
        raise RuntimeError("Sources changed during collection")
    save(output / "completion.json", {"exit_code": 0, "sessions": 2, "requests": 4})


if __name__ == "__main__":
    main()
