"""src/backend/api.py: library ids, endpoints, resource types, pages and Apple's error answers."""

import unittest

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend.api import (api_error, is_library_id, item_endpoint, page_data,
                                    resource_type)
from applemusic.backend.errors import EngineError


class LibraryIdTest(unittest.TestCase):
    def test_prefixes(self):
        for item_id in ('l.abc', 'p.xyz', 'r.1', 'i.song1'):
            self.assertTrue(is_library_id(item_id), item_id)
        for item_id in ('1000000002', 'pl.u-1', 'ra.1', '', None):
            self.assertFalse(is_library_id(item_id), item_id)


class EndpointTest(unittest.TestCase):
    def test_library_and_catalog(self):
        self.assertEqual(item_endpoint('album', 'l.abc', 'gb'),
                         '/v1/me/library/albums/l.abc?include=tracks,artists')
        self.assertEqual(item_endpoint('album', '123', 'gb'),
                         '/v1/catalog/gb/albums/123?include=tracks,artists')
        self.assertEqual(item_endpoint('playlist', 'p.xyz', 'us'),
                         '/v1/me/library/playlists/p.xyz?include=tracks')
        self.assertEqual(item_endpoint('playlist', 'pl.u-1', 'us'),
                         '/v1/catalog/us/playlists/pl.u-1?include=tracks')
        self.assertEqual(item_endpoint('artist', 'r.1', 'us'),
                         '/v1/me/library/artists/r.1?include=albums')
        self.assertEqual(item_endpoint('artist', '9', 'us'),
                         '/v1/catalog/us/artists/9?include=albums')
        self.assertEqual(item_endpoint('station', 'ra.1', 'us'), '/v1/catalog/us/stations/ra.1')
        self.assertEqual(item_endpoint('song', 'l.s', 'us'), '/v1/me/library/songs/l.s')
        self.assertEqual(item_endpoint('song', 'i.s', 'us'), '/v1/me/library/songs/i.s')
        self.assertEqual(item_endpoint('song', '5', 'us'), '/v1/catalog/us/songs/5')
        self.assertEqual(item_endpoint('video', '7', 'us'), '/v1/catalog/us/videos/7')


class ResourceTypeTest(unittest.TestCase):
    def test_types(self):
        self.assertEqual(resource_type('song', '1'), 'song')
        self.assertEqual(resource_type('song', 'i.1'), 'library-song')
        self.assertEqual(resource_type('album', 'l.1'), 'library-album')
        self.assertEqual(resource_type('playlist', 'p.1'), 'library-playlist')
        self.assertEqual(resource_type('playlist', 'pl.1'), 'playlist')
        self.assertEqual(resource_type('video', 'i.1'), 'library-music-video')
        self.assertEqual(resource_type('musicVideo', '1'), 'music-video')
        self.assertEqual(resource_type('station', 'ra.1'), 'station')
        for kind, item_id in (('artist', '1'), ('category', '1'), ('song', None)):
            with self.assertRaises(EngineError) as raised:
                resource_type(kind, item_id)
            self.assertEqual(raised.exception.code, 'usage')


class PageDataTest(unittest.TestCase):
    def test_the_resources_of_a_page(self):
        self.assertEqual(page_data({'data': [{'id': '1'}, 'junk', None, {'id': '2'}]}),
                         [{'id': '1'}, {'id': '2'}])
        for answer in ({}, {'data': None}, {'data': 'x'}, None, [], 'text'):
            self.assertEqual(page_data(answer), [], answer)


class ApiErrorTest(unittest.TestCase):
    def test_errors_answers(self):
        cases = (
            ({'errors': [{'status': '404', 'title': 'Not Found', 'detail': 'No such album'}]},
             'item: 404 Not Found: No such album'),
            ({'errors': [{'status': '400', 'title': 'Bad'}]}, 'item: 400 Bad:'),
            ({'errors': [{}]}, 'item: ? error:'),
            ({'errors': ['junk']}, 'item: ? error:'),
            ({'errors': 'Unauthorized'}, 'item: ? error:'),
        )
        for answer, message in cases:
            error = api_error(answer, 'item')
            self.assertIsInstance(error, EngineError, answer)
            self.assertEqual((error.code, error.message), ('api', message), answer)

    def test_anything_else_is_no_error(self):
        for answer in ({'data': []}, {'errors': []}, {'errors': None}, {}, None, [], 'text'):
            self.assertIsNone(api_error(answer, 'item'), answer)


if __name__ == '__main__':
    unittest.main()
