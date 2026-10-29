import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("kv_summary", ROOT / "scripts/android/summarize_phone_kv_record.py")
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def fixture():
    return {"session_id": "session", "history_mode": "kv_block32", "ready": {"pid": 42},
            "requests": [{"submitted_at_unix": 20, "request": {"id": "one", "session_token": "session",
                          "messages": [{"role": "user", "content": "hi"}], "max_new_tokens": 8, "max_context_tokens": 512},
                          "response": {"id": "one", "pid": 42, "event": "final", "ok": True,
                          "status": "max_tokens_finished", "status_code": 2, "tokens": [4], "text": "ok",
                          "stream_bytes_match": True, "stream_transport_ok": True, "history_mode": "kv_block32",
                          "message_count": 1, "committed_message_count": 2, "input_tokens": 65,
                          "prompt_tokens": 33, "reused_tokens": 32, "kv_block_size": 32,
                          "before_all_seq": 32, "reset_applied": False}}]}


class KVSummaryTests(unittest.TestCase):
    def test_explicit_policy_and_zero_unknown(self):
        record = fixture()
        self.assertEqual(summary.summarize_record(record)["events"][0]["classification"], "positive_reuse")
        response = record["requests"][0]["response"]
        response.update(prompt_tokens=65, reused_tokens=0)
        self.assertEqual(summary.summarize_record(record)["events"][0]["classification"], "zero_reuse_reason_unobserved")
        record.pop("history_mode")
        response.pop("history_mode")
        self.assertEqual(summary.summarize_record(record)["events"][0]["classification"], "policy_unobserved")

    def test_full_mode_does_not_need_kv_specific_fields(self):
        record = fixture()
        record["history_mode"] = "full_recompute"
        response = record["requests"][0]["response"]
        response.update(history_mode="full_recompute", prompt_tokens=65)
        response.pop("reused_tokens")
        self.assertEqual(summary.summarize_record(record)["events"][0]["classification"], "cross_turn_reuse_disabled")

    def test_contradictions_are_rejected(self):
        mutations = [lambda d: d["requests"][0]["response"].update(reused_tokens=31),
                     lambda d: d["requests"][0]["response"].update(history_mode="full_recompute"),
                     lambda d: d["requests"][0].update(remote_outcome="unknown"),
                     lambda d: d.update(pending_request=copy.deepcopy(d["requests"][0])),
                     lambda d: d["requests"][0]["response"].update(reset_applied=True),
                     lambda d: d["requests"][0]["response"].update(committed_message_count=3)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                record = fixture()
                mutate(record)
                with self.assertRaises(ValueError):
                    summary.summarize_record(record)

    def test_pending_is_unknown_even_if_it_contains_final(self):
        record = fixture()
        record["pending_request"] = record["requests"].pop()
        self.assertEqual(summary.summarize_record(record)["events"][0]["classification"], "transport_outcome_unknown")

    def test_pid_bool_or_reserved_is_not_a_worker_identity(self):
        for invalid in (True, False, 0, 1, "42"):
            with self.subTest(pid=invalid):
                record = fixture()
                record["ready"]["pid"] = invalid
                with self.assertRaises(ValueError):
                    summary.summarize_record(record)

    def test_reset_without_time_does_not_claim_association(self):
        record = fixture()
        trace = record["requests"][0]
        trace.pop("submitted_at_unix")
        trace["response"].update(prompt_tokens=65, reused_tokens=0, before_all_seq=0, reset_applied=True,
                                  reset_history=0, reset_output=0, reset_all_seq=0, reset_gen_seq=0)
        event = summary.summarize_record(record)["events"][0]
        self.assertTrue(event["reset_context_confirmed"])
        self.assertIsNone(event["preceding_confirmed_new_index"])
        self.assertEqual(event["reset_cause"], "unobserved")

    def test_budget_rejection_is_separate_and_validated(self):
        record = fixture()
        request = copy.deepcopy(record["requests"][0]["request"])
        request.update(id="rejected", max_new_tokens=512)
        record["rejected_requests"] = [{"state": "rejected", "request": request,
            "response": {"id": "rejected", "pid": 42, "event": "rejected", "reason": "context_budget_exceeded",
                         "input_tokens": 65, "max_new_tokens": 512, "max_context_tokens": 512}}]
        self.assertEqual(summary.summarize_record(record)["events"][-1]["classification"], "budget_rejected_not_committed")
        record["rejected_requests"][0]["response"]["input_tokens"] = 0
        with self.assertRaises(ValueError):
            summary.summarize_record(record)

    def test_cli_creates_only_new_output_and_preserves_input(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "session.json"
            raw = json.dumps(fixture()).encode()
            source.write_bytes(raw)
            output = base / "new"
            self.assertEqual(summary.main([str(source), "--output-dir", str(output)]), 0)
            with self.assertRaises(SystemExit) as error:
                summary.main([str(source), "--output-dir", str(output)])
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(source.read_bytes(), raw)
            self.assertTrue((output / "summary.json").is_file())


if __name__ == "__main__":
    unittest.main()
