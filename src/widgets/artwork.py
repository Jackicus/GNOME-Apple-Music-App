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
import functools
import itertools
import logging
from collections import OrderedDict

import gi

gi.require_version('Gdk', '4.0')

gi.require_version('GdkPixbuf', '2.0')

from gi.repository import Gdk, GdkPixbuf, Gio, GLib  # noqa: E402

from .util import weak_method  # noqa: E402

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


class ArtworkSlot:
    """The artwork of one widget: which file it shows, at what size, and only while the
    widget is on screen. Every widget that draws artwork owns one (a tile, a cover, a category
    tile, the artist portrait) and keeps only the drawing:

        self._slot = ArtworkSlot(self._set_art, size)   # size: the edge drawn, logical px
        self._slot.attach(self)                         # map, unmap, the scale factor
        self._slot.set_paths(item.art, item.thumb)      # in bind; set_paths() in unbind

        def _set_art(self, paintable, found):           # found: a texture, not empty()
            self.picture.set_paintable(paintable)
            self.placeholder_icon.set_opacity(0 if found else 1)

    `on_texture(paintable, found)` is a bound method of the widget, held weakly (a slot is
    held by the widget's signal handlers: widgets/util.py), and always gets a paintable: a
    texture (found True), or empty() at the size drawn (found False), never None, so nothing
    is laid out again when artwork comes and goes (a widget that shows something else
    without artwork, an Adw.Avatar's initials, reads `found`).

    The paths are in order of preference; None and '' are skipped. While mapped, the first
    path decoded at the pixel size (size times the widget's scale factor) is shown at once
    from the Artwork cache; until a better one arrives, any of them decoded at another size
    stands in (a detail page's cover shows the thumbnail its tile decoded). The better ones
    are asked for in an idle after the frame, not as the widget binds or maps (a list maps
    and binds rows by the hundred and shows a dozen): the first path, and on failure the next,
    down to the one shown. The idle reads the paths and the best found when it runs, so a
    rebind before it runs asks for the new item's. Unmapped, the request is cancelled and the
    texture let go of (empty() shown); a new size or scale factor while mapped asks again at
    the new pixel size.
    """

    def __init__(self, on_texture, size=0, loader=None):
        self._on_texture = weak_method(on_texture)
        self._loader = loader  # None: get_default(), looked up when used
        self._size = size
        self._scale = 1
        self._paths = ()
        self._mapped = False
        self._best = 0  # the position of the path shown from the cache; len(paths): none
        self._token = None  # the decode asked for
        self._idle = None  # the idle that will ask for it

    @property
    def paths(self):
        return self._paths

    @property
    def pixels(self):
        """The edge drawn in device pixels: the size the texture is decoded at."""
        return self._size * self._scale

    def attach(self, widget):
        """Follow `widget`'s map, unmap and scale factor (the handlers hold the slot, and the
        slot the widget weakly: no cycle)."""
        widget.connect('map', _on_map, self)
        widget.connect('unmap', _on_unmap, self)
        widget.connect('notify::scale-factor', _on_scale_factor, self)
        if widget.get_mapped():
            self.map(widget.get_scale_factor())

    def set_paths(self, *paths):
        """Show the first of these files that decodes. False when they are the paths shown
        already (nothing is asked for again: refresh() does that)."""
        paths = tuple(path for path in paths if path)
        if paths == self._paths:
            return False
        self._paths = paths
        if self._mapped:
            self._show()
        return True

    def refresh(self):
        """Look for the paths again: a better file has arrived on disk since."""
        if self._mapped:
            self._show()

    def set_size(self, size):
        """The edge drawn, in logical pixels."""
        if size != self._size:
            self._size = size
            if self._mapped:
                self._show()

    def set_scale(self, scale):
        if scale != self._scale:
            self._scale = scale
            if self._mapped:
                self._show()

    def map(self, scale=1):
        self._mapped = True
        self._scale = scale
        self._show()

    def unmap(self):
        self._mapped = False
        self._cancel()
        self._emit(None)

    def _get_loader(self):
        return self._loader or get_default()

    def _show(self):
        """Show the best path decoded already, and ask for the better ones before it once the
        frame has settled."""
        loader = self._get_loader()
        loader.cancel(self._token)
        self._token = None
        pixels = self.pixels
        best, texture = len(self._paths), None
        for position, path in enumerate(self._paths):
            texture = loader.get(path, pixels)
            if texture is not None:
                best = position
                break
        else:  # meanwhile, one of the paths decoded at another size, if any is
            texture = next((found for found in map(loader.get_any, self._paths)
                            if found is not None), None)
        self._best = best
        self._emit(texture)
        if best > 0 and self._idle is None:
            self._idle = GLib.idle_add(self._request_soon, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _request_soon(self):
        self._idle = None
        if self._mapped:
            self._request(0)
        return GLib.SOURCE_REMOVE

    def _request(self, position):
        """Decode the path at position, and on failure the next, stopping short of the one
        shown."""
        if position >= self._best:
            return
        callback = functools.partial(self._on_decoded, position)
        token = self._get_loader().request(self._paths[position], callback, self.pixels)
        if token is not None:  # None: answered already, and the callback went on from there
            self._token = token

    def _on_decoded(self, position, texture):
        self._token = None
        if texture is not None:
            self._emit(texture)
        else:
            self._request(position + 1)

    def _cancel(self):
        if self._idle is not None:
            GLib.source_remove(self._idle)
            self._idle = None
        self._get_loader().cancel(self._token)
        self._token = None

    def _emit(self, texture):
        if texture is None:
            self._on_texture(empty(self.pixels), False)
        else:
            self._on_texture(texture, True)


def _on_map(widget, slot):
    slot.map(widget.get_scale_factor())


def _on_unmap(_widget, slot):
    slot.unmap()


def _on_scale_factor(widget, _pspec, slot):
    slot.set_scale(widget.get_scale_factor())


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
