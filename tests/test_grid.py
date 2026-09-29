"""The grid pages (pages/grid.py): their columns, their sort orders and the Sort By menu, the
title in the header bar, and a folder that is gone."""

import unittest

from tests import ROOT  # noqa: F401  registers src/ as applemusic
from tests.page_harness import PageTestCase, album

from applemusic.pages import estimate_width


class EstimateWidthTest(unittest.TestCase):
    def test_the_default_width_less_the_sidebar(self):
        self.assertEqual(estimate_width(1100, False, 1920, 280), 820)

    def test_maximized_takes_the_monitors_width(self):
        self.assertEqual(estimate_width(1100, True, 1920, 280), 1640)

    def test_a_collapsed_sidebar_takes_nothing(self):
        self.assertEqual(estimate_width(360, False, 1920, 0), 360)

    def test_no_monitor_known_keeps_the_default(self):
        self.assertEqual(estimate_width(1100, True, 0, 280), 820)


class GridTest(PageTestCase):
    def test_columns_for(self):
        from applemusic.pages.grid import columns_for

        # Phase 19's figures: the content pane at 700, 1100 and 1920 px wide.
        self.assertEqual(columns_for(700), 4)
        self.assertEqual(columns_for(1100), 6)
        self.assertEqual(columns_for(1920), 11)
        self.assertEqual(columns_for(200), 2)  # never fewer

    def test_the_title_starts_at_the_first_cover(self):
        from applemusic.pages.grid import cover_start

        # 5 columns of 166 in 830 px less the padding: 163.6 px each, the cover centred.
        self.assertAlmostEqual(cover_start(830, 12), 6 + (818 / 4 - 160) / 2)
        self.assertAlmostEqual(cover_start(1000, 3), 6 + (988 / 3 - 160) / 2)

    def test_grid_sorts(self):
        from gi.repository import Gio, Gtk

        from applemusic.library import Item
        from applemusic.pages.grid import SORTS

        items = [Item(dict(album(0), title='B', subtitle='Zed', year=2001)),
                 Item(dict(album(1), title='A', subtitle='Zed', year=1999)),
                 Item(dict(album(2), title='C', subtitle='Abe', year=0)),
                 Item(dict(album(3), title='D', subtitle='Abe', year=2020))]
        store = Gio.ListStore(item_type=Item)
        store.splice(0, 0, items)

        def titles(key):
            model = Gtk.SortListModel(model=store, sorter=SORTS[key][1]())
            return [model.get_item(n).title for n in range(model.get_n_items())]

        self.assertEqual(titles('title'), ['A', 'B', 'C', 'D'])
        # By artist, then year (unknown first), then title.
        self.assertEqual(titles('artist'), ['C', 'D', 'A', 'B'])
        # Newest first, an unknown year last.
        self.assertEqual(titles('year'), ['D', 'B', 'A', 'C'])

    async def test_the_sort_menu_sorts_and_is_remembered_by_page(self):
        from gi.repository import Gio, GLib

        from applemusic.library import Item
        from applemusic.pages.grid import GridPage

        store = Gio.ListStore(item_type=Item)
        store.splice(0, 0, [Item(album(n, year=1990 + n)) for n in range(6)])
        self.addCleanup(self.app.settings.reset, 'grid-sort')
        page = GridPage(self.library, 'Invented Grid', store, sorts=('title', 'year'))
        page.set_tag('albums')
        await self.show_root(page)
        self.assertTrue(page.sort_button.get_visible())
        page.activate_action('page.sort', GLib.Variant('s', 'year'))
        model = page.grid_view.get_model()
        self.assertEqual(model.get_item(0).title, 'Invented Album 005')
        self.assertEqual(self.app.settings.get_value('grid-sort').unpack(), {'albums': 'year'})

        self.window.navigation_view.replace([self.window.root_page])  # one page a tag
        self.window.navigation_view.remove(page)
        self._roots.remove(page)
        again = GridPage(self.library, 'Invented Grid', store, sorts=('title', 'year'))
        again.set_tag('albums')
        await self.show_root(again)
        self.assertEqual(again.grid_view.get_model().get_item(0).title, 'Invented Album 005')

    async def test_the_header_shows_the_title_only_while_the_big_one_is_away(self):
        from gi.repository import Gio

        from applemusic.library import Item
        from applemusic.pages.grid import GridPage

        store = Gio.ListStore(item_type=Item)
        page = await self.push(GridPage(self.library, 'Invented Grid', store, root=False))
        self.assertEqual(page.stack.get_visible_child_name(), 'empty')
        self.assertTrue(await self.until(page.header_bar.get_show_title))
        store.splice(0, 0, [Item(album(n)) for n in range(60)])
        self.assertTrue(await self.until(lambda: not page.header_bar.get_show_title()))
        page.scrolled_window.get_vadjustment().set_value(400)
        self.assertTrue(await self.until(page.header_bar.get_show_title))

    async def test_a_folder_that_is_gone_says_so(self):
        from applemusic.pages import folder

        page = await self.show_root(folder(self.library, 'l.gone', 'Invented Folder'))
        self.assertEqual(page.stack.get_visible_child_name(), 'empty')
        self.assertEqual(page.empty_page.get_title(), 'Folder Not Found')


if __name__ == '__main__':
    unittest.main()
