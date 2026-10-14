"""Single-submit isolated stage-cost gate or ABBA; never replay a request."""
import argparse
import hashlib
import json
from pathlib import Path
import traceback

from collect_chat_metrics import ROOT, run_session, save
from collect_w4_execution_contract import extract_trace
from phone_chat_metrics import Full32StatsSession


PREFIX = "MNN_W4_STAGE_COST "


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def isolated_session(binary, manifest, source, label, enabled):
    class StageCostSession(Full32StatsSession):
        binary_path = binary
        manifest_path = manifest
        worker_source = source
        release = "/data/local/tmp/MNN/w4-stage-cost-20261013/" + label
        record_root = ROOT / "results/android/w4-stage-cost-20261013/isolated-sessions"
        extra_config = {"chunk": 32, "enable_debug": True,
                        "op_profile_enabled": False, "w4_stage_cost_enabled": enabled}

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.record["stage_cost_variant"] = label
            self.record["stage_cost_enabled"] = enabled
            self.persist()

        def start(self):
            result = super().start()
            ready = self.record["ready"]
            if ready.get("op_profile_enabled") is not False:
                raise RuntimeError("Callback profiling must remain disabled")
            if ready.get("w4_stage_cost_enabled") is not enabled:
                raise RuntimeError("Native stage-cost switch differs from requested switch")
            return result

        def validate_response(self, response):
            super().validate_response(response)
            profile = response.get("op_profile")
            if (not isinstance(profile, dict) or profile.get("version") != 1 or
                    profile.get("enabled") is not False or profile.get("balance") is not True or
                    any(type(profile.get(k)) is not int or profile[k] != 0
                        for k in ("before_count", "after_count", "pairs")) or
                    profile.get("op_aggregate") != [] or profile.get("samples") != []):
                raise RuntimeError("Unexpected public callback profiling")

    return StageCostSession


def validate_request_markers(stage, rows, enabled):
    session_dirs = list((stage / "sessions").iterdir())
    session = json.loads((session_dirs[0] / "session.json").read_text(encoding="utf-8"))
    expected = [request["response"]["id"].encode("utf-8").hex()
                for request in session["requests"]]
    begins, ends = [], []
    active = None
    totals = []
    for row in rows:
        trace = row["trace"]
        if trace.get("event") == "totals":
            if active is not None or trace.get("enabled") is not enabled:
                raise RuntimeError("Invalid final timing totals")
            if (trace.get("errors") != 0 or trace.get("clock_failures") != 0 or
                    trace.get("registrations_remaining") != 0 or
                    trace.get("request_open") is not False):
                raise RuntimeError("Unclosed native trace or native trace error")
            if not enabled and trace.get("hot_clock_calls") != 0:
                raise RuntimeError("Disabled variant made hot clock calls")
            totals.append(trace)
        elif trace.get("event") == "request_begin":
            if active is not None:
                raise RuntimeError("Nested request trace")
            if totals or trace.get("enabled") is not enabled:
                raise RuntimeError("Invalid request-begin switch or ordering")
            active = trace.get("id_hex")
            begins.append(active)
        elif trace.get("event") == "request_end":
            if active is None or trace.get("id_hex") != active:
                raise RuntimeError("Unmatched request-end marker")
            if trace.get("enabled") is not enabled or trace.get("clock_failures") != 0:
                raise RuntimeError("Invalid request-end switch or clock failures")
            if not enabled and (trace.get("executions") != 0 or trace.get("hot_clock_calls") != 0):
                raise RuntimeError("Disabled request recorded timed work")
            ends.append(active)
            active = None
        elif active is None:
            raise RuntimeError("Stage trace outside a request")
        elif trace.get("id_hex") != active:
            raise RuntimeError("Stage trace request identity mismatch")
        elif not enabled:
            raise RuntimeError("Disabled variant emitted execution trace")
    if (active is not None or begins != expected or ends != expected or not expected or
            len(totals) != 1):
        raise RuntimeError("Request markers differ from committed requests")
    save(stage / "marker-check.json", {"matched_requests": len(expected),
                                      "enabled": enabled, "request_ids_hex": expected})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--binary", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--worker-source", type=Path, required=True)
    p.add_argument("--phase", choices=("guard", "gate", "abba"), required=True)
    a = p.parse_args()
    for name in ("output", "binary", "manifest", "worker_source"):
        setattr(a, name, getattr(a, name).resolve())
    a.output.mkdir(parents=True, exist_ok=False)
    sources = [Path(__file__).resolve(), a.worker_source] + [
        Path(__file__).with_name(name).resolve() for name in (
            "collect_chat_metrics.py", "collect_w4_execution_contract.py", "phone_chat_metrics.py",
            "phone_chat_session.py", "phone_chat_transfer.py", "phone_chat_cleanup.py", "phone_infer.py")]
    snapshot = a.output / "source"
    snapshot.mkdir()
    identities = {}
    for source in sources:
        data = source.read_bytes()
        (snapshot / source.name).write_bytes(data)
        identities[str(source.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
    save(a.output / "source-identity.json", identities)
    manifest = json.loads(a.manifest.read_text(encoding="utf-8-sig"))
    if (manifest.get("binary_sha256") != sha(a.binary) or
            manifest.get("source_sha256") != sha(a.worker_source) or
            manifest.get("actual_compiler_exit", manifest.get("exit")) != 0):
        raise RuntimeError("Successful build and actual binary/source identities required")
    (a.output / "worker-build.json").write_bytes(a.manifest.read_bytes())
    save(a.output / "build-identities.json", {
        "binary": str(a.binary.relative_to(ROOT)), "binary_sha256": sha(a.binary),
        "manifest": str(a.manifest.relative_to(ROOT)), "manifest_sha256": sha(a.manifest)})
    turns = [
        {"question": "Background: " + "The garden has green trees and quiet paths. " * 8 +
                     "Reply with only the word Hello.", "budget": 8},
        {"question": "Again reply with only the word Hello.", "budget": 8},
    ]
    save(a.output / "scenario.json", turns)
    switches = [False] if a.phase == "guard" else (
        [False, True] if a.phase == "gate" else [False, True, True, False])
    sequence = [(f"{i:02d}-{'on' if enabled else 'off'}", enabled)
                for i, enabled in enumerate(switches)]
    state = {"phase": a.phase, "serial": a.serial, "order": [label for label, _ in sequence],
             "max_requests": len(sequence) * len(turns), "completed_sessions": [],
             "rule": "No reconnect, replay or resubmit after unknown request outcome",
             "trace_prefix": PREFIX, "performance_claim": False}
    save(a.output / "run.json", state)
    try:
        for label, enabled in sequence:
            state["active_session"] = label
            save(a.output / "run.json", state)
            cls = isolated_session(a.binary, a.manifest, a.worker_source, label, enabled)
            run_session(a.output / label, cls, a.serial, turns)
            rows = extract_trace(a.output / label, PREFIX)
            validate_request_markers(a.output / label, rows, enabled)
            state["completed_sessions"].append(label)
            state.pop("active_session", None)
            save(a.output / "run.json", state)
        after = {str(source.relative_to(ROOT)): sha(source) for source in sources}
        save(a.output / "source-identity-after.json", after)
        if after != identities:
            raise RuntimeError("Collection sources changed during execution")
        save(a.output / "completion.json", {"exit_code": 0, "sessions": len(sequence),
                                            "requests": len(sequence) * len(turns)})
    except BaseException as error:
        state["error"] = {"type": type(error).__name__, "message": str(error)}
        save(a.output / "run.json", state)
        (a.output / "collector-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
