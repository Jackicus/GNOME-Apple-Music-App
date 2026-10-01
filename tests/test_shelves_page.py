# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""New, Made for You and a category (pages/shelves.py) over the stand-in engine
(tests/page_harness.py): Refresh, the title, a day-old answer, a popped page's requests."""

import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from tests.page_harness import PageTestCase, album

from applemusic.backend.errors import EngineError


def answer(count=2, cached=None):
    shelves = [{'key': f'shelf{n}', 'title': f'Invented Shelf {n}',
                'items': [dict(album(i, tracks=0), id=f's{n}.{i}') for i in range(4)]}
               for n in range(count)]
    return {'shelves': shelves, 'cached': cached}


def stamp(age):
    return (datetime.now(UTC) - age).strftime('%Y-%m-%dT%H:%M:%SZ')


class ShelvesPageTest(PageTestCase):
    def page(self, fetch, root=True):
        from applemusic.pages.shelves import ShelvesPage

        return ShelvesPage('Invented Page', fetch, root=root)

    def test_a_day_old_answer_expires(self):
        from applemusic.pages.shelves import expired

        self.assertTrue(expired({'cached': stamp(timedelta(hours=25))}))
        self.assertFalse(expired({'cached': stamp(timedelta(hours=1))}))
        self.assertFalse(expired({}))
        self.assertFalse(expired({'cached': 'not a time'}))

    async def test_the_title_is_in_the_header_while_the_page_has_no_shelves(self):
        self.app.engine.answers['browse'] = EngineError('timeout')
        page = await self.show_root(self.page(self.app.engine.browse))
        await self.settle()
        self.assertEqual(page.stack.get_visible_child_name(), 'status')
        self.assertTrue(page.header_bar.get_show_title())
        self.app.engine.answers['browse'] = answer()
        page.status_button.emit('clicked')  # Try Again
        await self.settle()
        self.assertEqual(page.stack.get_visible_child_name(), 'items')
        self.assertTrue(await self.until(lambda: not page.header_bar.get_show_title()))

    async def test_refresh_does_nothing_while_a_request_runs(self):
        waiting = asyncio.get_running_loop().create_future()

        async def slow(_refresh):
            await waiting
            return answer()

        self.app.engine.answers['browse'] = slow
        page = await self.show_root(self.page(self.app.engine.browse))
        await self.turn()
        page.refresh_button.emit('clicked')
        self.assertTrue(page.refresh_button.get_sensitive())
        self.assertEqual(self.app.engine.calls, ['browse'])
        waiting.set_result(None)
        await self.settle()
        page.refresh_button.emit('clicked')
        await self.settle()
        self.assertEqual(self.app.engine.calls, ['browse', 'browse'])

    async def test_refresh_starts_the_engine_while_it_is_down(self):
        page = await self.show_root(self.page(self.app.engine.browse))  # engine-down
        await self.settle()
        self.assertEqual(page.status_button.get_label(), 'Start Engine')
        self.app.engine.answers['browse'] = answer()
        page.refresh_button.emit('clicked')
        await self.settle()
        self.assertEqual(self.app.engine.calls, ['browse', 'start', 'browse'])
        self.assertEqual(page.stack.get_visible_child_name(), 'items')

    async def test_signed_out_it_asks_to_sign_in_not_to_start_the_engine(self):
        self.app.settings.set_boolean('signed-in', False)  # first run, or after Sign Out
        page = await self.show_root(self.page(self.app.engine.browse))  # engine-down
        await self.settle()
        self.assertEqual(page.status_page.get_title(), 'Sign In to Apple Music')
        self.assertEqual(page.status_button.get_label(), 'Sign In…')
        page.status_button.emit('clicked')
        self.assertEqual(self.app.actions, ['sign-in'])
        self.assertNotIn('start', self.app.engine.calls)

    async def test_the_demo_has_no_refresh(self):
        self.app.demo = True
        page = await self.show_root(self.page(self.app.engine.browse))
        await self.settle()
        self.assertFalse(page.refresh_button.get_visible())
        self.assertEqual(page.status_page.get_title(), 'Not Available in the Demo')
        self.assertFalse(page.status_button.get_visible())

    async def test_a_day_old_answer_is_asked_again_when_shown(self):
        self.app.engine.answers['browse'] = answer(cached=stamp(timedelta(days=2)))
        page = await self.show_root(self.page(self.app.engine.browse))
        await self.settle()
        self.window.navigation_view.replace([self.window.root_page])
        self.assertTrue(await self.until(lambda: not page.get_mapped()))
        self.window.navigation_view.replace([page])
        await self.settle()
        self.assertEqual(self.app.engine.calls, ['browse', 'browse'])

    async def test_a_popped_category_page_stops_its_requests(self):
        waiting = asyncio.get_running_loop().create_future()

        async def never(*_args):
            await waiting

        self.app.engine.answers['category'] = never
        page = await self.push(self.page(
            lambda refresh: self.app.engine.category('c1', refresh=refresh), root=False))
        self.assertTrue(await self.until(lambda: page._task is not None))
        task = page._task
        self.assertFalse(task.done())
        self.window.navigation_view.pop()
        self.assertTrue(await self.until(task.done))
        self.assertTrue(task.cancelled())


if __name__ == '__main__':
    unittest.main()
