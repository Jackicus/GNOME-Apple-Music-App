"""Artwork: covers decoded off the main thread into a small LRU of Gdk.Textures.

One loader serves the whole process (get_default()). A widget first asks get(path), which
answers from the cache at once; on a miss it calls request(path, callback), which decodes the
file in a thread and calls back on the main loop with the texture, or with None when the path is
not on disk or not an image (a missing file is no artwork, as the backend README says). Requests
for a path already being decoded share one decode. request() returns a token; a widget that is
recycled or scrolled away passes it to cancel(), after which its callback never runs, so a tile
never shows the cover of the item it showed before. A decode nothing waits for any more is
cancelled: one still queued for a thread never runs, which is what keeps a fast scroll through
thousands of tiles from decoding every cover it passes.

Widgets hold a texture only while they are on screen (the grid tiles let go of theirs when
unmapped), so the cache's size bounds what the artwork costs: 200 textures of 320×320 are about
80 MB.

A sync fetches only thumbnails; the covers (an Item's `art`, 640 px) are fetched here on
demand, by the pages that show one: `await fetch_cover(item)` downloads it into <cache>/art/
in a thread, from the `artUrl` the sync kept in the Item, and answers True when a file arrived
(the page then shows it). Artwork the library does not name (the item playing, a search hit)
comes through `await fetch_remote(url, size)`: the URL re-sized to `size` px square, fetched
into <cache>/remote-art/ (named by the sized URL's hash; the sync trims that directory to a
budget) and answered as its path. Nothing here needs the engine.

The engine's search, suggest, category, browse and made-for-you answers name a small catalog
URL as an item's `art` when the sync has not fetched its cover (and no `thumb`): `remote_item(
data)` gives such a dict the paths fetch_remote() would use (`thumb` at THUMB_SIZE, `art` at
COVER_SIZE, with `thumbUrl` and `artUrl` to fetch them by), so a tile shows the thumbnail once
`await fetch_thumb(item)` has brought it and a detail page's fetch_cover() the cover.
"""

import asyncio
import itertools
import logging
import os
import re
from collections import OrderedDict

import gi

gi.require_version('Gdk', '4.0')

from gi.repository import Gdk, Gio, GLib  # noqa: E402

from ..backend import config  # noqa: E402
from ..backend import sync as backend  # noqa: E402

log = logging.getLogger(__name__)

CACHE_SIZE = 200

# The size at the end of an Apple artwork URL ("…/256x256bb.jpg", "…/600x600bb-60.jpg"), to
# re-size one; a template ("{w}x{h}") is filled by the backend first.
ART_SIZE_RE = re.compile(r'/(\d+)x(\d+)([a-z]{0,3}(?:-\d+)?)(\.[A-Za-z0-9]+)$')


def sized_url(url, size):
    """`url` naming a `size`×`size` px image: Apple's templates and sized URLs both take it;
    any other URL is left as it is."""
    if not url:
        return None
    url = backend.template_artwork_url(str(url), size, size)
    return ART_SIZE_RE.sub(lambda match: f'/{size}x{size}{match.group(3)}{match.group(4)}', url)


def remote_art_path(url, size=config.COVER_SIZE):
    """Where fetch_remote keeps the image at `url` re-sized to `size`: under <cache>/remote-art/,
    named by the sized URL's hash. None without a URL."""
    url = sized_url(url, size)
    if not url:
        return None
    return os.path.join(str(config.cache_dir()), 'remote-art', backend.artwork_filename(url))


def is_url(value):
    return isinstance(value, str) and value.startswith(('https://', 'http://'))


def remote_item(data):
    """An Item dict from the engine's search, category, browse or made-for-you answers with
    its artwork where this app fetches it: when `art` is a catalog URL (the sync has not
    fetched the cover), a copy naming fetch_remote()'s files instead (`thumb` at THUMB_SIZE
    unless one is on disk, `art` at COVER_SIZE) and the URLs to fetch them by (`thumbUrl`,
    `artUrl`, for fetch_thumb() and fetch_cover()). A dict whose artwork is on disk, or that
    has none, is returned as it is."""
    url = data.get('art') if isinstance(data, dict) else None
    if not is_url(url):
        return data
    item = dict(data)
    item['thumbUrl'] = sized_url(url, config.THUMB_SIZE)
    item['artUrl'] = sized_url(url, config.COVER_SIZE)
    if not item.get('thumb'):
        item['thumb'] = remote_art_path(url, config.THUMB_SIZE)
    item['art'] = remote_art_path(url, config.COVER_SIZE)
    return item


def thumb_missing(item):
    """Whether an Item (remote_item's) has a thumbnail to fetch that is not on disk yet."""
    raw = item.raw if isinstance(item.raw, dict) else {}
    return bool(item.thumb and raw.get('thumbUrl') and backend._art_missing(item.thumb))


class Artwork:
    """An LRU of decoded textures by path, and the decodes in flight."""

    def __init__(self, size=CACHE_SIZE):
        self.size = size
        self._textures = OrderedDict()  # path -> Gdk.Texture, least recently used first
        self._decodes = {}  # path -> (asyncio.Task, {token: callback})
        self._paths = {}  # token -> path, for the requests still waiting
        self._tokens = itertools.count(1)
        self._fetches = {}  # cover path -> the asyncio.Task fetching it

    async def fetch_cover(self, item):
        """The Item's cover (`art`) on disk: True when this call downloaded it (from the
        Item's `artUrl`, in a thread), False when it was there already, the Item has no
        cover URL, or the fetch failed (logged; the page keeps the thumbnail). Concurrent
        calls for one cover share the download."""
        path = item.art
        url = item.raw.get('artUrl') if isinstance(item.raw, dict) else None
        if not path or not url:
            return False
        task = self._fetches.get(path)
        if task is None:
            task = asyncio.get_event_loop().create_task(self._fetch(path, url))
            self._fetches[path] = task
            task.add_done_callback(lambda _task: self._fetches.pop(path, None))
        return await asyncio.shield(task)

    async def fetch_remote(self, url, size=config.COVER_SIZE):
        """The image at `url` (an Apple artwork URL of any size), re-sized to `size` px
        square and on disk under <cache>/remote-art/: its path, once it is there (fetched
        in a thread when it is not), or None without a URL or when the fetch failed
        (logged). Concurrent calls for one image share the download. The player bar asks
        at the cover size, so the Now Playing sheet finds the same file."""
        path = remote_art_path(url, size)
        if not path:
            return None
        if not backend._art_missing(path):
            return path
        task = self._fetches.get(path)
        if task is None:
            task = asyncio.get_event_loop().create_task(self._fetch(path, sized_url(url, size)))
            self._fetches[path] = task
            task.add_done_callback(lambda _task: self._fetches.pop(path, None))
        return path if await asyncio.shield(task) or not backend._art_missing(path) else None

    async def fetch_thumb(self, item):
        """The Item's thumbnail on disk, for an item from a search or browse answer
        (remote_item()): True once it is there (fetched in a thread when it was not), False
        when the item names none or the fetch failed."""
        raw = item.raw if isinstance(item.raw, dict) else {}
        url = raw.get('thumbUrl')
        if not item.thumb or not url:
            return False
        if not backend._art_missing(item.thumb):
            return True
        return await self.fetch_remote(url, config.THUMB_SIZE) is not None

    async def _fetch(self, path, url):
        def fetch():
            if not backend._art_missing(path):
                return False
            return backend.cache_artwork(url, str(config.cache_dir()), dest_path=path) is not None
        try:
            fetched = await asyncio.to_thread(fetch)
        except Exception:
            log.exception('Cannot fetch the cover %s', path)
            return False
        if fetched:
            log.debug('fetched the cover %s', path)
        return fetched

    def get(self, path):
        """The texture for path if it is decoded and cached, else None. Counts as a use."""
        texture = self._textures.get(path)
        if texture is not None:
            self._textures.move_to_end(path)
        return texture

    def request(self, path, callback):
        """Decode path and call callback(texture or None) on the main loop.

        Returns a token for cancel(). A cached path, or no path at all, is answered at once,
        before this returns, and gives None as its token: there is nothing to cancel.
        """
        texture = self.get(path) if path else None
        if texture is not None or not path:
            callback(texture)
            return None
        token = next(self._tokens)
        decode = self._decodes.get(path)
        if decode is None:
            task = asyncio.get_event_loop().create_task(self._decode(path))
            decode = self._decodes[path] = (task, {})
        decode[1][token] = callback
        self._paths[token] = path
        return token

    def cancel(self, token):
        """Forget a request: its callback will not be called. None and spent tokens are fine."""
        path = self._paths.pop(token, None)
        decode = self._decodes.get(path)
        if decode is None:
            return
        task, waiters = decode
        waiters.pop(token, None)
        if not waiters:
            del self._decodes[path]
            task.cancel()

    def clear(self):
        """Drop every cached texture (widgets keep the ones they show)."""
        self._textures.clear()

    def pending(self):
        """How many paths are being decoded: for tests and debugging."""
        return len(self._decodes)

    async def _decode(self, path):
        try:
            texture = await asyncio.to_thread(_load, path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Cannot decode artwork %s', path)
            texture = None
        if texture is not None:
            self._textures[path] = texture
            while len(self._textures) > self.size:
                self._textures.popitem(last=False)
        _task, waiters = self._decodes.pop(path, (None, {}))
        for token, callback in waiters.items():
            self._paths.pop(token, None)
            try:
                callback(texture)
            except Exception:
                log.exception('Artwork callback for %s failed', path)


def _load(path):
    """Decode an image file into a texture, in a worker thread (a Gdk.Texture is immutable, and
    creating one off the main thread is safe). None when the file is missing or unreadable."""
    try:
        return Gdk.Texture.new_from_filename(path)
    except GLib.Error as error:
        if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
            log.debug('Cannot read artwork %s: %s', path, error.message)
        return None


def art_colour(text):
    """An Item's art_color ('#1a2b3c': Apple's colour for the artwork's background) as a
    Gdk.RGBA, or None when there is none or it does not parse."""
    if not text:
        return None
    rgba = Gdk.RGBA()
    return rgba if rgba.parse(text) else None


def is_dark(rgba):
    """Whether this colour takes white text rather than black: its perceived brightness (the
    BT.601 luma of the sRGB values) is under 60%. WCAG's luminance would put black on the
    saturated mid-tones Apple's artwork colours often are (a mid blue, a leaf green), where
    white reads better and is what Apple uses."""
    return 0.299 * rgba.red + 0.587 * rgba.green + 0.114 * rgba.blue < 0.6


# The text drawn on an item's colour (the hero cards' band, the category tiles): white on a
# dark colour, style.css's .light-art black, rgb(0 0 6 / 80%), on a light one.
LIGHT_TEXT = ((1.0, 1.0, 1.0), 1.0)
DARK_TEXT = ((0.0, 0.0, 6 / 255), 0.8)
# WCAG 2's AA contrast for text under 18 pt (14 pt bold), which every caption here is.
MIN_CONTRAST = 4.5


def _linear(channel):
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(rgb):
    """WCAG's relative luminance of an sRGB colour, (r, g, b) in 0-1."""
    red, green, blue = (_linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(text, opacity, background):
    """WCAG's contrast ratio of `text` drawn at `opacity` over `background` (sRGB tuples)."""
    shown = tuple(opacity * t + (1 - opacity) * b for t, b in zip(text, background))
    lighter, darker = sorted((luminance(shown), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def readable_band(rgb, dark, text_opacity=1.0):
    """The colour to draw under a caption instead of `rgb` (an sRGB tuple), so that its text
    (white when `dark`, else the black of DARK_TEXT) at `text_opacity` reads at MIN_CONTRAST:
    rgb itself when it does, else rgb mixed towards black (under white text) or white (under
    black) just far enough, in steps of 2%. The hue stays; mid-tones under white text get
    deeper (the demo's orange by about a third)."""
    text, opacity = LIGHT_TEXT if dark else DARK_TEXT
    opacity *= text_opacity
    towards = (0.0, 0.0, 0.0) if dark else (1.0, 1.0, 1.0)
    for step in range(51):
        amount = step / 50
        band = tuple(c * (1 - amount) + t * amount for c, t in zip(rgb, towards))
        if contrast_ratio(text, opacity, band) >= MIN_CONTRAST:
            return band
    return towards


def band_colour(rgba, text_opacity=1.0):
    """(the Gdk.RGBA to draw under a caption, dark) for an item's colour: dark says whether
    its text is white (is_dark), and the colour is readable_band()'s for text drawn at
    text_opacity (a dimmed subtitle's)."""
    dark = is_dark(rgba)
    red, green, blue = readable_band((rgba.red, rgba.green, rgba.blue), dark, text_opacity)
    band = Gdk.RGBA()
    band.red, band.green, band.blue, band.alpha = red, green, blue, 1.0
    return band, dark


_default = None


def get_default():
    """The process-wide loader."""
    global _default
    if _default is None:
        _default = Artwork()
    return _default
