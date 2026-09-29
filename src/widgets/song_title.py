"""AppleMusicSongTitle: the Songs table's title cell, a thumbnail, the title and an explicit
badge."""

from gi.repository import Gtk

from .cover import Cover  # noqa: F401  registers $AppleMusicCover for the template


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/song_title.ui')
class SongTitle(Gtk.Box):
    """A track's 32 px thumbnail (its own, else its album's), its title and, for explicit
    tracks, an "E" badge.

    bind(track) and unbind() are called by the column's factory as rows are recycled. The
    thumbnail is an AppleMusicCover: asked for only while the cell is on screen, in an idle
    after the frame (a view creates and maps 200 rows at a time and shows a dozen; a re-sort
    rebinds them all), let go of when it is unmapped, and decoded at 32 px times the scale
    factor, which the Artwork cache holds thousands of.
    """

    __gtype_name__ = 'AppleMusicSongTitle'

    cover = Gtk.Template.Child()
    label = Gtk.Template.Child()
    explicit_badge = Gtk.Template.Child()

    _track = None

    @property
    def context_item(self):
        """The row's Track, for the Songs table's context menu and drag: the other cells of
        a row find it here (widgets/context_menu.py)."""
        return self._track

    def bind(self, track):
        self._track = track
        self.cover.set_paths(track.thumb)
        self.label.set_text(track.title)
        self.explicit_badge.set_visible(track.explicit)

    def unbind(self):
        self._track = None
        self.cover.set_paths()
