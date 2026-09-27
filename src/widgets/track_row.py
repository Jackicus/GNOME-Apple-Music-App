"""AppleMusicTrackRow: a track of an album or playlist, as a detail page's list shows it."""

from gi.repository import Gtk

from .cover import Cover  # noqa: F401  registers $AppleMusicCover for the template


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/track_row.ui')
class TrackRow(Gtk.Box):
    """A leading track number (albums) or 40 px thumbnail (playlists), the title with an
    explicit badge, the artist under it when it says something, and the duration.

    bind() and unbind() are called by a list factory as rows are recycled; the thumbnail is
    drawn only while the row is on screen (Cover). Activating the row is the list's business:
    the page plays the track's group from the track (window.play_request).
    """

    __gtype_name__ = 'AppleMusicTrackRow'

    number_label = Gtk.Template.Child()
    cover = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    explicit_badge = Gtk.Template.Child()
    artist_label = Gtk.Template.Child()
    duration_label = Gtk.Template.Child()

    def bind(self, track, album_artist=None):
        """Show track. album_artist given: an album's row, numbered, with the artist shown only
        when it is not the album's; None: a playlist's row, with thumbnail and artist."""
        album = album_artist is not None
        self.number_label.set_visible(album)
        self.cover.set_visible(not album)
        if album:
            self.number_label.set_text(str(track.track_number) if track.track_number else '')
            show_artist = bool(track.artist) and track.artist != album_artist
        else:
            self.cover.set_paths(track.thumb)
            show_artist = bool(track.artist)
        self.title_label.set_text(track.title)
        self.explicit_badge.set_visible(track.explicit)
        self.artist_label.set_visible(show_artist)
        self.artist_label.set_text(track.artist if show_artist else None)
        self.duration_label.set_text(track.duration_label)

    def unbind(self):
        self.cover.set_paths()
