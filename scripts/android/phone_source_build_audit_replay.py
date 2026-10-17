"""Replay a pinned source-build audit using its original read-only tool outputs.

This performs current Python validation, not current LLVM execution or device
validation. The external receipt SHA binds evidence; it is not an author signature.
"""
import argparse
import json
from pathlib import Path

import phone_source_build_audit as audit

LABELS = ("archive-members", "elf-header", "elf-dynamic", "elf-segments")
FILES = ("audit.json", "derived.json", "input-bindings.json") + tuple(
    label + suffix for label in LABELS for suffix in ("-command.json", "-stdout.log", "-stderr.log"))
ARGUMENTS = ("prepared", "expected_prepared_sha256", "build", "expected_build_sha256",
             "llvm_ar", "expected_llvm_ar_sha256", "llvm_readelf", "expected_llvm_readelf_sha256",
             "candidate", "expected_candidate_sha256")


def load(path):
    return json.loads(audit.candidate.plain(str(path)).read_text(encoding="utf-8-sig"),
                      object_pairs_hook=audit.candidate.pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON constant")))


def same_json(left, right):
    """JSON numbers and booleans are different contract types, unlike dict ==."""
    def encoded(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return encoded(left) == encoded(right)


def bindings(directory):
    return {name: {"sha256": audit.candidate.sha(audit.candidate.plain(str(directory / name))),
                   "size": (directory / name).stat().st_size} for name in FILES}


def create_receipt(args, output):
    """Publish the success marker last; no historical audit is retroactively blessed."""
    result = load(output / "audit.json")
    audit.require(result["status"] == "build_audit_passed" and result["native_tool_commands_started"] == 4,
                  "receipt requires four successful original native commands")
    for label in LABELS:
        command = load(output / (label + "-command.json"))
        audit.require(type(command["actual_exit"]) is int and command["actual_exit"] == 0
                      and command["timed_out"] is False, "original command failed")
    audit.save(output / "receipt.json", {"schema_version": 1, "kind": "source_build_audit_receipt",
               "arguments": {key: getattr(args, key) for key in ARGUMENTS}, "files": bindings(output),
               "original_native_commands": 4, "production_admitted": False})


def inspect_receipt(path, expected):
    receipt_path, receipt = audit.candidate.read_pinned(path, expected)
    audit.require(set(receipt) == {"schema_version", "kind", "arguments", "files", "original_native_commands",
                                 "production_admitted"}, "unexpected receipt fields")
    audit.require(type(receipt["schema_version"]) is int and receipt["schema_version"] == 1
                  and receipt["kind"] == "source_build_audit_receipt"
                  and type(receipt["original_native_commands"]) is int and receipt["original_native_commands"] == 4
                  and receipt["production_admitted"] is False, "unsupported audit receipt")
    audit.require(type(receipt["arguments"]) is dict and set(receipt["arguments"]) == set(ARGUMENTS),
                  "unexpected receipt arguments")
    audit.require(type(receipt["files"]) is dict and set(receipt["files"]) == set(FILES), "receipt file inventory differs")
    for row in receipt["files"].values():
        audit.require(type(row) is dict and set(row) == {"sha256", "size"}
                      and audit.candidate.valid_hash(row["sha256"])
                      and type(row["size"]) is int and row["size"] >= 0, "invalid receipt file binding")
    directory = receipt_path.parent
    audit.require(bindings(directory) == receipt["files"], "receipt evidence bytes differ")
    return receipt_path, receipt


def new_output(value, receipt_path, receipt):
    output = Path(value).absolute()
    audit.require(not output.exists() and not output.is_symlink(), "output exists; preserve previous evidence")
    for parent in (output.parent, *output.parent.parents):
        audit.require(not parent.is_symlink() and not (getattr(parent.lstat(), "st_file_attributes", 0) & 0x400),
                      "output ancestry contains a link")
    output = output.resolve()
    args = argparse.Namespace(**receipt["arguments"])
    _, plan = audit.candidate.read_pinned(args.prepared, args.expected_prepared_sha256)
    protected_paths = [receipt_path.parent]
    protected_paths += [Path(plan[key]).resolve() for key in
                        ("output", "records", "source_directory", "dependency_directory", "worker_directory", "build_directory")]
    inputs = load(receipt_path.parent / "input-bindings.json")
    protected_paths += [audit.canonical(path) for path in inputs["files"]]
    for protected in protected_paths:
        audit.require(not output.is_relative_to(protected) and not protected.is_relative_to(output),
                      "replay output overlaps inputs or original evidence")
    output.mkdir()
    return output


def replay(path, expected, output, *, root=audit.ROOT):
    receipt_path, receipt = inspect_receipt(path, expected)
    args = argparse.Namespace(**receipt["arguments"])
    original = receipt_path.parent
    inputs = load(original / "input-bindings.json")
    audit.require(type(inputs) is dict and set(inputs) == {"files", "historical_path_aliases"}, "invalid input bindings")
    audit.require(type(inputs["files"]) is dict and bool(inputs["files"]), "empty input bindings")
    for name, digest in inputs["files"].items():
        audit.require(audit.candidate.valid_hash(digest) and audit.candidate.sha(audit.candidate.plain(name)) == digest,
                      "original external input changed")
    for row in inputs["historical_path_aliases"]:
        audit.require(set(row) == {"recorded", "resolved"}
                      and str(audit.canonical(row["recorded"])) == row["resolved"], "historical path alias changed")
    original_result = load(original / "audit.json")
    audit.require(original_result.get("status") == "build_audit_passed"
                  and type(original_result.get("native_tool_commands_started")) is int
                  and original_result["native_tool_commands_started"] == 4, "original audit was not successful")
    calls = []

    def recorded_tool(destination, label, argv, state):
        audit.require(len(calls) < 4 and label == LABELS[len(calls)], "replay command order differs")
        command = load(original / (label + "-command.json"))
        audit.require(set(command) == {"arguments", "actual_exit", "timed_out"}
                      and command["arguments"] == [str(value) for value in argv]
                      and type(command["actual_exit"]) is int and command["actual_exit"] == 0
                      and command["timed_out"] is False, "original native command differs or failed")
        calls.append(label)
        for suffix in ("-stdout.log", "-stderr.log"):
            with (destination / (label + suffix)).open("xb") as stream:
                stream.write((original / (label + suffix)).read_bytes())
        audit.save(destination / (label + "-replayed-command.json"),
                   {"arguments": command["arguments"], "original_command_path": str(original / (label + "-command.json")),
                    "original_command_sha256": audit.candidate.sha(original / (label + "-command.json")),
                    "original_actual_exit": 0,
                    "original_timed_out": False, "current_native_execution": False})
        return (original / (label + "-stdout.log")).read_bytes().decode("utf-8", errors="strict")

    current = audit.audit(args, output, {"native_tool_commands_started": 0}, root=root,
                          tool_runner=recorded_tool, write_receipt=False)
    audit.require(calls == list(LABELS), "not all original commands consumed")
    audit.require(same_json(load(output / "derived.json"), load(original / "derived.json")), "recomputed derived evidence differs")
    audit.require(same_json(load(output / "input-bindings.json"), inputs), "recomputed input bindings differ")
    expected_current = dict(original_result, native_tool_commands_started=0)
    audit.require(same_json(current, expected_current), "recomputed audit result differs")
    inspect_receipt(path, expected)
    result = {"schema_version": 1, "kind": "source_build_audit_replay", "status": "audit_replay_passed",
              "receipt_sha256": expected, "original_native_commands": 4, "current_native_commands": 0,
              "device_commands": 0, "model_requests": 0, "compilation_performed": False,
              "candidate_status": "candidate_pending", "production_admitted": False,
              "actual_object_count": current["actual_object_count"], "assets": current["assets"],
              "scope": "Current Python checks with pinned original LLVM output; no current native or device validation."}
    audit.save(output / "replay.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--expected-receipt-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = None
    try:
        receipt_path, receipt = inspect_receipt(args.receipt, args.expected_receipt_sha256)
        output = new_output(args.output, receipt_path, receipt)
        result = replay(args.receipt, args.expected_receipt_sha256, output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        if output is not None:
            audit.save(output / "replay-failure.json", {"status": "audit_replay_failed", "error": str(error),
                       "production_admitted": False, "current_native_commands": 0,
                       "device_commands": 0, "model_requests": 0})
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
