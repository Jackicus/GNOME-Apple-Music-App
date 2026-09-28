"""The backend's shaping of the New page (editorial groupings) and Made for You
(recommendations) into shelves, over invented answers."""

import json
import os
import pathlib
import tempfile
import unittest

from tests import SRC  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend import sync

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'


def mix(playlist_id, name, playlist_type='personal-mix'):
    return {'id': playlist_id, 'type': 'playlists', 'attributes': {
        'name': name, 'curatorName': 'Apple Music for You', 'playlistType': playlist_type,
        'artwork': {'url': 'https://x/mix/{w}x{h}{c}.{f}', 'bgColor': '223344'},
        'playParams': {'id': playlist_id, 'kind': 'playlist'}}}


def station(station_id, name):
    return {'id': station_id, 'type': 'stations', 'attributes': {
        'name': name, 'isLive': False,
        'artwork': {'url': 'https://x/station/{w}x{h}{c}.{f}', 'bgColor': '445566'},
        'playParams': {'id': station_id, 'kind': 'radioStation'}}}


def album(album_id, name):
    return {'id': album_id, 'type': 'albums', 'attributes': {
        'name': name, 'artistName': 'Paper Parachutes', 'trackCount': 10}}


def recommendation(rec_id, title, contents, members=None):
    rec = {'id': rec_id, 'type': 'personal-recommendation',
           'attributes': {'title': {'stringForDisplay': title}, 'kind': 'music-recommendations',
                          'isGroupRecommendation': bool(members)},
           'relationships': {'contents': {'data': contents}}}
    if members:
        rec['relationships']['recommendations'] = {'data': members}
    return rec


class EditorialShelvesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(FIXTURES / 'editorial_groupings.json', encoding='utf-8') as file:
            self.raw = json.load(file)

    def test_the_groupings_elements_become_shelves_in_order(self):
        out = sync.editorial_shelves(self.raw, self.tmp.name)
        shelves = out['shelves']
        self.assertEqual([(s['key'], s['title'], len(s['items'])) for s in shelves], [
            ('new-banners', 'Featured', 2),      # the banners' items, the uploaded video left out
            ('new-best-new-songs', 'Best New Songs', 2),
            ('new-new-releases', 'New Releases', 2),  # the nameless album left out
            ('new-stations', 'Stations', 1),
        ])  # the rooms (links only) and the link row are not shelves

    def test_featured_holds_the_banners_items_as_their_kinds(self):
        featured = sync.editorial_shelves(self.raw, self.tmp.name)['shelves'][0]
        kinds = [(item['kind'], item['id'], item['title']) for item in featured['items']]
        self.assertEqual(kinds, [('album', '900000201', 'Lantern Season'),
                                 ('category', '900000301', 'Folk')])
        category = featured['items'][1]
        self.assertEqual(category['subtitle'], 'Apple Music Folk')
        self.assertEqual(category['artColor'], '#6b4f2a')
        self.assertIn('320x320', category['art'])

    def test_items_are_shaped_as_search_hits(self):
        shelves = sync.editorial_shelves(self.raw, self.tmp.name)['shelves']
        song = shelves[1]['items'][0]
        self.assertEqual(song['kind'], 'song')
        self.assertEqual(song['play'], {'kind': 'song', 'id': '900000501'})
        self.assertEqual(song['groups'], [])
        self.assertIn('320x320', song['art'])  # the thumbnail-sized URL: nothing on disk
        self.assertIsNone(song['thumb'])
        self.assertTrue(shelves[1]['items'][1]['explicit'])
        album = shelves[2]['items'][0]
        self.assertEqual((album['kind'], album['subtitle'], album['artColor']),
                         ('album', 'Paper Parachutes', '#2a3b4c'))
        self.assertIsNone(shelves[2]['items'][1]['art'])  # no artwork at all
        self.assertEqual(shelves[3]['items'][0]['play'], {'kind': 'station', 'id': 'ra.900000601'})

    def test_nothing(self):
        self.assertEqual(sync.editorial_shelves(None, self.tmp.name), {'shelves': []})
        self.assertEqual(sync.editorial_shelves({'data': ['junk', {}]}, self.tmp.name),
                         {'shelves': []})
        self.assertEqual(sync.editorial_shelves({'errors': [{'status': '500'}]}, self.tmp.name),
                         {'shelves': []})

    def test_a_category_page_keeps_its_keys_and_leaves_untitled_elements_out(self):
        grouping = self.raw['data'][0]
        raw = {'data': [{'id': '900000301', 'type': 'apple-curators',
                         'attributes': {'name': 'Apple Music Folk', 'shortName': 'Folk'},
                         'relationships': {'grouping': {'data': [grouping]}}}]}
        out = sync.category_page(raw, self.tmp.name)
        self.assertEqual(out['title'], 'Folk')
        self.assertEqual([s['key'] for s in out['shelves']],
                         ['cat-best-new-songs', 'cat-new-releases', 'cat-stations'])

    def test_cache_paths(self):
        self.assertEqual(sync.browse_cache_path('/c'), os.path.join('/c', 'browse.json'))
        self.assertEqual(sync.made_for_you_cache_path('/c'),
                         os.path.join('/c', 'made-for-you.json'))


class MadeForYouShelvesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_keeps_the_mixes_and_stations_and_leaves_the_rest_to_home(self):
        recs = [
            recommendation('r-mixes', 'Made for You',
                           [mix('pl.pm-1', 'Favourites Mix'), mix('pl.pm-2', 'Chill Mix')]),
            recommendation('r-recent', 'Recently Played',
                           [album('900000201', 'Lantern Season'),
                            station('ra.q-1', 'Quiet Radio')]),
            recommendation('r-albums', 'New Releases for You',
                           [album('900000202', 'Static Skyline')]),
            recommendation('r-stations', 'Stations for You',
                           [station('ra.u-1', 'Your Station'), station('ra.9', 'Folk Station')]),
            recommendation('r-editorial', 'Playlists for You',
                           [mix('pl.df-1', 'Folk Essentials', 'editorial')]),
            recommendation('r-empty', 'Nothing', []),
        ]
        shelves = sync.made_for_you_shelves(recs, self.tmp.name)
        self.assertEqual([(s['key'], s['title'], [i['title'] for i in s['items']])
                          for s in shelves], [
            ('rec-r-mixes', 'Made for You', ['Favourites Mix', 'Chill Mix']),
            ('rec-r-stations', 'Stations for You', ['Your Station', 'Folk Station']),
        ])
        mix_item = shelves[0]['items'][0]
        self.assertEqual((mix_item['kind'], mix_item['play'], mix_item['artColor']),
                         ('playlist', {'kind': 'playlist', 'id': 'pl.pm-1'}, '#223344'))
        self.assertEqual(mix_item['groups'], [])
        self.assertEqual(shelves[1]['items'][0]['kind'], 'station')

    def test_a_group_recommendation_is_its_members(self):
        recs = [recommendation('r-group', 'For You', [], members=[
            recommendation('r-a', 'Mixes', [mix('pl.pm-1', 'Get Up! Mix')]),
            recommendation('r-b', 'Albums', [album('900000201', 'Lantern Season')]),
        ])]
        shelves = sync.made_for_you_shelves(recs, self.tmp.name)
        self.assertEqual([s['key'] for s in shelves], ['rec-r-a'])

    def test_nothing(self):
        self.assertEqual(sync.made_for_you_shelves(None, self.tmp.name), [])
        self.assertEqual(sync.made_for_you_shelves(['junk', {}], self.tmp.name), [])

    def test_the_recommendations_fixture(self):
        # The invented Home fixture's "Made for You" set has no playlistType: not mixes.
        with open(FIXTURES / 'recommendations.json', encoding='utf-8') as file:
            raw = json.load(file)
        self.assertEqual(sync.made_for_you_shelves(raw['data'], self.tmp.name), [])


if __name__ == '__main__':
    unittest.main()
