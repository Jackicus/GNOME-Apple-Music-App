"""src/lyrics.py: the engine's lyrics answer as a model, and the current line by position."""

import json
import pathlib
import unittest

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic.lyrics import LyricLine, Lyrics, line_index_at, parse_lines

FIXTURE = pathlib.Path(__file__).parent / 'fixtures' / 'lyrics.json'


def fixture():
    with open(FIXTURE, encoding='utf-8') as file:
        return json.load(file)


class ParseTest(unittest.TestCase):
    def test_lines_in_time_order_with_blanks_and_junk_dropped(self):
        answer = {'synced': True, 'lines': [
            {'startMs': 5000, 'endMs': 6000, 'text': 'Second'},
            {'startMs': 1000, 'endMs': 2000, 'text': '  First  '},
            {'startMs': 3000, 'endMs': 4000, 'text': '   '},
            'not a line',
            {'startMs': None, 'endMs': 'x', 'text': 'Untimed'},
            {'text': None},
        ]}
        self.assertEqual(parse_lines(answer), [
            (0, 0, 'Untimed'), (1000, 2000, 'First'), (5000, 6000, 'Second')])

    def test_unsynced_lines_keep_their_order(self):
        answer = {'synced': False, 'lines': [{'text': 'B'}, {'text': 'A'}]}
        self.assertEqual([text for _s, _e, text in parse_lines(answer)], ['B', 'A'])

    def test_nothing_from_nothing(self):
        for answer in (None, {}, {'lines': None}, {'lines': 'x'}, [], 'text'):
            self.assertEqual(parse_lines(answer), [], answer)


class IndexTest(unittest.TestCase):
    STARTS = [1000, 5000, 9000]

    def test_before_the_first_line(self):
        self.assertEqual(line_index_at(self.STARTS, 0), -1)
        self.assertEqual(line_index_at(self.STARTS, 0.999), -1)
        self.assertEqual(line_index_at(self.STARTS, -3), -1)

    def test_on_a_line_and_between_lines(self):
        self.assertEqual(line_index_at(self.STARTS, 1.0), 0)
        self.assertEqual(line_index_at(self.STARTS, 2.5), 0)
        self.assertEqual(line_index_at(self.STARTS, 4.999), 0)  # the gap keeps the line
        self.assertEqual(line_index_at(self.STARTS, 5.0), 1)
        self.assertEqual(line_index_at(self.STARTS, 8.9), 1)

    def test_after_the_last_line(self):
        self.assertEqual(line_index_at(self.STARTS, 9.0), 2)
        self.assertEqual(line_index_at(self.STARTS, 600), 2)

    def test_no_lines(self):
        self.assertEqual(line_index_at([], 4), -1)


class LyricsObjectTest(unittest.TestCase):
    def test_the_fixture(self):
        lyrics = Lyrics(fixture(), '1000000001')
        self.assertTrue(lyrics.synced)
        self.assertEqual(lyrics.catalog_id, '1000000001')
        self.assertEqual(len(lyrics), 24)
        first = lyrics.lines.get_item(0)
        self.assertIsInstance(first, LyricLine)
        self.assertEqual((first.start_ms, first.end_ms), (1200, 4800))
        self.assertEqual(first.text, 'Fog on the water, a bell on the buoy')
        self.assertEqual(lyrics.index_at(0), -1)
        self.assertEqual(lyrics.index_at(1.2), 0)
        self.assertEqual(lyrics.index_at(24.0), 5)
        self.assertEqual(lyrics.index_at(46.5), 10)
        self.assertEqual(lyrics.index_at(500), 23)
        self.assertEqual(lyrics.start_of(5), 23.1)
        self.assertEqual(lyrics.start_of(99), 0.0)
        self.assertEqual(lyrics.text.splitlines()[4], 'Low tide warning on the radio')

    def test_unsynced_lyrics_have_text_and_no_current_line(self):
        lyrics = Lyrics({'synced': False, 'lines': [{'text': 'One'}, {'text': 'Two'}]})
        self.assertFalse(lyrics.synced)
        self.assertEqual(len(lyrics), 2)
        self.assertEqual(lyrics.text, 'One\nTwo')
        self.assertEqual(lyrics.index_at(30), -1)

    def test_synced_without_lines_is_not_synced(self):
        lyrics = Lyrics({'synced': True, 'lines': []})
        self.assertFalse(lyrics.synced)
        self.assertEqual(len(lyrics), 0)
        self.assertEqual(lyrics.text, '')


if __name__ == '__main__':
    unittest.main()
