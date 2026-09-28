import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from tests import SRC  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend import config, normalize

# The sizes these tests were written against (the extension's defaults). The
# app's own defaults come from config; TestArtSizes covers those.
UPSTREAM_SIZES = {'cover': 512, 'thumb': 256}

try:
    import gi
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf
except (ImportError, ValueError):  # pragma: no cover - depends on the system
    GdkPixbuf = None


def pixbuf_scaler(src_path, dest_path, size):
    """A normalize.scale_image, as the app installs one."""
    scaled = GdkPixbuf.Pixbuf.new_from_file_at_scale(src_path, size, size, True)
    scaled.savev(dest_path, 'jpeg', ['quality'], ['90'])


class TestSync(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        normalize.ART_SIZES.update(UPSTREAM_SIZES)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)
        normalize.ART_SIZES.update(normalize.DEFAULT_ART_SIZES)

    def test_format_duration(self):
        self.assertEqual(normalize.format_duration(0), '0:00')
        self.assertEqual(normalize.format_duration(-10), '0:00')
        self.assertEqual(normalize.format_duration(45000), '0:45')
        self.assertEqual(normalize.format_duration(216000), '3:36')
        self.assertEqual(normalize.format_duration(3600000), '1:00:00')
        self.assertEqual(normalize.format_duration(3661000), '1:01:01')

    def test_format_color(self):
        self.assertIsNone(normalize.format_color(None))
        self.assertIsNone(normalize.format_color(''))
        self.assertEqual(normalize.format_color('1a1a1a'), '#1a1a1a')
        self.assertEqual(normalize.format_color('#1a1a1a'), '#1a1a1a')

    def test_strip_html(self):
        self.assertIsNone(normalize.strip_html(None))
        self.assertIsNone(normalize.strip_html(''))
        self.assertEqual(normalize.strip_html('<p>Hello <b>World</b>&amp;Friends</p>'),
                         'Hello World&Friends')
        # Line breaks and paragraphs keep their words apart.
        self.assertEqual(normalize.strip_html(
            'First paragraph.<br><br>Second.<p>Third</p><p class="x">Fourth</p>'),
            'First paragraph.\n\nSecond.\n\nThird\n\nFourth')
        self.assertEqual(normalize.strip_html('One<br/>two <BR> three'), 'One\ntwo\nthree')
        self.assertIsNone(normalize.strip_html('<p></p><br>'))

    def test_artwork_url_and_path(self):
        art_obj = {'url': 'https://example.com/art/{w}x{h}bb.{f}', 'bgColor': '222222'}
        formatted_url = normalize.format_artwork_url(art_obj, 512, 512)
        self.assertEqual(formatted_url, 'https://example.com/art/512x512bb.jpg')

        path = normalize.artwork_cache_path(formatted_url, self.tmp_dir)
        self.assertTrue(path.endswith('.jpg'))
        self.assertTrue(path.startswith(os.path.join(self.tmp_dir, 'art')))

    def test_normalize_track(self):
        raw = {
            'id': '1724040711',
            'type': 'songs',
            'attributes': {
                'name': 'Heroes Get Remembered',
                'artistName': 'Paper Parachutes',
                'albumName': 'Static Skyline',
                'trackNumber': 3,
                'discNumber': 1,
                'durationInMillis': 216000,
                'contentRating': 'explicit',
                'playParams': {'catalogId': '1724040711'},
            },
        }
        track = normalize.normalize_track(raw, index=2)
        self.assertEqual(track['id'], '1724040711')
        self.assertEqual(track['catalogId'], '1724040711')
        self.assertEqual(track['title'], 'Heroes Get Remembered')
        self.assertEqual(track['artist'], 'Paper Parachutes')
        self.assertEqual(track['album'], 'Static Skyline')
        self.assertEqual(track['trackNumber'], 3)
        self.assertEqual(track['discNumber'], 1)
        self.assertEqual(track['durationMs'], 216000)
        self.assertEqual(track['durationLabel'], '3:36')
        self.assertTrue(track['explicit'])
        self.assertEqual(track['index'], 2)
        self.assertIsNone(track['thumb'])
        self.assertEqual(track['type'], 'songs')

    def test_a_track_keeps_its_api_type(self):
        video = normalize.normalize_track({'id': 'i.video1', 'type': 'library-music-videos',
                                           'attributes': {'name': 'Harbour Lights (Live)'}})
        self.assertEqual(video['type'], 'library-music-videos')
        self.assertEqual(normalize.normalize_track({'id': 'i.x', 'attributes': {}})['type'], '')
        # In a playlist, each row keeps its own.
        playlist = normalize.normalize_playlist(
            {'id': 'p.1', 'attributes': {'name': 'Mixed'}},
            tracks=[{'id': 'i.s', 'type': 'library-songs', 'attributes': {'name': 'S'}},
                    {'id': 'i.v', 'type': 'library-music-videos', 'attributes': {'name': 'V'}}])
        self.assertEqual([entry['type'] for entry in playlist['groups'][0]['entries']],
                         ['library-songs', 'library-music-videos'])

    def test_normalize_track_names_thumbnail_when_asked(self):
        raw = {
            'id': 'i.one',
            'attributes': {'name': 'One', 'artwork': {'url': 'https://x/{w}x{h}bb.jpg'}},
        }
        art_urls = {}
        track = normalize.normalize_track(raw, index=0, cache_dir=self.tmp_dir, art_urls=art_urls)
        self.assertTrue(track['thumb'].startswith(os.path.join(self.tmp_dir, 'thumb')))
        self.assertEqual(art_urls[track['thumb']], 'https://x/256x256bb.jpg')
        # The same cover's full-size file has the same name, one folder over.
        self.assertEqual(os.path.basename(track['thumb']),
                         os.path.basename(normalize.artwork_cache_path('https://x/512x512bb.jpg',
                                                                       self.tmp_dir)))

    def test_normalize_album(self):
        raw_album = {
            'id': 'l.alb123',
            'type': 'library-albums',
            'attributes': {
                'name': 'Static Skyline',
                'artistName': 'Paper Parachutes',
                'releaseDate': '2007-09-18',
                'genreNames': ['Rock'],
                'contentRating': 'explicit',
                'trackCount': 2,
                'artwork': {'url': 'https://example.com/{w}x{h}.{f}', 'bgColor': '112233'},
                'editorialNotes': {'standard': '<p>A classic easycore record.</p>'},
            },
        }
        raw_tracks = [
            {
                'id': 't2',
                'attributes': {
                    'name': 'Track Two',
                    'artistName': 'Paper Parachutes',
                    'trackNumber': 2,
                    'discNumber': 1,
                    'durationInMillis': 180000,
                },
            },
            {
                'id': 't1',
                'attributes': {
                    'name': 'Track One',
                    'artistName': 'Paper Parachutes',
                    'trackNumber': 1,
                    'discNumber': 1,
                    'durationInMillis': 120000,
                },
            },
            {
                'id': 't3',
                'attributes': {
                    'name': 'Bonus Track',
                    'artistName': 'Paper Parachutes',
                    'trackNumber': 1,
                    'discNumber': 2,
                    'durationInMillis': 200000,
                },
            },
        ]

        item = normalize.normalize_album(raw_album, cache_dir=self.tmp_dir, tracks=raw_tracks)
        self.assertEqual(item['id'], 'l.alb123')
        self.assertEqual(item['kind'], 'album')
        self.assertEqual(item['title'], 'Static Skyline')
        self.assertEqual(item['subtitle'], 'Paper Parachutes')
        self.assertEqual(item['year'], 2007)
        self.assertEqual(item['genre'], 'Rock')
        self.assertEqual(item['summary'], 'A classic easycore record.')
        self.assertEqual(item['artColor'], '#112233')
        self.assertEqual(item['play'], {'kind': 'album', 'id': 'l.alb123'})
        # Counts, not words: the model says "3 songs, 8 min".
        self.assertEqual((item['trackCount'], item['durationMs']), (3, 500000))
        self.assertNotIn('countLabel', item)

        # Two discs
        self.assertEqual(len(item['groups']), 2)
        disc1 = item['groups'][0]
        self.assertEqual(disc1['name'], 'Disc 1')
        self.assertEqual(len(disc1['entries']), 2)
        # Check sorting by trackNumber
        self.assertEqual(disc1['entries'][0]['title'], 'Track One')
        self.assertEqual(disc1['entries'][0]['index'], 0)
        self.assertEqual(disc1['entries'][1]['title'], 'Track Two')
        self.assertEqual(disc1['entries'][1]['index'], 1)

        disc2 = item['groups'][1]
        self.assertEqual(disc2['name'], 'Disc 2')
        self.assertEqual(len(disc2['entries']), 1)
        self.assertEqual(disc2['entries'][0]['title'], 'Bonus Track')
        # Numbered on from disc 1: the album plays as one queue, whichever disc's row starts it.
        self.assertEqual(disc2['play'], item['play'])
        self.assertEqual(disc2['entries'][0]['index'], len(disc1['entries']))

    def test_an_albums_entries_are_its_queue_in_order(self):
        tracks = [{'id': f'{disc}-{number}',
                   'attributes': {'name': f'{disc}.{number}', 'discNumber': disc,
                                  'trackNumber': number}}
                  for disc in (3, 1, 2) for number in (2, 1, 3)]
        album = normalize.normalize_album({'id': 'l.a', 'attributes': {'name': 'A'}},
                                          tracks=tracks)
        entries = [entry for group in album['groups'] for entry in group['entries']]
        self.assertEqual([entry['index'] for entry in entries], list(range(9)))
        self.assertEqual([entry['title'] for entry in entries],
                         [f'{disc}.{number}' for disc in (1, 2, 3) for number in (1, 2, 3)])
        # An artist's groups carry them as they are: an album's positions in its own queue.
        artist = normalize.normalize_artist({'id': 'l.r', 'attributes': {'name': 'R'}},
                                            albums=[album])
        self.assertEqual([entry['index'] for entry in artist['groups'][0]['entries']],
                         list(range(9)))

    def test_normalize_playlist(self):
        raw_playlist = {
            'id': 'p.pl123',
            'type': 'library-playlists',
            'attributes': {
                'name': 'Workout Mix',
                'curatorName': 'Jordan',
                'lastModifiedDate': '2024-05-01',
                'trackCount': 1,
            },
        }
        raw_tracks = [
            {
                'id': 's1',
                'attributes': {
                    'name': 'Energy',
                    'artistName': 'Artist',
                    'trackNumber': 1,
                    'durationInMillis': 210000,
                },
            }
        ]

        item = normalize.normalize_playlist(raw_playlist, cache_dir=self.tmp_dir, tracks=raw_tracks)
        self.assertEqual(item['id'], 'p.pl123')
        self.assertEqual(item['kind'], 'playlist')
        self.assertEqual(item['title'], 'Workout Mix')
        self.assertEqual(item['subtitle'], 'Jordan')
        self.assertEqual(item['year'], 2024)
        self.assertEqual(len(item['groups']), 1)
        self.assertEqual(item['groups'][0]['name'], 'Tracks')
        self.assertEqual(item['groups'][0]['entries'][0]['title'], 'Energy')
        self.assertIn('thumb', item)
        self.assertIn('thumb', item['groups'][0]['entries'][0])

    def test_recommendation_shelves_follow_apples_order_and_titles(self):
        def album(i):
            return {'id': f'a{i}', 'type': 'albums',
                    'attributes': {'name': f'Album {i}', 'artistName': 'X'}}
        raw = [
            {'id': 'one', 'attributes': {'title': {'stringForDisplay': 'New Releases for You'}},
             'relationships': {'contents': {'data': [album(1), album(2)]}}},
            # A group: its members are shelves of their own, in place of it.
            {'id': 'grp',
             'attributes': {'isGroupRecommendation': True, 'title': {'stringForDisplay': 'Genres'}},
             'relationships': {'recommendations': {'data': [
                 {'id': 'rock', 'attributes': {'title': {'stringForDisplay': 'Rock'}},
                  'relationships': {'contents': {'data': [album(3)]}}},
                 {'id': 'empty', 'attributes': {'title': {'stringForDisplay': 'Nothing'}},
                  'relationships': {'contents': {'data': []}}},
             ]}}},
            {'id': 'st', 'attributes': {'title': {'stringForDisplay': 'Stations for You'}},
             'relationships': {'contents': {'data': [
                 {'id': 'ra.1', 'type': 'stations', 'attributes': {'name': 'Alt Station'}}]}}},
        ]
        shelves = normalize.recommendation_shelves(raw)
        self.assertEqual([s['key'] for s in shelves], ['rec-one', 'rec-rock', 'rec-st'])
        self.assertEqual([s['title'] for s in shelves],
                         ['New Releases for You', 'Rock', 'Stations for You'])
        self.assertEqual([it['title'] for it in shelves[0]['items']], ['Album 1', 'Album 2'])
        self.assertEqual(shelves[2]['items'][0]['kind'], 'station')
        self.assertEqual(shelves[0]['items'][0]['groups'], [])

    def test_recommendations_keep_to_what_the_app_can_show(self):
        curator = {'id': '900000901', 'type': 'apple-curators',
                   'attributes': {'name': 'Apple Music Folk', 'shortName': 'Folk'}}
        banner = {'id': 'e.1', 'type': 'editorial-items', 'attributes': {'name': 'A Banner'}}
        album = {'id': 'a1', 'type': 'albums', 'attributes': {'name': 'Album 1'}}
        mix = {'id': 'pl.m', 'type': 'playlists',
               'attributes': {'name': 'Chill Mix', 'playlistType': 'personal-mix'}}
        raw = [
            {'id': 'mixed', 'attributes': {'title': {'stringForDisplay': 'For the Weekend'}},
             'relationships': {'contents': {'data': [curator, banner, album]}}},
            {'id': 'none', 'attributes': {'title': {'stringForDisplay': 'Browse'}},
             'relationships': {'contents': {'data': [curator, banner]}}},
            {'attributes': {}, 'relationships': {'contents': {'data': [mix]}}},
        ]
        shelves = normalize.recommendation_shelves(raw, self.tmp_dir)
        # Home: the curator and the banner are left out; a shelf with nothing else goes.
        self.assertEqual([(s['key'], [it['id'] for it in s['items']]) for s in shelves],
                         [('rec-mixed', ['a1']), ('rec-1', ['pl.m'])])
        self.assertEqual(shelves[0]['items'][0]['groups'], [])
        # Made for You takes only the recommendations made entirely of mixes and stations.
        self.assertEqual([s['key'] for s in normalize.made_for_you_shelves(raw, self.tmp_dir)],
                         ['rec-0'])

    def test_search_results_are_shelved_in_apples_order(self):
        with open(os.path.join(os.path.dirname(__file__), 'fixtures', 'search_results.json')) as f:
            raw = json.load(f)
        # The catalog's own Top Results — the same song again, and the album
        # — and its own order for the shelves, songs ahead of albums.
        song = raw['results']['songs']['data'][0]
        album = raw['results']['albums']['data'][0]
        raw['results']['topResults'] = {'data': [song, album]}
        raw['meta'] = {'results': {'order': ['topResults', 'artists', 'songs', 'albums',
                                             'playlists']}}
        # A cover the sync has fetched already, and its thumbnail.
        cover_url = normalize.template_artwork_url(album['attributes']['artwork']['url'], 512, 512)
        thumb_path = normalize.thumb_cache_path(cover_url, self.tmp_dir)
        os.makedirs(os.path.dirname(thumb_path))
        with open(thumb_path, 'wb') as f:
            f.write(b'jpg')

        out = normalize.search_results(raw, self.tmp_dir)

        self.assertEqual([s['key'] for s in out['shelves']],
                         ['top', 'artists', 'songs', 'albums', 'playlists'])
        self.assertEqual([s['title'] for s in out['shelves']], [''] * 5)  # the page's words
        top = out['shelves'][0]['items']
        self.assertEqual([it['kind'] for it in top], ['song', 'album'])
        self.assertEqual(top[0]['groups'], [])
        self.assertEqual(set(out), {'shelves'})
        # The album's thumbnail is on disk and stays; its cover is not, so a
        # small catalog URL stands in. The song carries no artwork at all.
        hit_album = out['shelves'][3]['items'][0]
        self.assertEqual(hit_album['thumb'], thumb_path)
        self.assertTrue(hit_album['art'].startswith('https://'))
        self.assertIn('256x256', hit_album['art'])
        hit_song = out['shelves'][2]['items'][0]
        self.assertIsNone(hit_song['thumb'])
        self.assertIsNone(hit_song['art'])

    def test_search_results_with_nothing(self):
        self.assertEqual(normalize.search_results(None, self.tmp_dir), {'shelves': []})
        self.assertEqual(normalize.search_results({'results': {'albums': {'data': []}}},
                                                  self.tmp_dir),
                         {'shelves': []})

    def test_search_results_without_apples_order_take_the_usual(self):
        raw = {'results': {
            'albums': {'data': [{'id': '1', 'type': 'albums',
                                 'attributes': {'name': 'A', 'artistName': 'X'}}]},
            'artists': {'data': [{'id': '2', 'type': 'artists', 'attributes': {'name': 'X'}}]},
            'stations': {'data': [{'id': 'ra.3', 'type': 'stations', 'attributes': {'name': 'S'}}]},
        }}
        out = normalize.search_results(raw, self.tmp_dir)
        self.assertEqual([s['key'] for s in out['shelves']], ['artists', 'albums', 'stations'])

    def test_search_results_shelve_music_videos_as_videos(self):
        raw = {'results': {
            'music-videos': {'data': [{'id': '9', 'type': 'music-videos', 'attributes': {
                'name': 'Orbit Parade', 'artistName': 'Hollow Satellites',
                'durationInMillis': 240000,
                'artwork': {'url': 'https://x/{w}x{h}bb.{f}'}}}]},
        }}
        out = normalize.search_results(raw, self.tmp_dir)
        self.assertEqual([(s['key'], s['title']) for s in out['shelves']],
                         [('music-videos', '')])
        video = out['shelves'][0]['items'][0]
        self.assertEqual(video['kind'], 'video')
        self.assertEqual(video['title'], 'Orbit Parade')
        self.assertEqual(video['subtitle'], 'Hollow Satellites')
        self.assertEqual(video['play'], {'kind': 'musicVideo', 'id': '9'})
        self.assertIn('256x256', video['art'])

    def test_search_suggestions_are_terms_and_top_hits(self):
        raw = {'results': {'suggestions': [
            {'kind': 'terms', 'searchTerm': 'glow', 'displayTerm': 'glow'},
            {'kind': 'terms', 'searchTerm': 'glow in the dark', 'displayTerm': 'glow in the dark'},
            {'kind': 'terms', 'searchTerm': 'Glow', 'displayTerm': 'Glow'},
            {'kind': 'topResults', 'content': {'id': '1', 'type': 'songs', 'attributes': {
                'name': 'Glow', 'artistName': 'Lantern Theory', 'durationInMillis': 1000}}},
            {'kind': 'topResults', 'content': {'id': '1', 'type': 'songs', 'attributes': {
                'name': 'Glow', 'artistName': 'Lantern Theory', 'durationInMillis': 1000}}},
            {'kind': 'topResults',
             'content': {'id': '2', 'type': 'artists', 'attributes': {'name': 'Glow'}}},
            {'kind': 'topResults', 'content': 'not a resource'},
            'not a suggestion',
        ]}}
        out = normalize.search_suggestions(raw, self.tmp_dir)
        self.assertEqual(out['terms'], [{'term': 'glow', 'display': 'glow'},
                                        {'term': 'glow in the dark',
                                         'display': 'glow in the dark'}])
        self.assertEqual([(it['kind'], it['id'], it['title']) for it in out['items']],
                         [('song', '1', 'Glow'), ('artist', '2', 'Glow')])
        self.assertEqual(out['items'][0]['groups'], [])
        self.assertIsNone(out['items'][0]['art'])

    def test_search_suggestions_with_nothing(self):
        self.assertEqual(normalize.search_suggestions(None, self.tmp_dir),
                         {'terms': [], 'items': []})
        self.assertEqual(normalize.search_suggestions({'results': {}}, self.tmp_dir),
                         {'terms': [], 'items': []})

    def test_search_landing_is_apples_curators_in_order(self):
        curator = lambda cid, name, short=None, bg='dd6848': {  # noqa: E731
            'id': cid, 'type': 'apple-curators', 'attributes': {
                'name': name, 'shortName': short or name,
                'url': f'https://music.apple.com/gb/curator/x/{cid}',
                'artwork': {'url': 'https://x/{w}x{h}{c}.{f}', 'bgColor': bg, 'width': 1080,
                            'height': 1080}}}
        raw = {'data': [
            {'id': 'r1', 'type': 'personal-recommendation',
             'attributes': {'title': {'stringForDisplay': 'Browse Categories'}},
             'relationships': {'contents': {'data': [curator('1', 'Apple Music Live')]}}},
            {'id': 'r2', 'type': 'personal-recommendation',
             'relationships': {'contents': {'data': [
                 curator('2', 'Apple Music Rock', 'Rock'),
                 {'id': 'e1', 'type': 'editorial-items', 'attributes': {'editorialArtwork': {}}},
                 curator('2', 'Apple Music Rock', 'Rock'),
                 {'id': '3', 'type': 'apple-curators', 'attributes': {'artwork': {}}},
                 'junk',
             ]}}},
        ]}
        out = normalize.search_landing(raw, self.tmp_dir)
        self.assertEqual([(c['id'], c['title'], c['subtitle']) for c in out['categories']],
                         [('1', 'Apple Music Live', None), ('2', 'Rock', 'Apple Music Rock')])
        rock = out['categories'][1]
        self.assertEqual(rock['kind'], 'category')
        self.assertEqual(rock['art'], 'https://x/320x320bb.jpg')
        self.assertEqual(rock['artColor'], '#dd6848')
        self.assertEqual(rock['url'], 'https://music.apple.com/gb/curator/x/2')
        self.assertEqual(normalize.search_landing(None, self.tmp_dir), {'categories': []})

    def test_category_page_is_the_groupings_shelves(self):
        element = lambda eid, name, contents: {  # noqa: E731
            'id': eid, 'type': 'editorial-elements', 'attributes': {'editorialElementKind': '326',
                                                                    'name': name},
            'relationships': {'contents': {'data': contents}}}
        raw = {'data': [{
            'id': '900000101', 'type': 'apple-curators',
            'attributes': {'name': 'Apple Music Rock', 'shortName': 'Rock'},
            'relationships': {'grouping': {'data': [{
                'id': 'g', 'type': 'groupings', 'attributes': {'name': 'Rock'},
                'relationships': {'tabs': {'data': [{
                    'id': 't', 'type': 'editorial-elements',
                    'relationships': {'children': {'data': [
                        element('hero', '', []),
                        element('songs', 'Best New Songs', [
                            {'id': 's1', 'type': 'songs',
                             'attributes': {'name': 'Quiet Engines', 'artistName': 'A',
                                            'durationInMillis': 1000}},
                            {'id': 's2', 'type': 'songs'},
                        ]),
                        element('albums', 'New Releases', [
                            {'id': 'a1', 'type': 'albums',
                             'attributes': {'name': 'Borrowed Lanterns', 'artistName': 'B',
                                            'artwork': {'url': 'https://x/{w}x{h}{c}.{f}'}}},
                        ]),
                        element('empty', 'Nothing Here', []),
                    ]}}}]}}}]}},
        }]}
        out = normalize.category_page(raw, self.tmp_dir)
        self.assertEqual(out['id'], '900000101')
        self.assertEqual(out['title'], 'Rock')
        self.assertEqual([(s['key'], s['title'], len(s['items'])) for s in out['shelves']],
                         [('cat-songs', 'Best New Songs', 1), ('cat-albums', 'New Releases', 1)])
        self.assertEqual(out['shelves'][0]['items'][0]['kind'], 'song')
        album = out['shelves'][1]['items'][0]
        self.assertEqual(album['groups'], [])
        self.assertIn('256x256', album['art'])
        self.assertEqual(normalize.category_page(None, self.tmp_dir),
                         {'id': '', 'title': '', 'shelves': []})

    def test_normalize_station(self):
        raw_station = {
            'id': 'ra.12345',
            'type': 'stations',
            'attributes': {
                'name': 'Apple Music 1',
                'stationProviderName': 'Apple Music',
                'description': {'standard': 'The new music that matters.'},
            },
        }
        item = normalize.normalize_station(raw_station, cache_dir=self.tmp_dir)
        self.assertEqual(item['id'], 'ra.12345')
        self.assertEqual(item['kind'], 'station')
        self.assertEqual(item['title'], 'Apple Music 1')
        self.assertEqual(item['subtitle'], 'Apple Music')
        self.assertEqual(item['summary'], 'The new music that matters.')
        self.assertEqual(item['groups'], [])

    def test_counts_are_numbers(self):
        # Without its tracks (a shelf item): Apple's count, and no total time.
        shelf_album = normalize.normalize_album({'id': '1', 'attributes': {'name': 'A',
                                                                           'trackCount': 11}})
        self.assertEqual((shelf_album['trackCount'], shelf_album['durationMs']), (11, None))
        empty = normalize.normalize_playlist({'id': 'p.1', 'attributes': {'name': 'P'}}, tracks=[])
        self.assertEqual((empty['trackCount'], empty['durationMs']), (0, None))
        playlist = normalize.normalize_playlist(
            {'id': 'p.2', 'attributes': {'name': 'P'}},
            tracks=[{'id': 'i.1', 'attributes': {'name': 'T', 'durationInMillis': 61000}}])
        self.assertEqual((playlist['trackCount'], playlist['durationMs']), (1, 61000))
        artist = normalize.normalize_artist({'id': 'l.r', 'attributes': {'name': 'R'}},
                                            albums=[shelf_album, playlist])
        self.assertEqual(artist['albumCount'], 2)
        song = normalize.normalize_item({'id': '9', 'type': 'songs',
                                         'attributes': {'name': 'S', 'durationInMillis': 1000}})
        self.assertEqual(song['durationMs'], 1000)
        station = normalize.normalize_station({'id': 'ra.1', 'attributes': {'name': 'S'}})
        for item in (shelf_album, empty, playlist, artist, song, station):
            with self.subTest(kind=item['kind']):
                self.assertNotIn('countLabel', item)
        self.assertNotIn('trackCount', station)

    def test_nothing_is_attributed_to_anyone_made_up(self):
        station = normalize.normalize_station({'id': 'ra.1', 'attributes': {'name': 'S'}})
        album = normalize.normalize_album({'id': 'l.a', 'attributes': {'name': 'A'}})
        playlist = normalize.normalize_playlist({'id': 'p.1', 'attributes': {'name': 'Mine'}})
        artist = normalize.normalize_artist({'id': 'l.r', 'attributes': {'name': 'R'}})
        for item in (station, album, playlist, artist):
            with self.subTest(kind=item['kind']):
                self.assertEqual(item['subtitle'], '')

    def test_a_resource_of_no_known_type_plays_nothing(self):
        item = normalize.normalize_item({'id': 'e.1', 'type': 'editorial-items',
                                         'attributes': {'name': 'A Banner'}})
        self.assertEqual((item['kind'], item['title'], item['play']), ('unknown', 'A Banner', {}))

    def test_songs_without_names_group_under_nameless_albums_and_artists(self):
        songs = [{'id': 'i.1', 'type': 'library-songs',
                  'attributes': {'name': 'Untitled', 'artistName': '', 'trackNumber': 1}},
                 {'id': 'i.2', 'type': 'library-songs',
                  'attributes': {'name': 'Also Untitled', 'trackNumber': 2}}]
        albums, artists = normalize.group_songs_into_albums_and_artists(songs)
        self.assertEqual([(album['title'], album['subtitle']) for album in albums], [('', '')])
        self.assertEqual([artist['title'] for artist in artists], [''])
        self.assertEqual(len(albums[0]['groups'][0]['entries']), 2)

    def test_a_stand_in_album_plays_its_songs(self):
        def song(song_id, number, album=None):
            raw = {'id': song_id, 'type': 'library-songs', 'attributes': {
                'name': f'Song {number}', 'artistName': 'The Invented Band',
                'albumName': 'Loose Ends', 'trackNumber': number, 'discNumber': 1}}
            if album:
                raw['relationships'] = {'albums': {'data': [{'id': album, 'type': 'library-albums',
                                                             'attributes': {'name': 'Tidewater'}}]}}
            return raw

        songs = [song('i.c', 3), song('i.a', 1), song('i.real', 1, album='l.alb1'), song('i.b', 2)]
        albums, artists = normalize.group_songs_into_albums_and_artists(songs)
        stand_in = next(album for album in albums if album['id'].startswith('l.alb_'))
        real = next(album for album in albums if album['id'] == 'l.alb1')
        play = {'kind': 'songs', 'id': 'i.a,i.b,i.c'}  # in the entries' order
        self.assertEqual(stand_in['play'], play)
        self.assertEqual([group['play'] for group in stand_in['groups']], [play])
        entries = stand_in['groups'][0]['entries']
        self.assertEqual([(entry['id'], entry['index']) for entry in entries],
                         [('i.a', 0), ('i.b', 1), ('i.c', 2)])
        self.assertEqual(real['play'], {'kind': 'album', 'id': 'l.alb1'})  # a real one as it was
        # The artist's group for it plays the same.
        band = next(artist for artist in artists if artist['title'] == 'The Invented Band')
        self.assertEqual([(group['name'], group['play']) for group in band['groups']],
                         [('Loose Ends', play)])

    def test_group_songs_into_albums_and_artists(self):
        songs = [
            {
                'id': 's1',
                'attributes': {
                    'name': 'Track A',
                    'artistName': 'Artist One',
                    'albumName': 'Album One',
                    'trackNumber': 1,
                    'discNumber': 1,
                    'durationInMillis': 120000,
                    'releaseDate': '2020-01-01',
                    'genreNames': ['Pop'],
                },
                'relationships': {
                    'albums': {
                        'data': [{
                            'id': 'l.alb1',
                            'type': 'library-albums',
                            'attributes': {
                                'name': 'Album One',
                                'artistName': 'Artist One',
                                'releaseDate': '2020-01-01',
                                'genreNames': ['Pop'],
                            },
                        }]
                    }
                },
            },
            {
                'id': 's2',
                'attributes': {
                    'name': 'Track B',
                    'artistName': 'Artist One',
                    'albumName': 'Album One',
                    'trackNumber': 2,
                    'discNumber': 1,
                    'durationInMillis': 180000,
                    'releaseDate': '2020-01-01',
                    'genreNames': ['Pop'],
                },
                'relationships': {
                    'albums': {
                        'data': [{
                            'id': 'l.alb1',
                            'type': 'library-albums',
                            'attributes': {
                                'name': 'Album One',
                                'artistName': 'Artist One',
                                'releaseDate': '2020-01-01',
                                'genreNames': ['Pop'],
                            },
                        }]
                    }
                },
            },
        ]

        albums, artists = normalize.group_songs_into_albums_and_artists(songs, self.tmp_dir)
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]['title'], 'Album One')
        self.assertEqual(len(albums[0]['groups'][0]['entries']), 2)

        self.assertEqual(len(artists), 1)
        self.assertEqual(artists[0]['title'], 'Artist One')
        self.assertEqual(len(artists[0]['groups']), 1)
        self.assertEqual(artists[0]['groups'][0]['name'], 'Album One')

    def test_artist_takes_first_albums_thumbnail_with_its_cover(self):
        song = {
            'id': 's1', 'type': 'library-songs',
            'attributes': {
                'name': 'Track A', 'artistName': 'Artist One', 'albumName': 'Album One',
                'trackNumber': 1, 'discNumber': 1, 'durationInMillis': 1000,
                'artwork': {'url': 'https://x/{w}x{h}bb.jpg', 'bgColor': '123456'},
            },
            'relationships': {'albums': {'data': [{
                'id': 'l.alb1', 'type': 'library-albums',
                'attributes': {'name': 'Album One', 'artistName': 'Artist One',
                               'artwork': {'url': 'https://x/{w}x{h}bb.jpg', 'bgColor': '123456'}},
            }]}},
        }
        albums, artists = normalize.group_songs_into_albums_and_artists([song], self.tmp_dir)
        self.assertEqual(artists[0]['art'], albums[0]['art'])
        self.assertEqual(artists[0]['thumb'], albums[0]['thumb'])
        self.assertTrue(artists[0]['thumb'].startswith(os.path.join(self.tmp_dir, 'thumb')))
        self.assertEqual(artists[0]['artColor'], '#123456')

    def test_save_library_replaces_the_file(self):
        library = {'version': 1, 'sections': {'albums': [{'id': 'a1', 'title': 'Album 1'}]},
                   'shelves': []}
        normalize.save_library(library, self.tmp_dir)
        normalize.save_library(dict(library, sections={}), self.tmp_dir, indent=None)
        lib_file = os.path.join(self.tmp_dir, 'library.json')
        with open(lib_file) as f:
            text = f.read()
        self.assertEqual(json.loads(text), {'version': 1, 'sections': {}, 'shelves': []})
        self.assertNotIn('\n', text)  # the compact form
        self.assertEqual(os.listdir(self.tmp_dir), ['library.json'])  # no lock, no temp

    def test_prune_art(self):
        art_dir = os.path.join(self.tmp_dir, 'art')
        thumb_dir = os.path.join(self.tmp_dir, 'thumb')
        os.makedirs(art_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)
        used_file = os.path.join(art_dir, 'used.jpg')
        unused_file = os.path.join(art_dir, 'unused.jpg')
        used_thumb = os.path.join(thumb_dir, 'used.jpg')
        unused_thumb = os.path.join(thumb_dir, 'unused.jpg')
        row_thumb = os.path.join(thumb_dir, 'row.jpg')

        for path in (used_file, unused_file, used_thumb, unused_thumb, row_thumb):
            with open(path, 'w') as f:
                f.write('test')

        lib_data = {
            'sections': {
                'albums': [{'art': used_file, 'thumb': used_thumb}],
                'artists': [],
                'playlists': [{'art': None, 'thumb': None,
                               'groups': [{'entries': [{'thumb': row_thumb}]}]}],
                'radio': [],
            },
            'shelves': [],
        }

        marker = os.path.join(art_dir, '.sizes')
        with open(marker, 'w') as f:
            f.write('{"cover": 512, "thumb": 256}')

        pruned = normalize.prune_art(lib_data, self.tmp_dir)
        self.assertEqual(pruned, 2)
        self.assertTrue(os.path.exists(marker))
        self.assertTrue(os.path.exists(used_file))
        self.assertTrue(os.path.exists(used_thumb))
        self.assertTrue(os.path.exists(row_thumb))
        self.assertFalse(os.path.exists(unused_file))
        self.assertFalse(os.path.exists(unused_thumb))


class TestArtworkDownload(unittest.TestCase):
    """Normalisation names the file; download_art fetches what is missing."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.art_urls = {}
        normalize.ART_SIZES.update(UPSTREAM_SIZES)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)
        normalize.ART_SIZES.update(normalize.DEFAULT_ART_SIZES)

    def _album(self, art_url):
        return {
            'id': 'l.one', 'type': 'library-albums',
            'attributes': {'name': 'One', 'artistName': 'A', 'artwork': {'url': art_url}},
        }

    def album(self, art_url, **changes):
        """An album normalised with this test's registry."""
        return normalize.normalize_album(dict(self._album(art_url), **changes),
                                         cache_dir=self.tmp_dir, art_urls=self.art_urls)

    def test_extract_registers_url_for_path(self):
        item = self.album('https://x/{w}x{h}bb.jpg')
        self.assertTrue(item['art'].startswith(os.path.join(self.tmp_dir, 'art')))
        self.assertEqual(self.art_urls[item['art']], 'https://x/512x512bb.jpg')
        self.assertTrue(item['thumb'].startswith(os.path.join(self.tmp_dir, 'thumb')))
        self.assertEqual(self.art_urls[item['thumb']], 'https://x/256x256bb.jpg')
        self.assertEqual(os.path.basename(item['thumb']), os.path.basename(item['art']))

    def test_the_registry_is_the_callers(self):
        self.assertFalse(hasattr(normalize, 'ART_URLS'))  # no state kept between calls
        first, second = {}, {}
        album = normalize.normalize_album(self._album('https://x/{w}x{h}bb.jpg'),
                                          cache_dir=self.tmp_dir, art_urls=first)
        normalize.normalize_album(self._album('https://y/{w}x{h}bb.jpg'),
                                  cache_dir=self.tmp_dir, art_urls=second)
        self.assertEqual(set(first), {album['art'], album['thumb']})
        self.assertNotIn(album['art'], second)
        # Without one, the paths are named all the same.
        again = normalize.normalize_album(self._album('https://x/{w}x{h}bb.jpg'),
                                          cache_dir=self.tmp_dir)
        self.assertEqual((again['art'], again['thumb']), (album['art'], album['thumb']))
        library = {'sections': {'albums': [album]}, 'shelves': []}
        self.assertEqual(normalize.collect_art_urls(library, first), first)
        self.assertEqual(normalize.collect_art_urls(library, second), {})

    def test_download_art_fetches_only_missing(self):
        item = self.album('https://x/{w}x{h}bb.jpg')
        other = self.album('https://y/{w}x{h}bb.jpg', id='l.two')
        os.makedirs(os.path.dirname(other['art']), exist_ok=True)
        with open(other['art'], 'wb') as f:
            f.write(b'already here')
        lib = {'sections': {'albums': [item, other]}, 'shelves': []}

        fetched = []

        def fake_cache(url, cache_dir, timeout=10.0, dest_path=None, generation=None):
            fetched.append(url)
            path = dest_path or normalize.artwork_cache_path(url, cache_dir)
            with open(path, 'wb') as f:
                f.write(b'img')
            return path

        def fake_thumb(url, cache_dir, dest_path, generation=None):
            fetched.append(url)
            with open(dest_path, 'wb') as f:
                f.write(b'img')
            return dest_path

        real = normalize.cache_artwork, normalize.cache_thumbnail
        normalize.cache_artwork, normalize.cache_thumbnail = fake_cache, fake_thumb
        try:
            counts = normalize.download_art(normalize.collect_art_urls(lib, self.art_urls),
                                            self.tmp_dir)
        finally:
            normalize.cache_artwork, normalize.cache_thumbnail = real
        # The one missing cover, then the thumbnails of both, in that order.
        self.assertEqual(fetched[0], 'https://x/512x512bb.jpg')
        self.assertEqual(sorted(fetched[1:]),
                         ['https://x/256x256bb.jpg', 'https://y/256x256bb.jpg'])
        self.assertEqual(counts, {'wanted': 4, 'fetched': 3, 'failed': 0})
        self.assertTrue(os.path.exists(item['art']))
        self.assertTrue(os.path.exists(item['thumb']))

    def test_download_art_counts_failures(self):
        self.album('https://x/{w}x{h}bb.jpg')
        real = normalize.cache_artwork, normalize.cache_thumbnail
        normalize.cache_artwork = lambda url, cache_dir, **kwargs: None

        def broken(url, cache_dir, dest_path, generation=None):
            raise OSError('no space left')
        normalize.cache_thumbnail = broken
        try:
            with self.assertLogs('applemusic.backend.normalize', 'WARNING') as logs:
                counts = normalize.download_art(self.art_urls, self.tmp_dir)
        finally:
            normalize.cache_artwork, normalize.cache_thumbnail = real
        # The cover and its thumbnail; the one that raised is logged with its reason (the one
        # that answered None logs its own).
        self.assertEqual(counts['failed'], 2)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('no space left', logs.output[0])

    def test_a_cancelled_download_says_so(self):
        self.album('https://x/{w}x{h}bb.jpg')
        urls = self.art_urls
        fetched = []
        real = normalize.cache_artwork, normalize.cache_thumbnail
        normalize.cache_artwork = lambda url, cache_dir, **kwargs: fetched.append(url)
        normalize.cache_thumbnail = lambda url, cache_dir, dest, **kwargs: fetched.append(url)
        try:
            with self.assertRaises(normalize.Cancelled):
                normalize.download_art(urls, self.tmp_dir, cancelled=lambda: True)
            self.assertEqual(fetched, [])  # nothing started
            # Cancelled while the covers come in: the thumbnails are never started.
            asked = []

            def cancelled():
                asked.append(True)
                return len(asked) > 1
            with self.assertRaises(normalize.Cancelled):
                normalize.download_art(urls, self.tmp_dir, cancelled=cancelled)
            self.assertEqual(fetched, ['https://x/512x512bb.jpg'])
        finally:
            normalize.cache_artwork, normalize.cache_thumbnail = real

    @unittest.skipIf(GdkPixbuf is None, 'GdkPixbuf not available')
    def test_thumbnail_is_scaled_from_the_cached_cover(self):
        self.addCleanup(setattr, normalize, 'scale_image', normalize.scale_image)
        normalize.scale_image = pixbuf_scaler
        item = self.album('https://x/{w}x{h}bb.jpg')
        os.makedirs(os.path.dirname(item['art']), exist_ok=True)
        cover = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 512, 512)
        cover.fill(0x336699ff)
        cover.savev(item['art'], 'jpeg', ['quality'], ['80'])
        # Nothing is fetched: the cover is on disk.
        real = normalize.cache_artwork
        normalize.cache_artwork = lambda *a, **k: self.fail('fetched a thumbnail it could scale')
        try:
            counts = normalize.download_art({item['thumb']: self.art_urls[item['thumb']]},
                                            self.tmp_dir)
        finally:
            normalize.cache_artwork = real
        self.assertEqual(counts, {'wanted': 1, 'fetched': 1, 'failed': 0})
        thumb = GdkPixbuf.Pixbuf.new_from_file(item['thumb'])
        self.assertEqual((thumb.get_width(), thumb.get_height()),
                         (normalize.ART_SIZES['thumb'], normalize.ART_SIZES['thumb']))

    def test_thumbnail_is_fetched_without_a_scaler(self):
        # The backend imports no gi: with no scale_image installed, a cover on
        # disk does not help, and the thumbnail is fetched at its own size.
        self.addCleanup(setattr, normalize, 'scale_image', normalize.scale_image)
        normalize.scale_image = None
        item = self.album('https://x/{w}x{h}bb.jpg')
        os.makedirs(os.path.dirname(item['art']), exist_ok=True)
        with open(item['art'], 'wb') as f:
            f.write(b'a cover')
        self.assertFalse(normalize.make_thumbnail(item['art'], item['thumb'], self.tmp_dir))
        fetched = []
        real = normalize.cache_artwork
        def fake_cache_artwork(url, cache_dir, dest_path=None, generation=None):
            fetched.append((url, dest_path))
            return dest_path
        normalize.cache_artwork = fake_cache_artwork
        try:
            self.assertEqual(normalize.cache_thumbnail(self.art_urls[item['thumb']],
                                                       self.tmp_dir, item['thumb']), item['thumb'])
        finally:
            normalize.cache_artwork = real
        self.assertEqual(fetched, [('https://x/256x256bb.jpg', item['thumb'])])

    def test_download_item_art_takes_the_rows_thumbnails_too(self):
        raw = {'id': 'p.one', 'type': 'library-playlists',
               'attributes': {'name': 'Mix', 'artwork': {'url': 'https://p/{w}x{h}bb.jpg'}}}
        tracks = [{'id': 'i.1',
                   'attributes': {'name': 'A', 'artwork': {'url': 'https://a/{w}x{h}bb.jpg'}}},
                  {'id': 'i.2',
                   'attributes': {'name': 'B', 'artwork': {'url': 'https://b/{w}x{h}bb.jpg'}}}]
        item = normalize.normalize_playlist(raw, cache_dir=self.tmp_dir, tracks=tracks,
                                            art_urls=self.art_urls)
        asked = []
        real = normalize.download_art
        normalize.download_art = lambda urls, cache_dir, **k: asked.append(sorted(urls.values()))
        try:
            normalize.download_item_art(item, self.tmp_dir, self.art_urls)
            normalize.download_item_art(item, self.tmp_dir, {})  # a registry without them
        finally:
            normalize.download_art = real
        self.assertEqual(asked, [[
            'https://a/256x256bb.jpg', 'https://b/256x256bb.jpg',
            'https://p/256x256bb.jpg', 'https://p/512x512bb.jpg',
        ], []])

    def test_album_stub_without_attributes_takes_song_name(self):
        songs = [{
            'id': 'i.1', 'type': 'library-songs',
            'attributes': {
                'name': 'First Light', 'albumName': 'Collected Postcards',
                'artistName': 'Isla Marren',
                'trackNumber': 1, 'discNumber': 1, 'durationInMillis': 1000,
                'artwork': {'url': 'https://x/{w}x{h}bb.jpg'},
            },
            'relationships': {'albums': {'data': [{'id': 'l.gone', 'type': 'library-albums'}]}},
        }]
        albums, artists = normalize.group_songs_into_albums_and_artists(songs, self.tmp_dir)
        self.assertEqual(albums[0]['id'], 'l.gone')
        self.assertEqual(albums[0]['title'], 'Collected Postcards')
        self.assertEqual(albums[0]['subtitle'], 'Isla Marren')
        self.assertIsNotNone(albums[0]['art'])
        self.assertEqual(artists[0]['title'], 'Isla Marren')


class CannedHandler(http.server.BaseHTTPRequestHandler):
    """Answers /ok.jpg with a few bytes and anything else with 404."""

    def do_GET(self):
        if self.path == '/ok.jpg':
            body = b'not really a jpeg'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


class TestCacheArtwork(unittest.TestCase):
    """cache_artwork against a server on the loopback interface."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), CannedHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        # No proxy between this test and its server.
        patcher = mock.patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '*'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def files(self):
        return sorted(os.path.relpath(os.path.join(folder, name), self.tmp_dir)
                      for folder, _dirs, names in os.walk(self.tmp_dir) for name in names)

    def test_a_download_lands_under_the_urls_name(self):
        url = f'{self.base}/ok.jpg'
        path = normalize.cache_artwork(url, self.tmp_dir)
        self.assertEqual(path, normalize.artwork_cache_path(url, self.tmp_dir))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'not really a jpeg')
        self.assertEqual(self.files(), [os.path.join('art', normalize.artwork_filename(url))])

    def test_a_404_is_none_logged_with_its_reason_and_leaves_nothing(self):
        with self.assertLogs('applemusic.backend.normalize', 'WARNING') as logs:
            self.assertIsNone(normalize.cache_artwork(f'{self.base}/missing.jpg', self.tmp_dir))
        self.assertIn('404', logs.output[0])
        self.assertEqual(self.files(), [])  # no temp file left


class TestArtSizes(unittest.TestCase):
    """The cover and thumbnail sizes: set per sync, recorded beside the
    covers, read back by every other command."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        normalize.ART_SIZES.update(normalize.DEFAULT_ART_SIZES)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)
        normalize.ART_SIZES.update(normalize.DEFAULT_ART_SIZES)

    def test_defaults_without_a_marker(self):
        self.assertEqual(normalize.load_art_sizes(self.tmp_dir),
                         {'cover': config.COVER_SIZE, 'thumb': config.THUMB_SIZE})

    def test_apply_writes_the_marker_and_clamps(self):
        self.assertEqual(normalize.apply_art_sizes(self.tmp_dir, 768, 128),
                         {'cover': 768, 'thumb': 128})
        with open(os.path.join(self.tmp_dir, 'art', '.sizes')) as f:
            self.assertEqual(json.load(f), {'cover': 768, 'thumb': 128})
        normalize.ART_SIZES.update(normalize.DEFAULT_ART_SIZES)
        self.assertEqual(normalize.load_art_sizes(self.tmp_dir), {'cover': 768, 'thumb': 128})
        self.assertEqual(normalize.ART_SIZES, {'cover': 768, 'thumb': 128})
        # Out of range, or not a number: the nearest sane size, or the one standing.
        self.assertEqual(normalize.apply_art_sizes(self.tmp_dir, 9000, 10),
                         {'cover': 1024, 'thumb': 96})
        self.assertEqual(normalize.apply_art_sizes(self.tmp_dir, 'x', None),
                         {'cover': 1024, 'thumb': 96})

    def test_sizes_name_the_urls(self):
        normalize.apply_art_sizes(self.tmp_dir, 640, 192)
        art_urls = {}
        item = normalize.normalize_album({'id': 'l.one', 'type': 'library-albums', 'attributes': {
            'name': 'One', 'artistName': 'A', 'artwork': {'url': 'https://x/{w}x{h}bb.jpg'}}},
                                         cache_dir=self.tmp_dir, art_urls=art_urls)
        self.assertEqual(art_urls[item['art']], 'https://x/640x640bb.jpg')
        self.assertEqual(art_urls[item['thumb']], 'https://x/192x192bb.jpg')
        # A search hit without a cover on disk takes the thumbnail's size.
        hit = {'art': item['art'], 'thumb': item['thumb']}
        raw = {'attributes': {'artwork': {'url': 'https://x/{w}x{h}bb.jpg'}}}
        normalize._settle_search_art(hit, raw)
        self.assertEqual(hit['art'], 'https://x/192x192bb.jpg')
        self.assertIsNone(hit['thumb'])

    def test_changed_thumb_size_wipes_the_thumbnails(self):
        thumb_dir = os.path.join(self.tmp_dir, 'thumb')
        os.makedirs(thumb_dir)
        with open(os.path.join(thumb_dir, 'a.jpg'), 'wb') as f:
            f.write(b'old')
        # The same size as the (implied) default: nothing happens.
        normalize.apply_art_sizes(self.tmp_dir, config.COVER_SIZE, config.THUMB_SIZE)
        self.assertTrue(os.path.exists(os.path.join(thumb_dir, 'a.jpg')))
        # A changed cover size alone: the thumbnails stay too.
        normalize.apply_art_sizes(self.tmp_dir, 768, config.THUMB_SIZE)
        self.assertTrue(os.path.exists(os.path.join(thumb_dir, 'a.jpg')))
        # A changed thumbnail size: they go, to be rebuilt from the covers.
        normalize.apply_art_sizes(self.tmp_dir, 768, 128)
        self.assertEqual(os.listdir(thumb_dir), [])


class TestPruneCaches(unittest.TestCase):
    """prune_caches over an invented cache."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir)
        self.now = 10 ** 9

    def put(self, name, age=0, size=10):
        path = os.path.join(self.tmp_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'x' * size)
        os.utime(path, (self.now - age, self.now - age))
        return path

    def names(self, folder=''):
        return sorted(os.listdir(os.path.join(self.tmp_dir, folder)))

    def test_what_only_grows_is_trimmed(self):
        day = normalize.ANSWER_MAX_AGE
        for number in range(5):  # played 0 to 4 hours ago
            self.put(f'lyrics/{number}.json', age=number * 3600)
        self.put('categories/old.json', age=day + 60)
        self.put('categories/fresh.json', age=60)
        self.put('landing.json', age=day + 60)
        self.put('browse.json', age=60)
        self.put('.x.json.tmp', age=2 * 3600)  # a crash's leftover
        self.put('lyrics/.y.tmp', age=2 * 3600)
        self.put('.z.tmp', age=60)  # a write in progress
        self.put('items/album-1.json')
        self.put('items/artist-2.json')
        for number in range(3):
            self.put(f'remote-art/{number}.jpg', age=number, size=100)
        self.put('library.json', age=10 * day)  # the library's, never these
        self.put('art/a.jpg', age=10 * day)
        gone = normalize.prune_caches(self.tmp_dir, now=self.now, remote_bytes=250,
                                      lyrics_keep=2)
        self.assertEqual(gone, {'remote-art': 1, 'lyrics': 3, 'answers': 2, 'items': 2,
                                'temps': 2})
        self.assertEqual(self.names('lyrics'), ['0.json', '1.json'])
        self.assertEqual(self.names('categories'), ['fresh.json'])
        self.assertEqual(self.names('remote-art'), ['0.jpg', '1.jpg'])
        self.assertEqual(self.names(), ['.z.tmp', 'art', 'browse.json', 'categories',
                                        'library.json', 'lyrics', 'remote-art'])
        # Again: nothing more to do.
        self.assertFalse(any(normalize.prune_caches(self.tmp_dir, now=self.now,
                                                    remote_bytes=250, lyrics_keep=2).values()))

    def test_an_empty_cache(self):
        self.assertFalse(any(normalize.prune_caches(os.path.join(self.tmp_dir, 'none')).values()))


class TestRemoteArtItems(unittest.TestCase):
    """An item fetched on demand keeps its artwork in remote-art/."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir)

    def test_place_in_remote_art(self):
        raw = {'id': 'pl.one', 'type': 'playlists',
               'attributes': {'name': 'Mix', 'artwork': {'url': 'https://p/{w}x{h}bb.jpg'}}}
        tracks = [{'id': '1', 'attributes': {'name': 'A',
                                             'artwork': {'url': 'https://a/{w}x{h}bb.jpg'}}}]
        art_urls = {}
        item = normalize.normalize_playlist(raw, self.tmp_dir, tracks=tracks, art_urls=art_urls)
        # The library has the row's thumbnail already: that one stays where it is.
        row_thumb = item['groups'][0]['entries'][0]['thumb']
        os.makedirs(os.path.dirname(row_thumb))
        with open(row_thumb, 'wb') as f:
            f.write(b'img')
        placed = normalize.place_in_remote_art(item, art_urls, self.tmp_dir)
        remote = os.path.join(self.tmp_dir, 'remote-art')
        cover = normalize.template_artwork_url('https://p/{w}x{h}bb.jpg')
        self.assertEqual(item['art'], os.path.join(remote, normalize.artwork_filename(cover)))
        size = normalize.ART_SIZES['thumb']
        thumb_url = normalize.template_artwork_url('https://p/{w}x{h}bb.jpg', size, size)
        self.assertEqual(item['thumb'],
                         os.path.join(remote, normalize.artwork_filename(thumb_url)))
        self.assertEqual(item['groups'][0]['entries'][0]['thumb'], row_thumb)
        self.assertEqual(placed, {item['art']: cover, item['thumb']: thumb_url,
                                  row_thumb: art_urls[row_thumb]})


class TestOtherCaches(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_prune_remote_art_keeps_the_newest_within_budget(self):
        folder = os.path.join(self.tmp_dir, 'remote-art')
        os.makedirs(folder)
        for i in range(5):
            path = os.path.join(folder, f'{i}.img')
            with open(path, 'wb') as f:
                f.write(b'x' * 100)
            os.utime(path, (1000 + i, 1000 + i))
        self.assertEqual(normalize.prune_remote_art(self.tmp_dir, max_bytes=250), 3)
        self.assertEqual(sorted(os.listdir(folder)), ['3.img', '4.img'])
        self.assertEqual(normalize.prune_remote_art(self.tmp_dir, max_bytes=250), 0)
        self.assertEqual(normalize.prune_remote_art(os.path.join(self.tmp_dir, 'nowhere')), 0)

    def test_answer_paths_are_safe_names(self):
        self.assertEqual(normalize.category_cache_path(self.tmp_dir, '98 85/../81'),
                         os.path.join(self.tmp_dir, 'categories', '98_85_.._81.json'))
        self.assertEqual(normalize.landing_cache_path(self.tmp_dir),
                         os.path.join(self.tmp_dir, 'landing.json'))

    def test_answers_are_kept_and_age_out(self):
        path = normalize.category_cache_path(self.tmp_dir, '1')
        self.assertIsNone(normalize.read_answer(path, 60))
        kept = normalize.write_answer(path, {'id': '1', 'groups': []}, self.tmp_dir)
        self.assertIn('cached', kept)
        self.assertEqual(normalize.read_answer(path, 60)['id'], '1')
        # Older than allowed: as good as none.
        with open(path, 'r+', encoding='utf-8') as f:
            data = json.load(f)
            data['cached'] = '2000-01-01T00:00:00Z'
            f.seek(0)
            f.truncate()
            json.dump(data, f)
        self.assertIsNone(normalize.read_answer(path, 60))
        # Unreadable: none.
        with open(path, 'w') as f:
            f.write('{not json')
        self.assertIsNone(normalize.read_answer(path, 60))


if __name__ == '__main__':
    unittest.main()
