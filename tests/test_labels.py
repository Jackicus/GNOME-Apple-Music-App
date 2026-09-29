"""widgets/labels.py: what tiles and rows read to assistive technology."""

import unittest

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic.library import Item, Track
from applemusic.widgets import labels


class AccessibleLabelTest(unittest.TestCase):
    def test_an_album_reads_its_title_and_subtitle(self):
        album = Item({'id': 'l.a1', 'kind': 'album', 'title': 'Invented Album',
                      'subtitle': 'Invented Artist'})
        self.assertEqual(labels.accessible_label(album), 'Invented Album, Invented Artist')

    def test_an_artist_reads_its_name(self):
        artist = Item({'id': 'l.r1', 'kind': 'artist', 'title': 'Invented Artist',
                       'subtitle': '3 albums'})
        self.assertEqual(labels.accessible_label(artist), 'Invented Artist')
        album = Item({'id': 'l.a1', 'kind': 'album', 'title': 'A', 'subtitle': 'B'})
        self.assertEqual(labels.accessible_label(album, artist=True), 'A')

    def test_no_subtitle_reads_the_title(self):
        folder = Item({'id': 'l.f1', 'kind': 'folder', 'title': 'Invented Folder'})
        self.assertEqual(labels.accessible_label(folder), 'Invented Folder')


class TrackLabelTest(unittest.TestCase):
    def track(self, **raw):
        return Track(dict({'id': 'i.1', 'title': 'T'}, **raw))

    def test_every_part(self):
        self.assertEqual(labels.track_label(self.track(artist='A', album='B')), 'T, A, B')

    def test_a_missing_part_is_left_out(self):
        self.assertEqual(labels.track_label(self.track(artist='A')), 'T, A')
        self.assertEqual(labels.track_label(self.track(album='B')), 'T, B')
        self.assertEqual(labels.track_label(self.track()), 'T')  # not "T, , "

    def test_explicit(self):
        self.assertEqual(labels.track_label(self.track(artist='A', explicit=True)),
                         'T, A, explicit')

    def test_the_parts_the_row_hides_are_not_read(self):
        track = self.track(artist='A', album='B')
        self.assertEqual(labels.track_label(track, show_artist=False), 'T, B')
        self.assertEqual(labels.track_label(track, show_album=False), 'T, A')


if __name__ == '__main__':
    unittest.main()
