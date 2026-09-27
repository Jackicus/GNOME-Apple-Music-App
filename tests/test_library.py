"""The library model: Library.load() over a generated demo library and small invented ones.

No display needed: the model is GObject and Gio only, and load() runs under asyncio.run.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import ROOT

from applemusic.library import SECTIONS, Group, Item, Library, Track


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


def album(album_id, title, track_ids):
    play = {'kind': 'album', 'id': album_id}
    return {'id': album_id, 'kind': 'album', 'title': title, 'subtitle': 'Test Artist',
            'year': 2001, 'genre': None, 'summary': None, 'art': None, 'thumb': None,
            'artColor': None, 'countLabel': f'{len(track_ids)} songs', 'explicit': False,
            'catalogId': None, 'url': None, 'play': play,
            'groups': [{'name': 'Disc 1', 'play': play,
                        'entries': [track(t, f'Song {t}', i) for i, t in enumerate(track_ids)]}]}


def write_library(cache_dir, albums, shelves=()):
    data = {'version': 1, 'generated': '2026-01-01T00:00:00Z', 'storefront': 'gb',
            'sections': {'albums': albums}, 'shelves': list(shelves)}
    Path(cache_dir, 'library.json').write_text(json.dumps(data), encoding='utf-8')


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
        songs = self.library.songs
        self.assertIs(self.library.songs, songs)
        self.assertEqual([t.id for t in songs], expected)
        self.assertEqual(self.library.song_count(), len(expected))
        # The same Track objects as the albums' own groups.
        first_album = self.library.albums.get_item(0)
        self.assertIs(songs.get_item(0), first_album.groups[0].entries.get_item(0))


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
        self.assertFalse(library._songs_built)

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
        self.assertEqual([t.id for t in library.songs], ['i.1', 'i.2', 'i.3'])
        self.assertEqual([t.album for t in library.songs], ['Test Album'] * 3)

    def test_properties_notify(self):
        item = Item({'id': 'l.x', 'kind': 'album', 'title': 'Before'})
        seen = []
        item.connect('notify::title', lambda obj, _pspec: seen.append(obj.title))
        item.title = 'After'
        item.set_property('title', 'Again')
        self.assertEqual(seen, ['After', 'Again'])
        self.assertEqual(item.get_property('title'), 'Again')


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
        albums, songs = library.albums, library.songs
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

        async def two_loads():
            with mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.cache}):
                first = asyncio.ensure_future(library.load())
                await asyncio.sleep(0)  # the first reads its path and goes to its thread
            with mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': other.name}):
                second = asyncio.ensure_future(library.load())
                await asyncio.sleep(0)
            await asyncio.gather(first, second)

        asyncio.run(two_loads())
        self.assertEqual([item.id for item in library.albums], ['l.new'])
        self.assertEqual(library.state, 'ready')
        self.assertEqual(len(changed), 1)  # the overtaken load gave up without a word


if __name__ == '__main__':
    unittest.main()
