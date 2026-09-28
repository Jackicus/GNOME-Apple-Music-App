"""The Application (src/main.py) built without being run: its settings on the memory backend,
stand-ins for the engine, the player and the library, no window. What it decides on its own:
when a sync may start, how it quits (where errors go: tests/test_errors.py).
"""

import asyncio
import unittest
from unittest import mock

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from gi.repository import GObject

from applemusic import main
from applemusic.backend import config
from applemusic.sync import LibrarySync

BASE_ID = 'io.github.jackicus.AppleMusic'


class FakeEngine(GObject.Object):
    """The Engine as the app sees it: `state`, `authorized`, `headless`, the `lost` signal,
    and start/stop/kill that record themselves."""

    __gsignals__ = {
        'lost': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    state = GObject.Property(type=str, default='down')
    authorized = GObject.Property(type=bool, default=False)
    headless = GObject.Property(type=bool, default=True)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.pid = None
        self.demo = False
        self.start_blocks = False
        self.stop_blocks = False

    async def start(self, visible=None):
        self.calls.append('start')
        if self.start_blocks:
            await asyncio.get_running_loop().create_future()
        self.state = 'up'
        self.authorized = True

    async def stop(self, grace=None):
        self.calls.append(('stop', grace))
        if self.stop_blocks:
            await asyncio.get_running_loop().create_future()
        self.state = 'down'
        self.authorized = False

    def kill(self):
        self.calls.append('kill')
        self.state = 'down'


class FakeLibrary(GObject.Object):
    syncing = GObject.Property(type=bool, default=False)
    file_state = GObject.Property(type=str, default='ok')
    version = GObject.Property(type=int, default=2)

    def song_count(self):
        return 0


def make_app(profile='default', demo=False):
    """An Application as do_startup would make it, with stand-ins, never run. The build
    profile config.set_build_profile() takes is put back after the test by the caller
    (tearDown). (The first application a process makes becomes its default:
    test_page_lifetime's stand-in makes itself the default again.)"""
    app = main.Application('0.9.0', f'{BASE_ID}.Test', BASE_ID, profile, None)
    app.demo = demo
    app.engine = FakeEngine()
    app.library = FakeLibrary()
    app.toasts = []
    app.toast = lambda title, button_label=None, action_name=None: app.toasts.append(
        (title, button_label, action_name))
    app.library_sync = LibrarySync(app)
    return app


class AppTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(config.set_build_profile, 'default')
        self.app = make_app()
        for key in ('signed-in', 'account-name', 'last-sync', 'sync-interval'):
            self.addCleanup(self.app.settings.reset, key)


class StartSyncTest(AppTestCase):
    def test_demo_refuses(self):
        self.app.demo = True
        self.assertIsNone(self.app.start_sync())
        self.assertEqual(self.app.toasts, [('Not available with the demo library', None, None)])
        self.assertEqual(self.app.engine.calls, [])


class ConstructionTest(unittest.TestCase):
    def test_the_profile_sets_the_paths(self):
        self.addCleanup(config.set_build_profile, 'default')
        with mock.patch.dict('os.environ', {'APPLE_MUSIC_CACHE': ''}):
            make_app(profile='development')
            self.assertTrue(str(config.cache_dir()).endswith('apple-music-devel'))
            make_app(profile='default')
            self.assertTrue(str(config.cache_dir()).endswith('apple-music'))


if __name__ == '__main__':
    unittest.main()
