"""The Search page over the stand-in engine (tests/page_harness.py): Your Library follows the
library's state."""

import unittest

from tests.page_harness import PageTestCase


class SearchPageTest(PageTestCase):
    async def search_page(self):
        from applemusic.pages.search import SearchPage

        return await self.show_root(SearchPage(self.library, 'Search'))

    async def test_your_library_leaves_its_spinner_when_the_library_is_ready(self):
        page = await self.search_page()
        self.library.songs_ready = True
        self.library.state = 'loading'
        page.set_mode('library')
        page.search_entry.set_text('no such thing')
        page.on_entry_activated(page.search_entry)
        self.assertEqual(page.stack.get_visible_child_name(), 'loading')
        self.library.state = 'ready'
        self.assertEqual(page.stack.get_visible_child_name(), 'status')
        self.assertEqual(page.status_page.get_title(), 'No Results Found')


if __name__ == '__main__':
    unittest.main()
