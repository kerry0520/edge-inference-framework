"""HTTP adapter for the existing admitted, full-history phone session.

The HTTP actor serializes start/ask/new/close. Only interrupt may run from a
different thread: it breaks the current transport, never replays a request.
"""
from copy import deepcopy
import socket
import threading

from phone_chat_plan import PlanCacheSession
from phone_chat_session import ContextBudgetExceeded


class PhoneBackend:
    def __init__(self, serial=None, profile_path=None, adb_path=None,
                 max_context_tokens=512, record_parent=None, session_factory=None,
                 model_profile=None):
        if model_profile is not None and (profile_path is not None or adb_path is not None):
            raise ValueError("A model profile uses the fixed native build, without plan/ADB overrides")
        if model_profile is not None and session_factory is None:
            from phone_model_session import LargerModelSession
            session_factory = LargerModelSession
        self._factory = PlanCacheSession if session_factory is None else session_factory
        self._arguments = dict(serial=serial, profile_path=profile_path,
                               adb_path=adb_path, max_context_tokens=max_context_tokens,
                               record_parent=record_parent)
        if model_profile is not None:
            self._arguments["model_profile"] = model_profile
        self._model_name = "Qwen3-0.6B" if model_profile is None else "Qwen3-1.7B"
        self._session = None
        self._state = "not_started"
        self._interrupted = False
        self._close_attempted = False
        self._close_error = None
        self._lock = threading.Lock()

    @property
    def uncertain(self):
        return self._interrupted or bool(
            self._session is not None and self._session.transport_uncertain)

    def start(self):
        with self._lock:
            if self._state != "not_started" or self._close_attempted:
                raise RuntimeError("Backend start is single-use")
            self._state = "starting"
        try:
            # The original constructor/start perform profile admission and
            # immutable build checks. Do not substitute a candidate on failure.
            self._session = self._factory(**self._arguments)
            if self._interrupted:
                self._session.transport_uncertain = True
                raise RuntimeError("Backend interrupted during startup")
            self._session.start()
            with self._lock:
                if self._interrupted:
                    raise RuntimeError("Backend interrupted during startup")
                self._state = "ready"
            return self
        except BaseException as startup_error:
            self._state = "start_failed"
            try:
                self.close()
            except BaseException as cleanup_error:
                # Retain startup as the raised error and cleanup as its cause.
                raise startup_error from cleanup_error
            raise

    def _ready(self):
        if self._state != "ready" or self._close_attempted or self.uncertain:
            raise RuntimeError("Backend unavailable; requests must not be replayed")
        return self._session

    def ask(self, prompt, max_new_tokens=64, on_text=None):
        return self._ready().ask(prompt, max_new_tokens=max_new_tokens, on_text=on_text)

    def new(self):
        return self._ready().new()

    def history(self):
        return deepcopy(self._session.history) if self._session is not None else []

    def info(self):
        result = {"state": self._state, "uncertain": self.uncertain,
                  "model": self._model_name, "history_mode": "full_recompute",
                  "max_context_tokens": self._arguments["max_context_tokens"]}
        if self._session is not None:
            pid = self._session.record.get("ready", {}).get("pid")
            if type(pid) is int and pid > 1:
                result["worker_pid"] = pid
        return result

    def interrupt(self):
        """Discard transport after exit/disconnect; preserve confirmed history."""
        with self._lock:
            if self._close_attempted:
                return
            self._interrupted = True
            self._state = "interrupted"
            session = self._session
            if session is not None:
                session.transport_uncertain = True
                connection = session.socket
            else:
                connection = None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                # Already closed/disconnected sockets still have no safe replay.
                pass

    def close(self):
        with self._lock:
            if self._close_attempted:
                if self._close_error is not None:
                    raise self._close_error
                return
            self._close_attempted = True
            self._state = "closing"
        try:
            if self._session is not None:
                self._session.close()
        except BaseException as error:
            self._close_error = error
            self._state = "close_failed"
            raise
        self._state = "closed"
