"""Real loopback listener lifecycle with mocked backend; no phone or model."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_http as entry


class HTTPStartupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="http-startup-tests-"))
        self.backend = Mock()
        self.backend.history.return_value = []
        self.backend.info.return_value = {}
        self.factory = self.stack.enter_context(patch("phone_http_backend.PhoneBackend", return_value=self.backend))
        self.run = self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("ADB forbidden")))
        self.popen = self.stack.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("model forbidden")))
        self.real_make_server = entry.make_server
        self.servers = []

        def capture(*args, **kwargs):
            server = self.real_make_server(*args, **kwargs)
            self.servers.append(server)
            return server

        self.make = self.stack.enter_context(patch.object(entry, "make_server", side_effect=capture))
        self.serve = self.stack.enter_context(patch.object(
            entry.ThreadingHTTPServer, "serve_forever", autospec=True, side_effect=self.interrupt_ready))

    def tearDown(self):
        self.run.assert_not_called()
        self.popen.assert_not_called()
        # Also release listeners if an assertion fails before main cleanup.
        for server in self.servers:
            server.socket.close()

    def invoke(self, *arguments):
        with patch.object(sys, "argv", ["phone_http.py", "--port", "0", "--record-dir", self.directory, *arguments]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return entry.main()

    def interrupt_ready(self, server):
        self.assertIsInstance(server.service, entry.HTTPService)
        self.assertIs(server.service.backend, self.backend)
        self.assertIs(server, self.servers[0])
        raise KeyboardInterrupt

    def assert_released(self, port):
        with socket.socket() as probe:
            if sys.platform == "win32":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))

    def test_existing_http_listener_rejects_before_backend_construction(self):
        occupied = self.real_make_server(None, port=0)
        try:
            with self.assertRaises(OSError):
                self.invoke("--port", str(occupied.server_port))
            self.factory.assert_not_called()
            self.backend.start.assert_not_called()
            self.serve.assert_not_called()
        finally:
            occupied.server_close()

    def test_exclusive_occupied_port_rejects_before_backend_construction(self):
        with socket.socket() as occupied:
            if sys.platform == "win32":
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            with self.assertRaises(OSError):
                self.invoke("--port", str(occupied.getsockname()[1]))
            self.factory.assert_not_called()
            self.backend.start.assert_not_called()
            self.serve.assert_not_called()

    def test_same_ephemeral_listener_is_held_until_service_ready(self):
        observed = []

        def start():
            server = self.servers[0]
            observed.append(server.socket)
            self.assertIsNone(server.service)
            self.assertGreater(server.server_port, 0)
            self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)
            self.serve.assert_not_called()
            with socket.socket() as conflicting:
                with self.assertRaises(OSError):
                    conflicting.bind(("127.0.0.1", server.server_port))
            if sys.platform == "win32":
                self.assertFalse(server.allow_reuse_address)
                self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)
                self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1)
            else:
                self.assertEqual(server.allow_reuse_address, entry.ThreadingHTTPServer.allow_reuse_address)

        self.backend.start.side_effect = start
        self.invoke()
        self.assertEqual(self.make.call_count, 1)
        self.assertIs(observed[0], self.servers[0].socket)
        self.serve.assert_called_once_with(self.servers[0])
        self.backend.close.assert_called_once_with()
        self.assertEqual(observed[0].fileno(), -1)
        self.assert_released(self.servers[0].server_port)

    def test_backend_constructor_failure_releases_listener_without_serving(self):
        self.factory.side_effect = ValueError("bad backend configuration")
        with self.assertRaisesRegex(ValueError, "bad backend configuration"):
            self.invoke()
        self.serve.assert_not_called()
        self.backend.start.assert_not_called()
        self.backend.close.assert_not_called()
        self.assert_released(self.servers[0].server_port)

    def test_backend_start_failure_closes_backend_and_releases_listener(self):
        self.backend.start.side_effect = RuntimeError("startup failed")
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            self.invoke()
        self.backend.close.assert_called_once_with()
        self.serve.assert_not_called()
        self.assert_released(self.servers[0].server_port)

    def test_service_constructor_failure_closes_backend_and_releases_listener(self):
        with patch.object(entry, "HTTPService", side_effect=OSError("record directory unavailable")):
            with self.assertRaisesRegex(OSError, "record directory unavailable"):
                self.invoke()
        self.backend.start.assert_called_once_with()
        self.backend.close.assert_called_once_with()
        self.serve.assert_not_called()
        self.assert_released(self.servers[0].server_port)

    def test_interrupt_during_backend_start_cleans_up_without_serving(self):
        self.backend.start.side_effect = KeyboardInterrupt
        self.invoke()
        self.backend.close.assert_called_once_with()
        self.serve.assert_not_called()
        self.assert_released(self.servers[0].server_port)

    def test_server_close_error_still_closes_ready_backend(self):
        def start():
            server = self.servers[0]
            real_close = server.server_close

            def close():
                real_close()
                raise OSError("listener cleanup error")

            server.server_close = close

        self.backend.start.side_effect = start
        with self.assertRaisesRegex(OSError, "listener cleanup error"):
            self.invoke()
        self.backend.close.assert_called_once_with()
        self.assert_released(self.servers[0].server_port)

    def test_invalid_cli_ports_reject_before_listener_or_backend(self):
        for value in ("-1", "65536", "not-a-port"):
            with self.subTest(port=value), self.assertRaises(SystemExit) as failure:
                self.invoke("--port", value)
            self.assertEqual(failure.exception.code, 2)
        self.make.assert_not_called()
        self.factory.assert_not_called()
        self.backend.start.assert_not_called()
        self.serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
