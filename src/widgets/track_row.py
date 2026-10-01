# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicTrackRow: a track of an album or playlist, as a detail page's list shows it, and
the header over a playlist's table (TrackTableHeader)."""

from gettext import gettext as _

from gi.repository import Gtk

from .cover import Cover  # noqa: F401  registers $AppleMusicCover for the template
from .track_links import TrackLink

# The thumbnail's edge in a playlist's rows (track_row.blp's cover), which the header's first
# column leaves room for.
COVER_SIZE = 40


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/track_row.ui')
class TrackRow(Gtk.Box):
    """A leading track number (albums) or 40 px thumbnail (playlists), the title with an
    explicit badge, the artist under it when it says something, and the duration; or, as a
    playlist's table row (`table`), the thumbnail, the title, the artist and the album in
    three equal columns (the artist and the album links: widgets/track_links.py), and the
    duration.

    bind() and unbind() are called by a list factory as rows are recycled; the thumbnail is
    drawn only while the row is on screen (Cover). Activating the row is the list's business:
    the page plays the track's group from the track (window.play_request). The Track bound is
    the row's `context_item`: the view's context menu and drag (widgets/context_menu.py).
    """

    __gtype_name__ = 'AppleMusicTrackRow'

    _track = None

    number_label = Gtk.Template.Child()
    cover = Gtk.Template.Child()
    columns = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    explicit_badge = Gtk.Template.Child()
    artist_label = Gtk.Template.Child()
    duration_label = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # A table's artist and album columns, made here (GtkBuilder runs no Python __init__).
        self.artist_link = TrackLink('artist', hexpand=True, visible=False)
        self.album_link = TrackLink('album', hexpand=True, visible=False)
        self.columns.append(self.artist_link)
        self.columns.append(self.album_link)
        self._table = False

    @property
    def context_item(self):
        """The Track shown, for its context menu and its drag (widgets/context_menu.py)."""
        return self._track

    def bind(self, track, album_artist=None, table=False):
        """Show track. album_artist given: an album's row, numbered, with the artist shown only
        when it is not the album's; None: a playlist's row, with thumbnail and artist, under
        the title, or with `table`, in a column of its own beside the album's."""
        self._track = track
        album = album_artist is not None
        table = table and not album
        self._set_table(table)
        self.number_label.set_visible(album)
        self.cover.set_visible(not album)
        if album:
            self.number_label.set_text(str(track.track_number) if track.track_number else '')
            show_artist = bool(track.artist) and track.artist != album_artist
        else:
            self.cover.set_paths(track.thumb)
            show_artist = bool(track.artist) and not table
        self.title_label.set_text(track.title)
        self.explicit_badge.set_visible(track.explicit)
        self.artist_label.set_visible(show_artist)
        self.artist_label.set_text(track.artist if show_artist else None)
        if table:
            self.artist_link.show(track)
            self.album_link.show(track)
        self.duration_label.set_text(track.duration_label)

    def _set_table(self, table):
        """The table's columns, or the list's title and artist (only when it changes: a row
        shown or hidden lays the list out again)."""
        if table == self._table:
            return
        self._table = table
        self.columns.set_homogeneous(table)
        self.artist_link.set_visible(table)
        self.album_link.set_visible(table)
        if not table:
            self.artist_link.show(None)
            self.album_link.show(None)

    def unbind(self):
        self._track = None
        self.cover.set_paths()


class TrackTableHeader(Gtk.Box):
    """The column titles over a playlist's table (Title, Artist, Album, Time), laid out as its
    rows are (TrackRow with `table`), so that each title is over its column."""

    def __init__(self, **kwargs):
        super().__init__(spacing=12, **kwargs)
        self.add_css_class('track-table-header')
        self.append(Gtk.Box(width_request=COVER_SIZE))  # over the thumbnails
        columns = Gtk.Box(spacing=12, homogeneous=True, hexpand=True)
        for title in (_('Title'), _('Artist'), _('Album')):
            columns.append(self._title(Gtk.Label(label=title, xalign=0)))
        self.append(columns)
        # "Time", as wide as the rows' durations: a stack sized by its widest child, an
        # inscription of the rows' duration_label's chars and classes that it never shows
        # (the title's smaller font would make the column narrower than theirs).
        time = Gtk.Stack(hhomogeneous=True)
        sizer = Gtk.Inscription(min_chars=5, nat_chars=7)
        sizer.add_css_class('numeric')
        time.add_child(sizer)
        time.add_child(self._title(Gtk.Label(label=_('Time'), xalign=1)))
        time.set_visible_child(time.get_last_child())
        self.append(time)

    @staticmethod
    def _title(label):
        label.add_css_class('caption-heading')
        label.add_css_class('dimmed')
        return label
