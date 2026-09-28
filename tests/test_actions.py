"""The item actions (applemusic.actions): the menus built per kind, and the win.* actions run
against a stand-in window, app and engine, so add to library, add to playlist, love and the
drop onto a sidebar playlist are checked without Apple. Gio and GObject only, no display;
the actions' coroutines run under asyncio."""

import asyncio
import unittest

from gi.repository import Gio, GLib, GObject

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic.actions import (ItemActions, TrackRef, build_menu, fill_sidebar_menu,
                                library_target, playlist_song, queue_target, rating_target,
                                set_favourite, web_url)
from applemusic.backend.errors import EngineError
from applemusic.library import Item, PlaylistTree, Track

# Invented items, in the library's shapes.
LIBRARY_ALBUM = Item({'id': 'l.alb1', 'kind': 'album', 'title': 'Tidewater',
                      'subtitle': 'The Invented Band', 'play': {'kind': 'album', 'id': 'l.alb1'}})
MADE_UP_ALBUM = Item({'id': 'l.alb_0123456789ab', 'kind': 'album', 'title': 'Loose Ends',
                      'play': {'kind': 'album', 'id': 'l.alb_0123456789ab'}})
CATALOG_ALBUM = Item({'id': '1000000002', 'kind': 'album', 'title': 'Harbour Suite',
                      'url': 'https://music.apple.com/gb/album/harbour-suite/1000000002',
                      'play': {'kind': 'album', 'id': '1000000002'}})
LIBRARY_ARTIST = Item({'id': 'l.art_0123456789ab', 'kind': 'artist', 'title': 'The Band',
                       'play': {'kind': 'artist', 'id': 'l.art_0123456789ab'}})
CATALOG_ARTIST = Item({'id': '1000000005', 'kind': 'artist', 'title': 'Someone',
                       'play': {'kind': 'artist', 'id': '1000000005'}})
STATION = Item({'id': 'ra.1000000006', 'kind': 'station', 'title': 'Harbour Radio',
                'url': 'https://music.apple.com/gb/station/harbour-radio/ra.1000000006',
                'play': {'kind': 'station', 'id': 'ra.1000000006'}})
CATALOG_SONG = Item({'id': '1000000007', 'kind': 'song', 'title': 'Pilot Light',
                     'catalogId': '1000000007', 'play': {'kind': 'song', 'id': '1000000007'}})
FOLDER = Item({'id': 'l.fd1', 'kind': 'folder', 'title': 'Evenings'})
CATEGORY = Item({'id': '1000000008', 'kind': 'category', 'title': 'Jazz'})
PLAYLIST = Item({'id': 'p.pl1', 'kind': 'playlist', 'title': 'Road Trip',
                 'play': {'kind': 'playlist', 'id': 'p.pl1'}})
READ_ONLY = Item({'id': 'p.pl2', 'kind': 'playlist', 'title': 'Apple Picks',
                  'attributes': {'canEdit': False}, 'play': {'kind': 'playlist', 'id': 'p.pl2'}})
FAVOURITES = Item({'id': 'p.fav', 'kind': 'playlist', 'title': 'Favourite Songs',
                   'attributes': {'isFavourites': True},
                   'play': {'kind': 'playlist', 'id': 'p.fav'}})
LIBRARY_TRACK = Track({'id': 'i.song1', 'catalogId': '1000000001', 'title': 'Harbour Lights',
                       'index': 2}, play={'kind': 'album', 'id': 'l.alb1'})
CATALOG_TRACK = Track({'id': '1000000009', 'catalogId': '1000000009', 'title': 'Shoreline',
                       'index': 0}, play={'kind': 'album', 'id': '1000000002'})
UPLOAD_TRACK = Track({'id': 'i.song2', 'title': 'Demo Take', 'index': 0},
                     play={'kind': 'playlist', 'id': 'p.pl1'})


def actions_of(menu):
    """[(action, target)] of a menu's items, sections and submenus flattened, in order."""
    found = []
    for position in range(menu.get_n_items()):
        action = menu.get_item_attribute_value(position, Gio.MENU_ATTRIBUTE_ACTION)
        if action is not None:
            target = menu.get_item_attribute_value(position, Gio.MENU_ATTRIBUTE_TARGET)
            found.append((action.get_string(), target.unpack() if target else None))
        for link in (Gio.MENU_LINK_SECTION, Gio.MENU_LINK_SUBMENU):
            linked = menu.get_item_link(position, link)
            if linked is not None:
                found.extend(actions_of(linked))
    return found


def names(menu):
    return [action for action, _target in actions_of(menu)] if menu is not None else None


class MenuTest(unittest.TestCase):
    def test_library_album(self):
        self.assertEqual(names(build_menu(LIBRARY_ALBUM)), [
            'win.item-play', 'win.item-play-next', 'win.item-play-later', 'win.item-love',
            'win.item-open-in-browser', 'win.item-copy-link'])
        self.assertTrue(all(target == ('album', 'l.alb1')
                            for _action, target in actions_of(build_menu(LIBRARY_ALBUM))))

    def test_catalog_album_can_be_added(self):
        self.assertIn('win.item-add-to-library', names(build_menu(CATALOG_ALBUM)))

    def test_loved_offers_to_remove(self):
        menu = build_menu(LIBRARY_ALBUM, loved=True)
        self.assertIn('win.item-unlove', names(menu))
        self.assertNotIn('win.item-love', names(menu))

    def test_track_with_playlists(self):
        menu = build_menu(LIBRARY_TRACK, playlists=[('p.pl1', 'Road Trip'), ('p.pl3', '')])
        self.assertEqual(actions_of(menu), [
            ('win.item-play', ('song', 'i.song1')),
            ('win.item-play-next', ('song', 'i.song1')),
            ('win.item-play-later', ('song', 'i.song1')),
            ('win.item-love', ('song', 'i.song1')),
            ('win.item-add-to-playlist', ('p.pl1', 'song', 'i.song1')),
            ('win.item-add-to-playlist', ('p.pl3', 'song', 'i.song1')),
            ('win.item-open-in-browser', ('song', 'i.song1')),
            ('win.item-copy-link', ('song', 'i.song1'))])
        # No playlists: no submenu. A catalog track can be added to the library.
        self.assertNotIn('win.item-add-to-playlist', names(build_menu(LIBRARY_TRACK)))
        self.assertIn('win.item-add-to-library', names(build_menu(CATALOG_TRACK)))

    def test_what_has_no_menu(self):
        self.assertIsNone(build_menu(FOLDER))
        self.assertIsNone(build_menu(CATEGORY))
        self.assertIsNone(build_menu(LIBRARY_ARTIST))  # its id is the library's invention
        self.assertIsNone(build_menu(object()))

    def test_other_kinds(self):
        self.assertEqual(names(build_menu(CATALOG_ARTIST)), [
            'win.item-play', 'win.item-open-in-browser', 'win.item-copy-link'])
        self.assertEqual(names(build_menu(STATION)), [
            'win.item-play', 'win.item-love', 'win.item-open-in-browser', 'win.item-copy-link'])
        self.assertEqual(names(build_menu(FAVOURITES)), [
            'win.item-play', 'win.item-play-next', 'win.item-play-later',
            'win.item-open-in-browser', 'win.item-copy-link'])
        self.assertEqual(names(build_menu(MADE_UP_ALBUM)), ['win.item-play'])
        self.assertNotIn('win.item-open-in-browser', names(build_menu(UPLOAD_TRACK)))

    def test_set_favourite_swaps_the_item(self):
        menu = build_menu(LIBRARY_ALBUM)
        target = GLib.Variant('(ss)', ('album', 'l.alb1'))
        self.assertFalse(set_favourite(menu, target, False))
        self.assertTrue(set_favourite(menu, target, True))
        self.assertEqual(names(menu).count('win.item-unlove'), 1)
        self.assertNotIn('win.item-love', names(menu))
        self.assertEqual(names(menu).index('win.item-unlove'), 3)  # where Favourite was

    def test_sidebar_menu(self):
        menu = Gio.Menu()
        fill_sidebar_menu(menu, PLAYLIST)
        self.assertEqual(actions_of(menu), [
            ('win.item-play', ('playlist', 'p.pl1')),
            ('win.item-play-next', ('playlist', 'p.pl1')),
            ('win.item-open-in-browser', ('playlist', 'p.pl1'))])
        fill_sidebar_menu(menu, None)
        self.assertEqual(menu.get_n_items(), 0)

    def test_targets(self):
        self.assertEqual(queue_target(LIBRARY_TRACK), ('song', '1000000001'))
        self.assertEqual(queue_target(LIBRARY_ALBUM), ('album', 'l.alb1'))
        self.assertIsNone(queue_target(STATION))
        self.assertEqual(rating_target(LIBRARY_TRACK), ('song', '1000000001'))
        self.assertEqual(rating_target(UPLOAD_TRACK), ('song', 'i.song2'))
        self.assertIsNone(rating_target(FAVOURITES))
        self.assertIsNone(rating_target(CATALOG_ARTIST))
        self.assertIsNone(library_target(LIBRARY_TRACK))
        self.assertEqual(library_target(CATALOG_SONG), ('song', '1000000007'))
        self.assertEqual(playlist_song(LIBRARY_TRACK), 'i.song1')
        self.assertEqual(playlist_song(CATALOG_SONG), '1000000007')
        self.assertIsNone(playlist_song(LIBRARY_ALBUM))

    def test_web_urls(self):
        self.assertEqual(web_url(CATALOG_ALBUM), CATALOG_ALBUM.url)
        self.assertEqual(web_url(PLAYLIST), 'https://music.apple.com/library/playlist/p.pl1')
        self.assertEqual(web_url(LIBRARY_ALBUM), 'https://music.apple.com/library/albums/l.alb1')
        self.assertEqual(web_url(LIBRARY_TRACK, 'gb'), 'https://music.apple.com/gb/song/1000000001')
        self.assertEqual(web_url(CATALOG_ARTIST, 'gb'),
                         'https://music.apple.com/gb/artist/1000000005')
        for obj in (UPLOAD_TRACK, LIBRARY_ARTIST, MADE_UP_ALBUM, FOLDER, None):
            self.assertIsNone(web_url(obj))

    def test_track_ref(self):
        ref = TrackRef.for_object(LIBRARY_TRACK)
        self.assertEqual((ref.song_id, ref.title), ('i.song1', 'Harbour Lights'))
        self.assertEqual(TrackRef.for_object(CATALOG_SONG).song_id, '1000000007')
        self.assertIsNone(TrackRef.for_object(LIBRARY_ALBUM))


class FakeEngine(GObject.Object):
    """The Engine's surface the actions use: state, authorized, the `rated` signal, and the
    writes, recorded (nothing is sent anywhere)."""

    __gsignals__ = {
        'rated': (GObject.SignalFlags.RUN_FIRST, None, (str, str, int)),
    }

    state = GObject.Property(type=str, default='up')
    authorized = GObject.Property(type=bool, default=True)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail = None
        self.ratings = {}
        self.catalog_urls = {}

    async def _record(self, name, *args):
        self.calls.append((name, *args))
        if self.fail is not None:
            raise self.fail

    async def love(self, kind, item_id):
        await self._record('love', kind, item_id)
        self.emit('rated', kind, item_id, 1)

    async def unlove(self, kind, item_id):
        await self._record('unlove', kind, item_id)
        self.emit('rated', kind, item_id, 0)

    async def rating(self, kind, item_id):
        await self._record('rating', kind, item_id)
        value = self.ratings.get((kind, item_id), 0)
        self.emit('rated', kind, item_id, value)
        return value

    async def add_to_library(self, kind, item_id):
        await self._record('add_to_library', kind, item_id)

    async def add_to_playlist(self, playlist_id, song_id):
        await self._record('add_to_playlist', playlist_id, song_id)

    async def catalog_url(self, kind, item_id):
        await self._record('catalog_url', kind, item_id)
        return self.catalog_urls.get(item_id)


class FakePlayer:
    def __init__(self, engine):
        self.engine = engine
        self.ensured = 0

    async def ensure_engine(self):
        self.ensured += 1

    async def play_next(self, kind, item_id):
        await self.engine._record('play_next', kind, item_id)

    async def play_later(self, kind, item_id):
        await self.engine._record('play_later', kind, item_id)


class FakeLibrary:
    storefront = 'gb'

    def __init__(self, items):
        self.items = {(item.kind, item.id): item for item in items}
        self.playlists = [item for item in items if item.kind == 'playlist']

    def by_id(self, kind, item_id):
        return self.items.get((kind, item_id))

    def playlist_tree(self):
        return PlaylistTree([], self.playlists)


class FakeApp:
    def __init__(self):
        self.engine = FakeEngine()
        self.player = FakePlayer(self.engine)
        self.library = FakeLibrary([LIBRARY_ALBUM, CATALOG_ALBUM, PLAYLIST, READ_ONLY,
                                    FAVOURITES])
        self.demo = False
        self.tasks = []
        self.reported = []

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    def report(self, error):
        self.reported.append(error.code)


class FakeClipboard:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class FakeWindow:
    def __init__(self):
        self.actions = {}
        self.toasts = []
        self.played = []
        self.clipboard = FakeClipboard()

    def add_action(self, action):
        self.actions[action.get_name()] = action

    def toast(self, title):
        self.toasts.append(title)

    def play_request(self, play, start_with=None, shuffle=False):
        self.played.append((play, start_with))

    def get_clipboard(self):
        return self.clipboard


class ItemActionsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FakeApp()
        self.window = FakeWindow()
        self.actions = ItemActions(self.window, self.app)
        self.launched = []
        self.actions.launch = self.launched.append

    async def run_action(self, name, *target):
        """Activate win.<name> with target, as a menu item does, and wait for its task."""
        signature = '(sss)' if len(target) == 3 else '(ss)'
        self.window.actions[name].activate(GLib.Variant(signature, target))
        await asyncio.gather(*self.app.tasks)
        self.app.tasks.clear()

    async def test_every_action_is_added_with_its_target_type(self):
        self.assertEqual(sorted(self.window.actions), sorted([
            'item-play', 'item-play-next', 'item-play-later', 'item-love', 'item-unlove',
            'item-add-to-library', 'item-add-to-playlist', 'item-open-in-browser',
            'item-copy-link']))
        self.assertEqual(self.window.actions['item-love'].get_parameter_type().dup_string(),
                         '(ss)')
        self.assertEqual(
            self.window.actions['item-add-to-playlist'].get_parameter_type().dup_string(),
            '(sss)')

    async def test_love_a_track_by_its_catalog_id(self):
        self.actions.menu_for(LIBRARY_TRACK)  # the menu names the track; its action finds it
        await self.run_action('item-love', 'song', 'i.song1')
        self.assertIn(('love', 'song', '1000000001'), self.app.engine.calls)
        self.assertEqual(self.window.toasts, ['Added to Favourite Songs'])
        self.assertEqual(self.app.player.ensured, 1)
        await self.run_action('item-unlove', 'song', 'i.song1')
        self.assertIn(('unlove', 'song', '1000000001'), self.app.engine.calls)
        self.assertEqual(self.window.toasts[-1], 'Removed from Favourite Songs')

    async def test_unlove_an_album_from_the_library(self):
        await self.run_action('item-unlove', 'album', 'l.alb1')
        self.assertEqual(self.app.engine.calls, [('unlove', 'album', 'l.alb1')])
        self.assertEqual(self.window.toasts, ['Removed from Favourites'])

    async def test_add_to_library(self):
        await self.run_action('item-add-to-library', 'album', '1000000002')
        self.assertEqual(self.app.engine.calls, [('add_to_library', 'album', '1000000002')])
        self.assertEqual(self.window.toasts, ['Added “Harbour Suite” to your library'])
        # A library item is there already: nothing is sent.
        await self.run_action('item-add-to-library', 'album', 'l.alb1')
        self.assertEqual(len(self.app.engine.calls), 1)
        self.assertEqual(self.window.toasts[-1], 'This is in your library already')

    async def test_add_to_playlist(self):
        self.actions.menu_for(LIBRARY_TRACK)
        await self.run_action('item-add-to-playlist', 'p.pl1', 'song', 'i.song1')
        self.assertEqual(self.app.engine.calls[-1], ('add_to_playlist', 'p.pl1', 'i.song1'))
        self.assertEqual(self.window.toasts, ['Added “Harbour Lights” to “Road Trip”'])

    async def test_the_submenu_lists_the_playlists_that_take_songs(self):
        menu = self.actions.menu_for(LIBRARY_TRACK)
        targets = [target for action, target in actions_of(menu)
                   if action == 'win.item-add-to-playlist']
        self.assertEqual(targets, [('p.pl1', 'song', 'i.song1')])  # not read-only, favourites

    async def test_drop_on_a_playlist(self):
        ref = TrackRef(song_id='1000000009', title='Shoreline')
        self.assertTrue(self.actions.drop(PLAYLIST, ref))
        await asyncio.gather(*self.app.tasks)
        self.assertEqual(self.app.engine.calls, [('add_to_playlist', 'p.pl1', '1000000009')])
        self.assertEqual(self.window.toasts, ['Added “Shoreline” to “Road Trip”'])
        for playlist in (READ_ONLY, FAVOURITES, FOLDER, LIBRARY_ALBUM, None):
            self.assertFalse(self.actions.drop(playlist, ref))
        self.assertFalse(self.actions.drop(PLAYLIST, TrackRef()))
        self.assertFalse(self.actions.drop(PLAYLIST, 'not a ref'))
        self.assertEqual(len(self.app.engine.calls), 1)

    async def test_a_failure_is_reported_not_confirmed(self):
        self.app.engine.fail = EngineError('api', 'HTTP 403 Forbidden')
        await self.run_action('item-add-to-playlist', 'p.pl1', 'song', 'i.song1')
        self.assertEqual(self.app.reported, ['api'])
        self.assertEqual(self.window.toasts, [])

    async def test_demo_sends_nothing(self):
        self.app.demo = True
        await self.run_action('item-love', 'album', 'l.alb1')
        self.assertEqual(self.app.engine.calls, [])
        self.assertEqual(self.window.toasts, ['Not available with the demo library'])

    async def test_play_next_and_later(self):
        self.actions.menu_for(LIBRARY_TRACK)
        await self.run_action('item-play-next', 'song', 'i.song1')
        await self.run_action('item-play-later', 'album', 'l.alb1')
        queued = [call for call in self.app.engine.calls if call[0] != 'rating']  # the menu's
        self.assertEqual(queued, [('play_next', 'song', '1000000001'),
                                  ('play_later', 'album', 'l.alb1')])
        self.assertEqual(self.window.toasts, ['“Harbour Lights” will play next',
                                              '“Tidewater” will play later'])

    async def test_play(self):
        self.actions.menu_for(LIBRARY_TRACK)
        await self.run_action('item-play', 'song', 'i.song1')
        await self.run_action('item-play', 'album', 'l.alb1')
        await self.run_action('item-play', 'station', 'ra.unknown')
        self.assertEqual(self.window.played, [
            ({'kind': 'album', 'id': 'l.alb1'}, 2), ({'kind': 'album', 'id': 'l.alb1'}, None),
            ({'kind': 'station', 'id': 'ra.unknown'}, None)])

    async def test_links(self):
        await self.run_action('item-copy-link', 'playlist', 'p.pl1')
        self.assertEqual(self.window.clipboard.value,
                         'https://music.apple.com/library/playlist/p.pl1')
        self.assertEqual(self.window.toasts, ['Link copied'])
        # A library album: its catalog page when the engine knows it, else its library page.
        self.app.engine.catalog_urls['l.alb1'] = 'https://music.apple.com/gb/album/x/1000000010'
        await self.run_action('item-open-in-browser', 'album', 'l.alb1')
        self.app.engine.state = 'down'
        await self.run_action('item-open-in-browser', 'album', 'l.alb1')
        self.assertEqual(self.launched, ['https://music.apple.com/gb/album/x/1000000010',
                                         'https://music.apple.com/library/albums/l.alb1'])
        self.actions.menu_for(UPLOAD_TRACK)
        await self.run_action('item-copy-link', 'song', 'i.song2')
        self.assertEqual(self.window.toasts[-1], 'This has no link to copy')

    async def test_the_menu_asks_whether_it_is_loved(self):
        self.app.engine.ratings[('song', '1000000001')] = 1
        menu = self.actions.menu_for(LIBRARY_TRACK)
        self.assertIn('win.item-love', names(menu))
        await asyncio.gather(*self.app.tasks)
        self.assertIn('win.item-unlove', names(menu))  # swapped once the engine answered
        self.assertNotIn('win.item-love', names(menu))
        # The next menu knows at once.
        self.app.engine.state = 'down'
        self.assertIn('win.item-unlove', names(self.actions.menu_for(LIBRARY_TRACK)))


if __name__ == '__main__':
    unittest.main()
