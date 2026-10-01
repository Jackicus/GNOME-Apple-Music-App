# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""widgets.engine_status.EngineStatus: the states an engine-backed page shows, with stand-ins
for the application and the engine (no display needed)."""

import asyncio
import unittest

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from gi.repository import GObject

from applemusic.backend.errors import EngineError
from applemusic.widgets.engine_status import EngineStatus


class Engine(GObject.Object):
    state = GObject.Property(type=str, default='down')
    authorized = GObject.Property(type=bool, default=False)

    def __init__(self):
        super().__init__()
        self.fail_start = False
        self.starts = 0

    async def start(self):
        self.starts += 1
        self.state = 'starting'
        await asyncio.sleep(0)
        if self.fail_start:
            self.state = 'down'
            raise EngineError('no-browser')
        self.state = 'up'


class Settings:
    def __init__(self, signed_in):
        self.values = {'signed-in': signed_in}

    def get_boolean(self, key):
        return self.values[key]


class App:
    """What EngineStatus asks of the application."""

    def __init__(self, signed_in=True, demo=False):
        self.engine = Engine()
        self.settings = Settings(signed_in)
        self.demo = demo
        self.actions = []
        self.reported = []  # the app's own report of a failed start
        self.tasks = []

    def account_key(self, name):
        return name

    def activate_action(self, name, _parameter=None):
        self.actions.append(name)

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    def start_engine(self):
        if self.demo:
            return None

        async def start():
            try:
                await self.engine.start()
            except EngineError as error:
                self.reported.append(error.code)

        return self.spawn(start())


class Page:
    """The page: what it was told to show, and how often it retried."""

    def __init__(self):
        self.shown = []
        self.retries = 0

    def show(self, status, title, description, button):
        self.shown.append((status, title, description, button))

    def retry(self):
        self.retries += 1


TEXTS = {
    'engine-down': 'Start the engine to load this',
    'not-signed-in': 'This appears once you sign in',
    'failed': 'Could Not Load This',
}


class EngineStatusTest(unittest.IsolatedAsyncioTestCase):
    def make(self, **app_options):
        self.app = App(**app_options)
        self.page = Page()
        self.status = EngineStatus(self.app, self.page.show, self.page.retry, TEXTS)
        return self.status

    async def settle(self):
        while self.app.tasks:
            tasks, self.app.tasks = self.app.tasks, []
            await asyncio.gather(*tasks)

    def last(self):
        return self.page.shown[-1]

    async def test_engine_down_while_starting_shows_loading_then_retries_when_up(self):
        status = self.make()
        status.watch()
        self.app.engine.state = 'starting'
        status.fail(EngineError('engine-down'))
        self.assertEqual(self.last()[0], 'loading')
        self.assertEqual(status.status, 'engine-down')
        self.app.engine.state = 'up'
        self.assertEqual(self.page.retries, 1)
        self.assertIsNone(status.status)

    async def test_engine_down_offers_start_engine_and_falls_back_when_down(self):
        status = self.make()
        status.watch()
        status.fail(EngineError('engine-down'))
        self.assertEqual(self.last(), ('engine-down', 'Engine Not Running',
                                       'Start the engine to load this', 'Start Engine'))
        self.app.engine.state = 'starting'  # started elsewhere
        self.assertEqual(self.last()[0], 'loading')
        self.app.engine.state = 'down'  # and it failed
        self.assertEqual(self.last()[0], 'engine-down')
        self.assertEqual(self.page.retries, 0)

    async def test_start_engine_retries_once_it_is_up(self):
        status = self.make()
        status.watch()
        status.fail(EngineError('engine-down'))
        status.activate()
        await self.settle()
        self.assertEqual(self.app.engine.starts, 1)
        self.assertEqual(self.page.retries, 1)

    async def test_a_failed_start_shows_engine_down_again_not_a_second_message(self):
        status = self.make()
        self.app.engine.fail_start = True
        status.fail(EngineError('engine-down'))  # not watched: the start's end tells
        status.activate()
        self.assertEqual(self.last()[0], 'loading')
        await self.settle()
        self.assertEqual(self.app.reported, ['no-browser'])  # the app's one report
        self.assertEqual(self.last()[0], 'engine-down')
        self.assertEqual(status.status, 'engine-down')
        self.assertEqual(self.page.retries, 0)

    async def test_demo_has_no_button(self):
        status = self.make(demo=True)
        status.fail(EngineError('engine-down'))
        self.assertEqual(self.last(), (
            'demo', 'Not Available in the Demo',
            'This page needs Apple Music; the demo library is offline', None))
        status.activate()
        self.assertEqual(self.app.actions, [])

    async def test_signed_out_engine_down_asks_to_sign_in_and_retries_once_authorized(self):
        status = self.make(signed_in=False)
        status.watch()
        status.fail(EngineError('engine-down'))
        self.assertEqual(self.last(), ('not-signed-in', 'Sign In to Apple Music',
                                       'This appears once you sign in', 'Sign In…'))
        status.activate()
        self.assertEqual(self.app.actions, ['sign-in'])
        self.app.engine.state = 'up'  # the sign-in's engine: not authorized yet
        self.assertEqual(self.page.retries, 0)
        self.app.engine.authorized = True
        self.assertEqual(self.page.retries, 1)

    async def test_a_title_pair_for_not_signed_in(self):
        status = self.make()
        status._texts = dict(TEXTS, **{'not-signed-in': ('Sign In to Search', 'Search once in')})
        status.fail(EngineError('not-signed-in'))
        self.assertEqual(self.last(), ('not-signed-in', 'Sign In to Search', 'Search once in',
                                       'Sign In…'))

    async def test_another_failure_says_the_apps_sentence_and_tries_again(self):
        status = self.make()
        status.fail(EngineError('timeout', 'the detail, for the log'))
        self.assertEqual(self.last(), ('failed', 'Could Not Load This',
                                       'Apple Music did not answer in time', 'Try Again'))
        status.activate()
        self.assertEqual(self.page.retries, 1)

    async def test_no_retry_after_unwatch_or_clear(self):
        status = self.make()
        status.watch()
        status.fail(EngineError('engine-down'))
        status.unwatch()
        self.app.engine.state = 'up'
        self.assertEqual(self.page.retries, 0)
        status.watch()  # shown again: it catches up
        self.assertEqual(self.page.retries, 1)
        status.fail(EngineError('not-signed-in'))
        status.clear()
        self.app.engine.authorized = True
        self.assertEqual(self.page.retries, 1)

    async def test_without_an_application_there_is_nothing_to_watch(self):
        # A widget test that shows a page without an application (pages.app() is None) still
        # maps and unmaps it, and neither may raise: GTK aborts on a widget left mapped.
        page = Page()
        status = EngineStatus(None, page.show, page.retry, TEXTS)
        status.watch()
        status.unwatch()
        status.unwatch()
        self.assertEqual(page.shown, [])
        self.assertIsNone(status.status)

    async def test_the_page_is_held_weakly(self):
        import gc
        import weakref

        status = self.make()
        page = weakref.ref(self.page)
        self.page = None
        gc.collect()
        self.assertIsNone(page())
        status.fail(EngineError('timeout'))  # nothing to show it: no error either


if __name__ == '__main__':
    unittest.main()
