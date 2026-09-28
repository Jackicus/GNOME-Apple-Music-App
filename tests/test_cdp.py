"""Unit tests for src/backend/cdp.py: the WebSocket handshake and frame codec, pure functions
(test_client.py drives them through a socket)."""

import base64
import hashlib
import struct
import unittest

from tests import SRC  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend.cdp import (
    WS_GUID,
    CDPError,
    _mask,
    check_handshake,
    close_frame,
    decode_frame,
    encode_frame,
    exception_message,
    handshake_request,
    parse_ws_url,
)

LENGTHS = (0, 1, 125, 126, 65535, 65536, 70000)


def server_frame(opcode, payload, fin=True, mask_key=None):
    """A server-to-client frame, unmasked unless `mask_key` is given."""
    header = bytearray([(0x80 if fin else 0) | opcode])
    mask_bit = 0x80 if mask_key else 0
    length = len(payload)
    if length < 126:
        header.append(mask_bit | length)
    elif length <= 0xFFFF:
        header.append(mask_bit | 126)
        header.extend(struct.pack('!H', length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack('!Q', length))
    if mask_key:
        header.extend(mask_key)
        payload = _mask(payload, mask_key)
    return bytes(header) + payload


class FrameTest(unittest.TestCase):
    def test_round_trips(self):
        for length in LENGTHS:
            with self.subTest(length=length):
                payload = bytes(range(256)) * (length // 256) + bytes(range(length % 256))
                frame = encode_frame(1, payload)
                self.assertTrue(frame[1] & 0x80, 'client frames are masked')
                self.assertEqual(decode_frame(frame), (1, True, payload, len(frame)))

    def test_the_length_field(self):
        self.assertEqual(encode_frame(1, b'x' * 125)[1] & 0x7F, 125)
        self.assertEqual(encode_frame(1, b'x' * 126)[1] & 0x7F, 126)
        self.assertEqual(encode_frame(1, b'x' * 65535)[1] & 0x7F, 126)
        self.assertEqual(encode_frame(1, b'x' * 65536)[1] & 0x7F, 127)

    def test_a_partial_frame_is_none(self):
        for length in (0, 125, 126, 65536):
            frame = server_frame(2, b'y' * length)
            for cut in range(len(frame)):
                if cut in (0, 1, 2, 3, 4, 9, 10, len(frame) - 1) or cut % 997 == 0:
                    with self.subTest(length=length, cut=cut):
                        self.assertIsNone(decode_frame(frame[:cut]))
            self.assertIsNotNone(decode_frame(frame))

    def test_what_follows_a_frame_is_left(self):
        first, second = server_frame(1, b'one'), server_frame(9, b'ping', fin=True)
        buffer = bytearray(first + second)
        opcode, fin, payload, used = decode_frame(buffer)
        self.assertEqual((opcode, fin, payload, used), (1, True, b'one', len(first)))
        del buffer[:used]
        self.assertEqual(decode_frame(buffer), (9, True, b'ping', len(second)))

    def test_masked_server_frames_and_fragments(self):
        key = b'\x01\x02\x03\x04'
        frame = server_frame(1, b'hello, masked', fin=False, mask_key=key)
        self.assertEqual(decode_frame(frame), (1, False, b'hello, masked', len(frame)))
        self.assertEqual(decode_frame(server_frame(0, b'', mask_key=key))[:3], (0, True, b''))

    def test_close_frame(self):
        opcode, fin, payload, _used = decode_frame(close_frame())
        self.assertEqual((opcode, fin, payload), (8, True, struct.pack('!H', 1000)))


class HandshakeTest(unittest.TestCase):
    def accept(self, key):
        return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()

    def test_request(self):
        request, key = handshake_request('/devtools/browser/x', '127.0.0.1:9300')
        text = request.decode('ascii')
        self.assertTrue(text.startswith('GET /devtools/browser/x HTTP/1.1\r\n'))
        self.assertIn('Host: 127.0.0.1:9300\r\n', text)
        self.assertIn(f'Sec-WebSocket-Key: {key}\r\n', text)
        self.assertIn('Sec-WebSocket-Version: 13\r\n', text)
        self.assertTrue(text.endswith('\r\n\r\n'))
        self.assertEqual(len(base64.b64decode(key)), 16)
        self.assertNotEqual(key, handshake_request('/', 'h')[1])

    def test_a_good_answer(self):
        key = 'dGhlIHNhbXBsZSBub25jZQ=='
        head = (f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                f'Sec-WebSocket-Accept: {self.accept(key)}').encode()
        check_handshake(head, key)
        # RFC 6455's own example.
        self.assertEqual(self.accept(key), 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')

    def test_not_101(self):
        with self.assertRaises(CDPError) as ctx:
            check_handshake(b'HTTP/1.1 500 Internal Server Error', 'k')
        self.assertIn('500', str(ctx.exception))
        with self.assertRaises(CDPError):
            check_handshake(b'', 'k')
        with self.assertRaises(CDPError):
            check_handshake(b'SSH-2.0-x', 'k')

    def test_a_wrong_accept(self):
        head = b'HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: bm9wZQ=='
        with self.assertRaises(CDPError) as ctx:
            check_handshake(head, 'dGhlIHNhbXBsZSBub25jZQ==')
        self.assertIn('mismatch', str(ctx.exception))
        with self.assertRaises(CDPError):
            check_handshake(b'HTTP/1.1 101 Switching Protocols', 'k')  # no Accept at all


class UrlTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_ws_url('ws://127.0.0.1:9300/devtools/browser/abc'),
                         ('ws', '127.0.0.1', 9300, '/devtools/browser/abc', '127.0.0.1:9300'))
        self.assertEqual(parse_ws_url('wss://example.org/x?y=1'),
                         ('wss', 'example.org', 443, '/x?y=1', 'example.org'))
        self.assertEqual(parse_ws_url('ws://localhost')[2:4], (80, '/'))

    def test_not_a_websocket(self):
        with self.assertRaises(CDPError):
            parse_ws_url('http://127.0.0.1:9300/json')
        with self.assertRaises(ValueError):
            parse_ws_url('ws://[bad/x')


class ExceptionMessageTest(unittest.TestCase):
    def test_the_first_line_without_error(self):
        details = {'text': 'Uncaught (in promise) Error: HTTP 500 Unable to update tracks',
                   'exception': {'description': 'Error: HTTP 500 Unable to update tracks\n'
                                                '    at apiWrite (<anonymous>:59:28)'}}
        self.assertEqual(exception_message(details), 'HTTP 500 Unable to update tracks')

    def test_other_errors_keep_their_name(self):
        details = {'exception': {'description': "TypeError: Cannot read properties of "
                                                "undefined (reading 'play')\n    at x"}}
        self.assertEqual(exception_message(details),
                         "TypeError: Cannot read properties of undefined (reading 'play')")

    def test_text_alone_or_nothing(self):
        self.assertEqual(exception_message({'text': 'Uncaught SyntaxError'}),
                         'Uncaught SyntaxError')
        self.assertEqual(exception_message({}), 'JS exception')


if __name__ == '__main__':
    unittest.main()
