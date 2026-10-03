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
                                is_shareable, library_target, mnemonic_escaped,
                                now_playing_track, playlist_song, playlist_track, queue_target,
                                rating_target, share_url, web_url)
from applemusic import actions as actions_module
from applemusic.backend.errors import EngineError
from applemusic.library import Item, PlaylistTree, Track, TrackRef
from applemusic.player import NowPlaying

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
TOP_LEVEL = Item({'id': 'root', 'kind': 'folder', 'title': ''})  # the top level: All Playlists
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

# The library's artist of LIBRARY_ALBUM, as the library makes artists up from albums.
BAND = Item({'id': 'l.art_00000000band', 'kind': 'artist', 'title': 'The Invented Band',
             'play': {}})

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
            'win.item-play', 'win.item-play-next', 'win.item-play-later',
            'win.item-go-to-artist', 'win.item-love', 'win.item-unlove',
            'win.item-open-in-browser', 'win.item-copy-link'])
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
            ('win.item-go-to-album', ('song', 'i.song1')),
            ('win.item-go-to-artist', ('song', 'i.song1')),
            ('win.item-love', ('song', 'i.song1')),
            ('win.item-unlove', ('song', 'i.song1')),
            ('win.item-add-to-playlist', ('p.pl1', 'song', 'i.song1')),
            ('win.item-add-to-playlist', ('p.pl3', 'song', 'i.song1')),
            ('win.item-add-to-playlist', ('p.pl4', 'song', 'i.song1')),
            ('win.item-new-playlist', ('song', 'i.song1')),
            ('win.item-open-in-browser', ('song', 'i.song1')),
            ('win.item-copy-link', ('song', 'i.song1'))])
        # A playlist's name is shown as it is: its underscores are not mnemonics.
        self.assertIn('work__focus', labels_of(menu))
        self.assertEqual(mnemonic_escaped('a_b__c'), 'a__b____c')
        # No playlists: the submenu has New Playlist… alone. A catalog track can be added to
        # the library.
        self.assertNotIn('win.item-add-to-playlist', names(build_menu(LIBRARY_TRACK)))
        self.assertIn('win.item-new-playlist', names(build_menu(LIBRARY_TRACK)))
        self.assertIn('win.item-add-to-library', names(build_menu(CATALOG_TRACK)))

    def test_go_to_album_and_artist(self):
        song = Track({'id': 'i.song3', 'title': 'Undertow', 'artist': 'The Invented Band',
                      'album': 'Tidewater', 'type': 'library-songs'},
                     play={'kind': 'playlist', 'id': 'p.pl1'})
        self.assertEqual(names(build_menu(song))[3:5],
                         ['win.item-go-to-album', 'win.item-go-to-artist'])
        # A music video has an artist, but no album to go to.
        self.assertNotIn('win.item-go-to-album', names(build_menu(VIDEO_TRACK)))
        self.assertIn('win.item-go-to-artist', names(build_menu(VIDEO_TRACK)))
        # An album goes to its artist; a playlist, a station, an artist nowhere.
        self.assertNotIn('win.item-go-to-album', names(build_menu(CATALOG_ALBUM)))
        for item in (PLAYLIST, STATION, CATALOG_ARTIST):
            self.assertFalse({'win.item-go-to-album', 'win.item-go-to-artist'}
                             & set(names(build_menu(item))), item.kind)
        # A song with nothing to look up by (an upload without names) goes nowhere.
        self.assertFalse({'win.item-go-to-album', 'win.item-go-to-artist'}
                         & set(names(build_menu(UPLOAD_TRACK))))

    def test_go_to_is_left_out_where_it_is_already(self):
        # An album's own tracks on its page; an artist's songs on theirs.
        self.assertNotIn('win.item-go-to-album', names(build_menu(LIBRARY_TRACK,
                                                                  here=LIBRARY_ALBUM)))
        self.assertIn('win.item-go-to-album', names(build_menu(LIBRARY_TRACK,
                                                               here=CATALOG_ALBUM)))
        song = Track({'id': '1000000013', 'title': 'Undertow', 'artist': 'the band'})
        self.assertNotIn('win.item-go-to-artist', names(build_menu(song, here=LIBRARY_ARTIST)))
        self.assertIn('win.item-go-to-artist', names(build_menu(song, here=CATALOG_ARTIST)))

    def test_a_queued_item_has_no_play(self):
        # The item playing's menu, Up Next's: Play would replace the queue.
        menu = names(build_menu(LIBRARY_TRACK, queued=True))
        self.assertNotIn('win.item-play', menu)
        self.assertIn('win.item-play-next', menu)
        self.assertIn('win.item-go-to-album', menu)

    def test_the_item_playing_as_a_track(self):
        playing = NowPlaying({'id': 'i.song1', 'catalogId': '1000000001', 'type': 'song',
                              'title': 'Harbour Lights', 'artist': 'The Invented Band',
                              'album': 'Tidewater', 'index': 4})
        track = now_playing_track(playing)
        self.assertEqual((track.id, track.catalog_id, track.kind, track.artist, track.album),
                         ('i.song1', '1000000001', 'song', 'The Invented Band', 'Tidewater'))
        self.assertEqual(track.play, {})
        self.assertEqual(rating_target(track), ('song', '1000000001'))
        video = now_playing_track(NowPlaying({'id': '1000000012', 'type': 'musicVideo',
                                              'title': 'Pilot Light (Video)'}))
        self.assertEqual(video.kind, 'video')
        self.assertEqual(playlist_track(video), ('video', '1000000012'))
        # A station's segment, an ad, nothing: no menu.
        self.assertIsNone(now_playing_track(NowPlaying({'id': 'x', 'type': 'stations'})))
        self.assertIsNone(now_playing_track(None))

    def test_what_has_no_menu(self):
        self.assertIsNone(build_menu(TOP_LEVEL))  # All Playlists: not renamed, nor deleted
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

    def test_a_playlist_of_the_users_is_renamed_and_deleted(self):
        self.assertEqual(names(build_menu(PLAYLIST))[-2:], ['win.item-rename', 'win.item-delete'])
        self.assertIn('_Delete Playlist…', labels_of(build_menu(PLAYLIST)))
        # Apple's own (canEdit false) and Favourite Songs: never.
        for playlist in (READ_ONLY, FAVOURITES):
            self.assertFalse({'win.item-rename', 'win.item-delete'}
                             & set(names(build_menu(playlist))), playlist.title)
        # A folder of the user's: only those two, Delete Folder… by name.
        self.assertEqual(actions_of(build_menu(FOLDER)), [
            ('win.item-rename', ('folder', 'l.fd1')), ('win.item-delete', ('folder', 'l.fd1'))])
        self.assertIn('_Delete Folder…', labels_of(build_menu(FOLDER)))

    def test_remove_from_playlist_where_the_track_is_shown(self):
        track = Track({'id': 'i.song1', 'title': 'Harbour Lights', 'type': 'library-songs',
                       'index': 4}, play={'kind': 'playlist', 'id': 'p.pl1'})
        menu = build_menu(track, container=PLAYLIST)
        # Its own entry, by index: the same song twice is two entries.
        self.assertIn(('win.item-remove-from-playlist', ('p.pl1', 'i.song1', 4)),
                      actions_of(menu))
        # Not in a playlist that is not the user's, nor anywhere else.
        for container in (READ_ONLY, FAVOURITES, LIBRARY_ALBUM, None):
            self.assertNotIn('win.item-remove-from-playlist',
                             names(build_menu(track, container=container)))
        # Every label of the fullest menus has a mnemonic of its own.
        for labels in (labels_of(menu), labels_of(build_menu(PLAYLIST)),
                       labels_of(build_menu(FOLDER))):
            letters = [label[label.index('_') + 1].lower() for label in labels if '_' in label]
            self.assertEqual(len(letters), len(set(letters)), labels)

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
            ('win.item-open-in-browser', ('playlist', 'p.pl1')),
            ('win.item-rename', ('playlist', 'p.pl1')),
            ('win.item-delete', ('playlist', 'p.pl1'))])
        # Apple's own and Favourite Songs: played, never renamed or deleted.
        for playlist in (READ_ONLY, FAVOURITES):
            fill_sidebar_menu(menu, playlist)
            self.assertFalse({'win.item-rename', 'win.item-delete'} & set(names(menu)))
        fill_sidebar_menu(menu, FOLDER)
        self.assertEqual(actions_of(menu), [
            ('win.item-new-playlist', ('folder', 'l.fd1')),
            ('win.item-rename', ('folder', 'l.fd1')),
            ('win.item-delete', ('folder', 'l.fd1'))])
        self.assertIn('_Delete Folder…', labels_of(menu))
        fill_sidebar_menu(menu, TOP_LEVEL)  # All Playlists
        self.assertEqual(actions_of(menu), [('win.item-new-playlist', ('folder', 'root'))])
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
        self.related_answers = {}  # (kind, id) -> related()'s answer
        self.listings = []  # folder_children()'s answers, one per call; then none

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

    async def related(self, kind, item_id):
        await self._record('related', kind, item_id)
        return self.related_answers.get((kind, item_id), {'album': None, 'artists': []})

    async def create_playlist(self, name, description='', tracks=(), folder_id=None):
        await self._record('create_playlist', name, description, tuple(tracks), folder_id)
        return 'p.new'

    async def edit_playlist(self, playlist_id, name=None, description=None):
        await self._record('edit_playlist', playlist_id, name, description)

    async def delete_playlist(self, playlist_id):
        await self._record('delete_playlist', playlist_id)

    async def remove_from_playlist(self, playlist_id, track_id, index=None):
        await self._record('remove_from_playlist', playlist_id, track_id, index)
        return True

    async def rename_folder(self, folder_id, name):
        await self._record('rename_folder', folder_id, name)

    async def delete_folder(self, folder_id):
        await self._record('delete_folder', folder_id)

    async def folder_children(self, folder_id=None):
        self.calls.append(('folder_children', folder_id))
        if self.listings:
            listing = self.listings.pop(0)
            if isinstance(listing, Exception):
                raise listing
            return listing
        return []

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

    def __init__(self, items, folders=()):
        self.items = {(item.kind, item.id): item for item in items}
        self.playlists = [item for item in items if item.kind == 'playlist']
        self.albums = [item for item in items if item.kind == 'album']
        self.artists = [item for item in items if item.kind == 'artist']
        self.tree = PlaylistTree(folders, self.playlists)
        for node in self.tree.folders():
            self.items[('folder', node.id)] = node.item
        self.placed = 0

    def by_id(self, kind, item_id):
        return self.items.get((kind, item_id))

    def playlist_tree(self):
        return self.tree

    def place_playlists(self):
        self.placed += 1

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
                                    self.favourites, BAND])
        self.demo = False
        self.tasks = []
        self.reported = []
        self.toasts = []
        self.syncs = []  # (quick,) per start_sync

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    def report(self, error):
        self.reported.append(error.code)

    def toast(self, title):
        self.toasts.append(title)

    def start_sync(self, quick=False, playlists=False):
        self.syncs.append('playlists' if playlists else quick)
        return None

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
        self.opened = []
        self.shown = None  # the Item of the page shown
        self.clipboard = FakeClipboard()
        self.left = []  # leave()'s (gone, parent)
        self.selected = []  # select_page()'s keys

    def leave(self, gone, parent=None):
        self.left.append((gone, parent))

    def select_page(self, key):
        self.selected.append(key)

    def open_item(self, item):
        self.opened.append(item)

    def shown_item(self):
        return self.shown

    def add_action(self, action):
        self.actions[action.get_name()] = action

    def play_request(self, play, start_with=None, shuffle=None, start_id=None):
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
            'item-copy-link', 'item-go-to-album', 'item-go-to-artist', 'item-new-playlist',
            'item-rename', 'item-delete', 'item-remove-from-playlist']))
        self.assertEqual(
            self.window.actions['item-remove-from-playlist'].get_parameter_type().dup_string(),
            '(ssi)')
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

    async def test_add_to_library_shows_what_it_added(self):
        # Apple answers the write with no body, so the item is read back: a quick sync, as
        # add_to_playlist fetches the playlist it wrote to. Without it the song is in the
        # account and nowhere in the app until the next full sync.
        await self.run_action('item-add-to-library', 'album', '1000000002')
        self.assertEqual(self.app.syncs, [True])

    async def test_nothing_is_synced_when_the_add_fails(self):
        self.app.engine.fail = EngineError('api', 'HTTP 403 Forbidden')
        await self.run_action('item-add-to-library', 'album', '1000000002')
        self.assertEqual(self.app.reported, ['api'])
        self.assertEqual(self.app.syncs, [])

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

    async def test_the_submenu_lists_the_playlists_in_the_sidebars_order(self):
        # By title, whatever the library's order (library.PlaylistTree).
        titles = ['Zulu', '2. Two', 'alpha', '1. One']
        self.app.library = FakeLibrary(
            [Item({'id': f'p.o{n}', 'kind': 'playlist', 'title': title, 'groups': []})
             for n, title in enumerate(titles)] + [self.app.favourites])
        self.assertEqual([title for _id, title in self.actions.playlists()],
                         ['1. One', '2. Two', 'alpha', 'Zulu'])

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

    async def test_go_to_the_library_album_and_artist(self):
        # A track of the library's album: its group's album, then the album's artist by
        # name; neither needs the engine.
        song = Track({'id': 'i.song1', 'title': 'Harbour Lights', 'album': 'Tidewater',
                      'artist': 'The Invented Band feat. Someone', 'type': 'library-songs'},
                     play={'kind': 'playlist', 'id': 'p.pl1'})
        self.actions.menu_for(song)
        await self.run_action('item-go-to-album', 'song', 'i.song1')
        await self.run_action('item-go-to-artist', 'song', 'i.song1')
        self.assertEqual(self.window.opened, [LIBRARY_ALBUM, BAND])
        self.assertEqual([call for call in self.app.engine.calls if call[0] != 'rating'], [])
        # An album goes to its artist; the page shown is not opened again.
        await self.run_action('item-go-to-artist', 'album', 'l.alb1')
        self.window.shown = BAND
        await self.run_action('item-go-to-artist', 'album', 'l.alb1')
        self.assertEqual(self.window.opened, [LIBRARY_ALBUM, BAND, BAND])
        self.assertEqual(self.app.toasts, [])

    async def test_go_to_the_catalog_album_and_artist(self):
        # The library has neither: the engine looks the catalog song up, once per kind of
        # answer, and the page opens on the answer's Item.
        song = Track({'id': '1000000009', 'catalogId': '1000000009', 'title': 'Shoreline',
                      'album': 'Coastal', 'artist': 'Mara Lind & Others'})
        self.app.engine.related_answers[('song', '1000000009')] = {
            'album': {'id': '1000000020', 'kind': 'album', 'title': 'Coastal',
                      'subtitle': 'Mara Lind', 'play': {'kind': 'album', 'id': '1000000020'}},
            'artists': [{'id': '1000000021', 'kind': 'artist', 'title': 'Someone Else'},
                        {'id': '1000000022', 'kind': 'artist', 'title': 'Mara Lind & Others'}]}
        self.actions.menu_for(song)
        await self.run_action('item-go-to-album', 'song', '1000000009')
        await self.run_action('item-go-to-artist', 'song', '1000000009')
        self.assertEqual([(item.kind, item.id) for item in self.window.opened],
                         [('album', '1000000020'), ('artist', '1000000022')])
        self.assertEqual([call for call in self.app.engine.calls if call[0] == 'related'],
                         [('related', 'song', '1000000009')] * 2)
        self.assertEqual(self.app.player.ensured, 2)
        # The catalog's album the library has is the library's.
        self.app.engine.related_answers[('song', '1000000009')]['album']['id'] = '1000000002'
        await self.run_action('item-go-to-album', 'song', '1000000009')
        self.assertIs(self.window.opened[-1], CATALOG_ALBUM)

    async def test_go_to_artist_is_apple_musics_page(self):
        # The library has the artist, but with the engine up the catalog names them exactly:
        # Apple Music's page opens (it shows the library's albums of theirs too).
        song = Track({'id': 'i.song4', 'catalogId': '1000000030', 'title': 'Harbour Lights',
                      'artist': 'The Invented Band', 'type': 'library-songs'})
        self.app.engine.related_answers[('song', '1000000030')] = {
            'album': None,
            'artists': [{'id': '1000000031', 'kind': 'artist', 'title': 'The Invented Band'}]}
        self.actions.menu_for(song)
        await self.run_action('item-go-to-artist', 'song', 'i.song4')
        self.assertEqual([(item.kind, item.id) for item in self.window.opened],
                         [('artist', '1000000031')])
        # The engine down: the library's artist, at once, nothing asked.
        self.app.engine.state = 'down'
        calls = len(self.app.engine.calls)
        await self.run_action('item-go-to-artist', 'song', 'i.song4')
        self.assertIs(self.window.opened[-1], BAND)
        self.assertEqual(len(self.app.engine.calls), calls)
        # The catalog failing, or naming no artist: the library's.
        self.app.engine.state = 'up'
        self.app.engine.related_answers[('song', '1000000030')] = {'album': None, 'artists': []}
        await self.run_action('item-go-to-artist', 'song', 'i.song4')
        self.app.engine.fail = EngineError('api', 'HTTP 500', status=500)
        await self.run_action('item-go-to-artist', 'song', 'i.song4')
        self.assertEqual(self.window.opened[-2:], [BAND, BAND])
        self.assertEqual(self.app.reported, [])
        self.assertEqual(self.app.toasts, [])

    async def test_go_to_finds_nothing(self):
        song = Track({'id': '1000000009', 'catalogId': '1000000009', 'title': 'Shoreline',
                      'album': 'Coastal'})
        self.actions.menu_for(song)
        await self.run_action('item-go-to-album', 'song', '1000000009')
        self.assertEqual(self.app.toasts, ['Could not find the album'])
        self.assertEqual(self.window.opened, [])
        # Signed out: reported, so the sign-in opens.
        self.app.engine.fail = EngineError('not-signed-in', 'sign in first')
        await self.run_action('item-go-to-artist', 'song', '1000000009')
        self.assertEqual(self.app.reported, ['not-signed-in'])
        # Gone from the storefront's catalog (Apple's 404): not found, not an error.
        self.app.engine.fail = EngineError('api', 'HTTP 404', status=404)
        await self.run_action('item-go-to-album', 'song', '1000000009')
        self.assertEqual(self.app.reported, ['not-signed-in'])
        self.assertEqual(self.app.toasts[-1], 'Could not find the album')
        # The demo asks no engine.
        self.app.engine.fail = None
        self.app.demo = True
        calls = len(self.app.engine.calls)
        await self.run_action('item-go-to-album', 'song', '1000000009')
        self.assertEqual(len(self.app.engine.calls), calls)
        self.assertEqual(self.app.toasts[-1], 'Not available with the demo library')
        # A target the actions know nothing of.
        await self.run_action('item-go-to-album', 'song', 'i.forgotten')
        self.assertEqual(self.app.toasts[-1], 'Could not find the album')

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


class PlaylistActionsTest(unittest.IsolatedAsyncioTestCase):
    """New Playlist, Rename, Delete and Remove from Playlist: the dialogs stood in for (what
    they would answer is called directly), the engine's writes recorded."""

    async def asyncSetUp(self):
        self.app = FakeApp()
        # The playlist in a folder of the user's, which holds it alone.
        folders = [{'id': 'root', 'title': '', 'parent': None,
                    'children': [{'kind': 'folder', 'id': 'p.fd1'},
                                 {'kind': 'playlist', 'id': 'p.pl2'}]},
                   {'id': 'p.fd1', 'title': 'Evenings', 'parent': 'root',
                    'children': [{'kind': 'playlist', 'id': 'p.pl1'}]}]
        self.app.library = FakeLibrary([self.app.playlist, READ_ONLY, self.app.favourites],
                                       folders)
        self.folder = self.app.library.by_id('folder', 'p.fd1')
        self.window = FakeWindow()
        self.actions = ItemActions(self.window, self.app)
        self.asked = []  # (heading, confirm, name, description, done) per name dialog
        self.confirms = []  # (heading, body, confirm, done) per confirmation
        self.actions.ask_name = lambda *args: self.asked.append(args)
        self.actions.ask_confirm = lambda *args: self.confirms.append(args)
        delays = actions_module.SETTLE_DELAYS
        actions_module.SETTLE_DELAYS = (0, 0, 0)
        self.addCleanup(setattr, actions_module, 'SETTLE_DELAYS', delays)

    async def run_action(self, name, *target):
        signature = {3: '(sss)'}.get(len(target), '(ss)')
        if name == 'item-remove-from-playlist':
            signature = '(ssi)'
        self.window.actions[name].activate(GLib.Variant(signature, target))
        await self.settle()

    async def settle(self):
        while self.app.tasks:
            tasks = list(self.app.tasks)
            self.app.tasks.clear()
            await asyncio.gather(*tasks)

    def writes(self):
        return [call for call in self.app.engine.calls
                if call[0] not in ('folder_children', 'rating')]  # reads

    async def test_remove_a_track_from_the_playlist_it_is_shown_in(self):
        self.app.engine.items[('playlist', 'p.pl1')] = PLAYLIST_ANSWER
        track = Track({'id': 'i.song1', 'title': 'Harbour Lights', 'type': 'library-songs',
                       'index': 2}, play={'kind': 'playlist', 'id': 'p.pl1'})
        menu = self.actions.menu_for(track)
        self.assertIn(('win.item-remove-from-playlist', ('p.pl1', 'i.song1', 2)),
                      actions_of(menu))
        await self.run_action('item-remove-from-playlist', 'p.pl1', 'i.song1', 2)
        self.assertEqual(self.writes()[0], ('remove_from_playlist', 'p.pl1', 'i.song1', 2))
        self.assertEqual(self.app.toasts, ['Removed “Harbour Lights” from “Road Trip”'])
        self.assertEqual(self.writes()[-1], ('item', 'playlist', 'p.pl1'))  # its page follows
        # Shown in Apple's own playlist, or in Favourite Songs: no such item.
        for playlist_id in ('p.pl2', 'p.fav'):
            elsewhere = Track({'id': 'i.song1', 'title': 'Harbour Lights'},
                              play={'kind': 'playlist', 'id': playlist_id})
            self.assertNotIn('win.item-remove-from-playlist',
                             names(self.actions.menu_for(elsewhere)))

    async def test_rename_a_playlist_and_its_description(self):
        self.app.engine.listings = [[], [{'kind': 'playlist', 'id': 'p.pl1',
                                          'name': 'Night Drive'}]]
        await self.run_action('item-rename', 'playlist', 'p.pl1')
        heading, confirm, name, description, done = self.asked[0]
        self.assertEqual((heading, confirm, name, description),
                         ('Rename Playlist', '_Rename', 'Road Trip', ''))
        done('Night Drive', 'Late, with the windows down')
        await self.settle()
        self.assertEqual(self.writes(), [
            ('edit_playlist', 'p.pl1', 'Night Drive', 'Late, with the windows down')])
        # The Item and the sidebar follow at once, the library once Apple lists the name:
        # in the folder that holds it, asked until it is listed.
        self.assertEqual(self.app.playlist.title, 'Night Drive')
        self.assertEqual(self.app.playlist.summary, 'Late, with the windows down')
        self.assertEqual(self.app.library.placed, 1)
        self.assertEqual([call for call in self.app.engine.calls
                          if call[0] == 'folder_children'],
                         [('folder_children', 'p.fd1')] * 2)
        self.assertEqual(self.app.syncs, ['playlists'])

    async def test_rename_a_folder(self):
        await self.run_action('item-rename', 'folder', 'p.fd1')
        heading, _confirm, name, description, done = self.asked[0]
        self.assertEqual((heading, name, description), ('Rename Folder', 'Evenings', None))
        done('Late Evenings', None)
        await self.settle()
        self.assertEqual(self.writes(), [('rename_folder', 'p.fd1', 'Late Evenings')])
        self.assertEqual(self.folder.title, 'Late Evenings')
        self.assertIn(('folder_children', 'root'), self.app.engine.calls)

    async def test_what_is_not_the_users_is_not_renamed_or_deleted(self):
        for action in ('item-rename', 'item-delete'):
            for target in (('playlist', 'p.pl2'), ('playlist', 'p.fav'), ('folder', 'root')):
                await self.run_action(action, *target)
        self.assertEqual((self.asked, self.confirms, self.writes()), ([], [], []))
        self.assertEqual(self.app.toasts, ['This cannot be renamed'] * 3
                         + ['This cannot be deleted'] * 3)

    async def test_delete_a_playlist_once_confirmed(self):
        await self.run_action('item-delete', 'playlist', 'p.pl1')
        heading, body, confirm, done = self.confirms[0]
        self.assertEqual((heading, confirm), ('Delete Playlist?', '_Delete'))
        self.assertIn('“Road Trip”', body)
        self.assertIn('all your devices', body)
        self.assertEqual(self.writes(), [])  # nothing before the confirmation
        done()
        await self.settle()
        self.assertEqual(self.writes(), [('delete_playlist', 'p.pl1')])
        self.assertEqual(self.app.toasts, ['Deleted “Road Trip”'])
        # Its pages are left, for the folder that held it; the library follows.
        self.assertEqual(self.window.left, [({('playlist', 'p.pl1')}, 'p.fd1')])
        self.assertEqual(self.app.syncs, ['playlists'])

    async def test_delete_a_folder_with_what_is_in_it(self):
        await self.run_action('item-delete', 'folder', 'p.fd1')
        heading, body, _confirm, done = self.confirms[0]
        self.assertEqual(heading, 'Delete Folder?')
        self.assertIn('“Evenings” and the playlist in it', body)
        done()
        await self.settle()
        self.assertEqual(self.writes(), [('delete_folder', 'p.fd1')])
        self.assertEqual(self.window.left,
                         [({('folder', 'p.fd1'), ('playlist', 'p.pl1')}, 'root')])

    async def test_a_failed_delete_is_reported_and_leaves_nothing(self):
        self.app.engine.fail = EngineError('api', 'HTTP 500')
        await self.run_action('item-delete', 'playlist', 'p.pl1')
        self.confirms[0][3]()
        await self.settle()
        self.assertEqual(self.app.reported, ['api'])
        self.assertEqual((self.window.left, self.app.syncs, self.app.toasts), ([], [], []))

    async def test_new_playlist_holding_a_song(self):
        self.actions.menu_for(LIBRARY_TRACK)
        await self.run_action('item-new-playlist', 'song', 'i.song1')
        heading, confirm, name, description, done = self.asked[0]
        self.assertEqual((heading, confirm, name, description),
                         ('New Playlist', '_Create', '', ''))
        done('Harbour Songs', None)
        await self.settle()
        self.assertEqual(self.writes(), [
            ('create_playlist', 'Harbour Songs', None, (('song', 'i.song1'),), None)])
        self.assertEqual(self.app.toasts,
                         ['Added “Harbour Lights” to the new playlist “Harbour Songs”'])
        self.assertIn(('folder_children', 'root'), self.app.engine.calls)
        self.assertEqual(self.window.selected, [])  # the song's page stays

    async def test_new_playlist_in_a_folder_is_shown(self):
        new = Item({'id': 'p.new', 'kind': 'playlist', 'title': 'Fresh'})

        def start_sync(quick=False, playlists=False):
            self.app.library.items[('playlist', 'p.new')] = new  # the pass brought it

        self.app.start_sync = start_sync
        await self.run_action('item-new-playlist', 'folder', 'p.fd1')
        self.asked[0][4]('Fresh', 'Brand new')
        await self.settle()
        self.assertEqual(self.writes(), [('create_playlist', 'Fresh', 'Brand new', (), 'p.fd1')])
        self.assertEqual(self.app.toasts, ['Created “Fresh”'])
        self.assertEqual(self.window.selected, ['playlist:p.new'])

    async def test_the_demo_shows_the_dialog_and_writes_nothing(self):
        self.app.demo = True
        await self.run_action('item-new-playlist', 'folder', 'root')
        self.asked[0][4]('Fresh', None)
        await self.run_action('item-delete', 'playlist', 'p.pl1')
        self.confirms[0][3]()
        await self.settle()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.app.toasts, ['Not available with the demo library'] * 2)

    async def test_waiting_for_the_listing(self):
        listed = [{'kind': 'playlist', 'id': 'p.pl1', 'name': 'Road Trip'}]
        engine = self.app.engine
        engine.listings = [[], listed]
        self.assertTrue(await self.actions.wait_listed('root', 'p.pl1', 'Road Trip'))
        engine.listings = [listed, []]
        self.assertTrue(await self.actions.wait_listed('root', 'p.pl1', None))
        engine.listings = [listed] * 3
        self.assertFalse(await self.actions.wait_listed('root', 'p.pl1', 'Night Drive'))
        engine.listings = [EngineError('engine-down')]
        self.assertFalse(await self.actions.wait_listed('root', 'p.pl1', None))


if __name__ == '__main__':
    unittest.main()
