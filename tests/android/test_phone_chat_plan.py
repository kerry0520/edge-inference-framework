"""Offline acceptance for routing and pre-commit plan-path validation."""
from copy import deepcopy
import contextlib
import io
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_chat as router
import phone_chat_plan as plan
import phone_chat_session as full
from test_phone_chat_metrics import frames
import test_phone_chat_router
from test_phone_stream_session import FakeConnection


def output(request, count=65, candidate=True, hit=False):
    result = frames(request)
    final = result[-1]
    final.update(input_tokens=count, prompt_tokens=count, full_input_ids=list(range(count)))
    final["metrics"].update(input_tokens=count, prefill_tokens=count)
    blocks = (count + 31) // 32
    optimized = candidate and blocks > 1
    final["plan_cache"] = {"version": 1, "requested": candidate,
        "eligible": optimized, "prepared": optimized, "cache_hit": hit,
        "trunk_count": blocks - 1 if optimized else 0,
        "full_count": 1 + (1 if optimized else blocks), "decode_count": 1,
        "route_check_us": 2, "prepare_us": 3, "release_us": 0}
    return result


class PlanTests(unittest.TestCase):
    def session(self, directory, factory=None, baseline=False):
        cls = plan.BaselinePlanSession if baseline else plan.PlanCacheSession
        with patch.object(plan, "resolve_plan_build", return_value={"identity": "test"}):
            session = cls("offline", Path(directory))
        session.record["ready"] = {"pid": 123}
        session.socket = FakeConnection(factory or output)
        return session

    def test_router_opt_in_default_512_and_existing_default_unchanged(self):
        helper = test_phone_chat_router.RouterTests()
        self.assertEqual(helper.invoke(["--stats", "--prefill-plan-cache"], "phone_chat_plan"),
                         [["selected_session.py", "--max-new-tokens", "64", "--max-context-tokens", "512"]])
        helper.test_default_stays_full_and_preserves_default_budgets()

    def test_bad_router_combinations_never_import_or_connect(self):
        for args in (["--prefill-plan-cache"],
                     ["--stats", "--prefill-plan-cache", "--mode", "kv"],
                     ["--stats", "--prefill-plan-cache", "--mode", "query"],
                     ["--stats", "--prefill-plan-cache", "--stats-build-tag", "fresh"],
                     ["--stats", "--prefill-plan-cache", "--max-context-tokens", "513"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["chat"] + args), \
                    patch.object(router.importlib, "import_module") as load, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    router.main()
                self.assertEqual(error.exception.code, 2)
                load.assert_not_called()

    def test_cold_warm_short_and_new_keep_full_history_and_plan(self):
        seen = []
        def factory(request):
            seen.append(request)
            if request.get("command") == "new":
                return [{"event": "new", "ok": True, "pid": 123,
                         "committed_message_count": 0, "reset_history": 0,
                         "reset_output": 0, "reset_all_seq": 0, "reset_gen_seq": 0}]
            short = request["messages"][-1]["content"] == "short"
            return output(request, 20 if short else 65,
                          hit=not short and request["messages"][-1]["content"] != "cold")
        with tempfile.TemporaryDirectory() as directory:
            s = self.session(directory, factory)
            s.ask("cold"); s.ask("warm"); s.ask("short")
            self.assertIn("短输入直接完整执行", s.format_stats())
            self.assertIn("复用token指KV", s.format_stats())
            self.assertEqual(len(seen[1]["messages"]), 3)
            self.assertEqual(len(s.history), 6)
            self.assertEqual(s.last_stats["reused_tokens"], 0)
            s.new(); s.ask("warm again")
            self.assertIn("已复用", s.format_stats())
            self.assertEqual(len(seen[-1]["messages"]), 1)
            self.assertTrue(s._plan_seen)

    def test_actual_wrong_routes_and_types_reject_without_history_commit(self):
        for key, bad in (("trunk_count", 0), ("full_count", 4), ("decode_count", 0),
                         ("cache_hit", True), ("eligible", False), ("requested", False),
                         ("prepared", False), ("version", True), ("prepare_us", -1)):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                def factory(request):
                    response = output(request); response[-1]["plan_cache"][key] = bad
                    return response
                s = self.session(directory, factory)
                with self.assertRaises(RuntimeError):
                    s.ask("bad")
                self.assertEqual(s.history, [])
                self.assertEqual(s.record["requests"], [])
                self.assertFalse(s._plan_seen)
                self.assertTrue(s.transport_uncertain)
                with self.assertRaises(RuntimeError): s.ask("no replay")

    def test_budget_rejection_preserves_confirmed_history_and_cached_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            s = self.session(directory); s.ask("good")
            before = deepcopy(s.history)
            s.socket = FakeConnection(lambda r: [{"id": r["id"], "pid": 123,
                "load_count": 1, "event": "rejected", "reason": "context_budget_exceeded",
                "input_tokens": 500, "max_new_tokens": 64, "max_context_tokens": 512}])
            with self.assertRaises(full.ContextBudgetExceeded): s.ask("too long")
            self.assertEqual(s.history, before)
            self.assertTrue(s._plan_seen)
            self.assertFalse(s.transport_uncertain)

    def test_baseline_same_library_no_optimization_and_constructor_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            s = self.session(directory, lambda r: output(r, candidate=False), baseline=True)
            s.ask("baseline")
            self.assertFalse(s._plan_seen)
            self.assertFalse(s.extra_config["prefill_head_elision"])
        with patch.object(full, "SessionAdb") as adb:
            for budget in (True, 0, 513):
                with self.assertRaises(ValueError): plan.PlanCacheSession(max_context_tokens=budget)
            adb.assert_not_called()

    def test_missing_profile_rejected_before_adb(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError): plan.resolve_plan_build(directory)

    def test_close_requires_native_release_evidence_and_preserves_unknown(self):
        for owners, stopped, unknown in ((0, True, False), (1, True, False),
                                         (0, False, False), (1, True, True)):
            with self.subTest(owners=owners, stopped=stopped, unknown=unknown), \
                    tempfile.TemporaryDirectory() as directory:
                s = self.session(directory)
                events = [{"event": "plan_cache_closed", "registry_owner_count": owners}]
                if stopped:
                    events.append({"event": "stopped", "pid": 123, "load_count": 1, "exit": 0})
                (s.directory / "worker-stdout.log").write_text(
                    "\n".join(json.dumps(e, separators=(",", ":")) for e in events), encoding="utf-8")
                s.record.update(worker_exit=0, status="closed_with_unconfirmed_request" if unknown else "closed")
                with patch.object(plan.FullStatsSession, "close", return_value=None):
                    if owners == 0 and stopped:
                        s.close()
                        self.assertNotIn("plan_close_error", s.record)
                    else:
                        with self.assertRaises(RuntimeError): s.close()
                        self.assertEqual(s.record["status"], "closed_with_unconfirmed_request" if unknown else "stopped_with_error")


if __name__ == "__main__": unittest.main()
