"""Registered selection contracts; no device or inference is simulated as real."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_model_catalog as catalog
import phone_chat as chat
import phone_chat_session as full
import phone_chat_plan as plan
import phone_model_session as larger
from phone_http_backend import PhoneBackend


class CatalogTests(unittest.TestCase):
    def test_listing_is_independent_and_describes_different_policies(self):
        rows = catalog.list_models()
        self.assertEqual([r["id"] for r in rows], ["qwen3-0.6b", "qwen3-1.7b"])
        self.assertNotEqual(rows[0]["strategy"], rows[1]["strategy"])
        rows[1]["model_profile"] = "bad"
        self.assertEqual(catalog.resolve_model("qwen3-1.7b")["model_profile"],
                         "configs/models/qwen3-1.7b-mnn.json")

    def test_only_known_keys_and_fixed_profile(self):
        for key in ("other", "", None, True, {"model": "qwen3-1.7b"}):
            with self.subTest(key=key), self.assertRaises(ValueError):
                catalog.selection(key)
        _, factory, args = catalog.selection("qwen3-1.7b")
        self.assertIs(factory, larger.LargerModelSession)
        self.assertEqual(larger.resolve_model_profile(args["model_profile"]),
                         larger.model_profile_document())

    def test_small_model_preserves_existing_plan_policy(self):
        _, factory, args = catalog.selection("qwen3-0.6b")
        self.assertIs(factory, plan.PlanCacheSession)
        self.assertEqual(args, {})
        self.assertTrue(factory.plan_requested)

    def test_http_registered_model_forwards_profile_and_closes(self):
        session = Mock(transport_uncertain=False, record={}, history=[])
        factory = Mock(return_value=session)
        backend = PhoneBackend(model="qwen3-1.7b", session_factory=factory)
        backend.start()
        self.assertEqual(factory.call_args.kwargs["model_profile"],
                         catalog.ROOT / "configs/models/qwen3-1.7b-mnn.json")
        self.assertEqual(backend.info()["model"], "Qwen3-1.7B")
        backend.close()
        session.close.assert_called_once_with()

    def test_conflicts_and_unknown_reject_before_connection(self):
        with patch.object(full, "SessionAdb") as adb:
            for kw in ({"model": "other"}, {"model": "qwen3-0.6b", "model_profile": "x"},
                       {"model": "qwen3-1.7b", "profile_path": "x"},
                       {"model": "qwen3-1.7b", "adb_path": "x"}):
                with self.subTest(kw=kw), self.assertRaises(ValueError):
                    PhoneBackend(**kw)
            adb.assert_not_called()

    def test_invalid_registered_profile_never_falls_back(self):
        with patch.object(larger, "resolve_model_profile", side_effect=ValueError("invalid")), \
                patch.object(full, "SessionAdb") as adb:
            with self.assertRaisesRegex(ValueError, "invalid"):
                PhoneBackend(model="qwen3-1.7b")
            adb.assert_not_called()

    def invoke_chat(self, *args):
        with patch.object(sys, "argv", ["phone_chat.py", *args]):
            return chat.main()

    def test_cli_list_without_session(self):
        output = io.StringIO()
        with patch.object(full, "SessionAdb") as adb, contextlib.redirect_stdout(output):
            self.assertEqual(self.invoke_chat("--list-models"), 0)
            self.assertEqual(len(json.loads(output.getvalue())), 2)
            adb.assert_not_called()

    def test_cli_selected_model_reuses_existing_full_main_and_session(self):
        factory = Mock(return_value="session")
        spec = catalog.resolve_model("qwen3-1.7b")
        def main(**kwargs):
            value = kwargs["session_type"]("offline", max_context_tokens=512)
            self.assertEqual(value, "session")
            return 7
        with patch.object(catalog, "selection", return_value=(spec, factory, {"model_profile": "fixed.json"})), \
                patch.object(full, "main", side_effect=main):
            self.assertEqual(self.invoke_chat("--model", "qwen3-1.7b", "--prompt", "hello"), 7)
        factory.assert_called_once_with(serial="offline", model_profile="fixed.json", max_context_tokens=512)

    def test_cli_selected_policy_rejects_incompatible_routes(self):
        for flags in (("--mode", "kv"), ("--stats",), ("--plan-profile", "x"),
                      ("--max-context-tokens", "513")):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(full, "main") as main:
                with self.assertRaises(SystemExit):
                    self.invoke_chat("--model", "qwen3-1.7b", *flags)
                main.assert_not_called()

    def test_default_http_model_and_factory_stay_unchanged(self):
        backend = PhoneBackend()
        self.assertIs(backend._factory, plan.PlanCacheSession)
        self.assertNotIn("model_profile", backend._arguments)
        self.assertEqual(backend.info()["model"], "Qwen3-0.6B")


if __name__ == "__main__":
    unittest.main()
