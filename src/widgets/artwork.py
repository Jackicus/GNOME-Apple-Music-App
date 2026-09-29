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
unmapped), so the cache's budget bounds what the artwork costs. A widget asks for the size it
draws (a tile 160 px, a Songs row 32 px, times the scale factor): a file bigger than that is
decoded scaled down to it, so a tile's texture is a quarter of the 320 px thumbnail's memory
at 1× and is drawn without scaling; the cache is keyed by path and size, and holds CACHE_BYTES
of pixels (a screen of tiles and a page ahead in a maximized 1920×1080 window at 2×), least
recently used first out. The renderer keeps a copy of each texture it draws on the GPU, about
the same again while the texture lives.

The files come from the library's sync (thumbnails) and from src/remote.py, which fetches
the covers and the artwork the library does not hold; nothing here downloads or needs the
engine.
"""

import asyncio
import itertools
import logging
from collections import OrderedDict

import gi

gi.require_version('Gdk', '4.0')

gi.require_version('GdkPixbuf', '2.0')

from gi.repository import Gdk, GdkPixbuf, Gio, GLib  # noqa: E402

log = logging.getLogger(__name__)

# The decoded pixels the cache keeps, at 4 bytes a pixel: 32 MB is 80 tiles' 320 px textures at
# 2× (a maximized 1920×1080 window shows 36 tiles: a screen and a page ahead), 320 at 1×, 20
# covers at 640 px, thousands of 32 px row thumbnails. It fills only as far as tiles are seen.
CACHE_BYTES = 32 * 1024 * 1024


class Artwork:
    """An LRU of decoded textures by (path, size), and the decodes in flight.

    `budget` is the pixel bytes kept (CACHE_BYTES); `max_entries`, when given, caps the number
    of textures too (tests)."""

    def __init__(self, max_entries=None, budget=CACHE_BYTES):
        self.max_entries = max_entries
        self.budget = budget
        self._bytes = 0  # what the cached textures' pixels weigh, at 4 bytes each
        self._textures = OrderedDict()  # (path, size) -> Gdk.Texture, least recently used first
        self._sizes = {}  # path -> the sizes it is cached at
        self._decodes = {}  # (path, size) -> (asyncio.Task, {token: callback})
        self._keys = {}  # token -> (path, size), for the requests still waiting
        self._tokens = itertools.count(1)

    def get(self, path, size=None):
        """The texture for path at `size` (the longest edge in pixels the widget draws;
        None: the file's own) if it is decoded and cached, else None. Counts as a use."""
        key = (path, size)
        texture = self._textures.get(key)
        if texture is not None:
            self._textures.move_to_end(key)
        return texture

    def get_any(self, path):
        """The biggest texture cached for path at any size, or None: something to show while
        the size wanted decodes (a detail page's cover over the thumbnail its tile decoded,
        drawn scaled meanwhile). Counts as a use."""
        sizes = self._sizes.get(path)
        if not sizes:
            return None
        return self.get(path, max(sizes, key=lambda size: float('inf') if size is None else size))

    def request(self, path, callback, size=None):
        """Decode path at `size` (as get()) and call callback(texture or None) on the main loop.

        Returns a token for cancel(). A cached path, or no path at all, is answered at once,
        before this returns, and gives None as its token: there is nothing to cancel.
        """
        texture = self.get(path, size) if path else None
        if texture is not None or not path:
            callback(texture)
            return None
        token = next(self._tokens)
        key = (path, size)
        decode = self._decodes.get(key)
        if decode is None:
            task = asyncio.get_event_loop().create_task(self._decode(key))
            decode = self._decodes[key] = (task, {})
        decode[1][token] = callback
        self._keys[token] = key
        return token

    def cancel(self, token):
        """Forget a request: its callback will not be called. None and spent tokens are fine."""
        key = self._keys.pop(token, None)
        decode = self._decodes.get(key)
        if decode is None:
            return
        task, waiters = decode
        waiters.pop(token, None)
        if not waiters:
            del self._decodes[key]
            task.cancel()

    def clear(self):
        """Drop every cached texture (widgets keep the ones they show)."""
        self._textures.clear()
        self._sizes.clear()
        self._bytes = 0

    def pending(self):
        """How many decodes are in flight: for tests and debugging."""
        return len(self._decodes)

    def cached(self):
        """(textures cached, their pixel bytes): for tests and debugging."""
        return len(self._textures), self._bytes

    async def _decode(self, key):
        path, size = key
        try:
            texture = await asyncio.to_thread(_load, path, size)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Cannot decode artwork %s', path)
            texture = None
        if texture is not None:
            self._textures[key] = texture
            self._sizes.setdefault(path, set()).add(size)
            self._bytes += _weight(texture)
            while self._textures and (
                    self._bytes > self.budget
                    or (self.max_entries is not None
                        and len(self._textures) > self.max_entries)):
                (dropped_path, dropped_size), dropped = self._textures.popitem(last=False)
                self._bytes -= _weight(dropped)
                sizes = self._sizes.get(dropped_path)
                if sizes is not None:
                    sizes.discard(dropped_size)
                    if not sizes:
                        del self._sizes[dropped_path]
        _task, waiters = self._decodes.pop(key, (None, {}))
        for token, callback in waiters.items():
            self._keys.pop(token, None)
            try:
                callback(texture)
            except Exception:
                log.exception('Artwork callback for %s failed', path)


_EMPTY = {}  # size -> its empty paintable


def empty(size):
    """A transparent paintable `size` px square, for a Gtk.Picture to show in place of a
    texture that size (none yet, or let go of): the picture lays itself out again, and every
    widget up to the window with it, whenever its paintable's size changes (None to a
    texture and back, as a tile scrolls in and out of view), and only redraws when it does
    not."""
    paintable = _EMPTY.get(size)
    if paintable is None:
        paintable = _EMPTY[size] = Gdk.Paintable.new_empty(size, size)
    return paintable


def _weight(texture):
    return texture.get_width() * texture.get_height() * 4


def _load(path, size=None):
    """Decode an image file into a texture, in a worker thread (a Gdk.Texture is immutable, and
    creating one off the main thread is safe). None when the file is missing or unreadable.

    With `size`, a file whose longest edge is bigger is decoded scaled down to it by
    GdkPixbuf, whose loader takes four times as long as GTK's (2.3 ms against 0.55 for a
    320 px JPEG) but gives a texture a quarter the size for a tile at 1×; a file no bigger,
    or no size, is decoded as it is by GTK's loader.
    """
    try:
        if size:
            _format, width, height = GdkPixbuf.Pixbuf.get_file_info(path)
            if max(width, height) > size:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
                return texture_from_pixbuf(pixbuf)
        return Gdk.Texture.new_from_filename(path)
    except GLib.Error as error:
        if not error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
            log.debug('Cannot read artwork %s: %s', path, error.message)
        return None


def texture_from_pixbuf(pixbuf):
    """A Gdk.Texture holding the pixbuf's pixels (Gdk.Texture.new_for_pixbuf is deprecated)."""
    layout = Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pixbuf.get_width(), pixbuf.get_height(), layout,
                                 pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())


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
    shown = tuple(opacity * t + (1 - opacity) * b for t, b in zip(text, background, strict=True))
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
        band = tuple(c * (1 - amount) + t * amount for c, t in zip(rgb, towards, strict=True))
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
