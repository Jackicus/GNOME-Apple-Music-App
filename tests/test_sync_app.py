# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The app's sync (src/sync.py) against a fake engine answering with invented fixture pages, and
Library.reload() keeping objects in place.

No network: the artwork fetchers are replaced by ones writing a byte or two. No display: the
Library is GObject only, and the sync runs under asyncio.
"""

import asyncio
import json
import os
import pathlib
import tempfile
import unittest
from datetime import UTC, datetime
from unittest import mock

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from applemusic import sync as app_sync
from applemusic.backend import config
from applemusic.backend import normalize as backend
from applemusic.backend.errors import EngineError
from applemusic.engine import Engine
from applemusic.library import EDITABLE, FAVOURITES, ROOT_FOLDER, Library

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'


def fixture(name):
    with open(FIXTURES / name, encoding='utf-8') as file:
        return json.load(file)


class FakeEngine:
    """What sync_library() asks of the engine: status(), api(), api_pages() and api_all();
    and start(), which LibrarySync awaits first.

    `answers` maps a path to an answer dict, to a function of the params (for a paged
    endpoint answering by offset), or to an EngineError to raise (status 404 for Apple's
    "there is none"). A path with no answer is an api error without a status: a failure.
    api_pages() is the Engine's own, over this api().
    """

    api_pages = Engine.api_pages

    def __init__(self, answers, authorized=True):
        self.answers = answers
        self.authorized = authorized
        self.calls = []
        self.state = 'up'

    async def start(self, visible=None):
        """Running already: nothing to do, as the Engine's start() says."""

    async def status(self):
        return {'ready': True, 'engine': True, 'authorized': self.authorized,
                'storefront': 'gb', 'bitrate': 256}

    async def api(self, path, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((path, params))
        answer = self.answers.get(path)
        if answer is None:
            raise EngineError('api', f'invented failure: {path}')
        if isinstance(answer, EngineError):
            raise answer
        if callable(answer):
            answer = answer(params)
        return json.loads(json.dumps(answer))  # a copy, as Chrome's answer would be

    async def api_all(self, paths, timeout=60):
        return [self.answers.get(path) for path in paths]


def one_page(answer):
    """A paged endpoint with one page: the answer at offset 0, nothing after."""
    def page(params):
        if params.get('offset'):
            return {'data': []}
        return answer
    return page


def answers():
    """The fake engine's answers: the fixtures wired to the endpoints the sync asks for."""
    folders = fixture('playlist_folders.json')
    tracks = fixture('playlist_tracks.json')
    playlists = fixture('library_playlists_tags.json')
    wired = {
        app_sync.SONGS_ENDPOINT: fixture('library_songs_albums.json'),
        app_sync.PLAYLISTS_ENDPOINT: playlists,
        app_sync.VIDEOS_ENDPOINT: fixture('library_music_videos.json'),
        app_sync.RADIO_ENDPOINT: fixture('radio_stations.json'),
        app_sync.RECOMMENDATIONS_ENDPOINT: fixture('recommendations.json'),
        '/v1/me/history/heavy-rotation': fixture('heavy_rotation.json'),
        '/v1/me/library/recently-added': one_page(fixture('recently_added.json')),
    }
    for apple_id, children in folders.items():
        wired[f'{app_sync.FOLDERS_ENDPOINT}/{apple_id}/children'] = children
    for playlist in playlists['data']:
        wired[f"{app_sync.PLAYLISTS_ENDPOINT}/{playlist['id']}/tracks"] = tracks
    return wired


class SyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = self.tmp.name
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.cache})
        patcher.start()
        self.addCleanup(patcher.stop)
        # No network: a thumbnail is a couple of bytes on disk, and a cover must never be asked
        # for by a sync (the pages fetch covers).
        self.fetched = []

        def fake_thumb(url, cache_dir, dest_path, generation=None):
            self.fetched.append(url)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, 'wb') as file:
                file.write(b'img')
            return dest_path

        def no_cover(url, cache_dir, timeout=10.0, dest_path=None, generation=None):
            self.fail(f'a sync fetched a cover: {url}')

        for name, fake in (('cache_thumbnail', fake_thumb), ('cache_artwork', no_cover)):
            patcher = mock.patch.object(backend, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.library = Library()
        self.progress = []

    def report(self, section, done, total):
        self.progress.append((section, done, total))

    def read_json(self):
        with open(os.path.join(self.cache, 'library.json'), encoding='utf-8') as file:
            return json.load(file)

    async def test_sync_writes_the_library_and_loads_it(self):
        engine = FakeEngine(answers())
        counts = await app_sync.sync_library(engine, self.library, self.report)
        data = self.read_json()
        self.assertEqual(data['version'], app_sync.LIBRARY_VERSION)
        self.assertEqual(data['storefront'], 'gb')
        self.assertEqual(set(data['sections']), {'albums', 'artists', 'playlists', 'videos',
                                                 'radio'})
        sections = data['sections']
        # Albums and artists from the songs: one real album, and a stand-in holding the song
        # with no album (a loose song), which plays that song.
        self.assertEqual(len(sections['albums']), 2)
        stand_in = next(a for a in sections['albums'] if a['id'] != 'l.alb123')
        self.assertEqual([entry['id'] for entry in stand_in['groups'][0]['entries']],
                         ['i.def456'])
        # Playlists, with Favourite Songs flagged as the demo flags it, from Apple's tag, and
        # as not editable (canEdit false); the editable ones carry no attributes.
        flagged = [p['id'] for p in sections['playlists'] if p.get('attributes')]
        self.assertEqual(flagged, ['p.favs0'])
        self.assertEqual(next(p for p in sections['playlists'] if p['id'] == 'p.favs0')
                         ['attributes'], {FAVOURITES: True, EDITABLE: False})
        self.assertTrue(all(p['groups'][0]['entries'] for p in sections['playlists']))
        # Videos are Items of kind video, played as MusicKit's musicVideo.
        self.assertEqual([(v['kind'], v['play']['kind']) for v in sections['videos']],
                         [('video', 'musicVideo')] * 2)
        self.assertEqual(len(sections['radio']), 2)
        # The folders: Apple's root is "root", nesting and order as Apple's children lists.
        folders = {f['id']: f for f in data['folders']}
        self.assertEqual(list(folders), [ROOT_FOLDER, 'p.fldA1', 'p.fldB2'])
        self.assertEqual(folders[ROOT_FOLDER]['parent'], None)
        self.assertEqual(folders[ROOT_FOLDER]['children'], [
            {'kind': 'playlist', 'id': 'p.pl123'}, {'kind': 'folder', 'id': 'p.fldA1'},
            {'kind': 'playlist', 'id': 'p.favs0'}])
        self.assertEqual((folders['p.fldA1']['parent'], folders['p.fldA1']['title']),
                         (ROOT_FOLDER, 'Workouts'))
        self.assertEqual(folders['p.fldA1']['children'], [
            {'kind': 'folder', 'id': 'p.fldB2'}, {'kind': 'playlist', 'id': 'p.pl456'}])
        self.assertEqual(folders['p.fldB2']['parent'], 'p.fldA1')
        # The shelves: Apple's recommendations first, then the fixed two (Recently Added
        # paged by `next`: the second page was empty).
        keys = [shelf['key'] for shelf in data['shelves']]
        self.assertTrue(keys[0].startswith('rec-'))
        self.assertEqual(keys[-2:], ['heavy-rotation', 'recently-added'])
        self.assertEqual([shelf['title'] for shelf in data['shelves'][-2:]], ['', ''])
        self.assertEqual(len(data['shelves'][-1]['items']), 2)
        # Every item with artwork knows its cover's URL; only thumbnails were fetched, at the
        # thumbnail size, and the sizes marker says what the cache is built at.
        album = next(a for a in sections['albums'] if a['id'] == 'l.alb123')
        self.assertTrue(album['art'].startswith(os.path.join(self.cache, 'art')))
        self.assertEqual(os.path.basename(album['art']), backend.artwork_filename(album['artUrl']))
        self.assertIn(f'{config.COVER_SIZE}x{config.COVER_SIZE}', album['artUrl'])
        self.assertTrue(os.path.exists(album['thumb']))
        self.assertFalse(os.path.exists(album['art']))
        self.assertTrue(all(f'{config.THUMB_SIZE}x{config.THUMB_SIZE}' in url
                            for url in self.fetched))
        with open(os.path.join(self.cache, 'art', '.sizes'), encoding='utf-8') as file:
            self.assertEqual(json.load(file), {'cover': config.COVER_SIZE,
                                               'thumb': config.THUMB_SIZE})
        # The counts, and the progress in order.
        self.assertEqual((counts['albums'], counts['artists'], counts['playlists'],
                          counts['loose'], counts['videos'], counts['radio'],
                          counts['folders']), (2, 1, 4, 1, 2, 2, 2))
        self.assertEqual(counts['art']['failed'], 0)
        self.assertGreater(counts['art']['fetched'], 0)
        first = []
        for section, _done, _total in self.progress:
            if section not in first:
                first.append(section)
        self.assertEqual(first, list(app_sync.PROGRESS_SECTIONS))
        self.assertIn(('songs', 2, 2), self.progress)
        self.assertIn(('playlists', 4, 4), self.progress)
        self.assertEqual(self.progress[-1][0], 'artwork')
        # The library followed.
        self.assertEqual(self.library.state, 'ready')
        self.assertEqual(self.library.albums.get_n_items(), 2)
        self.assertEqual(self.library.videos.get_n_items(), 2)
        self.assertEqual(self.library.favourite_songs().id, 'p.favs0')
        self.assertEqual([node.id for node in self.library.playlist_tree().folders()],
                         ['p.fldA1', 'p.fldB2'])
        await self.library.build_songs()
        # The loose song is in the Songs store once, as its stand-in album's track.
        self.assertEqual(self.library.song_count(), 2)
        self.assertEqual(sorted(track.id for track in self.library.songs),
                         ['i.abc123', 'i.def456'])

    async def test_not_signed_in_writes_nothing(self):
        engine = FakeEngine(answers(), authorized=False)
        with self.assertRaises(EngineError) as ctx:
            await app_sync.sync_library(engine, self.library, self.report)
        self.assertEqual(ctx.exception.code, 'not-signed-in')
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'library.json')))
        self.assertEqual(engine.calls, [])

    async def test_a_failed_section_keeps_last_times(self):
        await app_sync.sync_library(FakeEngine(answers()), self.library)
        before = self.read_json()
        wired = answers()
        del wired[app_sync.RADIO_ENDPOINT]
        del wired[f'{app_sync.PLAYLISTS_ENDPOINT}/p.pl456/tracks']
        del wired[f'{app_sync.FOLDERS_ENDPOINT}/p.fldB2/children']
        with self.assertLogs('applemusic.sync', 'WARNING'):
            await app_sync.sync_library(FakeEngine(wired), self.library)
        after = self.read_json()
        self.assertEqual(after['sections']['radio'], before['sections']['radio'])
        self.assertEqual(after['folders'], before['folders'])
        kept = next(p for p in after['sections']['playlists'] if p['id'] == 'p.pl456')
        self.assertEqual(kept, next(p for p in before['sections']['playlists']
                                    if p['id'] == 'p.pl456'))
        self.assertTrue(kept['groups'][0]['entries'])

    async def test_an_engine_failure_stops_the_sync(self):
        wired = answers()
        del wired[app_sync.SONGS_ENDPOINT]
        with self.assertRaises(EngineError):
            await app_sync.sync_library(FakeEngine(wired), self.library)
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'library.json')))

    def playlist(self, data, playlist_id):
        return next(p for p in data['sections']['playlists'] if p['id'] == playlist_id)

    async def test_a_404_for_a_playlists_tracks_is_an_empty_playlist(self):
        wired = answers()
        path = f'{app_sync.PLAYLISTS_ENDPOINT}/p.pl123/tracks'
        wired[path] = EngineError('api', 'Not Found', status=404)
        engine = FakeEngine(wired)
        await app_sync.sync_library(engine, self.library)
        chill = self.playlist(self.read_json(), 'p.pl123')
        self.assertEqual([group['entries'] for group in chill['groups']], [[]])
        self.assertEqual(chill['trackCount'], 0)
        self.assertEqual([call for call, _params in engine.calls].count(path), 1)

    async def test_failed_tracks_on_a_first_sync_are_left_to_the_page(self):
        wired = answers()
        del wired[f'{app_sync.PLAYLISTS_ENDPOINT}/p.pl123/tracks']
        with self.assertLogs('applemusic.sync', 'WARNING'):
            await app_sync.sync_library(FakeEngine(wired), self.library)
        chill = self.playlist(self.read_json(), 'p.pl123')
        self.assertEqual((chill['title'], chill['groups']), ('Chill Mix', []))
        self.assertNotIn('trackCount', chill)  # not "0 songs": not known
        self.assertEqual(self.library.by_id('playlist', 'p.pl123').count_label, '')

    async def test_failed_tracks_keep_last_times_under_the_new_title(self):
        await app_sync.sync_library(FakeEngine(answers()), self.library)
        before = self.playlist(self.read_json(), 'p.pl456')
        wired = answers()
        playlists = fixture('library_playlists_tags.json')
        playlists['data'][1]['attributes']['name'] = 'Rock Workout II'
        wired[app_sync.PLAYLISTS_ENDPOINT] = playlists
        del wired[f'{app_sync.PLAYLISTS_ENDPOINT}/p.pl456/tracks']
        with self.assertLogs('applemusic.sync', 'WARNING'):
            await app_sync.sync_library(FakeEngine(wired), self.library)
        after = self.playlist(self.read_json(), 'p.pl456')
        self.assertEqual(after['title'], 'Rock Workout II')
        self.assertEqual(after['groups'], before['groups'])
        self.assertEqual((after['trackCount'], after['durationMs']),
                         (before['trackCount'], before['durationMs']))

    async def test_a_folder_listing_its_own_ancestor_ends(self):
        wired = answers()
        folders = fixture('playlist_folders.json')
        folders['p.fldB2']['data'].append({'id': 'p.fldA1', 'type': app_sync.FOLDER_TYPE,
                                           'attributes': {'name': 'Workouts'}})
        wired[f'{app_sync.FOLDERS_ENDPOINT}/p.fldB2/children'] = folders['p.fldB2']
        with self.assertLogs('applemusic.sync', 'WARNING'):
            counts = await app_sync.sync_library(FakeEngine(wired), self.library)
        data = self.read_json()
        self.assertEqual([folder['id'] for folder in data['folders']],
                         [ROOT_FOLDER, 'p.fldA1', 'p.fldB2'])
        self.assertEqual(counts['folders'], 2)
        inner = next(folder for folder in data['folders'] if folder['id'] == 'p.fldB2')
        self.assertEqual(inner['children'], [{'kind': 'playlist', 'id': 'p.pl789'}])

    async def test_a_404_for_a_folders_children_is_an_empty_folder(self):
        wired = answers()
        wired[f'{app_sync.FOLDERS_ENDPOINT}/p.fldB2/children'] = EngineError(
            'api', 'Not Found', status=404)
        await app_sync.sync_library(FakeEngine(wired), self.library)
        inner = next(folder for folder in self.read_json()['folders']
                     if folder['id'] == 'p.fldB2')
        self.assertEqual(inner['children'], [])

    async def test_a_song_listed_twice_is_kept_once(self):
        wired = answers()
        songs = fixture('library_songs_albums.json')
        songs['data'].append(songs['data'][0])  # the library changed under the read
        wired[app_sync.SONGS_ENDPOINT] = songs
        await app_sync.sync_library(FakeEngine(wired), self.library)
        album = next(a for a in self.read_json()['sections']['albums'] if a['id'] == 'l.alb123')
        entries = [entry['id'] for group in album['groups'] for entry in group['entries']]
        self.assertEqual(len(entries), len(set(entries)))

    async def test_a_failed_sync_keeps_the_thumbnails_after_a_size_change(self):
        thumb = pathlib.Path(self.cache, 'thumb', 'old.jpg')
        marker = pathlib.Path(self.cache, 'art', '.sizes')
        for path in (thumb, marker):
            path.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b'img')
        marker.write_text(json.dumps({'cover': config.COVER_SIZE, 'thumb': 256}))
        wired = answers()
        del wired[app_sync.SONGS_ENDPOINT]
        with self.assertRaises(EngineError):
            await app_sync.sync_library(FakeEngine(wired), self.library)
        self.assertTrue(thumb.exists())
        self.assertEqual(json.loads(marker.read_text())['thumb'], 256)

    async def test_a_second_sync_keeps_objects_and_drops_what_is_gone(self):
        wired = answers()
        await app_sync.sync_library(FakeEngine(wired), self.library)
        await self.library.build_songs()
        album = self.library.by_id('album', 'l.alb123')
        track = album.groups[0].entries.get_item(0)
        chill = self.library.by_id('playlist', 'p.pl123')
        gone = self.library.by_id('playlist', 'p.pl789')
        root = self.library.by_id('folder', ROOT_FOLDER)
        shelf = self.library.shelf('heavy-rotation')
        songs = [self.library.songs.get_item(n) for n in range(self.library.songs.get_n_items())]
        self.assertIsNotNone(gone)
        changed = []
        self.library.connect('changed', lambda _l: changed.append(True))
        titles = []
        chill.connect('notify::title', lambda item, _p: titles.append(item.title))
        # Next time: one playlist renamed, one gone (from the listing and its folder).
        playlists = fixture('library_playlists_tags.json')
        playlists['data'][0]['attributes']['name'] = 'Chill Mix II'
        del playlists['data'][2]
        wired[app_sync.PLAYLISTS_ENDPOINT] = playlists
        folders = fixture('playlist_folders.json')
        folders['p.fldB2'] = {'data': [], 'meta': {'total': 0}}
        wired[f'{app_sync.FOLDERS_ENDPOINT}/p.fldB2/children'] = folders['p.fldB2']
        await app_sync.sync_library(FakeEngine(wired), self.library)
        self.assertEqual(len(changed), 1)
        self.assertIs(self.library.by_id('album', 'l.alb123'), album)
        self.assertIs(album.groups[0].entries.get_item(0), track)
        self.assertIs(self.library.by_id('playlist', 'p.pl123'), chill)
        self.assertEqual((chill.title, titles), ('Chill Mix II', ['Chill Mix II']))
        self.assertIsNone(self.library.by_id('playlist', 'p.pl789'))
        self.assertNotIn(gone, list(self.library.playlists))
        self.assertEqual(self.library.playlists.get_n_items(), 3)
        self.assertIs(self.library.by_id('folder', ROOT_FOLDER), root)
        self.assertIs(self.library.shelf('heavy-rotation'), shelf)
        self.assertEqual([self.library.songs.get_item(n)
                          for n in range(self.library.songs.get_n_items())], songs)
        self.assertEqual(self.library.playlist_tree().folder('p.fldB2').children, [])


def album_dict(album_id, title, track_ids, thumb=None):
    play = {'kind': 'album', 'id': album_id}
    entries = [{'id': track_id, 'catalogId': None, 'title': f'Song {track_id}',
                'artist': 'Test Artist', 'album': title, 'trackNumber': n + 1,
                'discNumber': 1, 'durationMs': 180000, 'durationLabel': '3:00',
                'explicit': False, 'index': n, 'thumb': None}
               for n, track_id in enumerate(track_ids)]
    return {'id': album_id, 'kind': 'album', 'title': title, 'subtitle': 'Test Artist',
            'year': 2001, 'genre': None, 'summary': None, 'art': None, 'thumb': thumb,
            'artColor': None, 'countLabel': f'{len(track_ids)} songs', 'explicit': False,
            'catalogId': None, 'url': None, 'play': play,
            'groups': [{'name': 'Disc 1', 'play': play, 'entries': entries}]}


class ReloadTest(unittest.TestCase):
    """Library.reload() over invented library.json files: the diff keeps objects in place."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.library = Library()
        self.library.batch_size = 2

    def write(self, albums, shelves=(), songs=None):
        data = {'version': 1, 'generated': '2026-01-01T00:00:00Z', 'storefront': 'gb',
                'sections': {'albums': albums}, 'shelves': list(shelves)}
        if songs is not None:
            data['sections']['songs'] = songs
        pathlib.Path(self.tmp.name, 'library.json').write_text(json.dumps(data), 'utf-8')

    def items(self, store):
        return [store.get_item(n) for n in range(store.get_n_items())]

    def test_reload_diffs_by_id(self):
        albums = [album_dict(f'l.a{n}', f'A{n}', [f'i.{n}']) for n in range(6)]
        self.write(albums, shelves=[{'key': 'recently-added', 'title': 'Recently Added',
                                     'items': [albums[3], albums[1]]}])
        asyncio.run(self.library.load())
        asyncio.run(self.library.build_songs())
        before = self.items(self.library.albums)
        tracks = self.items(self.library.songs)
        shelf = self.library.shelf('recently-added')
        events = []
        self.library.albums.connect('items-changed',
                                    lambda _s, pos, removed, added: events.append(
                                        (pos, removed, added)))
        # Next time: l.a1 renamed (its tracks the same), l.a2 gone, l.a9 new before l.a4, and
        # l.a5's tracks changed.
        albums[1]['title'] = 'A1 renamed'
        del albums[2]
        albums.insert(3, album_dict('l.a9', 'A9', ['i.9']))
        albums[-1] = album_dict('l.a5', 'A5', ['i.5', 'i.55'])
        self.write(albums, shelves=[{'key': 'recently-added', 'title': 'Recently Added',
                                     'items': [albums[3], albums[1]]}])
        asyncio.run(self.library.reload())
        after = self.items(self.library.albums)
        self.assertEqual([item.id for item in after], ['l.a0', 'l.a1', 'l.a3', 'l.a9', 'l.a4',
                                                       'l.a5'])
        for position in (0, 1, 2, 4, 5):
            self.assertIs(after[position], before[int(after[position].id[3:])])
        self.assertEqual(after[1].title, 'A1 renamed')
        self.assertIs(after[1].groups[0].entries.get_item(0), tracks[1])
        self.assertEqual([t.id for t in after[5].groups[0].entries], ['i.5', 'i.55'])
        self.assertIsNot(after[5].groups[0].entries.get_item(0), tracks[5])
        # The diff's two splices (the insertion at its old position, applied first, then the
        # removal), and the renamed item spliced over itself so that sorted views place it
        # again (a view's rows follow their Item's notify signals, not this splice); nothing
        # for the unchanged runs or for l.a5, whose title stayed (tests/test_reload.py).
        self.assertEqual(sorted(events), [(1, 1, 1), (2, 1, 0), (4, 0, 1)])
        self.assertIsNone(self.library.by_id('album', 'l.a2'))
        self.assertIs(self.library.shelf('recently-added'), shelf)
        self.assertEqual([item.id for item in shelf.items], ['l.a9', 'l.a1'])
        songs = self.items(self.library.songs)
        self.assertEqual([t.id for t in songs], ['i.0', 'i.1', 'i.3', 'i.9', 'i.4', 'i.5',
                                                 'i.55'])
        self.assertIs(songs[1], tracks[1])

    def test_reload_of_the_same_file_changes_nothing(self):
        albums = [album_dict(f'l.a{n}', f'A{n}', [f'i.{n}']) for n in range(3)]
        self.write(albums, songs=[album_dict('x', 'x', ['i.loose'])['groups'][0]['entries'][0]])
        asyncio.run(self.library.load())
        asyncio.run(self.library.build_songs())
        before = self.items(self.library.albums)
        tracks = self.items(self.library.songs)
        self.assertEqual([t.id for t in tracks], ['i.0', 'i.1', 'i.2', 'i.loose'])
        events = []
        for store in (self.library.albums, self.library.songs):
            store.connect('items-changed', lambda *args: events.append(args[1:]))
        asyncio.run(self.library.reload())
        self.assertEqual(events, [])
        self.assertEqual(self.items(self.library.albums), before)
        self.assertEqual(self.items(self.library.songs), tracks)
        self.assertEqual(self.library.song_count(), 4)
        self.assertEqual(self.library.state, 'ready')

    def test_load_after_reload_makes_new_objects(self):
        self.write([album_dict('l.a0', 'A0', ['i.0'])])
        asyncio.run(self.library.load())
        first = self.library.albums.get_item(0)
        asyncio.run(self.library.reload())
        self.assertIs(self.library.albums.get_item(0), first)
        asyncio.run(self.library.load())
        self.assertIsNot(self.library.albums.get_item(0), first)


class WhenToSyncTest(unittest.TestCase):
    """The sync-interval choices, sync_due() and last_sync_text() (untranslated here)."""

    NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

    def test_interval_index(self):
        self.assertEqual([app_sync.interval_index(hours) for hours in app_sync.INTERVALS],
                         [0, 1, 2, 3])
        # Values the key takes but Preferences does not offer: the nearest automatic one.
        self.assertEqual(app_sync.interval_index(2), 0)
        self.assertEqual(app_sync.interval_index(4), 1)
        self.assertEqual(app_sync.interval_index(12), 1)
        self.assertEqual(app_sync.interval_index(20), 2)
        self.assertEqual(app_sync.interval_index(168), 2)
        self.assertEqual(app_sync.interval_index(-1), 3)

    def test_sync_due(self):
        due = app_sync.sync_due
        self.assertTrue(due('', 6, self.NOW))  # never synced
        self.assertTrue(due('not a time', 6, self.NOW))
        self.assertFalse(due('', 0, self.NOW))  # manual: only when asked
        self.assertFalse(due('2026-09-28T07:00:00+00:00', 6, self.NOW))
        self.assertTrue(due('2026-09-28T05:59:00+00:00', 6, self.NOW))
        self.assertTrue(due('2026-09-28T10:59:00', 1, self.NOW))  # no zone: UTC
        self.assertFalse(due('2026-09-27T13:00:00+00:00', 24, self.NOW))

    def test_last_sync_text(self):
        text = app_sync.last_sync_text
        self.assertEqual(text('', self.NOW), 'Not refreshed yet')
        self.assertEqual(text('2026-09-28T11:59:40+00:00', self.NOW), 'Last refreshed just now')
        self.assertEqual(text('2026-09-28T11:59:00+00:00', self.NOW),
                         'Last refreshed 1 minute ago')
        self.assertEqual(text('2026-09-28T11:15:00+00:00', self.NOW),
                         'Last refreshed 45 minutes ago')
        self.assertEqual(text('2026-09-28T09:00:00+00:00', self.NOW),
                         'Last refreshed 3 hours ago')
        self.assertEqual(text('2026-09-27T11:00:00+00:00', self.NOW),
                         'Last refreshed 1 day ago')
        self.assertEqual(text('2026-09-28T13:00:00+01:00', self.NOW),
                         'Last refreshed just now')  # another zone, the same instant


if __name__ == '__main__':
    unittest.main()
