"""AppleMusicQueueView: the Now Playing sheet's Up Next tab, the Player's queue.

A Gtk.ListView over player.queue (a Gio.ListStore of NowPlaying): rows like a playlist's
track rows (a number, the title, the artist, the duration), the entry playing marked with
a play icon in place of its number and a bold title. Activating a row (a double click, or
Enter) plays that entry (player.queue_jump). An empty queue shows a compact status page.
"""

from gettext import gettext as _

from gi.repository import Adw, Gtk, Pango

from ..player import format_time
from .transport import run_command


class QueueRow(Gtk.Box):
    """One queue entry: number or play icon, title and artist, duration."""

    def __init__(self):
        super().__init__(spacing=12)
        self.list_item = None  # the Gtk.ListItem bound (its accessible description)
        self.number_label = Gtk.Inscription(min_chars=2, nat_chars=2, xalign=1,
                                            valign=Gtk.Align.CENTER)
        self.number_label.add_css_class('numeric')
        self.number_label.add_css_class('dim-label')
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
        self.title_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                     single_line_mode=True)
        self.title_label.add_css_class('queue-title')
        self.artist_label = Gtk.Inscription(xalign=0,
                                            text_overflow=Gtk.InscriptionOverflow.ELLIPSIZE_END,
                                            visible=False)
        self.artist_label.add_css_class('caption')
        self.artist_label.add_css_class('dim-label')
        titles.append(self.title_label)
        titles.append(self.artist_label)
        self.append(titles)

        self.duration_label = Gtk.Inscription(min_chars=5, nat_chars=5, xalign=1,
                                              valign=Gtk.Align.CENTER)
        self.duration_label.add_css_class('numeric')
        self.duration_label.add_css_class('dim-label')
        self.append(self.duration_label)

    def bind(self, entry, position, current):
        self.number_label.set_text(str(position + 1))
        self.title_label.set_label(entry.title or _('Unknown Title'))
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
        self._rows = {}  # bound position -> its QueueRow
        # What a row reads to assistive technology (looked up once: rows are bound often).
        self._label_format = _('{title}, {artist}')
        self._playing = _('Playing')

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
        self.list_view.connect('activate', self._on_activate)
        self.scrolled = Gtk.ScrolledWindow(child=self.list_view,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.add_named(self.scrolled, 'list')

    def set_player(self, player, app):
        self._player = player
        self._app = app
        self.list_view.set_model(Gtk.NoSelection(model=player.queue))
        player.queue.connect('items-changed', lambda *_: self._update_page())
        player.connect('notify::queue-index', lambda *_: self._on_index())
        self._update_page()
        self._on_index()

    def set_active(self, active):
        """Scroll to the entry playing when shown."""
        self._active = active
        if active:
            self._scroll_to_current()

    def _update_page(self):
        self.set_visible_child_name('list' if self._player.queue.get_n_items() else 'empty')

    def _on_index(self):
        index = self._player.queue_index
        for position, row in self._rows.items():
            row.set_current(position == index)
            self._describe(row, position == index)
        if self._active:
            self._scroll_to_current()

    def _scroll_to_current(self):
        index = self._player.queue_index if self._player is not None else -1
        if 0 <= index < self._player.queue.get_n_items():
            self.list_view.scroll_to(index, Gtk.ListScrollFlags.NONE, None)

    def _on_setup(self, _factory, list_item):
        list_item.set_child(QueueRow())

    def _on_bind(self, _factory, list_item):
        position = list_item.get_position()
        row = list_item.get_child()
        self._rows[position] = row
        entry = list_item.get_item()
        current = position == self._player.queue_index
        row.bind(entry, position, current)
        row.list_item = list_item
        title = row.title_label.get_label()
        list_item.set_accessible_label(
            self._label_format.format(title=title, artist=entry.artist) if entry.artist
            else title)
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
        run_command(self._app, self._player.queue_jump(position))
