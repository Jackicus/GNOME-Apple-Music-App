"""The library model: Library.load() over a generated demo library and small invented ones.

No display needed: the model is GObject and Gio only, and load() runs under asyncio.run.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import ROOT

from applemusic import library as library_module
from applemusic.library import (ROOT_FOLDER, SECTIONS, Group, Item, Library, PlaylistTree,
                                SongOrder, Track, fold)


def load(cache_dir, library=None):
    """A Library loaded from cache_dir/library.json, config pointed there as demo mode does."""
    library = library or Library()
    with mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': str(cache_dir)}):
        asyncio.run(library.load())
    return library


def track(track_id, title, index):
    return {'id': track_id, 'catalogId': None, 'title': title, 'artist': 'Test Artist',
            'album': 'Test Album', 'trackNumber': index + 1, 'discNumber': 1,
            'durationMs': 180000, 'durationLabel': '3:00', 'explicit': False, 'index': index,
            'thumb': None}


def build_songs(library):
    asyncio.run(library.build_songs())
    return library.songs


def album(album_id, title, track_ids):
    play = {'kind': 'album', 'id': album_id}
    return {'id': album_id, 'kind': 'album', 'title': title, 'subtitle': 'Test Artist',
            'year': 2001, 'genre': None, 'summary': None, 'art': None, 'thumb': None,
            'artColor': None, 'countLabel': f'{len(track_ids)} songs', 'explicit': False,
            'catalogId': None, 'url': None, 'play': play,
            'groups': [{'name': 'Disc 1', 'play': play,
                        'entries': [track(t, f'Song {t}', i) for i, t in enumerate(track_ids)]}]}


def write_library(cache_dir, albums, shelves=(), playlists=None, folders=None):
    data = {'version': 1, 'generated': '2026-01-01T00:00:00Z', 'storefront': 'gb',
            'sections': {'albums': albums}, 'shelves': list(shelves)}
    if playlists is not None:
        data['sections']['playlists'] = playlists
    if folders is not None:
        data['folders'] = folders
    Path(cache_dir, 'library.json').write_text(json.dumps(data), encoding='utf-8')


def playlist(playlist_id, title=None):
    return Item({'id': playlist_id, 'kind': 'playlist', 'title': title or playlist_id,
                 'play': {'kind': 'playlist', 'id': playlist_id}, 'groups': []})


def folder(folder_id, children, parent=ROOT_FOLDER, title=None):
    """A folders entry; children are 'f:<id>' for a folder and a plain id for a playlist."""
    return {'id': folder_id, 'title': title or folder_id, 'parent': parent,
            'children': [{'kind': 'folder', 'id': child[2:]} if child.startswith('f:')
                         else {'kind': 'playlist', 'id': child} for child in children]}


def shape(tree):
    """A PlaylistTree's flat list as (kind, id, depth, ancestors)."""
    return [(node.kind, node.id, node.depth, node.ancestors()) for node in tree.flat]


class TestDemoLibrary(unittest.TestCase):
    """The generated demo library, loaded once for the whole class."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.cache = cls.temp_dir.name
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'demo_library.py'),
                        '--cache', cls.cache], check=True, capture_output=True)
        with open(os.path.join(cls.cache, 'library.json'), encoding='utf-8') as file:
            cls.data = json.load(file)
        cls.library = load(cls.cache)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def store_ids(self, store):
        return [item.id for item in store]

    def test_state_and_metadata(self):
        self.assertEqual(self.library.state, 'ready')
        self.assertEqual(self.library.generated, self.data['generated'])
        self.assertEqual(self.library.storefront, self.data['storefront'])

    def test_counts_match_library_json(self):
        sections = self.data['sections']
        for name in SECTIONS:
            with self.subTest(section=name):
                store = getattr(self.library, name)
                self.assertEqual(self.store_ids(store),
                                 [item['id'] for item in sections.get(name, [])])
        self.assertEqual(self.library.videos.get_n_items(), 0)  # the demo has no videos
        self.assertEqual([(shelf.key, shelf.title, shelf.items.get_n_items())
                          for shelf in self.library.shelves],
                         [(shelf['key'], shelf['title'], len(shelf['items']))
                          for shelf in self.data['shelves']])
        self.assertIs(self.library.shelf('recently-added'), self.library.shelves[1])
        self.assertIsNone(self.library.shelf('no-such-shelf'))

    def test_item_mirrors_the_shape(self):
        raw = self.data['sections']['albums'][0]
        item = self.library.albums.get_item(0)
        self.assertIsInstance(item, Item)
        self.assertEqual(item.raw, raw)
        self.assertEqual(item.play, raw['play'])
        for prop, key in [('id', 'id'), ('kind', 'kind'), ('title', 'title'),
                          ('subtitle', 'subtitle'), ('year', 'year'), ('genre', 'genre'),
                          ('summary', 'summary'), ('art', 'art'), ('thumb', 'thumb'),
                          ('art-color', 'artColor'), ('count-label', 'countLabel'),
                          ('explicit', 'explicit'), ('catalog-id', 'catalogId'), ('url', 'url')]:
            with self.subTest(prop=prop):
                self.assertEqual(item.get_property(prop), raw[key])
        station = self.library.radio.get_item(0)
        self.assertIsNone(self.data['sections']['radio'][0]['year'])
        self.assertEqual(station.year, 0)  # JSON null in an int property

    def test_by_id(self):
        for name in SECTIONS:
            for item in getattr(self.library, name):
                self.assertIs(self.library.by_id(item.kind, item.id), item)
        first = self.library.albums.get_item(0)
        self.assertIsNone(self.library.by_id('playlist', first.id))
        self.assertIsNone(self.library.by_id('album', 'l.nothing'))

    def test_shelf_items_are_the_section_items(self):
        for shelf in self.library.shelves:
            for item in shelf.items:
                self.assertIs(self.library.by_id(item.kind, item.id), item)

    def test_songs_are_every_album_track_once(self):
        expected = []
        for raw in self.data['sections']['albums']:
            for group in raw['groups']:
                for entry in group['entries']:
                    if entry['id'] not in expected:
                        expected.append(entry['id'])
        self.assertEqual(self.library.song_count(), len(expected))
        songs = build_songs(self.library)
        self.assertTrue(self.library.songs_ready)
        self.assertIs(self.library.songs, songs)
        self.assertEqual([t.id for t in songs], expected)
        self.assertEqual(self.library.song_count(), len(expected))
        # The same Track objects as the albums' own groups.
        first_album = self.library.albums.get_item(0)
        self.assertIs(songs.get_item(0), first_album.groups[0].entries.get_item(0))

    def test_favourite_songs(self):
        flagged = [raw['id'] for raw in self.data['sections']['playlists']
                   if raw.get('attributes', {}).get('isFavourites')]
        self.assertEqual(len(flagged), 1)
        favourites = self.library.favourite_songs()
        self.assertIs(favourites, self.library.by_id('playlist', flagged[0]))
        self.assertTrue(favourites.favourites)
        self.assertEqual([item.id for item in self.library.playlists if item.favourites],
                         flagged)

    def test_playlist_tree(self):
        tree = self.library.playlist_tree()
        folders = {raw['id']: raw for raw in self.data['folders']}

        def expected(folder_id, depth, ancestors):
            nodes = []
            for child in folders[folder_id]['children']:
                nodes.append((child['kind'], child['id'], depth, ancestors))
                if child['kind'] == 'folder':
                    nodes += expected(child['id'], depth + 1, ancestors + [child['id']])
            return nodes

        self.assertEqual(shape(tree), expected(ROOT_FOLDER, 0, []))
        # Three folders, one inside another, and every playlist once, as its section Item.
        self.assertEqual([(node.item.title, node.depth) for node in tree.folders()],
                         [(folders[node.id]['title'], node.depth) for node in tree.folders()])
        self.assertEqual(sorted(node.depth for node in tree.folders()), [0, 0, 1])
        playlists = [node for node in tree.flat if node.kind == 'playlist']
        self.assertEqual(sorted(node.id for node in playlists),
                         sorted(self.store_ids(self.library.playlists)))
        for node in playlists:
            self.assertIs(node.item, self.library.by_id('playlist', node.id))
        # Nested: each node is among its parent's children, and the stores mirror them.
        for node in tree.flat:
            self.assertIn(node, node.parent.children)
        for folder_node in [tree.root] + tree.folders():
            self.assertIs(self.library.by_id('folder', folder_node.id), folder_node.item)
            self.assertEqual(folder_node.item.kind, 'folder')
            store = self.library.folder_items(folder_node.id)
            self.assertIs(store, folder_node.store)
            self.assertEqual(list(store), [child.item for child in folder_node.children])
        self.assertIs(tree.folder(ROOT_FOLDER), tree.root)
        self.assertEqual(tree.root.depth, -1)
        self.assertIsNone(self.library.folder_items('l.no-such-folder'))
        # Favourite Songs is in the tree like any playlist (the sidebar leaves it out).
        self.assertIn(self.library.favourite_songs(), [node.item for node in playlists])

    def test_track_at(self):
        two_discs = next(item for item in self.library.albums if len(item.groups) > 1)
        last = two_discs.groups[1].entries.get_item(0)  # counted on from disc 1
        self.assertGreater(last.index, 0)
        self.assertIs(self.library.track_at(two_discs.play, last.index), last)
        playlist = self.library.playlists.get_item(0)
        entry = playlist.groups[0].entries.get_item(2)
        self.assertIs(self.library.track_at(entry.play, 2), entry)
        self.assertIsNone(self.library.track_at(playlist.play, 10_000))
        self.assertIsNone(self.library.track_at({'kind': 'album', 'id': 'l.nothing'}, 0))
        self.assertIsNone(self.library.track_at(None, 0))


class TestLaziness(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache = self.temp_dir.name

    def test_groups_wrap_on_first_access(self):
        raw = album('l.a1', 'First', ['i.1', 'i.2', 'i.3'])
        raw['thumb'] = '/nowhere/thumb/a1.jpg'
        raw['groups'].append({'name': 'Disc 2', 'play': raw['play'],
                              'entries': [dict(track('i.4', 'Four', 3), discNumber=2)]})
        write_library(self.cache, [raw])
        library = load(self.cache)
        item = library.albums.get_item(0)
        self.assertIsNone(item._groups)
        self.assertFalse(library.songs_ready)
        self.assertEqual(library.songs.get_n_items(), 0)  # nothing asked for it

        groups = item.groups
        self.assertIs(item.groups, groups)
        self.assertEqual([g.name for g in groups], ['Disc 1', 'Disc 2'])
        self.assertIsInstance(groups[0], Group)
        self.assertEqual(groups[0].play, raw['play'])
        tracks = list(groups[0].entries)
        self.assertTrue(all(isinstance(t, Track) for t in tracks))
        self.assertEqual([(t.id, t.title, t.index, t.track_number) for t in tracks],
                         [('i.1', 'Song i.1', 0, 1), ('i.2', 'Song i.2', 1, 2),
                          ('i.3', 'Song i.3', 2, 3)])
        first = tracks[0]
        self.assertEqual(first.get_property('duration-ms'), 180000)
        self.assertEqual(first.duration_label, '3:00')
        self.assertEqual(first.play, raw['play'])
        self.assertEqual(first.thumb, raw['thumb'])  # an album track draws its album's cover
        self.assertEqual(groups[1].entries.get_item(0).disc_number, 2)

    def test_songs_deduplicated_by_id(self):
        write_library(self.cache, [album('l.a1', 'One', ['i.1', 'i.2']),
                                   album('l.a2', 'Two', ['i.2', 'i.3', 'i.1'])])
        library = load(self.cache)
        self.assertEqual(library.song_count(), 3)
        self.assertEqual([t.id for t in build_songs(library)], ['i.1', 'i.2', 'i.3'])
        self.assertEqual([t.album for t in library.songs], ['Test Album'] * 3)
        self.assertEqual(library.songs.get_item(0).search_key, 'song i.1\ntest artist\ntest album')

    def test_favourites_flag(self):
        self.assertTrue(Item({'kind': 'playlist', 'attributes': {'isFavourites': True}}).favourites)
        for raw in ({'kind': 'playlist'}, {'attributes': None}, {'attributes': ['isFavourites']},
                    {'attributes': {'isFavourites': 'yes'}}, {'attributes': {}}):
            with self.subTest(raw=raw):
                self.assertFalse(Item(raw).favourites)
        write_library(self.cache, [album('l.a1', 'One', ['i.1'])])
        self.assertIsNone(load(self.cache).favourite_songs())

    def test_properties_notify(self):
        item = Item({'id': 'l.x', 'kind': 'album', 'title': 'Before'})
        seen = []
        item.connect('notify::title', lambda obj, _pspec: seen.append(obj.title))
        item.title = 'After'
        item.set_property('title', 'Again')
        self.assertEqual(seen, ['After', 'Again'])
        self.assertEqual(item.get_property('title'), 'Again')

    def test_merge_takes_a_fetched_item_in(self):
        # A shelf hit: no groups, little else. The engine's item() answer fills it in.
        item = Item({'id': 'l.a1', 'kind': 'album', 'title': 'Shelf Title',
                     'subtitle': 'Test Artist', 'play': {'kind': 'album', 'id': 'l.a1'}})
        self.assertEqual(item.groups, [])
        seen = []
        item.connect('notify', lambda obj, pspec: seen.append(pspec.name))
        answer = album('l.a1', 'Full Title', ['i.1', 'i.2'])
        answer.update(genre='Rock', summary='Notes', art='/x/art.jpg', thumb='/x/thumb.jpg',
                      artColor='#112233', cached='2026-01-01T00:00:00Z', kind='album',
                      id='l.a1')
        item.merge(answer)
        self.assertEqual((item.title, item.genre, item.summary, item.year), ('Full Title',
                         'Rock', 'Notes', 2001))
        self.assertEqual((item.art, item.thumb, item.art_color, item.count_label),
                         ('/x/art.jpg', '/x/thumb.jpg', '#112233', '2 songs'))
        self.assertEqual(item.raw['groups'], answer['groups'])
        self.assertEqual(item.raw['cached'], answer['cached'])
        self.assertEqual([t.id for t in item.groups[0].entries], ['i.1', 'i.2'])
        self.assertEqual(item.groups[0].entries.get_item(0).thumb, '/x/thumb.jpg')
        self.assertEqual(item.play, answer['play'])
        self.assertIn('title', seen)
        self.assertIn('art', seen)
        self.assertNotIn('subtitle', seen)  # unchanged: no notify
        # Merging again with the same answer changes nothing and notifies nothing.
        seen.clear()
        item.merge(answer)
        self.assertEqual(seen, [])


class TestLoading(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache = self.temp_dir.name

    def watch(self, library):
        """The states library passes through and how often it emits 'changed'."""
        states, changed = [], []
        library.connect('notify::state', lambda obj, _pspec: states.append(obj.state))
        library.connect('changed', lambda _obj: changed.append(True))
        return states, changed

    def test_missing_file_is_empty(self):
        library = Library()
        states, changed = self.watch(library)
        load(self.cache, library)
        self.assertEqual(states, ['loading', 'empty'])
        self.assertEqual(len(changed), 1)
        self.assertEqual(library.albums.get_n_items(), 0)
        self.assertEqual(library.shelves, [])
        self.assertEqual(library.song_count(), 0)

    def test_unreadable_file_is_empty(self):
        Path(self.cache, 'library.json').write_text('{"version": 1, "sect', encoding='utf-8')
        with self.assertLogs('applemusic.library', 'WARNING'):
            library = load(self.cache)
        self.assertEqual(library.state, 'empty')

    def test_batches_keep_order(self):
        ids = [f'l.a{n}' for n in range(23)]
        write_library(self.cache, [album(i, i, []) for i in ids])
        library = Library()
        library.batch_size = 5
        states, changed = self.watch(library)
        load(self.cache, library)
        self.assertEqual([item.id for item in library.albums], ids)
        self.assertEqual(states, ['loading', 'ready'])
        self.assertEqual(len(changed), 1)

    def test_reload_refills_the_same_stores(self):
        write_library(self.cache, [album(f'l.a{n}', f'A{n}', [f'i.{n}']) for n in range(12)],
                      shelves=[{'key': 'recently-added', 'title': 'Recently Added',
                                'items': [album('l.a3', 'A3', [])]}])
        library = Library()
        library.batch_size = 5
        load(self.cache, library)
        albums, songs = library.albums, build_songs(library)
        self.assertEqual(songs.get_n_items(), 12)
        self.assertIs(library.shelf('recently-added').items.get_item(0),
                      library.by_id('album', 'l.a3'))

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        write_library(other.name, [album('l.b1', 'B1', ['i.x', 'i.y'])])
        load(other.name, library)
        self.assertIs(library.albums, albums)
        self.assertEqual([item.id for item in albums], ['l.b1'])
        self.assertIs(library.songs, songs)  # built before, so kept current
        self.assertEqual([t.id for t in songs], ['i.x', 'i.y'])
        self.assertIsNone(library.by_id('album', 'l.a3'))
        self.assertIsNone(library.shelf('recently-added'))

    def test_a_newer_load_wins(self):
        write_library(self.cache, [album('l.old', 'Old', [])])
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        write_library(other.name, [album('l.new', 'New', [])])
        library = Library()
        states, changed = self.watch(library)
        # The first load's read waits in its thread until the second load has started: without
        # that, a quick thread could finish the first load before the second began.
        second_started = threading.Event()
        read = library_module._read_library

        def read_after_second_starts(path):
            if Path(path).parent == Path(self.cache):
                second_started.wait(5)
            return read(path)

        async def two_loads():
            with mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.cache}):
                first = asyncio.ensure_future(library.load())
                await asyncio.sleep(0)  # the first reads its path and goes to its thread
            with mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': other.name}):
                second = asyncio.ensure_future(library.load())
                await asyncio.sleep(0)
            second_started.set()
            await asyncio.gather(first, second)

        with mock.patch.object(library_module, '_read_library', read_after_second_starts):
            asyncio.run(two_loads())
        self.assertEqual([item.id for item in library.albums], ['l.new'])
        self.assertEqual(library.state, 'ready')
        self.assertEqual(len(changed), 1)  # the overtaken load gave up without a word


class TestPlaylistTree(unittest.TestCase):
    """PlaylistTree over invented folders: the children lists decide, the rest is tidied up."""

    def test_nesting_and_order(self):
        playlists = [playlist(f'p{n}') for n in range(1, 6)]
        tree = PlaylistTree([
            folder('root', ['f:a', 'p5', 'f:c']),
            folder('a', ['f:b', 'p2', 'p1']),
            folder('b', ['p3'], parent='a'),
            folder('c', ['p4']),
        ], playlists)
        self.assertEqual(shape(tree), [
            ('folder', 'a', 0, []),
            ('folder', 'b', 1, ['a']),
            ('playlist', 'p3', 2, ['a', 'b']),
            ('playlist', 'p2', 1, ['a']),
            ('playlist', 'p1', 1, ['a']),
            ('playlist', 'p5', 0, []),
            ('folder', 'c', 0, []),
            ('playlist', 'p4', 1, ['c']),
        ])
        self.assertEqual([child.id for child in tree.root.children], ['a', 'p5', 'c'])
        self.assertEqual([child.id for child in tree.folder('a').children], ['b', 'p2', 'p1'])
        self.assertIs(tree.folder('b').parent, tree.folder('a'))
        self.assertIs(tree.flat[2].item, playlists[2])
        self.assertEqual(tree.folder('a').item.title, 'a')
        self.assertEqual([item.id for item in tree.folder('a').store], ['b', 'p2', 'p1'])

    def test_without_folders_every_playlist_is_at_the_top(self):
        playlists = [playlist('p2'), playlist('p1')]
        tree = PlaylistTree([], playlists)
        self.assertEqual(shape(tree), [('playlist', 'p2', 0, []), ('playlist', 'p1', 0, [])])
        self.assertEqual(list(tree.root.store), playlists)
        self.assertEqual(shape(PlaylistTree()), [])

    def test_what_nothing_lists_goes_at_the_end_of_the_top(self):
        playlists = [playlist(f'p{n}') for n in range(1, 5)]
        root = folder('root', ['p2', 'f:gone', 'p.gone', 'f:p1'])  # p1 is not a folder
        root['children'] += [{'kind': 'station', 'id': 'p4'}, 'p4', {'id': 'p4'}]
        tree = PlaylistTree([root, folder('orphan', ['p3'], parent='nowhere')], playlists)
        self.assertEqual(shape(tree), [
            ('playlist', 'p2', 0, []),
            ('folder', 'orphan', 0, []),
            ('playlist', 'p3', 1, ['orphan']),
            ('playlist', 'p1', 0, []),
            ('playlist', 'p4', 0, []),
        ])

    def test_nothing_twice_and_no_cycles(self):
        playlists = [playlist('p1'), playlist('p2')]
        tree = PlaylistTree([
            folder('root', ['f:a', 'p1', 'f:a']),
            folder('a', ['p1', 'f:b', 'f:a', 'f:root', 'p2']),
            folder('b', ['f:a', 'p2'], parent='a'),
        ], playlists)
        self.assertEqual(shape(tree), [
            ('folder', 'a', 0, []),
            ('playlist', 'p1', 1, ['a']),
            ('folder', 'b', 1, ['a']),
            ('playlist', 'p2', 2, ['a', 'b']),
        ])

    def test_without_a_root_entry_the_top_is_the_parentless_folders(self):
        playlists = [playlist('p1'), playlist('p2')]
        tree = PlaylistTree([
            folder('a', ['p1'], parent=None),
            folder('b', ['p2'], parent='a'),  # listed by nothing: at the top, after a
        ], playlists)
        self.assertEqual(shape(tree), [
            ('folder', 'a', 0, []),
            ('playlist', 'p1', 1, ['a']),
            ('folder', 'b', 0, []),
            ('playlist', 'p2', 1, ['b']),
        ])

    def test_loaded_with_the_library(self):
        with tempfile.TemporaryDirectory() as cache:
            raw = [{'id': f'p{n}', 'kind': 'playlist', 'title': f'P{n}',
                    'play': {'kind': 'playlist', 'id': f'p{n}'}, 'groups': []} for n in (1, 2)]
            write_library(cache, [], playlists=raw,
                          folders=[folder('root', ['f:a']), folder('a', ['p2', 'p1'])])
            library = load(cache)
            first = library.playlist_tree()
            self.assertEqual(shape(first), [('folder', 'a', 0, []), ('playlist', 'p2', 1, ['a']),
                                            ('playlist', 'p1', 1, ['a'])])
            self.assertIs(library.by_id('folder', 'a'), first.folder('a').item)
            self.assertEqual([item.id for item in library.folder_items('a')], ['p2', 'p1'])
            self.assertIs(library.folder_items('a').get_item(0), library.by_id('playlist', 'p2'))
            # A reload makes a new tree; one without folders puts everything at the top.
            write_library(cache, [], playlists=raw)
            load(cache, library)
            self.assertIsNot(library.playlist_tree(), first)
            self.assertEqual(shape(library.playlist_tree()),
                             [('playlist', 'p1', 0, []), ('playlist', 'p2', 0, [])])
            self.assertIsNone(library.by_id('folder', 'a'))
            self.assertIsNone(library.folder_items('a'))
        self.assertEqual(shape(Library().playlist_tree()), [])  # before any load


class TestBuildSongs(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache = self.temp_dir.name
        self.ids = [f'i.{n}' for n in range(30)]
        write_library(self.cache, [album(f'l.a{n}', f'A{n}', self.ids[n * 3:n * 3 + 3])
                                   for n in range(10)])

    def test_batches_pause_and_splice_once(self):
        library = load(self.cache)
        spliced = []
        library.songs.connect('items-changed', lambda _store, *change: spliced.append(change))
        pauses = []
        yield_to_frames = library_module.yield_to_frames

        async def counting_yield():
            pauses.append(True)
            await yield_to_frames()

        # A zero budget pauses after every album.
        with mock.patch.object(library_module, 'FRAME_BUDGET', 0), \
                mock.patch.object(library_module, 'yield_to_frames', counting_yield):
            build_songs(library)
        self.assertEqual([t.id for t in library.songs], self.ids)
        self.assertEqual(len(pauses), 10)
        self.assertEqual(spliced, [(0, 0, 30)])
        self.assertTrue(library.songs_ready)
        build_songs(library)  # filled already: nothing happens
        self.assertEqual(spliced, [(0, 0, 30)])

    def test_asked_while_loading_the_load_fills_it(self):
        library = Library()
        ready = []
        library.connect('notify::songs-ready', lambda obj, _pspec: ready.append(obj.state))

        async def load_and_ask():
            with mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.cache}):
                loading = asyncio.ensure_future(library.load())
                await asyncio.sleep(0)  # the load has started
            self.assertEqual(library.state, 'loading')
            await library.build_songs()  # returns at once: the load will do it
            self.assertFalse(library.songs_ready)
            await loading

        asyncio.run(load_and_ask())
        self.assertEqual([t.id for t in library.songs], self.ids)
        self.assertEqual(ready, ['loading'])  # filled before the load reported 'ready'

    def test_a_load_during_the_build_refills_it(self):
        library = load(self.cache)
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        write_library(other.name, [album('l.b1', 'B1', ['i.x', 'i.y'])])

        async def build_then_reload():
            build = asyncio.ensure_future(library.build_songs())
            await asyncio.sleep(0)  # the build has paused after its first album
            with mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': other.name}):
                await library.load()
            await build

        with mock.patch.object(library_module, 'FRAME_BUDGET', 0):
            asyncio.run(build_then_reload())
        self.assertEqual([t.id for t in library.songs], ['i.x', 'i.y'])
        self.assertTrue(library.songs_ready)

    def test_nothing_to_load_is_ready_and_empty(self):
        library = Library()
        load(tempfile.gettempdir() + '/no-such-apple-music-cache', library)
        self.assertEqual(build_songs(library).get_n_items(), 0)
        self.assertTrue(library.songs_ready)


class TestSongOrder(unittest.TestCase):
    def setUp(self):
        rows = [
            # id, title, artist, album, disc, track, ms
            ('i.1', 'beta', 'Zed', 'Second', 1, 2, 200000),
            ('i.2', 'Alpha', 'zed', 'Second', 1, 1, 100000),
            ('i.3', 'alpha', 'Ann', 'First', 2, 1, 300000),
            ('i.4', 'Gamma', 'Ann', 'First', 1, 1, 100000),
            ('i.5', 'delta', 'Ann', 'Third', 1, 1, 250000),
        ]
        self.tracks = [Track({'id': i, 'title': title, 'artist': artist, 'album': name,
                              'discNumber': disc, 'trackNumber': number, 'durationMs': ms})
                       for i, title, artist, name, disc, number, ms in rows]
        self.order = SongOrder(self.tracks)

    def ids(self, column, descending=False):
        return [t.id for t in self.order.tracks(column, descending)]

    def test_title_ignores_case_and_breaks_ties_by_artist(self):
        self.assertEqual(self.ids('title'), ['i.3', 'i.2', 'i.1', 'i.5', 'i.4'])
        self.assertEqual(self.ids('title', descending=True), ['i.4', 'i.5', 'i.1', 'i.3', 'i.2'])

    def test_artist_keeps_albums_and_tracks_in_order_both_ways(self):
        self.assertEqual(self.ids('artist'), ['i.4', 'i.3', 'i.5', 'i.2', 'i.1'])
        self.assertEqual(self.ids('artist', descending=True), ['i.2', 'i.1', 'i.4', 'i.3', 'i.5'])

    def test_album_then_artist_then_track(self):
        self.assertEqual(self.ids('album'), ['i.4', 'i.3', 'i.2', 'i.1', 'i.5'])
        self.assertEqual(self.ids('album', descending=True), ['i.5', 'i.2', 'i.1', 'i.4', 'i.3'])

    def test_time_is_numeric(self):
        self.assertEqual(self.ids('time'), ['i.2', 'i.4', 'i.1', 'i.5', 'i.3'])
        self.assertEqual(self.ids('time', descending=True), ['i.3', 'i.5', 'i.1', 'i.2', 'i.4'])

    def test_fold_ignores_case_and_accents(self):
        self.assertEqual(fold('Beyoncé'), 'beyonce')
        self.assertEqual(fold('ÉLAN Straße'), 'elan strasse')
        self.assertEqual(fold('Ｆｕｌｌ'), 'full')  # compatibility forms too
        self.assertEqual(fold('Plain ASCII'), 'plain ascii')
        track = Track({'title': 'Café', 'artist': 'Zoë', 'album': 'Über'})
        self.assertEqual(track.search_key, 'cafe\nzoe\nuber')

    def test_same_objects_and_repeatable(self):
        first = self.order.tracks('artist')
        self.assertTrue(all(any(t is u for u in self.tracks) for t in first))
        self.assertEqual(self.order.tracks('artist'), first)
        self.assertEqual(SongOrder([]).tracks('title'), [])


if __name__ == '__main__':
    unittest.main()
