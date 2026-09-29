# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicQueueView: the Now Playing sheet's Up Next tab, the Player's queue from the
entry playing on.

A Gtk.ListView over a Gtk.SliceListModel of player.queue (a Gio.ListStore of NowPlaying)
that starts at the entry playing (slice_offset) and runs to the queue's end (slice_size), so
the list shows what its name says: the entry playing first, marked with a play icon in place
of its number and a bold title, then what comes next. Rows are like a playlist's track rows
(a number, the title with its explicit badge, the artist, the duration). Activating a row (a
double click, or Enter) plays that entry (player.queue_jump, with the slice's offset put
back). An empty queue shows a compact status page. The list is named "Up Next" for assistive
technology and each row by labels.track_label.
"""

from gettext import gettext as _
from gettext import pgettext as C_

from gi.repository import Adw, GLib, Gtk, Pango

from ..player import format_time
from .labels import track_label


def slice_offset(queue_index):
    """Where the Up Next slice of the queue starts: the entry playing, or the top when
    nothing is (queue_index -1)."""
    return max(queue_index, 0)


def slice_size(offset):
    """The Up Next slice's size from `offset`: the rest of the queue, however long it gets.
    Not GLib.MAXUINT: a Gtk.SliceListModel whose offset plus size passes G_MAXUINT wraps its
    end and ignores the queue's changes after it (a new queue, entries added at its end)."""
    return GLib.MAXUINT32 - offset


class QueueRow(Gtk.Box):
    """One queue entry: number or play icon, title (and its explicit badge) and artist,
    duration."""

    def __init__(self):
        super().__init__(spacing=12)
        self.list_item = None  # the Gtk.ListItem bound (its accessible description)
        self.number_label = Gtk.Inscription(min_chars=2, nat_chars=2, xalign=1,
                                            valign=Gtk.Align.CENTER)
        self.number_label.add_css_class('numeric')
        self.number_label.add_css_class('dimmed')
        self.playing_icon = Gtk.Image(icon_name='media-playback-start-symbolic',
                                      valign=Gtk.Align.CENTER, visible=False,
                                      accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.playing_icon.add_css_class('accent')
        marker = Gtk.Stack(hhomogeneous=True, valign=Gtk.Align.CENTER)
        marker.add_named(self.number_label, 'number')
        marker.add_named(self.playing_icon, 'playing')
        self._marker = marker
        self.append(marker)

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                         hexpand=True)
        title_line = Gtk.Box(spacing=6)
        self.title_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                     single_line_mode=True)
        self.title_label.add_css_class('queue-title')
        # The badge marking a song with explicit lyrics, as the track rows show it; the
        # row's accessible name says "explicit" (track_label), so the badge is decoration.
        self.explicit_badge = Gtk.Label(
            # Translators: the badge marking a song with explicit lyrics, as Apple Music shows it
            label=C_('explicit badge', 'E'), tooltip_text=_('Explicit'), visible=False,
            valign=Gtk.Align.CENTER, accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.explicit_badge.add_css_class('caption')
        self.explicit_badge.add_css_class('explicit-badge')
        title_line.append(self.title_label)
        title_line.append(self.explicit_badge)
        self.artist_label = Gtk.Inscription(xalign=0,
                                            text_overflow=Gtk.InscriptionOverflow.ELLIPSIZE_END,
                                            visible=False)
        self.artist_label.add_css_class('caption')
        self.artist_label.add_css_class('dimmed')
        titles.append(title_line)
        titles.append(self.artist_label)
        self.append(titles)

        self.duration_label = Gtk.Inscription(min_chars=5, nat_chars=5, xalign=1,
                                              valign=Gtk.Align.CENTER)
        self.duration_label.add_css_class('numeric')
        self.duration_label.add_css_class('dimmed')
        self.append(self.duration_label)

    def bind(self, entry, position, current):
        self.number_label.set_text(str(position + 1))
        self.title_label.set_label(entry.title or _('Unknown Title'))
        self.explicit_badge.set_visible(entry.explicit)
        self.artist_label.set_text(entry.artist)
        self.artist_label.set_visible(bool(entry.artist))
        self.duration_label.set_text(
            format_time(entry.duration_ms / 1000) if entry.duration_ms else '')
        self.set_current(current)

    def set_current(self, current):
        self.playing_icon.set_visible(current)  # a stack shows only a visible child
        self._marker.set_visible_child_name('playing' if current else 'number')
        if current:
            self.add_css_class('current')
        else:
            self.remove_css_class('current')


class QueueView(Gtk.Stack):
    """The Up Next tab. See the module."""

    __gtype_name__ = 'AppleMusicQueueView'

    def __init__(self):
        super().__init__(vhomogeneous=True, hhomogeneous=True,
                         transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._player = None
        self._app = None
        self._active = False
        self._rows = {}  # bound position (in the slice) -> its QueueRow
        self._slice = None  # the queue from the entry playing on
        self._playing = _('Playing')  # looked up once: rows are bound often

        self.empty_page = Adw.StatusPage(icon_name='playlist-symbolic', title=_('Nothing Queued'),
                                         description=_('Play something to fill Up Next'))
        self.empty_page.add_css_class('compact')
        self.add_named(self.empty_page, 'empty')

        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self._on_setup)
        factory.connect('bind', self._on_bind)
        factory.connect('unbind', self._on_unbind)
        # Tab leaves the list after the entry with the focus; the arrows move between them.
        self.list_view = Gtk.ListView(factory=factory, model=Gtk.NoSelection(),
                                      tab_behavior=Gtk.ListTabBehavior.ITEM)
        self.list_view.add_css_class('queue-list')
        self.list_view.update_property([Gtk.AccessibleProperty.LABEL], [_('Up Next')])
        self.list_view.connect('activate', self._on_activate)
        self.scrolled = Gtk.ScrolledWindow(child=self.list_view,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.add_named(self.scrolled, 'list')

    def set_player(self, player, app):
        self._player = player
        self._app = app
        self._slice = Gtk.SliceListModel(model=player.queue, offset=0, size=slice_size(0))
        self.list_view.set_model(Gtk.NoSelection(model=self._slice))
        player.queue.connect('items-changed', lambda *_: self._on_items_changed())
        player.connect('notify::queue-index', lambda *_: self._on_index())
        self._on_index()
        self._update_page()

    def set_active(self, active):
        """Scroll to the entry playing when shown."""
        self._active = active
        if active:
            self._scroll_to_current()

    def _update_page(self):
        self.set_visible_child_name('list' if self._player.queue.get_n_items() else 'empty')

    def _on_items_changed(self):
        """A new queue: its page, and the entry playing on top (a new queue with the same
        index number fires no index notify)."""
        self._update_page()
        if self._active:
            self._scroll_to_current()

    def _on_index(self):
        """The entry playing changed: the slice starts there, and the rows show it."""
        index = self._player.queue_index
        offset = slice_offset(index)
        self._slice.set_size(slice_size(offset))  # first: only the offset changes the items
        self._slice.set_offset(offset)
        for position, row in self._rows.items():
            current = position == 0 and index >= 0
            row.set_current(current)
            self._describe(row, current)
        if self._active:
            self._scroll_to_current()

    def _scroll_to_current(self):
        if self._player is not None and self._player.queue_index >= 0 and self._slice.get_n_items():
            self.list_view.scroll_to(0, Gtk.ListScrollFlags.NONE, None)

    def _on_setup(self, _factory, list_item):
        list_item.set_child(QueueRow())

    def _on_bind(self, _factory, list_item):
        position = list_item.get_position()
        row = list_item.get_child()
        self._rows[position] = row
        entry = list_item.get_item()
        current = position == 0 and self._player.queue_index >= 0
        row.bind(entry, position, current)
        row.list_item = list_item
        list_item.set_accessible_label(track_label(entry, show_artist=True, show_album=False))
        self._describe(row, current)

    def _describe(self, row, current):
        """The entry playing is described as such (its play icon is decoration)."""
        list_item = getattr(row, 'list_item', None)
        if list_item is not None:
            list_item.set_accessible_description(self._playing if current else '')

    def _on_unbind(self, _factory, list_item):
        position = list_item.get_position()
        row = list_item.get_child()
        row.list_item = None
        if self._rows.get(position) is row:
            del self._rows[position]

    def _on_activate(self, _list_view, position):
        if self._player is None:
            return
        self._app.player_command(self._player.queue_jump(position + self._slice.get_offset()))
