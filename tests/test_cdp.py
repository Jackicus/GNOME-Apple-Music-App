"""Unit tests for src/backend/cdp.py (pure stdlib CDP client)."""

import base64
import hashlib
import http.server
import json
import socket
import struct
import threading
import time
import unittest
from typing import Any

from tests import SRC  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend.cdp import (
    CDPClient,
    CDPError,
    CDPTimeoutError,
    _mask,
    connect_to_chrome,
    discover_target,
)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class TestCDPDiscovery(unittest.TestCase):
    """Test target discovery from Chrome's /json and /json/list endpoints."""

    def setUp(self):
        self.server_port = 0
        self.targets: list[dict[str, Any]] = []
        self.status_code = 200
        self.serve_list_endpoint = False

        class Handler(http.server.BaseHTTPRequestHandler):
            test_case = self

            def do_GET(self):
                if self.path == "/json":
                    if self.test_case.serve_list_endpoint:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self.send_response(self.test_case.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(self.test_case.targets).encode("utf-8"))
                elif self.path == "/json/list":
                    self.send_response(self.test_case.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(self.test_case.targets).encode("utf-8"))
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                pass  # Suppress HTTP server stderr logs in test runs

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.server_port = self.httpd.server_port
        # A short poll, so shutdown() in tearDown does not wait out the default half second.
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_discover_apple_music_priority(self):
        """Apple Music page target should take precedence over other pages."""
        self.targets = [
            {
                "type": "page",
                "url": "chrome://newtab",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9999/other",
            },
            {
                "type": "page",
                "url": "https://music.apple.com/us/browse",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9999/music",
            },
            {
                "type": "service_worker",
                "url": "https://music.apple.com/sw.js",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9999/sw",
            },
        ]
        url = discover_target(port=self.server_port)
        self.assertEqual(url, "ws://127.0.0.1:9999/music")

    def test_discover_general_page_fallback(self):
        """When no Apple Music tab is open, any page target should be picked."""
        self.targets = [
            {
                "type": "service_worker",
                "url": "https://example.com/worker",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9999/worker",
            },
            {
                "type": "page",
                "url": "https://example.com/welcome",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9999/page",
            },
        ]
        url = discover_target(port=self.server_port)
        self.assertEqual(url, "ws://127.0.0.1:9999/page")

    def test_discover_any_ws_target_fallback(self):
        """When no page target exists, any target with a debugger URL is accepted."""
        self.targets = [
            {
                "type": "service_worker",
                "url": "https://example.com/worker",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9999/worker",
            }
        ]
        url = discover_target(port=self.server_port)
        self.assertEqual(url, "ws://127.0.0.1:9999/worker")

    def test_discover_json_list_fallback(self):
        """Should fall back to /json/list if /json returns 404."""
        self.serve_list_endpoint = True
        self.targets = [
            {
                "type": "page",
                "url": "https://apple.com",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9999/apple",
            }
        ]
        url = discover_target(port=self.server_port)
        self.assertEqual(url, "ws://127.0.0.1:9999/apple")

    def test_discover_no_suitable_target(self):
        """Should raise CDPError if no target has a webSocketDebuggerUrl."""
        self.targets = [{"type": "page", "url": "https://music.apple.com"}]
        with self.assertRaises(CDPError) as ctx:
            discover_target(port=self.server_port)
        self.assertIn("No suitable page target found", str(ctx.exception))

    def test_discover_connection_failure(self):
        """Should raise CDPError if port is not running."""
        # Find an unused port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        with self.assertRaises(CDPError):
            discover_target(port=free_port, timeout=1.0)


class MockWebSocketServer:
    """A minimal RFC 6455 WebSocket test server."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.client_sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self, handler):
        def run():
            try:
                self.sock.settimeout(2.0)
                client, _ = self.sock.accept()
                self.client_sock = client
                self._handle_handshake(client)
                handler(self, client)
            except Exception:
                pass
            finally:
                if self.client_sock:
                    try:
                        self.client_sock.close()
                    except Exception:
                        pass

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def _handle_handshake(self, client: socket.socket):
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(1024)
            if not chunk:
                return
            buf.extend(chunk)

        idx = buf.find(b"\r\n\r\n")
        header = bytes(buf[:idx]).decode("iso-8859-1")
        sec_key = None
        for line in header.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                sec_key = line.split(":", 1)[1].strip()
                break

        if not sec_key:
            return

        accept_val = base64.b64encode(
            hashlib.sha1((sec_key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")

        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_val}\r\n\r\n"
        )
        client.sendall(resp.encode("ascii"))

    def send_frame(self, opcode: int, payload: bytes, fin: bool = True):
        assert self.client_sock is not None
        header = bytearray()
        b0 = (0x80 if fin else 0) | (opcode & 0x0F)
        header.append(b0)
        length = len(payload)
        # Server frames are unmasked (mask bit = 0)
        if length < 126:
            header.append(length)
        elif length <= 0xFFFF:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        self.client_sock.sendall(header + payload)

    def recv_frame(self) -> tuple[int, bool, bytes]:
        assert self.client_sock is not None
        hdr = self.client_sock.recv(2)
        b0, b1 = hdr[0], hdr[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        mask = bool(b1 & 0x80)
        payload_len = b1 & 0x7F
        if payload_len == 126:
            payload_len = struct.unpack("!H", self.client_sock.recv(2))[0]
        elif payload_len == 127:
            payload_len = struct.unpack("!Q", self.client_sock.recv(8))[0]
        mask_key = self.client_sock.recv(4) if mask else None
        data = bytearray()
        while len(data) < payload_len:
            data.extend(self.client_sock.recv(payload_len - len(data)))
        payload = bytes(data)
        if mask and mask_key:
            payload = _mask(payload, mask_key)
        return opcode, fin, payload

    def close(self):
        self._stop_event.set()
        try:
            self.sock.close()
        except Exception:
            pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)


class TestCDPClient(unittest.TestCase):
    """Test CDPClient connection, request/response, evaluate, and RFC 6455 features."""

    def test_call_and_evaluate(self):
        """Test sending CDP calls and receiving responses, including Runtime.evaluate."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            # First request: custom call
            opcode, fin, payload = srv.recv_frame()
            msg = json.loads(payload.decode("utf-8"))
            srv.send_frame(1, json.dumps({"id": msg["id"], "result": {"ready": True}}).encode("utf-8"))

            # Second request: Runtime.evaluate
            opcode, fin, payload = srv.recv_frame()
            msg = json.loads(payload.decode("utf-8"))
            self.assertEqual(msg["method"], "Runtime.evaluate")
            srv.send_frame(1, json.dumps({
                "id": msg["id"],
                "result": {"result": {"type": "string", "value": "test-result"}}
            }).encode("utf-8"))

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                res = client.call("Custom.test", {"foo": "bar"})
                self.assertEqual(res, {"ready": True})

                val = client.evaluate("1 + 1")
                self.assertEqual(val, "test-result")
        finally:
            server.close()

    def test_evaluate_js_exception(self):
        """Test that Runtime.evaluate exceptionDetails raises CDPError."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            opcode, fin, payload = srv.recv_frame()
            msg = json.loads(payload.decode("utf-8"))
            srv.send_frame(1, json.dumps({
                "id": msg["id"],
                "result": {
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {"description": "ReferenceError: foo is not defined"}
                    }
                }
            }).encode("utf-8"))

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                with self.assertRaises(CDPError) as ctx:
                    client.evaluate("foo()")
                self.assertIn("ReferenceError: foo is not defined", str(ctx.exception))
        finally:
            server.close()

    def test_evaluate_js_exception_is_one_line(self):
        """The stack and the doubled "Uncaught" text stay out of the message."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            opcode, fin, payload = srv.recv_frame()
            msg = json.loads(payload.decode("utf-8"))
            srv.send_frame(1, json.dumps({
                "id": msg["id"],
                "result": {
                    "exceptionDetails": {
                        "text": "Uncaught (in promise) Error: HTTP 500 Unable to update tracks",
                        "exception": {"description": "Error: HTTP 500 Unable to update tracks\n"
                                                     "    at apiWrite (<anonymous>:59:28)"}
                    }
                }
            }).encode("utf-8"))

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                with self.assertRaises(CDPError) as ctx:
                    client.evaluate("window.__appleMusicLibrary.addToPlaylist('p', '1')", await_promise=True)
                self.assertEqual(str(ctx.exception), "JS Exception: HTTP 500 Unable to update tracks")
        finally:
            server.close()

    def test_cdp_error_response(self):
        """Test that a CDP error payload raises CDPError with code and message."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            opcode, fin, payload = srv.recv_frame()
            msg = json.loads(payload.decode("utf-8"))
            srv.send_frame(1, json.dumps({
                "id": msg["id"],
                "error": {"code": -32601, "message": "'NoSuchMethod' wasn't found"}
            }).encode("utf-8"))

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                with self.assertRaises(CDPError) as ctx:
                    client.call("NoSuchMethod")
                self.assertEqual(ctx.exception.code, -32601)
                self.assertIn("NoSuchMethod", str(ctx.exception))
        finally:
            server.close()

    def test_ping_pong_handling(self):
        """Test that a server Ping receives an immediate masked Pong with matching payload."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            # First receive the client's call
            call_op, call_fin, call_payload = srv.recv_frame()
            self.assertEqual(call_op, 1)  # Text
            call_msg = json.loads(call_payload.decode("utf-8"))

            # Send ping while client is waiting for response
            srv.send_frame(9, b"heartbeat")

            # Expect client to immediately respond with pong
            opcode, fin, payload = srv.recv_frame()
            self.assertEqual(opcode, 10)  # Pong
            self.assertEqual(payload, b"heartbeat")

            # Now send the response to the call
            srv.send_frame(1, json.dumps({"id": call_msg["id"], "result": {"ok": True}}).encode("utf-8"))

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                res = client.call("Test.ping")
                self.assertEqual(res, {"ok": True})
        finally:
            server.close()

    def test_fragmentation(self):
        """Test handling of fragmented text frames."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            opcode, fin, payload = srv.recv_frame()
            msg = json.loads(payload.decode("utf-8"))
            full_msg = json.dumps({"id": msg["id"], "result": {"payload": "fragmented data"}}).encode("utf-8")

            # Send in 2 fragments
            part1 = full_msg[:15]
            part2 = full_msg[15:]
            srv.send_frame(1, part1, fin=False)
            srv.send_frame(0, part2, fin=True)

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                res = client.call("Test.fragmented")
                self.assertEqual(res, {"payload": "fragmented data"})
        finally:
            server.close()

    def test_interleaved_events(self):
        """Test handling of incoming event notifications before command response."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            opcode, fin, payload = srv.recv_frame()
            msg = json.loads(payload.decode("utf-8"))

            # Send an event notification without "id"
            srv.send_frame(1, json.dumps({
                "method": "Page.frameNavigated",
                "params": {"frame": {"id": "123"}}
            }).encode("utf-8"))

            # Send the response
            srv.send_frame(1, json.dumps({"id": msg["id"], "result": {"done": True}}).encode("utf-8"))

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                res = client.call("Test.interleaved")
                self.assertEqual(res, {"done": True})
        finally:
            server.close()

    def test_large_message_framing(self):
        """Test sending and receiving large payloads (> 65536 bytes)."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            opcode, fin, payload = srv.recv_frame()
            msg = json.loads(payload.decode("utf-8"))
            large_string = "x" * 70000
            resp = json.dumps({"id": msg["id"], "result": {"data": large_string}}).encode("utf-8")
            srv.send_frame(1, resp)

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                res = client.call("Test.large")
                self.assertEqual(len(res["data"]), 70000)
        finally:
            server.close()

    def test_server_close_frame(self):
        """Test that server Close frame raises CDPError and marks connection closed."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            opcode, fin, payload = srv.recv_frame()
            # Send Close frame (opcode 8)
            srv.send_frame(8, b"")

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                with self.assertRaises(CDPError) as ctx:
                    client.call("Test.close")
                self.assertIn("WebSocket closed by server", str(ctx.exception))
        finally:
            server.close()


    def test_out_of_order_responses(self):
        """Test that responses arriving out of order are matched to their request ID."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            # Client sends call 1
            op1, fin1, payload1 = srv.recv_frame()
            msg1 = json.loads(payload1.decode("utf-8"))

            # Server sends response for ID 999 first (some other call or buffered), then for msg1
            srv.send_frame(1, json.dumps({"id": 999, "result": {"other": True}}).encode("utf-8"))
            srv.send_frame(1, json.dumps({"id": msg1["id"], "result": {"matched": True}}).encode("utf-8"))

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                res = client.call("Test.out_of_order")
                self.assertEqual(res, {"matched": True})
                # Verify that response 999 was buffered
                self.assertIn(999, client._pending_responses)
        finally:
            server.close()

    def test_call_timeout(self):
        """Test that client.call raises CDPTimeoutError when server does not reply in time."""
        server = MockWebSocketServer()

        def handler(srv: MockWebSocketServer, client: socket.socket):
            # Read client call but sleep without replying
            srv.recv_frame()
            time.sleep(1.0)

        server.start(handler)
        try:
            ws_url = f"ws://127.0.0.1:{server.port}/devtools/page/test"
            with CDPClient(ws_url, timeout=3.0) as client:
                with self.assertRaises(CDPTimeoutError):
                    client.call("Test.timeout", timeout=0.2)
        finally:
            server.close()

    def test_handshake_error_status(self):
        """Test that non-101 HTTP status during handshake raises CDPError."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def srv():
            try:
                c, _ = sock.accept()
                c.recv(1024)
                c.sendall(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
                c.close()
            except Exception:
                pass
            finally:
                sock.close()

        threading.Thread(target=srv, daemon=True).start()
        with self.assertRaises(CDPError) as ctx:
            CDPClient(f"ws://127.0.0.1:{port}/ws", timeout=1.0)
        self.assertIn("WebSocket handshake failed with status", str(ctx.exception))

    def test_handshake_bad_accept_header(self):
        """Test that invalid Sec-WebSocket-Accept raises CDPError."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def srv():
            try:
                c, _ = sock.accept()
                c.recv(1024)
                c.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: invalid-accept\r\n\r\n"
                )
                c.close()
            except Exception:
                pass
            finally:
                sock.close()

        threading.Thread(target=srv, daemon=True).start()
        with self.assertRaises(CDPError) as ctx:
            CDPClient(f"ws://127.0.0.1:{port}/ws", timeout=1.0)
        self.assertIn("Sec-WebSocket-Accept mismatch", str(ctx.exception))

    def test_connect_to_chrome_helper(self):
        """Test the connect_to_chrome convenience helper."""
        ws_srv = MockWebSocketServer()

        def ws_handler(srv: MockWebSocketServer, client: socket.socket):
            op, fin, payload = srv.recv_frame()
            msg = json.loads(payload.decode("utf-8"))
            srv.send_frame(1, json.dumps({"id": msg["id"], "result": {"connected": True}}).encode("utf-8"))

        ws_srv.start(ws_handler)

        class HttpHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                data = [{
                    "type": "page",
                    "url": "https://music.apple.com",
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{ws_srv.port}/devtools/page/test",
                }]
                self.wfile.write(json.dumps(data).encode("utf-8"))

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), HttpHandler)
        http_port = httpd.server_port
        t = threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True)
        t.start()

        try:
            with connect_to_chrome(port=http_port, timeout=3.0) as client:
                res = client.call("Test.connect")
                self.assertEqual(res, {"connected": True})
        finally:
            httpd.shutdown()
            httpd.server_close()
            ws_srv.close()

    def test_cdp_timeout_error_inheritance(self):
        """Test that CDPTimeoutError inherits from both CDPError and TimeoutError."""
        err = CDPTimeoutError("timed out")
        self.assertIsInstance(err, CDPError)
        self.assertIsInstance(err, TimeoutError)
        self.assertEqual(err.code, "timeout")


if __name__ == "__main__":
    unittest.main()
