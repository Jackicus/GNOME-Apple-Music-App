# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The WebSocket side of the Chrome DevTools Protocol, as pure functions: the RFC 6455
handshake and frame codec, and the one line a JavaScript exception comes to.

client.WebSocketTransport uses them for a DevTools port (the developer attach,
APPLE_MUSIC_DEBUG_PORT); the engine itself talks to Chrome over a pipe, where messages are
NUL-framed JSON and none of this is needed. Nothing here does I/O.
"""

import base64
import hashlib
import os
import struct
import urllib.parse

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class CDPError(Exception):
    """A WebSocket or DevTools failure: a bad URL, a refused handshake."""


def _mask(payload, mask_key):
    """XOR `payload` with the 4-byte `mask_key` (masking and unmasking alike)."""
    masked = bytearray(payload)
    for i in range(4):
        masked[i::4] = bytes(b ^ mask_key[i] for b in masked[i::4])
    return bytes(masked)


def parse_ws_url(ws_url):
    """(scheme, host, port, path, netloc) of a ws:// or wss:// URL; CDPError otherwise (and
    ValueError for one urllib cannot parse)."""
    parsed = urllib.parse.urlsplit(ws_url)
    if parsed.scheme not in ('ws', 'wss'):
        raise CDPError(f'Unsupported WebSocket scheme: {parsed.scheme}')
    host = parsed.hostname or '127.0.0.1'
    port = parsed.port or (443 if parsed.scheme == 'wss' else 80)
    path = parsed.path or '/'
    if parsed.query:
        path = f'{path}?{parsed.query}'
    return parsed.scheme, host, port, path, parsed.netloc


def handshake_request(path, host_header):
    """The client's HTTP Upgrade request for `path`, and the Sec-WebSocket-Key it carries
    (check_handshake wants it back)."""
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    request = (
        f'GET {path} HTTP/1.1\r\n'
        f'Host: {host_header}\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        f'Sec-WebSocket-Key: {key}\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        '\r\n'
    )
    return request.encode('ascii'), key


def check_handshake(head, key):
    """CDPError unless `head` (the response head, without its closing blank line) is a 101
    whose Sec-WebSocket-Accept answers `key`."""
    lines = head.split(b'\r\n')
    if not lines or not lines[0]:
        raise CDPError('Empty handshake response from server')
    status_line = lines[0].decode('iso-8859-1')
    parts = status_line.split()
    if len(parts) < 2 or parts[1] != '101':
        raise CDPError(f'WebSocket handshake failed with status: {status_line}')
    headers = {}
    for line in lines[1:]:
        if b':' in line:
            name, value = line.split(b':', 1)
            headers[name.strip().lower().decode('iso-8859-1')] = value.strip().decode('iso-8859-1')
    expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode('ascii')).digest())
    accept = headers.get('sec-websocket-accept')
    if accept != expected.decode('ascii'):
        raise CDPError(f'Handshake failed: Sec-WebSocket-Accept mismatch (got {accept})')


def encode_frame(opcode, payload):
    """One masked client-to-server frame (FIN set) carrying `payload`."""
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(struct.pack('!H', length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack('!Q', length))
    mask_key = os.urandom(4)
    header.extend(mask_key)
    return bytes(header) + _mask(payload, mask_key)


def decode_frame(buffer):
    """The frame at the front of `buffer` as (opcode, fin, payload, bytes consumed), or None
    while the buffer holds less than a whole frame. Server frames are unmasked; a masked one
    is unmasked all the same."""
    if len(buffer) < 2:
        return None
    fin = bool(buffer[0] & 0x80)
    opcode = buffer[0] & 0x0F
    masked = bool(buffer[1] & 0x80)
    length = buffer[1] & 0x7F
    pos = 2
    if length == 126:
        if len(buffer) < 4:
            return None
        length = struct.unpack('!H', bytes(buffer[2:4]))[0]
        pos = 4
    elif length == 127:
        if len(buffer) < 10:
            return None
        length = struct.unpack('!Q', bytes(buffer[2:10]))[0]
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


def close_frame():
    """A masked Close frame with the normal-closure status, 1000."""
    return encode_frame(8, struct.pack('!H', 1000))


def exception_message(details):
    """One line for a Runtime.evaluate exceptionDetails: `description` is the message and
    then the stack, and `text` repeats the message behind "Uncaught (in promise)"; the first
    line of the description, without "Error: ", is what a toast can show."""
    description = ((details.get('exception') or {}).get('description') or details.get('text')
                   or 'JS exception')
    message = description.splitlines()[0]
    if message.startswith('Error: '):
        message = message[len('Error: '):]
    return message
