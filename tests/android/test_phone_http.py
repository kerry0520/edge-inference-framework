"""Real loopback HTTP/SSE protocol tests; no ADB, model or phone invocation."""
import http.client
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_http as entry
from phone_chat_session import ContextBudgetExceeded


class FakeBackend:
    """Same transaction ordering as the backend: callbacks precede history commit."""
    def __init__(self):
        self._history = []
        self.uncertain = False
        self.closed = False
        self.calls = []
        self.mode = "normal"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.final_ready = threading.Event()
        self.finish = threading.Event()

    def history(self):
        return [dict(message) for message in self._history]

    def info(self):
        return {"backend": "offline-test", "history_mode": "full_recompute"}

    def ask(self, prompt, max_new_tokens=64, on_text=None):
        self.calls.append((prompt, max_new_tokens))
        self.entered.set()
        if self.mode == "budget":
            raise ContextBudgetExceeded("offline context budget exceeded")
        if self.mode == "error":
            self.uncertain = True
            raise RuntimeError("offline worker transport lost")
        if self.mode in ("blocked", "disconnect"):
            if not self.release.wait(5):
                raise RuntimeError("test backend barrier timed out")
            if self.uncertain:
                raise RuntimeError("test backend interrupted")
        try:
            parts = ["你", "好", "，世界"]
            if self.mode == "disconnect":
                parts = ["断" * 65536] * 16
            for part in parts:
                if on_text:
                    on_text(part)
        except Exception:
            self.uncertain = True
            raise
        text = "".join(parts)
        self._history += [{"role": "user", "content": prompt},
                         {"role": "assistant", "content": text}]
        if self.mode == "final_disconnect":
            self.final_ready.set()
            if not self.finish.wait(5):
                raise RuntimeError("test final barrier timed out")
        if self.mode == "postcommit_error":
            raise OSError("local backend record persist failed after history commit")
        return {"text": text, "status": "normal_finished", "tokens": [1, 2, 3]}

    def new(self):
        self.calls.append(("new",))
        self._history = []

    def close(self):
        self.closed = True

    def interrupt(self):
        self.uncertain = True
        self.release.set()


class HTTPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phone-http-tests-")
        self.backend = FakeBackend()
        self.service = entry.HTTPService(self.backend, Path(self.temp.name))
        self.server = entry.make_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.backend.release.set()
        self.backend.finish.set()
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.thread.join(5)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        encoded = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request_headers = {"Content-Type": "application/json"} if body is not None else {}
        request_headers.update(headers or {})
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        status, content_type = response.status, response.getheader("Content-Type", "")
        connection.close()
        value = json.loads(raw) if "application/json" in content_type else raw.decode()
        return status, value

    def health(self):
        status, data = self.request("GET", "/health")
        self.assertEqual(status, 200)
        return data

    def chat_body(self, **changes):
        state = self.health()
        body = {"session_id": state["session_id"], "revision": state["revision"],
                "prompt": "你好", "max_new_tokens": 64, "stream": False}
        body.update(changes)
        return body

    def wait_idle(self, status="ready"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.health()
            if state["status"] == status and state.get("active_request_id") is None:
                return state
            time.sleep(.01)
        self.fail(f"service did not become idle {status}: {state}")

    def test_json_answer_history_and_reset_revision_prevent_aba(self):
        original = self.chat_body()
        status, answer = self.request("POST", "/v1/chat", original)
        self.assertEqual(status, 200)
        self.assertEqual(answer["text"], "你好，世界")
        self.assertEqual(answer["revision"], original["revision"] + 1)
        self.assertEqual(len(self.health()["history"]), 2)
        self.wait_idle()
        reset = {key: answer[key] for key in ("session_id", "revision")}
        status, cleared = self.request("POST", "/v1/new", reset)
        self.assertEqual(status, 200)
        self.assertEqual(cleared["revision"], answer["revision"] + 1)
        self.wait_idle()
        self.assertEqual(self.health()["history"], [])
        self.assertEqual(self.request("POST", "/v1/chat", original)[0], 409)
        self.assertEqual(len(self.backend.calls), 2)

    def test_sse_preserves_chinese_and_terminal_metadata(self):
        status, stream = self.request("POST", "/v1/chat", self.chat_body(stream=True))
        self.assertEqual(status, 200)
        events = []
        for block in stream.strip().split("\n\n"):
            fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
            if "event" in fields:
                events.append((fields["event"].strip(), json.loads(fields["data"].strip())))
        self.assertEqual([event[0] for event in events], ["start", "delta", "delta", "delta", "done"])
        self.assertEqual("".join(data["text"] for event, data in events if event == "delta"), "你好，世界")
        self.assertEqual(events[-1][1]["text"], "你好，世界")
        self.assertEqual(events[0][1]["request_id"], events[-1][1]["request_id"])
        self.assertEqual(events[-1][1]["revision"], events[0][1]["revision"] + 1)

    def test_busy_rejects_chat_and_reset_without_queueing(self):
        self.backend.mode = "blocked"
        body = self.chat_body()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.request("POST", "/v1/chat", body)))
        worker.start()
        self.assertTrue(self.backend.entered.wait(2))
        try:
            state = self.health()
            self.assertEqual(state["status"], "busy")
            self.assertTrue(state["active_request_id"])
            self.assertEqual(self.request("POST", "/v1/chat", body)[0], 429)
            self.assertEqual(self.request("POST", "/v1/new", body)[0], 429)
            self.assertEqual(len(self.backend.calls), 1)
        finally:
            self.backend.release.set()
            worker.join(5)
        self.assertEqual(result[0][0], 200)

    def test_wrong_identity_and_stale_revision_never_reach_backend(self):
        body = self.chat_body()
        for changes in ({"session_id": "another-session"}, {"revision": body["revision"] + 1}):
            self.assertEqual(self.request("POST", "/v1/chat", dict(body, **changes))[0], 409)
        self.assertEqual(self.backend.calls, [])

    def test_invalid_types_and_bounds_never_reach_backend(self):
        body = self.chat_body()
        for changes in ({"prompt": ""}, {"prompt": "a\0b"}, {"prompt": 4},
                        {"max_new_tokens": True}, {"max_new_tokens": 0},
                        {"max_new_tokens": 513}, {"stream": "true"}, {"revision": True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request("POST", "/v1/chat", dict(body, **changes))[0], 400)
        self.assertEqual(self.backend.calls, [])

    def test_budget_rejection_preserves_history_revision_and_worker(self):
        self.backend.mode = "budget"
        before = self.health()
        status, error = self.request("POST", "/v1/chat", self.chat_body())
        self.assertEqual(status, 422)
        self.assertIn("error", error)
        after = self.health()
        self.assertEqual((after["history"], after["revision"]), (before["history"], before["revision"]))
        self.assertEqual(after["status"], "ready")
        self.assertFalse(self.backend.closed)

    def test_stream_budget_rejection_does_not_emit_done_or_commit(self):
        self.backend.mode = "budget"
        before = self.health()
        status, result = self.request("POST", "/v1/chat", self.chat_body(stream=True))
        if status == 200:
            self.assertIn("event: error", result)
            self.assertNotIn("event: done", result)
        else:
            self.assertEqual(status, 422)
        after = self.health()
        self.assertEqual((after["history"], after["revision"]), (before["history"], before["revision"]))
        self.assertEqual(after["status"], "ready")

    def test_stream_worker_error_emits_error_and_faults(self):
        self.backend.mode = "error"
        status, result = self.request("POST", "/v1/chat", self.chat_body(stream=True))
        if status == 200:
            self.assertIn("event: error", result)
            self.assertNotIn("event: done", result)
        else:
            self.assertEqual(status, 503)
        self.assertEqual(self.health()["status"], "faulted")
        self.assertTrue(self.backend.closed)

    def test_worker_error_faults_service_and_cannot_be_retried(self):
        self.backend.mode = "error"
        body = self.chat_body()
        self.assertEqual(self.request("POST", "/v1/chat", body)[0], 503)
        self.assertEqual(self.health()["status"], "faulted")
        self.assertEqual(self.request("POST", "/v1/chat", body)[0], 503)
        self.assertEqual(self.request("POST", "/v1/new", body)[0], 503)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertTrue(self.backend.closed)

    def test_backend_postcommit_error_preserves_confirmed_history(self):
        self.backend.mode = "postcommit_error"
        before = self.health()
        self.assertEqual(self.request("POST", "/v1/chat", self.chat_body())[0], 503)
        state = self.wait_idle("faulted")
        self.assertEqual(state["revision"], before["revision"] + 1)
        self.assertEqual(len(state["history"]), 2)
        self.assertTrue(self.backend.closed)

    def test_initial_record_write_failure_never_submits_native_request(self):
        body = self.chat_body()
        with patch.object(self.service, "_save", side_effect=OSError("offline disk full")):
            self.assertEqual(self.request("POST", "/v1/chat", body)[0], 503)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.health()["revision"], body["revision"])
        self.assertEqual(self.health()["history"], [])

    def test_postcommit_record_write_failure_preserves_confirmed_history(self):
        original_save = self.service._save
        def save(record):
            if record.get("committed"):
                raise OSError("offline disk full after commit")
            original_save(record)
        body = self.chat_body()
        with patch.object(self.service, "_save", side_effect=save):
            self.assertEqual(self.request("POST", "/v1/chat", body)[0], 503)
        state = self.wait_idle("faulted")
        self.assertEqual(state["revision"], body["revision"] + 1)
        self.assertEqual(len(state["history"]), 2)
        self.assertTrue(self.backend.closed)

    def test_budget_rejection_record_failure_still_returns_known_rejection(self):
        self.backend.mode = "budget"
        original_save = self.service._save
        def save(record):
            if record.get("outcome") == "budget_rejected":
                raise OSError("offline rejection record disk full")
            original_save(record)
        body = self.chat_body()
        with patch.object(self.service, "_save", side_effect=save):
            self.assertEqual(self.request("POST", "/v1/chat", body)[0], 422)
        state = self.wait_idle()
        self.assertEqual(state["revision"], body["revision"])
        self.assertEqual(state["history"], [])
        self.assertFalse(self.backend.closed)

    def test_delivered_sse_record_failure_does_not_emit_second_terminal_event(self):
        original_save = self.service._save
        def save(record):
            if record.get("delivered"):
                raise OSError("offline final delivered record disk full")
            original_save(record)
        with patch.object(self.service, "_save", side_effect=save):
            status, stream = self.request("POST", "/v1/chat", self.chat_body(stream=True))
        self.assertEqual(status, 200)
        self.assertEqual(stream.count("event: done\n"), 1)
        self.assertNotIn("event: error\n", stream)
        self.assertEqual(len(self.health()["history"]), 2)

    def test_origin_host_and_content_type_rejected_before_backend(self):
        body = self.chat_body()
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example"},
                        {"Content-Type": "text/plain"}):
            with self.subTest(headers=headers):
                self.assertIn(self.request("POST", "/v1/chat", body, headers)[0], (400, 403, 415))
        self.assertEqual(self.backend.calls, [])

    def test_oversize_body_rejected_without_submission(self):
        self.assertIn(self.request("POST", "/v1/chat", self.chat_body(prompt="x" * 65536))[0], (400, 413))
        self.assertEqual(self.backend.calls, [])

    def test_nonloopback_bind_is_rejected(self):
        with self.assertRaises((ValueError, RuntimeError)):
            entry.make_server(self.service, host="0.0.0.0", port=0)

    def test_malformed_json_and_nonobject_input_are_rejected(self):
        for payload in (b"{", b"[]", b"null", b"\xff"):
            with self.subTest(payload=payload):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
                connection.request("POST", "/v1/chat", payload, {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                connection.close()
        self.assertEqual(self.backend.calls, [])

    def test_ambiguous_http_body_framing_is_rejected(self):
        for framing in ("Content-Length: 2\r\nContent-Length: 2\r\n",
                        "Transfer-Encoding: chunked\r\n",
                        "Content-Length: -1\r\n", ""):
            with self.subTest(framing=framing):
                with socket.create_connection(("127.0.0.1", self.port), timeout=3) as connection:
                    wire = (f"POST /v1/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                            f"Content-Type: application/json\r\n{framing}\r\n{{}}").encode()
                    connection.sendall(wire)
                    response = connection.recv(4096)
                    self.assertIn(int(response.split(b" ", 2)[1]), (400, 411, 413, 501))
        self.assertEqual(self.backend.calls, [])

    def test_request_and_response_are_persisted(self):
        body = self.chat_body()
        status, answer = self.request("POST", "/v1/chat", body)
        self.assertEqual(status, 200)
        documents = [path.read_text(encoding="utf-8") for path in Path(self.temp.name).rglob("*.json")]
        combined = "\n".join(documents)
        self.assertTrue(documents)
        self.assertIn(answer["request_id"], combined)
        self.assertIn("你好", combined)
        self.assertIn("response", combined)

    def test_disconnect_fails_closed_without_history_commit(self):
        self.backend.mode = "disconnect"
        before = self.health()
        body = json.dumps(self.chat_body(stream=True)).encode()
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        wire = (f"POST /v1/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
        connection.sendall(wire)
        self.assertTrue(self.backend.entered.wait(2))
        # Read start headers before an abortive close. RST makes the first delta fail.
        connection.recv(8192)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0) if sys.platform == "win32" else struct.pack("ii", 1, 0))
        connection.close()
        self.backend.release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.health()
            if state["status"] == "faulted" and state.get("active_request_id") is None:
                break
            time.sleep(.01)
        self.assertEqual(state["status"], "faulted")
        self.assertEqual(state["revision"], before["revision"])
        self.assertEqual(state["history"], before["history"])
        self.assertTrue(self.backend.uncertain)
        self.assertTrue(self.backend.closed)

    def test_final_confirmed_before_disconnect_preserves_committed_history(self):
        self.backend.mode = "final_disconnect"
        before = self.health()
        body = json.dumps(self.chat_body(stream=True)).encode()
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        wire = (f"POST /v1/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body
        connection.sendall(wire)
        self.assertTrue(self.backend.final_ready.wait(2))
        connection.recv(8192)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0) if sys.platform == "win32" else struct.pack("ii", 1, 0))
        connection.close()
        self.backend.finish.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.health()
            if state["status"] == "faulted" and state.get("active_request_id") is None:
                break
            time.sleep(.01)
        self.assertEqual(state["status"], "faulted")
        self.assertEqual(state["revision"], before["revision"] + 1)
        self.assertEqual(len(state["history"]), 2)
        records = [json.loads(p.read_text(encoding="utf-8")) for p in Path(self.temp.name).glob("*.json")]
        self.assertTrue(any(record.get("committed") is True and record.get("outcome") == "delivery_unconfirmed" for record in records))

    def test_close_during_busy_does_not_resurrect_service(self):
        self.backend.mode = "blocked"
        body = self.chat_body()
        result = []
        worker = threading.Thread(target=lambda: result.append(self.request("POST", "/v1/chat", body)))
        worker.start()
        self.assertTrue(self.backend.entered.wait(2))
        self.service.close()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.health()["status"], "closed")
        self.assertEqual(self.request("POST", "/v1/chat", body)[0], 503)
        self.assertEqual(self.health()["status"], "closed")
        self.assertEqual(len(self.backend.calls), 1)


if __name__ == "__main__":
    unittest.main()
