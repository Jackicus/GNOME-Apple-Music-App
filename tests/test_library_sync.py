# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The app's sync (sync.LibrarySync) and sync_library()'s ending: a cancelled sync ends only
once its thread has, and writes nothing after. The engine is test_sync_app's FakeEngine over
invented fixtures; no network, no display.
"""

import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)
from tests.gtk import SCHEMA_ID
from tests.test_sync_app import FakeEngine, answers

from gi.repository import Gio, GObject

from applemusic import sync as app_sync
from applemusic.backend import normalize, store
from applemusic.backend.errors import EngineError
from applemusic.library import Library


class FakeApp:
    """What LibrarySync asks of the Application."""

    def account_key(self, name):
        return name  # the release build's keys

    def __init__(self, engine, library):
        self.engine = engine
        self.library = library
        self.settings = Gio.Settings.new(SCHEMA_ID)
        self.demo = False
        self.toasts = []
        self.reported = []

    def spawn(self, coro):
        return asyncio.get_running_loop().create_task(coro)

    def toast(self, title, button_label=None, action_name=None):
        self.toasts.append((title, button_label, action_name))

    def report(self, error):
        self.reported.append(error.code)

    def refuse_in_demo(self):
        if self.demo:
            self.toast('Not available with the demo library')
        return self.demo


class SyncTestCase(unittest.IsolatedAsyncioTestCase):
    """A temporary cache; thumbnails 'fetched' by writing a few bytes (the first at once,
    the rest once `gate` is set, when `slow` is on); a cover never fetched."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, 'cache')
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.cache})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)  # never leave a fetch waiting
        self.slow = False
        self.fetched = []

        def thumbnail(url, cache_dir, dest_path, generation=None):
            self.fetched.append(url)
            if self.slow and len(self.fetched) > 1:
                self.gate.wait(5)
            store.atomic_write(dest_path, lambda file: file.write(b'img'), root=cache_dir,
                               generation=generation)
            return dest_path

        def no_cover(url, cache_dir, timeout=10.0, dest_path=None, generation=None):
            self.fail(f'a sync fetched a cover: {url}')

        for name, fake in (('cache_thumbnail', thumbnail), ('cache_artwork', no_cover)):
            patcher = mock.patch.object(normalize, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        # When the build thread ends.
        self.ended = []
        build = app_sync._build_and_write

        def recorded_build(*args, **kwargs):
            try:
                return build(*args, **kwargs)
            finally:
                self.ended.append(time.monotonic())

        patcher = mock.patch.object(app_sync, '_build_and_write', recorded_build)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.library = Library()

    @property
    def library_json(self):
        return os.path.join(self.cache, 'library.json')


class CancelTest(SyncTestCase):
    async def test_a_cancelled_sync_ends_after_its_thread_and_writes_nothing(self):
        self.slow = True
        loop = asyncio.get_running_loop()
        task = None

        def report(section, done, total):
            if section == 'artwork' and done and not task.done() and not self.gate.is_set():
                task.cancel()  # during the artwork, the thread still running
                loop.call_later(0.2, self.gate.set)

        task = asyncio.ensure_future(
            app_sync.sync_library(FakeEngine(answers()), self.library, report))
        await asyncio.wait([task])
        finished = time.monotonic()
        self.assertTrue(task.cancelled())
        self.assertEqual(len(self.ended), 1, 'the task ended before its thread')
        self.assertLessEqual(self.ended[0], finished)
        self.assertFalse(os.path.exists(self.library_json))
        # Wiped now (a sign-out would): nothing of the sync comes back.
        shutil.rmtree(self.cache)
        await asyncio.sleep(0.3)
        self.assertFalse(os.path.exists(self.cache))

    async def test_a_wipe_during_the_artwork_stops_the_sync(self):
        self.slow = True
        loop = asyncio.get_running_loop()
        bumped = []

        def report(section, done, total):
            if section == 'artwork' and done and not bumped:
                bumped.append(store.bump_cache_generation())
                loop.call_later(0.1, self.gate.set)

        with self.assertRaises(store.CacheGone):
            await app_sync.sync_library(FakeEngine(answers()), self.library, report)
        self.assertFalse(os.path.exists(self.library_json))


class LibrarySyncTest(SyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine = FakeEngine(answers())
        self.app = FakeApp(self.engine, self.library)
        self.addCleanup(self.app.settings.reset, 'last-sync')
        self.addCleanup(self.app.settings.reset, 'signed-in')
        self.app.settings.set_boolean('signed-in', True)
        self.sync = app_sync.LibrarySync(self.app)
        self.progress = []
        self.sync.connect('progress', lambda _sync, *report: self.progress.append(report))

    async def test_a_sync_runs_once_and_says_so(self):
        task = self.sync.start()
        self.assertTrue(self.sync.props.running)
        self.assertIsNone(self.sync.start())  # one at a time
        await task
        await asyncio.sleep(0)
        self.assertFalse(self.sync.props.running)
        self.assertEqual(self.progress[0], ('', 0, None))
        self.assertEqual(self.progress[-1][0], 'artwork')
        self.assertTrue(self.app.settings.get_string('last-sync'))
        self.assertTrue(self.app.toasts[-1][0].startswith('Library synced'))
        self.assertTrue(os.path.exists(self.library_json))

    async def test_cancel_returns_after_the_thread_and_no_report_follows(self):
        self.slow = True
        loop = asyncio.get_running_loop()
        cancelled = []

        def on_progress(_sync, section, done, _total):
            if section == 'artwork' and done and not cancelled:
                cancelled.append(asyncio.ensure_future(self.sync.cancel()))
                loop.call_later(0.2, self.gate.set)

        self.sync.connect('progress', on_progress)
        self.sync.start()
        while not cancelled:
            await asyncio.sleep(0.01)
        await cancelled[0]
        finished = time.monotonic()
        self.assertEqual(len(self.ended), 1)
        self.assertLessEqual(self.ended[0], finished)
        reports = len(self.progress)
        await asyncio.sleep(0.2)  # a report the thread queued arrives after the end
        self.assertEqual(len(self.progress), reports)
        self.assertFalse(self.sync.props.running)
        self.assertFalse(os.path.exists(self.library_json))
        self.assertEqual(self.app.settings.get_string('last-sync'), '')
        self.assertEqual(self.app.toasts, [])

    async def test_a_sync_asked_while_the_engine_starts_waits_for_it(self):
        starts = []

        async def start(visible=None):
            starts.append(visible)
            await asyncio.sleep(0.05)
            self.engine.state = 'up'

        self.engine.state = 'starting'
        self.engine.start = start
        await self.sync.start()
        self.assertEqual(starts, [None])
        self.assertEqual(self.progress[0], ('', 0, None))  # the banner before the wait
        self.assertEqual(self.app.reported, [])
        self.assertTrue(self.app.toasts[-1][0].startswith('Library synced'))

    async def test_held_starts_nothing(self):
        with self.sync.held():
            self.assertIsNone(self.sync.start())
        self.assertIsNotNone(self.sync.start())
        await self.sync.cancel()

    async def test_the_library_says_it_is_syncing_while_a_sync_runs(self):
        seen = []
        self.library.connect('notify::syncing', lambda library, _p: seen.append(
            (library.props.syncing, library.props.state)))
        await self.sync.start()
        # On during the run (the library still empty then), off once it has reloaded.
        self.assertEqual(seen, [(True, 'empty'), (False, 'ready')])

    async def test_the_library_stops_syncing_when_the_sync_fails(self):
        del self.engine.answers[app_sync.SONGS_ENDPOINT]
        with self.assertLogs('applemusic.sync', 'WARNING'):
            await self.sync.start()
        self.assertFalse(self.library.props.syncing)

    async def test_an_unexpected_failure_is_toasted_with_retry(self):
        async def broken(engine, library, progress=None):
            raise OSError(28, 'No space left on device')

        with mock.patch.object(app_sync, 'sync_library', broken), \
                self.assertLogs('applemusic.sync', 'ERROR'):
            await self.sync.start()
        self.assertEqual(self.app.toasts, [('Could not sync your library', 'Retry', 'app.sync')])
        self.assertEqual(self.app.settings.get_string('last-sync'), '')

    async def test_an_api_failure_says_so_without_its_detail(self):
        del self.engine.answers[app_sync.SONGS_ENDPOINT]  # Apple says no
        with self.assertLogs('applemusic.sync', 'WARNING'):
            await self.sync.start()
        self.assertEqual(self.app.toasts, [('Could not sync your library', 'Retry', 'app.sync')])
        self.assertEqual(self.app.reported, [])

    async def test_a_missing_engine_is_reported(self):
        async def no_chrome(visible=None):
            raise EngineError('no-browser', 'Google Chrome was not found')

        self.engine.start = no_chrome
        await self.sync.start()
        self.assertEqual(self.app.reported, ['no-browser'])
        self.assertEqual(self.app.toasts, [])


class ProgressTextTest(unittest.TestCase):
    def test_every_section_has_both_sentences(self):
        texts = app_sync.progress_texts()
        self.assertEqual(set(texts), set(app_sync.PROGRESS_SECTIONS))
        for section, (counted, uncounted) in texts.items():
            self.assertTrue(counted and uncounted, section)
            self.assertNotIn('{', uncounted, section)

    def test_the_sentences(self):
        text = app_sync.progress_text
        self.assertEqual(text('', 0, None), 'Syncing your library…')
        self.assertEqual(text('songs', 0, None), 'Syncing songs…')
        self.assertEqual(text('songs', 300, 900), 'Syncing songs: 300 of 900')
        self.assertEqual(text('artwork', 5, 9), 'Downloading artwork: 5 of 9')
        self.assertEqual(text('shelves', 1, 3), 'Syncing recommendations…')


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class WhenDueTest(unittest.TestCase):
    """The scheduler's pure helpers."""

    def test_next_sync_delay(self):
        delay = app_sync.next_sync_delay
        self.assertIsNone(delay('2026-09-28T11:00:00+00:00', 0, NOW))  # manual
        self.assertEqual(delay('', 6, NOW), 0)  # never synced
        self.assertEqual(delay('not a time', 6, NOW), 0)
        self.assertEqual(delay('2026-09-28T11:00:00+00:00', 6, NOW), 5 * 3600)  # fresh
        self.assertEqual(delay('2026-09-28T05:00:00+00:00', 6, NOW), 0)  # overdue
        self.assertEqual(delay('2026-09-28T13:00:00+00:00', 6, NOW), 0)  # in the future

    def test_sync_due(self):
        due = app_sync.sync_due
        self.assertTrue(due('2026-09-28T13:00:00+00:00', 6, NOW))  # a clock set back
        self.assertFalse(due('2026-09-28T11:00:00+00:00', 0, NOW))
        # A library not to keep is due whatever the interval, manual too.
        self.assertTrue(due('2026-09-28T11:00:00+00:00', 0, NOW, library_ok=False))
        self.assertTrue(due('2026-09-28T11:59:00+00:00', 6, NOW, library_ok=False))

    def test_library_ok(self):
        library = Library()
        self.assertTrue(app_sync.library_ok(library))  # not read yet
        for state, version, ok in (('ok', 2, True), ('ok', 3, True), ('ok', 1, False),
                                   ('ok', 0, False), ('missing', 0, False),
                                   ('unreadable', 0, False)):
            library.props.file_state, library.props.version = state, version
            self.assertEqual(app_sync.library_ok(library), ok, (state, version))


class SchedulerEngine(GObject.Object):
    """The engine as the scheduler watches it."""

    state = GObject.Property(type=str, default='down')
    authorized = GObject.Property(type=bool, default=False)

    def __init__(self):
        super().__init__()
        self.starts = 0

    async def start(self, visible=None):
        self.starts += 1


class Timers:
    """add_timeout and remove_timeout that run nothing until fire() is called."""

    def __init__(self):
        self.timers = {}
        self.next_id = 1

    def add(self, seconds, callback):
        self.timers[self.next_id] = (seconds, callback)
        self.next_id += 1
        return self.next_id - 1

    def remove(self, source):
        del self.timers[source]

    def seconds(self):
        return sorted(seconds for seconds, _callback in self.timers.values())

    def fire(self, seconds):
        for source, (after, callback) in list(self.timers.items()):
            if after == seconds:
                del self.timers[source]
                callback()


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    """LibrarySync.schedule() with fake timers and clock; sync_library() replaced by one
    that answers counts at once."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = SchedulerEngine()
        self.library = Library()
        self.app = FakeApp(self.engine, self.library)
        for key in ('last-sync', 'signed-in', 'sync-interval'):
            self.addCleanup(self.app.settings.reset, key)
        self.app.settings.set_boolean('signed-in', True)
        self.counts = {'albums': 1, 'playlists': 1, 'art': {'wanted': 1, 'fetched': 1,
                                                            'failed': 0}}
        self.runs = []

        async def fake_sync(engine, library, progress=None):
            self.runs.append(engine)
            return self.counts

        patcher = mock.patch.object(app_sync, 'sync_library', fake_sync)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.timers = Timers()
        self.now = 1000.0
        self.sync = app_sync.LibrarySync(self.app, self.timers.add, self.timers.remove,
                                         clock=lambda: self.now)

    def stamp(self, hours_ago):
        when = datetime.now(UTC) - timedelta(hours=hours_ago)
        self.app.settings.set_string('last-sync', when.isoformat(timespec='seconds'))

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)

    def write_library(self, version):
        path = os.path.join(self.tmp.name, 'library.json')
        with open(path, 'w', encoding='utf-8') as file:
            json.dump({'version': version, 'sections': {}}, file)

    async def test_the_timer_starts_nothing_while_the_engine_is_down(self):
        self.stamp(10)  # overdue at 6 hours
        self.sync.schedule()
        self.assertEqual(self.timers.seconds(), [])  # nothing to look at with the engine down
        self.engine.state = 'up'  # up, not yet authorized: the timer is set
        self.assertEqual(self.timers.seconds(), [app_sync.CHECK_MIN])
        # The engine goes down just as the timer fires: nothing starts, not even Chrome.
        self.engine.freeze_notify()
        self.engine.state = 'down'
        self.timers.fire(app_sync.CHECK_MIN)
        await self.settle()
        self.assertEqual(self.runs, [])
        self.assertEqual(self.engine.starts, 0)
        self.engine.thaw_notify()
        self.assertEqual(self.timers.seconds(), [])

    async def test_the_engine_coming_up_starts_one_sync_when_due(self):
        self.stamp(10)
        self.sync.schedule()
        self.engine.authorized = True
        self.engine.state = 'up'
        await self.settle()
        self.assertEqual(len(self.runs), 1)
        self.engine.notify('state')  # up again: the sync is fresh now
        await self.settle()
        self.assertEqual(len(self.runs), 1)
        # The next look is when the fresh sync is due: 6 hours, looked at within the hour.
        self.assertEqual(self.timers.seconds(), [app_sync.CHECK_MAX])

    async def test_a_fresh_sync_is_not_due_and_a_timer_starts_one_later(self):
        self.stamp(1)
        self.engine.authorized = True
        self.engine.state = 'up'
        self.sync.schedule()
        self.sync.check()
        self.assertEqual(self.runs, [])
        self.stamp(7)  # time went by (changed::last-sync re-arms: at once, bounded)
        self.assertEqual(self.timers.seconds(), [app_sync.CHECK_MIN])
        self.timers.fire(app_sync.CHECK_MIN)
        await self.settle()
        self.assertEqual(len(self.runs), 1)

    async def test_manual_arms_nothing_but_a_library_not_to_keep_is_due(self):
        self.app.settings.set_int('sync-interval', 0)
        self.stamp(100)
        self.engine.authorized = True
        self.engine.state = 'up'
        self.sync.schedule()
        self.assertEqual(self.timers.seconds(), [])
        self.write_library(1)  # a library.json from before version 2
        await self.library.load()
        await self.settle()
        self.assertEqual(len(self.runs), 1)

    async def test_an_empty_current_library_is_not_due(self):
        self.stamp(1)
        self.write_library(app_sync.LIBRARY_VERSION)
        await self.library.load()
        self.engine.authorized = True
        self.engine.state = 'up'
        self.sync.schedule()
        self.assertFalse(self.sync.due())
        self.sync.check()
        await self.settle()
        self.assertEqual(self.runs, [])

    async def test_signed_out_or_held_starts_nothing(self):
        self.stamp(10)
        self.sync.schedule()
        self.app.settings.set_boolean('signed-in', False)
        self.engine.authorized = True
        self.engine.state = 'up'
        self.app.settings.set_boolean('signed-in', True)
        with self.sync.held():
            self.sync.check()
        await self.settle()
        self.assertEqual(self.runs, [])
        self.assertEqual(self.app.reported, [])  # nothing offered the sign-in either

    async def test_a_failed_sync_waits_before_the_next(self):
        async def failing(engine, library, progress=None):
            self.runs.append(engine)
            raise EngineError('api', 'invented failure')

        self.stamp(10)
        self.sync.schedule()
        with mock.patch.object(app_sync, 'sync_library', failing), \
                self.assertLogs('applemusic.sync', 'WARNING'):
            self.engine.authorized = True
            self.engine.state = 'up'
            await self.settle()
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.timers.seconds(), [app_sync.RETRY_DELAY])
        self.engine.notify('state')
        await self.settle()
        self.assertEqual(len(self.runs), 1)  # not again at once
        self.now += app_sync.RETRY_DELAY
        self.timers.fire(app_sync.RETRY_DELAY)
        await self.settle()
        self.assertEqual(len(self.runs), 2)

    async def test_failed_thumbnails_are_retried_once(self):
        self.counts['art']['failed'] = 3
        self.engine.authorized = True
        self.engine.state = 'up'
        self.sync.schedule()
        await self.sync.start()
        await self.settle()
        self.assertIn(app_sync.RETRY_DELAY, self.timers.seconds())
        stamp = self.app.settings.get_string('last-sync')
        self.timers.fire(app_sync.RETRY_DELAY)
        await self.settle()
        self.assertEqual(len(self.runs), 2)
        # The retry failed the same way: no second retry, the interval rules.
        self.assertNotIn(app_sync.RETRY_DELAY, self.timers.seconds())
        self.assertNotEqual(self.app.settings.get_string('last-sync'), '')
        self.assertGreaterEqual(self.app.settings.get_string('last-sync'), stamp)


if __name__ == '__main__':
    unittest.main()
