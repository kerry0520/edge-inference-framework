"""Loopback-only HTTP/SSE entry for one admitted resident phone worker."""
import argparse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import sys
import threading
import uuid

from phone_chat_session import ContextBudgetExceeded


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class HTTPService:
    def __init__(self, backend, record_dir):
        self.backend = backend
        self.record_dir = Path(record_dir)
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = uuid.uuid4().hex
        self.csrf_token = secrets.token_urlsafe(32)
        self.revision = 0
        self._history = backend.history()
        self._info = backend.info()
        self._state = "ready"
        self._active = None
        self._gate = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._recording_error = False

    def health(self):
        with self._state_lock:
            return deepcopy(dict(status=self._state, session_id=self.session_id,
                                 revision=self.revision, history=self._history,
                                 active_request_id=self._active, backend=self._info,
                                 recording_error=self._recording_error,
                                 csrf_token=self.csrf_token))

    def _save(self, record):
        target = self.record_dir / (record["request_id"] + ".json")
        temporary = target.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)
        except Exception:
            self._recording_error = True
            raise

    def run(self, body, reset=False, emit=None, deliver=None):
        if not self._gate.acquire(blocking=False):
            raise APIError(429, "busy", "A request is already running")
        record = None
        submitted = False
        committed = False
        initial_history = None
        try:
            with self._state_lock:
                if self._state != "ready":
                    raise APIError(503, "unavailable", "Start a new service; do not replay automatically")
                if body.get("session_id") != self.session_id or type(body.get("revision")) is not int or body["revision"] != self.revision:
                    raise APIError(409, "stale_state", "Read /health and review the confirmed conversation")
                request_id = uuid.uuid4().hex
                initial_history = deepcopy(self._history)
                record = dict(request_id=request_id, request=body, submitted=False,
                              committed=False, delivered=False)
                self._save(record)
                self._state, self._active = "busy", request_id
            if emit:
                emit("start", dict(request_id=request_id, session_id=self.session_id, revision=self.revision))
            submitted = True
            record["submitted"] = True
            self._save(record)
            if reset:
                self.backend.new()
                response, text = {}, ""
            else:
                result = self.backend.ask(body["prompt"], body.get("max_new_tokens", 64),
                                          on_text=(lambda text: emit("delta", {"text": text})) if emit else None)
                text = result["text"]
                response = {key: result[key] for key in ("status", "tokens") if key in result}
            history = self.backend.history()
            with self._state_lock:
                self.revision += 1
                self._history = history
                committed = True
                final = dict(request_id=request_id, session_id=self.session_id,
                             revision=self.revision, text=text, response=response)
            record.update(committed=True, response=final)
            self._save(record)
            if emit:
                emit("done", final)
            elif deliver:
                deliver(final)
            if emit or deliver:
                record["delivered"] = True
                try:
                    self._save(record)
                except Exception:
                    # The terminal response is already on the wire. Never append
                    # an error after done, or try to send a second JSON response.
                    self._recording_error = True
            return final, record
        except ContextBudgetExceeded:
            if record:
                record.update(outcome="budget_rejected")
                try:
                    self._save(record)
                except Exception:
                    self._recording_error = True
            raise APIError(422, "context_budget", "The conversation exceeds the context budget")
        except APIError:
            raise
        except Exception:
            if submitted:
                # Native commit precedes optional Python metrics/log persistence.
                # A later bookkeeping failure must not hide an already committed turn.
                try:
                    observed = self.backend.history()
                    if not committed and observed != initial_history:
                        with self._state_lock:
                            self._history = observed
                            self.revision += 1
                        committed = True
                        record["confirmed_history"] = observed
                except Exception as error:
                    record["history_read_error"] = repr(error)
                with self._state_lock:
                    if self._state != "closed":
                        self._state = "faulted"
                self.backend.interrupt()
                try:
                    self.backend.close()
                except Exception as error:
                    # Local evidence retains details; HTTP errors never expose paths.
                    record["cleanup_error"] = repr(error)
            if record:
                record.update(committed=committed, outcome="delivery_unconfirmed" if committed else "outcome_unknown" if submitted else "not_submitted")
                try:
                    self._save(record)
                except Exception:
                    # A failing record filesystem must not suppress the API failure.
                    pass
            raise APIError(503, "worker_unavailable", "Request could not be completed; do not replay automatically")
        finally:
            with self._state_lock:
                if self._state == "busy":
                    self._state = "ready"
                self._active = None
            self._gate.release()

    def close(self):
        if self._closed:
            return
        with self._state_lock:
            self._state = "closed"
        if self._gate.locked():
            self.backend.interrupt()
        if not self._gate.acquire(timeout=10):
            raise RuntimeError("HTTP request did not stop; cleanup not confirmed")
        try:
            self.backend.close()
            self._closed = True
        finally:
            self._gate.release()


def make_server(service, host="127.0.0.1", port=0):
    if host != "127.0.0.1":
        raise ValueError("Only 127.0.0.1 is supported")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def _json(self, status, value):
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self._security_headers()
            self.end_headers()
            self.close_connection = True
            self.wfile.write(raw)
            self.wfile.flush()

        def _security_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

        def _origin(self, mutation=False):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            sites = self.headers.get_all("Sec-Fetch-Site", [])
            if (len(hosts) != 1 or hosts[0] not in allowed or len(origins) > 1 or
                    len(sites) > 1 or (sites and sites[0] not in ("same-origin", "none")) or
                    (origins and origins[0] != "http://" + hosts[0])):
                raise APIError(403, "origin_rejected", "Only this local origin is supported")
            if mutation and origins:
                tokens = self.headers.get_all("X-Phone-CSRF", [])
                if (len(tokens) != 1 or not secrets.compare_digest(
                        tokens[0].encode("utf-8"), self.server.service.csrf_token.encode("ascii"))):
                    raise APIError(403, "csrf_rejected", "Refresh this page before submitting")

        def _asset(self):
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.path not in assets:
                raise APIError(404, "not_found", "Unknown endpoint")
            filename, content_type = assets[self.path]
            try:
                raw = (Path(__file__).resolve().parent / "phone_web" / filename).read_bytes()
            except OSError:
                raise APIError(503, "page_unavailable", "Chat page assets are unavailable")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self._security_headers()
            self.end_headers()
            self.close_connection = True
            self.wfile.write(raw)
            self.wfile.flush()

        def _error(self, error):
            self._json(error.status, {"error": {"code": error.code, "message": error.message}})

        def do_GET(self):
            try:
                self._origin()
                if self.path == "/health":
                    self._json(200, self.server.service.health())
                else:
                    self._asset()
            except APIError as error:
                self._error(error)
            except OSError:
                self.close_connection = True

        def do_POST(self):
            streaming = False
            started = False

            def emit(event, value):
                nonlocal started
                if not started:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Connection", "close")
                    self._security_headers()
                    self.end_headers()
                    self.close_connection = True
                    started = True
                raw = f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(raw)
                self.wfile.flush()

            try:
                if self.path not in ("/v1/chat", "/v1/new"):
                    raise APIError(404, "not_found", "Unknown endpoint")
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
                    raise APIError(400, "invalid_body", "A single Content-Length is required")
                length = int(lengths[0])
                if length > 65536:
                    raise APIError(413, "body_too_large", "JSON body exceeds 64 KiB")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise APIError(400, "invalid_body", "Incomplete JSON body")
                self._origin(mutation=True)
                if self.headers.get_content_type() != "application/json":
                    raise APIError(415, "content_type", "Content-Type must be application/json")
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    raise APIError(400, "invalid_body", "Invalid UTF-8 JSON")
                if not isinstance(body, dict):
                    raise APIError(400, "invalid_body", "JSON object required")
                if not isinstance(body.get("session_id"), str) or type(body.get("revision")) is not int:
                    raise APIError(400, "invalid_state", "A session_id and integer revision are required")
                reset = self.path == "/v1/new"
                if not reset:
                    prompt, tokens = body.get("prompt"), body.get("max_new_tokens", 64)
                    if not isinstance(prompt, str) or not prompt.strip() or "\0" in prompt or type(tokens) is not int or not 1 <= tokens <= 512 or type(body.get("stream", False)) is not bool:
                        raise APIError(400, "invalid_request", "Provide a nonempty prompt, 1..512 tokens and a boolean stream")
                    try:
                        prompt.encode("utf-8", errors="strict")
                    except UnicodeError:
                        raise APIError(400, "invalid_request", "Prompt must contain valid Unicode")
                    streaming = body.get("stream", False)
                self.server.service.run(body, reset, emit if streaming else None,
                            None if streaming else lambda value: self._json(200, value))
            except APIError as error:
                try:
                    if started:
                        emit("error", {"error": {"code": error.code, "message": error.message}})
                    else:
                        self._error(error)
                except OSError:
                    self.close_connection = True
            except OSError:
                self.close_connection = True

    class LoopbackHTTPServer(ThreadingHTTPServer):
        def server_bind(self):
            if sys.platform == "win32":
                # Windows SO_REUSEADDR permits another listener on this port.
                self.allow_reuse_address = False
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            return super().server_bind()

    server = LoopbackHTTPServer((host, port), Handler)
    server.service = service
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Registered deployment key; use --list-models")
    parser.add_argument("--list-models", action="store_true", help="List known deployments without connecting")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--serial")
    parser.add_argument("--plan-profile")
    parser.add_argument("--model-profile", help="Explicit separately installed model profile; default stays 0.6B")
    parser.add_argument("--adb-path")
    parser.add_argument("--max-context-tokens", type=int, default=512)
    parser.add_argument("--record-dir", type=Path)
    args = parser.parse_args()
    if args.list_models:
        from phone_model_catalog import list_models
        print(json.dumps(list_models(), ensure_ascii=False, indent=2))
        return 0
    if args.model is not None:
        from phone_model_catalog import selection
        try:
            selection(args.model)
            if args.model_profile is not None:
                raise ValueError("--model and --model-profile are mutually exclusive")
        except ValueError as error:
            parser.error(str(error))
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0..65535 (0 selects an available port)")
    from phone_http_backend import PhoneBackend
    directory = args.record_dir or Path("results/android/http-service-20261020/sessions") / uuid.uuid4().hex
    backend = service = server = None
    try:
        # Keep the actual listener reserved throughout backend startup.
        server = make_server(None, port=args.port)
        model_arguments = {"model": args.model} if args.model is not None else {}
        backend = PhoneBackend(serial=args.serial, profile_path=args.plan_profile,
                               adb_path=args.adb_path, max_context_tokens=args.max_context_tokens,
                               record_parent=directory / "native", model_profile=args.model_profile, **model_arguments)
        backend.start()
        service = HTTPService(backend, directory / "http")
        server.service = service
        print(f"Phone HTTP ready: http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if server:
                server.server_close()
        finally:
            if service:
                service.close()
            elif backend:
                backend.close()


if __name__ == "__main__":
    main()
