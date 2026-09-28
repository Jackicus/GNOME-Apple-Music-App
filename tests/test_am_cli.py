"""scripts/am.py: its command line (the modes, --attach), and its commands with a Chrome of
their own, run in this process against test_engine's fake Chrome on a temporary profile.
Nothing here starts a real Chrome or reaches a real profile."""

import contextlib
import importlib.util
import io
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests import ROOT
from tests.test_engine import EngineFixture  # its fake Chrome

from applemusic.backend import chrome
from applemusic.backend.errors import EngineError

_spec = importlib.util.spec_from_file_location('am_cli', ROOT / 'scripts' / 'am.py')
am = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(am)


class AmCliTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('APPLE_MUSIC_DEBUG_PORT', None)

    def refused(self, argv):
        """The SystemExit code and stderr of parsing `argv`."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            am.parse(argv)
        return ctx.exception.code, stderr.getvalue()

    def test_attach_without_a_port_needs_the_variable(self):
        code, message = self.refused(['--attach', 'status'])
        self.assertEqual(code, 2)
        self.assertIn('--attach needs a port, or APPLE_MUSIC_DEBUG_PORT', message)

    def test_attach_takes_the_variable_s_port_or_its_own(self):
        os.environ['APPLE_MUSIC_DEBUG_PORT'] = '9300'
        self.assertEqual(am.parse(['--attach', 'status']).attach, 9300)
        self.assertEqual(am.parse(['--attach', 'eval', '1 + 1']).attach, 9300)
        self.assertEqual(am.parse(['--attach', '9400', 'eval', '1']).attach, 9400)
        self.assertEqual(am.parse(['--attach=9401', 'events']).attach, 9401)
        self.assertIsNone(am.parse(['status']).attach)

    def test_a_bad_port_or_command(self):
        for argv in (['--attach', '70000', 'status'], ['--attach=x', 'status'], ['start'],
                     ['stop'], []):
            with self.subTest(argv=argv):
                self.assertEqual(self.refused(argv)[0], 2)

    def test_help_says_how_the_modes_work(self):
        text = am.build_parser().format_help()
        for words in ('--attach [PORT]', 'APPLE_MUSIC_DEBUG_PORT', 'cannot outlive',
                      'Nothing here signs in', '--devel'):
            self.assertIn(words, text)


class AmOwnChromeTest(EngineFixture):
    """The default mode: a Chrome of the command's own over the pipe, stopped at its end."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        os.environ['APPLE_MUSIC_PROFILE'] = str(self.profile)

    def chrome_gone(self):
        pids = [hello['pid'] for hello in self.chrome.chromes]
        return pids and not any(chrome.pid_alive(pid) for pid in pids)

    async def test_status_reads_the_bridge_and_stops_its_chrome(self):
        self.page.authorized = True
        result = await am.cmd_status(am.parse(['status']))
        self.assertEqual(result['mode'], 'own')
        self.assertIsNone(result['held_by'])
        self.assertTrue(result['bridge']['authorized'])
        self.assertEqual(len(self.chrome.chromes), 1)
        self.assertIn('--remote-debugging-pipe', self.chrome.argv)
        self.assertIn('--headless=new', self.chrome.argv)
        self.assertIn('Browser.close', self.chrome.methods(session=False))
        self.assertTrue(self.chrome_gone())

    async def test_eval_and_now_playing(self):
        self.page.bridge_answers['nowPlaying'] = {'state': 'paused', 'track': None}
        self.assertEqual(await am.cmd_now_playing(am.parse(['now-playing'])),
                         {'state': 'paused', 'track': None})
        self.assertEqual(await am.cmd_eval(am.parse(['eval', '0'])), 0)
        self.assertEqual(len(self.chrome.chromes), 2)  # one each
        self.assertTrue(self.chrome_gone())

    async def test_a_profile_the_app_holds_is_left_alone(self):
        self.profile.mkdir(parents=True)
        app_chrome = subprocess.Popen([sys.executable, '-S', '-c', 'import time; time.sleep(30)',
                                       f'--user-data-dir={self.profile}'])
        self.addCleanup(lambda: (app_chrome.kill(), app_chrome.wait()))
        deadline = time.monotonic() + 2
        while not chrome.pid_alive(app_chrome.pid, self.profile) and time.monotonic() < deadline:
            time.sleep(0.005)
        os.symlink(f'{socket.gethostname()}-{app_chrome.pid}', self.profile / 'SingletonLock')
        result = await am.cmd_status(am.parse(['status']))
        self.assertEqual((result['held_by'], result['bridge']), (app_chrome.pid, None))
        self.assertIn('APPLE_MUSIC_DEBUG_PORT', result['hint'])
        with self.assertRaises(EngineError) as ctx:
            await am.cmd_eval(am.parse(['eval', '1']))
        self.assertEqual(ctx.exception.code, 'usage')
        self.assertIn('use --attach', ctx.exception.message)
        self.assertEqual(self.chrome.chromes, [])  # nothing started
        self.assertIsNone(app_chrome.poll())  # and the app's Chrome untouched
        app_chrome.send_signal(signal.SIGTERM)


if __name__ == '__main__':
    unittest.main()
