"""Unit tests for src/backend/client.py: the asynchronous CDP client against a fake Chrome.

The fake is test_cdp.py's WebSocket server done over asyncio (the client and the server share
one loop, so a handler can assert and push events), answering CDP calls from a table of
responders and pushing events on demand. It reads the client's frames with cdp.decode_frame.
"""

import asyncio
import base64
import hashlib
import http.server
import json
import re
import socket
import struct
import threading
import unittest

from tests import SRC  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend import client as client_module
from applemusic.backend.cdp import WS_GUID, decode_frame
from applemusic.backend.client import CDPClient, connect_page, load_bridge
from applemusic.backend.errors import EngineError

PROBE = 'window.__appleMusicLibrary && window.__appleMusicLibrary.__version === "'
STATUS = 'window.__appleMusicLibrary ? window.__appleMusicLibrary.status() : null'
SUBSCRIBE = 'window.__appleMusicLibrary.subscribe()'


def server_frame(opcode, payload, fin=True):
    """An unmasked server-to-client RFC 6455 frame."""
    header = bytearray([(0x80 if fin else 0) | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack('!H', length))
    else:
        header.append(127)
        header.extend(struct.pack('!Q', length))
    return bytes(header) + payload


def value(v):
    """A Runtime.evaluate result carrying `v` by value."""
    if v is None:
        return {'result': {'type': 'undefined'}}
    return {'result': {'type': type(v).__name__, 'value': v}}


class CDPFailure(Exception):
    """A responder raising this answers with a CDP error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FakeChrome:
    """Answers each call from `responders[method](message)`: a result dict, None for no answer
    (the call hangs), or CDPFailure for an error. Unknown methods get {}. `calls` records
    every (method, params) received; `frames` every frame."""

    def __init__(self):
        self.responders = {}
        self.calls = []
        self.frames = []
        self.writer = None
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._serve, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return f'ws://127.0.0.1:{self.port}/devtools/page/test'

    async def _serve(self, reader, writer):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
        except asyncio.IncompleteReadError:
            writer.close()
            return
        key = ''
        for line in head.decode('iso-8859-1').split('\r\n'):
            if line.lower().startswith('sec-websocket-key:'):
                key = line.split(':', 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        writer.write((
            'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
            f'Sec-WebSocket-Accept: {accept}\r\n\r\n').encode())
        await writer.drain()
        self.writer = writer
        buffer = bytearray()
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while (frame := decode_frame(buffer)) is not None:
                    opcode, fin, payload, consumed = frame
                    del buffer[:consumed]
                    self.frames.append((opcode, payload))
                    if opcode == 8:
                        return
                    if opcode == 1:
                        await self._on_message(json.loads(payload))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _on_message(self, message):
        method, params = message['method'], message.get('params', {})
        self.calls.append((method, params))
        responder = self.responders.get(method)
        if responder is None:
            await self.send({'id': message['id'], 'result': {}})
            return
        try:
            result = responder(message)
            if asyncio.iscoroutine(result):
                result = await result
        except CDPFailure as e:
            await self.send({'id': message['id'], 'error': {'code': e.code, 'message': str(e)}})
            return
        if result is not None:
            await self.send({'id': message['id'], 'result': result})

    def methods(self):
        return [method for method, _ in self.calls]

    async def send(self, message):
        await self.send_raw(server_frame(1, json.dumps(message).encode()))

    async def send_raw(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def drop(self):
        """Hang up on the client."""
        self.writer.close()
        await self.writer.wait_closed()

    async def close(self):
        if self.writer is not None:
            self.writer.close()
        self.server.close()
        await self.server.wait_closed()


class FakePage:
    """What Runtime.evaluate meets: a page that may or may not hold the bridge (by version),
    with MusicKit ready or not. Installed as the fake's Runtime.evaluate responder."""

    def __init__(self, ready=True):
        self.bridge = None      # the version injected
        self.ready = ready
        self.injections = 0
        self.subscriptions = 0

    def __call__(self, message):
        expression = message['params']['expression']
        assert message['params']['returnByValue'] is True
        if expression.startswith('(' + PROBE):
            wanted = re.search(r'=== "([0-9a-f]+)"', expression).group(1)
            return value(self.status() if self.bridge == wanted else None)
        if expression.startswith('window.__appleMusicLibraryWanted = '):
            self.bridge = json.loads(expression.split('\n', 1)[0].split('= ', 1)[1].rstrip(';'))
            self.injections += 1
            return value(None)
        if expression == STATUS:
            return value(self.status() if self.bridge else None)
        if expression == SUBSCRIBE:
            self.subscriptions += 1
            return value({'subscribed': True})
        raise AssertionError(f'unexpected expression {expression[:60]!r}')

    def status(self):
        return {'ready': self.ready, 'engine': True, 'authorized': False, 'storefront': 'us',
                'bitrate': 256}


async def until(predicate, timeout=2.0):
    """Wait for `predicate()` to hold; fail loudly when it does not in time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError('condition not met in time')
        await asyncio.sleep(0.01)


class ClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.chrome = FakeChrome()
        self.chrome.responders['Page.getFrameTree'] = (
            lambda m: {'frameTree': {'frame': {'id': 'main', 'url': 'https://music.apple.com/'}}})
        self.ws_url = await self.chrome.start()
        self.client = CDPClient(timeout=3)

    async def asyncTearDown(self):
        await self.client.close()
        await self.chrome.close()

    async def test_connect_prepares_the_page(self):
        self.assertFalse(self.client.connected)
        await self.client.connect(self.ws_url)
        self.assertTrue(self.client.connected)
        self.assertEqual(self.chrome.methods(), [
            'Runtime.enable', 'Page.enable', 'Runtime.addBinding', 'Page.getFrameTree'])
        self.assertEqual(self.chrome.calls[2][1], {'name': '__amEvent'})
        self.assertEqual(self.client._main_frame, 'main')

    async def test_connect_browser_target_skips_the_page_setup(self):
        await self.client.connect(self.ws_url, page=False)
        self.assertEqual(self.chrome.calls, [])
        self.assertEqual(await self.client.call('Browser.getVersion'), {})

    async def test_call_and_response(self):
        self.chrome.responders['Custom.test'] = lambda m: {'ready': True, 'echo': m['params']}
        await self.client.connect(self.ws_url)
        result = await self.client.call('Custom.test', {'foo': 'bar'})
        self.assertEqual(result, {'ready': True, 'echo': {'foo': 'bar'}})
        self.assertEqual(await self.client.call('Custom.unknown'), {})

    async def test_responses_are_correlated_by_id(self):
        # The first call is answered after the second, and each gets its own.
        async def late(message):
            await asyncio.sleep(0.05)
            await self.chrome.send({'id': message['id'], 'result': {'slow': True}})

        def slow(message):
            asyncio.get_running_loop().create_task(late(message))
        self.chrome.responders['Slow'] = slow
        self.chrome.responders['Fast'] = lambda m: {'fast': True}
        await self.client.connect(self.ws_url)
        results = await asyncio.gather(self.client.call('Slow'), self.client.call('Fast'))
        self.assertEqual(results, [{'slow': True}, {'fast': True}])

    async def test_evaluate_awaits_a_promise_value(self):
        def evaluate(message):
            self.assertEqual(message['params'], {
                'expression': 'answer()', 'awaitPromise': True, 'returnByValue': True})
            return value(42)
        self.chrome.responders['Runtime.evaluate'] = evaluate
        await self.client.connect(self.ws_url)
        self.assertEqual(await self.client.evaluate('answer()'), 42)

    async def test_evaluate_undefined_and_objects(self):
        answers = {'undefined': value(None), 'obj': value({'a': [1, 2]}), 'no': value(False)}
        self.chrome.responders['Runtime.evaluate'] = lambda m: answers[m['params']['expression']]
        await self.client.connect(self.ws_url)
        self.assertIsNone(await self.client.evaluate('undefined'))
        self.assertEqual(await self.client.evaluate('obj'), {'a': [1, 2]})
        self.assertIs(await self.client.evaluate('no', await_promise=False), False)

    async def test_bridge_calls_pass_json_arguments(self):
        seen = []

        def evaluate(message):
            seen.append(message['params']['expression'])
            return value({'ok': True})
        self.chrome.responders['Runtime.evaluate'] = evaluate
        await self.client.connect(self.ws_url)
        self.assertEqual(await self.client.bridge('play', 'album', 'l.abc', {'shuffle': True}),
                         {'ok': True})
        self.assertEqual(
            seen, ['window.__appleMusicLibrary.play("album", "l.abc", {"shuffle": true})'])

    async def test_js_exception_is_an_api_error(self):
        self.chrome.responders['Runtime.evaluate'] = lambda m: {'exceptionDetails': {
            'text': 'Uncaught (in promise) Error: HTTP 500 Unable to update tracks',
            'exception': {'description': 'Error: HTTP 500 Unable to update tracks\n'
                                         '    at apiWrite (<anonymous>:59:28)'}}}
        await self.client.connect(self.ws_url)
        with self.assertRaises(EngineError) as ctx:
            await self.client.evaluate('window.__appleMusicLibrary.addToPlaylist("p", "1")')
        self.assertEqual(ctx.exception.code, 'api')
        self.assertEqual(ctx.exception.message, 'HTTP 500 Unable to update tracks')

    async def test_cdp_error_is_an_api_error(self):
        def missing(message):
            raise CDPFailure(-32601, "'NoSuch.method' wasn't found")
        self.chrome.responders['NoSuch.method'] = missing
        await self.client.connect(self.ws_url)
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('NoSuch.method')
        self.assertEqual(ctx.exception.code, 'api')
        self.assertIn("NoSuch.method: 'NoSuch.method' wasn't found", ctx.exception.message)

    async def test_binding_event_is_dispatched(self):
        await self.client.connect(self.ws_url)
        exact, wildcard, everything = [], [], []
        self.client.on('am:playbackStateDidChange', lambda name, data: exact.append((name, data)))
        self.client.on('am:*', lambda name, data: wildcard.append(name))
        self.client.on('*', lambda name, data: everything.append(name))
        await self.chrome.send({'method': 'Runtime.bindingCalled', 'params': {
            'name': '__amEvent', 'executionContextId': 1,
            'payload': json.dumps({'name': 'playbackStateDidChange',
                                   'data': {'state': 'playing'}})}})
        await self.chrome.send({'method': 'Runtime.bindingCalled', 'params': {
            'name': '__amEvent', 'executionContextId': 1,
            'payload': json.dumps({'name': 'playbackTimeDidChange', 'data': {'position': 3}})}})
        await until(lambda: len(everything) == 2)
        self.assertEqual(exact, [('am:playbackStateDidChange', {'state': 'playing'})])
        self.assertEqual(wildcard, ['am:playbackStateDidChange', 'am:playbackTimeDidChange'])
        self.assertEqual(everything, wildcard)

    async def test_other_bindings_and_cdp_events(self):
        await self.client.connect(self.ws_url)
        bridge, cdp = [], []
        self.client.on('am:*', lambda name, data: bridge.append(name))
        handler = self.client.on('Page.frameNavigated', lambda name, data: cdp.append(data))
        await self.chrome.send({'method': 'Runtime.bindingCalled', 'params': {
            'name': 'someoneElse', 'payload': 'x', 'executionContextId': 1}})
        await self.chrome.send({'method': 'Page.frameNavigated',
                                'params': {'frame': {'id': 'main'}}})
        await until(lambda: len(cdp) == 1)
        self.assertEqual(cdp, [{'frame': {'id': 'main'}}])
        self.assertEqual(bridge, [])
        self.client.off('Page.frameNavigated', handler)
        await self.chrome.send({'method': 'Page.frameNavigated', 'params': {'frame': {'id': '2'}}})
        await self.chrome.send({'method': 'Runtime.bindingCalled', 'params': {
            'name': '__amEvent', 'payload': json.dumps({'name': 'x', 'data': None}),
            'executionContextId': 1}})
        await until(lambda: bridge == ['am:x'])
        self.assertEqual(len(cdp), 1)

    async def test_a_coroutine_handler_runs_as_a_task(self):
        await self.client.connect(self.ws_url)
        done = asyncio.Event()

        async def handler(name, data):
            await asyncio.sleep(0)
            done.set()
        self.client.on('Page.loadEventFired', handler)
        await self.chrome.send({'method': 'Page.loadEventFired', 'params': {'timestamp': 1}})
        await asyncio.wait_for(done.wait(), 2)

    async def test_timeout(self):
        self.chrome.responders['Slow.call'] = lambda m: None
        await self.client.connect(self.ws_url)
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('Slow.call', timeout=0.2)
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertTrue(self.client.connected)  # a slow answer is not a dead Chrome
        self.assertEqual(self.client._pending, {})

    async def test_closed_socket_is_engine_down(self):
        async def hang_up(message):
            await self.chrome.drop()
        self.chrome.responders['Hang'] = hang_up
        await self.client.connect(self.ws_url)
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('Hang')
        self.assertEqual(ctx.exception.code, 'engine-down')
        await self.client.wait_closed()
        self.assertFalse(self.client.connected)
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('Anything')
        self.assertEqual(ctx.exception.code, 'engine-down')

    async def test_close_sends_a_close_frame(self):
        await self.client.connect(self.ws_url)
        await self.client.close()
        self.assertFalse(self.client.connected)
        await until(lambda: any(opcode == 8 for opcode, _ in self.chrome.frames))
        self.assertEqual(self.chrome.frames[-1][1], struct.pack('!H', 1000))

    async def test_ping_gets_a_pong(self):
        await self.client.connect(self.ws_url)
        await self.chrome.send_raw(server_frame(9, b'heartbeat'))
        await until(lambda: any(opcode == 10 for opcode, _ in self.chrome.frames))
        self.assertIn((10, b'heartbeat'), self.chrome.frames)

    async def test_fragmented_and_large_messages(self):
        big = 'x' * 70000

        async def fragmented(message):
            data = json.dumps({'id': message['id'], 'result': {'data': big}}).encode()
            await self.chrome.send_raw(server_frame(1, data[:100], fin=False))
            await self.chrome.send_raw(server_frame(0, data[100:], fin=True))
        self.chrome.responders['Big'] = fragmented
        await self.client.connect(self.ws_url)
        self.assertEqual((await self.client.call('Big'))['data'], big)

    async def test_connection_refused_is_engine_down(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            free = s.getsockname()[1]
        with self.assertRaises(EngineError) as ctx:
            await self.client.connect(f'ws://127.0.0.1:{free}/devtools/page/x')
        self.assertEqual(ctx.exception.code, 'engine-down')

    async def test_bad_handshake_is_engine_down(self):
        async def serve(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 500 Internal Server Error\r\n\r\n')
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with self.assertRaises(EngineError) as ctx:
                await self.client.connect(f'ws://127.0.0.1:{port}/ws')
            self.assertEqual(ctx.exception.code, 'engine-down')
            self.assertIn('500', ctx.exception.message)
        finally:
            server.close()
            await server.wait_closed()

    async def test_ensure_bridge_injects_once(self):
        page = FakePage()
        self.chrome.responders['Runtime.evaluate'] = page
        await self.client.connect(self.ws_url)
        await self.client.ensure_bridge()
        self.assertEqual(page.bridge, load_bridge()[1])
        self.assertEqual(page.injections, 1)
        self.assertEqual(page.subscriptions, 0)
        await self.client.ensure_bridge()  # the probe finds it
        self.assertEqual(page.injections, 1)
        await self.client.subscribe()
        self.assertEqual((page.injections, page.subscriptions), (1, 1))

    async def test_ensure_bridge_replaces_an_older_one(self):
        page = FakePage()
        page.bridge = 'deadbeef0000'
        self.chrome.responders['Runtime.evaluate'] = page
        await self.client.connect(self.ws_url)
        await self.client.ensure_bridge()
        self.assertEqual(page.bridge, load_bridge()[1])
        self.assertEqual(page.injections, 1)

    async def test_bridge_is_put_back_after_a_navigation(self):
        page = FakePage()
        self.chrome.responders['Runtime.evaluate'] = page
        await self.client.connect(self.ws_url)
        await self.client.subscribe()
        self.assertEqual((page.injections, page.subscriptions), (1, 1))
        # A context for another frame (an iframe): nothing.
        await self.chrome.send({'method': 'Runtime.executionContextCreated', 'params': {'context': {
            'id': 7, 'origin': 'https://idmsa.apple.com', 'name': '',
            'auxData': {'isDefault': True, 'type': 'default', 'frameId': 'iframe'}}}})
        await self.chrome.send({'method': 'Runtime.executionContextCreated', 'params': {'context': {
            'id': 8, 'origin': '://', 'name': 'isolated',
            'auxData': {'isDefault': False, 'type': 'isolated', 'frameId': 'main'}}}})
        await asyncio.sleep(0.05)
        self.assertEqual(page.injections, 1)
        # The page navigated: a new default context for the main frame, the bridge gone.
        page.bridge = None
        page.subscriptions = 0
        await self.chrome.send({'method': 'Runtime.executionContextCreated', 'params': {'context': {
            'id': 9, 'origin': 'https://music.apple.com', 'name': '',
            'auxData': {'isDefault': True, 'type': 'default', 'frameId': 'main'}}}})
        await until(lambda: page.subscriptions == 1)
        self.assertEqual(page.bridge, load_bridge()[1])
        self.assertEqual(page.injections, 2)

    async def test_no_bridge_until_asked(self):
        page = FakePage()
        self.chrome.responders['Runtime.evaluate'] = page
        await self.client.connect(self.ws_url)
        # Runtime.enable replays the existing contexts; nothing was asked for yet.
        await self.chrome.send({'method': 'Runtime.executionContextCreated', 'params': {'context': {
            'id': 1, 'auxData': {'isDefault': True, 'type': 'default', 'frameId': 'main'}}}})
        await asyncio.sleep(0.05)
        self.assertEqual(page.injections, 0)

    async def test_ensure_bridge_times_out_without_musickit(self):
        page = FakePage(ready=False)
        self.chrome.responders['Runtime.evaluate'] = page
        await self.client.connect(self.ws_url)
        with self.assertRaises(EngineError) as ctx:
            await self.client.ensure_bridge(timeout=0.5)
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertGreaterEqual(page.injections, 2)  # it kept trying

    async def test_ensure_bridge_retries_a_failing_evaluate(self):
        page = FakePage()
        attempts = []

        def flaky(message):
            attempts.append(1)
            if len(attempts) == 1:
                raise CDPFailure(-32000, 'Cannot find context with specified id')
            return page(message)
        self.chrome.responders['Runtime.evaluate'] = flaky
        await self.client.connect(self.ws_url)
        await self.client.ensure_bridge(timeout=2)
        self.assertEqual(page.injections, 1)


class ConnectPageTest(unittest.IsolatedAsyncioTestCase):
    """connect_page: the target from /json/list, then the WebSocket."""

    async def test_connect_page(self):
        chrome = FakeChrome()
        chrome.responders['Page.getFrameTree'] = lambda m: {'frameTree': {'frame': {'id': 'f'}}}
        ws_url = await chrome.start()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps([{'type': 'page', 'url': 'https://music.apple.com/us/new',
                                    'webSocketDebuggerUrl': ws_url}]).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
        try:
            client = await connect_page(httpd.server_port, timeout=3)
            try:
                self.assertTrue(client.connected)
                self.assertEqual(client._main_frame, 'f')
            finally:
                await client.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            await chrome.close()


class LoadBridgeTest(unittest.TestCase):
    def test_version_is_the_files_hash(self):
        source, version = load_bridge()
        self.assertEqual(len(version), 12)
        self.assertTrue(source.startswith(f'window.__appleMusicLibraryWanted = "{version}";\n'))
        self.assertIn('subscribe: function', source)
        self.assertEqual(client_module.BINDING, '__amEvent')


if __name__ == '__main__':
    unittest.main()
