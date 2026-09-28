"""AppleMusicSongTitle: the Songs table's title cell, a thumbnail, the title and an explicit
badge."""

from gi.repository import GLib, Gtk

from . import artwork

# The thumbnail's edge in logical pixels (song_title.blp): the texture asked for is this
# times the scale factor, a hundredth of the 320 px thumbnail's memory.
ART_SIZE = 32


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/song_title.ui')
class SongTitle(Gtk.Box):
    """A track's 32 px thumbnail (its own, else its album's), its title and, for explicit
    tracks, an "E" badge.

    bind(track) and unbind() are called by the column's factory as rows are recycled. As with
    the grid tiles, the thumbnail is asked for only while the cell is mapped, which in a list
    view means on screen, in an idle after the frame (a view creates and maps 200 rows at a
    time and shows a dozen; a re-sort rebinds them all), and let go of when it is unmapped.
    The texture is the album's thumbnail decoded at ART_SIZE, which the Artwork cache holds
    thousands of.
    """

    __gtype_name__ = 'AppleMusicSongTitle'

    placeholder_icon = Gtk.Template.Child()
    picture = Gtk.Template.Child()
    label = Gtk.Template.Child()
    explicit_badge = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._track = None
        self._path = None
        self._token = None
        self._idle = None
        self._artwork = artwork.get_default()

    @property
    def context_item(self):
        """The row's Track, for the Songs table's context menu and drag: the other cells of
        a row find it here (widgets/context_menu.py)."""
        return self._track

    def bind(self, track):
        self._track = track
        self._path = track.thumb
        self.label.set_text(track.title)
        self.explicit_badge.set_visible(track.explicit)
        if self.get_mapped():
            self._show_art_soon()

    def unbind(self):
        self._release_art()
        self._track = None
        self._path = None

    def do_map(self):
        Gtk.Box.do_map(self)
        if self._track is not None:
            self._show_art_soon()

    def do_unmap(self):
        self._release_art()
        Gtk.Box.do_unmap(self)

    def _show_art_soon(self):
        self._artwork.cancel(self._token)
        self._token = None
        size = ART_SIZE * self.get_scale_factor()
        texture = self._artwork.get(self._path, size) if self._path else None
        self._set_texture(texture)
        if texture is None and self._path and self._idle is None:
            self._idle = GLib.idle_add(self._show_art, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _show_art(self):
        self._idle = None
        if self._path and self.get_mapped():
            self._artwork.cancel(self._token)
            self._token = self._artwork.request(self._path, self._on_texture,
                                                ART_SIZE * self.get_scale_factor())
        return GLib.SOURCE_REMOVE

    def _release_art(self):
        if self._idle is not None:
            GLib.source_remove(self._idle)
            self._idle = None
        self._artwork.cancel(self._token)
        self._token = None
        self._set_texture(None)

    def _on_texture(self, texture):
        self._token = None
        self._set_texture(texture)

    def _set_texture(self, texture):
        # Never None, and the icon faded rather than hidden: either would lay the table out
        # again (artwork.empty()).
        self.picture.set_paintable(texture or artwork.empty(ART_SIZE * self.get_scale_factor()))
        self.placeholder_icon.set_opacity(0 if texture else 1)
