# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Where Go to Album and Go to Artist go (applemusic.related): the library's album and artist
of a song or an album, what the engine is asked by, and what its answer opens. A stand-in
library of invented items; no display."""

import unittest

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic.library import Item, Track
from applemusic.related import (artist_named, catalog_target, from_answer, has_album,
                                has_artist, library_album, library_artist, same_page, shows)


def album(album_id, title, artist, tracks=(), catalog_id=None):
    return Item({'id': album_id, 'kind': 'album', 'title': title, 'subtitle': artist,
                 'catalogId': catalog_id, 'play': {'kind': 'album', 'id': album_id},
                 'groups': [{'name': 'Disc 1', 'play': {'kind': 'album', 'id': album_id},
                             'entries': [dict(entry) for entry in tracks]}]})


def artist(artist_id, name):
    return Item({'id': artist_id, 'kind': 'artist', 'title': name, 'play': {}})


TIDEWATER = album('l.alb1', 'Tidewater', 'The Invented Band',
                  [{'id': 'i.s1', 'catalogId': '1000000101', 'title': 'Harbour Lights'}],
                  catalog_id='1000000100')
OTHER_TIDEWATER = album('l.alb2', 'Tidewater', 'Mara Lind',
                        [{'id': 'i.s2', 'title': 'Undertow'}])
COMPILATION = album('l.alb3', 'Summer Hits', 'Various Artists',
                    [{'id': 'i.s3', 'catalogId': '1000000103', 'title': 'Shoreline'}])
BAND = artist('l.art_band', 'The Invented Band')
MARA = artist('l.art_mara', 'Mara Lind')
MARA_TRIO = artist('l.art_trio', 'Mara Lind Trio')
VARIOUS = artist('l.art_various', 'Various Artists')


class FakeLibrary:
    def __init__(self):
        self.albums = [TIDEWATER, OTHER_TIDEWATER, COMPILATION]
        self.artists = [BAND, MARA, MARA_TRIO, VARIOUS]

    def by_id(self, kind, item_id):
        found = self.albums if kind == 'album' else self.artists
        return next((item for item in found if item.id == item_id), None)


def track(album_name='', artist_name='', play=None, **raw):
    return Track(dict(raw, album=album_name, artist=artist_name), play=play)


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.library = FakeLibrary()

    def test_the_album_a_group_plays(self):
        # An album page's or the Songs page's track: its group's album, whatever its names.
        song = track('Renamed', 'Nobody', play={'kind': 'album', 'id': 'l.alb2'}, id='i.s2')
        self.assertIs(library_album(self.library, song), OTHER_TIDEWATER)

    def test_the_album_by_title_and_artist(self):
        # A playlist's track: the album of that title by its artist, case and accents aside.
        song = track('TIDEWATER', 'mara  lind', play={'kind': 'playlist', 'id': 'p.pl1'})
        self.assertIs(library_album(self.library, song), OTHER_TIDEWATER)
        featured = track('Tidewater', 'The Invented Band feat. Mara Lind')
        self.assertIs(library_album(self.library, featured), TIDEWATER)
        # Neither artist: none of the two, unless one holds the song.
        self.assertIsNone(library_album(self.library, track('Tidewater', 'Someone')))
        held = track('Tidewater', 'Someone', id='i.s9', catalogId='1000000101')
        self.assertIs(library_album(self.library, held), TIDEWATER)

    def test_the_best_of_same_titled_albums(self):
        # Two "Live" albums: the exact artist over the first one credited, and the one
        # holding the song over both.
        lind = album('l.alb4', 'Live', 'Mara Lind')
        trio = album('l.alb5', 'Live', 'Mara Lind & The Tide',
                     [{'id': 'i.s5', 'title': 'Tide'}])
        self.library.albums += [lind, trio]
        self.assertIs(library_album(self.library, track('Live', 'Mara Lind & The Tide')), trio)
        self.assertIs(library_album(self.library, track('Live', 'Mara Lind feat. Someone')),
                      lind)
        self.assertIs(library_album(self.library, track('Live', 'Mara Lind', id='i.s5')), trio)

    def test_a_compilation_holds_the_song(self):
        song = track('Summer Hits', 'Paper Parachutes', id='1000000103')
        self.assertIs(library_album(self.library, song), COMPILATION)
        self.assertIsNone(library_album(self.library, track('Winter Hits', 'Paper Parachutes')))
        self.assertIsNone(library_album(self.library, track('', 'Mara Lind')))

    def test_a_song_item_by_its_album_name(self):
        top_song = Item({'id': '1000000110', 'kind': 'song', 'title': 'Undertow',
                         'subtitle': 'Mara Lind', 'album': 'Tidewater'})
        self.assertIs(library_album(self.library, top_song), OTHER_TIDEWATER)
        self.assertIsNone(library_album(self.library, TIDEWATER))  # an album is on no album

    def test_the_artist_by_name(self):
        self.assertIs(artist_named(self.library, 'mara lind'), MARA)
        self.assertIs(artist_named(self.library, 'Mara Lind Trio'), MARA_TRIO)
        self.assertIsNone(artist_named(self.library, 'Mara'))
        self.assertIsNone(artist_named(self.library, ''))

    def test_the_first_artist_credited(self):
        for name in ('Mara Lind feat. Paper Parachutes', 'Mara Lind & Paper Parachutes',
                     'Mara Lind, Someone and Someone Else', 'Mara Lind x Someone',
                     'Mara Lind with the Harbour Strings', 'Mara Lind ft. Someone'):
            with self.subTest(name=name):
                self.assertIs(artist_named(self.library, name), MARA)
        # The longest name the credit starts with; a longer word is another artist.
        self.assertIs(artist_named(self.library, 'Mara Lind Trio & Friends'), MARA_TRIO)
        self.assertIsNone(artist_named(self.library, 'Mara Lindqvist'))
        self.assertIsNone(artist_named(self.library, 'Mara Lind Tribute Band'))
        # A word joiner after a space only: "Max" is not "Ma" and "x".
        self.library.artists.append(artist('l.art_ma', 'Ma'))
        self.assertIsNone(artist_named(self.library, 'Max'))
        self.assertIsNone(artist_named(self.library, 'Mand'))
        self.assertIs(artist_named(self.library, 'Ma x Max'), self.library.artists[-1])
        self.assertIs(artist_named(self.library, 'Ma, Max'), self.library.artists[-1])

    def test_the_artist_of_each_kind(self):
        self.assertIs(library_artist(self.library, track('X', 'Mara Lind')), MARA)
        self.assertIs(library_artist(self.library, TIDEWATER), BAND)
        # An artist page's album, subtitled with its year, by its artistName.
        release = Item({'id': '1000000120', 'kind': 'album', 'title': 'Ladders',
                        'subtitle': '2026', 'artistName': 'Mara Lind'})
        self.assertIs(library_artist(self.library, release), MARA)
        playlist = Item({'id': 'p.1', 'kind': 'playlist', 'title': 'Mine',
                         'subtitle': 'Mara Lind'})
        self.assertIsNone(library_artist(self.library, playlist))  # a curator, not an artist


class OfferTest(unittest.TestCase):
    def test_what_is_offered(self):
        self.assertTrue(has_album(track('Tidewater', 'Mara Lind')))
        self.assertTrue(has_album(track(play={'kind': 'album', 'id': 'l.alb1'}, id='i.s1')))
        self.assertTrue(has_album(track(id='1000000130')))  # the engine can say
        self.assertFalse(has_album(track(id='i.s4')))  # an upload with no names
        self.assertFalse(has_album(track('Live', 'Mara Lind', type='music-videos')))
        self.assertFalse(has_album(TIDEWATER))
        self.assertTrue(has_artist(TIDEWATER))
        self.assertTrue(has_artist(track(artist_name='Mara Lind')))
        self.assertFalse(has_artist(track(id='i.s4')))
        self.assertFalse(has_artist(MARA))

    def test_catalog_targets(self):
        self.assertEqual(catalog_target(track(id='i.s1', catalogId='1000000101')),
                         ('song', '1000000101'))
        self.assertEqual(catalog_target(track(id='1000000102')), ('song', '1000000102'))
        self.assertEqual(catalog_target(track(id='i.v1', catalogId='1000000140',
                                              type='library-music-videos')),
                         ('video', '1000000140'))
        self.assertIsNone(catalog_target(track(id='i.s4')))
        self.assertEqual(catalog_target(TIDEWATER), ('album', '1000000100'))
        self.assertIsNone(catalog_target(OTHER_TIDEWATER))  # the library's, no catalog id
        self.assertIsNone(catalog_target(MARA))

    def test_where_it_is_already(self):
        own = track(artist_name='Mara Lind feat. Someone', play={'kind': 'album', 'id': 'l.alb1'},
                    id='i.s1')
        self.assertTrue(shows(TIDEWATER, own, 'album'))
        self.assertFalse(shows(OTHER_TIDEWATER, own, 'album'))
        self.assertFalse(shows(MARA, own, 'album'))
        self.assertTrue(shows(MARA, own, 'artist'))
        self.assertFalse(shows(BAND, own, 'artist'))
        self.assertFalse(shows(None, own, 'artist'))
        # The item playing has no group: its album's page by name and artist, or holding it.
        playing = track('tidewater', 'The Invented Band', id='i.s9')
        self.assertTrue(shows(TIDEWATER, playing, 'album'))
        self.assertFalse(shows(OTHER_TIDEWATER, playing, 'album'))
        held = track('Tidewater', 'Someone', id='i.s9', catalogId='1000000101')
        self.assertTrue(shows(TIDEWATER, held, 'album'))
        self.assertFalse(shows(TIDEWATER, track('Tidewater', 'Someone', id='i.s9'), 'album'))

    def test_the_same_page(self):
        self.assertTrue(same_page(MARA, MARA))
        self.assertTrue(same_page(MARA, artist('1000000150', 'Mara Lind')))
        self.assertTrue(same_page(TIDEWATER, album('l.alb1', 'Tidewater', '')))
        self.assertFalse(same_page(TIDEWATER, OTHER_TIDEWATER))
        self.assertFalse(same_page(None, MARA))


class AnswerTest(unittest.TestCase):
    def setUp(self):
        self.library = FakeLibrary()

    def test_the_album_answered(self):
        answer = {'album': {'id': '1000000160', 'kind': 'album', 'title': 'Coastal',
                            'subtitle': 'Paper Parachutes',
                            'art': 'https://is1-ssl.mzstatic.com/image/thumb/a/100x100bb.jpg'},
                  'artists': []}
        found = from_answer(self.library, answer, 'album')
        self.assertEqual((found.kind, found.id, found.title), ('album', '1000000160', 'Coastal'))
        self.assertTrue(found.raw.get('artUrl'))  # its cover fetched as a search hit's
        # The library's own, when it has that catalog album.
        answer['album']['id'] = '1000000100'
        self.assertIs(from_answer(self.library, answer, 'album'), TIDEWATER)
        self.assertIsNone(from_answer(self.library, {'album': None, 'artists': []}, 'album'))
        self.assertIsNone(from_answer(self.library, None, 'album'))

    def test_the_artist_answered(self):
        answer = {'album': None, 'artists': [
            {'id': '1000000170', 'kind': 'artist', 'title': 'Paper Parachutes'},
            {'id': '1000000171', 'kind': 'artist', 'title': 'Harbour Strings'}]}
        self.assertEqual(from_answer(self.library, answer, 'artist', 'Harbour  Strings').id,
                         '1000000171')
        self.assertEqual(from_answer(self.library, answer, 'artist', 'Someone').id,
                         '1000000170')  # none of that name: the first
        # The library's own of the same name.
        answer['artists'][0]['title'] = 'Mara Lind'
        self.assertIs(from_answer(self.library, answer, 'artist'), MARA)
        self.assertIsNone(from_answer(self.library, {'artists': [{}]}, 'artist'))


if __name__ == '__main__':
    unittest.main()
