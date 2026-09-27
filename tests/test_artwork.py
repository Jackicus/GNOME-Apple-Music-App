"""The Artwork loader: decoding in threads, the LRU, shared and cancelled requests.

Runs under asyncio.run, without GTK's main loop or a display: textures are made from files.
"""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

import gi

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic.widgets import artwork

gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf  # noqa: E402


def run(coroutine):
    return asyncio.run(coroutine)


async def settle():
    """Let the decodes started so far finish and call back."""
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not asyncio.all_tasks() - {asyncio.current_task()}:
            return


class TestArtwork(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.paths = []
        for n in range(3):
            path = os.path.join(self.temp_dir.name, f'cover{n}.png')
            pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 4 + n, 4 + n)
            pixbuf.fill(0x336699ff)
            pixbuf.savev(path, 'png', [], [])
            self.paths.append(path)
        self.decoded = []
        load = artwork._load

        def counting_load(path):
            self.decoded.append(path)
            return load(path)

        patcher = mock.patch.object(artwork, '_load', counting_load)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_request_decodes_then_get_hits(self):
        loader = artwork.Artwork()
        results = []

        async def go():
            self.assertIsNone(loader.get(self.paths[0]))
            token = loader.request(self.paths[0], results.append)
            self.assertIsNotNone(token)
            self.assertEqual(results, [])  # not before the thread has decoded it
            await settle()

        run(go())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].get_width(), 4)
        self.assertIs(loader.get(self.paths[0]), results[0])
        self.assertEqual(loader.pending(), 0)

    def test_cached_path_answers_at_once(self):
        loader = artwork.Artwork()
        run(self._load(loader, self.paths[0]))
        results = []
        self.assertIsNone(loader.request(self.paths[0], results.append))
        self.assertEqual(len(results), 1)
        self.assertEqual(self.decoded, [self.paths[0]])

    def test_requests_for_one_path_share_a_decode(self):
        loader = artwork.Artwork()
        first, second = [], []

        async def go():
            loader.request(self.paths[1], first.append)
            loader.request(self.paths[1], second.append)
            self.assertEqual(loader.pending(), 1)
            await settle()

        run(go())
        self.assertEqual(self.decoded, [self.paths[1]])
        self.assertIs(first[0], second[0])

    def test_cancelled_request_is_not_called_back(self):
        loader = artwork.Artwork()
        kept, cancelled = [], []

        async def go():
            token = loader.request(self.paths[0], cancelled.append)
            loader.request(self.paths[0], kept.append)
            loader.cancel(token)
            await settle()

        run(go())
        self.assertEqual(cancelled, [])
        self.assertEqual(len(kept), 1)

    def test_cancelling_every_request_drops_the_decode(self):
        loader = artwork.Artwork()
        results = []

        async def go():
            token = loader.request(self.paths[0], results.append)
            loader.cancel(token)
            loader.cancel(token)  # twice is harmless
            loader.cancel(None)
            self.assertEqual(loader.pending(), 0)
            await settle()

        run(go())
        self.assertEqual(results, [])

    def test_missing_file_is_no_artwork(self):
        loader = artwork.Artwork()
        missing = os.path.join(self.temp_dir.name, 'missing.jpg')
        results = []

        async def go():
            loader.request(missing, results.append)
            await settle()

        run(go())
        self.assertEqual(results, [None])
        self.assertIsNone(loader.get(missing))

    def test_no_path_is_no_artwork(self):
        loader = artwork.Artwork()
        results = []
        self.assertIsNone(loader.request(None, results.append))
        self.assertEqual(results, [None])

    def test_least_recently_used_is_evicted(self):
        loader = artwork.Artwork(size=2)

        async def go():
            await self._load(loader, self.paths[0])
            await self._load(loader, self.paths[1])
            loader.get(self.paths[0])  # now paths[1] is the least recently used
            await self._load(loader, self.paths[2])

        run(go())
        self.assertIsNotNone(loader.get(self.paths[0]))
        self.assertIsNone(loader.get(self.paths[1]))
        self.assertIsNotNone(loader.get(self.paths[2]))

    def test_a_failing_callback_does_not_stop_the_others(self):
        loader = artwork.Artwork()
        results = []

        def fail(_texture):
            raise RuntimeError('callback failed')

        async def go():
            loader.request(self.paths[0], fail)
            loader.request(self.paths[0], results.append)
            with self.assertLogs(artwork.log, 'ERROR'):
                await settle()

        run(go())
        self.assertEqual(len(results), 1)

    async def _load(self, loader, path):
        loader.request(path, lambda _texture: None)
        await settle()


class TestArtColour(unittest.TestCase):
    """The hero cards' caption band: an Item's art_color, and the text that reads on it."""

    def test_parses_the_items_colour(self):
        rgba = artwork.art_colour('#0081a7')
        self.assertAlmostEqual(rgba.red, 0)
        self.assertAlmostEqual(rgba.green, 0x81 / 255)
        self.assertAlmostEqual(rgba.blue, 0xa7 / 255)

    def test_no_colour(self):
        self.assertIsNone(artwork.art_colour(None))
        self.assertIsNone(artwork.art_colour(''))
        self.assertIsNone(artwork.art_colour('#nothex'))

    def test_dark_colours_take_white_text(self):
        for colour in ('#000000', '#1a1a1a', '#0077b6', '#0081a7', '#499f68', '#a23b72',
                       '#d97706', '#495057'):
            self.assertTrue(artwork.is_dark(artwork.art_colour(colour)), colour)
        for colour in ('#ffffff', '#f2e8cf', '#ffd166', '#90e0ef'):
            self.assertFalse(artwork.is_dark(artwork.art_colour(colour)), colour)


if __name__ == '__main__':
    unittest.main()
