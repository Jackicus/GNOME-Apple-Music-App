"""The album, playlist and artist pages over the stand-in engine (tests/page_harness.py): an
item without tracks is fetched once, a popped page's fetch is cancelled, and the pages follow
their Item."""

import asyncio
import unittest

from tests.page_harness import PageTestCase, album, artist

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
    async def test_an_artist_without_albums_is_asked_for_once(self):
        from applemusic.library import Item
        from applemusic.pages.artist import ArtistPage

        self.app.engine.answers['item'] = artist([])
        page = await self.push(ArtistPage(self.library, Item(artist([]))))
        await self.settle()
        await self.turn()
        self.assertEqual(self.app.engine.calls, ['item'])
        self.assertEqual(page.status_page.get_title(), 'No Albums')

    async def test_an_album_the_library_lacks_is_fetched_when_opened(self):
        from applemusic.library import Item
        from applemusic.pages.artist import ArtistPage

        page = await self.push(ArtistPage(self.library, Item(artist([album(1), album(2)]))))
        self.assertEqual(page._albums.get_n_items(), 2)
        stand_in = page._albums.get_item(0)
        self.assertEqual(stand_in.kind, 'album')
        self.assertEqual(stand_in.groups, [])  # its page asks the engine for the whole album
        self.assertEqual(stand_in.subtitle, 'Invented Artist')

    async def test_the_page_follows_its_artist(self):
        from applemusic.library import Item
        from applemusic.pages.artist import ArtistPage

        item = Item(artist([album(1)]))
        page = await self.push(ArtistPage(self.library, item))
        item.merge(artist([album(1), album(2), album(3)]), replace=True)
        self.assertEqual(page._albums.get_n_items(), 3)


if __name__ == '__main__':
    unittest.main()
