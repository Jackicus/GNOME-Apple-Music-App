# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""src/backend/api.py: library ids, endpoints, resource types, pages and Apple's error answers."""

import unittest

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend.api import (api_error, http_status, is_final, is_library_id,
                                    item_endpoint, page_data, resource_type)
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
        for kind in ('video', 'musicVideo', 'music-video'):
            self.assertEqual(item_endpoint(kind, '7', 'us'), '/v1/catalog/us/music-videos/7')
        self.assertEqual(item_endpoint('video', 'i.v', 'us'), '/v1/me/library/music-videos/i.v')
        self.assertEqual(item_endpoint('curator', '5', 'us'), '/v1/catalog/us/curators/5')


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
        self.assertEqual(api_error(cases[0][0], 'item').status, 404)
        self.assertIsNone(api_error(cases[2][0], 'item').status)
        self.assertEqual(api_error({'errors': [{'status': 503}]}, 'x').status, 503)

    def test_anything_else_is_no_error(self):
        for answer in ({'data': []}, {'errors': []}, {'errors': None}, {}, None, [], 'text'):
            self.assertIsNone(api_error(answer, 'item'), answer)


class StatusTest(unittest.TestCase):
    def test_http_status(self):
        for value, status in (('404', 404), (' 429 ', 429), (503, 503), ('4o4', None),
                              ('', None), (None, None), (True, None), (42, None), (4.0, None)):
            self.assertEqual(http_status(value), status, value)

    def test_what_is_final(self):
        def error(code='api', status=None):
            return EngineError(code, 'x', status=status)

        for final in (error(status=400), error(status=403), error(status=404),
                      error('timeout')):
            self.assertTrue(is_final(final), (final.code, final.status))
        for passing in (error(status=429), error(status=500), error(status=503), error()):
            self.assertFalse(is_final(passing), (passing.code, passing.status))


if __name__ == '__main__':
    unittest.main()
