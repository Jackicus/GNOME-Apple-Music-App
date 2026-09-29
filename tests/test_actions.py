# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The item actions (applemusic.actions): the menus built per kind, and the win.* actions run
against a stand-in window, app and engine, so add to library, add to playlist, love and the
drop onto a sidebar playlist are checked without Apple. Gio and GObject only, no display;
the actions' coroutines run under asyncio."""

import asyncio
import unittest

from gi.repository import Gio, GLib, GObject

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic.actions import (ItemActions, build_menu, fill_sidebar_menu, is_apple_music_url,
                                is_shareable, library_target, mnemonic_escaped, playlist_song,
                                playlist_track, queue_target, rating_target, share_url,
                                web_url)
from applemusic.backend.errors import EngineError
from applemusic.library import Item, PlaylistTree, Track, TrackRef

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
CATALOG_VIDEO = Item({'id': '1000000012', 'kind': 'video', 'title': 'Pilot Light (Video)',
                      'play': {'kind': 'musicVideo', 'id': '1000000012'}})
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
                       'type': 'library-songs', 'index': 2},
                      play={'kind': 'album', 'id': 'l.alb1'})
CATALOG_TRACK = Track({'id': '1000000009', 'catalogId': '1000000009', 'title': 'Shoreline',
                       'index': 0}, play={'kind': 'album', 'id': '1000000002'})
UPLOAD_TRACK = Track({'id': 'i.song2', 'title': 'Demo Take', 'index': 0},
                     play={'kind': 'playlist', 'id': 'p.pl1'})
VIDEO_TRACK = Track({'id': 'i.vid1', 'catalogId': '1000000011', 'title': 'Harbour Lights (Live)',
                     'type': 'library-music-videos', 'index': 3},
                    play={'kind': 'playlist', 'id': 'p.pl1'})

# What the engine's item() answers for a playlist: the Item's shape with its groups.
PLAYLIST_ANSWER = {'id': 'p.pl1', 'kind': 'playlist', 'title': 'Road Trip', 'trackCount': 1,
                   'play': {'kind': 'playlist', 'id': 'p.pl1'},
                   'groups': [{'title': '', 'entries': [
                       {'id': 'i.song1', 'title': 'Harbour Lights', 'type': 'library-songs'}]}]}


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


def attributes_of(menu, name):
    """{action: attribute value} for the items of a menu (sections and submenus flattened)
    that carry the attribute `name`."""
    found = {}
    for position in range(menu.get_n_items()):
        action = menu.get_item_attribute_value(position, Gio.MENU_ATTRIBUTE_ACTION)
        value = menu.get_item_attribute_value(position, name)
        if action is not None and value is not None:
            found[action.get_string()] = value.unpack()
        for link in (Gio.MENU_LINK_SECTION, Gio.MENU_LINK_SUBMENU):
            linked = menu.get_item_link(position, link)
            if linked is not None:
                found.update(attributes_of(linked, name))
    return found


def labels_of(menu):
    """The labels of a menu's items, sections and submenus flattened, in order."""
    found = []
    for position in range(menu.get_n_items()):
        label = menu.get_item_attribute_value(position, Gio.MENU_ATTRIBUTE_LABEL)
        if label is not None:
            found.append(label.unpack())
        for link in (Gio.MENU_LINK_SECTION, Gio.MENU_LINK_SUBMENU):
            linked = menu.get_item_link(position, link)
            if linked is not None:
                found.extend(labels_of(linked))
    return found


class MenuTest(unittest.TestCase):
    def test_library_album(self):
        self.assertEqual(names(build_menu(LIBRARY_ALBUM)), [
            'win.item-play', 'win.item-play-next', 'win.item-play-later', 'win.item-love',
            'win.item-unlove', 'win.item-open-in-browser', 'win.item-copy-link'])
        self.assertTrue(all(target == ('album', 'l.alb1')
                            for _action, target in actions_of(build_menu(LIBRARY_ALBUM))))

    def test_catalog_album_can_be_added(self):
        self.assertIn('win.item-add-to-library', names(build_menu(CATALOG_ALBUM)))

    def test_both_favourite_items_hide_with_their_action(self):
        menu = build_menu(LIBRARY_ALBUM)
        self.assertEqual(attributes_of(menu, 'hidden-when'),
                         {'win.item-love': 'action-disabled',
                          'win.item-unlove': 'action-disabled'})

    def test_labels_have_distinct_mnemonics(self):
        labels = labels_of(build_menu(LIBRARY_TRACK, playlists=[('p.pl1', 'Road Trip')]))
        letters = [label[label.index('_') + 1].lower() for label in labels if '_' in label
                   and label != 'Road Trip']
        self.assertEqual(len(letters), len(set(letters)), labels)
        self.assertIn('_Play', labels)
        self.assertIn('Add to Pla_ylist', labels)

    def test_track_with_playlists(self):
        menu = build_menu(LIBRARY_TRACK, playlists=[('p.pl1', 'Road Trip'), ('p.pl3', ''),
                                                    ('p.pl4', 'work_focus')])
        self.assertEqual(actions_of(menu), [
            ('win.item-play', ('song', 'i.song1')),
            ('win.item-play-next', ('song', 'i.song1')),
            ('win.item-play-later', ('song', 'i.song1')),
            ('win.item-love', ('song', 'i.song1')),
            ('win.item-unlove', ('song', 'i.song1')),
            ('win.item-add-to-playlist', ('p.pl1', 'song', 'i.song1')),
            ('win.item-add-to-playlist', ('p.pl3', 'song', 'i.song1')),
            ('win.item-add-to-playlist', ('p.pl4', 'song', 'i.song1')),
            ('win.item-open-in-browser', ('song', 'i.song1')),
            ('win.item-copy-link', ('song', 'i.song1'))])
        # A playlist's name is shown as it is: its underscores are not mnemonics.
        self.assertIn('work__focus', labels_of(menu))
        self.assertEqual(mnemonic_escaped('a_b__c'), 'a__b____c')
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
            'win.item-play', 'win.item-love', 'win.item-unlove', 'win.item-open-in-browser',
            'win.item-copy-link'])
        self.assertEqual(names(build_menu(FAVOURITES)), [
            'win.item-play', 'win.item-play-next', 'win.item-play-later',
            'win.item-open-in-browser'])  # its library route is the owner's: no Copy Link
        self.assertEqual(names(build_menu(MADE_UP_ALBUM)), ['win.item-play'])
        self.assertNotIn('win.item-open-in-browser', names(build_menu(UPLOAD_TRACK)))

    def test_copy_link_only_for_a_shareable_page(self):
        # A library playlist's page opens for its owner only: Open in Browser, no Copy Link.
        self.assertIn('win.item-open-in-browser', names(build_menu(PLAYLIST)))
        self.assertNotIn('win.item-copy-link', names(build_menu(PLAYLIST)))
        # A catalog album has both; a library album too (its catalog page, from the engine).
        self.assertIn('win.item-copy-link', names(build_menu(CATALOG_ALBUM)))
        self.assertIn('win.item-copy-link', names(build_menu(LIBRARY_ALBUM)))

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

    def test_a_music_video_is_a_video(self):
        # A track that is a music video (Track.kind) is rated and added as one.
        self.assertEqual(rating_target(VIDEO_TRACK), ('video', '1000000011'))
        self.assertEqual(playlist_track(VIDEO_TRACK), ('video', 'i.vid1'))
        self.assertEqual(playlist_track(LIBRARY_TRACK), ('song', 'i.song1'))
        self.assertEqual(playlist_track(CATALOG_VIDEO), ('video', '1000000012'))
        self.assertEqual(rating_target(CATALOG_VIDEO), ('video', '1000000012'))
        self.assertEqual(web_url(VIDEO_TRACK, 'gb'),
                         'https://music.apple.com/gb/music-video/1000000011')

    def test_web_urls(self):
        self.assertEqual(web_url(CATALOG_ALBUM), CATALOG_ALBUM.url)
        self.assertEqual(web_url(PLAYLIST), 'https://music.apple.com/library/playlist/p.pl1')
        self.assertEqual(web_url(LIBRARY_ALBUM), 'https://music.apple.com/library/albums/l.alb1')
        self.assertEqual(web_url(LIBRARY_TRACK, 'gb'), 'https://music.apple.com/gb/song/1000000001')
        self.assertEqual(web_url(CATALOG_ARTIST, 'gb'),
                         'https://music.apple.com/gb/artist/1000000005')
        for obj in (UPLOAD_TRACK, LIBRARY_ARTIST, MADE_UP_ALBUM, FOLDER, None):
            self.assertIsNone(web_url(obj))

    def test_a_foreign_url_never_comes_back(self):
        for url in ('file:///etc/hostname', 'http://evil.example/', 'https://evil.example/x',
                    'javascript:alert(1)', 'https://music.apple.com.evil.example/'):
            with self.subTest(url=url):
                self.assertFalse(is_apple_music_url(url))
                item = Item({'id': '1000000002', 'kind': 'album', 'title': 'X', 'url': url})
                self.assertEqual(web_url(item, 'gb'),
                                 'https://music.apple.com/gb/album/1000000002')
        self.assertTrue(is_apple_music_url(CATALOG_ALBUM.url))
        self.assertTrue(is_apple_music_url('https://geo.music.apple.com/us/album/x/1'))

    def test_shareable_addresses(self):
        self.assertEqual(share_url(CATALOG_ALBUM), CATALOG_ALBUM.url)
        self.assertEqual(share_url(LIBRARY_TRACK, 'gb'),
                         'https://music.apple.com/gb/song/1000000001')
        self.assertIsNone(share_url(PLAYLIST))  # the owner's route
        self.assertIsNone(share_url(LIBRARY_ALBUM))  # its catalog page needs the engine
        self.assertFalse(is_shareable(None))
        self.assertFalse(is_shareable('https://music.apple.com/library/albums/l.alb1'))
        self.assertTrue(is_shareable('https://music.apple.com/gb/album/x/1000000010'))

    def test_track_ref(self):
        ref = TrackRef.for_object(LIBRARY_TRACK)
        self.assertEqual((ref.song_id, ref.kind, ref.title), ('i.song1', 'song', 'Harbour Lights'))
        self.assertEqual(TrackRef.for_object(CATALOG_SONG).song_id, '1000000007')
        self.assertEqual(TrackRef.for_object(VIDEO_TRACK).kind, 'video')
        self.assertEqual(TrackRef.for_object(CATALOG_VIDEO).kind, 'video')
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
        self.items = {}  # (kind, id) -> the Item dict item() answers

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

    async def add_to_playlist(self, playlist_id, song_id, kind='song'):
        await self._record('add_to_playlist', playlist_id, song_id, kind)

    async def catalog_url(self, kind, item_id):
        await self._record('catalog_url', kind, item_id)
        return self.catalog_urls.get(item_id)

    async def item(self, kind, item_id):
        await self._record('item', kind, item_id)
        answer = self.items.get((kind, item_id))
        if answer is None:
            raise EngineError('api', f'item not found: {kind} {item_id}')
        return answer


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

    def favourite_songs(self):
        return next((item for item in self.playlists if item.favourites), None)


class FakeApp:
    def account_key(self, name):
        return name  # the release build's keys

    def __init__(self):
        self.engine = FakeEngine()
        self.player = FakePlayer(self.engine)
        # The playlists are this test's own: an add merges the engine's answer into them.
        self.playlist = Item(dict(PLAYLIST.raw))
        self.favourites = Item(dict(FAVOURITES.raw))
        self.library = FakeLibrary([LIBRARY_ALBUM, CATALOG_ALBUM, self.playlist, READ_ONLY,
                                    self.favourites])
        self.demo = False
        self.tasks = []
        self.reported = []
        self.toasts = []

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    def report(self, error):
        self.reported.append(error.code)

    def toast(self, title):
        self.toasts.append(title)

    def refuse_in_demo(self):
        if self.demo:
            self.toast('Not available with the demo library')
        return self.demo


class FakeClipboard:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class FakeWindow:
    def __init__(self):
        self.actions = {}
        self.played = []
        self.clipboard = FakeClipboard()

    def add_action(self, action):
        self.actions[action.get_name()] = action

    def play_request(self, play, start_with=None, shuffle=None):
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

    def loved_shown(self):
        """(Favourite enabled, Remove from Favourites enabled): what a menu shows."""
        return (self.window.actions['item-love'].get_enabled(),
                self.window.actions['item-unlove'].get_enabled())

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
        self.assertEqual(self.app.toasts, ['Added to Favourite Songs'])
        self.assertEqual(self.app.player.ensured, 1)
        self.assertEqual(self.loved_shown(), (False, True))  # the engine said so
        await self.run_action('item-unlove', 'song', 'i.song1')
        self.assertIn(('unlove', 'song', '1000000001'), self.app.engine.calls)
        self.assertEqual(self.app.toasts[-1], 'Removed from Favourite Songs')
        self.assertEqual(self.loved_shown(), (True, False))

    async def test_loving_a_song_refreshes_favourite_songs(self):
        answer = dict(PLAYLIST_ANSWER, id='p.fav', title='Favourite Songs',
                      attributes={'isFavourites': True})
        self.app.engine.items[('playlist', 'p.fav')] = answer
        changed = []
        self.app.favourites.connect('groups-changed', lambda *_: changed.append(True))
        self.actions.menu_for(LIBRARY_TRACK)
        await self.run_action('item-love', 'song', 'i.song1')
        self.assertIn(('item', 'playlist', 'p.fav'), self.app.engine.calls)
        self.assertEqual(changed, [True])
        self.assertEqual([track.id for group in self.app.favourites.groups
                          for track in group.entries], ['i.song1'])
        # An album loved touches no playlist.
        await self.run_action('item-love', 'album', 'l.alb1')
        self.assertEqual(self.app.engine.calls.count(('item', 'playlist', 'p.fav')), 1)

    async def test_unlove_an_album_from_the_library(self):
        await self.run_action('item-unlove', 'album', 'l.alb1')
        self.assertEqual(self.app.engine.calls, [('unlove', 'album', 'l.alb1')])
        self.assertEqual(self.app.toasts, ['Removed from Favourites'])

    async def test_add_to_library(self):
        await self.run_action('item-add-to-library', 'album', '1000000002')
        self.assertEqual(self.app.engine.calls, [('add_to_library', 'album', '1000000002')])
        self.assertEqual(self.app.toasts, ['Added “Harbour Suite” to your library'])
        # A library item is there already: nothing is sent.
        await self.run_action('item-add-to-library', 'album', 'l.alb1')
        self.assertEqual(len(self.app.engine.calls), 1)
        self.assertEqual(self.app.toasts[-1], 'This is in your library already')

    async def test_add_to_playlist_and_refresh_it(self):
        self.app.engine.items[('playlist', 'p.pl1')] = PLAYLIST_ANSWER
        changed = []
        self.app.playlist.connect('groups-changed', lambda *_: changed.append(True))
        self.actions.menu_for(LIBRARY_TRACK)
        await self.run_action('item-add-to-playlist', 'p.pl1', 'song', 'i.song1')
        self.assertIn(('add_to_playlist', 'p.pl1', 'i.song1', 'song'), self.app.engine.calls)
        self.assertEqual(self.app.toasts, ['Added “Harbour Lights” to “Road Trip”'])
        # The playlist is fetched again and the library's Item follows: its page re-shows.
        self.assertEqual(self.app.engine.calls[-1], ('item', 'playlist', 'p.pl1'))
        self.assertEqual(changed, [True])
        self.assertEqual(self.app.playlist.raw.get('trackCount'), 1)

    async def test_a_refresh_that_fails_is_only_logged(self):
        self.actions.menu_for(LIBRARY_TRACK)
        await self.run_action('item-add-to-playlist', 'p.pl1', 'song', 'i.song1')
        self.assertEqual(self.app.engine.calls[-1], ('item', 'playlist', 'p.pl1'))  # no answer
        self.assertEqual(self.app.toasts, ['Added “Harbour Lights” to “Road Trip”'])
        self.assertEqual(self.app.reported, [])

    async def test_a_music_video_is_added_as_one(self):
        self.actions.menu_for(VIDEO_TRACK)
        await self.run_action('item-add-to-playlist', 'p.pl1', 'song', 'i.vid1')
        self.assertIn(('add_to_playlist', 'p.pl1', 'i.vid1', 'video'), self.app.engine.calls)
        await self.run_action('item-love', 'song', 'i.vid1')
        self.assertIn(('love', 'video', '1000000011'), self.app.engine.calls)

    async def test_the_submenu_lists_the_playlists_that_take_songs(self):
        menu = self.actions.menu_for(LIBRARY_TRACK)
        targets = [target for action, target in actions_of(menu)
                   if action == 'win.item-add-to-playlist']
        self.assertEqual(targets, [('p.pl1', 'song', 'i.song1')])  # not read-only, favourites

    async def test_drop_on_a_playlist(self):
        ref = TrackRef(song_id='1000000009', title='Shoreline')
        self.assertTrue(self.actions.drop(self.app.playlist, ref))
        await asyncio.gather(*self.app.tasks)
        self.assertEqual(self.app.engine.calls[0],
                         ('add_to_playlist', 'p.pl1', '1000000009', 'song'))
        self.assertEqual(self.app.toasts, ['Added “Shoreline” to “Road Trip”'])
        video = TrackRef(song_id='i.vid1', kind='video', title='Live')
        self.assertTrue(self.actions.drop(self.app.playlist, video))
        await asyncio.gather(*self.app.tasks)
        self.assertIn(('add_to_playlist', 'p.pl1', 'i.vid1', 'video'), self.app.engine.calls)
        for playlist in (READ_ONLY, self.app.favourites, FOLDER, LIBRARY_ALBUM, None):
            self.assertFalse(self.actions.drop(playlist, ref))
        self.assertFalse(self.actions.drop(self.app.playlist, TrackRef()))
        self.assertFalse(self.actions.drop(self.app.playlist, 'not a ref'))
        self.assertEqual(len([call for call in self.app.engine.calls
                              if call[0] == 'add_to_playlist']), 2)

    async def test_a_failure_is_reported_not_confirmed(self):
        self.app.engine.fail = EngineError('api', 'HTTP 403 Forbidden')
        await self.run_action('item-add-to-playlist', 'p.pl1', 'song', 'i.song1')
        self.assertEqual(self.app.reported, ['api'])
        self.assertEqual(self.app.toasts, [])

    async def test_signed_out_is_reported_so_the_sign_in_opens(self):
        self.app.engine.fail = EngineError('not-signed-in', 'sign in first')
        await self.run_action('item-love', 'album', 'l.alb1')
        self.assertEqual(self.app.reported, ['not-signed-in'])
        self.assertEqual(self.app.toasts, [])

    async def test_demo_sends_nothing(self):
        self.app.demo = True
        await self.run_action('item-love', 'album', 'l.alb1')
        self.assertEqual(self.app.engine.calls, [])
        self.assertEqual(self.app.toasts, ['Not available with the demo library'])

    async def test_play_next_and_later(self):
        self.actions.menu_for(LIBRARY_TRACK)
        await self.run_action('item-play-next', 'song', 'i.song1')
        await self.run_action('item-play-later', 'album', 'l.alb1')
        queued = [call for call in self.app.engine.calls if call[0] != 'rating']  # the menu's
        self.assertEqual(queued, [('play_next', 'song', '1000000001'),
                                  ('play_later', 'album', 'l.alb1')])
        self.assertEqual(self.app.toasts, ['“Harbour Lights” will play next',
                                           '“Tidewater” will play later'])
        self.assertEqual(self.app.player.ensured, 0)  # the Player's commands do that

    async def test_play(self):
        self.actions.menu_for(LIBRARY_TRACK)
        await self.run_action('item-play', 'song', 'i.song1')
        await self.run_action('item-play', 'album', 'l.alb1')
        await self.run_action('item-play', 'station', 'ra.unknown')
        self.assertEqual(self.window.played, [
            ({'kind': 'album', 'id': 'l.alb1'}, 2), ({'kind': 'album', 'id': 'l.alb1'}, None),
            ({'kind': 'station', 'id': 'ra.unknown'}, None)])

    async def test_links(self):
        # A library playlist's page opens, but is nobody else's to copy.
        await self.run_action('item-open-in-browser', 'playlist', 'p.pl1')
        self.assertEqual(self.launched, ['https://music.apple.com/library/playlist/p.pl1'])
        await self.run_action('item-copy-link', 'playlist', 'p.pl1')
        self.assertIsNone(self.window.clipboard.value)
        self.assertEqual(self.app.toasts, ['This has no link to copy'])
        # A library album: its catalog page when the engine knows it, else its library page
        # (opened, not copied).
        self.app.engine.catalog_urls['l.alb1'] = 'https://music.apple.com/gb/album/x/1000000010'
        await self.run_action('item-copy-link', 'album', 'l.alb1')
        self.assertEqual(self.window.clipboard.value,
                         'https://music.apple.com/gb/album/x/1000000010')
        self.assertEqual(self.app.toasts[-1], 'Link copied')
        await self.run_action('item-open-in-browser', 'album', 'l.alb1')
        self.app.engine.state = 'down'
        await self.run_action('item-open-in-browser', 'album', 'l.alb1')
        self.assertEqual(self.launched[1:], ['https://music.apple.com/gb/album/x/1000000010',
                                             'https://music.apple.com/library/albums/l.alb1'])
        self.actions.menu_for(UPLOAD_TRACK)
        await self.run_action('item-copy-link', 'song', 'i.song2')
        self.assertEqual(self.app.toasts[-1], 'This has no link to copy')

    async def test_a_foreign_catalog_answer_is_not_opened(self):
        self.app.engine.catalog_urls['l.alb1'] = 'http://evil.example/album'
        await self.run_action('item-open-in-browser', 'album', 'l.alb1')
        self.assertEqual(self.launched, ['https://music.apple.com/library/albums/l.alb1'])

    async def test_the_menu_asks_whether_it_is_loved(self):
        self.app.engine.ratings[('song', '1000000001')] = 1
        self.actions.menu_for(LIBRARY_TRACK)
        self.assertEqual(self.loved_shown(), (True, False))  # not known loved yet
        await asyncio.gather(*self.app.tasks)
        self.assertEqual(self.loved_shown(), (False, True))  # once the engine answered
        # The next menu knows at once, and another item's shows its own state.
        self.app.engine.state = 'down'
        self.actions.menu_for(LIBRARY_TRACK)
        self.assertEqual(self.loved_shown(), (False, True))
        self.actions.menu_for(LIBRARY_ALBUM)
        self.assertEqual(self.loved_shown(), (True, False))


if __name__ == '__main__':
    unittest.main()
