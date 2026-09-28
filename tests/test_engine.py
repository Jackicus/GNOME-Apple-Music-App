"""Unit tests for src/engine.py: the Engine's lifecycle and commands against fakes.

Chrome is a sleeping Python process started with the profile on its command line, so the
state file's liveness check, SIGTERM and SIGKILL are real; the DevTools wait is patched out
and the connection goes to test_client's fake Chrome, whose page answers the bridge's calls.
No display: the Engine is a GObject, the loop plain asyncio.
"""

import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests import ROOT, SRC  # noqa: F401  (registers src/ as the applemusic package)

from applemusic import engine as engine_module
from applemusic.backend import chrome, config, sync
from applemusic.backend.client import CDPClient
from applemusic.backend.errors import EngineError
from applemusic.engine import Engine, engine_paths, item_endpoint
from tests.test_client import FakeChrome, FakePage, until, value

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'
PLAYBACK_METHODS = ('play', 'playNext', 'playLater', 'control', 'seek', 'volume', 'shuffle',
                    'repeat', 'nowPlaying', 'queue')
SLEEPER = 'import time; time.sleep(60)'
STUBBORN = 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'


class FakeProcess:
    """What Engine._spawn returns: a real child (so signals and liveness are real) behind
    Gio.Subprocess's methods."""

    def __init__(self, argv, code=SLEEPER):
        # The last argument is the profile flag chrome.pid_alive looks for on the command line.
        profile_flag = next(arg for arg in argv if arg.startswith('--user-data-dir='))
        self.argv = argv
        self.popen = subprocess.Popen([sys.executable, '-c', code, profile_flag])
        self.signals = []
        # /proc/<pid>/cmdline is empty for an instant while the child execs (a few ms).
        profile = profile_flag[len('--user-data-dir='):]
        deadline = time.monotonic() + 2
        while not chrome.pid_alive(self.popen.pid, profile) and time.monotonic() < deadline:
            time.sleep(0.005)

    def get_identifier(self):
        return str(self.popen.pid)

    def send_signal(self, signum):
        self.signals.append(signum)
        self.popen.send_signal(signum)

    def force_exit(self):
        self.signals.append('kill')
        self.popen.kill()

    async def wait_async(self):
        await asyncio.to_thread(self.popen.wait)

    def cleanup(self):
        if self.popen.poll() is None:
            self.popen.kill()
            self.popen.wait()


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
        return {'ready': True, 'engine': True, 'authorized': self.authorized,
                'storefront': self.storefront, 'bitrate': 256}


class TestEngine(Engine):
    """An Engine whose Chrome is a FakeProcess and whose page is the fake's."""

    def __init__(self, test, **kwargs):
        super().__init__(**kwargs)
        self.test = test
        self.spawned = []
        self.connected = 0

    def _spawn(self, argv):
        process = FakeProcess(argv, self.test.process_code)
        self.spawned.append(process)
        self.test.processes.append(process)
        return process

    async def _connect(self):
        self.connected += 1
        client = CDPClient(timeout=3)
        await client.connect(self.test.ws_url)
        return client


class EngineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = pathlib.Path(self.tmp.name) / 'chrome-test'
        self.cache = pathlib.Path(self.tmp.name) / 'cache'
        self.processes = []
        self.process_code = SLEEPER
        self.chrome = FakeChrome()
        self.chrome.responders['Page.getFrameTree'] = (
            lambda m: {'frameTree': {'frame': {'id': 'main', 'url': 'https://music.apple.com/'}}})
        self.page = EnginePage()
        self.chrome.responders['Runtime.evaluate'] = self.page
        self.ws_url = await self.chrome.start()
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Chrome is not really there to answer /json/version.
        patcher = mock.patch.object(chrome, 'wait_for_devtools', self.fake_devtools)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.devtools_waits = 0
        self.engine = TestEngine(self, profile_dir=self.profile, port=9333)
        self.engine.stop_grace = 0.5
        self.states = []
        self.engine.connect('notify::state', lambda e, _p: self.states.append(e.state))

    async def asyncTearDown(self):
        await self.engine.stop()
        for process in self.processes:
            process.cleanup()
        await self.chrome.close()
        self.tmp.cleanup()

    async def fake_devtools(self, port, timeout=15.0):
        self.devtools_waits += 1
        self.assertEqual(port, 9333)
        return {'Browser': 'Fake/1'}

    # -- starting and stopping ------------------------------------------------------------

    async def test_start_spawns_chrome_and_brings_the_bridge_up(self):
        self.assertEqual(self.engine.state, 'down')
        await self.engine.start()
        self.assertEqual(self.states, ['starting', 'up'])
        self.assertEqual(len(self.engine.spawned), 1)
        argv = self.engine.spawned[0].argv
        self.assertIn('--headless=new', argv)
        self.assertIn(f'--user-data-dir={self.profile}', argv)
        self.assertIn('--remote-debugging-port=9333', argv)
        self.assertTrue(self.profile.is_dir())
        self.assertEqual(self.devtools_waits, 1)
        self.assertEqual(self.engine.connected, 1)
        self.assertEqual((self.page.injections, self.page.subscriptions), (1, 1))
        self.assertTrue(self.engine.headless)
        self.assertFalse(self.engine.authorized)
        self.assertEqual(self.engine.pid, self.engine.spawned[0].popen.pid)
        # engine.json describes it, inside the profile (not the default profile).
        state = chrome.EngineState.load(self.engine.state_file)
        self.assertEqual(self.engine.state_file, self.profile / 'engine.json')
        self.assertEqual((state.pid, state.port, state.headless, state.profile),
                         (self.engine.pid, 9333, True, str(self.profile)))
        self.assertTrue(state.alive)

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
        first = self.engine.spawned[0]
        await self.engine.start(visible=True)
        self.assertEqual(len(self.engine.spawned), 2)
        self.assertIsNotNone(first.popen.poll())  # the first is gone
        self.assertIn(15, first.signals)  # SIGTERM
        self.assertIn('--app=https://music.apple.com/', self.engine.spawned[1].argv)
        self.assertNotIn('--headless=new', self.engine.spawned[1].argv)
        self.assertFalse(self.engine.headless)
        self.assertEqual(self.engine.state, 'up')
        self.assertEqual(self.states, ['starting', 'up', 'down', 'starting', 'up'])

    async def test_stop_terminates_chrome_and_forgets_it(self):
        await self.engine.start()
        process = self.engine.spawned[0]
        await self.engine.stop()
        self.assertEqual(self.engine.state, 'down')
        self.assertIsNotNone(process.popen.poll())
        self.assertEqual(process.signals, [15])
        self.assertFalse(self.engine.state_file.exists())
        self.assertIsNone(self.engine.pid)
        await self.chrome_saw_close()
        with self.assertRaises(EngineError) as ctx:
            await self.engine.status()
        self.assertEqual(ctx.exception.code, 'engine-down')
        await self.engine.stop()  # twice is fine

    async def chrome_saw_close(self):
        await until(lambda: any(opcode == 8 for opcode, _ in self.chrome.frames))

    async def test_stop_kills_a_chrome_that_ignores_sigterm(self):
        self.process_code = STUBBORN
        await self.engine.start()
        process = self.engine.spawned[0]
        started = time.monotonic()
        await self.engine.stop()
        self.assertEqual(process.signals, [15, 'kill'])
        self.assertIsNotNone(process.popen.poll())
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertEqual(self.engine.state, 'down')

    async def test_restart(self):
        await self.engine.start()
        await self.engine.restart(visible=False)
        self.assertEqual(len(self.engine.spawned), 2)
        self.assertIsNotNone(self.engine.spawned[0].popen.poll())
        self.assertEqual(self.engine.state, 'up')

    async def test_a_failed_start_cleans_up(self):
        self.chrome.responders['Runtime.evaluate'] = FakePage(ready=False)
        with mock.patch.object(engine_module, 'BRIDGE_WAIT', 0.3):
            with self.assertRaises(EngineError) as ctx:
                await self.engine.start()
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertEqual(self.engine.state, 'down')
        self.assertIsNotNone(self.engine.spawned[0].popen.poll())
        self.assertFalse(self.engine.state_file.exists())

    async def test_no_chrome_is_engine_down(self):
        with mock.patch.object(chrome, 'find_chrome', lambda command: None):
            with self.assertRaises(EngineError) as ctx:
                await self.engine.start()
        self.assertEqual(ctx.exception.code, 'engine-down')
        self.assertIn('not found', ctx.exception.message)
        self.assertEqual(self.engine.state, 'down')
        self.assertEqual(self.engine.spawned, [])

    async def test_browser_command_is_tried_first(self):
        seen = []

        def find(command):
            seen.append(command)
            return None
        self.engine.browser_command = 'my-chrome'
        with mock.patch.object(chrome, 'find_chrome', find):
            with self.assertRaises(EngineError):
                await self.engine.start()
        self.assertEqual(seen, ['my-chrome'])

    # -- reclaiming ------------------------------------------------------------------------

    def leftover(self, headless=True, port=9333):
        """A Chrome from an earlier run, as engine.json describes it: a live process."""
        process = FakeProcess([f'--user-data-dir={self.profile}'])
        self.processes.append(process)
        chrome.EngineState(process.popen.pid, port, headless, self.profile).save(
            self.engine.state_file)
        return process

    async def test_a_live_chrome_in_the_same_mode_is_reclaimed(self):
        leftover = self.leftover(headless=True)
        await self.engine.start()
        self.assertEqual(self.engine.spawned, [])
        self.assertEqual(self.engine.pid, leftover.popen.pid)
        self.assertEqual(self.engine.state, 'up')
        self.assertIsNone(leftover.popen.poll())
        await self.engine.stop()
        self.assertIsNotNone(leftover.popen.poll())  # os.kill, no process object
        self.assertFalse(self.engine.state_file.exists())

    async def test_a_live_chrome_in_the_other_mode_is_replaced(self):
        leftover = self.leftover(headless=False)
        await self.engine.start()
        self.assertIsNotNone(leftover.popen.poll())
        self.assertEqual(len(self.engine.spawned), 1)
        self.assertEqual(self.engine.pid, self.engine.spawned[0].popen.pid)

    async def test_a_live_chrome_on_another_port_is_replaced(self):
        leftover = self.leftover(port=9444)
        await self.engine.start()
        self.assertIsNotNone(leftover.popen.poll())
        self.assertEqual(len(self.engine.spawned), 1)

    async def test_a_stale_state_file_is_ignored(self):
        leftover = self.leftover()
        leftover.cleanup()
        await self.engine.start()
        self.assertEqual(len(self.engine.spawned), 1)
        self.assertEqual(chrome.EngineState.load(self.engine.state_file).pid,
                         self.engine.spawned[0].popen.pid)

    # -- the connection --------------------------------------------------------------------

    async def test_losing_the_connection_takes_the_engine_down(self):
        await self.engine.start()
        process = self.engine.spawned[0]
        await self.chrome.drop()
        await until(lambda: self.engine.state == 'down', timeout=3)
        self.assertIsNotNone(process.popen.poll())  # what was left of Chrome is stopped
        self.assertFalse(self.engine.state_file.exists())
        self.assertFalse(self.engine.authorized)

    async def test_bridge_events_are_re_emitted(self):
        await self.engine.start()
        seen = []
        self.engine.connect('event', lambda e, name, data: seen.append((name, data)))
        await self.chrome.send({'method': 'Runtime.bindingCalled', 'params': {
            'name': '__amEvent', 'executionContextId': 1,
            'payload': json.dumps({'name': 'playbackStateDidChange',
                                   'data': {'state': 'playing'}})}})
        await self.chrome.send({'method': 'Runtime.bindingCalled', 'params': {
            'name': '__amEvent', 'executionContextId': 1,
            'payload': json.dumps({'name': 'authorizationStatusDidChange',
                                   'data': {'authorized': True, 'status': 1}})}})
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
        await self.chrome.send({'method': 'Runtime.bindingCalled', 'params': {
            'name': '__amEvent', 'executionContextId': 1,
            'payload': json.dumps({'name': 'authorizationStatusDidChange',
                                   'data': {'authorized': True, 'status': 1}})}})
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


class PlaybackTest(EngineTest):
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

    async def test_commands_when_down(self):
        for coro in (self.engine.control('toggle'), self.engine.seek(1), self.engine.volume(1),
                     self.engine.shuffle('on'), self.engine.repeat('all'),
                     self.engine.now_playing(), self.engine.queue(),
                     self.engine.play_next('song', 'i.1')):
            with self.assertRaises(EngineError) as raised:
                await coro
            self.assertEqual(raised.exception.code, 'engine-down')


class DemoEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_demo_does_nothing_and_every_command_is_engine_down(self):
        engine = Engine(profile_dir='/nowhere/chrome', port=9999, demo=True)
        await engine.start()
        await engine.start(visible=True)
        self.assertEqual(engine.state, 'down')
        for command in (engine.status(), engine.item('album', '1'), engine.signin(),
                        engine.account_name()):
            with self.assertRaises(EngineError) as ctx:
                await command
            self.assertEqual(ctx.exception.code, 'engine-down')
        await engine.stop()
        self.assertFalse(pathlib.Path('/nowhere/chrome').exists())


class PathsTest(unittest.TestCase):
    def env(self, **values):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('APPLE_MUSIC_PROFILE', 'APPLE_MUSIC_PORT', 'XDG_DATA_HOME'):
            os.environ.pop(name, None)
        os.environ.update(values)

    def test_release_and_development(self):
        self.env(XDG_DATA_HOME='/x/data')
        self.assertEqual(engine_paths('default', 9228),
                         (pathlib.Path('/x/data/apple-music/chrome'), 9228))
        self.assertEqual(engine_paths('development', 9228),
                         (pathlib.Path('/x/data/apple-music/chrome-devel'), 9229))
        self.assertEqual(engine_paths('development', 9300),
                         (pathlib.Path('/x/data/apple-music/chrome-devel'), 9301))

    def test_environment_wins(self):
        self.env(XDG_DATA_HOME='/x/data', APPLE_MUSIC_PROFILE='/o/profile',
                 APPLE_MUSIC_PORT='9400')
        self.assertEqual(engine_paths('development', 9228), (pathlib.Path('/o/profile'), 9400))
        self.assertEqual(engine_paths('default', 9228), (pathlib.Path('/o/profile'), 9400))

    def test_state_file_follows_the_profile(self):
        self.env(XDG_DATA_HOME='/x/data', XDG_RUNTIME_DIR='/x/run')
        release = Engine(*engine_paths('default', 9228), demo=True)
        devel = Engine(*engine_paths('development', 9228), demo=True)
        self.assertEqual(release.state_file, pathlib.Path('/x/run/apple-music/engine.json'))
        self.assertEqual(devel.state_file,
                         pathlib.Path('/x/data/apple-music/chrome-devel/engine.json'))
        self.assertEqual(config.state_file(devel.profile_dir), devel.state_file)


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
        self.assertEqual(item_endpoint('song', '5', 'us'), '/v1/catalog/us/songs/5')
        self.assertEqual(item_endpoint('video', '7', 'us'), '/v1/catalog/us/videos/7')


if __name__ == '__main__':
    unittest.main()
