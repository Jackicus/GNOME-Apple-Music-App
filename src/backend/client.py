"""One asynchronous CDP connection to Chrome, attached to the music.apple.com page: calls,
events, the bridge.

    transport = PipeTransport(read_fd, write_fd)  # Chrome's --remote-debugging-pipe
    client = CDPClient()
    await client.connect(transport)           # Chrome's browser endpoint
    await client.attach_page()                # the music.apple.com page found (or opened) and
                                              # attached as a session: Runtime, Page and
                                              # Inspector enabled, the __amEvent binding added
    await client.call('Page.reload')          # any CDP method on the page, correlated by id
    await client.call('Browser.close', browser=True)   # or on the browser itself
    await client.evaluate('1 + 1')            # Runtime.evaluate, by value, promises awaited
    await client.ensure_bridge()              # bridge.js in the page (idempotent), kept there
                                              # across the page's own navigations
    await client.subscribe()                  # MusicKit events start arriving...
    client.on('am:playbackStateDidChange', lambda name, data: ...)   # ...here
    client.on('Page.frameNavigated', ...)     # raw CDP events too; 'am:*' and '*' are wildcards
    await client.close()

    client = await open_page(transport)       # connect() and attach_page(), closed on failure
    client = await attach_devtools(port)      # the same through a DevTools port's WebSocket
                                              # (scripts/am.py --attach)

The client always talks to the browser endpoint and reaches the page through the session
`Target.attachToTarget({flatten: true})` answers, so a pipe and a WebSocket behave alike. The
connection counts as lost (pending calls fail, `wait_closed()` returns) when the transport
closes, and also when the page crashes or goes away while Chrome runs on: the engine then goes
down and the next command starts a fresh Chrome.

Every failure is an EngineError: 'engine-down' for a lost or refused connection, 'timeout' when
Chrome does not answer in time, 'api' for a CDP error or a JS exception. Nothing here blocks:
the pipes and the socket are asyncio's; bridge.js and big answers are read and parsed in a
thread.
"""

import asyncio
import collections
import hashlib
import json
import logging
import math
import os

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
PAGE_WAIT = 15.0        # how long a new Chrome gets to show music.apple.com
BIG_MESSAGE = 512 * 1024  # answers at least this long are parsed in a thread
READ_CHUNK = 1 << 20

# What evaluate() answers for the values JSON cannot carry (Runtime's unserializableValue).
UNSERIALIZABLE = {'NaN': math.nan, 'Infinity': math.inf, '-Infinity': -math.inf, '-0': -0.0}


def load_bridge():
    """bridge.js as injected (its version set first) and the version: a prefix of the file's
    sha1, which the page keeps as __version so a changed bridge is injected over the old."""
    source = config.BRIDGE_JS.read_text(encoding='utf-8')
    version = hashlib.sha1(source.encode('utf-8')).hexdigest()[:12]
    return f'window.__appleMusicLibraryWanted = {json.dumps(version)};\n{source}', version


def unserializable(text):
    """A value Runtime.evaluate could only describe: NaN, ±Infinity, -0 or a BigInt ('5n')."""
    if text in UNSERIALIZABLE:
        return UNSERIALIZABLE[text]
    if isinstance(text, str) and text.endswith('n'):
        try:
            return int(text[:-1])
        except ValueError:
            pass
    return text


# -- transports ------------------------------------------------------------------------------
# A transport carries whole CDP messages (JSON as bytes): `await open()`, `await send(payload)`,
# `await receive()` (the next message, None once the other end has closed) and `close()`.


class _MessageReader(asyncio.Protocol):
    """The reading end of a pipe, split into NUL-terminated messages."""

    def __init__(self):
        self.messages = collections.deque()
        self.closed = False
        self._buffer = bytearray()
        self._waiter = None

    def data_received(self, data):
        start = len(self._buffer)
        self._buffer += data
        end = self._buffer.find(b'\0', start)  # only the new bytes can end a message
        while end >= 0:
            self.messages.append(bytes(self._buffer[:end]))
            del self._buffer[:end + 1]
            end = self._buffer.find(b'\0')
        self._wake()

    def eof_received(self):
        self.closed = True
        self._wake()

    def connection_lost(self, exc):
        self.closed = True
        self._wake()

    def _wake(self):
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    async def wait(self):
        self._waiter = asyncio.get_running_loop().create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None


class _MessageWriter(asyncio.Protocol):
    """The writing end of a pipe, with flow control: drain() waits while the pipe is full."""

    def __init__(self):
        self.lost = None
        self._paused = False
        self._waiters = []

    def pause_writing(self):
        self._paused = True

    def resume_writing(self):
        self._paused = False
        self._release()

    def connection_lost(self, exc):
        self.lost = exc or BrokenPipeError('the pipe to Chrome is closed')
        self._release()

    def _release(self):
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def drain(self):
        if self.lost is None and self._paused:
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter
        if self.lost is not None:
            raise ConnectionResetError(str(self.lost))


class PipeTransport:
    """Chrome's --remote-debugging-pipe: JSON messages, each ended by a NUL byte, read from
    `read_fd` (Chrome's fd 4) and written to `write_fd` (Chrome's fd 3). It owns both
    descriptors. asyncio's pipe transports, so it runs on any loop that watches file
    descriptors (the app's GLib-backed one, the tests' and the debug CLI's)."""

    def __init__(self, read_fd, write_fd):
        self._fds = [read_fd, write_fd]
        self._read = self._write = None
        self._reader = self._writer = None

    async def open(self):
        loop = asyncio.get_running_loop()
        read_fd, write_fd = self._fds
        self._fds = []
        read_file = os.fdopen(read_fd, 'rb', buffering=0)
        write_file = os.fdopen(write_fd, 'wb', buffering=0)
        try:
            self._read, self._reader = await loop.connect_read_pipe(_MessageReader, read_file)
            self._write, self._writer = await loop.connect_write_pipe(_MessageWriter,
                                                                      write_file)
        except (OSError, ValueError) as e:
            self.close()
            read_file.close()
            write_file.close()
            raise EngineError('engine-down', f'the pipe to Chrome could not be opened: {e}') from e

    async def send(self, payload):
        if self._write is None or self._write.is_closing():
            raise ConnectionResetError('the pipe to Chrome is closed')
        self._write.write(payload + b'\0')
        await self._writer.drain()

    async def receive(self):
        reader = self._reader
        if reader is None:
            return None
        while not reader.messages:
            if reader.closed:
                return None  # a message cut short by the end is dropped with it
            await reader.wait()
        return reader.messages.popleft()

    def close(self):
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []
        for transport in (self._write, self._read):
            if transport is not None:
                transport.close()


class WebSocketTransport:
    """CDP over a DevTools WebSocket: `ws_url` is the browser endpoint /json/version names. The
    app never uses it (the engine is a pipe); scripts/am.py --attach reaches an engine started
    with APPLE_MUSIC_DEBUG_PORT through it."""

    def __init__(self, ws_url, timeout=30.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self._reader = self._writer = None
        self._buffer = bytearray()
        self._fragments = []

    async def open(self):
        try:
            scheme, host, port, path, netloc = parse_ws_url(self.ws_url)
        except (CDPError, ValueError) as e:
            raise EngineError('engine-down', f'not a DevTools address: {self.ws_url}') from e
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
                'timeout', f'the WebSocket handshake with {self.ws_url} took too long') from e
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, CDPError) as e:
            writer.close()
            raise EngineError(
                'engine-down', f'WebSocket handshake with {self.ws_url} failed: {e}') from e
        self._reader, self._writer = reader, writer

    async def send(self, payload):
        if self._writer is None:
            raise ConnectionResetError('the WebSocket is closed')
        self._writer.write(encode_frame(1, payload))
        await self._writer.drain()

    async def receive(self):
        while self._reader is not None:
            frame = decode_frame(self._buffer)
            if frame is None:
                chunk = await self._reader.read(READ_CHUNK)
                if not chunk:
                    return None
                self._buffer.extend(chunk)
                continue
            opcode, fin, payload, consumed = frame
            del self._buffer[:consumed]
            if opcode == 9:  # ping: pong it, same payload
                self._writer.write(encode_frame(10, payload))
            elif opcode == 8:  # close
                return None
            elif opcode in (1, 2):
                if fin:
                    return payload
                self._fragments = [payload]
            elif opcode == 0:
                self._fragments.append(payload)
                if fin:
                    message, self._fragments = b''.join(self._fragments), []
                    return message
            elif opcode != 10:  # a pong needs nothing
                log.warning('CDP: unsupported WebSocket opcode %d', opcode)
        return None

    def close(self):
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                writer.write(close_frame())
            except (OSError, RuntimeError):
                pass
            writer.close()


# -- the client ------------------------------------------------------------------------------


class CDPClient:
    """The connection. One per engine; reconnect by making a new one."""

    def __init__(self, timeout=30.0):
        self.timeout = timeout
        self._transport = None
        self._reader_task = None
        self._next_id = 1
        self._pending = {}       # id -> Future for the response
        self._handlers = {}      # event name -> [callback]
        self._closed = True
        self._session = None     # the page's sessionId, once attached
        self._target = None      # the page's targetId
        self._targets_changed = asyncio.Event()
        self._bridge = None      # (source, version) once bridge.js has been read
        self._bridge_wanted = False
        self._bridge_lock = asyncio.Lock()
        self._subscribed = False
        self._main_frame = None
        self._tasks = set()

    @property
    def connected(self):
        return self._transport is not None and not self._closed

    @property
    def attached(self):
        return self.connected and self._session is not None

    # -- the connection --------------------------------------------------------------------

    async def connect(self, transport):
        """Open `transport` (Chrome's browser endpoint) and start reading from it."""
        if self._transport is not None or self._reader_task is not None:
            raise RuntimeError('this CDPClient is connected already; make a new one')
        try:
            await transport.open()
        except BaseException:
            transport.close()
            raise
        self._transport = transport
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_loop(), name='cdp-reader')

    async def attach_page(self, wait=PAGE_WAIT):
        """Find the music.apple.com page, attach to it as a session and prepare it for the
        bridge. Chrome shows the page a moment after it starts, so it is waited for up to
        `wait` seconds; then a page showing something else is sent to music.apple.com, and
        with no page at all one is opened. The connection is closed on failure."""
        try:
            target_id, navigate = await self._find_page(wait)
            answer = await self.call('Target.attachToTarget',
                                     {'targetId': target_id, 'flatten': True}, browser=True)
            session = answer.get('sessionId')
            if not isinstance(session, str) or not session:
                raise EngineError('engine-down', 'Chrome gave no session for the page')
            self._target, self._session = target_id, session
            await self._prepare()
            if navigate:
                await self.call('Page.navigate', {'url': chrome.START_URL})
        except BaseException:
            await self.close()
            raise

    async def _find_page(self, wait):
        """(targetId, whether it must be sent to music.apple.com)."""
        await self.call('Target.setDiscoverTargets', {'discover': True}, browser=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            self._targets_changed.clear()
            answer = await self.call('Target.getTargets', browser=True)
            targets = [t for t in answer.get('targetInfos') or () if isinstance(t, dict)]
            target = chrome.select_page(targets)
            if target is not None:
                return target['targetId'], False
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._targets_changed.wait(), min(0.25, remaining))
            except TimeoutError:
                pass
        pages = [t for t in targets if t.get('type') == 'page' and t.get('targetId')]
        if pages:
            log.info('Chrome shows no music.apple.com page; sending its page there')
            return pages[0]['targetId'], True
        log.info('Chrome shows no page; opening music.apple.com')
        answer = await self.call('Target.createTarget', {'url': chrome.START_URL}, browser=True)
        target_id = answer.get('targetId')
        if not isinstance(target_id, str) or not target_id:
            raise EngineError('engine-down', 'Chrome would not open music.apple.com')
        return target_id, False

    async def _prepare(self):
        """The domains and the binding the bridge needs; the main frame, whose new documents
        get the bridge again. Inspector reports a crash of the page."""
        await self.call('Runtime.enable')
        await self.call('Page.enable')
        await self.call('Inspector.enable')
        await self.call('Runtime.addBinding', {'name': BINDING})
        tree = await self.call('Page.getFrameTree')
        self._main_frame = ((tree.get('frameTree') or {}).get('frame') or {}).get('id')

    async def close(self):
        """Drop the connection (a pipe's close ends Chrome's end of it too). Pending calls fail
        with 'engine-down'."""
        transport, self._transport = self._transport, None
        self._closed = True
        reader = self._reader_task
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()
            await asyncio.wait([reader], timeout=1.0)
        if transport is not None:
            transport.close()
        self._fail_pending(EngineError('engine-down', 'the connection to Chrome was closed'))
        for task in list(self._tasks):
            if task is not asyncio.current_task():
                task.cancel()

    async def wait_closed(self):
        """Until the connection is gone: Chrome quit, the page went, or close() was called."""
        if self._reader_task is not None:
            await asyncio.wait([self._reader_task])

    def _fail_pending(self, error):
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    def _lost(self, reason):
        """The connection went away under us: Chrome quit or was killed, or the page crashed
        or closed. Pending calls fail and the reader ends."""
        if self._transport is None:
            return
        self._closed = True
        transport, self._transport = self._transport, None
        transport.close()
        log.info('CDP connection lost: %s', reason)
        self._fail_pending(
            EngineError('engine-down', f'the connection to Chrome was lost: {reason}'))
        reader = self._reader_task
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()

    async def _read_loop(self):
        reason = 'closed by Chrome'
        try:
            while not self._closed:
                payload = await self._transport.receive()
                if payload is None:
                    break
                try:
                    if len(payload) >= BIG_MESSAGE:  # a big answer: the loop keeps drawing
                        message = await asyncio.to_thread(json.loads, payload)
                    else:
                        message = json.loads(payload)
                except ValueError:
                    log.warning('CDP: undecodable message of %d bytes', len(payload))
                    continue
                try:
                    self._dispatch(message)
                except Exception:
                    log.exception('CDP: a message could not be handled')
        except asyncio.CancelledError:
            raise
        except (OSError, EngineError) as e:
            reason = str(e) or type(e).__name__
        except Exception:
            log.exception('CDP reader failed')
            reason = 'reader error'
        finally:
            self._lost(reason)

    def _dispatch(self, message):
        if not isinstance(message, dict):
            return
        if 'id' in message:
            future = self._pending.pop(message['id'], None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        method = message.get('method')
        if not isinstance(method, str):
            return
        params = message.get('params')
        params = params if isinstance(params, dict) else {}
        session = message.get('sessionId')
        if session is None:
            self._on_browser_event(method, params)
        elif session == self._session:
            self._on_event(method, params)

    async def _send(self, message):
        try:
            await self._transport.send(json.dumps(message).encode('utf-8'))
        except (OSError, ConnectionError, RuntimeError) as e:
            self._lost(str(e) or type(e).__name__)
            raise EngineError(
                'engine-down', f'sending {message["method"]} to Chrome failed: {e}') from e

    # -- calls -----------------------------------------------------------------------------

    async def call(self, method, params=None, timeout=None, browser=False):
        """One CDP command on the page (on the browser with `browser`); its `result` dict.
        EngineError('api') for a CDP error."""
        if not self.connected:
            raise EngineError('engine-down', 'not connected to Chrome')
        message = {'id': self._next_id, 'method': method, 'params': params or {}}
        if not browser:
            if self._session is None:
                raise EngineError('engine-down', 'no page is attached')
            message['sessionId'] = self._session
        self._next_id += 1
        timeout = self.timeout if timeout is None else timeout
        future = asyncio.get_running_loop().create_future()
        self._pending[message['id']] = future
        try:
            await self._send(message)
        except EngineError:
            self._pending.pop(message['id'], None)
            raise
        try:
            response = await asyncio.wait_for(future, timeout)
        except TimeoutError as e:
            self._pending.pop(message['id'], None)
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
        if not isinstance(value, dict):
            return None
        if 'unserializableValue' in value:
            return unserializable(value['unserializableValue'])
        return value.get('value')

    async def bridge(self, method, *args, timeout=None):
        """`window.__appleMusicLibrary.<method>(*args)`, the arguments as JSON. A call made
        while the bridge is being put back after a navigation waits for it; one that finds
        the bridge gone (a TypeError from the page before the re-injection began) is made
        again once it is back."""
        expression = f'{GLOBAL}.{method}({", ".join(json.dumps(arg) for arg in args)})'
        if self._bridge_lock.locked():
            async with self._bridge_lock:
                pass
        try:
            return await self.evaluate(expression, timeout=timeout)
        except EngineError as e:
            if not (e.code == 'api' and self._bridge_wanted and e.message.startswith('TypeError')
                    and await self._bridge_missing()):
                raise
            log.debug('bridge.%s: the bridge is gone (a navigation); again once it is back',
                      method)
        await self.ensure_bridge()
        return await self.evaluate(expression, timeout=timeout)

    async def _bridge_missing(self):
        try:
            return await self.evaluate(f'typeof {GLOBAL} === "undefined"', await_promise=False,
                                       timeout=5) is True
        except EngineError:
            return False

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

    def _on_browser_event(self, method, params):
        """An event of the browser's own (no session): targets appearing, and the end of
        the attached page."""
        if method in ('Target.targetCreated', 'Target.targetInfoChanged'):
            self._targets_changed.set()
        elif self._target is not None:
            if method == 'Target.targetCrashed' and params.get('targetId') == self._target:
                self._lost('the page crashed')
            elif (method == 'Target.detachedFromTarget'
                  and params.get('sessionId') == self._session):
                self._lost('the page was detached')
            elif method == 'Target.targetDestroyed' and params.get('targetId') == self._target:
                self._lost('the page was closed')
        self._emit(method, params)

    def _on_event(self, method, params):
        """An event of the attached page."""
        if method == 'Inspector.targetCrashed':
            self._lost('the page crashed')
        elif method == 'Inspector.detached':
            self._lost(f'the page was detached ({params.get("reason") or "no reason"})')
        elif method == 'Runtime.bindingCalled' and params.get('name') == BINDING:
            try:
                event = json.loads(params.get('payload') or 'null')
            except (ValueError, TypeError):
                event = None
            if not isinstance(event, dict) or not isinstance(event.get('name'), str):
                log.warning('bridge: unreadable event %.200r', params.get('payload'))
                return
            self._emit(EVENT_PREFIX + event['name'], event.get('data'))
            return
        elif method == 'Runtime.executionContextCreated':
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
        EngineError('timeout') when MusicKit does not come up in `timeout` seconds, with the
        last error the page gave (a SyntaxError in the bridge, say)."""
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
        # ready (injection is idempotent). Each evaluate gets what is left of `timeout`.
        probe = (f'({GLOBAL} && {GLOBAL}.__version === {json.dumps(version)})'
                 f' ? {GLOBAL}.status() : null')
        status = f'{GLOBAL} ? {GLOBAL}.status() : null'
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last = None
        while True:
            try:
                step = max(0.5, min(5.0, deadline - loop.time()))
                state = await self.evaluate(probe, await_promise=False, timeout=step)
                if not (state and state.get('ready')):
                    await self.evaluate(source, await_promise=False, timeout=step)
                    step = max(0.5, min(5.0, deadline - loop.time()))
                    state = await self.evaluate(status, await_promise=False, timeout=step)
                if state and state.get('ready'):
                    break
                last = None  # the page answers; MusicKit is still loading
            except EngineError as e:
                if e.code == 'engine-down':
                    raise
                last = e
                log.debug('bridge not ready: %s', e)  # a navigation in progress, mostly
            remaining = deadline - loop.time()
            if remaining <= 0:
                why = last.message if last is not None else 'MusicKit not loaded'
                raise EngineError('timeout', f'music.apple.com did not become ready: {why}')
            await asyncio.sleep(min(0.3, remaining))
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


async def open_page(transport, timeout=30.0, wait=PAGE_WAIT):
    """A CDPClient on `transport`, attached to the music.apple.com page."""
    client = CDPClient(timeout=timeout)
    await client.connect(transport)
    await client.attach_page(wait)
    return client


async def attach_devtools(port, timeout=30.0, wait=PAGE_WAIT):
    """A CDPClient on the page of the Chrome whose DevTools listen on 127.0.0.1:`port` (an
    engine started with APPLE_MUSIC_DEBUG_PORT), through its browser WebSocket."""
    version = await chrome.get_json(port, '/json/version', timeout=min(timeout, 5.0))
    ws_url = version.get('webSocketDebuggerUrl') if isinstance(version, dict) else None
    if not isinstance(ws_url, str) or not ws_url:
        raise EngineError('engine-down', f'DevTools on port {port} named no browser endpoint')
    return await open_page(WebSocketTransport(ws_url, timeout), timeout, wait)
