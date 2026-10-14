"""Offline framed-byte stream validation; no ADB or model calls."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/android"))
import phone_stream_session as entry


class FakeConnection:
    def __init__(self, messages):
        self.messages = messages
        self.buffer = bytearray()
        self.closed = False

    def sendall(self, frame):
        request = json.loads(frame[4:].decode())
        for message in self.messages(request):
            data = json.dumps(message, ensure_ascii=False).encode()
            self.buffer.extend(struct.pack("!I", len(data)) + data)

    def recv(self, length):
        length = min(length, 2)  # Every frame header/body has short reads.
        data = bytes(self.buffer[:length])
        del self.buffer[:length]
        return data

    def close(self):
        self.closed = True


def messages(request, text="中文A"):
    identity = {"id": request["id"], "pid": 123, "load_count": 1}
    raw = text.encode()
    parts = [raw[:1], raw[1:2], raw[2:4], raw[4:]]
    result = [{**identity, "event": "chunk", "seq": i, "bytes_hex": part.hex()}
              for i, part in enumerate(parts) if part]
    result.append({**identity, "event": "final", "chunk_count": len(result), "text": text,
                   "tokens": [1, 2], "ok": True, "status": "normal_finished", "status_code": 1,
                   "stream_bytes_match": True, "stream_transport_ok": True,
                   "reset_history": 0, "reset_output": 0, "reset_all_seq": 0, "reset_gen_seq": 0})
    return result


class StreamTests(unittest.TestCase):
    def run_case(self, factory, should_fail=False):
        with tempfile.TemporaryDirectory(prefix="stream-tests-") as folder:
            session = entry.Session("offline", Path(folder))
            session.record["ready"] = {"pid": 123}
            connection = FakeConnection(factory)
            session.socket = connection
            displayed = []
            if should_fail:
                with self.assertRaises((RuntimeError, UnicodeError)):
                    session.ask("test", on_text=displayed.append)
                self.assertTrue(connection.closed)
                self.assertIsNone(session.socket)
                self.assertTrue(session.transport_uncertain)
                self.assertEqual(session.record["status"], "request_transport_unconfirmed")
                self.assertTrue(session.record["pending_request"]["frames"])
            else:
                final = session.ask("test", on_text=displayed.append)
                self.assertEqual("".join(displayed), final["text"])
                self.assertNotIn("pending_request", session.record)
                self.assertEqual(session.record["status"], "ready")
            return displayed

    def test_utf8_across_chunk_boundaries_and_short_reads(self):
        self.assertEqual("".join(self.run_case(messages)), "中文A")

    def test_ascii_and_max_tokens_finished(self):
        def factory(request):
            frames = messages(request, "abcde")
            frames[-1].update(status="max_tokens_finished", status_code=2)
            return frames
        self.assertEqual("".join(self.run_case(factory)), "abcde")

    def test_identity_sequence_hex_and_final_mismatch_rejected(self):
        changes = [(0, "pid", 456), (0, "load_count", True), (0, "id", "other"),
                   (0, "seq", 1), (0, "bytes_hex", "a b"), (-1, "text", "different"),
                   (-1, "chunk_count", 20), (-1, "status_code", 2),
                   (-1, "reset_history", 1), (-1, "stream_transport_ok", False)]
        for index, key, value in changes:
            with self.subTest(field=key):
                def factory(request):
                    frames = messages(request)
                    frames[index][key] = value
                    return frames
                self.run_case(factory, should_fail=True)

    def test_incomplete_and_invalid_utf8_rejected(self):
        for encoded in ("e4b8", "ff"):
            with self.subTest(encoded=encoded):
                def factory(request):
                    frames = messages(request)
                    frames[0]["bytes_hex"] = encoded
                    return [frames[0], frames[-1]]
                self.run_case(factory, should_fail=True)

    def test_partial_output_does_not_imply_success(self):
        def factory(request):
            frames = messages(request, "hello")
            frames[-1].update(ok=False, status="internal_error", status_code=4)
            return frames
        self.assertEqual("".join(self.run_case(factory, should_fail=True)), "hello")

    def test_eof_without_final_preserves_partial(self):
        self.run_case(lambda request: messages(request)[:-1], should_fail=True)


if __name__ == "__main__":
    unittest.main()
