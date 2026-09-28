"""AppleMusicSongTitle: the Songs table's title cell, a thumbnail, the title and an explicit
badge."""

from gi.repository import Gtk

from . import artwork


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/song_title.ui')
class SongTitle(Gtk.Box):
    """A track's 32 px thumbnail (its own, else its album's), its title and, for explicit
    tracks, an "E" badge.

    bind(track) and unbind() are called by the column's factory as rows are recycled. As with
    the grid tiles, the thumbnail is asked for only while the cell is mapped, which in a list
    view means on screen, and let go of when it is unmapped. The texture is the album's 320 px
    thumbnail, shared with the album's tile through the Artwork cache.
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
            self._show_art()

    def unbind(self):
        self._release_art()
        self._track = None
        self._path = None

    def do_map(self):
        Gtk.Box.do_map(self)
        if self._track is not None:
            self._show_art()

    def do_unmap(self):
        self._release_art()
        Gtk.Box.do_unmap(self)

    def _show_art(self):
        self._artwork.cancel(self._token)
        self._token = None
        texture = self._artwork.get(self._path) if self._path else None
        self._set_texture(texture)
        if texture is None and self._path:
            self._token = self._artwork.request(self._path, self._on_texture)

    def _release_art(self):
        self._artwork.cancel(self._token)
        self._token = None
        self._set_texture(None)

    def _on_texture(self, texture):
        self._token = None
        self._set_texture(texture)

    def _set_texture(self, texture):
        self.picture.set_paintable(texture)
        self.placeholder_icon.set_visible(texture is None)
