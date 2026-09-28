import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

import gi

from tests import ROOT

from applemusic.backend import config, normalize

gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf  # noqa: E402

REPO_DIR = str(ROOT)


# The numbers each kind of Item carries for its caption, as the normaliser writes them.
COUNTS = {'album': ('trackCount', 'durationMs'), 'playlist': ('trackCount', 'durationMs'),
          'artist': ('albumCount',), 'video': ('durationMs',)}


class TestDemoLibrarySchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Built once, through the command line as the scripts run it: drawing
        # the covers is most of this suite's time.
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.out_dir = cls.temp_dir.name
        cls.cli = subprocess.run(
            [sys.executable, os.path.join(REPO_DIR, 'scripts', 'demo_library.py'), '--cache',
             cls.out_dir],
            capture_output=True,
            text=True,
        )
        if cls.cli.returncode != 0:
            cls.temp_dir.cleanup()
            raise AssertionError(f'CLI failed: {cls.cli.stderr}')
        cls.lib_path = os.path.join(cls.out_dir, 'library.json')
        with open(cls.lib_path, encoding='utf-8') as f:
            cls.data = json.load(f)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_top_level_schema(self):
        self.assertEqual(self.data.get('version'), 2)  # sync.LIBRARY_VERSION
        self.assertEqual(self.data.get('storefront'), 'us')
        self.assertIn('generated', self.data)
        self.assertTrue(isinstance(self.data['generated'], str))
        self.assertIn('sections', self.data)
        self.assertIn('shelves', self.data)

    def test_sections_presence_and_counts(self):
        sections = self.data['sections']
        self.assertSetEqual(set(sections.keys()),
                            {'albums', 'artists', 'playlists', 'radio', 'videos'})

        # 40 albums, 15 artists, 13 playlists (Favourite Songs the last), 8 radio stations,
        # 6 music videos
        self.assertEqual(len(sections['albums']), 40)
        self.assertEqual(len(sections['artists']), 15)
        self.assertEqual(len(sections['playlists']), 13)
        self.assertEqual(len(sections['radio']), 8)
        self.assertEqual(len(sections['videos']), 6)

    def test_shelves_schema(self):
        shelves = self.data['shelves']
        self.assertEqual(len(shelves), 4)
        shelf_keys = [s.get('key') for s in shelves]
        self.assertEqual(
            shelf_keys,
            ['heavy-rotation', 'recently-added', 'recently-played', 'made-for-you'],
        )
        for shelf in shelves:
            self.assertIn('title', shelf)
            self.assertIn('items', shelf)
            self.assertGreater(len(shelf['items']), 0)
            for item in shelf['items']:
                self._validate_item(item)

    def _validate_track(self, track, expected_album=None, expected_artist=None):
        self.assertIsInstance(track, dict)
        for field in (
            'id', 'catalogId', 'title', 'artist', 'album',
            'trackNumber', 'discNumber', 'durationMs', 'durationLabel',
            'explicit', 'index', 'type'
        ):
            self.assertIn(field, track, f'Track missing field: {field}')
        self.assertEqual(track['type'], 'library-songs')  # the API's, as the sync keeps it

        self.assertTrue(isinstance(track['id'], str) and track['id'])
        self.assertTrue(isinstance(track['title'], str) and track['title'])
        self.assertTrue(isinstance(track['artist'], str) and track['artist'])
        self.assertTrue(isinstance(track['album'], str) and track['album'])
        self.assertIsInstance(track['trackNumber'], int)
        self.assertGreaterEqual(track['trackNumber'], 1)
        self.assertIsInstance(track['discNumber'], int)
        self.assertGreaterEqual(track['discNumber'], 1)
        self.assertIsInstance(track['durationMs'], int)
        self.assertGreater(track['durationMs'], 0)
        self.assertIsInstance(track['durationLabel'], str)
        self.assertRegex(track['durationLabel'], r'^\d+:\d{2}$')
        self.assertIsInstance(track['explicit'], bool)
        self.assertIsInstance(track['index'], int)
        self.assertGreaterEqual(track['index'], 0)

        if expected_album:
            self.assertEqual(track['album'], expected_album)
        if expected_artist:
            self.assertEqual(track['artist'], expected_artist)

    def _validate_item(self, item):
        self.assertIsInstance(item, dict)
        for field in (
            'id', 'kind', 'title', 'subtitle', 'year', 'genre',
            'summary', 'art', 'thumb', 'artColor', 'explicit',
            'catalogId', 'url', 'play', 'groups'
        ):
            self.assertIn(field, item, f'Item missing field: {field}')
        # Counts, never words: the app makes those (library.Item.count_text).
        self.assertNotIn('countLabel', item)
        for field in COUNTS.get(item['kind'], ()):
            self.assertIsInstance(item[field], int, f'{item["kind"]} {field}')

        self.assertIn(item['kind'], {'album', 'playlist', 'artist', 'station', 'video'})
        self.assertTrue(isinstance(item['id'], str) and item['id'])
        self.assertTrue(isinstance(item['title'], str) and item['title'])
        self.assertIsInstance(item['subtitle'], str)
        self.assertIsInstance(item['explicit'], bool)

        # art path exists on disk
        if item['art'] is not None:
            self.assertTrue(os.path.isabs(item['art']), f"Art path not absolute: {item['art']}")
            self.assertTrue(os.path.exists(item['art']), f"Art file does not exist: {item['art']}")
            self.assertGreater(os.path.getsize(item['art']), 1000)
            # And its thumbnail beside it, under the same name.
            self.assertTrue(os.path.exists(item['thumb']),
                            f"Thumb file does not exist: {item['thumb']}")
            self.assertEqual(os.path.basename(item['thumb']), os.path.basename(item['art']))
            self.assertLess(os.path.getsize(item['thumb']), os.path.getsize(item['art']))

        # artColor is a hex string
        if item['artColor'] is not None:
            self.assertRegex(item['artColor'], r'^#[0-9a-fA-F]{6}$')

        # play object
        self.assertIsInstance(item['play'], dict)
        self.assertIn('kind', item['play'])
        self.assertIn('id', item['play'])

        # groups
        self.assertIsInstance(item['groups'], list)
        for group in item['groups']:
            self.assertIn('name', group)
            self.assertIn('play', group)
            self.assertIn('entries', group)
            self.assertIsInstance(group['entries'], list)
            for track in group['entries']:
                self._validate_track(track)

    def test_albums_detail(self):
        albums = self.data['sections']['albums']
        two_disc_found = False

        for alb in albums:
            self._validate_item(alb)
            self.assertEqual(alb['kind'], 'album')
            self.assertEqual(alb['play']['kind'], 'album')
            self.assertEqual(alb['play']['id'], alb['id'])
            self.assertIsInstance(alb['year'], int)
            self.assertGreaterEqual(alb['year'], 2000)
            self.assertIsInstance(alb['genre'], str)

            # Check groups and track indexing
            groups = alb['groups']
            self.assertIn(len(groups), (1, 2))
            if len(groups) == 2:
                two_disc_found = True
                self.assertEqual(groups[0]['name'], 'Disc 1')
                self.assertEqual(groups[1]['name'], 'Disc 2')

            # Check 0-based sequential indexing across all tracks in the album
            total_tracks = []
            for g in groups:
                total_tracks.extend(g['entries'])
            self.assertGreaterEqual(len(total_tracks), 8)
            self.assertLessEqual(len(total_tracks), 25)
            for expected_idx, track in enumerate(total_tracks):
                self.assertEqual(track['index'], expected_idx)
                self.assertEqual(track['album'], alb['title'])
            self.assertEqual(alb['trackCount'], len(total_tracks))
            self.assertEqual(alb['durationMs'], sum(t['durationMs'] for t in total_tracks))

        self.assertTrue(two_disc_found, 'Expected at least one 2-disc album')

    def test_artists_detail(self):
        artists = self.data['sections']['artists']
        for artist in artists:
            self._validate_item(artist)
            self.assertEqual(artist['kind'], 'artist')
            self.assertEqual(artist['play']['kind'], 'artist')
            self.assertEqual(artist['play']['id'], artist['id'])

            self.assertEqual(artist['subtitle'], '')  # no word of the data's
            # Groups represent albums
            self.assertGreater(len(artist['groups']), 0)
            self.assertEqual(artist['albumCount'], len(artist['groups']))
            for group in artist['groups']:
                self.assertTrue(group['name'])
                self.assertEqual(group['play']['kind'], 'album')
                self.assertGreater(len(group['entries']), 0)
                for track in group['entries']:
                    self.assertEqual(track['artist'], artist['title'])

    def test_playlists_detail(self):
        playlists = self.data['sections']['playlists']
        for pl in playlists:
            self._validate_item(pl)
            self.assertEqual(pl['kind'], 'playlist')
            self.assertEqual(pl['play']['kind'], 'playlist')
            self.assertEqual(pl['play']['id'], pl['id'])
            self.assertEqual(len(pl['groups']), 1)
            group = pl['groups'][0]
            self.assertEqual(group['name'], 'Tracks')
            self.assertGreater(len(group['entries']), 0)
            for idx, track in enumerate(group['entries']):
                self.assertEqual(track['index'], idx)
            self.assertEqual(pl['trackCount'], len(group['entries']))

    def test_one_favourites_playlist(self):
        # The flag the app looks for (applemusic.library.FAVOURITES) is on one playlist only, and
        # no other item carries attributes.
        flagged = [pl for pl in self.data['sections']['playlists']
                   if pl.get('attributes', {}).get('isFavourites') is True]
        self.assertEqual([pl['title'] for pl in flagged], ['Favourite Songs'])
        self.assertEqual(flagged[0]['attributes'], {'isFavourites': True})
        self.assertGreater(len(flagged[0]['groups'][0]['entries']), 20)
        others = [item for section in self.data['sections'].values() for item in section
                  if item is not flagged[0]]
        self.assertFalse([item['id'] for item in others if 'attributes' in item])

    def test_folders(self):
        # library.json's folders: the "root" entry lists the top level; three folders, one of
        # them inside another; every playlist in exactly one children list.
        folders = self.data['folders']
        by_id = {folder['id']: folder for folder in folders}
        self.assertEqual(len(by_id), len(folders))
        self.assertIsNone(by_id['root']['parent'])
        others = [folder for folder in folders if folder['id'] != 'root']
        self.assertEqual(len(others), 3)
        self.assertEqual(sorted(folder['parent'] != 'root' for folder in others),
                         [False, False, True])
        listed = []
        for folder in folders:
            self.assertEqual(set(folder), {'id', 'title', 'parent', 'children'})
            self.assertTrue(isinstance(folder['title'], str) and folder['title'])
            for child in folder['children']:
                self.assertEqual(set(child), {'kind', 'id'})
                self.assertIn(child['kind'], ('folder', 'playlist'))
                listed.append((child['kind'], child['id']))
                if child['kind'] == 'folder':
                    self.assertEqual(by_id[child['id']]['parent'], folder['id'])
        playlist_ids = [pl['id'] for pl in self.data['sections']['playlists']]
        self.assertEqual(sorted(listed), sorted([('folder', folder['id']) for folder in others]
                                                + [('playlist', pid) for pid in playlist_ids]))
        top = [child['kind'] for child in by_id['root']['children']]
        self.assertIn('playlist', top)  # loose playlists, not only folders

    def test_radio_stations_detail(self):
        radio = self.data['sections']['radio']
        for st in radio:
            self._validate_item(st)
            self.assertEqual(st['kind'], 'station')
            self.assertEqual(st['play']['kind'], 'station')
            self.assertEqual(st['play']['id'], st['id'])
            self.assertEqual(st['groups'], [])

    def test_music_videos_detail(self):
        # The Item shape the sync gives a library music video (normalize_item): a song's
        # fields, played as MusicKit's musicVideo, with 16:9 artwork.
        videos = self.data['sections']['videos']
        artists = {artist['title'] for artist in self.data['sections']['artists']}
        self.assertEqual(len({video['id'] for video in videos}), len(videos))
        for video in videos:
            self._validate_item(video)
            self.assertEqual(video['kind'], 'video')
            self.assertEqual(video['play'], {'kind': 'musicVideo', 'id': video['id']})
            self.assertEqual(video['groups'], [])
            self.assertIsNone(video['summary'])
            self.assertIn(video['subtitle'], artists)
            self.assertGreater(video['durationMs'], 0)
            _fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(video['art'])
            self.assertEqual((width, height), (config.COVER_SIZE, config.COVER_SIZE * 9 // 16))
            _fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(video['thumb'])
            self.assertEqual((width, height), (config.THUMB_SIZE, config.THUMB_SIZE * 9 // 16))

    def test_the_demo_has_the_normalisers_shapes(self):
        # Each kind of Item has the keys the real sync gives it (normalize), no more, no less,
        # apart from this app's own additions (library.json's `attributes`, `artUrl`).
        real = {
            'album': normalize.normalize_album({'id': 'l.a', 'attributes': {'name': 'A'}}),
            'playlist': normalize.normalize_playlist({'id': 'p.1', 'attributes': {'name': 'P'}}),
            'artist': normalize.normalize_artist({'id': 'l.r', 'attributes': {'name': 'R'}}),
            'station': normalize.normalize_station({'id': 'ra.1', 'attributes': {'name': 'S'}}),
            'video': normalize.normalize_item({'id': '1', 'type': 'library-music-videos',
                                               'attributes': {'name': 'V'}}),
        }
        additions = {'attributes', 'artUrl'}
        for name, items in self.data['sections'].items():
            if name == 'songs':
                continue  # Tracks, not Items
            for item in items:
                with self.subTest(section=name, id=item['id']):
                    self.assertEqual(set(item) - additions, set(real[item['kind']]))

    def test_artwork_at_config_sizes(self):
        with open(os.path.join(self.out_dir, 'art', '.sizes'), encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'cover': config.COVER_SIZE, 'thumb': config.THUMB_SIZE})
        album = self.data['sections']['albums'][0]
        _fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(album['art'])
        self.assertEqual((width, height), (config.COVER_SIZE, config.COVER_SIZE))
        _fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(album['thumb'])
        self.assertEqual((width, height), (config.THUMB_SIZE, config.THUMB_SIZE))

    def test_cli_execution(self):
        self.assertEqual(self.cli.returncode, 0, f'CLI failed: {self.cli.stderr}')
        self.assertIn('demo library: 40 albums', self.cli.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.out_dir, 'library.json')))
        self.assertTrue(os.path.isdir(os.path.join(self.out_dir, 'art')))


class TestGeneratedAlbums(unittest.TestCase):
    """--albums N: the hand-written albums, then generated ones (no drawing here)."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'demo_library', os.path.join(REPO_DIR, 'scripts', 'demo_library.py'))
        cls.demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.demo)

    def test_default_is_the_hand_written_library(self):
        artists, albums = self.demo.generated_albums(len(self.demo.ALBUMS_DATA))
        self.assertEqual(artists, self.demo.ARTISTS_DATA)
        self.assertEqual(albums, self.demo.ALBUMS_DATA)

    def test_generated_albums(self):
        artists, albums = self.demo.generated_albums(300)
        self.assertEqual(len(albums), 300)
        self.assertEqual(albums[:40], self.demo.ALBUMS_DATA)
        self.assertEqual(artists[:15], self.demo.ARTISTS_DATA)
        self.assertEqual(len({album['title'] for album in albums}), 300)
        self.assertEqual(len({artist['name'] for artist in artists}), len(artists))
        for album in albums:
            self.assertLess(album['artist_idx'], len(artists))
            self.assertTrue(album['discs'] and all(album['discs']))
        # Every generated artist has an album, and the same count gives the same library.
        self.assertEqual({album['artist_idx'] for album in albums}, set(range(len(artists))))
        self.assertEqual(self.demo.generated_albums(300), (artists, albums))

    def test_tracks_option_sizes_the_generated_albums(self):
        # --tracks N: the library holds N songs in all, the hand-written albums untouched.
        artists, albums = self.demo.generated_albums(300, tracks=4000)
        self.assertEqual(sum(len(disc) for album in albums for disc in album['discs']), 4000)
        self.assertEqual(albums[:40], self.demo.ALBUMS_DATA)
        self.assertEqual(len(albums), 300)
        generated = [len(album['discs'][0]) for album in albums[40:]]
        self.assertTrue(all(count >= 1 for count in generated))
        self.assertLessEqual(max(generated) - min(generated), 10)
        with self.assertRaises(ValueError):
            self.demo.generated_albums(300, tracks=100)

    def test_generated_song_titles_are_distinct(self):
        # As a real library's nearly all are, so collating them costs what it would; the
        # generated ones differ from the hand-written ones too.
        _artists, albums = self.demo.generated_albums(1000, tracks=12000)
        written = {song for album in albums[:40] for disc in album['discs'] for song in disc}
        generated = [song for album in albums[40:] for disc in album['discs'] for song in disc]
        self.assertEqual(len(generated), 12000 - sum(
            len(disc) for album in albums[:40] for disc in album['discs']))
        self.assertEqual(len(set(generated)), len(generated))
        self.assertFalse(written & set(generated))

    def test_generated_playlists(self):
        # --playlists N: the hand-written playlists, then invented ones, N with Favourite
        # Songs counted; the same N gives the same playlists.
        playlists = self.demo.generated_playlists(50)
        self.assertEqual(len(playlists), 49)
        self.assertEqual(playlists[:12], self.demo.PLAYLISTS_DATA)
        self.assertEqual(len({playlist['title'] for playlist in playlists}), 49)
        genres = {artist['genre'] for artist in self.demo.ARTISTS_DATA}
        for playlist in playlists[12:]:
            self.assertEqual(set(playlist),
                             {'title', 'subtitle', 'genre', 'summary', 'filter_genres'})
            self.assertIn(playlist['genre'], genres)
            self.assertTrue(set(playlist['filter_genres']) <= genres)
            self.assertIn(playlist['genre'], playlist['filter_genres'])
        self.assertEqual(self.demo.generated_playlists(50), playlists)
        self.assertEqual(self.demo.generated_playlists(13), self.demo.PLAYLISTS_DATA)


if __name__ == '__main__':
    unittest.main()
