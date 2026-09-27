"""One asynchronous CDP connection to the music.apple.com page: calls, events, the bridge.

    client = CDPClient()
    await client.connect(ws_url)              # handshake; Runtime and Page enabled, the
                                              # __amEvent binding registered
    await client.call('Page.reload')          # any CDP method, correlated by id
    await client.evaluate('1 + 1')            # Runtime.evaluate, by value, promises awaited
    await client.ensure_bridge()              # bridge.js in the page (idempotent), kept there
                                              # across the page's own navigations
    await client.subscribe()                  # MusicKit events start arriving...
    client.on('am:playbackStateDidChange', lambda name, data: ...)   # ...here
    client.on('Page.frameNavigated', ...)     # raw CDP events too; 'am:*' and '*' are wildcards
    await client.close()

Every failure is an EngineError: 'engine-down' for a lost or refused connection, 'timeout' when
Chrome does not answer in time, 'api' for a CDP error or a JS exception. Nothing here blocks:
the socket is asyncio's, bridge.js is read in a thread, and the only threads are those.
"""

import asyncio
import hashlib
import json
import logging

from . import chrome, config
from .cdp import (
    CDPError,
    check_handshake,
    close_frame,
    decode_frame,
    encode_frame,
    exception_message,
    handshake_request,
    parse_ws_url,
)
from .errors import EngineError

log = logging.getLogger(__name__)

BINDING = '__amEvent'   # the page calls window.__amEvent(json) to post a bridge event
EVENT_PREFIX = 'am:'    # bridge events are dispatched as 'am:<MusicKit event name>'
GLOBAL = 'window.__appleMusicLibrary'
BRIDGE_TIMEOUT = 15.0   # how long the page gets to load MusicKit


def load_bridge():
    """bridge.js as injected (its version set first) and the version: a prefix of the file's
    sha1, which the page keeps as __version so a changed bridge is injected over the old."""
    source = config.BRIDGE_JS.read_text(encoding='utf-8')
    version = hashlib.sha1(source.encode('utf-8')).hexdigest()[:12]
    return f'window.__appleMusicLibraryWanted = {json.dumps(version)};\n{source}', version


class CDPClient:
    """The connection. One per engine; reconnect by making a new one."""

    def __init__(self, timeout=30.0):
        self.timeout = timeout
        self._reader = None
        self._writer = None
        self._reader_task = None
        self._next_id = 1
        self._pending = {}       # id -> Future for the response
        self._handlers = {}      # event name -> [callback]
        self._buffer = bytearray()
        self._fragments = []
        self._closed = True
        self._bridge = None      # (source, version) once bridge.js has been read
        self._bridge_wanted = False
        self._bridge_lock = asyncio.Lock()
        self._subscribed = False
        self._main_frame = None
        self._tasks = set()

    @property
    def connected(self):
        return self._writer is not None and not self._closed

    # -- the connection --------------------------------------------------------------------

    async def connect(self, ws_url, page=True):
        """Open `ws_url` (a target's webSocketDebuggerUrl), then, for a page, enable Runtime
        and Page and register the event binding, so the client is ready for the bridge. With
        page=False (the browser target from /json/version) only the socket is opened."""
        scheme, host, port, path, netloc = parse_ws_url(ws_url)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=True if scheme == 'wss' else None),
                self.timeout)
        except TimeoutError as e:
            raise EngineError('timeout', f'connecting to {host}:{port} took too long') from e
        except OSError as e:
            raise EngineError('engine-down', f'could not connect to {host}:{port}: {e}') from e

        request, key = handshake_request(path, netloc or f'{host}:{port}')
        try:
            writer.write(request)
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), self.timeout)
            check_handshake(head[:-4], key)
        except TimeoutError as e:
            writer.close()
            raise EngineError(
                'timeout', f'the WebSocket handshake with {ws_url} took too long') from e
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, CDPError) as e:
            writer.close()
            raise EngineError(
                'engine-down', f'WebSocket handshake with {ws_url} failed: {e}') from e

        self._reader, self._writer = reader, writer
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_loop(), name='cdp-reader')
        if page:
            await self._prepare()

    async def _prepare(self):
        """The domains and the binding the bridge needs; the main frame, whose new documents
        get the bridge again."""
        await self.call('Runtime.enable')
        await self.call('Page.enable')
        await self.call('Runtime.addBinding', {'name': BINDING})
        tree = await self.call('Page.getFrameTree')
        self._main_frame = ((tree.get('frameTree') or {}).get('frame') or {}).get('id')

    async def close(self):
        """Send a Close frame and drop the connection. Pending calls fail with 'engine-down'."""
        writer = self._writer
        if writer is None:
            return
        self._closed = True
        self._writer = None
        self._reader = None
        if self._reader_task and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
        try:
            writer.write(close_frame())
            await asyncio.wait_for(writer.drain(), 1.0)
        except Exception:
            pass
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1.0)
        except Exception:
            pass
        self._fail_pending(EngineError('engine-down', 'the connection to Chrome was closed'))
        for task in list(self._tasks):
            task.cancel()

    async def wait_closed(self):
        """Until the connection is gone: Chrome quit, or close() was called."""
        if self._reader_task is not None:
            await asyncio.wait([self._reader_task])

    def _fail_pending(self, error):
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    def _lost(self, reason):
        """The socket went away under us (Chrome quit, or was killed)."""
        if self._closed and self._writer is None:
            return
        self._closed = True
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
        log.info('CDP connection lost: %s', reason)
        self._fail_pending(
            EngineError('engine-down', f'the connection to Chrome was lost: {reason}'))

    async def _read_loop(self):
        reason = 'closed by Chrome'
        try:
            while not self._closed:
                chunk = await self._reader.read(65536)
                if not chunk:
                    break
                self._buffer.extend(chunk)
                while True:
                    frame = decode_frame(self._buffer)
                    if frame is None:
                        break
                    opcode, fin, payload, consumed = frame
                    del self._buffer[:consumed]
                    if not self._on_frame(opcode, fin, payload):
                        return
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.IncompleteReadError) as e:
            reason = str(e) or type(e).__name__
        except Exception:
            log.exception('CDP reader failed')
            reason = 'reader error'
        finally:
            self._lost(reason)

    def _on_frame(self, opcode, fin, payload):
        """One frame in; False when it was a Close."""
        if opcode == 9:   # ping: pong it, same payload
            self._send(encode_frame(10, payload))
        elif opcode == 10:  # pong
            pass
        elif opcode == 8:   # close
            return False
        elif opcode in (1, 2):
            if fin:
                self._dispatch(payload)
            else:
                self._fragments = [payload]
        elif opcode == 0:
            self._fragments.append(payload)
            if fin:
                message, self._fragments = b''.join(self._fragments), []
                self._dispatch(message)
        else:
            log.warning('CDP: unsupported WebSocket opcode %d', opcode)
        return True

    def _send(self, frame):
        if self._writer is not None:
            try:
                self._writer.write(frame)
            except OSError as e:
                self._lost(str(e))

    def _dispatch(self, payload):
        try:
            message = json.loads(payload.decode('utf-8'))
        except ValueError:
            log.warning('CDP: undecodable message of %d bytes', len(payload))
            return
        if not isinstance(message, dict):
            return
        if 'id' in message:
            future = self._pending.pop(message['id'], None)
            if future is not None and not future.done():
                future.set_result(message)
        elif 'method' in message:
            self._on_event(message['method'], message.get('params') or {})

    # -- calls -----------------------------------------------------------------------------

    async def call(self, method, params=None, timeout=None):
        """One CDP command; its `result` dict. EngineError('api') for a CDP error."""
        if not self.connected:
            raise EngineError('engine-down', 'not connected to Chrome')
        timeout = self.timeout if timeout is None else timeout
        msg_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        payload = json.dumps({'id': msg_id, 'method': method, 'params': params or {}})
        try:
            self._writer.write(encode_frame(1, payload.encode('utf-8')))
            await self._writer.drain()
        except (OSError, ConnectionError) as e:
            self._pending.pop(msg_id, None)
            self._lost(str(e))
            raise EngineError('engine-down', f'sending {method} to Chrome failed: {e}') from e
        try:
            response = await asyncio.wait_for(future, timeout)
        except TimeoutError as e:
            self._pending.pop(msg_id, None)
            raise EngineError('timeout', f'{method} took longer than {timeout:g} s') from e
        if 'error' in response:
            error = response['error']
            if isinstance(error, dict):
                text = error.get('message', 'CDP error')
                if error.get('data'):
                    text = f'{text} ({error["data"]})'
            else:
                text = str(error)
            raise EngineError('api', f'{method}: {text}')
        result = response.get('result')
        return result if isinstance(result, dict) else {}

    async def evaluate(self, js, await_promise=True, timeout=None):
        """`js` in the page, its value by value (a promise's settled value when await_promise).
        A thrown exception, or a rejected promise, is EngineError('api') with its message."""
        result = await self.call('Runtime.evaluate', {
            'expression': js,
            'awaitPromise': await_promise,
            'returnByValue': True,
        }, timeout)
        if 'exceptionDetails' in result:
            raise EngineError('api', exception_message(result['exceptionDetails']))
        value = result.get('result')
        return value.get('value') if isinstance(value, dict) else None

    async def bridge(self, method, *args, timeout=None):
        """`window.__appleMusicLibrary.<method>(*args)`, the arguments as JSON."""
        call = ', '.join(json.dumps(arg) for arg in args)
        return await self.evaluate(f'{GLOBAL}.{method}({call})', timeout=timeout)

    # -- events ----------------------------------------------------------------------------

    def on(self, event, callback):
        """Call `callback(name, data)` for each `event`: a CDP method ('Page.frameNavigated',
        data its params), a bridge event ('am:playbackStateDidChange', data the bridge's
        payload), or the wildcards 'am:*' and '*'. A coroutine returned runs as a task."""
        self._handlers.setdefault(event, []).append(callback)
        return callback

    def off(self, event, callback):
        handlers = self._handlers.get(event)
        if handlers and callback in handlers:
            handlers.remove(callback)

    def _on_event(self, method, params):
        if method == 'Runtime.bindingCalled' and params.get('name') == BINDING:
            try:
                event = json.loads(params.get('payload') or 'null')
                name, data = event['name'], event.get('data')
            except (ValueError, TypeError, KeyError):
                log.warning('bridge: unreadable event %r', params.get('payload'))
                return
            self._emit(EVENT_PREFIX + name, data)
            return
        if method == 'Runtime.executionContextCreated':
            aux = (params.get('context') or {}).get('auxData') or {}
            if (aux.get('isDefault') and aux.get('frameId') == self._main_frame
                    and self._bridge_wanted):
                # The page navigated (music.apple.com does on its own while it starts), which
                # wiped window and the bridge with it.
                self._spawn(self._reinject())
        self._emit(method, params)

    def _emit(self, name, data):
        callbacks = list(self._handlers.get(name, ()))
        if name.startswith(EVENT_PREFIX):
            callbacks += self._handlers.get(EVENT_PREFIX + '*', ())
        callbacks += self._handlers.get('*', ())
        for callback in callbacks:
            try:
                result = callback(name, data)
            except Exception:
                log.exception('event handler for %s failed', name)
                continue
            if asyncio.iscoroutine(result):
                self._spawn(result)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error('CDP task failed', exc_info=task.exception())

    # -- the bridge ------------------------------------------------------------------------

    async def ensure_bridge(self, timeout=BRIDGE_TIMEOUT):
        """bridge.js in the page and MusicKit ready, injecting when the page has no bridge or
        an older one. From now on the bridge is put back after every navigation of the page.
        EngineError('timeout') when MusicKit does not come up in `timeout` seconds."""
        self._bridge_wanted = True
        async with self._bridge_lock:
            await self._inject(timeout)

    async def _inject(self, timeout):
        if self._bridge is None:
            self._bridge = await asyncio.to_thread(load_bridge)
        source, version = self._bridge
        # A page that already has this bridge answers the probe alone, which spares it the
        # source; one that has none, or an older one, gets it. The page navigates on its own
        # while it starts, which wipes window, so keep going until the bridge reports MusicKit
        # ready (injection is idempotent).
        probe = (f'({GLOBAL} && {GLOBAL}.__version === {json.dumps(version)})'
                 f' ? {GLOBAL}.status() : null')
        status = f'{GLOBAL} ? {GLOBAL}.status() : null'
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                state = await self.evaluate(probe, await_promise=False, timeout=5)
                if not (state and state.get('ready')):
                    await self.evaluate(source, await_promise=False, timeout=5)
                    state = await self.evaluate(status, await_promise=False, timeout=5)
                if state and state.get('ready'):
                    break
            except EngineError as e:
                if e.code == 'engine-down':
                    raise
                log.debug('bridge not ready: %s', e)  # a navigation in progress, mostly
            if loop.time() >= deadline:
                raise EngineError(
                    'timeout', 'music.apple.com did not become ready (MusicKit not loaded)')
            await asyncio.sleep(0.3)
        if self._subscribed:
            await self.evaluate(f'{GLOBAL}.subscribe()', await_promise=False, timeout=5)

    async def _reinject(self):
        log.debug('bridge: a new document in the page; putting the bridge back')
        try:
            await self.ensure_bridge()
        except EngineError as e:
            if e.code != 'engine-down':
                log.warning('bridge after navigation: %s', e)

    async def subscribe(self):
        """Have the bridge forward MusicKit's events (the am:* events), now and after every
        navigation. Ensures the bridge first."""
        self._subscribed = True
        await self.ensure_bridge()

    async def unsubscribe(self):
        self._subscribed = False
        if self.connected:
            await self.evaluate(f'{GLOBAL} ? {GLOBAL}.unsubscribe() : null', await_promise=False)


async def connect_page(port, timeout=30.0, wait=BRIDGE_TIMEOUT):
    """A CDPClient connected to the music.apple.com page of the Chrome on `port`."""
    target = await chrome.wait_for_target(port, timeout=wait)
    client = CDPClient(timeout=timeout)
    await client.connect(target['webSocketDebuggerUrl'])
    return client
