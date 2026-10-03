# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""What the library holds of an artist (applemusic.discography): their albums, a stand-in
album of loose songs among them, the albums made up for groups the library lacks, and what
Play queues. A stand-in library of invented items; no display."""

import unittest

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic.backend.normalize import stand_in_album_id
from applemusic.discography import albums, library_artist, play_target, song_ids
from applemusic.library import Item


def tracks(prefix, count):
    return [{'id': f'i.{prefix}{number}', 'title': f'Song {number}',
             'artist': 'The Invented Band'} for number in range(count)]


def album(album_id, title, year, entries, play=None):
    play = play or {'kind': 'album', 'id': album_id}
    return Item({'id': album_id, 'kind': 'album', 'title': title,
                 'subtitle': 'The Invented Band', 'year': year, 'play': play,
                 'groups': [{'name': 'Disc 1', 'play': play, 'entries': entries}]})


TIDEWATER = album('l.alb1', 'Tidewater', 2019, tracks('a', 3))
LANTERNS = album('l.alb2', 'Lanterns', 2023, tracks('b', 2))
# A song added without its album: the sync's stand-in album, which plays its songs by id.
LOOSE_PLAY = {'kind': 'songs', 'id': 'i.c0'}
LOOSE = album(stand_in_album_id('Harbour Single', 'The Invented Band'), 'Harbour Single',
              2024, tracks('c', 1), play=LOOSE_PLAY)
OTHER = album('l.alb9', 'Elsewhere', 2020, tracks('d', 1))


def group(item, entries=()):
    """An artist's group for an album, its tracks dropped as the library drops them for the
    albums it has."""
    return {'name': item.title, 'play': dict(item.play), 'entries': list(entries)}


def artist(groups, artist_id='l.art_band', name='The Invented Band'):
    return Item({'id': artist_id, 'kind': 'artist', 'title': name, 'play': {},
                 'groups': groups})


class FakeLibrary:
    def __init__(self, albums_=(), artists=()):
        self.albums = list(albums_)
        self.artists = list(artists)

    def by_id(self, kind, item_id):
        found = self.albums if kind == 'album' else self.artists
        return next((item for item in found if item.id == item_id), None)


class LibraryArtistTest(unittest.TestCase):
    def test_the_library_artist_itself_or_by_name(self):
        band = artist([])
        library = FakeLibrary(artists=[band])
        self.assertIs(library_artist(library, band), band)
        # A catalog artist's page: the library's artist of that name, case and accents aside.
        catalog = Item({'id': '1000000500', 'kind': 'artist', 'title': 'THE INVENTED BÄND'})
        self.assertIs(library_artist(library, catalog), band)
        # Only the same name: not one it starts with ("A feat. B" is a song's artist).
        catalog.title = 'The Invented Band & Friends'
        self.assertIsNone(library_artist(library, catalog))
        self.assertIsNone(library_artist(library, Item({'id': '1', 'kind': 'artist',
                                                        'title': 'Nobody'})))
        self.assertIsNone(library_artist(library, TIDEWATER))  # not an artist
        self.assertIsNone(library_artist(library, None))


class AlbumsTest(unittest.TestCase):
    def test_the_library_albums_in_the_artists_order(self):
        band = artist([group(TIDEWATER), group(LANTERNS)])
        library = FakeLibrary([OTHER, LANTERNS, TIDEWATER], [band])
        self.assertEqual(albums(library, band), [TIDEWATER, LANTERNS])
        self.assertEqual(albums(library, None), [])

    def test_a_loose_song_is_its_stand_in_album(self):
        # The group plays the songs by id; the album is found by the id the sync made up.
        band = artist([group(TIDEWATER), group(LOOSE, LOOSE.groups[0].raw['entries'])])
        library = FakeLibrary([TIDEWATER, LOOSE], [band])
        self.assertEqual(albums(library, band), [TIDEWATER, LOOSE])
        # A stand-in under another id (an older sync's): found by what it plays.
        renamed = album('l.alb_renamed', 'Harbour Single', 2024, tracks('c', 1),
                        play=LOOSE_PLAY)
        library = FakeLibrary([TIDEWATER, renamed], [band])
        self.assertEqual(albums(library, band), [TIDEWATER, renamed])

    def test_a_group_without_its_album_is_an_album_of_its_tracks(self):
        missing = {'name': 'Undertow', 'play': {'kind': 'album', 'id': 'l.gone'},
                   'entries': tracks('e', 2)}
        empty = {'name': 'Nothing', 'play': {'kind': 'album', 'id': 'l.none'}, 'entries': []}
        band = artist([missing, empty, group(TIDEWATER)])
        library = FakeLibrary([TIDEWATER], [band])
        made = {}
        found = albums(library, band, made)
        self.assertEqual(len(found), 2)  # nothing to show for the empty one
        undertow = found[0]
        self.assertEqual((undertow.kind, undertow.id, undertow.title, undertow.subtitle),
                         ('album', 'l.gone', 'Undertow', 'The Invented Band'))
        self.assertEqual([undertow.groups[0].entries.get_item(n).id for n in range(2)],
                         ['i.e0', 'i.e1'])
        self.assertIs(found[1], TIDEWATER)
        # Asked again with the same `made`, the same Item: a tile keeps it.
        self.assertIs(albums(library, band, made)[0], undertow)
        self.assertIsNot(albums(library, band)[0], undertow)

    def test_an_album_twice_is_shown_once(self):
        band = artist([group(TIDEWATER), group(TIDEWATER)])
        self.assertEqual(albums(FakeLibrary([TIDEWATER], [band]), band), [TIDEWATER])


class PlayTest(unittest.TestCase):
    def test_the_songs_album_by_album_in_the_order_given(self):
        self.assertEqual(song_ids([LANTERNS, TIDEWATER]),
                         ['i.b0', 'i.b1', 'i.a0', 'i.a1', 'i.a2'])
        self.assertEqual(play_target([LANTERNS, LOOSE]),
                         {'kind': 'songs', 'id': 'i.b0,i.b1,i.c0'})

    def test_at_most_the_limit_and_nothing_without_songs(self):
        self.assertEqual(song_ids([TIDEWATER, LANTERNS], limit=4),
                         ['i.a0', 'i.a1', 'i.a2', 'i.b0'])
        empty = Item({'id': 'l.empty', 'kind': 'album', 'title': 'Empty', 'groups': []})
        self.assertIsNone(play_target([empty]))
        self.assertIsNone(play_target([]))


if __name__ == '__main__':
    unittest.main()
