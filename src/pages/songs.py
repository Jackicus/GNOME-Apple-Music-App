# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicSongsPage: every song in the library as a table, sorted by any column and filtered
as you type.

The chain is library.songs → the songs in the chosen column's order (library.SongOrder) → those
matching the filter → a Gio.ListStore of the rows shown → Gtk.SingleSelection →
Gtk.ColumnView. Ordering and filtering happen in Python rather than in a Gtk.SortListModel and
Gtk.FilterListModel because GTK's are slow over Python objects; measured on 30,000 songs
(scripts/demo_library.py --albums 2500):

- GTK's sorters and filters read each track's properties from C, about 3 µs a read into Python.
  The view's Gtk.ColumnViewSorter has no sort keys, so a Gtk.SortListModel over it compares
  pairs and reads both tracks at every comparison: 1.6 to 6 s a click. SongOrder sorts with
  keys it computes once: 5 to 60 ms.
- A Gtk.AnyFilter of three Gtk.StringFilters refilters once per sub-filter it changes: up to
  1.1 s a keystroke; one Gtk.StringFilter over the three fields still takes 60 ms a pass.
  Testing each track's folded search key (Track.search_key) in Python takes 13 ms.
- Whichever model filters, a Gtk.ColumnView keeps 200 rows of widgets alive, and when a change
  removes the items they show it destroys them and builds new ones, about 0.5 ms a row: 100 ms
  and more on the first keystroke. _show() replaces the rows in an order that lets it recycle
  them instead (inserting, moving the view, then removing), so it only rebinds them.

In all, a click re-sorts the table in 30 to 50 ms and a keystroke refilters it in 10 to 50 ms,
rebinding included.

Clicking a column header, or choosing in the header's Sort By menu (the keyboard's way: the
headers take no focus), re-sorts; typing in the header's entry refilters (after the entry's own
short delay), and the new count is announced. A sync that changes the songs brings the rows up
to date with the fewest splices (library.apply_diff), so the table keeps its scroll position;
only a new sort or filter starts it again from the top. Activating a row (Enter, a click: the
view's single-click-activate, docs/decisions.md) asks the window to play it; a right click, a
long press or the Menu key opens its context menu, and a row drags onto a sidebar playlist
(widgets/context_menu.py). The artist and the album are links to their pages
(widgets/track_links.py). The table keeps no selection (a Gtk.NoSelection): GTK selects the
hovered row of a single-click list, which would have followed the pointer around.

The row of the song playing is marked (SongTitle's play icon and bold title, "Playing" as
the row's accessible description): the page follows the Player's `notify::track` while it is
shown, through the title cells and the rows it has bound (widgets/track_row.py's
PlayingMark), and catches up as it maps.
"""

from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, GLib, Gtk

from ..library import SongOrder, Track, apply_diff, fold
from ..widgets import context_menu, track_links
from ..widgets.labels import track_label
from ..widgets.song_title import SongTitle
from ..widgets.track_links import TrackLink
from ..widgets.track_row import PlayingMark
from ..widgets.util import HeaderTitle, MappedHandlers, connect_weak
from . import app, mark_bound

# The columns' names in the Sort By menu's targets and SongOrder's keys.
COLUMNS = ('title', 'artist', 'album', 'time')


def _string_sorter(name):
    return Gtk.StringSorter(expression=Gtk.PropertyExpression.new(Track, None, name))


def songs_state(total, library_state, songs_ready, song_count, syncing=False):
    """What the Songs page shows: 'items' once it has ordered songs (`total`); 'loading' while
    the library loads, before the songs are built, while songs exist that are not ordered yet
    (`song_count`: SongOrder.prepare() runs over frames), and while the first sync fills an
    empty library (`syncing`); 'empty' when there are no songs at all."""
    if total:
        return 'items'
    if (library_state == 'loading' or not songs_ready or song_count
            or (library_state == 'empty' and syncing)):
        return 'loading'
    return 'empty'


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/songs.ui')
class SongsPage(Adw.NavigationPage):
    """The Songs destination: library.songs, which it asks the library to build when it is
    first shown."""

    __gtype_name__ = 'AppleMusicSongsPage'

    header_bar = Gtk.Template.Child()
    filter_entry = Gtk.Template.Child()
    sort_button = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    count_label = Gtk.Template.Child()
    results_stack = Gtk.Template.Child()
    column_view = Gtk.Template.Child()
    title_column = Gtk.Template.Child()
    artist_column = Gtk.Template.Child()
    album_column = Gtk.Template.Child()
    time_column = Gtk.Template.Child()

    def __init__(self, library, title, icon_name=None):
        super().__init__(title=title)
        self._library = library
        self._songs_handler = None
        self._order = None  # a SongOrder over the songs as they are, made when first needed
        self._ordered = []  # every song, in the chosen order
        self._search = ''  # the filter's text, folded
        self._matches = []  # the ordered songs that match it
        self._shown = []  # what the table's rows hold (the last matches that were any)
        self._follow = False  # the songs changed: the rows follow, keeping their place
        self._prepare_task = None  # the SongOrder being prepared
        self._quiet = False  # the page is changing the sort itself
        self._bound = False  # a row has been bound (the startup timing's mark)
        self._title_cells = set()  # the title cells bound: marked again as the item playing changes
        self._row_items = set()  # and the rows bound (their descriptions)

        self.title_label.set_label(title)
        self._header_title = HeaderTitle(self.header_bar, self.title_label)  # while loading
        self.empty_page.set_icon_name(icon_name)
        self.empty_page.set_title(_('No Songs'))
        self.empty_page.set_description(_('Songs in your library appear here'))

        # column -> its SongOrder key. The sorters make the headers clickable and say what the
        # order is; SongOrder does the sorting.
        self._columns = {
            self.title_column: 'title',
            self.artist_column: 'artist',
            self.album_column: 'album',
            self.time_column: 'time',
        }
        self.title_column.set_sorter(_string_sorter('title'))
        self.artist_column.set_sorter(_string_sorter('artist'))
        self.album_column.set_sorter(_string_sorter('album'))
        self.time_column.set_sorter(Gtk.NumericSorter(
            expression=Gtk.PropertyExpression.new(Track, None, 'duration_ms')))

        self.title_column.set_factory(self._factory(self._setup_title, self._bind_title,
                                                    self._unbind_title))
        self.artist_column.set_factory(self._link_factory('artist'))
        self.album_column.set_factory(self._link_factory('album'))
        self.time_column.set_factory(self._text_factory('duration_label', numeric=True))

        # Each row reads its title, artist and album to assistive technology (labels.py),
        # the time (or "Playing") as its description.
        row_factory = Gtk.SignalListItemFactory()
        row_factory.connect('bind', self._bind_row)
        row_factory.connect('unbind', self._unbind_row)
        self.column_view.set_row_factory(row_factory)

        self._rows = Gio.ListStore(item_type=Track)
        self.column_view.set_model(Gtk.NoSelection(model=self._rows))
        # Every cell of a row finds the row's Track in its title cell (SongTitle.context_item).
        context_menu.attach(self.column_view, drag=True)
        track_links.attach(self.column_view)

        # The Sort By menu's actions (songs.sort-column, songs.sort-order), following the
        # headers too; the songs are put in this order when the page is realized.
        self._sort_column = Gio.SimpleAction.new_stateful(
            'sort-column', GLib.VariantType.new('s'), GLib.Variant('s', 'title'))
        self._sort_order = Gio.SimpleAction.new_stateful(
            'sort-order', GLib.VariantType.new('s'), GLib.Variant('s', 'ascending'))
        actions = Gio.SimpleActionGroup()
        for action in (self._sort_column, self._sort_order):
            connect_weak(action, 'change-state', self._on_sort_chosen)
            actions.add_action(action)
        self.insert_action_group('songs', actions)
        self.column_view.sort_by_column(self.title_column, Gtk.SortType.ASCENDING)
        self.column_view.get_sorter().connect('changed', self._on_sort_changed)
        self._handlers = MappedHandlers(self)
        self._handlers.add(library, 'notify::state', self._update_state)
        self._handlers.add(library, 'notify::songs-ready', self._update_state)
        self._handlers.add(library, 'notify::syncing', self._update_state)
        # The item playing (a test's stand-in application may have no player).
        self._playing = PlayingMark(getattr(app(), 'player', None))
        if self._playing.player is not None:
            self._handlers.add(self._playing.player, 'notify::track', self._on_track_changed)
        self._update_state()

    # The library and the player outlive the window, so the page listens to them only while
    # it is shown (self._handlers), and to the songs store, which the rows must follow even
    # while the page is hidden, while it is realized.

    def do_realize(self):
        Adw.NavigationPage.do_realize(self)
        self._songs_handler = self._library.songs.connect('items-changed',
                                                          self._on_songs_changed)
        self._on_songs_changed()

    def do_unrealize(self):
        self._library.songs.disconnect(self._songs_handler)
        self._songs_handler = None
        self._cancel_prepare()
        Adw.NavigationPage.do_unrealize(self)

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        if not self._library.songs_ready:
            app().spawn(self._library.build_songs())
        self._update_state()
        self._update_playing()  # the item playing now, changed or not while hidden

    # Order and filter.

    def _on_songs_changed(self, *_args):
        self._order = None
        self._cancel_prepare()
        self._follow = True  # a sync's change: the rows keep their place
        self._sort()

    def _cancel_prepare(self):
        if self._prepare_task is not None and not self._prepare_task.done():
            self._prepare_task.cancel()
        self._prepare_task = None

    def _on_sort_changed(self, sorter, _change):
        if self._quiet:
            return
        column = self._columns.get(sorter.get_primary_sort_column())
        if column is not None:  # the menu follows a header's click
            descending = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
            self._sort_column.set_state(GLib.Variant('s', column))
            self._sort_order.set_state(
                GLib.Variant('s', 'descending' if descending else 'ascending'))
        self._follow = False
        self._sort()

    def _on_sort_chosen(self, action, value):
        """The Sort By menu: sort as a header click would, its arrow on its column."""
        action.set_state(value)
        column = next(column for column, key in self._columns.items()
                      if key == self._sort_column.get_state().get_string())
        order = (Gtk.SortType.DESCENDING
                 if self._sort_order.get_state().get_string() == 'descending'
                 else Gtk.SortType.ASCENDING)
        self._quiet = True
        try:
            self.column_view.sort_by_column(None, order)  # the previous column's arrow goes
        finally:
            self._quiet = False
        self.column_view.sort_by_column(column, order)

    def _sort(self):
        sorter = self.column_view.get_sorter()
        key = self._columns.get(sorter.get_primary_sort_column())
        if key is None:
            self._ordered = list(self._library.songs)
        else:
            if self._order is None:
                # A new order over new songs: its keys are made a few thousand tracks at a
                # time with frames between (SongOrder.prepare), then this runs again. Sorting
                # by a column whose keys exist (a header click) is immediate.
                self._order = SongOrder(self._library.songs)
                self._prepare_task = app().spawn(self._prepare(self._order, key))
                return
            descending = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
            self._ordered = self._order.tracks(key, descending)
        self._filter()

    async def _prepare(self, order, key):
        await order.prepare(key)
        if self._order is order:  # the songs have not changed meanwhile
            self._sort()

    def _filter(self):
        search = self._search
        if search:
            self._matches = [track for track in self._ordered if search in track.search_key]
        else:
            self._matches = self._ordered
        # With no matches the rows stay as they were, hidden behind "No Results Found": the
        # next matches can then recycle their widgets rather than build new ones.
        if self._matches or not self._ordered:
            self._show(self._matches)
        self._update_state()

    def _show(self, tracks):
        """Make the table's rows tracks: where they were, after a sync changed the songs
        (the fewest splices, library.apply_diff: runs of the same tracks stay, and the view
        its scroll position); from the top, after a new sort or filter.

        Replacing the rows in one splice would make the view destroy the widgets of rows
        whose tracks it no longer has and build new ones. Inserted at the front instead, the
        new rows push the old ones down with the view following them; moving the view back to
        the top leaves the old rows' widgets out of view, where the view recycles them for the
        new rows; removing the old rows then removes rows without widgets.
        """
        follow, self._follow = self._follow, False
        if tracks == self._shown:
            return
        if follow and self._shown:
            apply_diff(self._rows, tracks, old=self._shown)
            self._shown = tracks
            return
        old = self._rows.get_n_items()
        self._rows.splice(0, 0, tracks)
        if tracks:
            self.column_view.scroll_to(0, None, Gtk.ListScrollFlags.NONE, None)
        self._rows.splice(len(tracks), old, [])
        self._shown = tracks

    def _update_state(self, *_args):
        total = len(self._ordered)
        library = self._library
        name = songs_state(total, library.state, library.songs_ready,
                           library.songs.get_n_items(), library.syncing)
        self.stack.set_visible_child_name(name)
        self.filter_entry.set_visible(name == 'items')
        self.sort_button.set_visible(name == 'items')
        if name != 'items':
            return
        shown = len(self._matches)
        self.results_stack.set_visible_child_name('table' if shown else 'no-results')
        if self._search:
            label = ngettext('{shown} of {total} song', '{shown} of {total} songs', total)
        else:
            label = ngettext('{total} song', '{total} songs', total)
        self.count_label.set_label(label.format(shown=f'{shown:n}', total=f'{total:n}'))

    def set_filter(self, text):
        """Filter the table by `text` at once, as typing it in the header's entry would after
        its short delay (See All from a search's songs: the whole table must not show first)."""
        self.filter_entry.set_text(text)
        self.on_filter_changed(self.filter_entry)

    def focus_filter(self):
        """Put the cursor in the filter with its text selected (Ctrl+F on this page); False
        when the page has no filter to show yet (no songs)."""
        if not self.filter_entry.get_visible():
            return False
        self.filter_entry.grab_focus()
        self.filter_entry.select_region(0, -1)
        return True

    @Gtk.Template.Callback()
    def on_filter_changed(self, entry):
        search = fold(entry.get_text())
        if search != self._search:
            self._search = search
            self._follow = False
            self._filter()
            self._announce_count()

    def _announce_count(self):
        """Say the new count once the typing has paused (the entry's own delay), not at
        every keystroke."""
        root = self.get_root()
        if root is not None and self.get_mapped() and self.count_label.get_mapped():
            root.announce(self.count_label.get_label(), Gtk.AccessibleAnnouncementPriority.LOW)

    @Gtk.Template.Callback()
    def on_stop_search(self, entry):
        entry.set_text('')

    @Gtk.Template.Callback()
    def on_activate(self, _column_view, position):
        track = self._rows.get_item(position)
        if track is not None:
            self.get_root().play_request(track.play, start_with=track.index,
                                         start_id=track.id)

    # Cells. The time is a Gtk.Inscription: its size comes from its line count, not its text,
    # so rebinding it redraws it without laying it out again. The title, the artist and the
    # album are one-line labels (the badge follows the title's text, a link is only its
    # text), which a single line keeps cheap to measure again.

    def _factory(self, setup, bind, unbind=None):
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', setup)
        factory.connect('bind', bind)
        if unbind is not None:
            factory.connect('unbind', unbind)
        return factory

    def _text_factory(self, name, numeric=False):
        def setup(_factory, cell):
            # Centred at its one line's height: given the row's height, it would wrap text
            # too long for the column onto a second line.
            inscription = Gtk.Inscription(xalign=1 if numeric else 0, valign=Gtk.Align.CENTER,
                                          text_overflow=Gtk.InscriptionOverflow.ELLIPSIZE_END)
            if numeric:
                inscription.add_css_class('numeric')
            cell.set_child(inscription)

        def bind(_factory, cell):
            cell.get_child().set_text(getattr(cell.get_item(), name))

        return self._factory(setup, bind)

    def _link_factory(self, kind):
        """The artist's or the album's cells: TrackLinks, one-line labels as wide as their
        text, so that only the text is the link."""
        def setup(_factory, cell):
            cell.set_child(TrackLink(kind))

        def bind(_factory, cell):
            cell.get_child().show(cell.get_item())

        return self._factory(setup, bind)

    def _bind_row(self, _factory, row):
        track = row.get_item()
        self._row_items.add(row)
        row.set_accessible_label(track_label(track))
        row.set_accessible_description(
            self._playing.description(track, self._playing.matches(track)))

    def _unbind_row(self, _factory, row):
        self._row_items.discard(row)

    def _setup_title(self, _factory, cell):
        cell.set_child(SongTitle())

    def _bind_title(self, _factory, cell):
        track = cell.get_item()
        self._title_cells.add(cell)
        cell.get_child().bind(track, self._playing.matches(track))
        if not self._bound:
            self._bound = True
            mark_bound(self)

    def _unbind_title(self, _factory, cell):
        self._title_cells.discard(cell)
        cell.get_child().unbind()

    # The song playing.

    def _on_track_changed(self, _player, _pspec):
        self._update_playing()

    def _update_playing(self):
        """Mark the row of the song playing now, and no other, when the item changed."""
        if not self._playing.update():
            return
        for cell in self._title_cells:
            cell.get_child().set_playing(self._playing.matches(cell.get_item()))
        for row in self._row_items:
            track = row.get_item()
            row.set_accessible_description(
                self._playing.description(track, self._playing.matches(track)))
