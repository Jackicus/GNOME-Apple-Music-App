"""Unit tests for src/backend/chrome.py and errors.py: the argv, the state file, finding Chrome,
picking the page target, and the DevTools polling (against a local HTTP server)."""

import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import SRC  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend import chrome, config
from applemusic.backend.errors import EngineError

SAMPLE_TARGETS = [
    {'type': 'page', 'url': 'chrome://newtab/', 'id': 'A',
     'webSocketDebuggerUrl': 'ws://127.0.0.1:9228/devtools/page/A'},
    {'type': 'service_worker', 'url': 'https://music.apple.com/sw.js', 'id': 'B',
     'webSocketDebuggerUrl': 'ws://127.0.0.1:9228/devtools/page/B'},
    {'type': 'page', 'url': 'https://music.apple.com/us/new', 'id': 'C', 'title': 'Apple Music',
     'webSocketDebuggerUrl': 'ws://127.0.0.1:9228/devtools/page/C'},
    {'type': 'page', 'url': 'https://music.apple.com/us/browse', 'id': 'D',
     'webSocketDebuggerUrl': 'ws://127.0.0.1:9228/devtools/page/D'},
]


class EngineErrorTest(unittest.TestCase):
    def test_codes(self):
        for code in ('engine-down', 'not-signed-in', 'api', 'timeout', 'usage'):
            error = EngineError(code, 'why')
            self.assertEqual((error.code, error.message, str(error)), (code, 'why', f'{code}: why'))
        self.assertEqual(str(EngineError('timeout')), 'timeout')
        with self.assertRaises(ValueError):
            EngineError('bogus', 'no such code')


class ChromeArgsTest(unittest.TestCase):
    def test_headless(self):
        args = chrome.chrome_args('/opt/google/chrome/chrome', '/p/chrome', 9228, headless=True)
        self.assertEqual(args[0], '/opt/google/chrome/chrome')
        self.assertIn('--user-data-dir=/p/chrome', args)
        self.assertIn('--remote-debugging-port=9228', args)
        self.assertIn('--remote-debugging-address=127.0.0.1', args)
        self.assertIn('--autoplay-policy=no-user-gesture-required', args)
        self.assertIn('--disable-features=HardwareMediaKeyHandling', args)
        self.assertIn('--no-first-run', args)
        self.assertIn('--no-default-browser-check', args)
        self.assertIn('--headless=new', args)
        self.assertEqual(args[-1], 'https://music.apple.com/')
        self.assertNotIn('--app=https://music.apple.com/', args)

    def test_visible(self):
        args = chrome.chrome_args('chrome', pathlib.Path('/p/chrome'), 9229, headless=False)
        self.assertNotIn('--headless=new', args)
        self.assertEqual(args[-1], '--app=https://music.apple.com/')
        self.assertIn('--user-data-dir=/p/chrome', args)
        self.assertIn('--remote-debugging-port=9229', args)
        self.assertIn('--disable-features=HardwareMediaKeyHandling', args)


class FindChromeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = pathlib.Path(self.tmp.name) / 'bin'
        self.bin.mkdir()
        self.opt = pathlib.Path(self.tmp.name) / 'opt' / 'chrome'
        # The absolute candidate is this directory's, so the real /opt/google/chrome stays out.
        patcher = mock.patch.object(
            chrome, 'CANDIDATES', ('google-chrome-stable', 'google-chrome', str(self.opt)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n')
        path.chmod(0o755)
        return str(path)

    def test_configured_command_first(self):
        mine = self.executable(self.bin / 'my-chrome')
        self.executable(self.bin / 'google-chrome-stable')
        self.assertEqual(chrome.find_chrome('my-chrome', path=str(self.bin)), mine)

    def test_candidates_in_order(self):
        second = self.executable(self.bin / 'google-chrome')
        self.assertEqual(chrome.find_chrome('missing', path=str(self.bin)), second)
        first = self.executable(self.bin / 'google-chrome-stable')
        self.assertEqual(chrome.find_chrome(None, path=str(self.bin)), first)

    def test_absolute_candidate(self):
        opt = self.executable(self.opt)
        self.assertEqual(chrome.find_chrome(None, path=str(self.bin)), opt)

    def test_none_when_absent(self):
        (self.bin / 'google-chrome').write_text('not executable')
        self.assertIsNone(chrome.find_chrome('missing', path=str(self.bin)))
        self.assertIsNone(chrome.find_chrome(None, path=str(self.bin)))


class EngineStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / 'run' / 'engine.json'

    def test_round_trip(self):
        state = chrome.EngineState(4242, 9228, False, '/p/chrome', started=1700000000)
        state.save(self.path)  # creates the directory
        self.assertEqual(json.loads(self.path.read_text()), {
            'pid': 4242, 'port': 9228, 'headless': False, 'profile': '/p/chrome',
            'started': 1700000000})
        loaded = chrome.EngineState.load(self.path)
        self.assertEqual(loaded.to_dict(), state.to_dict())
        self.assertFalse(list(self.path.parent.glob('*.tmp')))
        chrome.EngineState.remove(self.path)
        self.assertFalse(self.path.exists())
        chrome.EngineState.remove(self.path)  # twice is fine

    def test_started_defaults_to_now(self):
        state = chrome.EngineState(1, 9228, True, '/p')
        self.assertGreater(state.started, 1700000000)

    def test_missing_or_broken_is_none(self):
        self.assertIsNone(chrome.EngineState.load(self.path))
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{not json')
        self.assertIsNone(chrome.EngineState.load(self.path))
        self.path.write_text('{"port": 9228}')  # no pid
        self.assertIsNone(chrome.EngineState.load(self.path))

    def test_alive(self):
        # This process: alive, but not a Chrome on the profile.
        self.assertTrue(chrome.pid_alive(os.getpid()))
        self.assertFalse(chrome.EngineState(os.getpid(), 9228, True, '/p/chrome').alive)
        # A process that has exited.
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])
        proc.wait()
        self.assertFalse(chrome.pid_alive(proc.pid))
        self.assertFalse(chrome.pid_alive(0))
        self.assertFalse(chrome.pid_alive(-1))

    @unittest.skipUnless(os.path.isdir('/proc'), 'needs /proc')
    def test_alive_checks_the_profile_on_the_command_line(self):
        profile = pathlib.Path(self.tmp.name) / 'chrome'
        proc = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(30)', f'--user-data-dir={profile}'])
        try:
            time.sleep(0.2)  # /proc/<pid>/cmdline is empty for an instant while it execs
            self.assertTrue(chrome.EngineState(proc.pid, 9228, True, profile).alive)
            self.assertFalse(chrome.EngineState(proc.pid, 9228, True, '/elsewhere').alive)
        finally:
            proc.kill()
            proc.wait()
        self.assertFalse(chrome.EngineState(proc.pid, 9228, True, profile).alive)


class StateFileTest(unittest.TestCase):
    """config.state_file is keyed by profile: the default profile's is in the runtime directory,
    any other keeps its own inside itself (so a release and a .Devel build never share one)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {
            'XDG_DATA_HOME': '/x/data', 'XDG_RUNTIME_DIR': '/x/run'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('APPLE_MUSIC_PROFILE', None)

    def test_default_profile_uses_the_runtime_dir(self):
        self.assertEqual(config.state_file(), pathlib.Path('/x/run/apple-music/engine.json'))
        self.assertEqual(config.state_file('/x/data/apple-music/chrome'),
                         pathlib.Path('/x/run/apple-music/engine.json'))

    def test_other_profiles_keep_their_own(self):
        self.assertEqual(config.state_file('/x/data/apple-music/chrome-devel'),
                         pathlib.Path('/x/data/apple-music/chrome-devel/engine.json'))
        self.assertEqual(config.state_file(pathlib.Path('/tmp/t/profile')),
                         pathlib.Path('/tmp/t/profile/engine.json'))


class SelectTargetTest(unittest.TestCase):
    def test_first_music_page(self):
        self.assertEqual(chrome.select_target(SAMPLE_TARGETS)['id'], 'C')

    def test_needs_a_page_with_a_debugger_url(self):
        self.assertIsNone(chrome.select_target([]))
        self.assertIsNone(chrome.select_target(SAMPLE_TARGETS[:2]))
        busy = dict(SAMPLE_TARGETS[2])
        del busy['webSocketDebuggerUrl']  # another client is attached
        self.assertIsNone(chrome.select_target([busy]))
        self.assertEqual(chrome.select_target([busy, SAMPLE_TARGETS[3]])['id'], 'D')

    def test_url_must_be_music_apple_com(self):
        other = {'type': 'page', 'url': 'https://example.com/music.apple.com',
                 'webSocketDebuggerUrl': 'ws://x'}
        self.assertIsNone(chrome.select_target([other]))


class DevToolsHttpTest(unittest.IsolatedAsyncioTestCase):
    """wait_for_devtools, list_targets and find_target against a local /json server."""

    def setUp(self):
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append(self.path)
                if self.path == '/json/version':
                    body = {'Browser': 'Chrome/154.0', 'webSocketDebuggerUrl': 'ws://x/browser'}
                elif self.path == '/json/list':
                    body = test.targets
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.requests = []
        self.targets = SAMPLE_TARGETS
        self.httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    @staticmethod
    def free_port():
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]

    async def test_wait_for_devtools(self):
        version = await chrome.wait_for_devtools(self.port, timeout=2)
        self.assertEqual(version['Browser'], 'Chrome/154.0')
        self.assertEqual(self.requests, ['/json/version'])

    async def test_wait_for_devtools_times_out(self):
        with self.assertRaises(EngineError) as ctx:
            await chrome.wait_for_devtools(self.free_port(), timeout=0.5)
        self.assertEqual(ctx.exception.code, 'timeout')

    async def test_get_json_on_a_closed_port_is_engine_down(self):
        with self.assertRaises(EngineError) as ctx:
            await chrome.get_json(self.free_port(), '/json/version')
        self.assertEqual(ctx.exception.code, 'engine-down')

    async def test_list_and_find_target(self):
        targets = await chrome.list_targets(self.port)
        self.assertEqual([t['id'] for t in targets], ['A', 'B', 'C', 'D'])
        self.assertEqual((await chrome.find_target(self.port))['id'], 'C')
        self.targets = SAMPLE_TARGETS[:1]
        self.assertIsNone(await chrome.find_target(self.port))

    async def test_wait_for_target(self):
        self.targets = []
        with self.assertRaises(EngineError) as ctx:
            await chrome.wait_for_target(self.port, timeout=0.5)
        self.assertEqual(ctx.exception.code, 'timeout')
        self.assertGreaterEqual(len(self.requests), 2)  # it polled
        self.targets = SAMPLE_TARGETS
        self.assertEqual((await chrome.wait_for_target(self.port, timeout=1))['id'], 'C')


if __name__ == '__main__':
    unittest.main()
