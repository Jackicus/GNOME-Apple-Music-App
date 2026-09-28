"""Unit tests for src/backend/client.py: the asynchronous CDP client against fake Chromes.

FakeBrowser is Chrome's browser endpoint as the client meets it: Target.getTargets lists a
music.apple.com page, Target.attachToTarget answers a session, and calls on that session are
answered from a table of responders, as the page would answer them. PipeFakeChrome carries it
over two pipes, NUL-framed as --remote-debugging-pipe is (the engine's transport);
FakeChrome over a WebSocket, as a DevTools port does (scripts/am.py --attach). The client and
the fake share one loop, so a responder can assert and push events. tests/test_engine.py puts
the same FakeBrowser behind a Unix socket.
"""

import asyncio
import base64
import hashlib
import http.server
import json
import math
import os
import re
import socket
import struct
import threading
import time
import unittest

from tests import SRC  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend import chrome
from applemusic.backend import client as client_module
from applemusic.backend.cdp import WS_GUID, decode_frame
from applemusic.backend.client import (
    CDPClient,
    PipeTransport,
    WebSocketTransport,
    attach_devtools,
    load_bridge,
    open_page,
)
from applemusic.backend.errors import EngineError

PROBE = 'window.__appleMusicLibrary && window.__appleMusicLibrary.__version === "'
STATUS = 'window.__appleMusicLibrary ? window.__appleMusicLibrary.status() : null'
SUBSCRIBE = 'window.__appleMusicLibrary.subscribe()'
MISSING = 'typeof window.__appleMusicLibrary === "undefined"'
PAGE_TARGET = {'targetId': 'PAGE-1', 'type': 'page', 'title': 'Apple Music',
               'url': 'https://music.apple.com/us/new', 'attached': False}
PAGE_SETUP = ['Runtime.enable', 'Page.enable', 'Inspector.enable', 'Runtime.addBinding',
              'Page.getFrameTree']


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


def thrown(description):
    """A Runtime.evaluate result for a thrown exception."""
    return {'result': {'type': 'object'}, 'exceptionDetails': {
        'text': 'Uncaught', 'exception': {'description': description}}}


class CDPFailure(Exception):
    """A responder raising this answers with a CDP error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FakeBrowser:
    """Chrome's browser endpoint. Browser methods (no sessionId) get the defaults below, or
    `browser_responders[method](message)`; a page's (with the session attachToTarget gave) are
    answered from `responders[method](message)`: a result dict, None for no answer (the call
    hangs), or CDPFailure for an error; unknown methods get {}. `targets` is what
    Target.getTargets lists; `calls` records every (method, params, sessionId). Subclasses
    carry the messages (write(), serve)."""

    def __init__(self):
        self.responders = {'Page.getFrameTree': lambda m: {
            'frameTree': {'frame': {'id': 'main', 'url': 'https://music.apple.com/'}}}}
        self.browser_responders = {}
        self.targets = [dict(PAGE_TARGET)]
        self.calls = []
        self.sessions = {}          # sessionId -> targetId
        self.session = None         # the latest attached
        self.close_on_browser_close = True
        self._sessions_made = 0

    def methods(self, session=True):
        """The methods called on a page session (on the browser with session=False)."""
        return [method for method, _params, sid in self.calls if (sid is not None) == session]

    async def on_message(self, message):
        method, params = message['method'], message.get('params', {})
        session = message.get('sessionId')
        self.calls.append((method, params, session))
        if session is None:
            responder = self.browser_responders.get(method) or getattr(
                self, 'browser_' + method.replace('.', '_'), None)
        elif session not in self.sessions:
            await self.send({'id': message['id'], 'sessionId': session, 'error': {
                'code': -32001, 'message': 'Session with given id not found.'}})
            return
        else:
            responder = self.responders.get(method)
        reply = {'id': message['id']}
        if session is not None:
            reply['sessionId'] = session
        if responder is None:
            await self.send(dict(reply, result={}))
            return
        try:
            result = responder(message)
            if asyncio.iscoroutine(result):
                result = await result
        except CDPFailure as e:
            await self.send(dict(reply, error={'code': e.code, 'message': str(e)}))
            return
        if result is not None:
            await self.send(dict(reply, result=result))

    # -- the browser's own methods ---------------------------------------------------------

    def browser_Target_getTargets(self, message):
        return {'targetInfos': [dict(target) for target in self.targets]}

    def browser_Target_attachToTarget(self, message):
        target_id = message['params'].get('targetId')
        if not any(target['targetId'] == target_id for target in self.targets):
            raise CDPFailure(-32602, 'No target with given id found')
        assert message['params'].get('flatten') is True
        self._sessions_made += 1
        session = f'SESSION-{self._sessions_made}'
        self.sessions[session] = target_id
        self.session = session
        return {'sessionId': session}

    def browser_Target_createTarget(self, message):
        target = {'targetId': f'PAGE-{len(self.targets) + 1}', 'type': 'page', 'title': '',
                  'url': message['params']['url'], 'attached': False}
        self.targets.append(target)
        return {'targetId': target['targetId']}

    async def browser_Browser_close(self, message):
        if not self.close_on_browser_close:
            return None  # a wedged Chrome: no answer, still running
        await self.send({'id': message['id'], 'result': {}})
        await self.drop()
        return None

    # -- pushing -----------------------------------------------------------------------------

    async def send_event(self, method, params, session=None):
        """An event of the attached page (or of `session`)."""
        await self.send({'method': method, 'params': params,
                         'sessionId': session or self.session})

    async def send_browser_event(self, method, params):
        await self.send({'method': method, 'params': params})

    async def send_binding(self, name, data):
        """A bridge event, as the page's window.__amEvent posts it."""
        await self.send_event('Runtime.bindingCalled', {
            'name': '__amEvent', 'executionContextId': 1,
            'payload': json.dumps({'name': name, 'data': data})})

    async def send(self, message):
        await self.write(json.dumps(message).encode())


class PipeFakeChrome(FakeBrowser):
    """FakeBrowser on two pipes, as Chrome's --remote-debugging-pipe: client_transport() is
    the client's end, and the fake reads and writes the other with a PipeTransport of its
    own (the framing is the same both ways)."""

    def __init__(self):
        super().__init__()
        self._client_read, own_write = os.pipe()
        own_read, self._client_write = os.pipe()
        self.own = PipeTransport(own_read, own_write)
        self.hung_up = False
        self._task = None

    def client_transport(self):
        return PipeTransport(self._client_read, self._client_write)

    async def start(self):
        await self.own.open()
        self._task = asyncio.create_task(self._serve())

    async def _serve(self):
        while (payload := await self.own.receive()) is not None:
            await self.on_message(json.loads(payload))
        self.hung_up = True

    async def write(self, payload):
        await self.own.send(payload)

    def write_raw(self, data):
        self.own._write.write(data)

    async def drop(self):
        """Chrome goes: its end of the pipe closes."""
        self.own.close()

    async def close(self):
        self.own.close()
        if self._task is not None:
            self._task.cancel()
            await asyncio.wait([self._task])


class FakeChrome(FakeBrowser):
    """FakeBrowser behind a DevTools WebSocket (the browser endpoint /json/version names):
    what scripts/am.py --attach meets. `frames` records every frame received."""

    def __init__(self):
        super().__init__()
        self.frames = []
        self.writer = None
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._serve, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return f'ws://127.0.0.1:{self.port}/devtools/browser/test'

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
                        await self.on_message(json.loads(payload))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def write(self, payload):
        await self.send_raw(server_frame(1, payload))

    async def send_raw(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def drop(self):
        self.writer.close()
        await self.writer.wait_closed()

    async def close(self):
        if self.writer is not None:
            self.writer.close()
        self.server.close()
        await self.server.wait_closed()


class FakePage:
    """What Runtime.evaluate meets: a page that may or may not hold the bridge (by version),
    with MusicKit ready or not. Installed as the fake's Runtime.evaluate responder. A call
    into the bridge while it is missing throws the TypeError the page would."""

    def __init__(self, ready=True):
        self.bridge = None      # the version injected
        self.ready = ready
        self.injections = 0
        self.subscriptions = 0
        self.inject_error = None  # a description the injection throws (a broken bridge.js)

    def __call__(self, message):
        expression = message['params']['expression']
        assert message['params']['returnByValue'] is True
        if expression.startswith('(' + PROBE):
            wanted = re.search(r'=== "([0-9a-f]+)"', expression).group(1)
            return value(self.status() if self.bridge == wanted else None)
        if expression.startswith('window.__appleMusicLibraryWanted = '):
            if self.inject_error:
                return thrown(self.inject_error)
            self.bridge = json.loads(expression.split('\n', 1)[0].split('= ', 1)[1].rstrip(';'))
            self.injections += 1
            return value(None)
        if expression == STATUS:
            return value(self.status() if self.bridge else None)
        if expression == MISSING:
            return value(self.bridge is None)
        if expression == SUBSCRIBE:
            self.subscriptions += 1
            return value({'subscribed': True})
        if expression == '0':  # the engine asking whether the page answers at all
            return value(0)
        if expression.startswith('window.__appleMusicLibrary.') and self.bridge is None:
            method = expression[len('window.__appleMusicLibrary.'):].split('(', 1)[0]
            return thrown(f"TypeError: Cannot read properties of undefined (reading '{method}')"
                          '\n    at <anonymous>:1:28')
        if expression == 'window.__appleMusicLibrary.status()':
            return value(self.status())
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


def context_created(context_id, frame='main', default=True):
    return ('Runtime.executionContextCreated', {'context': {
        'id': context_id, 'origin': 'https://music.apple.com', 'name': '',
        'auxData': {'isDefault': default, 'type': 'default' if default else 'isolated',
                    'frameId': frame}}})


class PipeClientTest(unittest.IsolatedAsyncioTestCase):
    """The client over the pipe: the session model, calls, events and their failures."""

    async def asyncSetUp(self):
        self.chrome = PipeFakeChrome()
        await self.chrome.start()
        self.client = CDPClient(timeout=3)

    async def asyncTearDown(self):
        await self.client.close()
        await self.chrome.close()

    async def open(self, wait=2):
        await self.client.connect(self.chrome.client_transport())
        await self.client.attach_page(wait)

    # -- connecting and attaching ------------------------------------------------------------

    async def test_attach_finds_the_page_and_prepares_its_session(self):
        self.assertFalse(self.client.connected)
        await self.open()
        self.assertTrue(self.client.attached)
        self.assertEqual(self.chrome.methods(session=False), [
            'Target.setDiscoverTargets', 'Target.getTargets', 'Target.attachToTarget'])
        attach = next(params for method, params, _ in self.chrome.calls
                      if method == 'Target.attachToTarget')
        self.assertEqual(attach, {'targetId': 'PAGE-1', 'flatten': True})
        self.assertEqual(self.chrome.methods(), PAGE_SETUP)
        self.assertTrue(all(sid == 'SESSION-1' for _, _, sid in self.chrome.calls[3:]))
        binding = next(params for method, params, _ in self.chrome.calls
                       if method == 'Runtime.addBinding')
        self.assertEqual(binding, {'name': '__amEvent'})
        self.assertEqual(self.client._main_frame, 'main')

    async def test_attach_waits_for_music_apple_com(self):
        self.chrome.targets = [{'targetId': 'PAGE-1', 'type': 'page', 'url': 'about:blank'}]

        async def load():
            await asyncio.sleep(0.1)
            self.chrome.targets[0]['url'] = 'https://music.apple.com/us/new'
            await self.chrome.send_browser_event('Target.targetInfoChanged',
                                                 {'targetInfo': self.chrome.targets[0]})
        loading = asyncio.create_task(load())
        await self.open(wait=3)
        await loading
        self.assertEqual(self.chrome.session, 'SESSION-1')
        self.assertNotIn('Page.navigate', self.chrome.methods())
        self.assertNotIn('Target.createTarget', self.chrome.methods(session=False))

    async def test_a_page_elsewhere_is_sent_to_music_apple_com(self):
        self.chrome.targets = [
            {'targetId': 'W', 'type': 'service_worker', 'url': 'https://music.apple.com/sw.js'},
            {'targetId': 'P', 'type': 'page', 'url': 'chrome://newtab/'}]
        await self.open(wait=0.1)
        self.assertEqual(self.chrome.sessions[self.chrome.session], 'P')
        navigate = [params for method, params, _ in self.chrome.calls if method == 'Page.navigate']
        self.assertEqual(navigate, [{'url': chrome.START_URL}])

    async def test_with_no_page_one_is_opened(self):
        self.chrome.targets = []
        await self.open(wait=0.1)
        created = [params for method, params, _ in self.chrome.calls
                   if method == 'Target.createTarget']
        self.assertEqual(created, [{'url': chrome.START_URL}])
        self.assertEqual(self.chrome.sessions[self.chrome.session], 'PAGE-1')

    async def test_connecting_twice_is_refused(self):
        await self.open()
        other = PipeFakeChrome()
        transport = other.client_transport()
        try:
            with self.assertRaises(RuntimeError):
                await self.client.connect(transport)
        finally:
            transport.close()
            other.own.close()

    async def test_a_failed_attach_leaves_nothing_running(self):
        def refuse(message):
            raise CDPFailure(-32000, 'Page.enable failed')
        self.chrome.responders['Page.enable'] = refuse
        await self.client.connect(self.chrome.client_transport())
        reader = self.client._reader_task
        with self.assertRaises(EngineError) as ctx:
            await self.client.attach_page(1)
        self.assertEqual(ctx.exception.code, 'api')
        self.assertFalse(self.client.connected)
        await asyncio.sleep(0)
        self.assertTrue(reader.done())
        await until(lambda: self.chrome.hung_up)  # our end of the pipe was let go

    async def test_open_page(self):
        client = await open_page(self.chrome.client_transport(), timeout=3, wait=1)
        try:
            self.assertTrue(client.attached)
        finally:
            await client.close()

    # -- calls -----------------------------------------------------------------------------

    async def test_calls_go_to_the_page_or_the_browser(self):
        self.chrome.responders['Custom.test'] = lambda m: {'ready': True, 'echo': m['params']}
        await self.open()
        self.assertEqual(await self.client.call('Custom.test', {'foo': 'bar'}),
                         {'ready': True, 'echo': {'foo': 'bar'}})
        self.assertEqual(await self.client.call('Custom.unknown'), {})
        self.assertEqual(await self.client.call('Browser.getVersion', browser=True), {})
        self.assertEqual(self.chrome.calls[-3][2], 'SESSION-1')
        self.assertIsNone(self.chrome.calls[-1][2])

    async def test_a_page_call_needs_a_page(self):
        await self.client.connect(self.chrome.client_transport())
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('Runtime.evaluate')
        self.assertEqual(ctx.exception.code, 'engine-down')

    async def test_responses_are_correlated_by_id(self):
        # The first call is answered after the second, and each gets its own.
        async def late(message):
            await asyncio.sleep(0.05)
            await self.chrome.send({'id': message['id'], 'sessionId': message['sessionId'],
                                    'result': {'slow': True}})

        def slow(message):
            asyncio.get_running_loop().create_task(late(message))
        self.chrome.responders['Slow'] = slow
        self.chrome.responders['Fast'] = lambda m: {'fast': True}
        await self.open()
        results = await asyncio.gather(self.client.call('Slow'), self.client.call('Fast'))
        self.assertEqual(results, [{'slow': True}, {'fast': True}])

    async def test_evaluate_awaits_a_promise_value(self):
        def evaluate(message):
            self.assertEqual(message['params'], {
                'expression': 'answer()', 'awaitPromise': True, 'returnByValue': True})
            return value(42)
        self.chrome.responders['Runtime.evaluate'] = evaluate
        await self.open()
        self.assertEqual(await self.client.evaluate('answer()'), 42)

    async def test_evaluate_undefined_objects_and_unserializable_values(self):
        answers = {'undefined': value(None), 'obj': value({'a': [1, 2]}), 'no': value(False)}
        for text in ('NaN', 'Infinity', '-Infinity', '-0', '5n', 'odd'):
            answers[text] = {'result': {'type': 'number', 'unserializableValue': text}}
        self.chrome.responders['Runtime.evaluate'] = lambda m: answers[m['params']['expression']]
        await self.open()
        self.assertIsNone(await self.client.evaluate('undefined'))
        self.assertEqual(await self.client.evaluate('obj'), {'a': [1, 2]})
        self.assertIs(await self.client.evaluate('no', await_promise=False), False)
        self.assertTrue(math.isnan(await self.client.evaluate('NaN')))
        self.assertEqual(await self.client.evaluate('Infinity'), math.inf)
        self.assertEqual(await self.client.evaluate('-Infinity'), -math.inf)
        zero = await self.client.evaluate('-0')
        self.assertEqual((zero, math.copysign(1, zero)), (0, -1))
        self.assertEqual(await self.client.evaluate('5n'), 5)
        self.assertEqual(await self.client.evaluate('odd'), 'odd')

    async def test_bridge_calls_pass_json_arguments(self):
        seen = []

        def evaluate(message):
            seen.append(message['params']['expression'])
            return value({'ok': True})
        self.chrome.responders['Runtime.evaluate'] = evaluate
        await self.open()
        self.assertEqual(await self.client.bridge('play', 'album', 'l.abc', {'shuffle': True}),
                         {'ok': True})
        self.assertEqual(
            seen, ['window.__appleMusicLibrary.play("album", "l.abc", {"shuffle": true})'])

    async def test_js_exception_is_an_api_error(self):
        self.chrome.responders['Runtime.evaluate'] = lambda m: {'exceptionDetails': {
            'text': 'Uncaught (in promise) Error: HTTP 500 Unable to update tracks',
            'exception': {'description': 'Error: HTTP 500 Unable to update tracks\n'
                                         '    at apiWrite (<anonymous>:59:28)'}}}
        await self.open()
        with self.assertRaises(EngineError) as ctx:
            await self.client.evaluate('window.__appleMusicLibrary.addToPlaylist("p", "1")')
        self.assertEqual(ctx.exception.code, 'api')
        self.assertEqual(ctx.exception.message, 'HTTP 500 Unable to update tracks')

    async def test_cdp_error_is_an_api_error(self):
        def missing(message):
            raise CDPFailure(-32601, "'NoSuch.method' wasn't found")
        self.chrome.responders['NoSuch.method'] = missing
        await self.open()
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('NoSuch.method')
        self.assertEqual(ctx.exception.code, 'api')
        self.assertIn("NoSuch.method: 'NoSuch.method' wasn't found", ctx.exception.message)

    async def test_timeout(self):
        self.chrome.responders['Slow.call'] = lambda m: None
        timed_out = []
        self.client.on_timeout = timed_out.append
        await self.open()
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('Slow.call', timeout=0.2)
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertTrue(self.client.connected)  # a slow answer is not a dead Chrome
        self.assertEqual(self.client._pending, {})
        self.assertEqual(timed_out, ['Slow.call'])

    async def test_a_message_split_across_reads(self):
        await self.open()

        async def halves(message):
            data = json.dumps({'id': message['id'], 'sessionId': message['sessionId'],
                               'result': {'whole': True}}).encode()
            self.chrome.write_raw(data[:10])
            await asyncio.sleep(0.05)
            self.chrome.write_raw(data[10:] + b'\0')
        self.chrome.responders['Split'] = halves
        self.assertEqual(await self.client.call('Split'), {'whole': True})

    async def test_big_answers_keep_their_order(self):
        big = 'x' * (client_module.BIG_MESSAGE + 1000)
        seen = []
        await self.open()
        self.client.on('Custom.before', lambda name, data: seen.append(name))
        self.client.on('Custom.after', lambda name, data: seen.append(name))

        async def answer(message):
            await self.chrome.send_event('Custom.before', {})
            await self.chrome.send({'id': message['id'], 'sessionId': message['sessionId'],
                                    'result': {'data': big}})
            await self.chrome.send_event('Custom.after', {})
        self.chrome.responders['Big'] = answer
        result = await self.client.call('Big')
        self.assertEqual(len(result['data']), len(big))
        await until(lambda: len(seen) == 2)
        self.assertEqual(seen, ['Custom.before', 'Custom.after'])

    # -- the connection going ----------------------------------------------------------------

    async def test_chrome_going_is_engine_down(self):
        async def hang_up(message):
            await self.chrome.drop()
        self.chrome.responders['Hang'] = hang_up
        await self.open()
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('Hang')
        self.assertEqual(ctx.exception.code, 'engine-down')
        await self.client.wait_closed()
        self.assertFalse(self.client.connected)
        self.assertEqual(self.client.lost_reason, 'closed by Chrome')
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('Anything')
        self.assertEqual(ctx.exception.code, 'engine-down')

    async def test_close_fails_calls_in_flight_and_lets_go_of_the_pipe(self):
        self.chrome.responders['Slow'] = lambda m: None
        await self.open()
        call = asyncio.create_task(self.client.call('Slow'))
        await until(lambda: 'Slow' in self.chrome.methods())
        await self.client.close()
        with self.assertRaises(EngineError) as ctx:
            await call
        self.assertEqual(ctx.exception.code, 'engine-down')
        self.assertFalse(self.client.connected)
        await until(lambda: self.chrome.hung_up)

    async def assert_lost(self, reason, push):
        self.chrome.responders['Hang'] = lambda m: None
        await self.open()
        call = asyncio.create_task(self.client.call('Hang'))
        await until(lambda: 'Hang' in self.chrome.methods())
        await push()
        with self.assertRaises(EngineError) as ctx:
            await call
        self.assertEqual(ctx.exception.code, 'engine-down')
        self.assertIn(reason, ctx.exception.message)
        await asyncio.wait_for(self.client.wait_closed(), 1)
        self.assertFalse(self.client.connected)
        self.assertEqual(self.client.lost_reason, reason)

    async def test_a_crashed_page_loses_the_connection(self):
        await self.assert_lost('the page crashed', lambda: self.chrome.send_event(
            'Inspector.targetCrashed', {}))

    async def test_the_browser_saying_the_page_crashed(self):
        await self.assert_lost('the page crashed', lambda: self.chrome.send_browser_event(
            'Target.targetCrashed', {'targetId': 'PAGE-1', 'status': 'crashed',
                                     'errorCode': 139}))

    async def test_the_page_closing(self):
        await self.assert_lost('the page was closed', lambda: self.chrome.send_browser_event(
            'Target.targetDestroyed', {'targetId': 'PAGE-1'}))

    async def test_the_session_detached(self):
        await self.assert_lost('the page was detached', lambda: self.chrome.send_browser_event(
            'Target.detachedFromTarget', {'sessionId': 'SESSION-1', 'targetId': 'PAGE-1'}))

    async def test_another_page_crashing_changes_nothing(self):
        await self.open()
        await self.chrome.send_browser_event('Target.targetCrashed', {'targetId': 'OTHER'})
        await self.chrome.send_browser_event('Target.detachedFromTarget',
                                             {'sessionId': 'SESSION-9'})
        await self.chrome.send_event('Inspector.targetCrashed', {}, session='SESSION-9')
        self.assertEqual(await self.client.call('Still.there'), {})
        self.assertTrue(self.client.connected)

    # -- events ----------------------------------------------------------------------------

    async def test_binding_event_is_dispatched(self):
        await self.open()
        exact, wildcard, everything = [], [], []
        self.client.on('am:playbackStateDidChange', lambda name, data: exact.append((name, data)))
        self.client.on('am:*', lambda name, data: wildcard.append(name))
        self.client.on('*', lambda name, data: everything.append(name))
        await self.chrome.send_binding('playbackStateDidChange', {'state': 'playing'})
        await self.chrome.send_binding('playbackTimeDidChange', {'position': 3})
        await until(lambda: len(everything) == 2)
        self.assertEqual(exact, [('am:playbackStateDidChange', {'state': 'playing'})])
        self.assertEqual(wildcard, ['am:playbackStateDidChange', 'am:playbackTimeDidChange'])
        self.assertEqual(everything, wildcard)

    async def test_other_bindings_sessions_and_cdp_events(self):
        await self.open()
        bridge, cdp = [], []
        self.client.on('am:*', lambda name, data: bridge.append(name))
        handler = self.client.on('Page.frameNavigated', lambda name, data: cdp.append(data))
        await self.chrome.send_event('Runtime.bindingCalled', {
            'name': 'someoneElse', 'payload': 'x', 'executionContextId': 1})
        await self.chrome.send_event('Page.frameNavigated', {'frame': {'id': 'other'}},
                                     session='SESSION-9')  # not ours
        await self.chrome.send_event('Page.frameNavigated', {'frame': {'id': 'main'}})
        await until(lambda: len(cdp) == 1)
        self.assertEqual(cdp, [{'frame': {'id': 'main'}}])
        self.assertEqual(bridge, [])
        self.client.off('Page.frameNavigated', handler)
        await self.chrome.send_event('Page.frameNavigated', {'frame': {'id': '2'}})
        await self.chrome.send_binding('x', None)
        await until(lambda: bridge == ['am:x'])
        self.assertEqual(len(cdp), 1)

    async def test_a_coroutine_handler_runs_as_a_task(self):
        await self.open()
        done = asyncio.Event()

        async def handler(name, data):
            await asyncio.sleep(0)
            done.set()
        self.client.on('Page.loadEventFired', handler)
        await self.chrome.send_event('Page.loadEventFired', {'timestamp': 1})
        await asyncio.wait_for(done.wait(), 2)

    async def test_a_raising_handler_does_not_stop_later_events(self):
        await self.open()
        seen = []

        def broken(name, data):
            raise RuntimeError('a bug in a handler')
        self.client.on('am:*', broken)
        self.client.on('am:*', lambda name, data: seen.append(name))
        with self.assertLogs(client_module.log, 'ERROR') as logs:
            await self.chrome.send_binding('one', None)
            await self.chrome.send_binding('two', None)
            await until(lambda: seen == ['am:one', 'am:two'])
        self.assertEqual(len(logs.records), 2)

    async def test_malformed_messages_are_logged_and_the_session_goes_on(self):
        await self.open()
        seen = []
        self.client.on('am:*', lambda name, data: seen.append((name, data)))
        with self.assertLogs(client_module.log, 'WARNING') as logs:
            for payload in (json.dumps({'name': 1}), '[1]', 'not json', json.dumps('x')):
                await self.chrome.send_event('Runtime.bindingCalled', {
                    'name': '__amEvent', 'executionContextId': 1, 'payload': payload})
            self.chrome.write_raw(b'{not json at all\0')
            await self.chrome.send({'id': [1], 'result': {}})  # an id that cannot be looked up
            await self.chrome.send_binding('fine', {'ok': True})
            await until(lambda: seen == [('am:fine', {'ok': True})])
        self.assertTrue(self.client.connected)
        self.assertEqual(await self.client.call('Still.there'), {})
        messages = '\n'.join(logs.output)
        self.assertIn('unreadable event', messages)
        self.assertIn('undecodable message', messages)

    # -- the bridge ------------------------------------------------------------------------

    async def open_with_page(self, **kwargs):
        page = FakePage(**kwargs)
        self.chrome.responders['Runtime.evaluate'] = page
        await self.open()
        return page

    async def test_ensure_bridge_injects_once(self):
        page = await self.open_with_page()
        await self.client.ensure_bridge()
        self.assertEqual(page.bridge, load_bridge()[1])
        self.assertEqual(page.injections, 1)
        self.assertEqual(page.subscriptions, 0)
        await self.client.ensure_bridge()  # the probe finds it
        self.assertEqual(page.injections, 1)
        await self.client.subscribe()
        self.assertEqual((page.injections, page.subscriptions), (1, 1))

    async def test_ensure_bridge_replaces_an_older_one(self):
        page = await self.open_with_page()
        page.bridge = 'deadbeef0000'
        await self.client.ensure_bridge()
        self.assertEqual(page.bridge, load_bridge()[1])
        self.assertEqual(page.injections, 1)

    async def test_bridge_is_put_back_after_a_navigation(self):
        page = await self.open_with_page()
        resets = []
        self.client.on('am:bridgeReset', lambda name, data: resets.append(data))
        await self.client.subscribe()
        self.assertEqual((page.injections, page.subscriptions), (1, 1))
        # A context for another frame (an iframe), or an isolated one: nothing.
        await self.chrome.send_event(*context_created(7, frame='iframe'))
        await self.chrome.send_event(*context_created(8, default=False))
        await asyncio.sleep(0.05)
        self.assertEqual(page.injections, 1)
        # The page navigated: a new default context for the main frame, the bridge gone.
        page.bridge = None
        page.subscriptions = 0
        await self.chrome.send_event(*context_created(9))
        await until(lambda: page.subscriptions == 1)
        self.assertEqual(page.bridge, load_bridge()[1])
        self.assertEqual(page.injections, 2)
        await until(lambda: resets == [{}])  # the page's state went with the document

    async def test_no_bridge_until_asked(self):
        page = await self.open_with_page()
        # Runtime.enable replays the existing contexts; nothing was asked for yet.
        await self.chrome.send_event(*context_created(1))
        await asyncio.sleep(0.05)
        self.assertEqual(page.injections, 0)

    async def test_ensure_bridge_times_out_without_musickit(self):
        page = await self.open_with_page(ready=False)
        with self.assertRaises(EngineError) as ctx:
            await self.client.ensure_bridge(timeout=0.5)
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertIn('MusicKit not loaded', ctx.exception.message)
        self.assertGreaterEqual(page.injections, 2)  # it kept trying

    async def test_ensure_bridge_names_the_page_s_error_and_keeps_to_its_time(self):
        page = await self.open_with_page()
        page.inject_error = 'SyntaxError: Unexpected token }\n    at <anonymous>:12:3'
        started = time.monotonic()
        with self.assertRaises(EngineError) as ctx:
            await self.client.ensure_bridge(timeout=0.5)
        elapsed = time.monotonic() - started
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertIn('SyntaxError: Unexpected token }', ctx.exception.message)
        self.assertLess(elapsed, 1.0)

    async def test_ensure_bridge_retries_a_failing_evaluate(self):
        page = FakePage()
        attempts = []

        def flaky(message):
            attempts.append(1)
            if len(attempts) == 1:
                raise CDPFailure(-32000, 'Cannot find context with specified id')
            return page(message)
        self.chrome.responders['Runtime.evaluate'] = flaky
        await self.open()
        await self.client.ensure_bridge(timeout=2)
        self.assertEqual(page.injections, 1)

    async def test_a_bridge_call_waits_for_the_bridge_to_come_back(self):
        page = await self.open_with_page()
        await self.client.subscribe()
        page.bridge = None
        page.ready = False  # MusicKit still loading in the new document
        await self.chrome.send_event(*context_created(9))
        await until(lambda: self.client._bridge_lock.locked())
        call = asyncio.create_task(self.client.bridge('status'))
        await asyncio.sleep(0.1)
        self.assertFalse(call.done())  # waiting, not failing with a TypeError
        page.ready = True
        self.assertEqual((await asyncio.wait_for(call, 3))['ready'], True)

    async def test_a_bridge_call_that_meets_a_missing_bridge_is_made_again(self):
        page = await self.open_with_page()
        await self.client.subscribe()
        page.bridge = None  # navigated; the re-injection has not begun
        self.assertEqual((await self.client.bridge('status'))['ready'], True)
        self.assertEqual(page.injections, 2)

    async def test_a_page_that_never_comes_back_loses_the_connection(self):
        page = await self.open_with_page()
        await self.client.subscribe()
        self.client.reinject_timeout = 0.1
        navigated = []
        self.chrome.responders['Page.navigate'] = lambda m: navigated.append(m) or {}
        page.bridge = None
        page.ready = False
        with self.assertLogs(client_module.log, 'WARNING'):
            await self.chrome.send_event(*context_created(9))
            await asyncio.wait_for(self.client.wait_closed(), 3)
        self.assertEqual(len(navigated), 1)  # sent to music.apple.com before the last try
        self.assertEqual(self.client.lost_reason,
                         'music.apple.com did not come back after a navigation')


class WebSocketClientTest(unittest.IsolatedAsyncioTestCase):
    """The same client through a DevTools WebSocket (scripts/am.py --attach)."""

    async def asyncSetUp(self):
        self.chrome = FakeChrome()
        self.ws_url = await self.chrome.start()
        self.client = CDPClient(timeout=3)

    async def asyncTearDown(self):
        await self.client.close()
        await self.chrome.close()

    async def open(self):
        await self.client.connect(WebSocketTransport(self.ws_url, timeout=3))
        await self.client.attach_page(1)

    async def test_attach_and_call(self):
        self.chrome.responders['Custom.test'] = lambda m: {'echo': m['params']}
        await self.open()
        self.assertEqual(self.chrome.methods(), PAGE_SETUP)
        self.assertEqual(await self.client.call('Custom.test', {'a': 1}), {'echo': {'a': 1}})

    async def test_close_sends_a_close_frame(self):
        await self.open()
        await self.client.close()
        self.assertFalse(self.client.connected)
        await until(lambda: any(opcode == 8 for opcode, _ in self.chrome.frames))
        self.assertEqual(self.chrome.frames[-1][1], struct.pack('!H', 1000))

    async def test_ping_gets_a_pong(self):
        await self.open()
        await self.chrome.send_raw(server_frame(9, b'heartbeat'))
        await until(lambda: any(opcode == 10 for opcode, _ in self.chrome.frames))
        self.assertIn((10, b'heartbeat'), self.chrome.frames)

    async def test_fragmented_and_large_messages(self):
        big = 'x' * 70000

        async def fragmented(message):
            data = json.dumps({'id': message['id'], 'sessionId': message['sessionId'],
                               'result': {'data': big}}).encode()
            await self.chrome.send_raw(server_frame(1, data[:100], fin=False))
            await self.chrome.send_raw(server_frame(0, data[100:], fin=True))
        self.chrome.responders['Big'] = fragmented
        await self.open()
        self.assertEqual((await self.client.call('Big'))['data'], big)

    async def test_hang_up_is_engine_down(self):
        await self.open()
        await self.chrome.drop()
        await asyncio.wait_for(self.client.wait_closed(), 2)
        with self.assertRaises(EngineError) as ctx:
            await self.client.call('Anything')
        self.assertEqual(ctx.exception.code, 'engine-down')

    async def test_connection_refused_is_engine_down(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            free = s.getsockname()[1]
        with self.assertRaises(EngineError) as ctx:
            await self.client.connect(WebSocketTransport(f'ws://127.0.0.1:{free}/x'))
        self.assertEqual(ctx.exception.code, 'engine-down')
        self.assertFalse(self.client.connected)

    async def test_not_a_websocket_address_is_engine_down(self):
        for url in ('http://127.0.0.1:1/x', 'ws://[bad/x'):
            with self.subTest(url=url):
                with self.assertRaises(EngineError) as ctx:
                    await CDPClient().connect(WebSocketTransport(url))
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
                await self.client.connect(WebSocketTransport(f'ws://127.0.0.1:{port}/ws'))
            self.assertEqual(ctx.exception.code, 'engine-down')
            self.assertIn('500', ctx.exception.message)
        finally:
            server.close()
            await server.wait_closed()

    async def test_attach_devtools(self):
        ws_url = self.ws_url

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps({'Browser': 'Fake/1', 'webSocketDebuggerUrl': ws_url}).encode()
                self.send_response(200 if self.path == '/json/version' else 404)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
        try:
            client = await attach_devtools(httpd.server_port, timeout=3, wait=1)
            try:
                self.assertTrue(client.attached)
                self.assertEqual(client._main_frame, 'main')
            finally:
                await client.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


class LoadBridgeTest(unittest.TestCase):
    def test_version_is_the_files_hash(self):
        source, version = load_bridge()
        self.assertEqual(len(version), 12)
        self.assertTrue(source.startswith(f'window.__appleMusicLibraryWanted = "{version}";\n'))
        self.assertIn('subscribe: function', source)
        self.assertEqual(client_module.BINDING, '__amEvent')


if __name__ == '__main__':
    unittest.main()
