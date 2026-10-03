# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The Artists page (pages/artists.py): the library's artists A to Z beside the library's page
of the one selected, the collapsed layout's one pane at a time and its way back, and the
loading and empty states. Invented library items in a stand-in window."""

import unittest

from tests.gtk import requires_gtk
from tests.page_harness import PageTestCase, album, artist


@requires_gtk
class ArtistsPageTest(PageTestCase):
    def add_artist(self, artist_id, name, number):
        """An artist of the library's with one album (number) the library has."""
        from applemusic.library import Item

        item = Item(album(number, tracks=1))
        self.library._index[('album', item.id)] = item
        self.library.albums.append(item)
        data = artist([album(number)], artist_id=artist_id)
        data['title'] = name
        data['groups'][0]['entries'] = []
        band = Item(data)
        self.library._index[('artist', band.id)] = band
        self.library.artists.append(band)
        return band, item

    async def page(self):
        from applemusic.pages.artists import ArtistsPage

        page = await self.show_root(ArtistsPage(self.library, 'Artists'))
        await self.turn()
        return page

    def listed(self, page):
        model = page.list_view.get_model()
        return [model.get_item(position).title for position in range(model.get_n_items())]

    async def test_the_artists_a_to_z_beside_the_first(self):
        self.library.state = 'ready'
        zed, zed_album = self.add_artist('l.art2', 'Zephyr Lines', 2)
        aura, aura_album = self.add_artist('l.art1', 'Aura Invented', 1)
        page = await self.page()
        self.assertEqual(page.stack.get_visible_child_name(), 'artists')
        self.assertEqual(self.listed(page), ['Aura Invented', 'Zephyr Lines'])
        self.assertIs(page.detail.artist, aura)
        self.assertEqual(page.detail.shown_albums(), [aura_album])
        # Another selected: shown in place.
        page.list_view.get_model().set_selected(1)
        self.assertIs(page.detail.artist, zed)
        self.assertEqual(page.detail.shown_albums(), [zed_album])
        self.assertFalse(page.can_go_back())  # side by side, there is no back

    async def test_collapsed_one_pane_at_a_time(self):
        self.library.state = 'ready'
        self.add_artist('l.art1', 'Aura Invented', 1)
        band, _album = self.add_artist('l.art2', 'Zephyr Lines', 2)
        page = await self.page()
        changes = []
        page.connect('panes-changed', lambda _page: changes.append(True))
        page.split_view.set_collapsed(True)
        self.assertTrue(changes)
        self.assertFalse(page.split_view.get_show_content())  # the list first
        self.assertFalse(page.can_go_back())
        # Enter (the list's activate) shows the artist's albums, and Back the list again.
        page.list_view.emit('activate', 1)
        self.assertIs(page.detail.artist, band)
        self.assertTrue(page.split_view.get_show_content())
        self.assertTrue(page.can_go_back())
        self.assertIs(page.banner_host().get_ancestor(type(page.detail)), page.detail)
        self.assertTrue(page.go_back())
        self.assertFalse(page.split_view.get_show_content())
        self.assertFalse(page.go_back())
        self.assertIs(page.banner_host(), page.list_toolbar)

    async def test_the_list_has_its_first_rows_at_once_and_the_rest_frames_apart(self):
        from applemusic.pages.artists import FIRST_ROWS

        self.library.state = 'ready'
        for number in range(FIRST_ROWS + 30):
            self.add_artist(f'l.art{number}', f'Invented Artist {number:03d}', number)
        page = await self.page()
        self.assertTrue(await self.until(
            lambda: len(self.listed(page)) == FIRST_ROWS + 30))
        self.assertEqual(self.listed(page)[-1], f'Invented Artist {FIRST_ROWS + 29:03d}')

    async def test_loading_then_empty(self):
        self.library.state = 'loading'
        page = await self.page()
        self.assertEqual(page.stack.get_visible_child_name(), 'loading')
        self.library.state = 'ready'
        self.assertEqual(page.stack.get_visible_child_name(), 'empty')
        self.assertEqual(page.empty_page.get_title(), 'No Artists')
        # An artist arriving (a sync) shows the list, the artist selected.
        band, _album = self.add_artist('l.art1', 'Aura Invented', 1)
        self.assertEqual(page.stack.get_visible_child_name(), 'artists')
        self.assertIs(page.detail.artist, band)


if __name__ == '__main__':
    unittest.main()
