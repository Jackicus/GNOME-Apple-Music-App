# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

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

        # The content pane at 700, 1100 and 1920 px wide.
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



class FirstSyncTest(PageTestCase):
    """While the first sync fills an empty library, the pages are on their way, not empty."""

    async def test_home_albums_and_songs_show_loading_during_the_first_sync(self):
        from applemusic import pages
        from applemusic.sections import sidebar_sections

        destinations = {destination.key: destination
                        for section in sidebar_sections() for destination in section.destinations}
        self.library.syncing = True  # an empty library ('empty'), the first sync running
        shown = {}
        for key in ('home', 'albums', 'songs', 'radio'):
            page = await self.show_root(pages.create(destinations[key], self.library))
            await self.turn()
            shown[key] = page.stack.get_visible_child_name()
        self.assertEqual(shown, dict.fromkeys(shown, 'loading'))

        self.library.syncing = False  # it ended with nothing: empty
        self.assertEqual(page.stack.get_visible_child_name(), 'empty')


SIGNED_OUT = 'Sign in to Apple Music to see your library'


def offer(page):
    """The Sign In… button on a page's empty state (pages.SignInOffer's)."""
    return page.empty_page.get_child()


class SignInOfferTest(PageTestCase):
    """A library page's empty state while signed out (pages.SignInOffer): Sign In… and what
    signing in does; the page's own words once signed in, and in the demo."""

    async def empty_grid(self):
        from gi.repository import Gio

        from applemusic.library import Item
        from applemusic.pages.grid import GridPage

        page = GridPage(self.library, 'Albums', Gio.ListStore(item_type=Item),
                        empty_title='No Albums',
                        empty_description='Albums in your library appear here')
        await self.show_root(page)
        self.assertEqual(page.stack.get_visible_child_name(), 'empty')
        return page

    async def test_signed_out_offers_sign_in_and_says_what_it_does(self):
        self.app.settings.set_boolean('signed-in', False)
        page = await self.empty_grid()
        button = offer(page)
        self.assertTrue(button.get_visible())
        self.assertEqual(button.get_label(), 'Sign In…')
        self.assertEqual(button.get_action_name(), 'app.sign-in')
        self.assertTrue(button.has_css_class('pill'))
        self.assertTrue(button.has_css_class('suggested-action'))
        self.assertEqual(page.empty_page.get_title(), 'No Albums')
        self.assertEqual(page.empty_page.get_description(), SIGNED_OUT)
        # Signed in: the page's own words, no button (a first sync is the loading state).
        self.app.settings.set_boolean('signed-in', True)
        self.assertFalse(button.get_visible())
        self.assertEqual(page.empty_page.get_description(), 'Albums in your library appear here')

    async def test_the_demo_has_no_account_to_sign_in_to(self):
        self.app.demo = True
        self.app.settings.set_boolean('signed-in', False)
        page = await self.empty_grid()
        self.assertFalse(offer(page).get_visible())
        self.assertEqual(page.empty_page.get_description(), 'Albums in your library appear here')

    async def test_a_page_shown_again_catches_up(self):
        page = await self.empty_grid()
        self.assertFalse(offer(page).get_visible())
        self.window.navigation_view.replace([self.window.root_page])  # hidden: not following
        self.assertTrue(await self.until(lambda: not page.get_mapped()))
        self.app.settings.set_boolean('signed-in', False)
        self.assertFalse(offer(page).get_visible())
        self.window.navigation_view.replace([page])
        self.assertTrue(await self.until(page.get_mapped))
        self.assertTrue(offer(page).get_visible())
        self.assertEqual(page.empty_page.get_description(), SIGNED_OUT)

    async def test_every_library_page_offers_it(self):
        from applemusic import pages
        from applemusic.sections import sidebar_sections

        self.app.settings.set_boolean('signed-in', False)
        destinations = {destination.key: destination
                        for section in sidebar_sections() for destination in section.destinations}
        offered = {}
        for key in ('home', 'radio', 'albums', 'artists', 'recently-added', 'songs',
                    'all-playlists', 'favourite-songs', 'music-videos'):
            page = await self.show_root(pages.create(destinations[key], self.library))
            await self.turn()
            self.assertEqual(page.stack.get_visible_child_name(), 'empty', key)
            offered[key] = (offer(page).get_visible(), page.empty_page.get_description())
        self.assertEqual(offered, dict.fromkeys(offered, (True, SIGNED_OUT)))


if __name__ == '__main__':
    unittest.main()
