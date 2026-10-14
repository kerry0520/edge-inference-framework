"""Native metrics are validated before confirmed history can change."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"scripts/android"))
import phone_chat_metrics as metrics
import phone_chat_session as full
from test_phone_chat_session import frames as full_frames
from test_phone_chat_kv_session import frames as kv_frames
from test_phone_stream_session import FakeConnection


def frames(request, mode="full"):
    result = (full_frames if mode == "full" else kv_frames)(request)
    response = result[-1]
    response["full_input_ids"] = list(range(response["input_tokens"]))
    response["metrics"] = {"version": 1, "input_tokens": response["input_tokens"],
                           "prefill_tokens": response["prompt_tokens"],
                           "reused_tokens": response.get("reused_tokens", 0),
                           "output_tokens": len(response["tokens"]), "decode_steps": 1,
                           "prefill_us": 100, "decode_us": 200, "response_wall_us": 400}
    return result


class MetricsTests(unittest.TestCase):
    def make(self, directory, mode="full", factory=None):
        cls = metrics.FullStatsSession if mode == "full" else metrics.KvStatsSession
        session = cls("offline", Path(directory))
        session.record["ready"] = {"pid": 123}
        session.socket = FakeConnection(factory or (lambda r: frames(r, mode)))
        return session

    def test_full_and_kv_success_and_last_confirmed_request(self):
        for mode in ("full", "kv"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                session = self.make(d, mode)
                self.assertIsNone(session.last_stats)
                session.ask("first"); session.ask("second")
                self.assertEqual(len(session.history), 4)
                self.assertEqual(session.last_stats["reused_tokens"], 64 if mode == "kv" else 0)
                self.assertEqual(session.record["requests"][-1]["metrics_summary"], session.last_stats)
                for phrase in ("token", "ms", "电脑客户端", "不是TTFT"):
                    self.assertIn(phrase, session.format_stats())
                self.assertIsNotNone(session.last_stats["client_first_text_seconds"])
                self.assertGreaterEqual(session.last_stats["client_tail_seconds"], 0)
                self.assertIn("不是原生TTFT", session.format_stats())

    def test_postcommit_timing_error_does_not_damage_success(self):
        with tempfile.TemporaryDirectory() as d:
            session = self.make(d)
            with patch.object(metrics, "derive_client_text_timing", side_effect=ValueError("bad trace")):
                response = session.ask("good")
            self.assertEqual(len(session.history), 2)
            self.assertEqual(len(session.record["requests"]), 1)
            self.assertFalse(session.transport_uncertain)
            self.assertEqual(session.last_stats["request_id"], response["id"])
            self.assertIsNone(session.last_stats["client_first_text_seconds"])
            self.assertEqual(session.last_stats["client_text_timing_error"], "bad trace")
            self.assertIn("无可用", session.format_stats())

    def test_stats_display_does_not_create_request_or_change_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            session = self.make(d); session.ask("good")
            before = deepcopy(session.record)
            self.assertEqual(session.format_stats(), session.format_stats())
            self.assertEqual(session.record, before)

    def test_every_integer_field_rejects_bool_negative_float_and_missing(self):
        keys = frames({"id":"x", "messages":[{}]})[-1]["metrics"]
        for key in keys:
            for bad in (True, -1, 1.5, None):
                with self.subTest(key=key, bad=bad), tempfile.TemporaryDirectory() as d:
                    def factory(request):
                        output = frames(request)
                        output[-1]["metrics"][key] = bad
                        return output
                    session = self.make(d, factory=factory)
                    with self.assertRaises(RuntimeError):
                        session.ask("bad")
                    self.assertEqual(session.history, [])
                    self.assertEqual(session.record["requests"], [])
                    self.assertIsNone(session.last_stats)
                    self.assertTrue(session.transport_uncertain)

    def test_inconsistent_counters_never_commit(self):
        for key, bad in (("version",2), ("input_tokens",21), ("prefill_tokens",19),
                         ("reused_tokens",1), ("output_tokens",3), ("decode_steps",2),
                         ("response_wall_us",299)):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as d:
                def factory(request):
                    result=frames(request);result[-1]["metrics"][key]=bad;return result
                session=self.make(d, factory=factory)
                with self.assertRaises(RuntimeError):session.ask("bad")
                self.assertEqual(session.history, [])

    def test_max_budget_last_token_still_decodes(self):
        with tempfile.TemporaryDirectory() as d:
            def factory(request):
                result=frames(request)
                result[-1].update(status="max_tokens_finished",status_code=2)
                result[-1]["metrics"]["decode_steps"]=2
                return result
            session=self.make(d,factory=factory);session.ask("budget",max_new_tokens=2)
            self.assertEqual(session.last_stats["decode_steps"],2)

    def test_full_input_ids_required_before_commit(self):
        for mode in ("full", "kv"):
            for ids in (None, [], [0], [True]*20, [-1]*20, [0.0]*20):
                with self.subTest(mode=mode,ids=ids), tempfile.TemporaryDirectory() as d:
                    def factory(request):
                        result=frames(request,mode)
                        if ids is None:result[-1].pop("full_input_ids")
                        else:result[-1]["full_input_ids"]=ids
                        return result
                    session=self.make(d,mode,factory)
                    with self.assertRaises(RuntimeError):session.ask("bad")
                    self.assertEqual(session.history,[])
                    self.assertEqual(session.record["requests"],[])
                    self.assertIsNone(session.last_stats)

    def test_rejection_and_interrupt_keep_last_success_snapshot(self):
        for failure in ("rejected", "interrupt", "duplicate"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as d:
                session=self.make(d);session.ask("good")
                before=deepcopy(session.last_stats);history=deepcopy(session.history)
                if failure=="rejected":
                    session.socket=FakeConnection(lambda r:[{"id":r["id"],"pid":123,"load_count":1,
                        "event":"rejected","reason":"context_budget_exceeded","input_tokens":2040,
                        "max_new_tokens":64,"max_context_tokens":2048}])
                    with self.assertRaises(full.ContextBudgetExceeded):session.ask("long")
                elif failure=="interrupt":
                    with self.assertRaises(KeyboardInterrupt):
                        session.ask("interrupt",on_text=lambda text: (_ for _ in ()).throw(KeyboardInterrupt()))
                else:
                    def factory(request):
                        result=frames(request);result[1]["seq"]=0;return result
                    session.socket=FakeConnection(factory)
                    with self.assertRaises(RuntimeError):session.ask("duplicate")
                self.assertEqual(session.last_stats,before)
                self.assertEqual(session.history,history)

    def test_default_full_assets_remain_unchanged(self):
        self.assertEqual(full.Session.binary_path.name,"phone_chat_worker")
        self.assertEqual(full.Session.extra_config,{})
        self.assertEqual(metrics.Full32StatsSession.extra_config,{"chunk":32})


class ClientTextTimingTests(unittest.TestCase):
    def trace(self, chunks, times=None, final_at=1.0, elapsed=1.2):
        times = times or [0.1 * (i + 1) for i in range(len(chunks))]
        frames = [{"message": {"id": "x", "event": "chunk", "seq": i,
                               "bytes_hex": raw.hex()}, "elapsed_seconds": at}
                  for i, (raw, at) in enumerate(zip(chunks, times))]
        response = {"id": "x", "event": "final", "chunk_count": len(chunks),
                    "text": b"".join(chunks).decode("utf-8")}
        frames.append({"message": response, "elapsed_seconds": final_at})
        return {"frames": frames, "response": response, "elapsed_seconds": elapsed}

    def test_multiple_chunks_use_first_decodable_text(self):
        trace = self.trace([b"Hello", b" world"], [0.25, 0.6])
        self.assertEqual(metrics.derive_client_text_timing(trace), {
            "client_first_text_seconds": 0.25, "client_final_frame_seconds": 1.0,
            "client_tail_seconds": 0.75})

    def test_empty_and_split_utf8_chunks_are_not_visible_text(self):
        trace = self.trace([b"", b"\xe4", b"\xb8", b"\xad", b"A"],
                           [0.0, 0.1, 0.2, 0.3, 0.5])
        self.assertEqual(metrics.derive_client_text_timing(trace)["client_first_text_seconds"], 0.3)

    def test_no_text_eos_has_null_first_and_tail(self):
        # Pure trace derivation supports no text; existing chat still rejects
        # an empty answer before commit, so this does not relax business rules.
        for chunks in ([], [b""]):
            timing = metrics.derive_client_text_timing(self.trace(chunks))
            self.assertIsNone(timing["client_first_text_seconds"])
            self.assertIsNone(timing["client_tail_seconds"])
            self.assertEqual(timing["client_final_frame_seconds"], 1.0)

    def test_zero_and_equal_timestamps_are_valid(self):
        timing = metrics.derive_client_text_timing(self.trace([b"a"], [0], 0, 0))
        self.assertEqual(timing["client_first_text_seconds"], 0)
        self.assertEqual(timing["client_tail_seconds"], 0)

    def test_nonfinite_bool_negative_or_nonmonotonic_times_rejected(self):
        for bad in (float("nan"), float("inf"), -1, True, "0.1", None):
            for location in ("frame", "elapsed"):
                with self.subTest(bad=bad, location=location):
                    trace = self.trace([b"a"])
                    if location == "frame": trace["frames"][0]["elapsed_seconds"] = bad
                    else: trace["elapsed_seconds"] = bad
                    with self.assertRaises(ValueError): metrics.derive_client_text_timing(trace)
        for trace in (self.trace([b"a", b"b"], [0.7, 0.5]),
                      self.trace([b"a"], [1.1]), self.trace([b"a"], elapsed=0.9)):
            with self.assertRaises(ValueError): metrics.derive_client_text_timing(trace)

    def test_invalid_final_identity_bytes_and_sequence_rejected(self):
        for kind in ("identity", "final", "bytes", "sequence", "missing"):
            with self.subTest(kind=kind):
                trace = self.trace([b"a"])
                if kind == "identity": trace["frames"][0]["message"]["id"] = "other"
                elif kind == "final": trace["response"]["text"] = "b"
                elif kind == "bytes": trace["frames"][0]["message"]["bytes_hex"] = "aZ"
                elif kind == "sequence": trace["frames"][0]["message"]["seq"] = True
                else: trace["frames"].pop()
                with self.assertRaises(ValueError): metrics.derive_client_text_timing(trace)


if __name__=="__main__":unittest.main()
