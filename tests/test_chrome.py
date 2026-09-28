"""Unit tests for src/backend/chrome.py and errors.py: the argv, finding Chrome, which Chrome
holds a profile, picking the page target, and a DevTools port's /json (against a local HTTP
server)."""

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

from applemusic.backend import chrome
from applemusic.backend.errors import EngineError

SAMPLE_TARGETS = [
    {'type': 'page', 'url': 'chrome://newtab/', 'targetId': 'A'},
    {'type': 'service_worker', 'url': 'https://music.apple.com/sw.js', 'targetId': 'B'},
    {'type': 'page', 'url': 'https://music.apple.com/us/new', 'targetId': 'C',
     'title': 'Apple Music'},
    {'type': 'page', 'url': 'https://music.apple.com/us/browse', 'targetId': 'D'},
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
        args = chrome.chrome_args('/opt/google/chrome/chrome', '/p/chrome', headless=True)
        self.assertEqual(args[0], '/opt/google/chrome/chrome')
        self.assertIn('--user-data-dir=/p/chrome', args)
        self.assertIn('--remote-debugging-pipe', args)
        self.assertFalse([arg for arg in args if arg.startswith(('--remote-debugging-port',
                                                                '--remote-debugging-address'))])
        self.assertIn('--autoplay-policy=no-user-gesture-required', args)
        self.assertIn('--disable-features=HardwareMediaKeyHandling', args)
        self.assertIn('--no-first-run', args)
        self.assertIn('--no-default-browser-check', args)
        self.assertIn('--headless=new', args)
        self.assertEqual(args[-1], 'https://music.apple.com/')
        self.assertNotIn('--app=https://music.apple.com/', args)

    def test_visible(self):
        args = chrome.chrome_args('chrome', pathlib.Path('/p/chrome'), headless=False)
        self.assertNotIn('--headless=new', args)
        self.assertEqual(args[-1], '--app=https://music.apple.com/')
        self.assertIn('--user-data-dir=/p/chrome', args)
        self.assertIn('--remote-debugging-pipe', args)
        self.assertIn('--disable-features=HardwareMediaKeyHandling', args)

    def test_debug_port(self):
        args = chrome.chrome_args('chrome', '/p/chrome', debug_port=9300)
        self.assertIn('--remote-debugging-pipe', args)
        self.assertIn('--remote-debugging-port=9300', args)
        self.assertIn('--remote-debugging-address=127.0.0.1', args)

    def test_host_through_flatpak_spawn(self):
        args = chrome.chrome_args('/usr/bin/google-chrome', '/p/chrome', headless=True,
                                  host=True)
        self.assertEqual(args[:6], ['flatpak-spawn', '--host', '--watch-bus', '--forward-fd=3',
                                    '--forward-fd=4', '/usr/bin/google-chrome'])
        self.assertEqual(args[6:], chrome.chrome_args('/usr/bin/google-chrome', '/p/chrome',
                                                      headless=True, host=False)[1:])

    def test_host_follows_the_sandbox(self):
        with mock.patch.object(chrome, 'in_flatpak', lambda: True):
            self.assertEqual(chrome.chrome_args('chrome', '/p')[0], 'flatpak-spawn')
        with mock.patch.object(chrome, 'in_flatpak', lambda: False):
            self.assertEqual(chrome.chrome_args('chrome', '/p')[0], 'chrome')


class HostChromeTest(unittest.TestCase):
    """find_chrome() in a Flatpak sandbox: the host's shell resolves the names. The runner
    stands in for flatpak-spawn by running the host part here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = pathlib.Path(self.tmp.name)
        self.calls = []

    def run_here(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertEqual(argv[:2], ['flatpak-spawn', '--host'])
        return subprocess.run(argv[2:], **kwargs)

    def executable(self, name, mode=0o755):
        path = self.bin / name
        path.write_text('#!/bin/sh\n')
        path.chmod(mode)
        return str(path)

    def test_first_executable_name(self):
        self.executable('not-executable', 0o644)
        second = self.executable('second')
        names = [str(self.bin / 'missing'), str(self.bin / 'not-executable'), second,
                 self.executable('third')]
        self.assertEqual(chrome.find_host_chrome(names, run=self.run_here), second)

    def test_none_when_the_host_has_none(self):
        self.assertIsNone(chrome.find_host_chrome([str(self.bin / 'missing')],
                                                  run=self.run_here))

    def test_none_when_flatpak_spawn_fails(self):
        def fails(argv, **kwargs):
            raise FileNotFoundError('flatpak-spawn')
        with self.assertLogs(chrome.log, 'WARNING'):
            self.assertIsNone(chrome.find_host_chrome(['google-chrome'], run=fails))

    def test_find_chrome_asks_the_host_in_the_sandbox(self):
        seen = []
        with mock.patch.object(chrome, 'in_flatpak', lambda: True), \
                mock.patch.object(chrome, 'find_host_chrome',
                                  lambda names: seen.append(names) or '/usr/bin/x'):
            self.assertEqual(chrome.find_chrome('my-chrome'), '/usr/bin/x')
        self.assertEqual(seen, [['my-chrome', *chrome.CANDIDATES]])


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


def sleeper(*args):
    """A live process whose /proc cmdline carries `args` (waited for: it is empty, then the
    runner's, for an instant while the child execs)."""
    process = subprocess.Popen([sys.executable, '-S', '-c', 'import time; time.sleep(30)', *args])
    deadline = time.monotonic() + 2
    wanted = os.fsencode(args[-1]) if args else b'time.sleep'
    while time.monotonic() < deadline:
        try:
            if wanted in pathlib.Path(f'/proc/{process.pid}/cmdline').read_bytes():
                break
        except OSError:
            pass
        time.sleep(0.005)
    return process


def stop(process):
    if process.poll() is None:
        process.kill()
    process.wait()


class CmdlineTest(unittest.TestCase):
    """cmdline_names_profile on the command lines Chrome's processes really have: the browser
    rewrites its own into one space-joined string."""

    def test_chrome_s_rewritten_browser_line(self):
        line = (b'/opt/google/chrome/chrome --user-data-dir=/x/chrome --remote-debugging-pipe '
                b'--headless=new https://music.apple.com/\0')
        self.assertTrue(chrome.cmdline_names_profile(line, '/x/chrome'))
        self.assertTrue(chrome.cmdline_names_profile(line, pathlib.Path('/x/chrome')))

    def test_another_profile_with_the_same_prefix(self):
        line = b'/opt/google/chrome/chrome --user-data-dir=/x/chrome-devel --headless=new\0'
        self.assertFalse(chrome.cmdline_names_profile(line, '/x/chrome'))
        self.assertTrue(chrome.cmdline_names_profile(line, '/x/chrome-devel'))

    def test_a_helper_is_not_the_browser(self):
        line = (b'/opt/google/chrome/chrome --type=renderer --user-data-dir=/x/chrome '
                b'--lang=en-GB\0\0\0')
        self.assertFalse(chrome.cmdline_names_profile(line, '/x/chrome'))

    def test_a_plain_argv(self):
        argv = b'\0'.join([b'/opt/google/chrome/chrome', b'--user-data-dir=/x/chrome',
                           b'--remote-debugging-pipe']) + b'\0'
        self.assertTrue(chrome.cmdline_names_profile(argv, '/x/chrome'))
        self.assertFalse(chrome.cmdline_names_profile(argv, '/x/chrom'))
        self.assertTrue(chrome.cmdline_names_profile(b'chrome --user-data-dir=/x/chrome',
                                                     '/x/chrome'))


class PidAliveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = pathlib.Path(self.tmp.name) / 'chrome'

    def test_alive(self):
        self.assertTrue(chrome.pid_alive(os.getpid()))
        # This process: alive, but not a Chrome on the profile.
        self.assertFalse(chrome.pid_alive(os.getpid(), self.profile))
        proc = subprocess.Popen([sys.executable, '-S', '-c', 'pass'])
        proc.wait()
        self.assertFalse(chrome.pid_alive(proc.pid))
        self.assertFalse(chrome.pid_alive(0))
        self.assertFalse(chrome.pid_alive(-1))

    @unittest.skipUnless(os.path.isdir('/proc'), 'needs /proc')
    def test_alive_checks_the_profile_on_the_command_line(self):
        proc = sleeper(f'--user-data-dir={self.profile}')
        devel = sleeper(f'--user-data-dir={self.profile}-devel')
        try:
            self.assertTrue(chrome.pid_alive(proc.pid, self.profile))
            self.assertFalse(chrome.pid_alive(proc.pid, '/elsewhere'))
            self.assertFalse(chrome.pid_alive(devel.pid, self.profile))
        finally:
            stop(proc)
            stop(devel)
        self.assertFalse(chrome.pid_alive(proc.pid, self.profile))


@unittest.skipUnless(os.path.isdir('/proc'), 'needs /proc')
class ProfileOwnerTest(unittest.TestCase):
    """profile_owner: the Chrome SingletonLock names, when it really is one on the profile."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = pathlib.Path(self.tmp.name) / 'chrome'
        self.profile.mkdir()
        patcher = mock.patch.object(chrome, 'in_flatpak', lambda: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lock(self, target):
        (self.profile / 'SingletonLock').symlink_to(target)

    def test_the_chrome_the_lock_names(self):
        proc = sleeper(f'--user-data-dir={self.profile}')
        self.addCleanup(stop, proc)
        self.lock(f'{socket.gethostname()}-{proc.pid}')
        self.assertEqual(chrome.profile_owner(self.profile), proc.pid)
        stop(proc)
        self.assertIsNone(chrome.profile_owner(self.profile))  # a stale lock

    def test_no_lock(self):
        self.assertIsNone(chrome.profile_owner(self.profile))
        self.assertIsNone(chrome.profile_owner(pathlib.Path(self.tmp.name) / 'missing'))

    def test_another_host_s_lock(self):
        proc = sleeper(f'--user-data-dir={self.profile}')
        self.addCleanup(stop, proc)
        self.lock(f'elsewhere.example-{proc.pid}')
        self.assertIsNone(chrome.profile_owner(self.profile))

    def test_a_pid_that_is_no_chrome_on_the_profile(self):
        self.lock(f'{socket.gethostname()}-{os.getpid()}')  # this test runner
        self.assertIsNone(chrome.profile_owner(self.profile))
        other = sleeper(f'--user-data-dir={self.profile}-devel')
        self.addCleanup(stop, other)
        (self.profile / 'SingletonLock').unlink()
        self.lock(f'{socket.gethostname()}-{other.pid}')
        self.assertIsNone(chrome.profile_owner(self.profile))

    def test_a_malformed_lock(self):
        self.lock('not-a-pid')
        self.assertIsNone(chrome.profile_owner(self.profile))

    def test_none_in_a_flatpak(self):
        proc = sleeper(f'--user-data-dir={self.profile}')
        self.addCleanup(stop, proc)
        self.lock(f'{socket.gethostname()}-{proc.pid}')
        with mock.patch.object(chrome, 'in_flatpak', lambda: True):
            self.assertIsNone(chrome.profile_owner(self.profile))


class SelectPageTest(unittest.TestCase):
    def test_first_music_page(self):
        self.assertEqual(chrome.select_page(SAMPLE_TARGETS)['targetId'], 'C')

    def test_needs_a_page(self):
        self.assertIsNone(chrome.select_page([]))
        self.assertIsNone(chrome.select_page(SAMPLE_TARGETS[:2]))
        self.assertEqual(chrome.select_page(SAMPLE_TARGETS[3:])['targetId'], 'D')

    def test_url_must_be_music_apple_com(self):
        for url in ('https://example.com/music.apple.com', 'https://music.apple.com.example.net/',
                    'http://music.apple.com/', 'https://evil.com/?https://music.apple.com/',
                    'https://[bad', ''):
            with self.subTest(url=url):
                self.assertIsNone(chrome.select_page([{'type': 'page', 'targetId': 'X',
                                                       'url': url}]))
        self.assertTrue(chrome.is_music_url('https://music.apple.com/us/album/x?i=1'))


class DevToolsHttpTest(unittest.IsolatedAsyncioTestCase):
    """get_json against a local /json server: the developer attach's /json/version."""

    def setUp(self):
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append(self.path)
                if self.path != '/json/version':
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps({'Browser': 'Chrome/154.0',
                                   'webSocketDebuggerUrl': 'ws://x/browser'}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.requests = []
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

    async def test_get_json(self):
        version = await chrome.get_json(self.port, '/json/version', timeout=2)
        self.assertEqual(version['Browser'], 'Chrome/154.0')
        self.assertEqual(self.requests, ['/json/version'])

    async def test_get_json_on_a_closed_port_is_engine_down(self):
        with self.assertRaises(EngineError) as ctx:
            await chrome.get_json(self.free_port(), '/json/version')
        self.assertEqual(ctx.exception.code, 'engine-down')


if __name__ == '__main__':
    unittest.main()
