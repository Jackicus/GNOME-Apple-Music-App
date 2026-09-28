"""Unit tests for src/engine.py: the Engine's lifecycle and commands against a fake Chrome.

The tests run on gi.events' GLib-backed loop, as the app does, and the Engine spawns "Chrome"
through its own Gio.Subprocess code with the DevTools pipe on descriptors 3 and 4: only the
binary is fake. It is tests/fake_chrome_relay.py, which relays the pipe to the fake browser the
test runs on a Unix socket (UnixFakeChrome, test_client's FakeBrowser), so signals, exits and
the pipe closing are real. chrome.find_chrome is patched to name it, so nothing looks for, or
runs, a real Chrome. No display: the Engine is a GObject.
"""

import asyncio
import json
import os
import pathlib
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest import mock

from tests import ROOT, SRC  # noqa: F401  (registers src/ as the applemusic package)

from gi.events import GLibEventLoop

from applemusic import engine as engine_module
from applemusic.backend import chrome, sync
from applemusic.backend.errors import EngineError
from applemusic.engine import Engine, item_endpoint, resource_type
from tests.test_client import FakeBrowser, FakePage, until, value

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'
RELAY = pathlib.Path(__file__).parent / 'fake_chrome_relay.py'
# PyGObject 3.56's awaitable Gio calls look the loop up through asyncio's policy, which Python
# 3.14 deprecates; main.use_glib_event_loop() filters the same warning in the app.
warnings.filterwarnings('ignore', r"'asyncio\.\w*policy\w*' is deprecated", DeprecationWarning)
PLAYBACK_METHODS = ('play', 'playNext', 'playLater', 'control', 'seek', 'volume', 'shuffle',
                    'repeat', 'nowPlaying', 'queue', 'queueJump', 'lyrics',
                    'search', 'suggest', 'searchAndSuggest', 'searchLanding', 'category',
                    'rating', 'addToLibrary', 'addToPlaylist')


class UnixFakeChrome(FakeBrowser):
    """FakeBrowser behind a Unix socket, where each fake Chrome the engine starts connects
    (its first line says its pid and argv, in `chromes`). The latest connection is the one
    answered; drop() hangs up on it, which ends that Chrome."""

    def __init__(self):
        super().__init__()
        self.chromes = []
        self.server = None
        self.writer = None

    async def start(self, path):
        self.server = await asyncio.start_unix_server(self._serve, path, limit=1 << 24)

    async def _serve(self, reader, writer):
        try:
            hello = json.loads(await reader.readline())
        except (ValueError, ConnectionError):
            writer.close()
            return
        self.chromes.append(hello)
        self.writer = writer
        buffer = b''
        try:
            while chunk := await reader.read(1 << 20):
                buffer += chunk
                *messages, buffer = buffer.split(b'\0')
                for message in messages:
                    await self.on_message(json.loads(message))
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def write(self, payload):
        if self.writer is not None and not self.writer.is_closing():
            self.writer.write(payload + b'\0')
            await self.writer.drain()

    async def drop(self):
        """This Chrome goes: the relay sees the socket close and exits."""
        if self.writer is not None:
            self.writer.close()

    async def close(self):
        if self.writer is not None:
            self.writer.close()
        self.server.close()
        await self.server.wait_closed()

    @property
    def argv(self):
        """The latest Chrome's arguments (without the binary)."""
        return self.chromes[-1]['argv']


class EnginePage(FakePage):
    """The fake page: the bridge's status(), signin(), api() and apiAll(), and the account
    name lookup, on top of FakePage's probe/inject/subscribe."""

    def __init__(self):
        super().__init__()
        self.authorized = False
        self.storefront = 'us'
        self.api_answers = {}   # path, or (path, offset) for a page -> answer dict
        self.api_calls = []
        self.api_params = []    # the params of each api() call, in order
        self.signin_calls = 0
        self.account = None
        self.bridge_calls = []  # (method, args) of the playback commands
        self.bridge_answers = {}  # method -> what it answers (None: {ok: True})

    def __call__(self, message):
        expression = message['params']['expression']
        match = re.match(r'window\.__appleMusicLibrary\.(\w+)\((.*)\)$', expression, re.S)
        if match and match.group(1) in PLAYBACK_METHODS:
            args = json.loads('[' + match.group(2) + ']') if match.group(2) else []
            self.bridge_calls.append((match.group(1), *args))
            return value(self.bridge_answers.get(match.group(1), {'ok': True}))
        if expression == 'window.__appleMusicLibrary.status()':
            return value(self.status())
        if expression == 'window.__appleMusicLibrary.signin()':
            self.signin_calls += 1
            return None  # hangs, as mk.authorize() does until the user acts
        if expression.startswith('window.__appleMusicLibrary.api('):
            path, params = json.loads(
                '[' + expression[len('window.__appleMusicLibrary.api('):-1] + ']')
            self.api_calls.append(path)
            self.api_params.append(params)
            answer = self.api_answers.get((path, params.get('offset')))
            if answer is None:
                answer = self.api_answers.get(path, {'errors': [{'status': '404', 'title': 'no'}]})
            return value(answer)
        if expression.startswith('window.__appleMusicLibrary.apiAll('):
            paths = json.loads(expression[len('window.__appleMusicLibrary.apiAll('):-1])
            self.api_calls.extend(paths)
            return value([self.api_answers.get(path) for path in paths])
        if expression == engine_module.ACCOUNT_NAME_JS:
            return value(self.account)
        return super().__call__(message)

    def status(self):
        return {'ready': self.ready, 'engine': True, 'authorized': self.authorized,
                'storefront': self.storefront, 'bitrate': 256}


class TestEngine(Engine):
    """An Engine that keeps every Chrome it spawned (the Gio.Subprocess and its argv)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.spawned = []   # [(process, argv)]

    def _spawn(self, argv):
        process, transport = super()._spawn(argv)
        self.spawned.append((process, argv))
        return process, transport


def exited(process):
    """Whether a Gio.Subprocess has ended (and been reaped)."""
    return process.get_identifier() is None


class EngineFixture(unittest.IsolatedAsyncioTestCase):
    """The fake Chrome, its page and a TestEngine on them. No tests of its own: the classes
    below subclass it, and a test here would run once for each of them."""

    loop_factory = GLibEventLoop

    # Seconds before the engine's first retry of a failed API read (doubling after): short,
    # so a read that fails every attempt takes milliseconds rather than 1.5 s.
    api_retry_delay = 0.01

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.profile = root / 'chrome-test'
        self.cache = root / 'cache'
        self.log_file = root / 'chrome.log'
        # The fake google-chrome-stable: the relay, on this Python.
        self.binary = root / 'google-chrome-stable'
        python, relay = shlex.quote(sys.executable), shlex.quote(str(RELAY))
        self.binary.write_text(f'#!/bin/sh\nexec {python} -S {relay} "$@"\n')
        self.binary.chmod(0o755)
        self.chrome = UnixFakeChrome()
        self.page = EnginePage()
        self.chrome.responders['Runtime.evaluate'] = self.page
        await self.chrome.start(str(root / 'chrome.sock'))
        environment = {'APPLE_MUSIC_CACHE': str(self.cache),
                       'FAKE_CHROME_SOCKET': str(root / 'chrome.sock'),
                       'FAKE_CHROME_LOG': str(self.log_file), 'FAKE_CHROME_MODE': ''}
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('APPLE_MUSIC_DEBUG_PORT', 'APPLE_MUSIC_PROFILE'):
            os.environ.pop(name, None)
        self.browser_commands = []
        patcher = mock.patch.object(chrome, 'find_chrome', self.find_chrome)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = TestEngine(profile_dir=self.profile)
        self.engine.stop_grace = 0.5
        self.engine.api_retry_delay = self.api_retry_delay
        self.states = []
        self.engine.connect('notify::state', lambda e, _p: self.states.append(e.state))

    async def asyncTearDown(self):
        await self.engine.stop()
        for process, _argv in self.engine.spawned:
            if not exited(process):
                process.force_exit()
        await self.chrome.close()

    def find_chrome(self, command=None):
        self.browser_commands.append(command)
        return str(self.binary)

    def chrome_mode(self, mode):
        """How the next fake Chrome behaves: '', 'stubborn' or 'exit:N' (the relay's)."""
        os.environ['FAKE_CHROME_MODE'] = mode

    def chrome_log(self):
        """What the fake Chromes did, as (event, pid) pairs."""
        try:
            lines = self.log_file.read_text().split()
        except OSError:
            return []
        return [(lines[i], int(lines[i + 1])) for i in range(0, len(lines), 2)]

    async def wait_exited(self, process, timeout=3.0):
        await until(lambda: exited(process), timeout=timeout)


class LifecycleTest(EngineFixture):
    """Starting and stopping Chrome, the connection, and the reads the rest rests on (api,
    status, item, sign-in, the account name)."""

    # -- starting and stopping ------------------------------------------------------------

    async def test_start_spawns_chrome_on_the_pipe_and_brings_the_bridge_up(self):
        self.assertEqual(self.engine.state, 'down')
        await self.engine.start()
        self.assertEqual(self.states, ['starting', 'up'])
        self.assertEqual(len(self.engine.spawned), 1)
        argv = self.chrome.argv
        self.assertIn('--remote-debugging-pipe', argv)
        self.assertFalse([arg for arg in argv if arg.startswith('--remote-debugging-port')])
        self.assertFalse([arg for arg in argv if arg.startswith('--remote-debugging-address')])
        self.assertIn('--headless=new', argv)
        self.assertIn(f'--user-data-dir={self.profile}', argv)
        self.assertTrue(self.profile.is_dir())
        self.assertEqual(self.browser_commands, [None])
        self.assertEqual(self.chrome.methods(session=False)[:3], [
            'Target.setDiscoverTargets', 'Target.getTargets', 'Target.attachToTarget'])
        self.assertEqual((self.page.injections, self.page.subscriptions), (1, 1))
        self.assertTrue(self.engine.headless)
        self.assertFalse(self.engine.authorized)
        self.assertEqual(self.engine.pid, self.chrome.chromes[-1]['pid'])
        self.assertFalse((self.profile / 'engine.json').exists())

    async def test_the_debug_port_is_opt_in_and_warned_about(self):
        os.environ['APPLE_MUSIC_DEBUG_PORT'] = '9300'
        with self.assertLogs(engine_module.log, 'WARNING') as logs:
            await self.engine.start()
        argv = self.chrome.argv
        self.assertIn('--remote-debugging-pipe', argv)
        self.assertIn('--remote-debugging-port=9300', argv)
        self.assertIn('--remote-debugging-address=127.0.0.1', argv)
        self.assertTrue(any('APPLE_MUSIC_DEBUG_PORT is set' in line and '127.0.0.1:9300' in line
                            and 'any local program' in line for line in logs.output))

    async def test_start_reads_authorized(self):
        self.page.authorized = True
        await self.engine.start()
        self.assertTrue(self.engine.authorized)

    async def test_start_again_in_the_same_mode_does_nothing(self):
        await self.engine.start()
        await self.engine.start()
        self.assertEqual(len(self.engine.spawned), 1)
        self.assertEqual(self.states, ['starting', 'up'])

    async def test_start_visible_restarts_a_headless_chrome(self):
        await self.engine.start()
        first, _ = self.engine.spawned[0]
        await self.engine.start(visible=True)
        self.assertEqual(len(self.engine.spawned), 2)
        self.assertTrue(exited(first))
        self.assertIn('--app=https://music.apple.com/', self.chrome.argv)
        self.assertNotIn('--headless=new', self.chrome.argv)
        self.assertFalse(self.engine.headless)
        self.assertEqual(self.engine.state, 'up')
        self.assertEqual(self.states, ['starting', 'up', 'down', 'starting', 'up'])

    async def test_stop_ends_chrome_and_forgets_it(self):
        await self.engine.start()
        process, _ = self.engine.spawned[0]
        await self.engine.stop()
        self.assertEqual(self.engine.state, 'down')
        self.assertTrue(exited(process))
        self.assertIsNone(self.engine.pid)
        with self.assertRaises(EngineError) as ctx:
            await self.engine.status()
        self.assertEqual(ctx.exception.code, 'engine-down')
        await self.engine.stop()  # twice is fine

    async def test_stop_kills_a_chrome_that_ignores_sigterm(self):
        self.chrome_mode('stubborn')
        await self.engine.start()
        process, _ = self.engine.spawned[0]
        pid = self.engine.pid
        started = time.monotonic()
        await self.engine.stop()
        self.assertIn(('term', pid), self.chrome_log())
        self.assertTrue(exited(process))
        self.assertTrue(process.get_if_signaled())
        self.assertEqual(process.get_term_sig(), signal.SIGKILL)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertEqual(self.engine.state, 'down')

    async def test_restart(self):
        await self.engine.start()
        await self.engine.restart(visible=False)
        self.assertEqual(len(self.engine.spawned), 2)
        self.assertTrue(exited(self.engine.spawned[0][0]))
        self.assertEqual(self.engine.state, 'up')

    async def test_start_without_a_mode_keeps_a_running_engine(self):
        await self.engine.start(visible=True)
        await self.engine.start()  # a play request, a sync: whatever mode it runs in
        self.assertEqual(len(self.engine.spawned), 1)
        self.assertFalse(self.engine.headless)
        self.assertEqual(self.states, ['starting', 'up'])

    async def test_start_without_a_mode_follows_prefer_headless(self):
        self.engine.prefer_headless = False  # engine-headless off: a window, for debugging
        await self.engine.start()
        self.assertNotIn('--headless=new', self.chrome.argv)
        self.assertFalse(self.engine.headless)
        self.engine.prefer_headless = True
        await self.engine.restart()
        self.assertIn('--headless=new', self.chrome.argv)
        self.assertTrue(self.engine.headless)

    async def test_a_failed_start_cleans_up(self):
        self.page.ready = False
        with mock.patch.object(engine_module, 'BRIDGE_WAIT', 0.3):
            with self.assertRaises(EngineError) as ctx:
                await self.engine.start()
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertEqual(self.engine.state, 'down')
        await self.wait_exited(self.engine.spawned[0][0])

    async def test_no_chrome_is_engine_down(self):
        with mock.patch.object(chrome, 'find_chrome', lambda command: None):
            with self.assertRaises(EngineError) as ctx:
                await self.engine.start()
        self.assertEqual(ctx.exception.code, 'engine-down')
        self.assertIn('not found', ctx.exception.message)
        self.assertEqual(self.engine.state, 'down')
        self.assertEqual(self.engine.spawned, [])

    async def test_browser_command_is_tried_first(self):
        self.engine.browser_command = 'my-chrome'
        await self.engine.start()
        self.assertEqual(self.browser_commands, ['my-chrome'])

    # -- a Chrome already on the profile ----------------------------------------------------

    def leftover(self, lock=True):
        """A Chrome another process left on the profile (scripts/am.py's, or one an app crash
        left): a live process with the profile on its command line, which the profile's
        SingletonLock names."""
        self.profile.mkdir(parents=True, exist_ok=True)
        flag = f'--user-data-dir={self.profile}'
        process = subprocess.Popen(
            [sys.executable, '-S', '-c', 'import time; time.sleep(30)', flag])
        self.addCleanup(lambda: (process.kill(), process.wait()))
        deadline = time.monotonic() + 2
        while not chrome.pid_alive(process.pid, self.profile) and time.monotonic() < deadline:
            time.sleep(0.005)  # its cmdline is the runner's for an instant while it execs
        if lock:
            os.symlink(f'{socket.gethostname()}-{process.pid}', self.profile / 'SingletonLock')
        return process

    async def test_a_chrome_holding_the_profile_is_stopped_first(self):
        leftover = self.leftover()
        await self.engine.start()
        self.assertIsNotNone(leftover.poll())
        self.assertEqual(leftover.returncode, -signal.SIGTERM)
        self.assertEqual(len(self.engine.spawned), 1)
        self.assertEqual(self.engine.state, 'up')

    async def test_stop_ends_the_chrome_on_the_profile_even_when_it_never_started(self):
        leftover = self.leftover()
        await self.engine.stop()
        self.assertIsNotNone(leftover.poll())
        self.assertEqual(self.engine.spawned, [])

    async def test_a_lock_naming_no_chrome_on_the_profile_is_left_alone(self):
        leftover = self.leftover(lock=False)
        os.symlink(f'{socket.gethostname()}-{os.getpid()}', self.profile / 'SingletonLock')
        await self.engine.start()
        await self.engine.stop()
        self.assertIsNone(leftover.poll())

    # -- the connection --------------------------------------------------------------------

    async def test_losing_the_connection_takes_the_engine_down(self):
        await self.engine.start()
        process, _ = self.engine.spawned[0]
        await self.chrome.drop()
        await until(lambda: self.engine.state == 'down', timeout=3)
        await self.wait_exited(process)
        self.assertFalse(self.engine.authorized)

    async def test_bridge_events_are_re_emitted(self):
        await self.engine.start()
        seen = []
        self.engine.connect('event', lambda e, name, data: seen.append((name, data)))
        await self.chrome.send_binding('playbackStateDidChange', {'state': 'playing'})
        await self.chrome.send_binding('authorizationStatusDidChange',
                                       {'authorized': True, 'status': 1})
        await until(lambda: len(seen) == 2)
        self.assertEqual(seen, [
            ('playbackStateDidChange', {'state': 'playing'}),
            ('authorizationStatusDidChange', {'authorized': True, 'status': 1})])
        self.assertTrue(self.engine.authorized)

    # -- commands --------------------------------------------------------------------------

    async def test_api_is_the_bridge_read(self):
        await self.engine.start()
        self.page.api_answers['/v1/me/library/albums'] = {'data': [{'id': 'l.1'}]}
        self.assertEqual(await self.engine.api('/v1/me/library/albums', {'limit': 5}),
                         {'data': [{'id': 'l.1'}]})
        self.assertEqual(self.page.api_params[-1], {'limit': 5})
        with self.assertRaises(EngineError) as ctx:
            await self.engine.api('/v1/nothing')
        self.assertEqual(ctx.exception.code, 'api')

    async def test_api_pages_with_a_total_fetches_the_rest_at_once(self):
        await self.engine.start()
        path = '/v1/me/library/songs'
        pages = {0: ['a', 'b'], 2: ['c', 'd'], 4: ['e', 'f'], 6: ['g']}
        for offset, ids in pages.items():
            self.page.api_answers[(path, offset)] = {
                'data': [{'id': i} for i in ids], 'meta': {'total': 7},
                'next': f'{path}?offset={offset + 2}' if offset < 6 else None}
        seen = []
        items = await self.engine.api_pages(path, {'include': 'albums'}, page=2,
                                            progress=lambda d, t: seen.append((d, t)))
        self.assertEqual([item['id'] for item in items], list('abcdefg'))
        self.assertEqual(self.page.api_calls.count(path), 4)
        self.assertEqual([p['offset'] for p in self.page.api_params if p.get('include')],
                         [0, 2, 4, 6])
        self.assertEqual(seen[0], (2, 7))
        self.assertEqual(seen[-1], (7, 7))

    async def test_api_pages_without_a_total_follows_next(self):
        await self.engine.start()
        path = '/v1/me/library/recently-added'
        self.page.api_answers[(path, 0)] = {'data': [{'id': 'x'}, {'id': 'y'}],
                                            'next': f'{path}?offset=2'}
        self.page.api_answers[(path, 2)] = {'data': [{'id': 'z'}]}
        seen = []
        items = await self.engine.api_pages(path, page=2,
                                            progress=lambda d, t: seen.append((d, t)))
        self.assertEqual([item['id'] for item in items], ['x', 'y', 'z'])
        self.assertEqual(seen, [(2, None), (3, None)])
        # A limit stops the paging and trims the answer.
        self.page.api_calls.clear()
        items = await self.engine.api_pages(path, page=2, limit=1)
        self.assertEqual([item['id'] for item in items], ['x'])
        self.assertEqual(self.page.api_calls, [path])

    async def test_api_all(self):
        await self.engine.start()
        self.page.api_answers['/v1/a'] = {'data': [1]}
        self.assertEqual(await self.engine.api_all(['/v1/a', '/v1/b']), [{'data': [1]}, None])

    async def test_status(self):
        with self.assertRaises(EngineError) as ctx:
            await self.engine.status()
        self.assertEqual(ctx.exception.code, 'engine-down')
        await self.engine.start()
        self.assertEqual(await self.engine.status(), self.page.status())
        self.page.authorized = True
        self.assertTrue((await self.engine.status())['authorized'])
        self.assertTrue(self.engine.authorized)  # status keeps the property current

    async def test_item_needs_sign_in(self):
        with self.assertRaises(EngineError) as ctx:
            await self.engine.item('album', '1724040700')
        self.assertEqual(ctx.exception.code, 'engine-down')
        await self.engine.start()
        with self.assertRaises(EngineError) as ctx:
            await self.engine.item('album', '1724040700')
        self.assertEqual(ctx.exception.code, 'not-signed-in')

    async def test_item_fetches_shapes_and_keeps_an_album(self):
        self.page.authorized = True
        with open(FIXTURES / 'catalog_album.json') as f:
            self.page.api_answers['/v1/catalog/us/albums/1724040700?include=tracks,artists'] = (
                json.load(f))
        await self.engine.start()
        fetched = []
        with mock.patch.object(sync, 'download_item_art',
                               lambda item, cache_dir: fetched.append(cache_dir) or item):
            item = await self.engine.item('album', '1724040700')
        self.assertEqual(self.page.api_calls,
                         ['/v1/catalog/us/albums/1724040700?include=tracks,artists'])
        self.assertEqual((item['kind'], item['id'], item['title']),
                         ('album', '1724040700', 'Static Skyline (Deluxe Edition)'))
        self.assertEqual(len(item['groups']), 2)  # the fixture's two discs
        self.assertEqual([entry['title'] for entry in item['groups'][0]['entries']][:1],
                         ['Overpass'])
        self.assertEqual(fetched, [str(self.cache)])
        self.assertIn('cached', item)
        kept = self.cache / 'items' / 'album-1724040700.json'
        self.assertTrue(kept.is_file())
        self.assertEqual(json.loads(kept.read_text())['id'], '1724040700')

    async def test_item_library_ids_go_to_the_library_and_a_miss_is_api(self):
        self.page.authorized = True
        await self.engine.start()
        with mock.patch.object(engine_module, 'API_RETRIES', 1):
            with self.assertRaises(EngineError) as ctx:
                await self.engine.item('playlist', 'p.abc')
        self.assertEqual(ctx.exception.code, 'api')
        self.assertEqual(self.page.api_calls, ['/v1/me/library/playlists/p.abc?include=tracks'])

    async def test_item_artist_fetches_its_albums_at_once(self):
        self.page.authorized = True
        with open(FIXTURES / 'catalog_album.json') as f:
            album = json.load(f)
        self.page.api_answers['/v1/catalog/us/artists/42?include=albums'] = {'data': [{
            'id': '42', 'type': 'artists', 'attributes': {'name': 'Paper Parachutes'},
            'relationships': {'albums': {'data': [
                {'id': '1724040700', 'type': 'albums'},
                {'id': 'l.missing', 'type': 'library-albums',
                 'attributes': {'name': 'Unreachable'}}]}}}]}
        self.page.api_answers['/v1/catalog/us/albums/1724040700?include=tracks'] = album
        await self.engine.start()
        with mock.patch.object(sync, 'download_item_art', lambda item, cache_dir: item):
            item = await self.engine.item('artist', '42')
        self.assertEqual(self.page.api_calls, [
            '/v1/catalog/us/artists/42?include=albums',
            '/v1/catalog/us/albums/1724040700?include=tracks',
            '/v1/me/library/albums/l.missing?include=tracks'])
        self.assertEqual((item['kind'], item['title']), ('artist', 'Paper Parachutes'))
        names = [group['name'] for group in item['groups']]
        self.assertIn('Static Skyline (Deluxe Edition)', names)
        self.assertTrue((self.cache / 'items' / 'artist-42.json').is_file())

    async def test_signin_waits_for_the_event(self):
        await self.engine.start()
        task = asyncio.create_task(self.engine.signin(timeout=5))
        await until(lambda: self.engine.state == 'signing-in')
        await until(lambda: self.page.signin_calls == 1)  # mk.authorize() was asked
        self.page.authorized = True
        await self.chrome.send_binding('authorizationStatusDidChange',
                                       {'authorized': True, 'status': 1})
        self.assertTrue(await asyncio.wait_for(task, 3))
        self.assertTrue(self.engine.authorized)
        self.assertEqual(self.engine.state, 'up')

    async def test_signin_polls_the_status(self):
        await self.engine.start()
        with mock.patch.object(engine_module, 'SIGNIN_POLL', 0.1):
            task = asyncio.create_task(self.engine.signin(timeout=5))
            await asyncio.sleep(0.25)
            self.assertFalse(task.done())
            self.page.authorized = True  # no event: the poll finds it
            self.assertTrue(await asyncio.wait_for(task, 3))
        self.assertTrue(self.engine.authorized)
        self.assertEqual(self.engine.state, 'up')

    async def test_signin_times_out(self):
        await self.engine.start()
        with mock.patch.object(engine_module, 'SIGNIN_POLL', 0.1):
            with self.assertRaises(EngineError) as ctx:
                await self.engine.signin(timeout=0.3)
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertEqual(self.engine.state, 'up')
        self.assertFalse(self.engine.authorized)

    async def test_signin_cancelled_leaves_the_engine_up(self):
        await self.engine.start()
        task = asyncio.create_task(self.engine.signin(timeout=5))
        await until(lambda: self.engine.state == 'signing-in')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.engine.state, 'up')

    async def test_signin_when_the_engine_stops(self):
        await self.engine.start()
        with mock.patch.object(engine_module, 'SIGNIN_POLL', 0.1):
            task = asyncio.create_task(self.engine.signin(timeout=5))
            await until(lambda: self.engine.state == 'signing-in')
            await self.engine.stop()
            with self.assertRaises(EngineError) as ctx:
                await task
        self.assertEqual(ctx.exception.code, 'engine-down')
        self.assertEqual(self.engine.state, 'down')

    async def test_signin_already_authorized(self):
        self.page.authorized = True
        await self.engine.start()
        self.assertTrue(await self.engine.signin())
        self.assertEqual(self.page.signin_calls, 0)

    async def test_account_name(self):
        await self.engine.start()
        self.assertEqual(await self.engine.account_name(), '')
        self.page.account = '  Jo  Bloggs '
        self.assertEqual(await self.engine.account_name(), 'Jo Bloggs')
        self.page.account = 'x' * 65  # not a name
        self.assertEqual(await self.engine.account_name(), '')
        self.page.account = 7
        self.assertEqual(await self.engine.account_name(), '')


class PlaybackTest(EngineFixture):
    """The playback commands: thin calls into the bridge, their arguments as am.py sent
    them, their answers shaped, and the errors when the engine is down or signed out."""

    async def up(self, authorized=True):
        self.page.authorized = authorized
        await self.engine.start()

    async def test_play_sends_kind_id_and_options(self):
        await self.up()
        await self.engine.play('album', 'l.alb1')
        await self.engine.play('playlist', 'p.pl1', start_with=3)
        await self.engine.play('station', 'ra.1', shuffle=True)
        self.assertEqual(self.page.bridge_calls, [
            ('play', 'album', 'l.alb1', {'startWith': 0, 'shuffle': False}),
            ('play', 'playlist', 'p.pl1', {'startWith': 3, 'shuffle': False}),
            ('play', 'station', 'ra.1', {'startWith': 0, 'shuffle': True}),
        ])

    async def test_play_needs_a_signed_in_engine(self):
        with self.assertRaises(EngineError) as raised:
            await self.engine.play('album', 'l.alb1')
        self.assertEqual(raised.exception.code, 'engine-down')
        await self.up(authorized=False)
        with self.assertRaises(EngineError) as raised:
            await self.engine.play('album', 'l.alb1')
        self.assertEqual(raised.exception.code, 'not-signed-in')
        self.assertEqual(self.page.bridge_calls, [])

    async def test_play_needs_a_target(self):
        await self.up()
        for kind, item_id in (('', 'x'), (None, 'x'), ('album', ''), ('album', None)):
            with self.assertRaises(EngineError) as raised:
                await self.engine.play(kind, item_id)
            self.assertEqual(raised.exception.code, 'usage')

    async def test_play_next_and_later(self):
        await self.up()
        await self.engine.play_next('song', 'i.1')
        await self.engine.play_later('album', 'l.2')
        self.assertEqual(self.page.bridge_calls,
                         [('playNext', 'song', 'i.1'), ('playLater', 'album', 'l.2')])

    async def test_control_seek_volume(self):
        await self.up()
        for action in engine_module.CONTROL_ACTIONS:
            await self.engine.control(action)
        await self.engine.seek(42)
        await self.engine.seek(-3)
        self.page.bridge_answers['volume'] = {'volume': 0.5}
        self.assertEqual(await self.engine.volume(0.5), 0.5)
        self.page.bridge_answers['volume'] = None  # not a dict: the level asked for
        self.assertEqual(await self.engine.volume(2), 1.0)
        self.assertEqual(self.page.bridge_calls, [
            ('control', 'play'), ('control', 'pause'), ('control', 'toggle'),
            ('control', 'next'), ('control', 'previous'), ('control', 'stop'),
            ('seek', 42.0), ('seek', 0.0), ('volume', 0.5), ('volume', 1.0)])
        with self.assertRaises(EngineError) as raised:
            await self.engine.control('rewind')
        self.assertEqual(raised.exception.code, 'usage')

    async def test_shuffle_and_repeat(self):
        await self.up()
        self.page.bridge_answers['shuffle'] = {'shuffle': 'on', 'repeat': 'none'}
        self.page.bridge_answers['repeat'] = {'shuffle': 'on', 'repeat': 'all'}
        self.assertEqual(await self.engine.shuffle('toggle'), {'shuffle': 'on', 'repeat': 'none'})
        self.assertEqual(await self.engine.repeat('cycle'), {'shuffle': 'on', 'repeat': 'all'})
        self.assertEqual(self.page.bridge_calls, [('shuffle', 'toggle'), ('repeat', 'cycle')])
        for method, mode in (('shuffle', 'maybe'), ('repeat', 'twice')):
            with self.assertRaises(EngineError) as raised:
                await getattr(self.engine, method)(mode)
            self.assertEqual(raised.exception.code, 'usage')

    async def test_now_playing_and_queue(self):
        await self.up()
        answer = {'state': 'playing', 'track': {'id': 'i.1', 'title': 'Harbour Lights'},
                  'position': 3, 'duration': 200, 'shuffle': 'off', 'repeat': 'none',
                  'volume': 1}
        self.page.bridge_answers['nowPlaying'] = answer
        self.page.bridge_answers['queue'] = {'index': 1, 'items': [{'id': 'i.0'}, {'id': 'i.1'}]}
        self.assertEqual(await self.engine.now_playing(), answer)
        self.assertEqual((await self.engine.queue())['index'], 1)
        self.assertEqual(self.page.bridge_calls, [('nowPlaying',), ('queue',)])
        self.page.bridge_answers['nowPlaying'] = None
        with self.assertRaises(EngineError) as raised:
            await self.engine.now_playing()
        self.assertEqual(raised.exception.code, 'api')

    async def test_queue_jump(self):
        await self.up()
        await self.engine.queue_jump(3)
        self.assertEqual(self.page.bridge_calls, [('queueJump', 3)])
        for index in (-1, 'x', None, 2.5, True):
            with self.assertRaises(EngineError) as raised:
                await self.engine.queue_jump(index)
            self.assertEqual(raised.exception.code, 'usage')

    async def test_lyrics_are_fetched_once_and_kept(self):
        await self.up()
        answer = {'synced': True, 'lines': [
            {'startMs': 1000, 'endMs': 3000.4, 'text': ' Harbour lights '},
            {'startMs': 'x', 'text': ''},
            {'startMs': 4000, 'endMs': 6000, 'text': 'Out on the water'}]}
        self.page.bridge_answers['lyrics'] = answer
        lyrics = await self.engine.lyrics('1000000001')
        self.assertEqual(lyrics, {'synced': True, 'lines': [
            {'startMs': 1000, 'endMs': 3000, 'text': 'Harbour lights'},
            {'startMs': 4000, 'endMs': 6000, 'text': 'Out on the water'}]})
        path = self.cache / 'lyrics' / '1000000001.json'
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), lyrics)
        # Again: from the file, no bridge call, and no engine needed.
        self.page.bridge_answers['lyrics'] = {'synced': False, 'lines': []}
        self.assertEqual(await self.engine.lyrics('1000000001'), lyrics)
        await self.engine.stop()
        self.assertEqual(await self.engine.lyrics('1000000001'), lyrics)
        self.assertEqual(self.page.bridge_calls, [('lyrics', '1000000001')])

    async def test_no_lyrics_are_not_kept(self):
        await self.up()
        self.page.bridge_answers['lyrics'] = {'synced': False, 'lines': []}
        self.assertEqual(await self.engine.lyrics('1000000002'), {'synced': False, 'lines': []})
        self.page.bridge_answers['lyrics'] = None  # not a dict either
        self.assertEqual(await self.engine.lyrics('1000000002'), {'synced': False, 'lines': []})
        self.assertFalse((self.cache / 'lyrics').exists())
        self.assertEqual(len(self.page.bridge_calls), 2)
        for bad in ('', None, '../x', 'a/b'):
            with self.assertRaises(EngineError) as raised:
                await self.engine.lyrics(bad)
            self.assertEqual(raised.exception.code, 'usage')

    async def test_commands_when_down(self):
        for coro in (self.engine.control('toggle'), self.engine.seek(1), self.engine.volume(1),
                     self.engine.shuffle('on'), self.engine.repeat('all'),
                     self.engine.now_playing(), self.engine.queue(),
                     self.engine.play_next('song', 'i.1'), self.engine.queue_jump(0),
                     self.engine.lyrics('1000000001'), self.engine.search('x'),
                     self.engine.suggest('x'), self.engine.landing(), self.engine.category('1'),
                     self.engine.browse(), self.engine.made_for_you()):
            with self.assertRaises(EngineError) as raised:
                await coro
            self.assertEqual(raised.exception.code, 'engine-down')


class LibraryWriteTest(EngineFixture):
    """love, unlove, rating, add_to_library, playlists, add_to_playlist and catalog_url: the
    bridge calls am.py made, with the API's types for library and catalog ids, all needing a
    signed-in engine. The fake page records the writes; nothing reaches Apple."""

    async def up(self, authorized=True):
        self.page.authorized = authorized
        await self.engine.start()

    async def test_love_and_unlove_rate_by_type(self):
        await self.up()
        rated = []
        self.engine.connect('rated', lambda _e, kind, item_id, value: rated.append(
            (kind, item_id, value)))
        await self.engine.love('song', '1000000001')
        await self.engine.love('song', 'i.song1')
        await self.engine.unlove('album', 'l.alb1')
        await self.engine.love('playlist', 'pl.u-1')
        await self.engine.love('video', '1000000009')
        await self.engine.unlove('station', 'ra.1')
        self.assertEqual(self.page.bridge_calls, [
            ('rating', 'song', '1000000001', True),
            ('rating', 'library-song', 'i.song1', True),
            ('rating', 'library-album', 'l.alb1', False),
            ('rating', 'playlist', 'pl.u-1', True),
            ('rating', 'music-video', '1000000009', True),
            ('rating', 'station', 'ra.1', False)])
        self.assertEqual(rated, [('song', '1000000001', 1), ('song', 'i.song1', 1),
                                 ('album', 'l.alb1', 0), ('playlist', 'pl.u-1', 1),
                                 ('video', '1000000009', 1), ('station', 'ra.1', 0)])

    async def test_rating_reads_the_ids_form(self):
        await self.up()
        self.page.api_answers['/v1/me/ratings/songs'] = {
            'data': [{'id': '1000000001', 'type': 'ratings', 'attributes': {'value': 1}}]}
        self.page.api_answers['/v1/me/ratings/library-albums'] = {'data': []}
        rated = []
        self.engine.connect('rated', lambda _e, *args: rated.append(args))
        self.assertEqual(await self.engine.rating('song', '1000000001'), 1)
        self.assertEqual(await self.engine.rating('album', 'l.alb1'), 0)
        self.assertEqual(self.page.api_params[-2:], [{'ids': '1000000001'}, {'ids': 'l.alb1'}])
        self.assertEqual(rated, [('song', '1000000001', 1), ('album', 'l.alb1', 0)])

    async def test_nothing_to_rate(self):
        await self.up()
        for kind, item_id in (('artist', '1'), ('folder', 'x'), ('song', ''), (None, '1')):
            with self.assertRaises(EngineError) as raised:
                await self.engine.love(kind, item_id)
            self.assertEqual(raised.exception.code, 'usage')
        self.assertEqual(self.page.bridge_calls, [])

    async def test_add_to_library_takes_catalog_ids(self):
        await self.up()
        await self.engine.add_to_library('album', '1000000002')
        await self.engine.add_to_library('song', '1000000003')
        await self.engine.add_to_library('video', '1000000004')
        self.assertEqual(self.page.bridge_calls, [
            ('addToLibrary', 'album', '1000000002'), ('addToLibrary', 'song', '1000000003'),
            ('addToLibrary', 'music-video', '1000000004')])
        for kind, item_id in (('album', 'l.alb1'), ('song', 'i.1'), ('artist', '5'),
                              ('station', 'ra.1'), ('song', '')):
            with self.assertRaises(EngineError) as raised:
                await self.engine.add_to_library(kind, item_id)
            self.assertEqual(raised.exception.code, 'usage')

    async def test_playlists_are_the_editable_ones(self):
        await self.up()
        self.page.api_answers['/v1/me/library/playlists'] = {'data': [
            {'id': 'p.1', 'attributes': {'name': 'Road Trip', 'canEdit': True}},
            {'id': 'p.2', 'attributes': {'name': 'Favourite Songs', 'canEdit': False}},
            {'id': 'p.3', 'attributes': {'name': 'Kitchen'}},
            {'id': '', 'attributes': {'name': 'Nameless'}}]}
        self.assertEqual(await self.engine.playlists(), [
            {'id': 'p.1', 'title': 'Road Trip'}, {'id': 'p.3', 'title': 'Kitchen'}])

    async def test_add_to_playlist_types_the_song(self):
        await self.up()
        await self.engine.add_to_playlist('p.1', 'i.song1')
        await self.engine.add_to_playlist('p.1', '1000000001')
        self.assertEqual(self.page.bridge_calls, [
            ('addToPlaylist', 'p.1', 'i.song1', 'library-songs'),
            ('addToPlaylist', 'p.1', '1000000001', 'songs')])
        for playlist_id, song_id in (('pl.u-1', '1'), ('', '1'), ('p.1', '')):
            with self.assertRaises(EngineError) as raised:
                await self.engine.add_to_playlist(playlist_id, song_id)
            self.assertEqual(raised.exception.code, 'usage')

    async def test_a_refused_write_is_api(self):
        await self.up()

        def refuse(message):
            expression = message['params']['expression']
            if 'addToPlaylist' in expression:
                return {'result': {'type': 'object'}, 'exceptionDetails': {
                    'text': 'Uncaught', 'exception': {
                        'description': 'Error: HTTP 403 Forbidden: Not allowed'}}}
            return page(message)

        page = self.page
        self.chrome.responders['Runtime.evaluate'] = refuse
        with self.assertRaises(EngineError) as raised:
            await self.engine.add_to_playlist('p.1', 'i.song1')
        self.assertEqual(raised.exception.code, 'api')
        self.assertIn('403', raised.exception.message)

    async def test_catalog_url_of_a_library_album(self):
        await self.up()
        self.page.api_answers['/v1/me/library/albums/l.alb1/catalog'] = {'data': [
            {'id': '1000000002', 'type': 'albums',
             'attributes': {'url': 'https://music.apple.com/gb/album/x/1000000002'}}]}
        self.page.api_answers['/v1/me/library/albums/l.alb2/catalog'] = {'data': []}
        self.assertEqual(await self.engine.catalog_url('album', 'l.alb1'),
                         'https://music.apple.com/gb/album/x/1000000002')
        self.assertIsNone(await self.engine.catalog_url('album', 'l.alb2'))
        with self.assertRaises(EngineError) as raised:
            await self.engine.catalog_url('album', '1000000002')
        self.assertEqual(raised.exception.code, 'usage')

    async def test_writes_need_a_signed_in_engine(self):
        for coro in (self.engine.love('song', '1'), self.engine.rating('song', '1'),
                     self.engine.add_to_library('song', '1'), self.engine.playlists(),
                     self.engine.add_to_playlist('p.1', '1')):
            with self.assertRaises(EngineError) as raised:
                await coro
            self.assertEqual(raised.exception.code, 'engine-down')
        await self.up(authorized=False)
        for coro in (self.engine.unlove('song', '1'), self.engine.rating('song', '1'),
                     self.engine.add_to_library('album', '1'), self.engine.playlists(),
                     self.engine.add_to_playlist('p.1', 'i.1'),
                     self.engine.catalog_url('album', 'l.1')):
            with self.assertRaises(EngineError) as raised:
                await coro
            self.assertEqual(raised.exception.code, 'not-signed-in')
        self.assertEqual(self.page.bridge_calls, [])


def curator(curator_id, name, short=None):
    return {'id': curator_id, 'type': 'apple-curators', 'attributes': {
        'name': name, 'shortName': short or name,
        'url': f'https://music.apple.com/gb/curator/x/{curator_id}',
        'artwork': {'url': 'https://x/{w}x{h}{c}.{f}', 'bgColor': 'dd6848'}}}


class SearchTest(EngineFixture):
    """search, suggest, landing, category, browse and made_for_you: the bridge calls as the
    extension's am.py made them, the shaping, and the day-long caches."""

    async def up(self, authorized=True):
        self.page.authorized = authorized
        await self.engine.start()

    def fixture(self, name):
        with open(FIXTURES / name, encoding='utf-8') as file:
            return json.load(file)

    async def test_search_needs_a_signed_in_engine(self):
        await self.up(authorized=False)
        for coro in (self.engine.search('paper'), self.engine.suggest('pa'),
                     self.engine.landing(), self.engine.category('1'), self.engine.browse(),
                     self.engine.made_for_you()):
            with self.assertRaises(EngineError) as raised:
                await coro
            self.assertEqual(raised.exception.code, 'not-signed-in')
        self.assertEqual(self.page.bridge_calls, [])

    async def test_search_is_shaped_into_shelves(self):
        await self.up()
        self.page.bridge_answers['search'] = self.fixture('search_results.json')
        answer = await self.engine.search('  paper   parachutes ')
        self.assertEqual(self.page.bridge_calls[-1], ('search', 'paper parachutes', False, 20))
        self.assertEqual([shelf['key'] for shelf in answer['shelves']],
                         ['artists', 'songs', 'albums', 'playlists'])
        self.assertEqual(answer['shelves'][2]['title'], 'Albums')
        self.assertEqual([item['kind'] for item in answer['items']],
                         ['artist', 'song', 'album', 'playlist'])
        self.assertNotIn('terms', answer)
        album = answer['shelves'][2]['items'][0]
        self.assertEqual(album['groups'], [])
        self.assertTrue(album['art'].startswith('https://'))  # not on disk: a catalog URL
        self.assertIsNone(album['thumb'])
        # The library's search, with a limit.
        await self.engine.search('paper', library=True, limit=5)
        self.assertEqual(self.page.bridge_calls[-1], ('search', 'paper', True, 5))

    async def test_search_with_suggestions_in_one_round_trip(self):
        await self.up()
        self.page.bridge_answers['searchAndSuggest'] = {
            'search': self.fixture('search_results.json'),
            'suggestions': {'results': {'suggestions': [
                {'kind': 'terms', 'searchTerm': 'paper', 'displayTerm': 'paper'},
                {'kind': 'terms', 'searchTerm': 'paper parachutes',
                 'displayTerm': 'paper parachutes'},
                {'kind': 'terms', 'searchTerm': 'paper planes', 'displayTerm': 'paper planes'},
            ]}}}
        answer = await self.engine.search('paper', suggest=2)
        self.assertEqual(self.page.bridge_calls[-1], ('searchAndSuggest', 'paper', False, 20, 2))
        self.assertEqual([term['term'] for term in answer['terms']], ['paper', 'paper parachutes'])
        self.assertEqual(len(answer['shelves']), 4)
        # No suggestions (the page's promise failed): a search all the same.
        self.page.bridge_answers['searchAndSuggest'] = {
            'search': self.fixture('search_results.json'), 'suggestions': None}
        answer = await self.engine.search('paper', suggest=3)
        self.assertEqual((len(answer['shelves']), answer['terms']), (4, []))

    async def test_search_errors(self):
        await self.up()
        with self.assertRaises(EngineError) as raised:
            await self.engine.search('   ')
        self.assertEqual(raised.exception.code, 'usage')
        self.page.bridge_answers['search'] = {'errors': [{'status': '400', 'title': 'Bad'}]}
        with self.assertRaises(EngineError) as raised:
            await self.engine.search('x')
        self.assertEqual(raised.exception.code, 'api')
        self.assertIn('400 Bad', raised.exception.message)
        # An answer that is not one: nothing found.
        self.page.bridge_answers['search'] = None
        self.assertEqual(await self.engine.search('x'), {'shelves': [], 'items': []})

    async def test_suggest(self):
        await self.up()
        self.page.bridge_answers['suggest'] = {'results': {'suggestions': [
            {'kind': 'terms', 'searchTerm': 'glow', 'displayTerm': 'glow'},
            {'kind': 'topResults', 'content': {
                'id': '900000701', 'type': 'artists',
                'attributes': {'name': 'Glowline', 'genreNames': ['Pop']}}},
        ]}}
        answer = await self.engine.suggest('gl', limit=4)
        self.assertEqual(self.page.bridge_calls[-1], ('suggest', 'gl', 4))
        self.assertEqual(answer['terms'], [{'term': 'glow', 'display': 'glow'}])
        self.assertEqual([(item['kind'], item['title']) for item in answer['items']],
                         [('artist', 'Glowline')])

    async def test_landing_is_kept_for_a_day(self):
        await self.up()
        self.page.bridge_answers['searchLanding'] = {'data': [
            {'id': 'r1', 'type': 'personal-recommendation',
             'relationships': {'contents': {'data': [
                 curator('900000801', 'Apple Music Folk', 'Folk'),
                 curator('900000802', 'Apple Music Live')]}}}]}
        answer = await self.engine.landing()
        self.assertEqual(self.page.bridge_calls, [('searchLanding',)])
        self.assertEqual([(c['id'], c['kind'], c['title']) for c in answer['categories']],
                         [('900000801', 'category', 'Folk'),
                          ('900000802', 'category', 'Apple Music Live')])
        self.assertIn('cached', answer)
        path = self.cache / 'landing.json'
        self.assertTrue(path.is_file())
        # Kept: answered from the file, even with the engine down.
        await self.engine.stop()
        again = await self.engine.landing()
        self.assertEqual(again['categories'], answer['categories'])
        self.assertEqual(len(self.page.bridge_calls), 1)
        # A refresh, or an old stamp, asks Apple again.
        await self.up()
        await self.engine.landing(refresh=True)
        self.assertEqual(len(self.page.bridge_calls), 2)
        kept = json.loads(path.read_text())
        kept['cached'] = '2020-01-01T00:00:00Z'
        path.write_text(json.dumps(kept))
        await self.engine.landing()
        self.assertEqual(len(self.page.bridge_calls), 3)

    async def test_category_is_kept_by_id(self):
        await self.up()
        self.page.bridge_answers['category'] = {'data': [{
            'id': '900000801', 'type': 'apple-curators',
            'attributes': {'name': 'Apple Music Folk', 'shortName': 'Folk'},
            'relationships': {'grouping': {'data': [
                self.fixture('editorial_groupings.json')['data'][0]]}}}]}
        answer = await self.engine.category('900000801')
        self.assertEqual(self.page.bridge_calls, [('category', '900000801')])
        self.assertEqual(answer['title'], 'Folk')
        self.assertEqual([s['key'] for s in answer['shelves']],
                         ['cat-best-new-songs', 'cat-new-releases', 'cat-stations'])
        self.assertTrue((self.cache / 'categories' / '900000801.json').is_file())
        await self.engine.stop()
        self.assertEqual((await self.engine.category('900000801'))['title'], 'Folk')
        with self.assertRaises(EngineError) as raised:
            await self.engine.category('900000802')  # not kept: needs the engine
        self.assertEqual(raised.exception.code, 'engine-down')
        with self.assertRaises(EngineError) as raised:
            await self.engine.category('')
        self.assertEqual(raised.exception.code, 'usage')

    async def test_browse_is_the_editorial_groupings_kept_for_a_day(self):
        self.page.storefront = 'gb'
        await self.up()
        path = '/v1/editorial/gb/groupings'
        self.page.api_answers[path] = self.fixture('editorial_groupings.json')
        answer = await self.engine.browse()
        self.assertEqual(self.page.api_calls, [path])
        self.assertEqual(self.page.api_params[-1],
                         {'name': 'music', 'platform': 'web', 'extend': 'editorialArtwork'})
        self.assertEqual([s['title'] for s in answer['shelves']],
                         ['Featured', 'Best New Songs', 'New Releases', 'Stations'])
        self.assertTrue((self.cache / 'browse.json').is_file())
        await self.engine.stop()
        self.assertEqual(len((await self.engine.browse())['shelves']), 4)
        await self.up()
        await self.engine.browse(refresh=True)
        self.assertEqual(self.page.api_calls, [path, path])

    async def test_browse_failure_is_api(self):
        await self.up()
        with mock.patch.object(engine_module, 'API_RETRIES', 1):
            with self.assertRaises(EngineError) as raised:
                await self.engine.browse()
        self.assertEqual(raised.exception.code, 'api')
        self.assertFalse((self.cache / 'browse.json').exists())

    async def test_made_for_you_keeps_the_mixes_and_stations(self):
        await self.up()
        mixes = [{'id': f'pl.pm-{n}', 'type': 'playlists', 'attributes': {
            'name': f'Mix {n}', 'playlistType': 'personal-mix',
            'artwork': {'url': 'https://x/mix/{w}x{h}{c}.{f}', 'bgColor': '223344'},
            'playParams': {'id': f'pl.pm-{n}', 'kind': 'playlist'}}} for n in (1, 2)]
        albums = [{'id': '900000201', 'type': 'albums',
                   'attributes': {'name': 'Lantern Season', 'artistName': 'P', 'trackCount': 3}}]
        self.page.api_answers['/v1/me/recommendations'] = {'data': [
            {'id': 'r-mixes', 'type': 'personal-recommendation',
             'attributes': {'title': {'stringForDisplay': 'Made for You'}},
             'relationships': {'contents': {'data': mixes}}},
            {'id': 'r-albums', 'type': 'personal-recommendation',
             'attributes': {'title': {'stringForDisplay': 'New Releases for You'}},
             'relationships': {'contents': {'data': albums}}},
        ]}
        answer = await self.engine.made_for_you()
        self.assertEqual(self.page.api_calls, ['/v1/me/recommendations'])
        self.assertEqual(self.page.api_params[-1], {'limit': 25})
        self.assertEqual([(s['key'], s['title'], len(s['items'])) for s in answer['shelves']],
                         [('rec-r-mixes', 'Made for You', 2)])
        self.assertTrue((self.cache / 'made-for-you.json').is_file())
        await self.engine.stop()
        self.assertEqual(len((await self.engine.made_for_you())['shelves']), 1)


class DemoEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_demo_does_nothing_and_every_command_is_engine_down(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        # A kept answer is not served either: a demo has none of Apple's.
        sync.write_answer(sync.landing_cache_path(tmp.name), {'categories': []})
        engine = Engine(profile_dir='/nowhere/chrome', demo=True)
        await engine.start()
        await engine.start(visible=True)
        self.assertEqual(engine.state, 'down')
        for command in (engine.status(), engine.item('album', '1'), engine.signin(),
                        engine.account_name(), engine.search('x'), engine.suggest('x'),
                        engine.landing(), engine.category('1'), engine.browse(),
                        engine.made_for_you()):
            with self.assertRaises(EngineError) as ctx:
                await command
            self.assertEqual(ctx.exception.code, 'engine-down')
        await engine.stop()
        self.assertFalse(pathlib.Path('/nowhere/chrome').exists())


class CacheTest(unittest.TestCase):
    """cache_size() and clear_cache() over an invented cache directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.cache = self.root / 'cache'
        files = {
            'library.json': 100, 'library.lock': 0, 'landing.json': 10, 'browse.json': 10,
            'made-for-you.json': 10, 'art/l.alb1.jpg': 1000, 'art/.sizes': 20,
            'thumb/l.alb1.jpg': 300, 'remote-art/abc.jpg': 400, 'items/album-1.json': 50,
            'lyrics/1000000001.json': 30, 'categories/c1.json': 40, 'notes.txt': 5,
        }
        for name, size in files.items():
            path = self.cache / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * size)
        self.total = sum(files.values())
        # A link to something outside: neither counted nor followed, only removed.
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        (self.outside / 'big.bin').write_bytes(b'x' * 5000)
        (self.cache / 'art' / 'elsewhere').symlink_to(self.outside)

    def test_size_adds_up_the_files(self):
        self.assertEqual(engine_module.cache_size(self.cache), self.total)
        self.assertEqual(engine_module.cache_size(self.root / 'missing'), 0)

    def test_clear_removes_the_cache_and_leaves_the_rest(self):
        removed = engine_module.clear_cache(self.cache)
        self.assertEqual(removed, len(engine_module.CACHE_ENTRIES))
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()),
                         ['library.lock', 'notes.txt'])
        self.assertTrue((self.outside / 'big.bin').is_file())  # the link's target stays
        self.assertEqual(engine_module.cache_size(self.cache), 5)
        self.assertEqual(engine_module.clear_cache(self.cache), 0)  # nothing left to clear
        self.assertEqual(engine_module.clear_cache(self.root / 'missing'), 0)


class EndpointTest(unittest.TestCase):
    def test_library_and_catalog(self):
        self.assertEqual(item_endpoint('album', 'l.abc', 'gb'),
                         '/v1/me/library/albums/l.abc?include=tracks,artists')
        self.assertEqual(item_endpoint('album', '123', 'gb'),
                         '/v1/catalog/gb/albums/123?include=tracks,artists')
        self.assertEqual(item_endpoint('playlist', 'p.xyz', 'us'),
                         '/v1/me/library/playlists/p.xyz?include=tracks')
        self.assertEqual(item_endpoint('playlist', 'pl.u-1', 'us'),
                         '/v1/catalog/us/playlists/pl.u-1?include=tracks')
        self.assertEqual(item_endpoint('artist', 'r.1', 'us'),
                         '/v1/me/library/artists/r.1?include=albums')
        self.assertEqual(item_endpoint('artist', '9', 'us'),
                         '/v1/catalog/us/artists/9?include=albums')
        self.assertEqual(item_endpoint('station', 'ra.1', 'us'), '/v1/catalog/us/stations/ra.1')
        self.assertEqual(item_endpoint('song', 'l.s', 'us'), '/v1/me/library/songs/l.s')
        self.assertEqual(item_endpoint('song', 'i.s', 'us'), '/v1/me/library/songs/i.s')
        self.assertEqual(item_endpoint('song', '5', 'us'), '/v1/catalog/us/songs/5')
        self.assertEqual(item_endpoint('video', '7', 'us'), '/v1/catalog/us/videos/7')


class ResourceTypeTest(unittest.TestCase):
    def test_types(self):
        self.assertEqual(resource_type('song', '1'), 'song')
        self.assertEqual(resource_type('song', 'i.1'), 'library-song')
        self.assertEqual(resource_type('album', 'l.1'), 'library-album')
        self.assertEqual(resource_type('playlist', 'p.1'), 'library-playlist')
        self.assertEqual(resource_type('playlist', 'pl.1'), 'playlist')
        self.assertEqual(resource_type('video', 'i.1'), 'library-music-video')
        self.assertEqual(resource_type('musicVideo', '1'), 'music-video')
        self.assertEqual(resource_type('station', 'ra.1'), 'station')
        for kind, item_id in (('artist', '1'), ('category', '1'), ('song', None)):
            with self.assertRaises(EngineError):
                resource_type(kind, item_id)


if __name__ == '__main__':
    unittest.main()
