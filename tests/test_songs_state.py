# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The Songs page (pages/songs.py): what it shows while the songs are on their way, and a
sync that changes the songs keeps the table's place; the Sort By menu sorts as a header does;
a row plays its own song, on a click; the row of the song playing is marked.
Over an invented library.json in a temporary cache (tests/page_harness.py's window)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.page_harness import PageTestCase, playing, track


def library_json(cache, tracks):
    play = {'kind': 'album', 'id': 'l.album001'}
    album = {'id': 'l.album001', 'kind': 'album', 'title': 'Invented Album',
             'subtitle': 'Invented Artist', 'play': play,
             'groups': [{'name': '', 'play': play,
                         'entries': [track('l.album001', n, title=f'Song {n:04d}')
                                     for n in range(tracks)]}]}
    data = {'version': 1, 'generated': '2026-01-01T00:00:00Z', 'storefront': 'gb',
            'sections': {'albums': [album], 'playlists': [], 'radio': []}, 'shelves': []}
    Path(cache, 'library.json').write_text(json.dumps(data), encoding='utf-8')


class SongsStateTest(PageTestCase):
    def test_songs_that_exist_but_are_not_ordered_yet_are_loading(self):
        from applemusic.pages.songs import songs_state

        self.assertEqual(songs_state(0, 'ready', True, 3), 'loading')  # not ordered yet
        self.assertEqual(songs_state(0, 'ready', True, 0), 'empty')
        self.assertEqual(songs_state(5, 'ready', True, 5), 'items')
        self.assertEqual(songs_state(0, 'loading', True, 0), 'loading')
        self.assertEqual(songs_state(0, 'ready', False, 0), 'loading')  # not built yet
        # The first sync fills an empty library: on its way, not "No Songs".
        self.assertEqual(songs_state(0, 'empty', True, 0, syncing=True), 'loading')
        self.assertEqual(songs_state(0, 'empty', True, 0, syncing=False), 'empty')


class SongsPageTest(PageTestCase):
    async def asyncSetUp(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.cache = cache.name
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.cache})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def songs_page(self, tracks):
        from applemusic.pages.songs import SongsPage

        library_json(self.cache, tracks)
        await self.library.load()
        page = await self.show_root(SongsPage(self.library, 'Songs'))
        self.assertTrue(await self.until(lambda: page._rows.get_n_items() == tracks, 2))
        return page

    async def test_a_sync_that_adds_a_song_keeps_the_scroll_position(self):
        page = await self.songs_page(300)
        adjustment = page.column_view.get_vadjustment()
        self.assertTrue(await self.until(lambda: adjustment.get_upper() > 3000))
        adjustment.set_value(2000)
        await self.turn()
        library_json(self.cache, 301)
        await self.library.reload()
        self.assertTrue(await self.until(lambda: page._rows.get_n_items() == 301, 2))
        await self.turn()
        self.assertEqual(adjustment.get_value(), 2000)

    async def test_the_sort_menu_sorts_as_a_header_does(self):
        page = await self.songs_page(5)
        page.activate_action('songs.sort-order', _variant('descending'))
        self.assertEqual(page._rows.get_item(0).title, 'Song 0004')
        sorter = page.column_view.get_sorter()
        self.assertIs(sorter.get_primary_sort_column(), page.title_column)
        page.activate_action('songs.sort-column', _variant('time'))
        self.assertIs(sorter.get_primary_sort_column(), page.time_column)
        # A header click (the sorter changing) moves the menu's choice.
        page.column_view.sort_by_column(None, 0)
        page.column_view.sort_by_column(page.album_column, 0)
        self.assertEqual(page._sort_column.get_state().get_string(), 'album')
        self.assertEqual(page._sort_order.get_state().get_string(), 'ascending')

    async def test_a_filter_set_from_elsewhere_applies_at_once(self):
        page = await self.songs_page(12)
        page.set_filter('Song 0011')
        self.assertEqual(page._rows.get_n_items(), 1)
        self.assertEqual(page.count_label.get_label(), '1 of 12 songs')

    async def test_a_row_plays_its_album_from_its_own_song(self):
        from gi.repository import Gtk

        page = await self.songs_page(5)
        page.activate_action('songs.sort-order', _variant('descending'))
        page.on_activate(page.column_view, 1)
        window = page.get_root()
        track = page._rows.get_item(1)
        self.assertEqual(window.played[-1], (track.play, track.index, None))
        self.assertEqual(window.started_with[-1], track.id)
        # On a click; with no selection, which GTK would move to the hovered row.
        self.assertTrue(page.column_view.get_single_click_activate())
        self.assertIsInstance(page.column_view.get_model(), Gtk.NoSelection)

    async def test_the_song_playing_is_marked(self):
        page = await self.songs_page(5)
        self.assertTrue(await self.until(lambda: len(page._title_cells) == 5))
        cells = {cell.get_item().id: cell.get_child() for cell in page._title_cells}
        rows = {row.get_item().id: row for row in page._row_items}
        self.assertEqual(len(rows), 5)
        self.app.player.track = playing(track('l.album001', 2))
        self.assertEqual([song_id for song_id, cell in cells.items() if cell.playing],
                         ['l.album001.t1.2'])
        self.assertEqual(rows['l.album001.t1.2'].get_accessible_description(), 'Playing')
        self.assertEqual(rows['l.album001.t1.1'].get_accessible_description(), '3:00')
        self.app.player.track = None
        self.assertFalse(any(cell.playing for cell in cells.values()))
        self.assertEqual(rows['l.album001.t1.2'].get_accessible_description(), '3:00')


def _variant(text):
    from gi.repository import GLib

    return GLib.Variant('s', text)


if __name__ == '__main__':
    unittest.main()
