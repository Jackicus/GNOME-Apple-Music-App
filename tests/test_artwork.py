"""The Artwork loader: decoding in threads, the LRU, shared and cancelled requests.

Runs under asyncio.run, without GTK's main loop or a display: textures are made from files.
"""

import asyncio
import concurrent.futures
import gc
import itertools
import os
import tempfile
import threading
import unittest
import warnings
from unittest import mock

import gi

from tests import ROOT  # noqa: F401  registers src/ as applemusic
from tests.gtk import pump

from applemusic.widgets import artwork

gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf  # noqa: E402

# PyGObject 3.56 looks the asyncio loop up through asyncio's policy when a GLib source runs
# (the slots' idles, run by pump()), which Python 3.14 deprecates; the app filters it too.
warnings.filterwarnings('ignore', r"'asyncio\.\w*policy\w*' is deprecated", DeprecationWarning)


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

        def counting_load(path, size=None):
            self.decoded.append(path)
            return load(path, size)

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
        loader = artwork.Artwork(max_entries=2)

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

    def test_decoded_at_the_size_asked_for(self):
        # paths[2] is 6 px square: asked at 3, the texture is 3 px and kept apart from the
        # file's own size; asked at more than the file has, it is decoded as it is.
        loader = artwork.Artwork()
        small, native, big = [], [], []

        async def go():
            loader.request(self.paths[2], small.append, size=3)
            loader.request(self.paths[2], native.append)
            loader.request(self.paths[2], big.append, size=12)
            self.assertEqual(loader.pending(), 3)
            await settle()

        run(go())
        self.assertEqual(small[0].get_width(), 3)
        self.assertEqual(native[0].get_width(), 6)
        self.assertEqual(big[0].get_width(), 6)
        self.assertIs(loader.get(self.paths[2], 3), small[0])
        self.assertIs(loader.get(self.paths[2]), native[0])
        self.assertIsNone(loader.get(self.paths[2], 4))
        self.assertEqual(loader.cached(), (3, (9 + 36 + 36) * 4))

    def test_a_wide_image_is_decoded_by_its_shorter_edge(self):
        # A 16:9 video still fills a square tile by its height (content-fit: cover): decoded
        # to fit its width, it would be drawn blown up.
        wide = os.path.join(self.temp_dir.name, 'wide.png')
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 12, 6)
        pixbuf.fill(0x336699ff)
        pixbuf.savev(wide, 'png', [], [])
        texture = artwork._load(wide, 3)
        self.assertEqual((texture.get_width(), texture.get_height()), (6, 3))
        tall = artwork._load(wide, 6)  # its shorter edge is no bigger: as it is
        self.assertEqual((tall.get_width(), tall.get_height()), (12, 6))

    def test_get_any_is_the_biggest_size_cached(self):
        # A stand-in while another size decodes: the biggest there is, the file's own size
        # counting as the biggest; eviction and clear() forget the sizes.
        loader = artwork.Artwork(budget=150)
        small = []

        async def go():
            self.assertIsNone(loader.get_any(self.paths[2]))
            loader.request(self.paths[2], small.append, size=3)
            await settle()
            self.assertIs(loader.get_any(self.paths[2]), small[0])
            await self._load(loader, self.paths[2])  # its own 6 px: 144 bytes, 36 evicted
            self.assertEqual(loader.get_any(self.paths[2]).get_width(), 6)
            self.assertIsNone(loader.get(self.paths[2], 3))
            loader.clear()
            self.assertIsNone(loader.get_any(self.paths[2]))

        run(go())

    def test_budget_evicts_the_least_recently_used(self):
        # 4, 5 and 6 px squares weigh 64, 100 and 144 bytes: a budget of 250 holds two.
        loader = artwork.Artwork(budget=250)

        async def go():
            await self._load(loader, self.paths[0])
            await self._load(loader, self.paths[1])
            loader.get(self.paths[0])  # now paths[1] is the least recently used
            await self._load(loader, self.paths[2])

        run(go())
        self.assertIsNotNone(loader.get(self.paths[0]))
        self.assertIsNone(loader.get(self.paths[1]))
        self.assertIsNotNone(loader.get(self.paths[2]))
        self.assertEqual(loader.cached(), (2, 64 + 144))
        loader.clear()
        self.assertEqual(loader.cached(), (0, 0))

    def test_decodes_do_not_wait_for_downloads(self):
        # Every download thread busy (a slow connection): a cover on screen still decodes.
        from applemusic import remote

        busy = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.addCleanup(busy.shutdown)
        release = threading.Event()
        self.addCleanup(release.set)
        loader = artwork.Artwork()
        results = []

        async def go():
            with mock.patch.object(remote, '_executor', busy):
                download = asyncio.get_running_loop().run_in_executor(
                    remote._fetch_executor(), release.wait, 5)
                loader.request(self.paths[0], results.append)
                await settle()
                self.assertFalse(download.done())
                release.set()
                await download

        run(go())
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0])

    async def _load(self, loader, path):
        loader.request(path, lambda _texture: None)
        await settle()


class FakeLoader:
    """What an ArtworkSlot asks of the Artwork loader: `cache` {(path, size): texture} answers
    get() and get_any(); request() records (path, size) and waits for answer()."""

    def __init__(self):
        self.cache = {}
        self.requests = []  # (path, size), in order
        self.waiting = {}  # token -> (path, size, callback)
        self.cancelled = []
        self._tokens = itertools.count(1)

    def get(self, path, size=None):
        return self.cache.get((path, size))

    def get_any(self, path):
        return next((texture for (cached, _size), texture in self.cache.items()
                     if cached == path), None)

    def request(self, path, callback, size=None):
        self.requests.append((path, size))
        texture = self.get(path, size)
        if texture is not None:
            callback(texture)
            return None
        token = next(self._tokens)
        self.waiting[token] = (path, size, callback)
        return token

    def cancel(self, token):
        if self.waiting.pop(token, None) is not None:
            self.cancelled.append(token)

    def answer(self, path, texture):
        """Decode `path`: its waiting callback gets `texture` (None: not an image)."""
        for token, (waiting, _size, callback) in list(self.waiting.items()):
            if waiting == path:
                del self.waiting[token]
                callback(texture)


class Shown:
    """The widget side of a slot: what it was given to draw."""

    def __init__(self):
        self.calls = []  # (paintable, found)

    def on_texture(self, paintable, found):
        self.calls.append((paintable, found))

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


class TestArtworkSlot(unittest.TestCase):
    """The slot's decisions, with a fake loader: what is shown at once, what is asked for in
    the idle, and when."""

    def setUp(self):
        self.loader = FakeLoader()
        self.shown = Shown()
        self.slot = artwork.ArtworkSlot(self.shown.on_texture, 40, loader=self.loader)

    def texture(self, name):
        return f'<texture {name}>'  # the slot hands textures on without looking at them

    def test_a_cached_path_answers_without_a_request(self):
        self.loader.cache[('/a/thumb', 80)] = self.texture('a')
        self.slot.set_paths('/a/thumb')
        self.assertEqual(self.shown.calls, [])  # nothing drawn while unmapped
        self.slot.map(scale=2)
        pump()
        self.assertEqual(self.shown.last, (self.texture('a'), True))
        self.assertEqual(self.loader.requests, [])

    def test_a_miss_shows_empty_then_asks_in_an_idle(self):
        self.slot.map()
        self.slot.set_paths('/a/thumb')
        paintable, found = self.shown.last
        self.assertFalse(found)
        self.assertIs(paintable, artwork.empty(40))
        self.assertEqual(self.loader.requests, [])  # not in bind: after the frame
        pump()
        self.assertEqual(self.loader.requests, [('/a/thumb', 40)])
        self.loader.answer('/a/thumb', self.texture('a'))
        self.assertEqual(self.shown.last, (self.texture('a'), True))

    def test_another_size_stands_in_meanwhile(self):
        self.loader.cache[('/a/thumb', 160)] = self.texture('big')
        self.slot.map()
        self.slot.set_paths('/a/art', '/a/thumb')
        self.assertEqual(self.shown.last, (self.texture('big'), True))
        pump()
        self.assertEqual(self.loader.requests, [('/a/art', 40)])

    def test_a_rebind_before_the_idle_asks_for_the_new_paths(self):
        # A's thumbnail is cached (only the cover is asked for, in an idle); before the idle
        # runs the slot is rebound to B, of which nothing is cached: B's cover and then B's
        # thumbnail are asked for, not A's leftover choice.
        self.loader.cache[('/a/thumb', 40)] = self.texture('a')
        self.slot.map()
        self.slot.set_paths('/a/art', '/a/thumb')
        self.assertEqual(self.shown.last, (self.texture('a'), True))
        self.slot.set_paths()
        self.slot.set_paths('/b/art', '/b/thumb')
        pump()
        self.assertEqual(self.loader.requests, [('/b/art', 40)])
        self.loader.answer('/b/art', None)  # B's cover is not on disk
        self.assertEqual(self.loader.requests, [('/b/art', 40), ('/b/thumb', 40)])
        self.loader.answer('/b/thumb', self.texture('b'))
        self.assertEqual(self.shown.last, (self.texture('b'), True))

    def test_a_failure_falls_through_to_the_next_path(self):
        self.slot.map()
        self.slot.set_paths('/a/art', '/a/thumb')
        pump()
        self.loader.answer('/a/art', None)
        self.loader.answer('/a/thumb', None)
        self.assertEqual(self.loader.requests, [('/a/art', 40), ('/a/thumb', 40)])
        self.assertEqual(self.shown.last, (artwork.empty(40), False))
        self.assertEqual(self.loader.waiting, {})

    def test_asks_only_for_the_paths_better_than_the_one_shown(self):
        self.loader.cache[('/a/thumb', 40)] = self.texture('a')
        self.slot.map()
        self.slot.set_paths('/a/art', '/a/thumb')
        pump()
        self.loader.answer('/a/art', None)
        self.assertEqual(self.loader.requests, [('/a/art', 40)])  # not the thumbnail again
        self.assertEqual(self.shown.last, (self.texture('a'), True))

    def test_unmap_cancels_and_lets_go(self):
        self.slot.map()
        self.slot.set_paths('/a/thumb')
        self.slot.unmap()
        pump()
        self.assertEqual(self.loader.requests, [])  # the idle went with the unmap
        self.slot.map()
        pump()
        self.slot.unmap()
        self.assertEqual(self.loader.cancelled, [1])
        self.assertEqual(self.shown.last, (artwork.empty(40), False))
        self.loader.answer('/a/thumb', self.texture('late'))  # a cancelled decode's answer
        self.assertEqual(self.shown.last, (artwork.empty(40), False))

    def test_a_rebind_cancels_the_decode_asked_for(self):
        self.slot.map()
        self.slot.set_paths('/a/thumb')
        pump()
        self.slot.set_paths('/b/thumb')
        self.assertEqual(self.loader.cancelled, [1])
        pump()
        self.loader.answer('/a/thumb', self.texture('a'))  # cancelled: never shown
        self.loader.answer('/b/thumb', self.texture('b'))
        self.assertEqual(self.shown.last, (self.texture('b'), True))
        self.assertNotIn((self.texture('a'), True), self.shown.calls)

    def test_the_same_paths_ask_nothing_until_refreshed(self):
        self.slot.map()
        self.assertTrue(self.slot.set_paths('/a/thumb', None, ''))
        self.assertEqual(self.slot.paths, ('/a/thumb',))
        pump()
        self.loader.answer('/a/thumb', None)
        self.assertFalse(self.slot.set_paths('/a/thumb'))
        pump()
        self.assertEqual(len(self.loader.requests), 1)
        self.slot.refresh()  # the file has arrived since
        pump()
        self.assertEqual(len(self.loader.requests), 2)

    def test_a_new_size_or_scale_asks_again(self):
        self.loader.cache[('/a/thumb', 40)] = self.texture('small')
        self.slot.map()
        self.slot.set_paths('/a/thumb')
        self.slot.set_size(60)
        self.assertEqual(self.shown.last, (self.texture('small'), True))  # stands in
        pump()
        self.assertEqual(self.loader.requests, [('/a/thumb', 60)])
        self.slot.set_scale(2)
        pump()
        self.assertEqual(self.loader.requests[-1], ('/a/thumb', 120))
        self.assertEqual(self.slot.pixels, 120)
        # Unmapped, a new size is only noted: asked for at the next map.
        self.slot.unmap()
        self.slot.set_size(80)
        pump()
        self.assertEqual(len(self.loader.requests), 2)
        self.slot.map(scale=1)
        pump()
        self.assertEqual(self.loader.requests[-1], ('/a/thumb', 80))

    def test_the_widget_is_held_weakly(self):
        shown = Shown()
        slot = artwork.ArtworkSlot(shown.on_texture, 40, loader=self.loader)
        del shown
        gc.collect()
        slot.map()  # nothing to draw into, and no error
        slot.set_paths('/a/thumb')
        pump()
        self.loader.answer('/a/thumb', self.texture('a'))


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


class TestReadableBand(unittest.TestCase):
    """The caption band's colour: the item's, made deep or light enough for its text to read
    at WCAG's AA contrast (4.5:1), the dimmed subtitle's too."""

    # The demo library's art colours and a few more.
    COLOURS = ('#0077b6', '#0081a7', '#0b6e4f', '#1b4965', '#219ebc', '#457b9d', '#499f68',
               '#8d99ae', '#9d4edd', '#a23b72', '#d1495b', '#d90429', '#d97706', '#e76f51',
               '#fca311', '#90e0ef', '#ffd166', '#ffffff', '#000000', '#808080')

    @staticmethod
    def rgb(colour):
        return tuple(int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def test_contrast_ratio_matches_wcag(self):
        self.assertAlmostEqual(artwork.contrast_ratio((1, 1, 1), 1.0, (0, 0, 0)), 21.0)
        self.assertAlmostEqual(artwork.contrast_ratio((0, 0, 0), 1.0, (0, 0, 0)), 1.0)
        # WCAG's example grey: #767676 on white is just over 4.5:1.
        grey = self.rgb('#767676')
        self.assertAlmostEqual(artwork.contrast_ratio(grey, 1.0, (1, 1, 1)), 4.54, places=2)

    def test_every_caption_reads(self):
        for colour in self.COLOURS:
            rgba = artwork.art_colour(colour)
            for opacity in (1.0, 0.75):
                band, dark = artwork.band_colour(rgba, opacity)
                text, text_opacity = artwork.LIGHT_TEXT if dark else artwork.DARK_TEXT
                ratio = artwork.contrast_ratio(text, text_opacity * opacity,
                                               (band.red, band.green, band.blue))
                self.assertGreaterEqual(ratio, artwork.MIN_CONTRAST, (colour, opacity))

    def test_readable_colours_are_kept(self):
        for colour in ('#1b4965', '#000000', '#ffffff', '#ffd166'):
            rgb = self.rgb(colour)
            dark = artwork.is_dark(artwork.art_colour(colour))
            self.assertEqual(artwork.readable_band(rgb, dark, 0.75), rgb, colour)

    def test_white_text_deepens_the_colour(self):
        rgb = self.rgb('#d97706')  # an orange that takes white text
        band = artwork.readable_band(rgb, True, 0.75)
        self.assertLess(artwork.luminance(band), artwork.luminance(rgb))
        # The hue stays: every channel scaled by the same amount.
        self.assertAlmostEqual(band[0] / rgb[0], band[1] / rgb[1], places=6)


if __name__ == '__main__':
    unittest.main()
