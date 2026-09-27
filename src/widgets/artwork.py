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
"""

import asyncio
import itertools
import logging
from collections import OrderedDict

import gi

gi.require_version('Gdk', '4.0')

from gi.repository import Gdk, Gio, GLib  # noqa: E402

log = logging.getLogger(__name__)

CACHE_SIZE = 200


class Artwork:
    """An LRU of decoded textures by path, and the decodes in flight."""

    def __init__(self, size=CACHE_SIZE):
        self.size = size
        self._textures = OrderedDict()  # path -> Gdk.Texture, least recently used first
        self._decodes = {}  # path -> (asyncio.Task, {token: callback})
        self._paths = {}  # token -> path, for the requests still waiting
        self._tokens = itertools.count(1)

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


_default = None


def get_default():
    """The process-wide loader."""
    global _default
    if _default is None:
        _default = Artwork()
    return _default
