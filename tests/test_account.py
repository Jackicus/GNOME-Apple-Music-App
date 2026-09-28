"""Signing out and clearing the cache (src/account.py) with a stand-in app: the order of the
steps (the session revoked, the cache's generation bumped, the sync cancelled, the engine
stopped, only then the wipe), what is wiped and what is left, and the settings forgotten.
No engine, no display; the cache and the profile are temporary directories.
"""

import asyncio
import contextlib
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import gi

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)
from tests.gtk import SCHEMA_ID

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gio, GObject  # noqa: E402

from applemusic import account  # noqa: E402
from applemusic.backend import store  # noqa: E402
from applemusic.backend.errors import EngineError  # noqa: E402

ACCOUNT_KEYS = ('signed-in', 'account-name', 'last-sync', 'expanded-folders', 'last-page')


class FakeEngine(GObject.Object):
    state = GObject.Property(type=str, default='up')
    authorized = GObject.Property(type=bool, default=True)
    headless = GObject.Property(type=bool, default=True)

    def __init__(self, calls, profile_dir, state='up'):
        super().__init__()
        self.calls = calls
        self.profile_dir = profile_dir
        self.state = state
        self.start_fails = False

    async def start(self, visible=None):
        self.calls.append(('start', visible))
        if self.start_fails:
            raise EngineError('no-browser', 'no Chrome here')
        self.state = 'up'

    async def unauthorize(self, timeout=None):
        self.calls.append('unauthorize')
        return True

    async def stop(self, grace=None):
        self.calls.append('stop')
        self.state = 'down'


class FakeSync:
    def __init__(self, calls):
        self.calls = calls
        self.holds = 0
        self.held_while = []

    @contextlib.contextmanager
    def held(self):
        self.holds += 1
        try:
            yield
        finally:
            self.holds -= 1

    async def cancel(self):
        self.calls.append('cancel')
        self.held_while.append(self.holds > 0)

    def start(self):
        self.calls.append('sync')


class FakeLibrary:
    def __init__(self, calls):
        self.calls = calls

    async def load(self):
        self.calls.append('load')


class FakeMpris:
    def __init__(self, calls):
        self.calls = calls

    def refresh_art(self):
        self.calls.append('refresh-art')


class FakeWindow:
    def __init__(self, calls):
        self.calls = calls

    def forget_account_pages(self):
        self.calls.append('forget-pages')


class FakeApp:
    def __init__(self, calls, profile_dir, engine_state='up'):
        self.calls = calls
        self.demo = False
        self.signing_out = False
        self.settings = Gio.Settings.new(SCHEMA_ID)
        self.engine = FakeEngine(calls, profile_dir, engine_state)
        self.library_sync = FakeSync(calls)
        self.library = FakeLibrary(calls)
        self.mpris = FakeMpris(calls)
        self.window = FakeWindow(calls)
        self.toasts = []

    def get_active_window(self):
        return self.window

    def toast(self, title, button_label=None, action_name=None):
        self.toasts.append(title)


class AccountTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.cache = root / 'cache'
        self.profile = root / 'profile'
        for path in (self.cache / 'art', self.profile / 'Default'):
            path.mkdir(parents=True)
        for path in (self.cache / 'library.json', self.cache / 'art' / 'cover.jpg',
                     self.cache / '.library.json.x1.tmp', self.cache / 'notes.txt',
                     self.profile / 'Default' / 'Cookies'):
            path.write_text('invented')
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        # The steps that change the disk, recorded in the order they run.
        wipe, bump = account._wipe, store.bump_cache_generation
        for target, name, original, label in ((account, '_wipe', wipe, 'wipe'),
                                              (account.store, 'bump_cache_generation', bump,
                                               'bump')):
            def recorded(*args, _original=original, _label=label):
                self.calls.append(_label)
                return _original(*args)
            patcher = mock.patch.object(target, name, recorded)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_app(self, engine_state='up'):
        app = FakeApp(self.calls, self.profile, engine_state)
        for key in ACCOUNT_KEYS:
            self.addCleanup(app.settings.reset, key)
        app.settings.set_boolean('signed-in', True)
        app.settings.set_string('account-name', 'Invented Person')
        app.settings.set_string('last-sync', '2026-09-28T10:00:00+00:00')
        app.settings.set_strv('expanded-folders', ['p.fldA1'])
        return app


class SignOutTest(AccountTestCase):
    async def test_the_order_and_what_is_left(self):
        app = self.make_app()
        app.settings.set_string('last-page', 'playlist:p.pl123')
        await account.sign_out(app)
        self.assertEqual(self.calls, ['unauthorize', 'bump', 'cancel', 'stop', 'wipe',
                                      'refresh-art', 'load', 'forget-pages'])
        self.assertEqual(app.library_sync.held_while, [True])
        self.assertEqual(app.toasts, ['Signed out'])
        self.assertFalse(app.signing_out)
        for key in ACCOUNT_KEYS:
            self.assertFalse(app.settings.get_user_value(key), key)
        # The cache's entries and temporary files are gone, not the directory or a file of
        # someone else's; the profile is gone.
        self.assertEqual(sorted(os.listdir(self.cache)), ['notes.txt'])
        self.assertFalse(self.profile.exists())

    async def test_a_destination_stays_the_last_page(self):
        app = self.make_app()
        app.settings.set_string('last-page', 'albums')
        await account.sign_out(app)
        self.assertEqual(app.settings.get_string('last-page'), 'albums')

    async def test_a_stopped_engine_is_started_to_revoke(self):
        app = self.make_app(engine_state='down')
        await account.sign_out(app)
        self.assertEqual(self.calls[:3], [('start', False), 'unauthorize', 'bump'])

    async def test_an_engine_that_will_not_start_does_not_stop_the_wipe(self):
        app = self.make_app(engine_state='down')
        app.engine.start_fails = True
        with self.assertLogs('applemusic.account', 'WARNING'):
            await account.sign_out(app)
        self.assertNotIn('unauthorize', self.calls)
        self.assertIn('wipe', self.calls)
        self.assertFalse(app.settings.get_boolean('signed-in'))

    async def test_a_slow_start_is_bounded(self):
        app = self.make_app(engine_state='down')

        async def never(visible=None):
            await asyncio.get_running_loop().create_future()

        app.engine.start = never
        with mock.patch.object(account, 'REVOKE_TIMEOUT', 0.05), \
                self.assertLogs('applemusic.account', 'WARNING'):
            await account.sign_out(app)
        self.assertIn('wipe', self.calls)

    async def test_signed_out_already_starts_nothing(self):
        app = self.make_app(engine_state='down')
        app.settings.set_boolean('signed-in', False)
        await account.sign_out(app)
        self.assertEqual(self.calls[0], 'bump')

    async def test_demo_does_nothing(self):
        app = self.make_app()
        app.demo = True
        await account.sign_out(app)
        self.assertEqual(self.calls, [])
        self.assertTrue((self.cache / 'library.json').exists())


class ClearCacheTest(AccountTestCase):
    async def test_the_order_and_what_is_left(self):
        app = self.make_app()
        self.assertTrue(await account.clear_cache(app))
        self.assertEqual(self.calls, ['bump', 'cancel', 'refresh-art', 'load', 'sync'])
        self.assertEqual(app.library_sync.held_while, [True])
        self.assertEqual(sorted(os.listdir(self.cache)), ['notes.txt'])  # no *.tmp either
        self.assertTrue(self.profile.exists())
        self.assertEqual(app.settings.get_string('last-sync'), '')
        self.assertTrue(app.settings.get_boolean('signed-in'))

    async def test_signed_out_syncs_nothing(self):
        app = self.make_app()
        app.settings.set_boolean('signed-in', False)
        await account.clear_cache(app)
        self.assertNotIn('sync', self.calls)

    async def test_demo_does_nothing(self):
        app = self.make_app()
        app.demo = True
        self.assertFalse(await account.clear_cache(app))
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
