# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Library.reload() over invented library.json files: what a reload tells, and to whom.

A reload keeps every Item, Track and Shelf still in the library and says what changed by
signals: property notifies, an Item's `groups-changed`, and `items-changed` on the stores only
where something came, went or moved, or where a sort key changed. No sync, no display.
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import ROOT  # noqa: F401  (registers src/ as applemusic)

from applemusic import library as library_module
from applemusic.library import Item, Library


def track(track_id, title=None, index=0, album='Test Album'):
    return {'id': track_id, 'catalogId': None, 'title': title or f'Song {track_id}',
            'artist': 'Test Artist', 'album': album, 'trackNumber': index + 1,
            'discNumber': 1, 'durationMs': 180000, 'durationLabel': '3:00', 'explicit': False,
            'index': index, 'thumb': None}


def album(album_id, title=None, track_ids=(), thumb=None):
    play = {'kind': 'album', 'id': album_id}
    title = title or album_id
    return {'id': album_id, 'kind': 'album', 'title': title, 'subtitle': 'Test Artist',
            'year': 2001, 'genre': None, 'summary': None, 'art': None, 'thumb': thumb,
            'artColor': None, 'countLabel': f'{len(track_ids)} songs', 'explicit': False,
            'catalogId': None, 'url': None, 'play': play,
            'groups': [{'name': 'Disc 1', 'play': play,
                        'entries': [track(t, index=n, album=title)
                                    for n, t in enumerate(track_ids)]}]}


def playlist(playlist_id, track_ids=(), title=None):
    play = {'kind': 'playlist', 'id': playlist_id}
    return {'id': playlist_id, 'kind': 'playlist', 'title': title or playlist_id,
            'subtitle': '', 'year': None, 'art': None, 'thumb': None,
            'countLabel': f'{len(track_ids)} songs', 'play': play,
            'groups': [{'name': 'Tracks', 'play': play,
                        'entries': [track(t, index=n) for n, t in enumerate(track_ids)]}]}


def station(station_id, title=None, thumb=None):
    return {'id': station_id, 'kind': 'station', 'title': title or station_id,
            'subtitle': 'Apple Music', 'thumb': thumb, 'art': None,
            'play': {'kind': 'station', 'id': station_id}, 'groups': []}


def shelf(key, items, title=None):
    return {'key': key, 'title': title if title is not None else key, 'items': items}


class ReloadCase(unittest.TestCase):
    """A Library over a temporary cache directory, config pointed there as demo mode does."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.cache = temp_dir.name
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.cache})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.library = Library()

    def write(self, albums=(), playlists=(), radio=(), shelves=(), folders=None, songs=None):
        sections = {'albums': list(albums), 'playlists': list(playlists), 'radio': list(radio)}
        if songs is not None:
            sections['songs'] = songs
        data = {'version': 1, 'generated': '2026-01-01T00:00:00Z', 'storefront': 'gb',
                'sections': sections, 'shelves': list(shelves)}
        if folders is not None:
            data['folders'] = folders
        Path(self.cache, 'library.json').write_text(json.dumps(data), encoding='utf-8')

    def load(self):
        asyncio.run(self.library.load())

    def reload(self):
        asyncio.run(self.library.reload())

    def notifies(self, obj):
        """The names of the properties obj notifies from now on, in order."""
        seen = []
        obj.connect('notify', lambda _obj, pspec: seen.append(pspec.name))
        return seen

    def splices(self, store):
        """The items-changed (position, removed, added) store emits from now on."""
        seen = []
        store.connect('items-changed', lambda _store, *change: seen.append(change))
        return seen

    def groups_changed(self, item):
        seen = []
        item.connect('groups-changed', lambda _item: seen.append(True))
        return seen


class TestReloadTells(ReloadCase):
    def test_a_renamed_item_notifies_its_title_once(self):
        albums = [album('l.a0'), album('l.a1', track_ids=['i.1'])]
        self.write(albums)
        self.load()
        item = self.library.by_id('album', 'l.a1')
        seen = self.notifies(item)
        changed = self.groups_changed(item)
        albums[1]['title'] = 'Renamed'
        self.write(albums)
        self.reload()
        self.assertIs(self.library.by_id('album', 'l.a1'), item)
        self.assertEqual(item.title, 'Renamed')
        self.assertEqual(seen, ['title'])
        self.assertEqual(changed, [])  # the tracks are the same

    def test_a_shelf_only_item_notifies_its_new_thumb(self):
        shelves = [shelf('heavy-rotation', [station('ra.1', thumb='/cache/thumb/old.jpg')])]
        self.write(shelves=shelves)
        self.load()
        item = self.library.shelf('heavy-rotation').items.get_item(0)
        seen = self.notifies(item)
        shelves[0]['items'][0]['thumb'] = '/cache/thumb/new.jpg'
        self.write(shelves=shelves)
        self.reload()
        self.assertIs(self.library.shelf('heavy-rotation').items.get_item(0), item)
        self.assertEqual(item.thumb, '/cache/thumb/new.jpg')
        self.assertEqual(seen, ['thumb'])

    def test_new_entries_of_the_same_count_emit_groups_changed_once(self):
        playlists = [playlist('p.1', ['i.1', 'i.2'])]
        self.write(playlists=playlists)
        self.load()
        item = self.library.by_id('playlist', 'p.1')
        groups = item.groups
        seen = self.notifies(item)
        changed = self.groups_changed(item)
        playlists[0]['groups'][0]['entries'][1] = track('i.3', index=1)
        self.write(playlists=playlists)
        self.reload()
        self.assertEqual(changed, [True])
        self.assertEqual(seen, [])  # the same count: nothing else changed
        self.assertIsNot(item.groups, groups)
        self.assertEqual([t.id for t in item.groups[0].entries], ['i.1', 'i.3'])
        # Reloading the same file again changes nothing.
        self.reload()
        self.assertEqual(changed, [True])

    def test_an_albums_new_thumbnail_reaches_its_tracks(self):
        albums = [album('l.a0', track_ids=['i.1', 'i.2'], thumb='/cache/thumb/old.jpg')]
        self.write(albums)
        self.load()
        asyncio.run(self.library.build_songs())
        item = self.library.by_id('album', 'l.a0')
        self.assertEqual(self.library.songs.get_item(0).thumb, '/cache/thumb/old.jpg')
        changed = self.groups_changed(item)
        albums[0]['thumb'] = '/cache/thumb/new.jpg'  # the tracks' own dicts are the same
        self.write(albums)
        self.reload()
        self.assertEqual(changed, [True])
        self.assertEqual(item.groups[0].entries.get_item(0).thumb, '/cache/thumb/new.jpg')
        self.assertEqual([t.thumb for t in self.library.songs], ['/cache/thumb/new.jpg'] * 2)
        self.assertIs(self.library.songs.get_item(0), item.groups[0].entries.get_item(0))

    def test_an_identical_reload_tells_nothing(self):
        albums = [album(f'l.a{n}', track_ids=[f'i.{n}']) for n in range(3)]
        playlists = [playlist('p.1', ['i.0'])]
        shelves = [shelf('recently-added', [albums[1], station('ra.1')])]
        self.write(albums, playlists, shelves=shelves)
        self.load()
        asyncio.run(self.library.build_songs())
        items = [*self.library.albums, *self.library.playlists,
                 *self.library.shelf('recently-added').items]
        seen = [self.notifies(item) for item in items]
        changed = [self.groups_changed(item) for item in items]
        stores = [self.library.albums, self.library.playlists, self.library.songs,
                  self.library.shelf('recently-added').items]
        splices = [self.splices(store) for store in stores]
        raws = [item.raw for item in items]
        self.reload()
        self.assertEqual(seen, [[]] * len(items))
        self.assertEqual(changed, [[]] * len(items))
        self.assertEqual(splices, [[]] * len(stores))
        # The dicts in hand are kept, not replaced by the new parse's equal copies.
        self.assertTrue(all(item.raw is raw for item, raw in zip(items, raws, strict=True)))

    def test_no_second_copy_of_the_dicts_is_kept(self):
        """Kept Groups and Tracks hold the dicts their Item holds, whether or not the Item
        changed, and a kept loose song's Track the dict the library holds."""
        albums = [album(f'l.a{n}', track_ids=[f'i.{n}', f'i.{n}{n}']) for n in range(2)]
        loose = [track('i.loose')]
        self.write(albums, songs=loose)
        self.load()
        asyncio.run(self.library.build_songs())
        first = self.library.by_id('album', 'l.a0')
        tracks = list(self.library.songs)
        albums[0]['genre'] = 'Jazz'  # changed, its groups not
        self.write(albums, songs=loose)
        self.reload()
        self.assertEqual(first.genre, 'Jazz')
        self.assertEqual(list(self.library.songs), tracks)  # the same Tracks
        for item in self.library.albums:
            with self.subTest(album=item.id):
                self.assertIs(item.groups[0].raw, item.raw['groups'][0])
                self.assertIs(item.groups[0].entries.get_item(0).raw,
                              item.raw['groups'][0]['entries'][0])
        self.assertIs(self.library.songs.get_item(0).raw,
                      self.library.albums.get_item(0).raw['groups'][0]['entries'][0])
        loose_track = self.library.songs.get_item(4)
        self.assertEqual(loose_track.id, 'i.loose')
        self.assertIs(loose_track.raw, self.library._loose[0])

    def test_merge_reads_as_the_getters_do(self):
        """merge() compares raw values with the getters' own normalising."""
        raws = [{'title': None, 'subtitle': 7, 'genre': '', 'year': None, 'explicit': 1,
                 'countLabel': None, 'art': 3.5, 'url': None},
                {'title': 'T', 'subtitle': '', 'genre': None, 'year': 1999, 'explicit': None,
                 'countLabel': '2 songs', 'art': None, 'url': 'https://example.com/x'},
                {'year': True, 'catalogId': 12345}]
        for raw in raws:
            # A playlist: an album or artist without a title is called by its kind, which a
            # merge does not change.
            item = Item(dict(raw, id='p.x', kind='playlist'))
            for name, key, read in Item._MERGED:
                with self.subTest(raw=raw, name=name):
                    self.assertEqual(read(item.raw.get(key)), item.get_property(name))

    def test_the_keep_path_pauses_within_its_frame_budget(self):
        albums = [album(f'l.a{n}') for n in range(20)]
        self.write(albums)
        self.load()
        for raw in albums:
            raw['genre'] = 'Jazz'
        self.write(albums)
        pauses = []
        yield_to_frames = library_module.yield_to_frames

        async def counting_yield():
            pauses.append(True)
            await yield_to_frames()

        with mock.patch.object(library_module, 'FRAME_BUDGET', 0), \
                mock.patch.object(library_module, 'yield_to_frames', counting_yield):
            self.reload()
        self.assertGreaterEqual(len(pauses), 20)  # a pause after each merge with no budget
        self.assertEqual({item.genre for item in self.library.albums}, {'Jazz'})

    def test_a_renamed_item_is_spliced_in_each_store_holding_it(self):
        albums = [album(f'l.a{n}') for n in range(5)]
        shelves = [shelf('recently-added', [albums[3], albums[1], albums[2]])]
        self.write(albums, shelves=shelves)
        self.load()
        in_albums = self.splices(self.library.albums)
        in_shelf = self.splices(self.library.shelf('recently-added').items)
        # l.a1 and l.a2 renamed (next to each other in both stores), l.a4's genre changed: the
        # views neither sort nor filter by the genre.
        for position in (1, 2):
            albums[position]['title'] += ' renamed'
        albums[4]['genre'] = 'Jazz'
        self.write(albums, shelves=[shelf('recently-added', [albums[3], albums[1], albums[2]])])
        self.reload()
        self.assertEqual(in_albums, [(1, 2, 2)])  # one splice for the run
        self.assertEqual(in_shelf, [(1, 2, 2)])
        self.assertEqual([item.title for item in self.library.albums],
                         ['l.a0', 'l.a1 renamed', 'l.a2 renamed', 'l.a3', 'l.a4'])

    def test_a_retitled_shelf_notifies_its_title(self):
        items = [album('l.a0')]
        self.write(items, shelves=[shelf('rec-1', items, title='Morning Mix')])
        self.load()
        kept = self.library.shelf('rec-1')
        seen = self.notifies(kept)
        self.write(items, shelves=[shelf('rec-1', items, title='Evening Mix')])
        self.reload()
        self.assertIs(self.library.shelf('rec-1'), kept)
        self.assertEqual(kept.title, 'Evening Mix')
        self.assertEqual(seen, ['title'])


if __name__ == '__main__':
    unittest.main()
