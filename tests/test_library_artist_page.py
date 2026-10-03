# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The library's page of an artist (pages/library_artist.py): the heading, the albums as tiles
with their years, newest first or by title, Play and Shuffle, the link to Apple Music's page
of the artist, and a new artist shown in place. Invented library items in a stand-in window."""

import unittest

from tests.gtk import requires_gtk
from tests.page_harness import PageTestCase, album, artist, find_all


@requires_gtk
class LibraryArtistPageTest(PageTestCase):
    def library_with(self, *numbers_and_years, artist_id='l.artist001', name='Invented Artist'):
        """An artist of the library's with an album for each (number, year), all in the
        library as the sync leaves them (the artist's groups without their tracks)."""
        from applemusic.library import Item

        albums = []
        for number, year in numbers_and_years:
            item = Item(album(number, tracks=2, year=year))
            self.library._index[('album', item.id)] = item
            self.library.albums.append(item)
            albums.append(item)
        data = artist([album(number, year=year) for number, year in numbers_and_years],
                      artist_id=artist_id)
        data['title'] = name
        for group in data['groups']:
            group['entries'] = []
        band = Item(data)
        self.library._index[('artist', band.id)] = band
        self.library.artists.append(band)
        return band, albums

    async def page(self, band):
        from applemusic.pages.library_artist import LibraryArtistPage

        page = await self.push(LibraryArtistPage(self.library, band))
        await self.until(lambda: page.grid_view.get_model().get_n_items() > 0)
        return page

    async def test_the_albums_newest_first_with_their_years(self):
        from applemusic.widgets.tile import Tile

        band, (first, second, third) = self.library_with((1, 2005), (2, 2019), (3, 2011))
        page = await self.page(band)
        self.assertEqual(page.name_label.get_label(), 'Invented Artist')
        self.assertEqual(page.get_title(), 'Invented Artist')
        self.assertEqual(page.shown_albums(), [second, third, first])
        self.assertEqual(page.stack.get_visible_child_name(), 'items')
        self.assertTrue(await self.until(lambda: len(find_all(page.grid_view, Tile)) == 3))
        labels = {tile.label.get_text() for tile in find_all(page.grid_view, Tile)}
        self.assertEqual(labels, {f'{item.title}\n{item.year}'
                                  for item in (first, second, third)})
        # The page is not Go to Artist's page of them: it has no `item`.
        self.assertFalse(hasattr(page, 'item'))

    async def test_sort_by_title_either_way(self):
        band, (first, second, third) = self.library_with((3, 2005), (1, 2019), (2, 2011))
        page = await self.page(band)
        page.activate_action('page.sort', variant('title'))
        self.assertEqual(page.shown_albums(), [second, third, first])
        page.activate_action('page.sort-order', variant('descending'))
        self.assertEqual(page.shown_albums(), [first, third, second])
        # Remembered for the next page (the direction is the page's own).
        self.assertEqual(self.app.settings.get_value('grid-sort').unpack().get('library-artist'),
                         'title')
        self.app.settings.reset('grid-sort')

    async def test_play_and_shuffle_queue_the_songs_as_shown(self):
        band, (first, second) = self.library_with((1, 2005), (2, 2019))
        page = await self.page(band)
        page.play_button.emit('clicked')
        page.shuffle_button.emit('clicked')
        ids = ','.join(['l.album002.t1.0', 'l.album002.t1.1', 'l.album001.t1.0',
                        'l.album001.t1.1'])
        self.assertEqual(self.window.played, [({'kind': 'songs', 'id': ids}, None, False),
                                              ({'kind': 'songs', 'id': ids}, None, True)])

    async def test_the_name_links_to_apple_musics_page_and_tiles_open_albums(self):
        band, (first,) = self.library_with((1, 2005))
        page = await self.page(band)
        page.name_button.emit('clicked')
        self.assertEqual(self.window.artist_pages, [band])
        page.grid_view.emit('activate', 0)
        self.assertEqual(self.window.opened, [first])
        page.more_button.popup()
        self.assertTrue(await self.until(lambda: self.window.item_actions.asked))
        self.assertIs(self.window.item_actions.asked[-1], band)
        page.more_button.popdown()

    async def test_another_artist_in_place_and_a_reload_followed(self):
        from applemusic.library import Item

        band, albums = self.library_with((1, 2005), (2, 2019))
        other, (third,) = self.library_with((3, 2001), artist_id='l.artist002', name='Another')
        page = await self.page(band)
        page.set_artist(other)
        self.assertEqual(page.shown_albums(), [third])
        self.assertEqual(page.name_label.get_label(), 'Another')
        # The artist's groups change (a sync): the page follows while shown.
        fourth = Item(album(4, tracks=1, year=2030))
        self.library._index[('album', fourth.id)] = fourth
        data = dict(other.raw)
        data['groups'] = [{'name': fourth.title, 'play': dict(fourth.play), 'entries': []},
                          *data['groups']]
        other.merge(data, replace=True)
        self.assertTrue(await self.until(lambda: page.shown_albums() == [fourth, third]))
        # Nothing: the heading goes, and so do the albums.
        page.set_artist(None)
        self.assertFalse(page.heading_box.get_visible())
        self.assertEqual(page.stack.get_visible_child_name(), 'empty')


def variant(text):
    from gi.repository import GLib

    return GLib.Variant('s', text)


if __name__ == '__main__':
    unittest.main()
