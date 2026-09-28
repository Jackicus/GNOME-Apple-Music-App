"""The Engine: Chrome's lifecycle inside the app, its one CDP connection, and the commands the
UI awaits.

    engine = Engine(profile_dir, port, browser_command)   # made once, in Application.do_startup
    await engine.start()                # headless Chrome on music.apple.com, the bridge in it
    await engine.start(visible=True)    # a window instead (sign-in); restarts if it was headless
    await engine.restart(visible=False)
    await engine.stop()                 # SIGTERM, 5 s, SIGKILL; forgets engine.json
    await engine.status()               # {ready, engine, authorized, storefront, bitrate}
    await engine.api(path, params)      # one Apple Music API read (mk.api.music), retried
    await engine.api_pages(path, params, page=100)   # every item of a paged endpoint
    await engine.api_all(paths)         # several reads at once; a failed one is None
    await engine.item(kind, id)         # a full Item with its groups, kept under <cache>/items/
    await engine.signin()               # until MusicKit is authorized (event or 2 s polls)
    await engine.account_name()         # the name on the page, or '' (best effort)
    await engine.play(kind, id, start_with=None, shuffle=False)   # mk.setQueue + mk.play
    await engine.play_next(kind, id); await engine.play_later(kind, id)
    await engine.control('toggle')      # play, pause, toggle, next, previous, stop
    await engine.seek(seconds); await engine.volume(level)   # volume answers the level set
    await engine.shuffle('toggle'); await engine.repeat('cycle')   # answer {shuffle, repeat}
    await engine.now_playing()   # {state, track, position, duration, shuffle, repeat, volume}
    await engine.queue()         # {index, items: [Track…]}

Properties `state` ('down', 'starting', 'up', 'signing-in'), `authorized`, `headless`; the
`event(name, data)` signal re-emits the bridge's MusicKit events (name without the 'am:'
prefix). Every failure is an EngineError; nothing here blocks the loop: Chrome is a
Gio.Subprocess (awaitable wait_async), the connection is the asynchronous CDPClient, and the
JSON shaping and artwork HTTP of item() run in a thread. A Chrome that outlived an earlier
app process is reclaimed from engine.json when its mode matches. In demo mode (`demo=True`)
start() and stop() do nothing and every command raises EngineError('engine-down') at once.
"""

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

from gi.repository import Gio, GLib, GObject

from .backend import chrome, config, sync
from .backend.client import EVENT_PREFIX, connect_page
from .backend.errors import EngineError

log = logging.getLogger(__name__)

STATES = ('down', 'starting', 'up', 'signing-in')

DEVTOOLS_TIMEOUT = 20.0   # Chrome opening its port
BRIDGE_WAIT = 15.0        # a fresh page loading MusicKit
STOP_GRACE = 5.0          # after SIGTERM, before SIGKILL
SIGNIN_TIMEOUT = 600.0    # ten minutes to sign in
SIGNIN_POLL = 2.0         # isAuthorized is polled this often while signing in
API_RETRIES = 3
PAGE_CONCURRENCY = 3      # pages of one endpoint fetched at once, when its total is known
PLAY_TIMEOUT = 60.0       # setQueue fetches the queue's items from Apple before playing

# The playback commands' arguments, as the bridge (and the extension's am.py) take them.
CONTROL_ACTIONS = ('play', 'pause', 'toggle', 'next', 'previous', 'stop')
SHUFFLE_MODES = ('on', 'off', 'toggle')
REPEAT_MODES = ('none', 'one', 'all', 'cycle')

# Library ids ("l." albums and playlists, "p." playlists, "r." radio) live under /v1/me/library;
# anything else is the catalog's.
LIBRARY_PREFIXES = ('l.', 'p.', 'r.')

# Where the signed-in page shows the account: the sidebar's footer (the reference layout). The
# page is Apple's (Svelte, hashed class names) and changes; signed out, the footer holds
# `div.auth-content` with the `button.signin` "Sign In" (checked 2026-09-27), so that
# container is where the account goes. No selector for the signed-in state is known to be
# stable, so the candidates are tried in turn and the first with a short, non-empty text that
# is not a prompt or a menu label is taken; none gives '', never a guess.
ACCOUNT_NAME_JS = r'''(() => {
  const candidates = [
    '.account-menu .user__name', '.user__name',
    '[data-testid="user-menu-name"]', '[data-testid="account-name"]',
    '.auth-content [class*="name"]', '.auth-content [data-testid*="name"]',
    '.navigation__account-name', '.account-name', '.user-name',
    '.auth-content button span', '.auth-content span', '.auth-content button',
    'nav footer button[aria-haspopup] span:not([class*="icon"])',
  ];
  const bad = /^(sign in|log in|sign out|log out|open in music\b.*|account|menu|settings)$/i;
  for (const selector of candidates) {
    let elements;
    try { elements = document.querySelectorAll(selector); } catch { continue; }
    for (const element of elements) {
      const text = (element.textContent || '').trim().replace(/\s+/g, ' ');
      if (text && text.length <= 64 && !bad.test(text)) return text;
    }
  }
  return null;
})()'''


def engine_paths(profile, port_setting):
    """(Chrome profile directory, DevTools port) for a build profile and the engine-port
    setting. The development build uses `chrome-devel` beside the release build's `chrome`
    and the port after the setting's (9229 by default), so both can run at once; the
    APPLE_MUSIC_PROFILE and APPLE_MUSIC_PORT environment overrides win over both."""
    if os.environ.get('APPLE_MUSIC_PROFILE'):
        profile_dir = config.profile_dir()
    elif profile == 'development':
        profile_dir = config.default_profile_dir().with_name('chrome-devel')
    else:
        profile_dir = config.default_profile_dir()
    if os.environ.get('APPLE_MUSIC_PORT'):
        port = config.port()
    else:
        port = int(port_setting) + (1 if profile == 'development' else 0)
    return profile_dir, port


def is_library_id(item_id):
    return str(item_id).startswith(LIBRARY_PREFIXES)


def item_endpoint(kind, item_id, storefront):
    """The API path that answers with one full item of `kind`, as the extension's `item`
    command asked for it: library items under /v1/me/library, the rest under the catalog."""
    library = is_library_id(item_id)
    if kind == 'album':
        return (f'/v1/me/library/albums/{item_id}?include=tracks,artists' if library
                else f'/v1/catalog/{storefront}/albums/{item_id}?include=tracks,artists')
    if kind == 'playlist':
        return (f'/v1/me/library/playlists/{item_id}?include=tracks' if library
                else f'/v1/catalog/{storefront}/playlists/{item_id}?include=tracks')
    if kind == 'artist':
        return (f'/v1/me/library/artists/{item_id}?include=albums' if library
                else f'/v1/catalog/{storefront}/artists/{item_id}?include=albums')
    if kind == 'station':
        return f'/v1/catalog/{storefront}/stations/{item_id}'
    if kind == 'song':
        return (f'/v1/me/library/songs/{item_id}' if library
                else f'/v1/catalog/{storefront}/songs/{item_id}')
    return f'/v1/catalog/{storefront}/{kind}s/{item_id}'


def album_endpoint(album_id, storefront):
    library = str(album_id).startswith(('l.', 'p.'))
    return (f'/v1/me/library/albums/{album_id}?include=tracks' if library
            else f'/v1/catalog/{storefront}/albums/{album_id}?include=tracks')


def _shape_item(raw, cache_dir):
    """In a thread: the API's resource as an Item with groups, its artwork fetched, the answer
    kept under <cache>/items/."""
    sync.load_art_sizes(cache_dir)
    item = sync.normalize_item(raw, cache_dir, include_groups=True)
    return _keep_item(item, cache_dir)


def _shape_artist(raw, item_id, stubs, answers, cache_dir):
    """In a thread: an artist resource plus its albums' answers (apiAll's list, a failed one
    None in its place, the stub standing in) as an artist Item, one group per album."""
    sync.load_art_sizes(cache_dir)
    artist = {'id': item_id, 'type': raw.get('type', 'artists'),
              'attributes': raw.get('attributes') or {}}
    albums = []
    for position, stub in enumerate(stubs):
        answer = answers[position] if position < len(answers) else None
        data = answer.get('data') if isinstance(answer, dict) else None
        albums.append(data[0] if isinstance(data, list) and data else stub)
    item = sync.normalize_artist(artist, cache_dir, albums=albums)
    return _keep_item(item, cache_dir)


def _keep_item(item, cache_dir):
    sync.download_item_art(item, cache_dir)
    return sync.write_answer(sync.item_cache_path(cache_dir, item['kind'], item['id']), item)


def _page_data(answer):
    """The resources of one page: its `data` list's dicts."""
    data = answer.get('data') if isinstance(answer, dict) else None
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


class Engine(GObject.Object):
    """Chrome and the bridge, as one object the UI talks to. See the module."""

    __gtype_name__ = 'AppleMusicEngine'

    __gsignals__ = {
        'event': (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }

    state = GObject.Property(type=str, default='down')
    authorized = GObject.Property(type=bool, default=False)
    headless = GObject.Property(type=bool, default=True)

    def __init__(self, profile_dir=None, port=None, browser_command=None, demo=False):
        super().__init__()
        self.demo = demo
        self.profile_dir = Path(profile_dir) if profile_dir else config.profile_dir()
        self.port = int(port) if port else config.port()
        self.browser_command = browser_command
        self.state_file = config.state_file(self.profile_dir)
        self.stop_grace = STOP_GRACE
        self._client = None
        self._process = None   # the Gio.Subprocess, when this process started Chrome
        self._pid = None       # Chrome's pid, ours or reclaimed
        self._watch = None     # the task waiting for the connection to drop
        self._relay = None     # the task relaying Chrome's stderr to the log (DEBUG only)
        self._lock = asyncio.Lock()

    @property
    def pid(self):
        return self._pid

    @property
    def cache_dir(self):
        return config.cache_dir()

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self, visible=False):
        """Chrome up with the bridge in it, headless unless `visible`. A running Chrome in the
        other mode is stopped first; one in the same mode (this process's, or a live one
        engine.json describes) is kept. EngineError when Chrome or the page will not come up."""
        if self.demo:
            return
        headless = not visible
        async with self._lock:
            if self.state != 'down':
                if self.headless == headless:
                    return
                log.info('the engine is %s; restarting it %s',
                         'headless' if self.headless else 'visible',
                         'headless' if headless else 'visible')
                await self._stop()
            self.state = 'starting'
            self.headless = headless
            try:
                await self._start(headless)
            except BaseException:
                await self._stop()
                raise

    async def _start(self, headless):
        state = chrome.EngineState.load(self.state_file)
        if state is not None and state.alive:
            if state.headless == headless and state.port == self.port:
                log.info('reclaiming Chrome %d, %s on port %d', state.pid,
                         'headless' if headless else 'visible', self.port)
                self._pid = state.pid
            else:
                log.info('Chrome %d from an earlier run is %s on port %d; stopping it',
                         state.pid, 'headless' if state.headless else 'visible', state.port)
                await self._terminate(state.pid)
                chrome.EngineState.remove(self.state_file)
        elif state is not None:
            chrome.EngineState.remove(self.state_file)
        if self._pid is None:
            binary = chrome.find_chrome(self.browser_command)
            if binary is None:
                raise EngineError(
                    'engine-down', 'Google Chrome was not found (google-chrome-stable, '
                    'google-chrome or /opt/google/chrome/chrome; the browser-command setting '
                    'names another)')
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            argv = chrome.chrome_args(binary, self.profile_dir, self.port, headless)
            log.debug('exec %s', ' '.join(argv))
            self._process = self._spawn(argv)
            self._pid = int(self._process.get_identifier())
            chrome.EngineState(self._pid, self.port, headless, self.profile_dir).save(
                self.state_file)
            log.info('Chrome %d started %s on port %d', self._pid,
                     'headless' if headless else 'visible', self.port)
        await chrome.wait_for_devtools(self.port, timeout=DEVTOOLS_TIMEOUT)
        client = await self._connect()
        self._client = client
        client.on(EVENT_PREFIX + '*', self._on_bridge_event)
        await client.ensure_bridge(timeout=BRIDGE_WAIT)
        await client.subscribe()  # finds the bridge in place; events from now on
        status = await client.bridge('status')
        self.authorized = bool(isinstance(status, dict) and status.get('authorized'))
        self.state = 'up'
        self._watch = asyncio.create_task(self._watch_connection(client), name='engine-watch')
        log.info('engine up: %s', 'authorized' if self.authorized else 'not signed in')

    def _spawn(self, argv):
        """Chrome as a Gio.Subprocess: its output silenced, or its stderr relayed to the log
        when that is at DEBUG."""
        debug = log.isEnabledFor(logging.DEBUG)
        flags = Gio.SubprocessFlags.STDOUT_SILENCE | (
            Gio.SubprocessFlags.STDERR_PIPE if debug else Gio.SubprocessFlags.STDERR_SILENCE)
        try:
            process = Gio.Subprocess.new(argv, flags)
        except GLib.Error as e:
            raise EngineError('engine-down', f'could not start {argv[0]}: {e.message}') from e
        if debug:
            self._relay = asyncio.create_task(self._relay_stderr(process), name='chrome-stderr')
        return process

    async def _relay_stderr(self, process):
        """Chrome's stderr, a line at a time, to the log at DEBUG. Read to the end whatever
        happens: an undrained pipe would stall Chrome."""
        stream = process.get_stderr_pipe()
        pending = b''
        try:
            while True:
                data = await stream.read_bytes_async(4096, GLib.PRIORITY_LOW)
                chunk = data.get_data() if data is not None else b''
                if not chunk:
                    break
                pending += chunk
                *lines, pending = pending.split(b'\n')
                for line in lines:
                    log.debug('chrome: %s', line.decode('utf-8', 'replace').rstrip())
            if pending:
                log.debug('chrome: %s', pending.decode('utf-8', 'replace').rstrip())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug('chrome stderr relay ended: %s', e)
        finally:
            try:
                stream.close(None)
            except GLib.Error:
                pass

    async def _connect(self):
        return await connect_page(self.port, timeout=30.0, wait=BRIDGE_WAIT)

    async def _watch_connection(self, client):
        await client.wait_closed()
        if self._client is not client:
            return  # stop() closed it
        log.warning('the connection to Chrome was lost; the engine is down')
        async with self._lock:
            if self._client is client:
                await self._stop()

    async def stop(self):
        """End Chrome: the connection closed, SIGTERM, up to stop_grace seconds, then
        SIGKILL; engine.json forgotten. Nothing to do when it is down."""
        if self.demo:
            return
        async with self._lock:
            await self._stop()

    async def _stop(self):
        client, self._client = self._client, None
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        if client is not None:
            client.off(EVENT_PREFIX + '*', self._on_bridge_event)
            await client.close()
        pid, self._pid = self._pid, None
        process, self._process = self._process, None
        if pid is not None:
            await self._terminate(pid, process)
        relay, self._relay = self._relay, None
        if relay is not None and not relay.done():
            try:
                await asyncio.wait_for(relay, 1.0)  # Chrome's pipe closes as it exits
            except Exception:
                relay.cancel()
        chrome.EngineState.remove(self.state_file)
        self.authorized = False
        self.state = 'down'

    async def _terminate(self, pid, process=None):
        """SIGTERM `pid` (this process's `process`, or a reclaimed Chrome), wait, SIGKILL."""
        if not chrome.pid_alive(pid, self.profile_dir):
            return
        log.info('stopping Chrome %d', pid)
        self._signal(pid, process, signal.SIGTERM)
        if not await self._wait_exit(pid, process, self.stop_grace):
            log.warning('Chrome %d ignored SIGTERM for %g s; killing it', pid, self.stop_grace)
            self._signal(pid, process, signal.SIGKILL)
            await self._wait_exit(pid, process, self.stop_grace)

    @staticmethod
    def _signal(pid, process, signum):
        try:
            if process is not None:
                if signum == signal.SIGKILL:
                    process.force_exit()
                else:
                    process.send_signal(signum)
            else:
                os.kill(pid, signum)
        except (OSError, GLib.Error):
            pass

    async def _wait_exit(self, pid, process, timeout):
        """True once the process is gone, False after `timeout` seconds with it still there."""
        if process is not None:
            try:
                await asyncio.wait_for(process.wait_async(), timeout)
                return True
            except TimeoutError:
                return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while chrome.pid_alive(pid, self.profile_dir):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)
        return True

    def kill(self):
        """SIGKILL Chrome now, without waiting: the last resort when stop() ran out of time."""
        pid, self._pid = self._pid, None
        process, self._process = self._process, None
        if pid is not None and chrome.pid_alive(pid, self.profile_dir):
            log.warning('killing Chrome %d', pid)
            self._signal(pid, process, signal.SIGKILL)
        chrome.EngineState.remove(self.state_file)
        self.state = 'down'
        self.authorized = False

    async def restart(self, visible=False):
        await self.stop()
        await self.start(visible=visible)

    # -- events --------------------------------------------------------------------------

    def _on_bridge_event(self, name, data):
        name = name[len(EVENT_PREFIX):]
        if name == 'authorizationStatusDidChange' and isinstance(data, dict):
            authorized = bool(data.get('authorized'))
            if authorized != self.authorized:
                self.authorized = authorized
        self.emit('event', name, data)

    # -- commands ----------------------------------------------------------------------------

    def _require_up(self):
        if self.demo or self._client is None or self.state not in ('up', 'signing-in'):
            raise EngineError('engine-down', 'the engine is not running')
        return self._client

    async def status(self):
        """The bridge's status: {ready, engine, authorized, storefront, bitrate}."""
        client = self._require_up()
        status = await client.bridge('status')
        if not isinstance(status, dict):
            raise EngineError('api', 'the page gave no status')
        authorized = bool(status.get('authorized'))
        if authorized != self.authorized:
            self.authorized = authorized
        return status

    async def _api(self, client, path, params=None, timeout=None):
        """One API read through the bridge, retried: MusicKit answers a failed request with a
        200 and {"errors": [...]}, which is a failure here as much as a rejected promise."""
        last = None
        for attempt in range(API_RETRIES):
            if attempt:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            try:
                answer = await client.bridge('api', path, params or {}, timeout=timeout)
            except EngineError as e:
                if e.code == 'engine-down':
                    raise
                last = e
                continue
            if isinstance(answer, dict) and answer.get('errors'):
                errors = answer['errors']
                first = errors[0] if isinstance(errors, list) and errors else {}
                if not isinstance(first, dict):
                    first = {}
                last = EngineError('api', f"{first.get('status', '?')} "
                                          f"{first.get('title', 'error')}: "
                                          f"{first.get('detail', '')}".strip())
                continue
            return answer if isinstance(answer, dict) else {}
        raise EngineError(last.code if last else 'api', f'{path}: {last.message if last else "?"}')

    async def api(self, path, params=None, timeout=None):
        """One Apple Music API read through the page's MusicKit (the bridge's api(), that is
        mk.api.music(path, params)), retried a few times; the answer's body as a dict
        ({data: [...], meta, next…}). EngineError('api') when Apple says no, 'engine-down'
        when there is no engine."""
        client = self._require_up()
        return await self._api(client, path, params, timeout)

    async def api_all(self, paths, timeout=60):
        """Several reads at once in the page (the bridge's apiAll): one answer per path, in
        order, None where one failed."""
        client = self._require_up()
        answers = await client.bridge('apiAll', list(paths), timeout=timeout)
        return answers if isinstance(answers, list) else []

    async def api_pages(self, path, params=None, page=100, limit=None, progress=None,
                        concurrency=PAGE_CONCURRENCY):
        """Every item (`data`) of a paged endpoint, `page` at a time by offset, following
        `next` until there is none or `limit` items are in hand. An endpoint whose first
        answer carries `meta.total` has its remaining pages fetched `concurrency` at a time;
        the rest are followed one by one. `progress(done, total)` is called after each page
        (total None until it is known)."""
        params = dict(params or {})
        first = await self.api(path, dict(params, limit=page, offset=0))
        items = _page_data(first)
        meta = first.get('meta')
        total = meta.get('total') if isinstance(meta, dict) else None
        total = total if isinstance(total, int) and not isinstance(total, bool) else None
        if limit is not None:
            total = min(total, limit) if total is not None else None
        if progress:
            progress(len(items), total)
        if not items or not first.get('next') or (limit is not None and len(items) >= limit):
            return items[:limit] if limit is not None else items
        if total is not None and total > len(items):
            # Every remaining offset is known: a few pages at a time, kept in order.
            offsets = list(range(len(items), total, page))
            for start in range(0, len(offsets), max(1, concurrency)):
                batch = offsets[start:start + max(1, concurrency)]
                answers = await asyncio.gather(
                    *(self.api(path, dict(params, limit=page, offset=offset))
                      for offset in batch))
                for answer in answers:
                    items.extend(_page_data(answer))
                if progress:
                    progress(min(len(items), total), total)
                if any(not _page_data(answer) for answer in answers):
                    break  # Apple ran out early (the total counted something we do not get)
            return items[:limit] if limit is not None else items
        while True:
            answer = await self.api(path, dict(params, limit=page, offset=len(items)))
            data = _page_data(answer)
            items.extend(data)
            if progress:
                progress(len(items), total)
            if not data or not answer.get('next') or (limit is not None and len(items) >= limit):
                break
        return items[:limit] if limit is not None else items

    async def item(self, kind, item_id):
        """One full Item of `kind` with its `groups` (an album's discs, a playlist's list, an
        artist's albums), its artwork fetched, the answer kept at <cache>/items/. Needs a
        signed-in engine: EngineError('not-signed-in') otherwise."""
        client = self._require_up()
        if not self.authorized:
            raise EngineError('not-signed-in', 'sign in to load items')
        status = await self.status()
        storefront = str(status.get('storefront') or 'us')
        answer = await self._api(client, item_endpoint(kind, item_id, storefront))
        data = answer.get('data')
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise EngineError('api', f'item not found: {kind} {item_id}')
        raw = data[0]
        cache_dir = str(self.cache_dir)
        if kind == 'artist':
            relationships = raw.get('relationships') or {}
            stubs = ((relationships.get('albums') or {}).get('data') or [])
            stubs = [stub for stub in stubs if isinstance(stub, dict)]
            # All at once in the page, not one round trip per album: an artist with a
            # couple of dozen albums took seconds one by one.
            endpoints = [album_endpoint(stub.get('id'), storefront) for stub in stubs]
            answers = []
            if endpoints:
                try:
                    answers = await client.bridge('apiAll', endpoints, timeout=60)
                except EngineError as e:
                    if e.code == 'engine-down':
                        raise
                    log.warning('the albums of artist %s: %s', item_id, e)
            if not isinstance(answers, list):
                answers = []
            return await asyncio.to_thread(_shape_artist, raw, str(item_id), stubs, answers,
                                           cache_dir)
        return await asyncio.to_thread(_shape_item, raw, cache_dir)

    async def signin(self, timeout=SIGNIN_TIMEOUT):
        """Until MusicKit is authorized: the bridge asks the page to authorize (Apple's sign-in
        in the visible Chrome window), then the authorizationStatusDidChange event or a poll
        of the status every SIGNIN_POLL seconds says so. The one place the app polls.
        True when signed in; EngineError('timeout') after `timeout` seconds."""
        client = self._require_up()
        if self.authorized:
            return True
        self.state = 'signing-in'
        authorized = asyncio.Event()

        def on_change(_name, data):
            if isinstance(data, dict) and data.get('authorized'):
                authorized.set()

        client.on(EVENT_PREFIX + 'authorizationStatusDidChange', on_change)
        prompt = asyncio.create_task(self._authorize(client, timeout), name='engine-authorize')
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                try:
                    await asyncio.wait_for(authorized.wait(), SIGNIN_POLL)
                except TimeoutError:
                    pass
                if self._client is not client:
                    raise EngineError('engine-down', 'the engine stopped while signing in')
                try:
                    status = await client.bridge('status', timeout=5)
                except EngineError as e:
                    if e.code == 'engine-down':
                        raise
                    log.debug('sign-in poll: %s', e)  # the page is navigating, mostly
                    status = None
                if isinstance(status, dict) and status.get('authorized'):
                    self.authorized = True
                    log.info('signed in')
                    return True
                if loop.time() >= deadline:
                    raise EngineError('timeout', f'not signed in within {timeout:g} s')
        finally:
            client.off(EVENT_PREFIX + 'authorizationStatusDidChange', on_change)
            if not prompt.done():
                prompt.cancel()
            if self.state == 'signing-in':
                self.state = 'up' if self._client is client else 'down'

    async def _authorize(self, client, timeout):
        """The bridge's signin(): mk.authorize(), whose promise settles when the user has
        signed in or given up. Its outcome is only logged: the poll decides."""
        try:
            result = await client.bridge('signin', timeout=timeout)
            log.debug('authorize answered %r', result)
        except EngineError as e:
            log.debug('authorize: %s', e)

    async def account_name(self, wait=0):
        """The account's display name as the signed-in page shows it, or '' when no known
        element holds one. Never a guess. Apple's page renders the account menu a moment
        after authorization, so `wait` seconds of polling (every half second) covers the
        gap right after sign-in."""
        deadline = time.monotonic() + wait
        while True:
            client = self._require_up()
            try:
                name = await client.evaluate(ACCOUNT_NAME_JS, await_promise=False, timeout=5)
            except EngineError as e:
                if e.code == 'engine-down':
                    raise
                log.debug('account name: %s', e)
                name = None
            if isinstance(name, str):
                name = ' '.join(name.split())
                if 0 < len(name) <= 64:
                    return name
            if time.monotonic() >= deadline:
                return ''
            await asyncio.sleep(0.5)

    # -- playback ------------------------------------------------------------------------
    # Thin wrappers over the bridge, as am.py's play/control/seek/volume/shuffle/repeat/
    # now-playing/queue commands were; the outcome shows up as MusicKit events (the `event`
    # signal), which is where the Player takes its state from, not from these answers.

    def _require_signed_in(self):
        client = self._require_up()
        if not self.authorized:
            raise EngineError('not-signed-in', 'sign in to Apple Music to play')
        return client

    async def play(self, kind, item_id, start_with=None, shuffle=False):
        """Play an album, playlist, station, song, musicVideo or artist (its top songs) by
        id, from queue position `start_with` (a track row), shuffled when asked: the
        bridge's play(), that is mk.setQueue({kind: id, startWith, startPlaying}) and
        mk.play(). Needs a signed-in engine: library ids and full songs are the account's."""
        client = self._require_signed_in()
        if not kind or item_id in (None, ''):
            raise EngineError('usage', 'play needs a kind and an id')
        options = {'startWith': int(start_with or 0), 'shuffle': bool(shuffle)}
        await client.bridge('play', str(kind), str(item_id), options, timeout=PLAY_TIMEOUT)

    async def play_next(self, kind, item_id):
        """Queue an item right after the one playing (mk.playNext)."""
        client = self._require_signed_in()
        await client.bridge('playNext', str(kind), str(item_id), timeout=PLAY_TIMEOUT)

    async def play_later(self, kind, item_id):
        """Queue an item at the end (mk.playLater)."""
        client = self._require_signed_in()
        await client.bridge('playLater', str(kind), str(item_id), timeout=PLAY_TIMEOUT)

    async def control(self, action):
        """One of CONTROL_ACTIONS: play, pause, toggle, next, previous, stop."""
        if action not in CONTROL_ACTIONS:
            raise EngineError('usage', f'unknown control action: {action}')
        client = self._require_up()
        await client.bridge('control', action)

    async def seek(self, seconds):
        """Jump to `seconds` into the item playing (mk.seekToTime)."""
        client = self._require_up()
        await client.bridge('seek', max(0.0, float(seconds)))

    async def volume(self, level):
        """Set MusicKit's volume, 0 to 1 (the engine's own, not the system's; Apple's page
        keeps it across restarts). Answers the level as MusicKit has it after the set."""
        client = self._require_up()
        level = min(1.0, max(0.0, float(level)))
        answer = await client.bridge('volume', level)
        value = answer.get('volume') if isinstance(answer, dict) else None
        return float(value) if isinstance(value, (int, float)) else level

    async def shuffle(self, mode):
        """Shuffle on, off or toggle; answers {shuffle: 'on'|'off', repeat: 'none'|'one'|'all'}
        as MusicKit has them after the change."""
        if mode not in SHUFFLE_MODES:
            raise EngineError('usage', f'unknown shuffle mode: {mode}')
        client = self._require_up()
        answer = await client.bridge('shuffle', mode)
        return answer if isinstance(answer, dict) else {}

    async def repeat(self, mode):
        """Repeat none, one, all, or cycle through them; answers as shuffle() does."""
        if mode not in REPEAT_MODES:
            raise EngineError('usage', f'unknown repeat mode: {mode}')
        client = self._require_up()
        answer = await client.bridge('repeat', mode)
        return answer if isinstance(answer, dict) else {}

    async def now_playing(self):
        """What plays: {state, track, position, duration, shuffle, repeat, volume}, the state
        the coarse playing/paused/stopped and the track the Track shape (or None)."""
        client = self._require_up()
        answer = await client.bridge('nowPlaying')
        if not isinstance(answer, dict):
            raise EngineError('api', 'the page gave no now-playing answer')
        return answer

    async def queue(self):
        """The queue: {index, items: [Track…]}."""
        client = self._require_up()
        answer = await client.bridge('queue')
        return answer if isinstance(answer, dict) else {'index': 0, 'items': []}
