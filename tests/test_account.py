# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

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
        # Sign-in: what signin() does (None: succeed), whether the window is closed right
        # after, the name the page shows, and a future the name read waits for.
        self.signin_error = None
        self.signin_waits = False
        self.closed_after_signin = False
        self.name = 'Invented Person'
        self.name_gate = None
        self.unauthorize_waits = False
        self.refuse_starts = None
        self.refused_at_stop = None

    async def start(self, visible=None):
        self.calls.append(('start', visible))
        if self.start_fails:
            raise EngineError('no-browser', 'no Chrome here')
        self.state = 'up'
        if visible is not None:
            self.headless = not visible

    async def restart(self, visible=None):
        self.calls.append(('restart', visible))
        await self.stop()
        await self.start(visible)

    async def signin(self, timeout=None):
        self.calls.append('signin')
        if self.signin_waits:
            await asyncio.get_running_loop().create_future()
        if self.signin_error is not None:
            raise self.signin_error
        self.authorized = True
        if self.closed_after_signin:
            self.state = 'down'  # the person closed Chrome's window at once
            self.authorized = False
        return True

    async def account_name(self, wait=0):
        self.calls.append('account-name')
        if self.name_gate is not None:
            await self.name_gate
        if self.state == 'down':
            raise EngineError('engine-down', 'the engine is not running')
        return self.name

    async def unauthorize(self, timeout=None):
        self.calls.append('unauthorize')
        if self.unauthorize_waits:
            await asyncio.get_running_loop().create_future()
        return True

    async def stop(self, grace=None):
        self.calls.append('stop')
        self.refused_at_stop = self.refuse_starts
        self.state = 'down'


class FakeSync:
    def __init__(self, calls):
        self.calls = calls
        self.holds = 0
        self.held_while = []

    def hold(self):
        self.holds += 1

    def release(self):
        self.holds -= 1

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
        self.calls.append(('sync', self.holds))


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
    def account_key(self, name):
        return name  # the release build's keys

    def __init__(self, calls, profile_dir, engine_state='up'):
        self.calls = calls
        self.demo = False
        self.signing_in = False
        self.signing_out = False
        self.reported = []
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

    def report(self, error):
        self.reported.append(error.code)

    def spawn(self, coro):
        return asyncio.get_running_loop().create_task(coro)


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
        # The sync first (revoking the session would fail it), the generation bumped before
        # the wipe, and once more at the end for what a page fetched meanwhile.
        self.assertEqual(self.calls, ['cancel', 'unauthorize', 'bump', 'stop', 'wipe',
                                      'refresh-art', 'load', 'forget-pages', 'bump'])
        self.assertEqual(app.library_sync.held_while, [True])
        # No Chrome may start while the profile goes; after, it may again.
        self.assertEqual(app.engine.refused_at_stop, 'signing out')
        self.assertIsNone(app.engine.refuse_starts)
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
        self.assertEqual(self.calls[:4], ['cancel', ('start', False), 'unauthorize', 'bump'])

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
        self.assertEqual(self.calls[:2], ['cancel', 'bump'])

    async def test_quitting_cuts_the_revocation_short_and_the_wipe_goes_on(self):
        app = self.make_app()
        app.engine.unauthorize_waits = True
        task = asyncio.ensure_future(account.sign_out(app))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertIsNotNone(app.revocation)
        app.revocation.cancel()  # as the quit does
        with self.assertLogs('applemusic.account', 'INFO'):
            await task
        self.assertIn('wipe', self.calls)
        self.assertFalse(app.settings.get_boolean('signed-in'))
        self.assertFalse(self.profile.exists())
        self.assertIsNone(app.revocation)

    async def test_demo_does_nothing(self):
        app = self.make_app()
        app.demo = True
        await account.sign_out(app)
        self.assertEqual(self.calls, [])
        self.assertTrue((self.cache / 'library.json').exists())


class SignInTest(AccountTestCase):
    def make_app(self, engine_state='down'):
        app = super().make_app(engine_state)
        for key in ('signed-in', 'account-name'):
            app.settings.reset(key)
        self.statuses = []
        return app

    async def settle(self):
        for _ in range(10):
            await asyncio.sleep(0)

    async def test_signed_in_is_the_commit_and_the_rest_follows(self):
        app = self.make_app()
        self.assertTrue(await account.sign_in(app, self.statuses.append))
        self.assertTrue(app.settings.get_boolean('signed-in'))
        self.assertEqual(app.toasts, ['Signed in'])
        self.assertEqual(self.statuses, ['Starting Chrome…',
                                         'Sign in with your Apple ID in the Chrome window',
                                         'Signed in'])
        self.assertTrue(app.signing_in)  # the rest runs on
        await self.settle()
        self.assertEqual(app.settings.get_string('account-name'), 'Invented Person')
        self.assertEqual(self.calls, ['cancel', ('restart', True), 'stop', ('start', True),
                                      'signin', 'account-name', ('start', False),
                                      ('sync', 0)])
        self.assertTrue(app.engine.headless)
        self.assertFalse(app.signing_in)
        self.assertEqual(app.library_sync.holds, 0)

    async def test_the_window_closed_after_signing_in_changes_nothing(self):
        app = self.make_app()
        app.engine.closed_after_signin = True
        self.assertTrue(await account.sign_in(app, self.statuses.append))
        await self.settle()
        self.assertEqual((app.engine.state, app.engine.headless), ('up', True))
        self.assertTrue(app.settings.get_boolean('signed-in'))
        self.assertEqual(self.calls.count(('sync', 0)), 1)
        self.assertEqual(app.toasts, ['Signed in'])
        self.assertEqual(app.reported, [])

    async def test_a_close_during_the_name_read_does_not_stop_the_engine(self):
        app = self.make_app()
        app.engine.name_gate = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(account.sign_in(app, self.statuses.append))
        await self.settle()
        self.assertTrue(task.done())  # committed: the dialog closes, nothing left to cancel
        task.cancel()
        await self.settle()
        self.assertNotIn('stop', self.calls[self.calls.index('signin'):])
        app.engine.name_gate.set_result(None)
        await self.settle()
        self.assertEqual(self.calls[-1], ('sync', 0))

    async def test_cancelled_before_the_commit_stops_the_engine(self):
        app = self.make_app()
        app.engine.signin_waits = True
        task = asyncio.ensure_future(account.sign_in(app, self.statuses.append))
        await self.settle()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.settle()
        self.assertEqual(self.calls[-1], 'stop')
        self.assertFalse(app.settings.get_boolean('signed-in'))
        self.assertEqual((app.signing_in, app.library_sync.holds), (False, 0))
        self.assertEqual(app.toasts, [])

    async def test_failures_in_words_for_a_sign_in(self):
        for error, told in ((EngineError('timeout', 'ten minutes'), 'Sign-in timed out'),
                            (EngineError('engine-down', 'closed'),
                             'Sign-in cancelled: the Chrome window was closed')):
            self.calls.clear()
            app = self.make_app()
            app.engine.signin_error = error
            with self.assertLogs('applemusic.account', 'WARNING'):
                self.assertFalse(await account.sign_in(app, self.statuses.append))
            await self.settle()
            self.assertEqual(app.toasts, [told])
            self.assertEqual(self.calls[-1], 'stop')
            self.assertFalse(app.settings.get_boolean('signed-in'))
            self.assertEqual((app.signing_in, app.library_sync.holds), (False, 0))

    async def test_no_chrome_is_reported(self):
        app = self.make_app()
        app.engine.start_fails = True
        with self.assertLogs('applemusic.account', 'WARNING'):
            self.assertFalse(await account.sign_in(app, self.statuses.append))
        self.assertEqual(app.reported, ['no-browser'])


class ClearCacheTest(AccountTestCase):
    async def test_the_order_and_what_is_left(self):
        app = self.make_app()
        self.assertTrue(await account.clear_cache(app))
        self.assertEqual(self.calls, ['bump', 'cancel', 'refresh-art', 'load', ('sync', 0)])
        self.assertEqual(app.library_sync.held_while, [True])
        self.assertEqual(sorted(os.listdir(self.cache)), ['notes.txt'])  # no *.tmp either
        self.assertTrue(self.profile.exists())
        self.assertEqual(app.settings.get_string('last-sync'), '')
        self.assertTrue(app.settings.get_boolean('signed-in'))

    async def test_signed_out_syncs_nothing(self):
        app = self.make_app()
        app.settings.set_boolean('signed-in', False)
        await account.clear_cache(app)
        self.assertNotIn(('sync', 0), self.calls)

    async def test_demo_does_nothing(self):
        app = self.make_app()
        app.demo = True
        self.assertFalse(await account.clear_cache(app))
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
