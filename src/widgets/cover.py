"""AppleMusicCover: square artwork over a placeholder, for rows, cards and detail pages."""

from gi.repository import GObject, Gtk

from . import artwork


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/cover.ui')
class Cover(Gtk.Overlay):
    """Artwork `size` px square: the first of set_paths()' paths that decodes, over a placeholder
    card with a music note until then, and for good when none does.

    An artwork.ArtworkSlot does the asking: only while the cover is mapped (in an idle after
    the frame: a list creates and maps rows by the hundred and shows a dozen), let go of when
    it is unmapped, so a recycled row holds a texture only while it is on screen, and decoded
    at `size` times the scale factor, again when either changes (Now Playing's cover grows
    with the window). Meanwhile a path further down the list already decoded at this size,
    or any of them decoded at another, is shown: a detail page shows the thumbnail its tile
    left in the cache, then its cover.

    GtkBuilder calls __init__ without arguments and sets `size` after it; a Python caller's
    `size` is set before __init__'s body runs, so the slot starts as a class attribute.
    """

    __gtype_name__ = 'AppleMusicCover'

    placeholder = Gtk.Template.Child()
    placeholder_icon = Gtk.Template.Child()
    picture = Gtk.Template.Child()

    _slot = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._slot = artwork.ArtworkSlot(self._set_art, self.size)
        self._slot.attach(self)

    def _get_size(self):
        return self.placeholder.get_size_request()[0]

    def _set_size(self, size):
        self.placeholder.set_size_request(size, size)
        self.placeholder_icon.set_pixel_size(max(16, size * 3 // 10))
        if self._slot is not None:
            self._slot.set_size(size)

    size = GObject.Property(type=int, default=0, getter=_get_size, setter=_set_size,
                            nick='Size', blurb='The width and height, in pixels')

    def set_paths(self, *paths):
        """Show the first of these files that decodes (None and missing files are skipped).
        False when they are the paths shown already (nothing is asked for again: refresh()
        does that)."""
        return self._slot.set_paths(*paths)

    def refresh(self):
        """Look for the paths again (a better one has arrived on disk since)."""
        self._slot.refresh()

    def _set_art(self, paintable, found):
        # Never None, and the icon faded rather than hidden: either would lay the list out
        # again (artwork.empty()).
        self.picture.set_paintable(paintable)
        self.placeholder_icon.set_opacity(0 if found else 1)
