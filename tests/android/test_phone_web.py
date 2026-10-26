"""Loopback socket regression tests for the browser entry and CSRF boundary."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import phone_http as entry
from test_phone_http import FakeBackend


class WebProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phone-web-tests-")
        self.backend = FakeBackend()
        self.service = entry.HTTPService(self.backend, Path(self.temp.name))
        self.server = entry.make_server(self.service, port=0)
        self.port = self.server.server_port
        self.host = f"127.0.0.1:{self.port}"
        self.origin = "http://" + self.host
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.thread.join(5)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=()):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        items = list(headers.items()) if isinstance(headers, dict) else list(headers)
        connection.putrequest(method, path, skip_host=True)
        if not any(key.lower() == "host" for key, value in items):
            connection.putheader("Host", self.host)
        for key, value in items:
            connection.putheader(key, value)
        if raw is not None:
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(raw)))
        connection.endheaders(raw)
        response = connection.getresponse()
        status, response_headers, data = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, data

    def health(self):
        status, headers, data = self.request("GET", "/health")
        self.assertEqual(status, 200)
        return json.loads(data)

    def body(self):
        state = self.health()
        return {"session_id": state["session_id"], "revision": state["revision"],
                "prompt": "你好", "max_new_tokens": 32, "stream": False}

    def wait_ready(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = self.health()
            if state["status"] == "ready" and state.get("active_request_id") is None:
                return
            time.sleep(.01)
        self.fail("service remained busy")

    def assert_security_headers(self, headers):
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_static_allowlist_content_types_and_csp(self):
        for path, mime in (("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css")):
            with self.subTest(path=path):
                status, headers, data = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(data)
                self.assertIn(mime, headers.get("Content-Type", ""))
                self.assert_security_headers(headers)
                csp = headers.get("Content-Security-Policy", "")
                for rule in ("default-src 'self'", "connect-src 'self'", "script-src 'self'",
                             "style-src 'self'", "frame-ancestors 'none'", "base-uri 'none'"):
                    self.assertIn(rule, csp)

    def test_static_paths_do_not_expose_repository_or_traversal(self):
        for path in ("/phone_http.py", "/../phone_http.py", "/%2e%2e/phone_http.py",
                     "/docs/", "/app.js/", "/index.html"):
            with self.subTest(path=path):
                self.assertIn(self.request("GET", path)[0], (400, 404))

    def test_health_has_per_service_csrf_and_is_not_cacheable(self):
        status, headers, data = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assert_security_headers(headers)
        state = json.loads(data)
        self.assertIsInstance(state["csrf_token"], str)
        self.assertGreaterEqual(len(state["csrf_token"]), 32)
        another = entry.HTTPService(FakeBackend(), Path(self.temp.name) / "another")
        try:
            self.assertNotEqual(state["csrf_token"], another.health()["csrf_token"])
        finally:
            another.close()

    def test_same_origin_with_token_chat_and_new_succeed(self):
        token = self.health()["csrf_token"]
        headers = {"Origin": self.origin, "X-Phone-CSRF": token, "Sec-Fetch-Site": "same-origin"}
        status, response_headers, data = self.request("POST", "/v1/chat", self.body(), headers)
        self.assertEqual(status, 200)
        answer = json.loads(data)
        self.assertEqual(answer["text"], "你好，世界")
        self.wait_ready()
        reset = {key: answer[key] for key in ("session_id", "revision")}
        self.assertEqual(self.request("POST", "/v1/new", reset, headers)[0], 200)
        self.assertEqual(self.health()["history"], [])

    def test_origin_requires_correct_csrf_for_chat_and_new(self):
        body = self.body()
        for path in ("/v1/chat", "/v1/new"):
            for token in (None, "wrong-token", ""):
                headers = {"Origin": self.origin}
                if token is not None:
                    headers["X-Phone-CSRF"] = token
                with self.subTest(path=path, token=token):
                    self.assertEqual(self.request("POST", path, body, headers)[0], 403)
        self.assertEqual(self.backend.calls, [])

    def test_authenticated_browser_sse_keeps_chinese_and_one_final_event(self):
        body = self.body()
        body["stream"] = True
        headers = {"Origin": self.origin, "X-Phone-CSRF": self.health()["csrf_token"],
                   "Sec-Fetch-Site": "same-origin"}
        status, response_headers, raw = self.request("POST", "/v1/chat", body, headers)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", response_headers.get("Content-Type", ""))
        stream = raw.decode("utf-8")
        self.assertEqual(stream.count("event: done\n"), 1)
        self.assertNotIn("event: error\n", stream)
        self.assertIn("世界", stream)

    def test_foreign_null_wrong_scheme_and_wrong_port_origins_are_rejected(self):
        token = self.health()["csrf_token"]
        origins = ("https://evil.example", "null", self.origin + "/", "https://" + self.host,
                   f"http://127.0.0.1:{self.port + 1}", f"http://localhost:{self.port}")
        for origin in origins:
            headers = {"Origin": origin, "X-Phone-CSRF": token}
            with self.subTest(origin=origin):
                self.assertEqual(self.request("POST", "/v1/chat", self.body(), headers)[0], 403)
                self.assertEqual(self.request("GET", "/health", headers=headers)[0], 403)
                self.assertEqual(self.request("GET", "/", headers=headers)[0], 403)
        self.assertEqual(self.backend.calls, [])

    def test_duplicate_origin_and_csrf_headers_are_rejected(self):
        token = self.health()["csrf_token"]
        cases = ([('Origin', self.origin), ('Origin', self.origin), ('X-Phone-CSRF', token)],
                 [('Origin', self.origin), ('X-Phone-CSRF', token), ('X-Phone-CSRF', token)])
        body = self.body()
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/v1/chat", body, headers)[0], 403)
        self.assertEqual(self.backend.calls, [])

    def test_cross_site_fetch_rejected_even_without_origin(self):
        body = self.body()
        headers = {"Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.request("POST", "/v1/chat", body, headers)[0], 403)
        for path in ("/health", "/", "/app.js", "/style.css"):
            self.assertEqual(self.request("GET", path, headers=headers)[0], 403)
        self.assertEqual(self.backend.calls, [])

    def test_same_site_and_duplicate_fetch_metadata_are_rejected(self):
        cases = ({"Sec-Fetch-Site": "same-site"},
                 [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")])
        body = self.body()
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/v1/chat", body, headers)[0], 403)
                self.assertEqual(self.request("GET", "/health", headers=headers)[0], 403)
        self.assertEqual(self.backend.calls, [])

    def test_same_origin_get_needs_no_mutation_token(self):
        headers = {"Origin": self.origin, "Sec-Fetch-Site": "same-origin"}
        for path in ("/health", "/", "/app.js", "/style.css"):
            self.assertEqual(self.request("GET", path, headers=headers)[0], 200)

    def test_same_origin_fetch_and_host_localhost_pair_are_supported(self):
        localhost = f"localhost:{self.port}"
        headers = {"Host": localhost, "Origin": "http://" + localhost,
                   "Sec-Fetch-Site": "same-origin", "X-Phone-CSRF": self.health()["csrf_token"]}
        self.assertEqual(self.request("POST", "/v1/chat", self.body(), headers)[0], 200)

    def test_original_nonbrowser_script_without_origin_still_works(self):
        self.assertEqual(self.request("POST", "/v1/chat", self.body())[0], 200)


if __name__ == "__main__":
    unittest.main()
