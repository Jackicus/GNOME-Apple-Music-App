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


class TestRemoteArt(unittest.TestCase):
    """fetch_remote: the URL re-sized, the file under <cache>/remote-art/, shared fetches."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.temp_dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fetched = []

        def fake_cache_artwork(url, cache_dir, timeout=10.0, dest_path=None):
            self.fetched.append(url)
            if 'missing' in url:
                return None
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, 'wb') as file:
                file.write(b'jpeg')
            return dest_path

        patcher = mock.patch.object(artwork.backend, 'cache_artwork', fake_cache_artwork)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sized_url(self):
        self.assertEqual(artwork.sized_url('https://x.invalid/a/256x256bb.jpg', 640),
                         'https://x.invalid/a/640x640bb.jpg')
        self.assertEqual(artwork.sized_url('https://x.invalid/a/{w}x{h}bb.jpg', 320),
                         'https://x.invalid/a/320x320bb.jpg')
        self.assertEqual(artwork.sized_url('https://x.invalid/a/600x600bb-60.webp', 640),
                         'https://x.invalid/a/640x640bb-60.webp')
        self.assertEqual(artwork.sized_url('https://x.invalid/a/cover.jpg', 640),
                         'https://x.invalid/a/cover.jpg')
        self.assertIsNone(artwork.sized_url(None, 640))
        self.assertIsNone(artwork.sized_url('', 640))

    def test_fetches_into_remote_art_once(self):
        loader = artwork.Artwork()
        url = 'https://x.invalid/a/256x256bb.jpg'

        async def go():
            first, second = await asyncio.gather(loader.fetch_remote(url, 640),
                                                 loader.fetch_remote(url, 640))
            self.assertEqual(first, second)
            self.assertEqual(os.path.dirname(first),
                             os.path.join(self.temp_dir.name, 'remote-art'))
            self.assertTrue(os.path.exists(first))
            self.assertEqual(self.fetched, ['https://x.invalid/a/640x640bb.jpg'])
            # On disk already: answered without a fetch.
            self.assertEqual(await loader.fetch_remote(url, 640), first)
            self.assertEqual(len(self.fetched), 1)
            # Another size is another file.
            other = await loader.fetch_remote(url, 320)
            self.assertNotEqual(other, first)
            self.assertEqual(self.fetched[-1], 'https://x.invalid/a/320x320bb.jpg')
        run(go())

    def test_failures_answer_none(self):
        loader = artwork.Artwork()

        async def go():
            self.assertIsNone(await loader.fetch_remote(None, 640))
            self.assertIsNone(await loader.fetch_remote('https://x.invalid/missing/1x1.jpg', 640))
        run(go())

    def test_remote_item_names_the_remote_art_files(self):
        hit = {'id': '1', 'kind': 'album', 'title': 'A', 'thumb': None,
               'art': 'https://x.invalid/a/320x320bb.jpg'}
        item = artwork.remote_item(hit)
        self.assertIsNot(item, hit)
        self.assertEqual(item['thumbUrl'], 'https://x.invalid/a/320x320bb.jpg')
        self.assertEqual(item['artUrl'], 'https://x.invalid/a/640x640bb.jpg')
        self.assertEqual(item['thumb'], artwork.remote_art_path(hit['art'], 320))
        self.assertEqual(item['art'], artwork.remote_art_path(hit['art'], 640))
        self.assertEqual(os.path.dirname(item['thumb']),
                         os.path.join(self.temp_dir.name, 'remote-art'))
        self.assertNotEqual(item['thumb'], item['art'])
        # A thumbnail the sync has on disk is kept; the cover still comes from the URL.
        kept = artwork.remote_item(dict(hit, thumb='/on/disk.jpg'))
        self.assertEqual(kept['thumb'], '/on/disk.jpg')
        self.assertEqual(kept['art'], item['art'])
        # Artwork on disk, or none: the dict as it is.
        for data in ({'art': '/cache/art/x.jpg', 'thumb': '/cache/thumb/x.jpg'},
                     {'art': None, 'thumb': None}, {}, None):
            self.assertIs(artwork.remote_item(data), data)

    def test_fetch_thumb(self):
        from applemusic.library import Item

        loader = artwork.Artwork()
        item = Item(artwork.remote_item({'id': '1', 'kind': 'album', 'title': 'A',
                                         'art': 'https://x.invalid/a/320x320bb.jpg'}))
        bare = Item({'id': '2', 'kind': 'album', 'title': 'B'})
        failing = Item(artwork.remote_item({'id': '3', 'kind': 'album', 'title': 'C',
                                            'art': 'https://x.invalid/missing/320x320bb.jpg'}))

        async def go():
            self.assertTrue(artwork.thumb_missing(item))
            self.assertTrue(await loader.fetch_thumb(item))
            self.assertTrue(os.path.exists(item.thumb))
            self.assertFalse(artwork.thumb_missing(item))
            self.assertEqual(self.fetched, ['https://x.invalid/a/320x320bb.jpg'])
            self.assertTrue(await loader.fetch_thumb(item))  # on disk: no fetch
            self.assertEqual(len(self.fetched), 1)
            self.assertFalse(artwork.thumb_missing(bare))
            self.assertFalse(await loader.fetch_thumb(bare))
            self.assertFalse(await loader.fetch_thumb(failing))
        run(go())


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
