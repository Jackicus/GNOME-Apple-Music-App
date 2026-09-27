"""AppleMusicCover: square artwork over a placeholder, for track rows and detail pages."""

import functools

from gi.repository import GObject, Gtk

from . import artwork


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/cover.ui')
class Cover(Gtk.Overlay):
    """Artwork `size` px square: the first of set_paths()' paths that decodes, over a placeholder
    card with a music note until then, and for good when none does.

    As with the grid tiles, the artwork is asked for only while the cover is mapped and let go
    of when it is unmapped, so a recycled row holds a texture only while it is on screen. A
    path further down the list that is already decoded is shown at once while a better one
    decodes: a detail page shows the thumbnail its tile left in the cache, then its cover.

    It may be built by GtkBuilder, which does not call __init__, so its state starts as class
    attributes.
    """

    __gtype_name__ = 'AppleMusicCover'

    placeholder = Gtk.Template.Child()
    placeholder_icon = Gtk.Template.Child()
    picture = Gtk.Template.Child()

    _paths = ()
    _token = None

    def _get_size(self):
        return self.placeholder.get_width_request()

    def _set_size(self, size):
        self.placeholder.set_size_request(size, size)
        self.placeholder_icon.set_pixel_size(max(16, size * 3 // 10))

    size = GObject.Property(type=int, default=0, getter=_get_size, setter=_set_size,
                            nick='Size', blurb='The width and height, in pixels')

    def set_paths(self, *paths):
        """Show the first of these files that decodes (None and missing files are skipped)."""
        paths = tuple(path for path in paths if path)
        if paths == self._paths:
            return
        self._paths = paths
        if self.get_mapped():
            self._show_art()

    def do_map(self):
        Gtk.Overlay.do_map(self)
        self._show_art()

    def do_unmap(self):
        self._release_art()
        Gtk.Overlay.do_unmap(self)

    def _show_art(self):
        """Show the best path decoded already, and ask for the better ones before it."""
        loader = artwork.get_default()
        loader.cancel(self._token)
        self._token = None
        best = len(self._paths)
        texture = None
        for position, path in enumerate(self._paths):
            texture = loader.get(path)
            if texture is not None:
                best = position
                break
        self._set_texture(texture)
        self._request(0, best)

    def _request(self, position, best):
        """Decode the path at position, and on failure the next, stopping short of best."""
        if position >= best:
            return
        callback = functools.partial(self._on_texture, position, best)
        token = artwork.get_default().request(self._paths[position], callback)
        if token is not None:  # None: answered already, and the callback went on from there
            self._token = token

    def _on_texture(self, position, best, texture):
        self._token = None
        if texture is not None:
            self._set_texture(texture)
        else:
            self._request(position + 1, best)

    def _release_art(self):
        artwork.get_default().cancel(self._token)
        self._token = None
        self._set_texture(None)

    def _set_texture(self, texture):
        self.picture.set_paintable(texture)
        self.placeholder_icon.set_visible(texture is None)
