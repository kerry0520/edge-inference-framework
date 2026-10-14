"""Isolated four-request W4 execution contract collection; never replay requests."""
import argparse
import hashlib
import json
from pathlib import Path
import traceback

from collect_chat_metrics import ROOT, run_session, save
from phone_chat_metrics import Full32StatsSession


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def isolated_session(binary, manifest, source, label):
    class W4ContractSession(Full32StatsSession):
        binary_path = binary
        manifest_path = manifest
        worker_source = source
        release = "/data/local/tmp/MNN/w4-execution-contract-20261013/" + label
        record_root = ROOT / "results/android/w4-execution-contract-20261013/isolated-sessions"
        extra_config = {"chunk": 32, "enable_debug": True, "op_profile_enabled": False}

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.record["w4_contract_variant"] = label
            self.persist()

        def start(self):
            result = super().start()
            if self.record["ready"].get("op_profile_enabled") is not False:
                raise RuntimeError("Callback must remain disabled in runtime dispatch experiment")
            return result

        def validate_response(self, response):
            super().validate_response(response)
            profile = response.get("op_profile")
            if (not isinstance(profile, dict) or profile.get("version") != 1 or
                    profile.get("enabled") is not False or profile.get("balance") is not True or
                    any(type(profile.get(k)) is not int or profile[k] != 0
                        for k in ("before_count", "after_count", "pairs")) or
                    profile.get("op_aggregate") != [] or profile.get("samples") != []):
                raise RuntimeError("Runtime dispatch experiment unexpectedly enabled callback profiling")

    return W4ContractSession


def extract_trace(stage, prefix):
    """stderr is intentionally merged into native worker-stdout by base launcher."""
    sessions = list((stage / "sessions").iterdir())
    if len(sessions) != 1:
        raise RuntimeError("Expected one native session")
    raw_path = sessions[0] / "worker-stdout.log"
    raw = raw_path.read_bytes()
    rows = []
    marker = prefix.encode("ascii")
    for number, line in enumerate(raw.splitlines(), 1):
        if line.startswith(marker):
            value = json.loads(line[len(marker):].decode("utf-8"))
            if not isinstance(value, dict):
                raise RuntimeError("W4 trace must be a JSON object")
            rows.append({"native_log_line": number, "trace": value})
    save(stage / "dispatch-trace.json", {
        "prefix": prefix, "native_log_path": str(raw_path.relative_to(ROOT)),
        "native_log_sha256": hashlib.sha256(raw).hexdigest(),
        "stderr_capture": "merged with stdout by unchanged native launcher",
        "count": len(rows), "rows": rows,
    })
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--baseline-binary", type=Path, required=True)
    p.add_argument("--baseline-manifest", type=Path, required=True)
    p.add_argument("--trace-binary", type=Path, required=True)
    p.add_argument("--trace-manifest", type=Path, required=True)
    p.add_argument("--worker-source", type=Path, required=True)
    p.add_argument("--trace-prefix", required=True)
    a = p.parse_args()
    if not a.trace_prefix or not a.trace_prefix.isascii() or "\n" in a.trace_prefix:
        p.error("Trace prefix must be one nonempty ASCII line")
    for name in ("output", "baseline_binary", "baseline_manifest", "trace_binary", "trace_manifest", "worker_source"):
        setattr(a, name, getattr(a, name).resolve())
    a.output.mkdir(parents=True, exist_ok=False)
    sources = [Path(__file__).resolve(), a.worker_source] + [
        Path(__file__).with_name(name).resolve() for name in (
            "collect_chat_metrics.py", "phone_chat_metrics.py", "phone_chat_session.py",
            "phone_chat_transfer.py", "phone_chat_cleanup.py", "phone_infer.py")]
    snapshots = a.output / "source"
    snapshots.mkdir()
    identities = {}
    for source in sources:
        data = source.read_bytes()
        (snapshots / source.name).write_bytes(data)
        identities[str(source.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
    save(a.output / "source-identity.json", identities)
    variants = [("00-guard", a.baseline_binary, a.baseline_manifest),
                ("01-trace", a.trace_binary, a.trace_manifest)]
    assets = {}
    for label, binary, manifest in variants:
        value = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if value.get("binary_sha256") != sha(binary) or value.get("source_sha256") != sha(a.worker_source):
            raise RuntimeError("Binary/source identity mismatch before device access")
        if value.get("actual_compiler_exit", value.get("exit")) != 0:
            raise RuntimeError("Build does not have a successful compiler exit")
        record = a.output / (label + "-build")
        record.mkdir()
        (record / "build.json").write_bytes(manifest.read_bytes())
        assets[label] = {"binary": str(binary.relative_to(ROOT)), "binary_sha256": sha(binary),
                         "manifest": str(manifest.relative_to(ROOT)), "manifest_sha256": sha(manifest)}
    save(a.output / "build-identities.json", assets)
    turns = [
        {"question": "Background: " + "The garden has green trees and quiet paths. " * 8 +
                     "Reply with only the word Hello.", "budget": 8},
        {"question": "Again reply with only the word Hello.", "budget": 8},
    ]
    save(a.output / "scenario.json", turns)
    state = {"phase": "w4_execution_contract", "serial": a.serial,
             "order": [v[0] for v in variants], "max_requests": 4, "completed_sessions": [],
             "rule": "No reconnect, replay or resubmit after unknown request outcome",
             "trace_prefix": a.trace_prefix, "performance_claim": False}
    save(a.output / "run.json", state)
    try:
        for label, binary, manifest in variants:
            state["active_session"] = label
            save(a.output / "run.json", state)
            cls = isolated_session(binary, manifest, a.worker_source, label)
            run_session(a.output / label, cls, a.serial, turns)
            rows = extract_trace(a.output / label, a.trace_prefix)
            if not rows:
                raise RuntimeError("Request markers absent from variant")
            state["completed_sessions"].append(label)
            state.pop("active_session", None)
            save(a.output / "run.json", state)
        after = {str(s.relative_to(ROOT)): sha(s) for s in sources}
        save(a.output / "source-identity-after.json", after)
        if after != identities:
            raise RuntimeError("Sources changed during collection")
        save(a.output / "completion.json", {"exit_code": 0, "sessions": 2, "requests": 4})
    except BaseException as error:
        state["error"] = {"type": type(error).__name__, "message": str(error)}
        save(a.output / "run.json", state)
        (a.output / "collector-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
