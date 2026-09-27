"""Chrome DevTools Protocol (CDP) WebSocket client.

A pure standard-library implementation of RFC 6455 WebSocket client
and Chrome DevTools Protocol client for controlling headless Chrome.

The handshake, the frame codec and the reading of a JS exception are
pure functions (handshake_request, check_handshake, encode_frame,
decode_frame, close_frame, exception_message), shared with the
asynchronous client in client.py; the CDPClient here is the blocking
one, kept as the extension has it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
from typing import Any
import urllib.parse

# No urllib.request, no http.client and no ssl at load: this module is on
# the path of every am.py command, and those three are a third of a short
# command's run. Chrome's /json is one plain GET on a loopback socket, done
# by hand below; ssl is imported only for a wss:// target, which Chrome on
# 127.0.0.1 never is.

__all__ = [
    "CDPError",
    "CDPTimeoutError",
    "CDPClient",
    "http_get_json",
    "discover_target",
    "connect_to_chrome",
    "parse_ws_url",
    "handshake_request",
    "check_handshake",
    "encode_frame",
    "decode_frame",
    "close_frame",
    "exception_message",
]

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CDPError(Exception):
    """Base error raised when a Chrome DevTools Protocol operation fails."""

    def __init__(self, message: str, code: int | str | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class CDPTimeoutError(CDPError, TimeoutError):
    """Raised when a CDP network or handshake operation times out."""

    def __init__(self, message: str, data: Any = None):
        super().__init__(message, code="timeout", data=data)


def _mask(payload: bytes, mask_key: bytes) -> bytes:
    """XOR mask or unmask payload using a 4-byte masking key."""
    masked = bytearray(payload)
    for i in range(4):
        masked[i::4] = bytes(b ^ mask_key[i] for b in masked[i::4])
    return bytes(masked)


# ---------------------------------------------------------------------------
# The WebSocket handshake and frame codec, as pure functions
# ---------------------------------------------------------------------------


def parse_ws_url(ws_url: str) -> tuple[str, str, int, str, str]:
    """(scheme, host, port, path, netloc) of a ws:// or wss:// URL; CDPError otherwise."""
    parsed = urllib.parse.urlsplit(ws_url)
    if parsed.scheme not in ("ws", "wss"):
        raise CDPError(f"Unsupported WebSocket scheme: {parsed.scheme}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return parsed.scheme, host, port, path, parsed.netloc


def handshake_request(path: str, host_header: str) -> tuple[bytes, str]:
    """The client's HTTP Upgrade request for `path`, and the Sec-WebSocket-Key it carries
    (check_handshake wants it back)."""
    sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {sec_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    return request.encode("ascii"), sec_key


def check_handshake(header_bytes: bytes, sec_key: str) -> None:
    """Raise CDPError unless `header_bytes` (the response head, without its closing blank line)
    is a 101 whose Sec-WebSocket-Accept answers `sec_key`."""
    lines = header_bytes.split(b"\r\n")
    if not lines or not lines[0]:
        raise CDPError("Empty handshake response from server")

    status_line = lines[0].decode("iso-8859-1")
    parts = status_line.split()
    if len(parts) < 2 or parts[1] != "101":
        raise CDPError(f"WebSocket handshake failed with status: {status_line}")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" in line:
            name, val = line.split(b":", 1)
            headers[name.strip().lower().decode("iso-8859-1")] = val.strip().decode("iso-8859-1")

    expected_accept = base64.b64encode(
        hashlib.sha1((sec_key + WS_GUID).encode("ascii")).digest()
    ).decode("ascii")

    actual_accept = headers.get("sec-websocket-accept")
    if actual_accept != expected_accept:
        raise CDPError(
            f"Handshake failed: Sec-WebSocket-Accept mismatch (expected {expected_accept}, got {actual_accept})"
        )


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """One masked client-to-server RFC 6455 frame (FIN set) carrying `payload`."""
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))  # FIN=1, RSV=0, opcode

    length = len(payload)
    mask_bit = 0x80  # Client-to-server frames MUST be masked
    if length < 126:
        header.append(mask_bit | length)
    elif length <= 0xFFFF:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))

    mask_key = os.urandom(4)
    header.extend(mask_key)
    return bytes(header) + _mask(payload, mask_key)


def decode_frame(buffer: bytes | bytearray) -> tuple[int, bool, bytes, int] | None:
    """The frame at the front of `buffer` as (opcode, fin, payload, bytes consumed), or None
    while the buffer holds less than a whole frame. Server frames are unmasked; a masked one
    is unmasked all the same."""
    if len(buffer) < 2:
        return None
    b0, b1 = buffer[0], buffer[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    pos = 2

    if length == 126:
        if len(buffer) < 4:
            return None
        length = struct.unpack("!H", bytes(buffer[2:4]))[0]
        pos = 4
    elif length == 127:
        if len(buffer) < 10:
            return None
        length = struct.unpack("!Q", bytes(buffer[2:10]))[0]
        pos = 10

    mask_key = None
    if masked:
        if len(buffer) < pos + 4:
            return None
        mask_key = bytes(buffer[pos:pos + 4])
        pos += 4

    if len(buffer) < pos + length:
        return None
    payload = bytes(buffer[pos:pos + length])
    if mask_key:
        payload = _mask(payload, mask_key)
    return opcode, fin, payload, pos + length


def close_frame() -> bytes:
    """A masked Close frame with the normal-closure status, 1000."""
    return encode_frame(8, struct.pack("!H", 1000))


def exception_message(exc: dict[str, Any]) -> str:
    """One line for a Runtime.evaluate exceptionDetails.

    `description` is the message followed by the stack, and `text` repeats
    the message behind "Uncaught (in promise)". One line is all a
    notification can show; the rest rides along with the exception itself.
    """
    desc = (exc.get("exception") or {}).get("description") or exc.get("text") or "JS exception"
    message = desc.splitlines()[0]
    if message.startswith("Error: "):
        message = message[len("Error: "):]
    return message


def http_get_json(port: int, path: str, host: str = "127.0.0.1", timeout: float = 5.0) -> Any:
    """GET http://{host}:{port}{path} and parse the body as JSON — Chrome's
    own target list, on a loopback socket, without urllib. Raises
    CDPTimeoutError on a timeout, CDPError (its `code` the HTTP status)
    on a refused connection, a status other than 200 or a body that is
    not JSON."""
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "User-Agent: AppleMusicGNOME/1.0\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(request)
            raw = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout()
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw.extend(chunk)
                # Done as soon as the body the headers promise is in, so a
                # server that keeps the connection open does not hold this
                # until it times out.
                head_end = raw.find(b"\r\n\r\n")
                if head_end < 0:
                    continue
                head = bytes(raw[:head_end]).decode("iso-8859-1")
                length = None
                for line in head.split("\r\n")[1:]:
                    if line.lower().startswith("content-length:"):
                        try:
                            length = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            length = None
                if length is not None and len(raw) - head_end - 4 >= length:
                    break
    except (socket.timeout, TimeoutError) as e:
        raise CDPTimeoutError(f"Timed out talking to {host}:{port}") from e
    except OSError as e:
        raise CDPError(f"Could not connect to {host}:{port}: {e}") from e

    head_end = raw.find(b"\r\n\r\n")
    if head_end < 0:
        raise CDPError(f"No HTTP response from {host}:{port}")
    head = bytes(raw[:head_end]).decode("iso-8859-1")
    body = bytes(raw[head_end + 4:])
    lines = head.split("\r\n")
    parts = lines[0].split()
    try:
        status = int(parts[1])
    except (IndexError, ValueError) as e:
        raise CDPError(f"Bad HTTP status line from {host}:{port}: {lines[0]!r}") from e
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _dechunk(body)
    else:
        length = headers.get("content-length")
        if length is not None and length.isdigit():
            body = body[:int(length)]
    if status != 200:
        raise CDPError(f"HTTP {status} from {host}:{port}{path}", code=status)
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as e:
        raise CDPError(f"Invalid JSON from {host}:{port}{path}: {e}") from e


def _dechunk(body: bytes) -> bytes:
    """A chunked transfer body, joined."""
    out = bytearray()
    while True:
        line_end = body.find(b"\r\n")
        if line_end < 0:
            break
        try:
            size = int(body[:line_end].split(b";", 1)[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        out.extend(body[line_end + 2:line_end + 2 + size])
        body = body[line_end + 2 + size + 2:]
    return bytes(out)


def discover_target(port: int, host: str = "127.0.0.1", timeout: float = 5.0) -> str:
    """Discover an active page target on the Chrome debugging port.

    Queries http://{host}:{port}/json (falling back to /json/list) to find
    available DevTools targets. Returns the webSocketDebuggerUrl of the
    best matching target:
      1. An active page target for music.apple.com or apple.com.
      2. Any active page target.
      3. Any target advertising a webSocketDebuggerUrl.

    Raises CDPTimeoutError on timeout, or CDPError on connection/discovery failure.
    """
    try:
        try:
            targets = http_get_json(port, "/json", host=host, timeout=timeout)
        except CDPError as e:
            # Some Chromium builds serve targets at /json/list instead of /json.
            if e.code != 404 or isinstance(e, CDPTimeoutError):
                raise
            targets = http_get_json(port, "/json/list", host=host, timeout=timeout)
    except CDPTimeoutError as e:
        raise CDPTimeoutError(f"Target discovery timed out connecting to {host}:{port}") from e
    except CDPError as e:
        raise CDPError(f"Failed to discover target on {host}:{port}: {e}") from e

    if not isinstance(targets, list):
        raise CDPError(f"Expected a list of targets from {host}:{port}, got {type(targets).__name__}")

    ws_targets = [t for t in targets if isinstance(t, dict) and t.get("webSocketDebuggerUrl")]

    # Priority a: page target visiting music.apple.com or apple.com
    for t in ws_targets:
        t_url = t.get("url", "")
        if t.get("type") == "page" and ("music.apple.com" in t_url or "apple.com" in t_url):
            return str(t["webSocketDebuggerUrl"])

    # Priority b: any page target
    for t in ws_targets:
        if t.get("type") == "page":
            return str(t["webSocketDebuggerUrl"])

    # Priority c: any target with webSocketDebuggerUrl
    if ws_targets:
        return str(ws_targets[0]["webSocketDebuggerUrl"])

    raise CDPError("No suitable page target found")


class CDPClient:
    """Pure standard-library RFC 6455 WebSocket client for Chrome DevTools Protocol."""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self._next_id = 1
        self._closed = False
        self._buffer = bytearray()
        self._pending_responses: dict[int, dict[str, Any]] = {}
        self._call_lock = threading.Lock()
        self._send_lock = threading.Lock()

        scheme, self.host, self.port, self.path, netloc = parse_ws_url(ws_url)

        # Connect TCP socket and optionally wrap with TLS
        try:
            raw_sock = socket.create_connection((self.host, self.port), timeout=timeout)
            if scheme == "wss":
                import ssl
                ssl_context = ssl.create_default_context()
                self._sock: socket.socket | None = ssl_context.wrap_socket(
                    raw_sock, server_hostname=self.host
                )
            else:
                self._sock = raw_sock
            self._sock.settimeout(timeout)
        except (socket.timeout, TimeoutError) as e:
            raise CDPTimeoutError(f"Connection timed out to {self.host}:{self.port}") from e
        except OSError as e:
            raise CDPError(f"Failed to connect to {self.host}:{self.port}: {e}") from e

        # Perform RFC 6455 opening handshake
        try:
            self._perform_handshake(netloc)
        except Exception:
            self.close()
            raise

    def _perform_handshake(self, netloc: str) -> None:
        """Perform the client-side HTTP Upgrade handshake."""
        request, sec_key = handshake_request(self.path, netloc or f"{self.host}:{self.port}")

        try:
            assert self._sock is not None
            self._sock.sendall(request)

            # Read HTTP response headers until delimiter \r\n\r\n
            while b"\r\n\r\n" not in self._buffer:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise CDPError("Connection closed before WebSocket handshake completed")
                self._buffer.extend(chunk)

            idx = self._buffer.find(b"\r\n\r\n")
            header_bytes = bytes(self._buffer[:idx])
            del self._buffer[:idx + 4]
            check_handshake(header_bytes, sec_key)

        except (socket.timeout, TimeoutError) as e:
            raise CDPTimeoutError(f"WebSocket handshake timed out for {self.ws_url}") from e
        except CDPError:
            raise
        except Exception as e:
            raise CDPError(f"WebSocket handshake failed: {e}") from e

    def _fill_buffer(self, deadline: float | None = None) -> None:
        """Read whatever the socket has into the buffer, at least one byte."""
        if self._closed or not self._sock:
            raise CDPError("WebSocket connection closed")

        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CDPTimeoutError("Timed out reading from WebSocket")
            self._sock.settimeout(max(0.001, remaining))

        try:
            chunk = self._sock.recv(65536)
        except (socket.timeout, TimeoutError) as e:
            raise CDPTimeoutError("Timed out reading from WebSocket") from e
        except OSError as e:
            raise CDPError(f"Socket error reading from WebSocket: {e}") from e

        if not chunk:
            raise CDPError("WebSocket connection closed unexpectedly by remote peer")
        self._buffer.extend(chunk)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        """Send a single masked RFC 6455 frame to the server."""
        if self._closed or not self._sock:
            raise CDPError("WebSocket connection is closed")

        frame = encode_frame(opcode, payload)
        with self._send_lock:
            try:
                self._sock.sendall(frame)
            except OSError as e:
                raise CDPError(f"Failed to send WebSocket frame: {e}") from e

    def _read_frame(self, deadline: float | None = None) -> tuple[int, bool, bytes]:
        """Read and decode a single RFC 6455 frame. Returns (opcode, fin, payload)."""
        while True:
            frame = decode_frame(self._buffer)
            if frame is not None:
                opcode, fin, payload, consumed = frame
                del self._buffer[:consumed]
                return opcode, fin, payload
            self._fill_buffer(deadline=deadline)

    def _read_message(self, deadline: float | None = None) -> str:
        """Read a complete message, handling fragmented frames, ping/pong, and close."""
        fragments: list[bytes] = []
        message_opcode: int | None = None

        while True:
            opcode, fin, payload = self._read_frame(deadline=deadline)

            # Control frames can be interleaved with fragmented message frames
            if opcode == 9:  # Ping
                # Immediately reply with masked Pong containing identical payload
                self._send_frame(10, payload)
                continue
            elif opcode == 10:  # Pong
                # Ignore Pong frame
                continue
            elif opcode == 8:  # Close
                self.close()
                raise CDPError("WebSocket closed by server")

            if opcode in (1, 2):  # Text or Binary
                if message_opcode is not None:
                    raise CDPError("Received new data frame before prior message finished")
                message_opcode = opcode
                fragments.append(payload)
                if fin:
                    break
            elif opcode == 0:  # Continuation frame
                if message_opcode is None:
                    raise CDPError("Received continuation frame without preceding data frame")
                fragments.append(payload)
                if fin:
                    break
            else:
                raise CDPError(f"Unsupported WebSocket opcode: {opcode}")

        full_payload = b"".join(fragments)
        return full_payload.decode("utf-8")

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a CDP command and wait for its corresponding response.

        Args:
            method: DevTools protocol domain and method (e.g. 'Runtime.evaluate').
            params: Parameters dictionary.
            timeout: Maximum wait time in seconds (defaults to self.timeout).

        Returns:
            The 'result' dictionary from the CDP response.

        Raises:
            CDPError on protocol or socket error, or CDPTimeoutError on timeout.
        """
        if self._closed or not self._sock:
            raise CDPError("WebSocket connection is closed")

        effective_timeout = timeout if timeout is not None else self.timeout
        deadline = time.monotonic() + effective_timeout if effective_timeout is not None else None

        with self._call_lock:
            msg_id = self._next_id
            self._next_id += 1

            payload = json.dumps({
                "id": msg_id,
                "method": method,
                "params": params if params is not None else {},
            }).encode("utf-8")

            self._send_frame(1, payload)

            response: dict[str, Any] | None = None
            try:
                while True:
                    if msg_id in self._pending_responses:
                        response = self._pending_responses.pop(msg_id)
                        break

                    msg_str = self._read_message(deadline=deadline)
                    try:
                        msg = json.loads(msg_str)
                    except json.JSONDecodeError as e:
                        raise CDPError(f"Failed to decode JSON from CDP: {e}") from e

                    if isinstance(msg, dict) and "id" in msg:
                        resp_id = msg["id"]
                        if resp_id == msg_id:
                            response = msg
                            break
                        else:
                            self._pending_responses[resp_id] = msg
                    # Event notifications carry no 'id'; nothing here listens
                    # for any, so they are read past.
            finally:
                if self._sock and not self._closed:
                    try:
                        self._sock.settimeout(self.timeout)
                    except OSError:
                        pass

            assert response is not None
            if "error" in response:
                err = response["error"]
                if isinstance(err, dict):
                    raise CDPError(
                        err.get("message", "CDP error"),
                        code=err.get("code"),
                        data=err.get("data"),
                    )
                raise CDPError(str(err))

            res = response.get("result")
            return res if isinstance(res, dict) else {}

    def evaluate(
        self,
        expression: str,
        await_promise: bool = True,
        return_by_value: bool = True,
        timeout: float | None = None,
    ) -> Any:
        """Evaluate a JavaScript expression in the page context.

        Args:
            expression: JavaScript expression string.
            await_promise: Whether to await a returned Promise.
            return_by_value: Whether to return the result value directly.
            timeout: Maximum wait time in seconds.

        Returns:
            The evaluated value returned by value.

        Raises:
            CDPError if evaluation throws a JS exception or CDP command fails.
        """
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": return_by_value,
            },
            timeout=timeout,
        )

        if "exceptionDetails" in result:
            exc = result["exceptionDetails"]
            raise CDPError(f"JS Exception: {exception_message(exc)}", data=exc)

        result_obj = result.get("result")
        if isinstance(result_obj, dict):
            return result_obj.get("value")
        return None

    def close(self) -> None:
        """Cleanly close the WebSocket connection."""
        if self._closed:
            return
        self._closed = True
        sock = self._sock
        self._sock = None

        if sock is not None:
            try:
                sock.sendall(close_frame())
            except Exception:
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def __enter__(self) -> CDPClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def connect_to_chrome(port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> CDPClient:
    """Discover the active page target and connect a CDPClient to it."""
    ws_url = discover_target(port=port, host=host, timeout=timeout)
    return CDPClient(ws_url, timeout=timeout)
