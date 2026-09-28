"""The Engine: Chrome's lifecycle inside the app, its one CDP connection, and the commands the
UI awaits.

    engine = Engine(profile_dir, browser_command)   # made once, in Application.do_startup
    await engine.start()                # Chrome on music.apple.com, the bridge in it: headless
                                        # (or a window when prefer_headless is off); a running
                                        # engine is kept in whichever mode it runs
    await engine.start(visible=True)    # a window instead (sign-in); restarts if it was headless
    await engine.restart(visible=False)
    await engine.stop()                 # Browser.close, then SIGTERM, 5 s, SIGKILL
    await engine.stop(grace=1)          # a shorter wait after SIGTERM (quitting)
    await engine.status()               # {ready, engine, authorized, storefront, bitrate}
    await engine.api(path, params)      # one Apple Music API read (mk.api.music), retried
    await engine.api_pages(path, params, page=100)   # every item of a paged endpoint
                                        # (unique=True: each id once, for a listing)
    await engine.api_all(paths)         # several reads at once; a failed one is None
    await engine.item(kind, id)         # a full Item with all its groups, its artwork fetched
                                        # (into <cache>/remote-art/ unless the library has it)
    await engine.signin()               # until MusicKit is authorized (event or 2 s polls)
    await engine.unauthorize()          # revoke the session at Apple's (sign-out); True if done
    await engine.account_name()         # the name on the page, or '' (best effort)
    await engine.play(kind, id, start_with=None, shuffle=False)   # mk.setQueue + mk.play
    await engine.play_next(kind, id); await engine.play_later(kind, id)
    await engine.control('toggle')      # play, pause, toggle, next, previous, stop
    await engine.seek(seconds); await engine.volume(level)   # volume answers the level set
    await engine.shuffle('toggle'); await engine.repeat('cycle')   # answer {shuffle, repeat}
    await engine.now_playing()   # {state, track, position, duration, shuffle, repeat, volume}
    await engine.queue()         # {index, items: [Track…]}
    await engine.queue_jump(3)   # play the queue's entry at index 3 (mk.changeToMediaAtIndex)
    await engine.lyrics(catalog_song_id)   # {synced, lines: [{startMs, endMs, text}]},
                                           # from <cache>/lyrics/ when fetched this month
    await engine.love('song', id); await engine.unlove('album', id)   # the rating, set or gone
    await engine.rating('song', id)        # 1 loved, -1 disliked, 0 neither
    await engine.add_to_library('album', catalog_id)
    await engine.catalog_url('album', library_id)   # its music.apple.com page, or None
    await engine.add_to_playlist(playlist_id, song_id)
    await engine.search(term, limit=20)    # {shelves}: a catalog search
    await engine.suggest(term, limit=10)   # {terms: [{term, display}], items}
    await engine.landing()       # {categories}: the search page's Browse Categories
    await engine.category(id)    # {id, title, shelves}: a category's page
    await engine.browse()        # {shelves}: the New page (the editorial groupings)
    await engine.made_for_you()  # {shelves}: the personal mixes and stations
    # The last four are kept under the cache for a day (landing.json, categories/, browse.json,
    # made-for-you.json) and answered from there without the engine; refresh=True asks again.
    # Each answer carries `cached`, when it was fetched; one older than a day, answered when
    # Apple cannot be asked, carries `stale: True` too.

Properties `state` ('down', 'starting', 'up', 'signing-in'), `authorized`, `headless`; the
`event(name, data)` signal re-emits the bridge's MusicKit events (name without the 'am:'
prefix), and `rated(kind, id, value)` follows love(), unlove() and rating() (the kind and id
as they were asked, value 1 loved, 0 not), so a heart shows what a menu did, and
`lost(reason)` says the engine went down on its own (Chrome crashed or was killed, the page
crashed, closed or stopped answering), never for a stop, a restart or quitting. Every failure
is an EngineError; nothing here blocks the loop: Chrome is a Gio.Subprocess (awaitable
wait_async), the connection is the asynchronous CDPClient over Chrome's DevTools pipe (its
descriptors 3 and 4: no port is open), and the JSON shaping and artwork HTTP of item() run in
a thread. In demo mode (`demo=True`) start() and stop() do nothing and every command raises
EngineError('engine-down') at once.

The browser command (`browser_command`) and the preferred mode (`prefer_headless`) are read
when Chrome is spawned, so a change applies at the next start and a running Chrome is left
alone. APPLE_MUSIC_DEBUG_PORT, when set, also opens DevTools on that port of 127.0.0.1 for a
developer's debug CLI to attach to, with a warning at every start. What the cache holds and
how it is cleared is src/cache.py's.
"""

import asyncio
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path

from gi.repository import Gio, GLib, GObject

from . import cache
from .backend import api, chrome, config, normalize, store
from .backend.api import is_library_id, resource_type
from .backend.client import EVENT_PREFIX, CDPClient, PipeTransport
from .backend.errors import EngineError

log = logging.getLogger(__name__)

PAGE_WAIT = 20.0          # a new Chrome showing music.apple.com
BRIDGE_WAIT = 15.0        # a fresh page loading MusicKit
EXIT_GRACE = 1.0          # Chrome's pipe closing to its exit being seen, when it fails
CDP_TIMEOUT = 30.0        # a CDP call without a timeout of its own
PROBE_TIMEOUT = 5.0       # after a call timed out: a page silent this long is wedged
CLOSE_WAIT = 2.0          # Chrome closing on Browser.close, before SIGTERM
STOP_GRACE = 5.0          # after SIGTERM, before SIGKILL
SIGNIN_TIMEOUT = 600.0    # ten minutes to sign in
SIGNIN_POLL = 2.0         # isAuthorized is polled this often while signing in
API_RETRIES = 3           # tries of an API read that may pass another time (api.is_final)
API_RETRY_DELAY = 0.5     # before the first retry of a failed API read; doubles after
READ_TIMEOUT = 15.0       # one read a person waits on: an item's page, a rating, a link
RELATIONSHIP_PAGES = 50   # pages of an item's relationship followed past the first, at most
ALBUM_BATCH = 25          # an artist's albums asked of the page at once
PAGE_CONCURRENCY = 3      # pages of one endpoint fetched at once, when its total is known
PLAY_TIMEOUT = 60.0       # setQueue fetches the queue's items from Apple before playing
LYRICS_TIMEOUT = 30.0     # one catalog read, parsed in the page
SEARCH_TIMEOUT = 30.0     # a catalog search, its suggestions, the landing or a category
BROWSE_TIMEOUT = 60.0     # the editorial groupings: a big answer
SEARCH_LIMIT = 20         # hits per kind
SUGGEST_LIMIT = 10        # completions and top hits while typing
ACCOUNT_NAME_POLL = 0.5   # the account name is looked for this often while waiting for it
UNAUTHORIZE_TIMEOUT = 5.0  # MusicKit revoking the session, at sign-out

# A catalog song id as it appears in a lyrics cache file name: digits, mostly; never a path.
CATALOG_ID_RE = re.compile(r'[A-Za-z0-9._-]{1,64}')

# What control(), shuffle() and repeat() take: the bridge's commands of the same names.
CONTROL_ACTIONS = ('play', 'pause', 'toggle', 'next', 'previous', 'stop')
SHUFFLE_COMMANDS = ('on', 'off', 'toggle')
REPEAT_COMMANDS = ('none', 'one', 'all', 'cycle')

# The relationship of an item() answer that holds its groups' contents, which Apple pages
# (25 albums, 100 tracks), giving a `next` link for the rest.
GROUP_RELATIONSHIPS = {'album': 'tracks', 'playlist': 'tracks', 'artist': 'albums'}

# The kinds add_to_library() takes (a station is followed, not added).
ADDABLE_KINDS = ('song', 'album', 'playlist', 'video', 'musicVideo', 'music-video')

def _shape_item(raw, cache_dir, generation):
    """In a thread: the API's resource as an Item with groups, its artwork fetched into
    <cache>/remote-art/ (normalize.place_in_remote_art; for the cache of `generation`:
    store.py)."""
    art_urls = {}
    item = normalize.normalize_item(raw, cache_dir, include_groups=True, art_urls=art_urls)
    return _fetch_item_art(item, art_urls, cache_dir, generation)


def _fetch_item_art(item, art_urls, cache_dir, generation):
    placed = normalize.place_in_remote_art(item, art_urls, cache_dir)
    normalize.download_item_art(item, cache_dir, placed, generation=generation)
    return item


def _shape_artist(raw, item_id, stubs, answers, cache_dir, generation):
    """In a thread: an artist resource plus its albums' answers (apiAll's list, a failed one
    None in its place, the stub standing in) as an artist Item, one group per album."""
    artist = {'id': item_id, 'type': raw.get('type', 'artists'),
              'attributes': raw.get('attributes') or {}}
    albums = []
    for position, stub in enumerate(stubs):
        answer = answers[position] if position < len(answers) else None
        data = answer.get('data') if isinstance(answer, dict) else None
        albums.append(data[0] if isinstance(data, list) and data else stub)
    art_urls = {}
    item = normalize.normalize_artist(artist, cache_dir, albums=albums, art_urls=art_urls)
    return _fetch_item_art(item, art_urls, cache_dir, generation)


def lyrics_answer(answer):
    """The bridge's lyrics answer as {synced: bool, lines: [{startMs, endMs, text}]}, the
    lines kept in order with their times as integers; anything odd is no lyrics."""
    if not isinstance(answer, dict):
        return {'synced': False, 'lines': []}
    lines = []
    for line in answer.get('lines') or []:
        if not isinstance(line, dict):
            continue
        text = line.get('text')
        if not isinstance(text, str) or not text.strip():
            continue
        start, end = line.get('startMs'), line.get('endMs')
        lines.append({
            'startMs': int(start) if isinstance(start, (int, float)) and start == start else 0,
            'endMs': int(end) if isinstance(end, (int, float)) and end == end else 0,
            'text': text.strip(),
        })
    return {'synced': bool(answer.get('synced')) and bool(lines), 'lines': lines}


def _raise_api_errors(answer, what):
    """MusicKit answers a failed request with a 200 and {"errors": [...]}: an EngineError."""
    error = api.api_error(answer, what)
    if error is not None:
        raise error


def _shape_and_keep(shaper, raw, path, cache_dir, generation):
    """In a thread: `shaper(raw, cache_dir)`'s answer, kept at `path` (stamped `cached`)
    unless the cache was cleared since `generation`."""
    return normalize.write_answer(path, shaper(raw, cache_dir), cache_dir, generation)


async def _read_pages(read, path, params, page, limit, progress, concurrency):
    """Engine.api_pages' reads through `read` (the Engine's api()): (the items in order, the
    total Apple counted or None). A function of its own, so a stand-in engine that borrows
    api_pages needs only an api() of its own."""
    params = dict(params or {})
    first = await read(path, dict(params, limit=page, offset=0))
    items = api.page_data(first)
    meta = first.get('meta')
    total = meta.get('total') if isinstance(meta, dict) else None
    total = total if isinstance(total, int) and not isinstance(total, bool) else None
    if limit is not None:
        total = min(total, limit) if total is not None else None
    if progress:
        progress(len(items), total)
    if not items or not first.get('next') or (limit is not None and len(items) >= limit):
        return items, total
    if total is not None and total > len(items):
        # Every remaining offset is known: a few pages at a time, kept in order.
        offsets = list(range(len(items), total, page))
        for start in range(0, len(offsets), max(1, concurrency)):
            batch = offsets[start:start + max(1, concurrency)]
            answers = await asyncio.gather(
                *(read(path, dict(params, limit=page, offset=offset))
                  for offset in batch))
            for answer in answers:
                items.extend(api.page_data(answer))
            if progress:
                progress(min(len(items), total), total)
            if any(not api.page_data(answer) for answer in answers):
                break  # Apple ran out early (the total counted something we do not get)
        return items, total
    while True:
        answer = await read(path, dict(params, limit=page, offset=len(items)))
        data = api.page_data(answer)
        items.extend(data)
        if progress:
            progress(len(items), total)
        if not data or not answer.get('next') or (limit is not None and len(items) >= limit):
            break
    return items, total


def _made_for_you(raw, cache_dir):
    return {'shelves': normalize.made_for_you_shelves(raw.get('data'), cache_dir)}


def _exit_status(process):
    """How an exited Gio.Subprocess ended, for a message: 'status 3', 'signal 9'."""
    if process.get_if_signaled():
        return f'signal {process.get_term_sig()}'
    if process.get_if_exited():
        return f'status {process.get_exit_status()}'
    return 'status unknown'


async def _process_exit(process):
    """Until the Gio.Subprocess has exited (a task can wait on it and be cancelled cleanly:
    a cancelled wait_async() raises GLib.Error, not CancelledError)."""
    try:
        await process.wait_async()
    except GLib.Error:
        pass


_setpriv_missing_told = False


def with_pdeathsig(argv):
    """argv run through `setpriv --pdeathsig TERM --`, so the kernel sends Chrome SIGTERM when
    the app ends however it ends (a crash, SIGKILL, a lost display): its pid and command line
    are Chrome's through setpriv's exec. Not in a Flatpak sandbox, whose flatpak-spawn
    --watch-bus ends Chrome already; argv as it is (and a warning, once) without util-linux's
    setpriv, when only a clean quit stops Chrome and the next start ends one left behind."""
    global _setpriv_missing_told
    if chrome.in_flatpak():
        return list(argv)
    setpriv = shutil.which('setpriv')
    if setpriv is None:
        if not _setpriv_missing_told:
            _setpriv_missing_told = True
            log.warning('setpriv (util-linux) was not found: Chrome may outlive the app if it '
                        'crashes')
        return list(argv)
    return [setpriv, '--pdeathsig', 'TERM', '--', *argv]


class Engine(GObject.Object):
    """Chrome and the bridge, as one object the UI talks to. See the module."""

    __gtype_name__ = 'AppleMusicEngine'

    __gsignals__ = {
        'event': (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        'rated': (GObject.SignalFlags.RUN_FIRST, None, (str, str, int)),
        'lost': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    state = GObject.Property(type=str, default='down')
    authorized = GObject.Property(type=bool, default=False)
    headless = GObject.Property(type=bool, default=True)

    def __init__(self, profile_dir=None, browser_command=None, demo=False):
        super().__init__()
        self.demo = demo
        self.profile_dir = Path(profile_dir) if profile_dir else config.profile_dir()
        self.browser_command = browser_command
        # Whether start() without a mode runs Chrome headless (the engine-headless setting);
        # sign-in asks for a window whatever this says.
        self.prefer_headless = True
        self.stop_grace = STOP_GRACE
        self.close_wait = CLOSE_WAIT
        self.probe_timeout = PROBE_TIMEOUT
        self.api_retry_delay = API_RETRY_DELAY
        self._client = None
        self._process = None   # the Gio.Subprocess running Chrome
        self._pid = None       # its pid
        self._watch = None     # the task waiting for the connection to drop
        self._relay = None     # the task relaying Chrome's stderr to the log (DEBUG only)
        self._lock = asyncio.Lock()
        self._starting = None  # (Future, headless) of the start under way
        self._tasks = set()    # small tasks of the engine's own, held until they end
        self._probe = None     # the task asking a page that timed out whether it answers
        self._storefront = None  # the account's, as the last status read had it

    @property
    def pid(self):
        return self._pid

    @property
    def client(self):
        """The CDPClient while the engine is up (the debug CLI drives the page through it),
        else None."""
        return self._client if self.state in ('up', 'signing-in') else None

    @property
    def cache_dir(self):
        return config.cache_dir()

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self, visible=None):
        """Chrome up with the bridge in it: headless unless `visible`. Without a mode, a
        running engine is kept whatever its mode, and a stopped one starts headless unless
        `prefer_headless` is off. Asked for a mode, a running Chrome in the other mode is
        stopped first; one in the same mode is kept. EngineError when Chrome or the page will
        not come up, and only EngineError: anything unexpected is logged and becomes
        'engine-down'.

        A start already under way is joined rather than queued behind it, when it suits
        (any start without a mode, the same mode asked): its outcome, success or error, is
        this call's too. Cancelling the call that started it cancels the start (its waiters
        get 'engine-down'); cancelling a joined call leaves the start running."""
        if self.demo:
            return
        if self._starting is not None:
            future, headless = self._starting
            if visible is None or headless == (not visible):
                await self._join_start(future)
                return
        future = asyncio.get_running_loop().create_future()
        starting = self._starting = (
            future, self.prefer_headless if visible is None else not visible)
        try:
            await self._start_locked(visible)
        except BaseException as e:
            if not future.done():
                future.set_exception(e if isinstance(e, EngineError) else EngineError(
                    'engine-down', 'the engine was stopped while it started'))
            raise
        else:
            if not future.done():
                future.set_result(None)
        finally:
            if self._starting is starting:
                self._starting = None
            if not future.cancelled() and future.done():
                future.exception()  # retrieved, whether anyone joined or not

    @staticmethod
    async def _join_start(future):
        """Wait for a start under way, its failure raised here as an EngineError of the same
        code. The start goes on when the waiter is cancelled."""
        await asyncio.wait({future})
        error = future.exception()
        if error is not None:
            raise EngineError(error.code, error.message) from error

    async def _start_locked(self, visible):
        async with self._lock:
            if visible is None:
                if self.state != 'down':
                    return
                headless = self.prefer_headless
            else:
                headless = not visible
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
            except BaseException as e:
                await self._stop()
                if isinstance(e, Exception) and not isinstance(e, EngineError):
                    log.error('the engine could not start', exc_info=e)
                    raise EngineError('engine-down', str(e) or type(e).__name__) from e
                raise

    async def _start(self, headless):
        # In a thread: in a Flatpak sandbox it asks the host through flatpak-spawn.
        binary = await asyncio.to_thread(chrome.find_chrome, self.browser_command)
        if binary is None:
            raise EngineError(
                'no-browser', 'Google Chrome was not found (google-chrome-stable, '
                'google-chrome or /opt/google/chrome/chrome; the browser-command setting '
                'names another)')
        try:
            await asyncio.to_thread(self.profile_dir.mkdir, parents=True, exist_ok=True)
        except OSError as e:
            raise EngineError('engine-down',
                              f'could not prepare {self.profile_dir}: {e.strerror or e}') from e
        # A Chrome already on the profile (the debug CLI's, or one an app crash left behind)
        # would take the new one's arguments and let it exit at once.
        await self._end_owner()
        debug_port = config.debug_port()
        if debug_port is not None:
            log.warning("APPLE_MUSIC_DEBUG_PORT is set: Chrome's DevTools listen on "
                        '127.0.0.1:%d and any local program can control the signed-in '
                        'session', debug_port)
        argv = with_pdeathsig(
            chrome.chrome_args(binary, self.profile_dir, headless, debug_port=debug_port))
        log.debug('exec %s', chrome.describe_argv(argv))  # the profile's path left out
        self._process, transport = self._spawn(argv, binary)
        self._pid = int(self._process.get_identifier())
        log.info('Chrome %d started %s', self._pid, 'headless' if headless else 'visible')
        client = self._client = CDPClient(timeout=CDP_TIMEOUT)
        client.on(EVENT_PREFIX + '*', self._on_bridge_event)
        client.on_timeout = lambda _method: self._on_timeout(client)
        await self._open(client, transport, self._process)
        await client.ensure_bridge(timeout=BRIDGE_WAIT)
        await client.subscribe()  # finds the bridge in place; events from now on
        status = await client.bridge('status')
        self.authorized = bool(isinstance(status, dict) and status.get('authorized'))
        self._storefront = status.get('storefront') if isinstance(status, dict) else None
        self.state = 'up'
        self._watch = asyncio.create_task(self._watch_connection(client), name='engine-watch')
        log.info('engine up: %s', 'authorized' if self.authorized else 'not signed in')

    def _spawn(self, argv, name=None):
        """Chrome as a Gio.Subprocess, the DevTools pipe on its descriptors 3 (it reads) and 4
        (it writes); its output silenced, or its stderr relayed to the log when that is at
        DEBUG. Answers the process and the PipeTransport over this end of the pipe."""
        debug = log.isEnabledFor(logging.DEBUG)
        flags = Gio.SubprocessFlags.STDOUT_SILENCE | (
            Gio.SubprocessFlags.STDERR_PIPE if debug else Gio.SubprocessFlags.STDERR_SILENCE)
        commands, chrome_in = os.pipe()     # Chrome reads commands on its 3
        chrome_out, answers = os.pipe()     # and writes answers and events on its 4
        launcher = Gio.SubprocessLauncher.new(flags)
        launcher.take_fd(commands, 3)
        launcher.take_fd(answers, 4)
        try:
            process = launcher.spawnv(argv)
        except GLib.Error as e:
            os.close(chrome_in)
            os.close(chrome_out)
            raise EngineError('no-browser',
                              f'could not start {name or argv[0]}: {e.message}') from e
        finally:
            launcher.close()  # Chrome's ends are Chrome's alone now
        if debug:
            self._relay = asyncio.create_task(self._relay_stderr(process), name='chrome-stderr')
        return process, PipeTransport(chrome_out, chrome_in)

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

    async def _open(self, client, transport, process):
        """_connect(), raced against Chrome's exit: a Chrome that exits first (a wrong browser
        command, a crash, a profile another Chrome holds) is EngineError('engine-down') at
        once with its exit status, not a timeout after the page wait."""
        opening = asyncio.ensure_future(self._connect(client, transport))
        exited = asyncio.ensure_future(_process_exit(process))
        try:
            done, _ = await asyncio.wait({opening, exited}, return_when=asyncio.FIRST_COMPLETED)
            if opening in done and opening.exception() is None:
                return
            if exited not in done:
                # The connection failed first; a Chrome that is going closes its pipe a
                # moment before it is reaped.
                await asyncio.wait({exited}, timeout=EXIT_GRACE)
            if exited.done():
                raise EngineError('engine-down', f'Chrome exited at once ({_exit_status(process)})')
            raise opening.exception()
        finally:
            for task in (opening, exited):
                if not task.done():
                    task.cancel()
                elif not task.cancelled():
                    task.exception()  # retrieved: its error, if any, is ours or superseded

    async def _connect(self, client, transport):
        """The client on Chrome's pipe, attached to the music.apple.com page."""
        await client.connect(transport)
        await client.attach_page(PAGE_WAIT)

    def _on_timeout(self, client):
        """A call ran out of time: is the page there at all? One probe at a time, and only
        while the engine is up (a start has waits of its own)."""
        if self._client is not client or self.state not in ('up', 'signing-in'):
            return
        if self._probe is None or self._probe.done():
            self._probe = self._in_background(self._probe_page(client))

    async def _probe_page(self, client):
        """`0` in the page. A page that answers is alive, and the slow part was MusicKit or
        Apple; one silent for probe_timeout is wedged (a hung or crashed renderer), so its
        connection is closed: the engine goes down (_watch_connection) and the next command
        starts a fresh Chrome, rather than every command timing out while the Player shows
        what played last."""
        try:
            await client.evaluate('0', await_promise=False, timeout=self.probe_timeout)
        except EngineError as e:
            if e.code == 'timeout' and self._client is client:
                log.warning('the page does not answer; the engine goes down')
                await client.close('the page stopped answering')

    async def _watch_connection(self, client):
        """The engine down when its connection goes on its own (Chrome crashed or was killed,
        the page crashed, closed or stopped answering): `lost(reason)` first, which a stop,
        a restart, sign-in's restarts and quitting never emit."""
        await client.wait_closed()
        async with self._lock:
            if self._client is not client:
                return  # stop() closed it
            reason = client.lost_reason or 'the connection to Chrome was closed'
            log.warning('the engine is down: %s', reason)
            self.emit('lost', reason)
            await self._stop()

    async def stop(self, grace=None):
        """End Chrome: Browser.close over the pipe and up to close_wait seconds for it to go,
        then SIGTERM, up to `grace` seconds (stop_grace), SIGKILL. Nothing to do when it is
        down. Chrome's process is kept until it has gone, so a stop cancelled on its way (a
        bounded quit) still leaves kill() something to kill."""
        if self.demo:
            return
        async with self._lock:
            await self._stop(grace)

    async def _stop(self, grace=None):
        client, self._client = self._client, None
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        process = self._process
        if client is not None:
            client.off(EVENT_PREFIX + '*', self._on_bridge_event)
            if process is not None and client.connected:
                await self._close_browser(client, process)
            await client.close()
        if process is not None:
            await self._terminate(self._pid, process, grace)
            if self._process is process:
                self._pid = self._process = None
        else:
            await self._end_owner()  # stopping an engine that was never started here
        relay, self._relay = self._relay, None
        if relay is not None and not relay.done():
            try:
                await asyncio.wait_for(relay, 1.0)  # Chrome's pipe closes as it exits
            except Exception:
                relay.cancel()
        self.authorized = False
        self._storefront = None
        self.state = 'down'

    async def _close_browser(self, client, process):
        """Browser.close over the pipe, and close_wait seconds in all for Chrome to exit."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.close_wait
        try:
            await client.call('Browser.close', browser=True, timeout=self.close_wait)
        except EngineError as e:  # the answer may not make it out before Chrome is gone
            log.debug('Browser.close: %s', e)
        await self._wait_exit(process, max(0.0, deadline - loop.time()))

    async def _terminate(self, pid, process, grace=None):
        """SIGTERM this process's Chrome, up to `grace` seconds (stop_grace), SIGKILL, and as
        long again for it to be gone."""
        if process.get_identifier() is None:
            return  # gone already, and reaped
        grace = self.stop_grace if grace is None else grace
        log.info('stopping Chrome %d', pid)
        self._signal(process, signal.SIGTERM)
        if not await self._wait_exit(process, grace):
            log.warning('Chrome %d ignored SIGTERM for %g s; killing it', pid, grace)
            self._signal(process, signal.SIGKILL)
            if not await self._wait_exit(process, max(grace, 1.0)):
                log.warning('Chrome %d is still there after SIGKILL', pid)

    async def _end_owner(self):
        """End a Chrome this process did not start that holds the profile (its SingletonLock
        names it): SIGTERM, stop_grace seconds, SIGKILL."""
        pid = await asyncio.to_thread(chrome.profile_owner, self.profile_dir)
        if pid is None:
            return
        log.info('Chrome %d holds the engine profile; stopping it', pid)
        for signum in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                return
            except OSError as e:
                log.warning('could not signal Chrome %d: %s', pid, e)
                return
            if await self._pid_gone(pid, self.stop_grace):
                return
            if signum == signal.SIGTERM:
                log.warning('Chrome %d ignored SIGTERM for %g s; killing it', pid,
                            self.stop_grace)
        log.warning('Chrome %d is still running after SIGKILL', pid)

    async def _pid_gone(self, pid, timeout):
        """True once `pid` is no Chrome on the profile, False after `timeout` seconds."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while await asyncio.to_thread(chrome.pid_alive, pid, self.profile_dir):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    @staticmethod
    def _signal(process, signum):
        try:
            if signum == signal.SIGKILL:
                process.force_exit()
            else:
                process.send_signal(signum)
        except GLib.Error:
            pass

    @staticmethod
    async def _wait_exit(process, timeout):
        """True once the process is gone, False after `timeout` seconds with it still there."""
        try:
            await asyncio.wait_for(_process_exit(process), timeout)
            return True
        except TimeoutError:
            return False

    def kill(self):
        """SIGKILL Chrome now, without waiting: the last resort when stop() ran out of time.
        The connection is let go of as a stop would (it ends as Chrome does), so no `lost`
        follows."""
        client, self._client = self._client, None
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        if client is not None:
            client.off(EVENT_PREFIX + '*', self._on_bridge_event)
        pid, self._pid = self._pid, None
        process, self._process = self._process, None
        if process is not None and process.get_identifier() is not None:
            log.warning('killing Chrome %d', pid)
            self._signal(process, signal.SIGKILL)
        self.state = 'down'
        self.authorized = False

    async def restart(self, visible=None):
        """stop(), then start(visible): without a mode, the preferred one (prefer_headless),
        with the browser the settings now name."""
        await self.stop()
        await self.start(visible=visible)

    # -- events --------------------------------------------------------------------------

    def _on_bridge_event(self, name, data):
        name = name[len(EVENT_PREFIX):]
        if name == 'authorizationStatusDidChange' and isinstance(data, dict):
            authorized = bool(data.get('authorized'))
            if authorized != self.authorized:
                self.authorized = authorized
            self._storefront = None  # the account's may differ: the next status says
        elif name == 'bridgeReset':
            # The page loaded a new document, and the bridge is back in it: what MusicKit
            # held (the sign-in, the queue) may have changed with it. The Player hears it too.
            self._in_background(self._reread_status())
        self.emit('event', name, data)

    async def _reread_status(self):
        try:
            await self.status()  # keeps `authorized` current
        except EngineError as e:
            log.debug('status after the bridge came back: %s', e)

    def _in_background(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- commands ----------------------------------------------------------------------------

    async def _ready(self):
        """The client, for a command: a start in progress is waited for (its failure is the
        command's); 'engine-down' when the engine is down, since commands never start Chrome
        themselves."""
        if self.demo:
            raise EngineError('engine-down', 'the engine is not running')
        if self._starting is not None:
            await self._join_start(self._starting[0])
        if self._client is None or self.state not in ('up', 'signing-in'):
            raise EngineError('engine-down', 'the engine is not running')
        return self._client

    async def status(self):
        """The bridge's status: {ready, engine, authorized, storefront, bitrate}. It keeps
        `authorized` and the storefront the commands use current."""
        client = await self._ready()
        status = await client.bridge('status')
        if not isinstance(status, dict):
            raise EngineError('api', 'the page gave no status')
        self._take_status(status)
        return status

    def _take_status(self, status):
        authorized = bool(status.get('authorized'))
        if authorized != self.authorized:
            self.authorized = authorized
        self._storefront = str(status.get('storefront') or '') or None

    async def _current_storefront(self):
        """The account's storefront ('gb'): as the last status read had it (the start's, or
        sign-in's), read again only when none has since the authorization changed."""
        if not self._storefront:
            await self.status()
        return self._storefront or 'us'

    async def _api(self, client, path, params=None, timeout=None):
        """One API read through the bridge. MusicKit answers a failed request with a 200 and
        {"errors": [...]}, which is a failure here as much as a rejected promise. What may
        pass another time (a 5xx, a 429, an error without a status, a promise the page
        rejected) is tried again, API_RETRIES in all, after a growing pause; what would fail
        the same way (a 4xx, a timeout: api.is_final) raises at once, its `status` Apple's."""
        where = path.partition('?')[0]  # its query stays out of messages and the log
        last = None
        for attempt in range(API_RETRIES):
            if attempt:
                await asyncio.sleep(self.api_retry_delay * 2 ** (attempt - 1))
            try:
                answer = await client.bridge('api', path, params or {}, timeout=timeout)
            except EngineError as e:
                if e.code == 'engine-down':
                    raise
                last = EngineError(e.code, f'{where}: {e.message}', status=e.status)
            else:
                last = api.api_error(answer, where)
                if last is None:
                    return answer if isinstance(answer, dict) else {}
            if api.is_final(last):
                break
        raise last

    async def api(self, path, params=None, timeout=None):
        """One Apple Music API read through the page's MusicKit (the bridge's api(), that is
        mk.api.music(path, params)), retried when it may pass another time (_api); the
        answer's body as a dict ({data: [...], meta, next…}). EngineError('api') when Apple
        says no (its `status` the HTTP status: a 404 is final), 'timeout' when the page did
        not answer in time, 'engine-down' when there is no engine."""
        client = await self._ready()
        return await self._api(client, path, params, timeout)

    async def api_all(self, paths, timeout=60):
        """Several reads at once in the page (the bridge's apiAll): one answer per path, in
        order, None where one failed."""
        paths = list(paths)
        client = await self._ready()
        answers = await client.bridge('apiAll', paths, timeout=timeout)
        answers = answers if isinstance(answers, list) else []
        return (answers + [None] * len(paths))[:len(paths)]

    async def api_pages(self, path, params=None, page=100, limit=None, progress=None,
                        concurrency=PAGE_CONCURRENCY, unique=False):
        """Every item (`data`) of a paged endpoint, `page` at a time by offset, following
        `next` until there is none or `limit` items are in hand. An endpoint whose first
        answer carries `meta.total` has its remaining pages fetched `concurrency` at a time;
        the rest are followed one by one. `progress(done, total)` is called after each page
        (total None until it is known).

        With `unique`, an item met twice is kept once, where it came first: when the listing
        changes while it is read (a song added on another device), every offset after the
        change shifts, and the item at a page's edge comes back on the next page. For a
        listing of resources, not a playlist's tracks, where a song may be twice."""
        items, total = await _read_pages(self.api, path, params, page, limit, progress,
                                         concurrency)
        if limit is not None:
            items = items[:limit]
        if unique:
            seen = set()
            kept = []
            for item in items:
                item_id = item.get('id')
                if item_id is None or item_id not in seen:
                    seen.add(item_id)
                    kept.append(item)
            if len(kept) < len(items) or (total is not None and len(kept) < total):
                log.debug('%s: %d items, %d of them once (%s counted)', path.partition('?')[0],
                          len(items), len(kept), total if total is not None else 'none')
            items = kept
        return items

    async def item(self, kind, item_id):
        """One full Item of `kind` with its `groups` (an album's discs, a playlist's list, an
        artist's albums), all of them however Apple pages them, its artwork fetched. Needs a
        signed-in engine: EngineError('not-signed-in') otherwise; a file that cannot be
        written is EngineError('api')."""
        generation = store.cache_generation()
        client = await self._require_signed_in('load items')
        storefront = await self._current_storefront()
        answer = await self._api(client, api.item_endpoint(kind, item_id, storefront),
                                 timeout=READ_TIMEOUT)
        data = answer.get('data')
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise EngineError('api', f'item not found: {kind} {item_id}')
        raw = await self._whole_relationship(client, data[0], GROUP_RELATIONSHIPS.get(kind))
        cache_dir = str(self.cache_dir)
        try:
            if kind == 'artist':
                stubs, answers = await self._artist_albums(raw, item_id, storefront)
                return await asyncio.to_thread(_shape_artist, raw, str(item_id), stubs,
                                               answers, cache_dir, generation)
            return await asyncio.to_thread(_shape_item, raw, cache_dir, generation)
        except OSError as e:
            raise EngineError('api', f'{kind} {item_id}: {e.strerror or e}') from e

    async def _whole_relationship(self, client, raw, name):
        """`raw` with its relationship `name` whole: Apple's `next` links followed (at most
        RELATIONSHIP_PAGES of them) and their resources added to its `data`."""
        relationships = raw.get('relationships')
        relationship = relationships.get(name) if isinstance(relationships, dict) else None
        if not isinstance(relationship, dict) or not relationship.get('next'):
            return raw
        data = api.page_data(relationship)
        link = relationship.get('next')
        for _ in range(RELATIONSHIP_PAGES):
            if not isinstance(link, str) or not link.startswith('/v1/'):
                break
            answer = await self._api(client, link, timeout=READ_TIMEOUT)
            page = api.page_data(answer)
            if not page:
                break
            data += page
            link = answer.get('next')
        else:
            log.warning('%s of %s: more than %d pages, the rest left out', name, raw.get('id'),
                        RELATIONSHIP_PAGES)
        whole = {key: value for key, value in relationship.items() if key != 'next'}
        return dict(raw, relationships=dict(relationships, **{name: dict(whole, data=data)}))

    async def _artist_albums(self, raw, item_id, storefront):
        """An artist's album stubs and each album's answer with its tracks, in order (None
        where one failed): ALBUM_BATCH at a time in the page, not one round trip per album."""
        relationships = raw.get('relationships') or {}
        stubs = api.page_data(relationships.get('albums'))
        endpoints = [api.album_endpoint(stub.get('id'), storefront) for stub in stubs]
        answers = []
        for start in range(0, len(endpoints), ALBUM_BATCH):
            batch = endpoints[start:start + ALBUM_BATCH]
            try:
                answers += await self.api_all(batch, timeout=60)
            except EngineError as e:
                if e.code == 'engine-down':
                    raise
                log.warning('the albums of artist %s: %s', item_id, e)
                answers += [None] * len(batch)
        return stubs, answers

    async def signin(self, timeout=SIGNIN_TIMEOUT):
        """Until MusicKit is authorized: the bridge asks the page to authorize (Apple's sign-in
        in the visible Chrome window), then the authorizationStatusDidChange event or a poll
        of the status every SIGNIN_POLL seconds says so. The one place the app polls.
        True when signed in; EngineError('timeout') after `timeout` seconds."""
        client = await self._ready()
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
                    # The event that woke the loop is spent: the next read waits for another
                    # event or the poll, not a loop as fast as the page can fail.
                    authorized.clear()
                if isinstance(status, dict) and status.get('authorized'):
                    self._take_status(status)
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

    async def unauthorize(self, timeout=UNAUTHORIZE_TIMEOUT):
        """Sign out of Apple Music in the page: the bridge's signout(), MusicKit's
        unauthorize(), which revokes the session at Apple's end, so a copy of it stops
        working too. Sign-out calls it before the app forgets the account. Best effort: True
        when MusicKit said it was done; False when it could not be (the engine down, the page
        failing or slow, MusicKit refusing), logged at WARNING. It never raises an
        EngineError."""
        try:
            client = await self._ready()
            answer = await client.bridge('signout', timeout=timeout)
        except EngineError as e:
            log.warning('could not sign out of Apple Music: %s', e)
            return False
        if isinstance(answer, dict) and answer.get('ok'):
            log.info('signed out of Apple Music')
            self.authorized = False
            return True
        error = answer.get('error') if isinstance(answer, dict) else None
        log.warning('could not sign out of Apple Music: %s', error or 'no answer')
        return False

    async def account_name(self, wait=0):
        """The account's display name as the signed-in page shows it (the bridge's
        accountName(): only elements that name the user, none while a sign-in control is on
        the page), or '' when none does. Never a guess. Apple's page renders the account menu
        a moment after authorization, so `wait` seconds of polling (every
        ACCOUNT_NAME_POLL) cover the gap right after sign-in."""
        deadline = time.monotonic() + wait
        while True:
            client = await self._ready()
            try:
                name = await client.bridge('accountName', timeout=5)
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
            await asyncio.sleep(ACCOUNT_NAME_POLL)

    # -- playback ------------------------------------------------------------------------
    # Thin wrappers over the bridge's methods of the same names, which the Player calls; the
    # outcome shows up as MusicKit events (the `event` signal), which is where the Player
    # takes its state from, not from these answers.

    async def _require_signed_in(self, what):
        """The client, for a command of the account's: 'not-signed-in' ("sign in to Apple
        Music to <what>") when MusicKit is not authorized."""
        client = await self._ready()
        if not self.authorized:
            raise EngineError('not-signed-in', f'sign in to Apple Music to {what}')
        return client

    async def play(self, kind, item_id, start_with=None, shuffle=False):
        """Play an album, playlist, station, song, musicVideo or artist (its top songs) by
        id, from queue position `start_with` (a track row), shuffled when asked: the
        bridge's play(), that is mk.setQueue({kind: id, startWith, startPlaying}) and
        mk.play(). Needs a signed-in engine: library ids and full songs are the account's."""
        client = await self._require_signed_in('play')
        if not kind or item_id in (None, ''):
            raise EngineError('usage', 'play needs a kind and an id')
        options = {'startWith': int(start_with or 0), 'shuffle': bool(shuffle)}
        await client.bridge('play', str(kind), str(item_id), options, timeout=PLAY_TIMEOUT)

    async def play_next(self, kind, item_id):
        """Queue an item right after the one playing (mk.playNext)."""
        client = await self._require_signed_in('play')
        await client.bridge('playNext', str(kind), str(item_id), timeout=PLAY_TIMEOUT)

    async def play_later(self, kind, item_id):
        """Queue an item at the end (mk.playLater)."""
        client = await self._require_signed_in('play')
        await client.bridge('playLater', str(kind), str(item_id), timeout=PLAY_TIMEOUT)

    async def control(self, action):
        """One of CONTROL_ACTIONS: play, pause, toggle, next, previous, stop."""
        if action not in CONTROL_ACTIONS:
            raise EngineError('usage', f'unknown control action: {action}')
        client = await self._ready()
        await client.bridge('control', action)

    async def seek(self, seconds):
        """Jump to `seconds` into the item playing (mk.seekToTime)."""
        client = await self._ready()
        await client.bridge('seek', max(0.0, float(seconds)))

    async def volume(self, level):
        """Set MusicKit's volume, 0 to 1 (the engine's own, not the system's; Apple's page
        keeps it across restarts). Answers the level as MusicKit has it after the set."""
        client = await self._ready()
        level = min(1.0, max(0.0, float(level)))
        answer = await client.bridge('volume', level)
        value = answer.get('volume') if isinstance(answer, dict) else None
        return float(value) if isinstance(value, (int, float)) else level

    async def shuffle(self, mode):
        """Shuffle on, off or toggle; answers {shuffle: 'on'|'off', repeat: 'none'|'one'|'all'}
        as MusicKit has them after the change."""
        if mode not in SHUFFLE_COMMANDS:
            raise EngineError('usage', f'unknown shuffle mode: {mode}')
        client = await self._ready()
        answer = await client.bridge('shuffle', mode)
        return answer if isinstance(answer, dict) else {}

    async def repeat(self, mode):
        """Repeat none, one, all, or cycle through them; answers as shuffle() does."""
        if mode not in REPEAT_COMMANDS:
            raise EngineError('usage', f'unknown repeat mode: {mode}')
        client = await self._ready()
        answer = await client.bridge('repeat', mode)
        return answer if isinstance(answer, dict) else {}

    async def now_playing(self):
        """What plays: {state, track, position, duration, shuffle, repeat, volume}, the state
        the coarse playing/paused/stopped and the track the Track shape (or None)."""
        client = await self._ready()
        answer = await client.bridge('nowPlaying')
        if not isinstance(answer, dict):
            raise EngineError('api', 'the page gave no now-playing answer')
        return answer

    async def queue(self):
        """The queue: {index, items: [Track…]}."""
        client = await self._ready()
        answer = await client.bridge('queue')
        return answer if isinstance(answer, dict) else {'index': 0, 'items': []}

    async def queue_jump(self, index):
        """Play the queue's entry at `index` (the Up Next list): mk.changeToMediaAtIndex. The
        queue position and now-playing events follow."""
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise EngineError('usage', f'not a queue index: {index!r}')
        client = await self._ready()
        await client.bridge('queueJump', index, timeout=PLAY_TIMEOUT)

    def lyrics_path(self, catalog_song_id):
        """Where a song's lyrics are kept: <cache>/lyrics/<catalog id>.json."""
        return self.cache_dir / 'lyrics' / f'{catalog_song_id}.json'

    async def lyrics(self, catalog_song_id):
        """A catalog song's lyrics: {synced, lines: [{startMs, endMs, text}]} (lyrics_answer's
        shape), from <cache>/lyrics/<id>.json when they were fetched in the last
        normalize.LYRICS_MAX_AGE (no engine needed then; the read marks the file as played,
        for prune_caches), else through the bridge (the catalog's TTML, parsed in the page)
        and kept there, stamped, when Apple had any. No lyrics ({synced: False, lines: []})
        is also what the page answers when Apple refuses (no subscription, a network
        failure), so an empty answer is not kept: the next play asks again."""
        generation = store.cache_generation()
        catalog_song_id = str(catalog_song_id or '')
        if not CATALOG_ID_RE.fullmatch(catalog_song_id):
            raise EngineError('usage', 'lyrics need a catalog song id')
        path = self.lyrics_path(catalog_song_id)
        cached = await asyncio.to_thread(cache.read_kept, path, normalize.LYRICS_MAX_AGE, True)
        if cached is not None and cached.get('lines'):
            return lyrics_answer(cached)
        client = await self._ready()
        answer = lyrics_answer(
            await client.bridge('lyrics', catalog_song_id, timeout=LYRICS_TIMEOUT))
        if answer['lines']:
            await asyncio.to_thread(normalize.write_answer, str(path), answer,
                                    str(self.cache_dir), generation)
        return answer

    # -- ratings and the library ---------------------------------------------------------
    # The heart and the item actions' writes, through the bridge's rating(), addToLibrary()
    # and addToPlaylist() (MusicKit's request builder, judged by the HTTP status: a refusal
    # is EngineError('api') with Apple's "HTTP 403 Forbidden: …"), and the reads beside them
    # (rating, catalog_url), each needing a signed-in engine.

    async def love(self, kind, item_id):
        """Love (favourite) a song, album, playlist, station or music video: PUT
        /v1/me/ratings/<type>s/<id> with the value 1. A library id rates the library item,
        a catalog id the catalog's; loving a song adds it to Favourite Songs."""
        await self._rate(kind, item_id, True)

    async def unlove(self, kind, item_id):
        """Take the rating away (DELETE /v1/me/ratings/<type>s/<id>)."""
        await self._rate(kind, item_id, False)

    async def _rate(self, kind, item_id, love):
        rated = resource_type(kind, item_id)
        client = await self._require_signed_in('rate items')
        await client.bridge('rating', rated, str(item_id), bool(love))
        self.emit('rated', str(kind), str(item_id), 1 if love else 0)

    async def rating(self, kind, item_id):
        """The item's rating: 1 loved, -1 disliked, 0 neither (a read of
        /v1/me/ratings/<type>s?ids=<id>, which answers an empty list for an item without
        one, where the single-item path answers a 404)."""
        rated = resource_type(kind, item_id)
        client = await self._require_signed_in('read ratings')
        answer = await self._api(client, f'/v1/me/ratings/{rated}s', {'ids': str(item_id)},
                                 timeout=READ_TIMEOUT)
        value = 0
        for entry in api.page_data(answer):
            attributes = entry.get('attributes')
            number = attributes.get('value') if isinstance(attributes, dict) else None
            if isinstance(number, int) and not isinstance(number, bool):
                value = max(-1, min(1, number))
                break
        self.emit('rated', str(kind), str(item_id), value)
        return value

    async def add_to_library(self, kind, item_id):
        """Add a catalog song, album, playlist or music video to the library (POST
        /v1/me/library?ids[<type>s]=<id>). A library id is refused: it is there already."""
        if kind not in ADDABLE_KINDS or item_id in (None, ''):
            raise EngineError('usage', f'cannot add {kind or "nothing"} to the library')
        if is_library_id(item_id):
            raise EngineError('usage', f'{item_id} is in the library already')
        client = await self._require_signed_in('add to your library')
        await client.bridge('addToLibrary', api.RESOURCE_TYPES[kind], str(item_id))

    async def catalog_url(self, kind, item_id):
        """The music.apple.com page of a library item's catalog equivalent (its
        /v1/me/library/<type>s/<id>/catalog relationship), or None when it has none (a
        playlist of the user's, an upload). A catalog id is refused: its Item has the URL."""
        rated = resource_type(kind, item_id)
        if not rated.startswith('library-') or rated == 'library-station':
            raise EngineError('usage', f'{item_id} is not a library item')
        client = await self._require_signed_in('look items up')
        try:
            answer = await self._api(client, f'/v1/me/library/{rated[len("library-"):]}s/'
                                             f'{item_id}/catalog', timeout=READ_TIMEOUT)
        except EngineError as e:
            if e.code == 'engine-down':
                raise
            log.debug('catalog of %s %s: %s', kind, item_id, e)
            return None
        for entry in api.page_data(answer):
            url = (entry.get('attributes') or {}).get('url')
            if isinstance(url, str) and url.startswith('https://'):
                return url
        return None

    async def add_to_playlist(self, playlist_id, song_id):
        """Add a song to the end of a library playlist (POST /v1/me/library/playlists/<id>/
        tracks): a library song ("i." id) as `library-songs`, a catalog one as `songs`."""
        playlist_id, song_id = str(playlist_id or ''), str(song_id or '')
        if not is_library_id(playlist_id) or not song_id:
            raise EngineError('usage', 'add to playlist needs a library playlist and a song')
        song_type = 'library-songs' if is_library_id(song_id) else 'songs'
        client = await self._require_signed_in('add to playlists')
        await client.bridge('addToPlaylist', playlist_id, song_id, song_type)

    # -- search and browsing -------------------------------------------------------------
    # The Search page's search, suggestions, landing and categories, the New page (browse)
    # and Made for You. Every one needs a signed-in engine, but a kept answer (the landing, a
    # category, browse and made-for-you are kept for normalize.ANSWER_MAX_AGE) is answered
    # without one. The shaping (backend.normalize) runs in a thread: it stats the artwork cache.

    async def search(self, term, limit=SEARCH_LIMIT):
        """A search of the catalog (the Search page's Apple Music mode): {shelves: [{key,
        title, items: [Item without groups]}]}, the shelves in Apple's order (Top Results
        first). `limit` is per kind. A hit's `art` is its cached cover or a thumbnail-sized
        catalog URL (widgets.artwork.remote_item gives it a place under <cache>/remote-art/);
        `thumb` is on disk or None."""
        term = ' '.join(str(term or '').split())
        if not term:
            raise EngineError('usage', 'search needs a term')
        client = await self._require_signed_in('search')
        raw = await client.bridge('search', term, int(limit), timeout=SEARCH_TIMEOUT)
        _raise_api_errors(raw, 'search')
        return await asyncio.to_thread(normalize.search_results, raw, str(self.cache_dir))

    async def suggest(self, term, limit=SUGGEST_LIMIT):
        """Apple's completions of a term half typed: {terms: [{term, display}], items: [Item
        without groups]}, the items its best few hits for the term as it stands."""
        term = ' '.join(str(term or '').split())
        if not term:
            raise EngineError('usage', 'suggest needs a term')
        client = await self._require_signed_in('search')
        raw = await client.bridge('suggest', term, int(limit), timeout=SEARCH_TIMEOUT)
        _raise_api_errors(raw, 'suggestions')
        return await asyncio.to_thread(normalize.search_suggestions, raw, str(self.cache_dir))

    async def _kept_answer(self, path, refresh, fetch, shaper):
        """A day-long answer, kept at `path`: from the file while it is younger than
        normalize.ANSWER_MAX_AGE (no engine needed) unless `refresh`, else `await
        fetch(client)`'s raw answer, shaped by `shaper(raw, cache_dir)` in a thread and kept
        there, stamped `cached`. When Apple cannot be asked (the engine down or signed out,
        the page or the network failing), an older answer kept there is answered instead,
        marked `stale: True`; with none, or on a refresh, the error. Demo mode has no
        answers of Apple's to keep: 'engine-down'."""
        if self.demo:
            raise EngineError('engine-down', 'the engine is not running')
        generation = store.cache_generation()
        if not refresh:
            kept = await asyncio.to_thread(cache.read_kept, path)
            if kept is not None:
                return kept
        try:
            client = await self._require_signed_in('browse')
            raw = await fetch(client)
        except EngineError as error:
            stale = None
            if not refresh and error.code != 'usage':
                stale = await asyncio.to_thread(cache.read_kept, path, allow_stale=True)
            if stale is None:
                raise
            log.info('answering a kept answer older than a day: %s', error)
            return stale
        return await asyncio.to_thread(_shape_and_keep, shaper, raw, path,
                                       str(self.cache_dir), generation)

    async def landing(self, refresh=False):
        """The search page's Browse Categories: {categories: [{id, kind: 'category', title,
        subtitle, art, artColor, url}]} in Apple's order, `art` a small catalog URL. From
        <cache>/landing.json for a day (no engine needed then), else the bridge's
        searchLanding (the search-landing recommendation set) shaped by normalize.search_landing
        and kept there (_kept_answer). `refresh` asks Apple again."""
        async def fetch(client):
            raw = await client.bridge('searchLanding', timeout=SEARCH_TIMEOUT)
            _raise_api_errors(raw, 'search landing')
            return raw
        return await self._kept_answer(normalize.landing_cache_path(str(self.cache_dir)),
                                       refresh, fetch, normalize.search_landing)

    async def category(self, category_id, refresh=False):
        """A category's page: {id, title, shelves: [{key, title, items}]}, the curator's
        grouping as shelves (Best New Songs, New Releases, Playlists, Stations…). From
        <cache>/categories/<id>.json for a day, else the bridge's category() and kept."""
        category_id = str(category_id or '')
        if not category_id:
            raise EngineError('usage', 'category needs an id')

        async def fetch(client):
            raw = await client.bridge('category', category_id, timeout=SEARCH_TIMEOUT)
            _raise_api_errors(raw, f'category {category_id}')
            return raw
        return await self._kept_answer(
            normalize.category_cache_path(str(self.cache_dir), category_id), refresh, fetch,
            normalize.category_page)

    async def browse(self, refresh=False):
        """The New page: {shelves: [{key, title, items}]}, the editorial groupings behind
        music.apple.com's own New page (api.BROWSE_ENDPOINT, name=music, platform=web) as
        shelves in Apple's order, the featured banners first ("Featured"), then Best New
        Songs, New Releases, playlists, stations, videos; items as search() has them. From
        <cache>/browse.json for a day, else fetched and kept."""
        async def fetch(client):
            storefront = await self._current_storefront()
            return await self._api(client, api.BROWSE_ENDPOINT.format(storefront=storefront),
                                   api.BROWSE_PARAMS, timeout=BROWSE_TIMEOUT)
        return await self._kept_answer(normalize.browse_cache_path(str(self.cache_dir)),
                                       refresh, fetch, normalize.editorial_shelves)

    async def made_for_you(self, refresh=False):
        """Made for You: {shelves: [{key, title, items}]}, the recommendations
        (api.RECOMMENDATIONS_ENDPOINT) made only of the personal mixes and stations, each a
        shelf titled as Apple titles it. From <cache>/made-for-you.json for a day, else
        fetched and kept."""
        async def fetch(client):
            return await self._api(client, api.RECOMMENDATIONS_ENDPOINT,
                                   api.RECOMMENDATIONS_PARAMS, timeout=BROWSE_TIMEOUT)
        return await self._kept_answer(normalize.made_for_you_cache_path(str(self.cache_dir)),
                                       refresh, fetch, _made_for_you)
