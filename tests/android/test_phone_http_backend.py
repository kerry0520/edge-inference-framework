"""Offline HTTP adapter checks; these mocks do not demonstrate phone inference."""
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_http_backend as backend
import phone_chat_plan as plan
import phone_chat_session as full


class FakeSession:
    def __init__(self):
        self.transport_uncertain = False
        self.history = [{"role": "user", "content": "confirmed"}]
        self.record = {"ready": {"pid": 123, "session_token": "secret"},
                       "session_id": "secret", "remote": "/private/path"}
        self.socket = Mock()
        self.start = Mock(return_value=self)
        self.ask = Mock(return_value={"text": "answer"})
        self.new = Mock()
        self.close = Mock()


class BackendTests(unittest.TestCase):
    def make(self, **kwargs):
        session = FakeSession()
        factory = Mock(return_value=session)
        wrapper = backend.PhoneBackend(session_factory=factory, **kwargs)
        return wrapper, session, factory

    def test_arguments_forwarded_without_profile_substitution(self):
        options = dict(serial="offline", profile_path=Path("admitted.json"),
                       adb_path=Path("adb"), max_context_tokens=256,
                       record_parent=Path("records"))
        wrapper, session, factory = self.make(**options)
        self.assertIs(wrapper.start(), wrapper)
        factory.assert_called_once_with(**options)
        session.start.assert_called_once_with()
        self.assertEqual(wrapper.info()["state"], "ready")

    def test_default_uses_original_class_and_no_candidate(self):
        session = FakeSession()
        with patch.object(backend, "PlanCacheSession", return_value=session) as factory:
            wrapper = backend.PhoneBackend()
            wrapper.start()
            factory.assert_called_once_with(serial=None, profile_path=None, adb_path=None,
                                            max_context_tokens=512, record_parent=None)

    def test_real_profile_rejection_is_not_bypassed(self):
        # Use the real PlanCacheSession constructor: its admission failure must
        # escape before ADB or an alternate session can be created.
        with patch.object(plan.PlanCacheSession, "resolve_runtime",
                          side_effect=ValueError("unadmitted profile")) as resolve, \
                patch.object(full, "SessionAdb") as adb:
            wrapper = backend.PhoneBackend(profile_path="unadmitted.json")
            with self.assertRaisesRegex(ValueError, "unadmitted"):
                wrapper.start()
            resolve.assert_called_once_with()
            adb.assert_not_called()
            self.assertEqual(wrapper.info()["state"], "closed")

    def test_real_default_rejects_adb_override_without_profile(self):
        wrapper = backend.PhoneBackend(adb_path="adb")
        with patch.object(full, "SessionAdb") as adb:
            with self.assertRaises(ValueError):
                wrapper.start()
            adb.assert_not_called()

    def test_start_failure_closes_constructed_session(self):
        wrapper, session, factory = self.make()
        session.start.side_effect = RuntimeError("not ready")
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            wrapper.start()
        session.close.assert_called_once_with()
        wrapper.close()
        session.close.assert_called_once_with()
        with self.assertRaises(RuntimeError):
            wrapper.start()
        factory.assert_called_once()

    def test_start_and_cleanup_failures_remain_observable(self):
        wrapper, session, _ = self.make()
        initial = RuntimeError("start failed")
        cleanup = OSError("cleanup failed")
        session.start.side_effect = initial
        session.close.side_effect = cleanup
        with self.assertRaises(RuntimeError) as caught:
            wrapper.start()
        self.assertIs(caught.exception, initial)
        self.assertIs(caught.exception.__cause__, cleanup)
        with self.assertRaises(OSError) as repeated:
            wrapper.close()
        self.assertIs(repeated.exception, cleanup)
        session.close.assert_called_once_with()

    def test_proxy_keeps_stream_callback_and_context_budget_exception(self):
        wrapper, session, _ = self.make()
        wrapper.start()
        callback = Mock()
        self.assertEqual(wrapper.ask("question", 23, callback), {"text": "answer"})
        session.ask.assert_called_once_with("question", max_new_tokens=23, on_text=callback)
        self.assertIs(backend.ContextBudgetExceeded, full.ContextBudgetExceeded)
        session.ask.side_effect = backend.ContextBudgetExceeded("budget")
        with self.assertRaises(backend.ContextBudgetExceeded):
            wrapper.ask("too long")
        self.assertFalse(wrapper.uncertain)
        self.assertEqual(len(wrapper.history()), 1)

    def test_new_delegates_and_history_is_deep_copy(self):
        wrapper, session, _ = self.make()
        wrapper.start()
        history = wrapper.history()
        history[0]["content"] = "changed"
        history.append({"role": "assistant", "content": "unconfirmed"})
        self.assertEqual(wrapper.history(), [{"role": "user", "content": "confirmed"}])
        wrapper.new()
        session.new.assert_called_once_with()

    def test_info_exposes_allowlist_only(self):
        wrapper, _, _ = self.make()
        wrapper.start()
        self.assertEqual(wrapper.info(), {
            "state": "ready", "uncertain": False, "model": "Qwen3-0.6B",
            "history_mode": "full_recompute", "max_context_tokens": 512,
            "worker_pid": 123})

    def test_interrupt_discards_transport_without_clearing_history(self):
        wrapper, session, _ = self.make()
        wrapper.start()
        wrapper.interrupt()
        self.assertTrue(wrapper.uncertain)
        self.assertTrue(session.transport_uncertain)
        session.socket.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        session.socket.sendall.assert_not_called()
        self.assertEqual(wrapper.history(), session.history)
        with self.assertRaises(RuntimeError):
            wrapper.ask("never replay")
        with self.assertRaises(RuntimeError):
            wrapper.new()
        session.ask.assert_not_called()
        session.new.assert_not_called()
        wrapper.close()
        wrapper.close()
        session.close.assert_called_once_with()

    def test_interrupt_of_disconnected_socket_still_disables_session(self):
        wrapper, session, _ = self.make()
        wrapper.start()
        session.socket.shutdown.side_effect = OSError("closed")
        wrapper.interrupt()
        self.assertTrue(wrapper.uncertain)
        wrapper.close()
        session.close.assert_called_once_with()

    def test_interrupt_shuts_down_real_local_socket_without_protocol_frame(self):
        wrapper, session, _ = self.make()
        connection, peer = socket.socketpair()
        try:
            session.socket = connection
            peer.settimeout(1)
            wrapper.start()
            wrapper.interrupt()
            self.assertEqual(peer.recv(1), b"")
            self.assertTrue(wrapper.uncertain)
            wrapper.close()
        finally:
            connection.close()
            peer.close()

    def test_interrupt_during_factory_prevents_start_and_closes_session(self):
        session = FakeSession()
        wrapper = None

        def factory(**kwargs):
            wrapper.interrupt()
            return session

        wrapper = backend.PhoneBackend(session_factory=factory)
        with self.assertRaisesRegex(RuntimeError, "interrupted during startup"):
            wrapper.start()
        self.assertTrue(wrapper.uncertain)
        session.start.assert_not_called()
        session.close.assert_called_once_with()

    def test_interrupt_during_start_closes_started_session(self):
        wrapper, session, _ = self.make()
        session.start.side_effect = wrapper.interrupt
        with self.assertRaisesRegex(RuntimeError, "interrupted during startup"):
            wrapper.start()
        self.assertTrue(wrapper.uncertain)
        session.close.assert_called_once_with()

    def test_backend_tracks_underlying_uncertain_state(self):
        wrapper, session, _ = self.make()
        wrapper.start()
        session.transport_uncertain = True
        self.assertTrue(wrapper.info()["uncertain"])
        with self.assertRaises(RuntimeError):
            wrapper.new()

    def test_close_error_is_never_converted_to_success(self):
        wrapper, session, _ = self.make()
        wrapper.start()
        failure = RuntimeError("native exit not confirmed")
        session.close.side_effect = failure
        for _ in range(2):
            with self.assertRaises(RuntimeError) as caught:
                wrapper.close()
            self.assertIs(caught.exception, failure)
        session.close.assert_called_once_with()
        self.assertEqual(wrapper.info()["state"], "close_failed")

    def test_unused_close_is_idempotent_and_blocks_start(self):
        wrapper, _, factory = self.make()
        wrapper.close()
        wrapper.close()
        with self.assertRaises(RuntimeError):
            wrapper.start()
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
