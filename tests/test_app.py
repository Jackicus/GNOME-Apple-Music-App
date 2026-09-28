"""The Application (src/main.py) built without being run: its settings on the memory backend,
stand-ins for the engine, the player and the library, no window. What it decides on its own:
when a sync may start, how it quits (where errors go: tests/test_errors.py).
"""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from gi.repository import Gio, GObject

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
        self._lock = asyncio.Lock()  # as the Engine's: a stop waits for a start under way

    async def start(self, visible=None):
        self.calls.append('start')
        async with self._lock:
            if self.start_blocks:
                await asyncio.get_running_loop().create_future()
            self.state = 'up'
            self.authorized = True

    async def stop(self, grace=None):
        self.calls.append(('stop', grace))
        async with self._lock:
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
    app._follow_account()
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

    def test_signed_out_starts_nothing_and_offers_the_sign_in(self):
        asked = []
        self.app.activate_action = lambda name, parameter=None: asked.append(name)
        with self.assertLogs('applemusic.main', 'WARNING'):
            self.assertIsNone(self.app.start_sync())
        self.assertEqual(asked, ['sign-in'])
        self.assertEqual(self.app.engine.calls, [])
        self.assertFalse(self.app.library_sync.props.running)

    def test_the_action_follows_the_account_the_demo_and_the_sync(self):
        action = self.app.lookup_action('sync')
        self.assertFalse(action.get_enabled())  # signed out
        self.app.settings.set_boolean('signed-in', True)
        self.assertTrue(action.get_enabled())
        self.app.library_sync.props.running = True
        self.assertFalse(action.get_enabled())
        self.app.library_sync.props.running = False
        self.assertTrue(action.get_enabled())
        self.app.signing_out = True
        self.assertFalse(action.get_enabled())
        self.app.signing_out = False
        self.app.demo = True
        self.app._update_sync_action()
        self.assertFalse(action.get_enabled())


class QuitTest(AppTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(main.Gio.Application, 'quit',
                                    lambda app: self.app.engine.calls.append('quit'))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_start_under_way_is_cancelled_first(self):
        app = self.app
        app.engine.start_blocks = True
        app._autostart_task = asyncio.ensure_future(app.engine.start())
        await asyncio.sleep(0)
        started = asyncio.get_running_loop().time()
        await app._quit()
        self.assertLess(asyncio.get_running_loop().time() - started, 1.0)
        self.assertTrue(app._autostart_task.cancelled())
        self.assertEqual(app.engine.calls, ['start', ('stop', main.QUIT_GRACE), 'quit'])

    async def test_a_stop_that_hangs_is_killed_then_quits(self):
        app = self.app
        app.engine.stop_blocks = True
        app.engine.pid = 4242
        with mock.patch.object(main, 'QUIT_TIMEOUT', 0.05), \
                self.assertLogs('applemusic.main', 'WARNING'):
            await app._quit()
        self.assertEqual(app.engine.calls, [('stop', main.QUIT_GRACE), 'kill', 'quit'])

    async def test_the_sync_is_stopped_within_the_bound(self):
        app = self.app
        cancelled = []

        async def cancel():
            cancelled.append(True)

        app.library_sync.cancel = cancel
        await app._quit()
        self.assertEqual(cancelled, [True])

    async def test_a_sign_out_under_way_is_let_finish(self):
        app = self.app
        order = []
        revocation = app.revocation = asyncio.ensure_future(
            asyncio.get_running_loop().create_future())

        async def sign_out():
            await asyncio.wait([revocation])  # as account.sign_out() waits for it
            await asyncio.sleep(0.05)  # the wipe
            order.append('wiped')

        async def finish():
            await asyncio.get_running_loop().create_future()  # the name read, say

        app._sign_out_task = asyncio.ensure_future(sign_out())
        app.sign_in_finish = asyncio.ensure_future(finish())
        await asyncio.sleep(0)
        app.engine.calls = order
        await app._quit()
        self.assertTrue(revocation.cancelled())
        self.assertTrue(app.sign_in_finish.cancelled())
        self.assertEqual(order, [('stop', main.QUIT_GRACE), 'wiped', 'quit'])
        app.settings.set_boolean('signed-in', True)
        self.assertIsNone(app.library_sync.start())  # held: nothing starts while quitting

    def test_activating_while_quitting_shows_nothing(self):
        self.app._quitting = object()
        self.app.do_activate()  # no window made: the stand-ins have none to give
        self.assertIsNone(self.app.get_active_window())


class BackgroundTest(AppTestCase):
    def test_a_closed_window_gets_a_notification_instead_of_a_toast(self):
        app = self.app
        del app.toast  # the Application's own, not the recording stand-in
        sent, withdrawn, released = [], [], []
        app.send_notification = lambda ident, notification: sent.append(ident)
        app.withdraw_notification = withdrawn.append
        app.release = lambda: released.append(True)
        app.background = mock.Mock(active=True)
        app.toast('Could not sync your library', 'Retry', 'app.sync')
        self.assertEqual(sent, [main.BACKGROUND_NOTIFICATION])
        app._release_background()  # the window is back
        self.assertEqual((withdrawn, released), ([main.BACKGROUND_NOTIFICATION], [True]))


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(config.set_build_profile, 'default')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': ''})
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, profile='development', demo_dir=None):
        return main.Application('0.9.0', f'{BASE_ID}.DemoTest', BASE_ID, profile, demo_dir)

    def test_the_demo_keeps_its_settings_apart(self):
        app = self.app(demo_dir=self.tmp.name)
        # The desktop's settings, as a backend of their own (the tests' default is memory).
        desktop = Gio.keyfile_settings_backend_new(
            os.path.join(self.tmp.name, 'desktop.ini'), '/', None)
        app.settings = Gio.Settings.new_full(app.settings.props.settings_schema, desktop, None)
        self.assertTrue(app._use_demo())
        self.assertTrue(app.demo)
        self.assertEqual(os.environ['APPLE_MUSIC_CACHE'], self.tmp.name)
        backend = app.settings.props.backend
        self.assertIsNot(backend, desktop)
        self.assertNotEqual(backend, Gio.SettingsBackend.get_default())
        app.settings.set_string('last-page', 'albums')
        Gio.Settings.sync()
        with open(os.path.join(self.tmp.name, 'settings.ini'), encoding='utf-8') as file:
            self.assertIn('albums', file.read())
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'desktop.ini')))

    def test_settings_on_the_memory_backend_are_kept(self):
        app = self.app(demo_dir=self.tmp.name)
        settings = app.settings
        self.assertTrue(app._use_demo())
        self.assertIs(app.settings, settings)

    def test_a_release_build_needs_a_library_named(self):
        app = self.app(profile='default')
        with self.assertLogs('applemusic.main', 'ERROR'):
            self.assertFalse(app._use_demo())
        self.assertFalse(app.demo)
        os.environ['APPLE_MUSIC_CACHE'] = self.tmp.name  # a generated library named
        self.assertTrue(app._use_demo())

    def test_the_demo_has_no_mpris(self):
        app = self.app(demo_dir=self.tmp.name)
        app._use_demo()
        app.library = FakeLibrary()
        with mock.patch.object(main, 'Mpris') as mpris:
            app._make_parts()
        mpris.assert_not_called()
        self.assertIsNone(app.mpris)
        self.assertTrue(app.engine.demo)


class AccountKeyTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(config.set_build_profile, 'default')

    def test_the_development_build_has_keys_of_its_own(self):
        release = make_app(profile='default')
        devel = make_app(profile='development')
        schema = devel.settings.props.settings_schema
        for name in main.ACCOUNT_KEYS:
            self.assertEqual(release.account_key(name), name)
            self.assertEqual(devel.account_key(name), name + '-devel')
            self.assertTrue(schema.has_key(devel.account_key(name)), name)
        self.assertEqual(devel.account_key('sync-interval'), 'sync-interval')

    def test_the_development_app_reads_its_own(self):
        app = make_app(profile='development')
        for key in ('signed-in', 'signed-in-devel'):
            self.addCleanup(app.settings.reset, key)
        action = app.lookup_action('sync')
        app.settings.set_boolean('signed-in', True)  # the release build's sign-in
        self.assertFalse(action.get_enabled())
        self.assertFalse(app._autostart_wanted())
        app.settings.set_boolean('signed-in-devel', True)
        self.assertTrue(action.get_enabled())
        self.assertTrue(app._autostart_wanted())


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
