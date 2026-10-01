# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The album, playlist and artist pages over the stand-in engine (tests/page_harness.py): an
item without tracks is fetched once, a popped page's fetch is cancelled, and the pages follow
their Item."""

import asyncio
import unittest

from tests.page_harness import PageTestCase, album, artist, artist_answer

from applemusic.backend.errors import EngineError


class DetailPageTest(PageTestCase):
    def page(self, data):
        from applemusic.library import Item
        from applemusic.pages.detail import DetailPage

        self.item = Item(data)
        return DetailPage(self.library, self.item)

    def rows(self, page):
        return page.list_view.get_model().get_n_items()

    def test_should_fetch(self):
        from applemusic.library import Item
        from applemusic.pages.detail import should_fetch

        groupless = Item(album(1, tracks=0))
        self.assertTrue(should_fetch(groupless, None))
        self.assertFalse(should_fetch(groupless, groupless))  # asked once already
        self.assertFalse(should_fetch(Item(album(2)), None))  # has its tracks
        self.assertFalse(should_fetch(Item(dict(album(3, tracks=0), kind='station')), None))
        self.assertFalse(should_fetch(None, None))

    async def test_an_answer_without_tracks_is_asked_for_once(self):
        self.app.engine.answers['item'] = dict(album(1, tracks=0), summary='Invented notes')
        page = await self.push(self.page(album(1, tracks=0)))
        await self.settle()
        await self.turn()
        self.assertEqual(self.app.engine.calls, ['item'])
        self.assertEqual(page.status_title.get_label(), 'No Songs')
        self.assertFalse(page.status_button.get_visible())
        self.assertFalse(page.play_button.get_sensitive())  # nothing to play
        self.assertEqual(page.summary_label.get_label(), 'Invented notes')

    async def test_an_answer_with_tracks_shows_them(self):
        self.app.engine.answers['item'] = album(1, tracks=4)
        page = await self.push(self.page(album(1, tracks=0)))
        self.assertTrue(page.play_button.get_sensitive())  # the engine plays it by its id
        await self.settle()
        self.assertTrue(await self.until(lambda: self.rows(page) == 5))  # the hero and four
        self.assertFalse(page.status_box.get_visible())
        self.assertTrue(page.play_button.get_sensitive())

    async def test_try_again_asks_again(self):
        self.app.engine.answers['item'] = EngineError('timeout')
        page = await self.push(self.page(album(1, tracks=0)))
        await self.settle()
        self.assertEqual(page.status_button.get_label(), 'Try Again')
        self.app.engine.answers['item'] = album(1, tracks=2)
        page.status_button.emit('clicked')
        await self.settle()
        self.assertEqual(self.app.engine.calls, ['item', 'item'])
        self.assertTrue(await self.until(lambda: self.rows(page) == 3))
        self.assertEqual(self.app.reported, [])  # shown on the page, not toasted too

    async def test_popping_the_page_cancels_its_fetch(self):
        waiting = asyncio.get_running_loop().create_future()

        async def never(*_args):
            await waiting
            return album(1)

        self.app.engine.answers['item'] = never
        page = await self.push(self.page(album(1, tracks=0)))
        await self.turn()
        task = page._fetch_task
        self.assertFalse(task.done())
        self.window.navigation_view.pop()
        self.assertTrue(await self.until(lambda: task.done()))
        self.assertTrue(task.cancelled())

    async def test_a_reload_that_changes_the_tracks_shows_them(self):
        page = await self.push(self.page(album(1, tracks=3, kind='playlist')))
        self.assertEqual(self.rows(page), 4)
        self.item.merge(album(1, tracks=5, kind='playlist'), replace=True)
        self.assertEqual(self.rows(page), 6)
        renamed = dict(album(1, tracks=5, kind='playlist'), title='Renamed Playlist')
        self.item.merge(renamed, replace=True)
        self.assertEqual(page.title_label.get_label(), 'Renamed Playlist')

    async def test_a_root_page_catches_up_when_shown_again(self):
        from applemusic.library import Item
        from applemusic.pages.detail import DetailPage

        item = Item(album(1, tracks=3, kind='playlist'))
        page = await self.show_root(DetailPage(self.library, find=lambda: item, root=True,
                                               title='Invented Root'))
        self.assertEqual(self.rows(page), 4)
        self.window.navigation_view.replace([self.window.root_page])  # hidden
        self.assertTrue(await self.until(lambda: not page.get_mapped()))
        item.merge(album(1, tracks=1, kind='playlist'), replace=True)
        self.window.navigation_view.replace([page])
        self.assertTrue(await self.until(page.get_mapped))
        self.assertEqual(self.rows(page), 2)

    async def test_play_does_not_shuffle(self):
        page = await self.push(self.page(album(1)))
        page.play_button.emit('clicked')
        self.assertEqual(self.window.played, [(self.item.play, None, False)])

    def test_resolve_artist(self):
        from applemusic.library import Item
        from applemusic.pages.detail import resolve_artist

        artist_item = Item(artist([]))

        class Library:
            artists = [artist_item]

            def by_id(self, kind, item_id):
                return artist_item if (kind, item_id) == ('artist', 'l.artist001') else None

        library = Library()
        by_name = Item(dict(album(1), subtitle='INVENTED ARTIST'))
        self.assertIs(resolve_artist(library, by_name), artist_item)  # case folded
        by_id = Item(dict(album(2), subtitle='Another Name', artistId='l.artist001'))
        self.assertIs(resolve_artist(library, by_id), artist_item)
        self.assertIsNone(resolve_artist(library, Item(dict(album(3), subtitle='Nobody'))))
        playlist = Item(album(4, kind='playlist'))  # its subtitle is its curator
        self.assertIsNone(resolve_artist(library, playlist))

    async def test_the_artist_links_to_their_page(self):
        from applemusic.library import Item

        # The library's artist opens at once.
        self.library.artists.append(Item(artist([])))
        page = await self.push(self.page(album(1)))
        self.assertTrue(page.artist_button.get_visible())
        self.assertFalse(page.subtitle_label.get_visible())
        page.artist_button.emit('clicked')
        self.assertEqual([item.id for item in self.window.opened], ['l.artist001'])

    async def test_a_catalog_albums_artist_is_looked_up(self):
        # Not the library's: a catalog album's artist is linked, and found on a click.
        page = await self.push(self.page(dict(album(1), id='1000000300')))
        self.assertTrue(page.artist_button.get_visible())
        page.artist_button.emit('clicked')
        self.assertEqual(self.window.item_actions.went, [(self.item, 'artist')])
        # Nor in the demo, or for the library's own album of an artist it lacks.
        self.app.demo = True
        page = await self.push(self.page(dict(album(2), id='1000000301')))
        self.assertFalse(page.artist_button.get_visible())
        self.assertTrue(page.subtitle_label.get_visible())
        self.app.demo = False
        page = await self.push(self.page(album(3)))
        self.assertFalse(page.artist_button.get_visible())

    async def test_a_wide_playlist_is_a_table(self):
        # The stand-in window is 1000 px wide: the page's breakpoint makes it a table, its
        # column titles the tracks' header and each row's artist and album links.
        page = await self.push(self.page(album(1, tracks=3, kind='playlist')))
        self.assertTrue(await self.until(lambda: page.table and len(page._bound) == 3))
        self.assertIs(page.list_view.get_header_factory(), page._table_header_factory)
        rows = [item.get_child().track_row for item in page._bound]
        for row in rows:
            self.assertTrue(row.artist_link.get_visible())
            self.assertEqual(row.artist_link.get_text(), 'Invented Artist')
            self.assertEqual(row.album_link.get_text(), 'l.playlist001')
            self.assertFalse(row.artist_label.get_visible())
            self.assertTrue(row.columns.get_homogeneous())
        # Narrower: the list's rows again, the artist under the title.
        page.table = False
        self.assertIsNone(page.list_view.get_header_factory())
        for row in rows:
            self.assertFalse(row.artist_link.get_visible())
            self.assertTrue(row.artist_label.get_visible())
            self.assertFalse(row.columns.get_homogeneous())

    async def test_an_album_is_never_a_table(self):
        page = await self.push(self.page(album(1, tracks=3)))
        self.assertTrue(await self.until(lambda: page.table and len(page._bound) == 3))
        self.assertIsNone(page.list_view.get_header_factory())
        self.assertFalse(any(item.get_child().track_row.artist_link.get_visible()
                             for item in page._bound))

    async def test_a_link_opens_its_page(self):
        from applemusic.widgets import track_links

        page = await self.push(self.page(album(1, tracks=3, kind='playlist')))
        self.assertTrue(await self.until(lambda: page.table and len(page._bound) == 3))
        row = next(iter(page._bound)).get_child().track_row
        await self.until(lambda: row.album_link.get_width() > 0)
        self.assertTrue(row.album_link.active)
        found, centre = row.album_link.compute_bounds(page.list_view)
        self.assertTrue(found)
        x = centre.get_x() + centre.get_width() / 2
        y = centre.get_y() + centre.get_height() / 2
        self.assertIs(track_links.link_at(page.list_view, x, y), row.album_link)
        self.assertIsNone(track_links.link_at(page.list_view, x + centre.get_width(), y))
        self.assertTrue(track_links.open_link(page.list_view, row.album_link))
        self.assertEqual(self.window.item_actions.went,
                         [(row.context_item, 'album')])

    async def test_the_more_options_menu_is_the_items(self):
        page = await self.push(self.page(album(1)))
        page.more_button.popup()
        self.assertTrue(await self.until(lambda: self.window.item_actions.asked))
        self.assertIs(self.window.item_actions.asked[-1], self.item)
        page.more_button.popdown()

    async def test_shift_tab_from_the_first_track_goes_back_to_the_hero(self):
        from gi.repository import Gdk, Gtk

        page = await self.push(self.page(album(1, tracks=3)))
        page.more_button.grab_focus()
        keys = next(controller for controller in _controllers(page.list_view)
                    if isinstance(controller, Gtk.EventControllerKey))
        self.assertTrue(keys.emit('key-pressed', Gdk.KEY_Tab, 0, Gdk.ModifierType(0)))
        self.assertTrue(await self.until(
            lambda: self.window.get_focus() is not None
            and self.window.get_focus().is_ancestor(page.list_view)))
        self.assertTrue(keys.emit('key-pressed', Gdk.KEY_ISO_Left_Tab, 0,
                                  Gdk.ModifierType.SHIFT_MASK))
        self.assertTrue(self.window.get_focus().is_ancestor(page.more_button))  # its toggle


def _controllers(widget):
    model = widget.observe_controllers()
    return [model.get_item(position) for position in range(model.get_n_items())]


class ArtistPageTest(PageTestCase):
    """The artist page over the catalog's answer (Engine.artist_page), and without it."""

    async def artist_page(self, data, answer=None):
        from applemusic.library import Item
        from applemusic.pages.artist import ArtistPage

        if answer is not None:
            self.app.engine.answers['artist_page'] = answer
        page = await self.push(ArtistPage(self.library, Item(data)))
        await self.settle()
        await self.turn()
        return page

    async def test_the_catalog_answers_the_page(self):
        page = await self.artist_page(dict(artist([album(1)]), catalogId='42'),
                                      artist_answer())
        self.assertEqual(self.app.engine.calls, ['artist_page'])
        self.assertEqual(page.release_title.get_label(), 'Latest Release')
        self.assertEqual(page.release_name.get_label(), 'Invented album 99')
        self.assertTrue(page.release_date.get_label().endswith('2026'))
        self.assertTrue(page.top_songs.get_visible())
        self.assertEqual(page.top_songs.shelf.items.get_n_items(), 4)
        # Apple's shelves in its order, About before Similar Artists; the library's albums
        # are not shown beside the catalog's.
        self.assertEqual([shelf.key for shelf in page._column.shelves],
                         ['featured-albums', 'full-albums'])
        self.assertEqual([shelf.key for shelf in page._after.shelves], ['similar-artists'])
        self.assertTrue(page._column.widgets[0].props.hero)  # Essential Albums: large cards
        self.assertFalse(page.status_page.get_visible())
        self.assertEqual(page.about_title.get_label(), 'About Invented Artist')
        self.assertEqual(page.summary_label.get_label(), 'An invented biography.')
        self.assertEqual(page.origin_label.get_label(), 'Invented Town, Nowhere')
        self.assertEqual(page.born_heading.get_label(), 'Born')
        self.assertTrue(page.play_button.get_visible())  # the catalog artist's top songs

    async def test_a_group_was_formed(self):
        page = await self.artist_page(dict(artist([]), catalogId='42'),
                                      artist_answer(group=True))
        self.assertEqual(page.born_heading.get_label(), 'Formed')

    async def test_a_library_artist_is_found_through_its_songs(self):
        data = artist([album(1), album(2)])
        for group in data['groups']:
            for number, entry in enumerate(group['entries']):
                entry['catalogId'] = f'{group["play"]["id"]}.{number}'
        asked = []

        def catalog_artist(name, song_ids):
            asked.append((name, song_ids))
            return '42'

        self.app.engine.answers['catalog_artist'] = catalog_artist
        await self.artist_page(data, artist_answer())
        self.assertEqual(self.app.engine.calls, ['catalog_artist', 'artist_page'])
        # One song an album, as the engine reads each with its artists.
        self.assertEqual(asked, [('Invented Artist', ['l.album001.0', 'l.album002.0'])])

    async def test_an_artist_with_nothing_to_find_shows_no_albums(self):
        page = await self.artist_page(artist([]))
        self.assertEqual(self.app.engine.calls, [])  # no catalog id, no songs to ask about
        self.assertTrue(page.status_page.get_visible())
        self.assertEqual(page.status_page.get_title(), 'No Albums')

    async def test_without_the_catalog_the_library_albums_show(self):
        page = await self.artist_page(dict(artist([album(1), album(2)]), catalogId='42'))
        self.assertEqual(self.app.engine.calls, ['artist_page'])  # the engine is down
        self.assertEqual([shelf.key for shelf in page._column.shelves], ['library'])
        self.assertEqual(page._column.widgets[0].shelf.items.get_n_items(), 2)
        self.assertTrue(page.status_page.get_visible())  # Start Engine, under the albums
        self.assertTrue(page.status_button.get_visible())

    async def test_a_top_song_plays_the_songs_from_it(self):
        page = await self.artist_page(dict(artist([]), catalogId='42'), artist_answer())
        page.top_songs.grid_view.emit('activate', 2)
        window = page.get_root()
        self.assertEqual(window.played[-1],
                         ({'kind': 'songs', 'id': '900,901,902,903'}, 2, None))
        self.assertEqual(window.started_with[-1], '902')

    async def test_see_all_fetches_the_rest_of_a_shelf(self):
        page = await self.artist_page(dict(artist([]), catalogId='42'), artist_answer())
        albums = page._column.shelves[1]
        first = albums.items.get_item(0)
        self.assertTrue(albums.more)
        rest = artist_answer()['shelves'][1]['items'] + [
            dict(artist_answer()['shelves'][1]['items'][0], id=f'album{n}',
                 title=f'Invented album {n}') for n in range(4, 8)]
        self.app.engine.answers['artist_view'] = rest
        await albums.complete()
        self.assertEqual(albums.items.get_n_items(), 7)
        self.assertIs(albums.items.get_item(0), first)  # what was shown stays
        self.assertFalse(albums.more)

    async def test_an_album_the_library_lacks_is_fetched_when_opened(self):
        page = await self.artist_page(artist([album(1), album(2)]))
        albums = page._library_shelf.items
        self.assertEqual(albums.get_n_items(), 2)
        stand_in = albums.get_item(0)
        self.assertEqual(stand_in.kind, 'album')
        self.assertEqual(stand_in.groups, [])  # its page asks the engine for the whole album
        self.assertEqual(stand_in.subtitle, 'Invented Artist')

    async def test_the_page_follows_its_artist(self):
        from applemusic.library import Item
        from applemusic.pages.artist import ArtistPage

        item = Item(artist([album(1)]))
        page = await self.push(ArtistPage(self.library, item))
        item.merge(artist([album(1), album(2), album(3)]), replace=True)
        self.assertEqual(page._library_shelf.items.get_n_items(), 3)


class ArtistWordsTest(PageTestCase):
    def test_a_release_date_in_the_readers_words(self):
        from applemusic.pages.artist import release_date

        self.assertRegex(release_date('2026-09-24'), r'^24 \w+ 2026$')
        self.assertEqual(release_date(''), '')
        self.assertEqual(release_date('soon'), '')
        self.assertEqual(release_date('2026-13-40'), '')

    def test_a_catalog_artist_and_a_library_one(self):
        from applemusic.library import Item
        from applemusic.pages.artist import catalog_id

        self.assertEqual(catalog_id(Item({'id': '42', 'kind': 'artist'})), '42')
        self.assertEqual(catalog_id(Item({'id': 'l.art001', 'kind': 'artist',
                                          'catalogId': '43'})), '43')
        self.assertIsNone(catalog_id(Item({'id': 'l.art_abc', 'kind': 'artist'})))


if __name__ == '__main__':
    unittest.main()
