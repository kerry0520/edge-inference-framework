"""Explain recorded session evidence without running a model or guessing KV decisions."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path


RESET_FIELDS = ("reset_history", "reset_output", "reset_all_seq", "reset_gen_seq")


def require(value, message):
    if not value:
        raise ValueError(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def array(record, name):
    value = record.get(name, [])
    require(isinstance(value, list), name + " must be a list")
    return value


def summarize_record(record):
    """Return evidence classifications, not engine cache-miss reasons or a replay verdict."""
    require(isinstance(record, dict), "session must be an object")
    require(isinstance(record.get("session_id"), str) and record["session_id"], "missing session_id")
    mode = record.get("history_mode")
    diagnostics = ["Native computed-prefix and per-layer KV contents were not observed by this reader."]
    if mode not in ("full_recompute", "kv_block32"):
        diagnostics.append("Unknown/missing history_mode; cross-turn policy is not inferred.")
    events = []
    seen = set()
    request_times = [trace.get("submitted_at_unix") if isinstance(trace, dict) else None
                     for trace in array(record, "requests")]
    ordered_clock = all(timestamp(at) for at in request_times) and all(
        left < right for left, right in zip(request_times, request_times[1:]))
    if not ordered_clock:
        diagnostics.append("Committed request timestamps missing/nonmonotonic; no reset/new timeline association inferred.")
    associated_resets = set()
    ready = record.get("ready", {})
    require(isinstance(ready, dict), "ready must be an object")
    require("pid" not in ready or integer(ready["pid"], 2), "invalid ready worker pid")

    def identity(trace):
        require(isinstance(trace, dict) and isinstance(trace.get("request"), dict), "missing request object")
        request = trace["request"]
        request_id = request.get("id")
        require(isinstance(request_id, str) and request_id, "missing request id")
        require(request_id not in seen, "request id occurs in multiple outcome lists: " + request_id)
        seen.add(request_id)
        token = request.get("session_token")
        require(token is None or token == record["session_id"], "request session mismatch")
        return request, request_id

    def response_identity(response, request_id):
        require(isinstance(response, dict) and response.get("id") == request_id, "response request mismatch")
        require("pid" not in response or integer(response["pid"], 2), "invalid response worker pid")
        if "pid" in ready:
            require(response.get("pid") == ready["pid"] and type(response.get("pid")) is int,
                    "response worker pid mismatch")
        if "load_count" in response:
            require(type(response["load_count"]) is int and response["load_count"] == 1, "invalid load count")

    resets = []
    for index, reset in enumerate(array(record, "conversation_resets")):
        require(isinstance(reset, dict) and isinstance(reset.get("reply"), dict), "invalid reset record")
        reply = reset["reply"]
        require("pid" not in reply or integer(reply["pid"], 2), "invalid reset worker pid")
        require(reply.get("event") == "new" and reply.get("ok") is True and
                type(reply.get("committed_message_count")) is int and reply["committed_message_count"] == 0 and
                all(type(reply.get(k)) is int and reply[k] == 0 for k in RESET_FIELDS), "reset not confirmed")
        if "pid" in ready:
            require(type(reply.get("pid")) is int and reply["pid"] == ready["pid"], "reset worker mismatch")
        at = reset.get("at_unix")
        resets.append(at if timestamp(at) else None)
        events.append({"kind": "reset", "classification": "explicit_new_confirmed", "reset_index": index,
                       "at_unix": at if timestamp(at) else None,
                       "meaning": "Context counters and committed messages reset; buffer release not proven."})

    for trace in array(record, "requests"):
        request, request_id = identity(trace)
        require(trace.get("remote_outcome") != "unknown" and trace.get("state") not in
                ("stream_unconfirmed", "submission_started", "sent_waiting_response", "receiving_chunks"),
                "unconfirmed trace stored as committed request")
        response = trace.get("response")
        response_identity(response, request_id)
        require(response.get("event") == "final" and response.get("ok") is True and
                response.get("status") in ("normal_finished", "max_tokens_finished"), "committed final is not successful")
        require(isinstance(response.get("text"), str) and response["text"] and
                response.get("stream_bytes_match") is True and response.get("stream_transport_ok") is True,
                "committed response lacks confirmed stream flags/text")
        require(type(response.get("status_code")) is int and response["status_code"] ==
                {"normal_finished": 1, "max_tokens_finished": 2}[response["status"]], "final status code mismatch")
        require(isinstance(response.get("tokens"), list) and response["tokens"] and
                all(integer(token) for token in response["tokens"]), "invalid generated token IDs")
        messages = request.get("messages")
        require(isinstance(messages, list) and messages and
                integer(response.get("message_count"), 1) and response["message_count"] == len(messages) and
                integer(response.get("committed_message_count"), 1) and response["committed_message_count"] == len(messages) + 1,
                "committed message count mismatch")
        response_mode = response.get("history_mode")
        require(mode is None or response_mode is None or mode == response_mode, "history mode disagreement")
        policy = mode if mode is not None else response_mode
        full, prefill = response.get("input_tokens"), response.get("prompt_tokens")
        require(integer(full, 1) and integer(prefill, 1) and prefill <= full, "invalid input/prefill counts")
        generated, budget = request.get("max_new_tokens"), request.get("max_context_tokens")
        require(integer(generated, 1) and integer(budget, 1) and full + generated <= budget,
                "committed request exceeds or lacks deployment budget")
        reused = response.get("reused_tokens")
        if policy == "full_recompute":
            require(full == prefill and (reused is None or type(reused) is int and reused == 0),
                    "full recompute contradicts reuse counters")
            classification = "cross_turn_reuse_disabled"
        elif policy == "kv_block32":
            require(integer(reused) and full == prefill + reused and reused % 32 == 0 and
                    type(response.get("kv_block_size")) is int and response["kv_block_size"] == 32 and
                    integer(response.get("before_all_seq")) and reused <= response["before_all_seq"],
                    "inconsistent block32 reuse counters")
            classification = "positive_reuse" if reused else "zero_reuse_reason_unobserved"
        else:
            if reused is not None:
                require(integer(reused) and full == prefill + reused, "contradictory reuse counters")
            classification = "policy_unobserved"
        reset_confirmed = response.get("reset_applied") is True
        require("reset_applied" not in response or type(response["reset_applied"]) is bool,
                "invalid reset flag")
        if reset_confirmed:
            require((reused is None or reused == 0) and
                    all(type(response.get(k)) is int and response[k] == 0 for k in RESET_FIELDS),
                    "reset snapshot contradicts reuse/state")
        submitted = trace.get("submitted_at_unix")
        relation = None
        if reset_confirmed and ordered_clock:
            eligible = [(at, i) for i, at in enumerate(resets) if at is not None and at < submitted and
                        i not in associated_resets and not any(at < other < submitted for other in request_times)]
            if eligible:
                relation = max(eligible)[1]
                associated_resets.add(relation)
        # A preceding new timestamp alone cannot prove this reset was its direct effect.
        event = {"kind": "committed_request", "request_id": request_id,
                 "at_unix": submitted if timestamp(submitted) else None,
                 "classification": classification, "input_tokens": full, "prefill_tokens": prefill,
                 "reused_tokens": reused, "reuse_count_source": "final_field" if reused is not None else "absent",
                 "reset_context_confirmed": reset_confirmed, "preceding_confirmed_new_index": relation,
                 "reset_cause": "unobserved", "computed_prefix_verified": False,
                 "input_ids_present": "full_input_ids" in response}
        if "full_input_ids" in response:
            event["input_ids_diagnostic"] = "Presence only; ID contents/length and numerical outputs were not verified."
        if classification == "zero_reuse_reason_unobserved":
            event["diagnostic"] = "Zero reuse does not identify cold start, prefix mismatch or an engine eligibility failure."
        if reset_confirmed and not timestamp(submitted):
            event["reset_timeline_diagnostic"] = "Request timestamp absent; no reset/new association inferred."
        events.append(event)

    for trace in array(record, "rejected_requests"):
        request, request_id = identity(trace)
        response = trace.get("response")
        response_identity(response, request_id)
        require(trace.get("state") == "rejected" and response.get("event") == "rejected" and
                response.get("reason") == "context_budget_exceeded", "unsupported rejection")
        total, generated, budget = (response.get(k) for k in ("input_tokens", "max_new_tokens", "max_context_tokens"))
        require(integer(total, 1) and integer(generated, 1) and integer(budget, 1) and total + generated > budget and
                integer(request.get("max_new_tokens"), 1) and integer(request.get("max_context_tokens"), 1) and
                generated == request.get("max_new_tokens") and budget == request.get("max_context_tokens"),
                "invalid budget rejection counts")
        events.append({"kind": "rejected_request", "request_id": request_id,
                       "classification": "budget_rejected_not_committed", "at_unix": trace.get("submitted_at_unix"),
                       "meaning": "Recorded in rejection list and absent from local committed IDs; history before/after was not proven; not a KV miss."})

    pending = record.get("pending_request")
    if pending is not None:
        _, request_id = identity(pending)
        events.append({"kind": "pending_request", "request_id": request_id,
                       "classification": "transport_outcome_unknown", "at_unix": pending.get("submitted_at_unix"),
                       "recorded_state": pending.get("state"), "meaning": "Not committed locally; never replay from this summary."})
    if record.get("new_error") is not None:
        events.append({"kind": "reset_unknown", "classification": "reset_outcome_unknown", "at_unix": None})
    if any(event.get("at_unix") is not None and not timestamp(event["at_unix"]) for event in events):
        diagnostics.append("Invalid event timestamps omitted; no total chronology inferred.")
        for event in events:
            if event.get("at_unix") is not None and not timestamp(event["at_unix"]):
                event["at_unix"] = None
    return {"schema_version": 1, "session_id": record["session_id"], "history_mode": mode,
            "scope": "recorded protocol evidence, not native KV correctness, performance or current device state",
            "event_order": "grouped by recorded container, not a reconstructed global timeline",
            "session_status": record.get("status"), "events": events,
            "classification_counts": dict(Counter(event["classification"] for event in events)),
            "diagnostics": diagnostics}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="Explicit existing Session JSON, never an automatic directory scan")
    parser.add_argument("--output-dir", required=True, type=Path, help="New directory; existing paths are refused")
    args = parser.parse_args(argv)
    try:
        original = args.session.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        summary = summarize_record(json.loads(original.decode("utf-8-sig")))
        require(hashlib.sha256(args.session.read_bytes()).hexdigest() == digest, "input changed during analysis")
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")
    summary["source"] = {"path": str(args.session.resolve()), "sha256_before": digest, "sha256_after": digest}
    (args.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = ["# KV recorded-evidence summary", "", summary["scope"], "", "| Kind | Request | Classification | Input/prefill/reuse |", "|---|---|---|---|"]
    for event in summary["events"]:
        rows.append("| {} | {} | {} | {}/{}/{} |".format(event["kind"], event.get("request_id", ""), event["classification"],
                    event.get("input_tokens", ""), event.get("prefill_tokens", ""), event.get("reused_tokens", "")))
    rows.extend(["", "Native miss reasons and computed KV contents remain unobserved. Original records were not edited."])
    (args.output_dir / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(str(args.output_dir / "summary.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
