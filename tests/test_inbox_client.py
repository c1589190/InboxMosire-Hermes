"""Client + SSE parsing tests against a throwaway loopback HTTP server."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _loader  # noqa: E402

client_mod = _loader.inbox_lib("client")
sse_mod = _loader.inbox_lib("sse")

TOKEN = "test-token-value"


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-inbox"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def _unauthenticated(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            body = json.dumps({"code": "UNAUTHORIZED", "message": "bad token"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        return False

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self._unauthenticated():
            return
        if self.path.endswith("/status"):
            self._send_json({"revision": 7, "available": 2, "inboxId": "main"})
            return
        if "/items?" in self.path or self.path.endswith("/items"):
            self._send_json({"items": [{"itemId": "i1", "channel": "qq", "kind": "mention", "title": "hi"}]})
            return
        if self.path.endswith("/events"):
            assert self.headers.get("Last-Event-ID") in (None, "5")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in (b"id: 6\nevent: items\ndata: {\"available\": 2}\n\n", b": keepalive\n\n", b"id: 7\ndata: nope\n\n"):
                self.wfile.write(chunk)
                self.wfile.flush()
            self.close_connection = True
            return
        self._send_json({"code": "NOT_FOUND"}, status=404)

    def do_PUT(self):
        if self._unauthenticated():
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._send_json({"ok": True})

    def do_DELETE(self):
        if self._unauthenticated():
            return
        self._send_json({"code": "NOT_FOUND"}, status=404)


class TokenFileMixin:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def token_file(self, mode: int = 0o600, value: str = TOKEN) -> str:
        path = self.tmp / "inbox.token"
        path.write_text(value, encoding="utf-8")
        os.chmod(path, mode)
        assert stat.S_IMODE(path.stat().st_mode) == mode
        return str(path)


class TokenRefTests(TokenFileMixin, unittest.TestCase):
    def test_parse_forms(self):
        self.assertEqual(client_mod.TokenRef.parse("file:/x/y").kind, "file")
        self.assertEqual(client_mod.TokenRef.parse("env:INBOX_TOKEN").target, "INBOX_TOKEN")
        self.assertEqual(client_mod.TokenRef.parse("/x/y").kind, "file")
        self.assertEqual(client_mod.TokenRef.parse("").describe(), "<unset>")

    def test_inline_is_rejected(self):
        with self.assertRaises(client_mod.InboxError) as ctx:
            client_mod.TokenRef.parse("inline:abc")
        self.assertEqual(ctx.exception.code, "TOKEN_INLINE_FORBIDDEN")

    def test_resolve_requires_0600(self):
        ref = client_mod.TokenRef.parse("file:" + self.token_file(mode=0o644))
        with self.assertRaises(client_mod.InboxError) as ctx:
            ref.resolve()
        self.assertEqual(ctx.exception.code, "TOKEN_FILE_PERMISSIONS")

    def test_resolve_reads_value_and_describe_never_leaks(self):
        ref = client_mod.TokenRef.parse("file:" + self.token_file())
        self.assertEqual(ref.resolve().strip(), TOKEN)
        self.assertNotIn(TOKEN, ref.describe())

    def test_missing_file(self):
        with self.assertRaises(client_mod.InboxError) as ctx:
            client_mod.TokenRef.parse("file:" + str(self.tmp / "nope")).resolve()
        self.assertEqual(ctx.exception.code, "TOKEN_UNREADABLE")


class ClientTests(TokenFileMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def client(self, token_path: str | None = None, token: str = TOKEN):
        path = token_path or self.token_file(value=token)
        return client_mod.InboxClient(self.base, client_mod.TokenRef.parse("file:" + path), timeout_seconds=5)

    def test_status_and_items(self):
        client = self.client()
        self.assertEqual(client.status("main")["available"], 2)
        items = client.items("main", limit=3)
        self.assertEqual(items[0]["itemId"], "i1")

    def test_channels_are_sent_as_repeated_query_keys(self):
        client = self.client()
        url = client._url("/v1/inboxes/main/items", {"limit": 5, "channel": ["qq", "bili"]})
        self.assertIn("channel=qq&channel=bili", url)
        self.assertIn("limit=5", url)
        # 空频道列表 = 不过滤（服务端 channels() 只认非空 channel 键）
        self.assertEqual(
            client._url("/v1/inboxes/main/status", {"channel": None}),
            f"{self.base}/v1/inboxes/main/status",
        )

    def test_limit_is_clamped_to_the_server_range(self):
        client = self.client()
        seen = {}
        client._request = lambda method, path, **kw: seen.update(kw) or {"items": []}
        client.items("main", limit=500)
        self.assertEqual(seen["query"]["limit"], 100)  # listAvailable 允许 1..100
        client.items("main", limit=0)
        self.assertEqual(seen["query"]["limit"], 1)

    def test_bad_token_maps_to_http_status(self):
        client = self.client(token="wrong")
        with self.assertRaises(client_mod.InboxError) as ctx:
            client.status("main")
        self.assertEqual(ctx.exception.status, 401)

    def test_invalid_inbox_id_is_rejected_before_network(self):
        with self.assertRaises(client_mod.InboxError) as ctx:
            self.client().status("bad id/../x")
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_delete_occupancy_tolerates_404(self):
        self.client().delete_occupancy("main", "sess-1")

    def test_transport_error_is_wrapped(self):
        client = client_mod.InboxClient(
            "http://127.0.0.1:9", client_mod.TokenRef.parse("file:" + self.token_file()), timeout_seconds=1
        )
        with self.assertRaises(client_mod.InboxError) as ctx:
            client.status("main")
        self.assertIn(ctx.exception.code, {"TRANSPORT", "TIMEOUT"})


class SseConsumeTests(unittest.TestCase):
    def worker(self, events, states):
        return sse_mod.SseWorker(
            client=None,
            inbox_id="main",
            last_event_id="",
            on_event=lambda eid, ev: events.append((eid, ev)),
            on_state=lambda state, detail: states.append(state),
            stop_event=threading.Event(),
        )

    def test_consume_parses_frames_and_comments(self):
        events, states = [], []
        worker = self.worker(events, states)
        stream = [b"id: 6\nevent: items\ndata: {\"available\": 2}\n\n", b": keepalive\n\n", b"id: 7\ndata: nope\n\n"]
        worker._consume(iter(stream))
        self.assertEqual([e[0] for e in events], ["6", "7"])
        self.assertEqual(events[0][1]["event"], "items")
        self.assertEqual(events[0][1]["data"], {"available": 2})
        self.assertEqual(events[1][1]["data"], {"raw": "nope"})
        self.assertEqual(worker.last_event_id, "7")

    def test_bad_consumer_does_not_kill_stream(self):
        boom = {"n": 0}

        def on_event(event_id, event):
            boom["n"] += 1
            raise RuntimeError("consumer exploded")

        worker = sse_mod.SseWorker(
            client=None, inbox_id="main", last_event_id="", on_event=on_event,
            on_state=lambda s, d: None, stop_event=threading.Event(),
        )
        worker._consume(iter([b"data: {}\n\n", b"data: {}\n\n"]))
        self.assertEqual(boom["n"], 2)


if __name__ == "__main__":
    unittest.main()
