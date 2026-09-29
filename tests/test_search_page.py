"""The Search page over the stand-in engine (tests/page_harness.py): stale Apple Music answers
never take over, Your Library follows the library's state, and the status pages offer Your
Library."""

import asyncio
import unittest

from tests.page_harness import PageTestCase, album

from applemusic.backend.errors import EngineError


class SearchPageTest(PageTestCase):
    async def search_page(self):
        from applemusic.pages.search import SearchPage

        page = await self.show_root(SearchPage(self.library, 'Search'))
        await self.settle()
        return page

    def held(self, name, answer):
        """Make the engine's `name` requests wait for the returned future, then answer."""
        waiting = asyncio.get_running_loop().create_future()

        async def respond(*_args):
            await waiting
            return answer

        self.app.engine.answers[name] = respond
        return waiting

    def type(self, page, text):
        """Type text, and let the pause after typing pass at once."""
        page.search_entry.set_text(text)
        page._cancel_debounce()
        page._refresh()

    SUGGESTIONS = {'terms': [{'term': 'invented', 'display': 'invented'}], 'items': []}
    RESULTS = {'shelves': [{'key': 'albums', 'title': '',
                            'items': [dict(album(1, tracks=0), id='c1')]}]}

    async def test_a_late_suggestion_does_not_take_over_your_library(self):
        page = await self.search_page()
        waiting = self.held('suggest', self.SUGGESTIONS)
        self.type(page, 'inv')
        await self.turn()
        page.set_mode('library')
        waiting.set_result(None)
        await self.settle()
        self.assertEqual(page.mode, 'library')
        self.assertNotEqual(page.stack.get_visible_child_name(), 'suggestions')

    async def test_a_late_suggestion_does_not_take_over_a_cleared_entry(self):
        page = await self.search_page()
        waiting = self.held('suggest', self.SUGGESTIONS)
        self.type(page, 'inv')
        await self.turn()
        self.type(page, '')
        waiting.set_result(None)
        await self.settle()
        self.assertNotEqual(page.stack.get_visible_child_name(), 'suggestions')

    async def test_late_results_do_not_take_over_after_enter_then_clear(self):
        page = await self.search_page()
        waiting = self.held('search', self.RESULTS)
        page.search_entry.set_text('inv')
        page._on_entry_activated(page.search_entry)  # Enter: the search
        await self.turn()
        page.search_entry.set_text('')
        page._on_entry_activated(page.search_entry)  # Enter on nothing: the landing
        waiting.set_result(None)
        await self.settle()
        self.assertNotEqual(page.stack.get_visible_child_name(), 'results')

    async def test_results_show_when_nothing_newer_was_asked(self):
        page = await self.search_page()
        self.app.engine.answers['search'] = self.RESULTS
        page.search_entry.set_text('inv')
        page._on_entry_activated(page.search_entry)
        await self.settle()
        self.assertEqual(page.stack.get_visible_child_name(), 'results')

    async def test_the_engine_status_offers_your_library(self):
        page = await self.search_page()  # the landing: engine-down
        self.assertEqual(page.status_button.get_label(), 'Start Engine')
        self.assertTrue(page.status_library_button.get_visible())
        page.status_library_button.emit('clicked')
        self.assertEqual(page.mode, 'library')
        self.assertEqual(page.status_page.get_title(), 'Search Your Library')
        self.assertFalse(page.status_library_button.get_visible())

    async def test_the_demo_status_offers_your_library(self):
        self.app.demo = True
        page = await self.search_page()
        self.assertEqual(page.status_page.get_title(), 'Not Available in the Demo')
        self.assertFalse(page.status_button.get_visible())
        self.assertTrue(page.status_library_button.get_visible())

    async def test_a_failure_after_a_switch_to_your_library_is_dropped(self):
        page = await self.search_page()
        waiting = asyncio.get_running_loop().create_future()

        async def fail(*_args):
            await waiting
            raise EngineError('timeout')

        self.app.engine.answers['suggest'] = fail
        self.type(page, 'inv')
        await self.turn()
        page.set_mode('library')
        waiting.set_result(None)
        await self.settle()
        self.assertEqual(page.status_page.get_title(), 'No Results Found')

    async def test_your_library_leaves_its_spinner_when_the_library_is_ready(self):
        page = await self.search_page()
        self.library.songs_ready = True
        self.library.state = 'loading'
        page.set_mode('library')
        page.search_entry.set_text('no such thing')
        page._on_entry_activated(page.search_entry)
        self.assertEqual(page.stack.get_visible_child_name(), 'loading')
        self.library.state = 'ready'
        self.assertEqual(page.stack.get_visible_child_name(), 'status')
        self.assertEqual(page.status_page.get_title(), 'No Results Found')

    async def test_focus_waits_until_the_page_shows(self):
        from applemusic.pages.search import SearchPage

        page = SearchPage(self.library, 'Search')
        page.focus_entry()  # not shown yet: nothing to focus
        await self.show_root(page)
        self.assertTrue(await self.until(
            lambda: self.window.get_focus() is not None
            and self.window.get_focus().is_ancestor(page.search_entry)))


    async def test_a_dropped_page_is_freed(self):
        # Sign-out drops the page (window.forget_account_pages()).
        import gc

        from applemusic.pages.search import SearchPage

        page = SearchPage(self.library, 'Search')
        self.window.navigation_view.add(page)
        self.window.navigation_view.replace([page])
        self.assertTrue(await self.until(page.get_mapped))
        page.set_mode('library')
        page.search_entry.set_text('invented')
        page._on_entry_activated(page.search_entry)
        finalized = []
        page.weak_ref(lambda: finalized.append(True))
        self.window.navigation_view.replace([self.window.root_page])
        self.window.navigation_view.remove(page)
        del page
        await self.settle()

        def freed():
            gc.collect()
            return finalized

        self.assertTrue(await self.until(freed))


if __name__ == '__main__':
    unittest.main()
