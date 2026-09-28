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

Clicking a column header re-sorts; typing in the header's entry refilters (after the entry's
own short delay). Activating a row (Enter, double-click) asks the window to play it; a right
click, a long press or the Menu key opens its context menu, and a row drags onto a sidebar
playlist (widgets/context_menu.py).
"""

from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, Gtk

from ..library import SongOrder, Track, fold
from ..widgets import context_menu
from ..widgets.song_title import SongTitle
from . import mark_bound


def _string_sorter(name):
    return Gtk.StringSorter(expression=Gtk.PropertyExpression.new(Track, None, name))


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/songs.ui')
class SongsPage(Adw.NavigationPage):
    """The Songs destination: library.songs, which it asks the library to build when it is
    first shown."""

    __gtype_name__ = 'AppleMusicSongsPage'

    filter_entry = Gtk.Template.Child()
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
        self._library_handlers = []
        self._songs_handler = None
        self._order = None  # a SongOrder over the songs as they are, made when first needed
        self._ordered = []  # every song, in the chosen order
        self._search = ''  # the filter's text, folded
        self._matches = []  # the ordered songs that match it
        self._shown = []  # what the table's rows hold (the last matches that were any)
        self._bound = False  # a row has been bound (the startup timing's mark)

        self.title_label.set_label(title)
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
        self.artist_column.set_factory(self._text_factory('artist'))
        self.album_column.set_factory(self._text_factory('album'))
        self.time_column.set_factory(self._text_factory('duration_label', numeric=True))

        # Each row reads "title, artist, album" to assistive technology, the time as its
        # description (the format looked up once: rows are rebound all the time).
        self._row_format = _('{title}, {artist}, {album}')
        row_factory = Gtk.SignalListItemFactory()
        row_factory.connect('bind', self._bind_row)
        self.column_view.set_row_factory(row_factory)

        self._rows = Gio.ListStore(item_type=Track)
        self._selection = Gtk.SingleSelection(model=self._rows, autoselect=False)
        self.column_view.set_model(self._selection)
        # Every cell of a row finds the row's Track in its title cell (SongTitle.context_item).
        context_menu.attach(self.column_view, drag=True)

        # The songs are put in this order when the page is realized.
        self.column_view.sort_by_column(self.title_column, Gtk.SortType.ASCENDING)
        self.column_view.get_sorter().connect('changed', self._on_sort_changed)
        self._update_state()

    # The library outlives the window, so the page listens to it only while it is shown, and
    # to the songs store, which the rows must follow even while the page is hidden, while it
    # is realized.

    def do_realize(self):
        Adw.NavigationPage.do_realize(self)
        self._songs_handler = self._library.songs.connect('items-changed',
                                                          self._on_songs_changed)
        self._on_songs_changed()

    def do_unrealize(self):
        self._library.songs.disconnect(self._songs_handler)
        self._songs_handler = None
        Adw.NavigationPage.do_unrealize(self)

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._library_handlers = [
            self._library.connect('notify::state', self._update_state),
            self._library.connect('notify::songs-ready', self._update_state),
        ]
        if not self._library.songs_ready:
            self.get_root().get_application().spawn(self._library.build_songs())
        self._update_state()

    def do_unmap(self):
        for handler in self._library_handlers:
            self._library.disconnect(handler)
        self._library_handlers = []
        Adw.NavigationPage.do_unmap(self)

    # Order and filter.

    def _on_songs_changed(self, *_args):
        self._order = None
        self._sort()

    def _on_sort_changed(self, _sorter, _change):
        self._sort()

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
                Gio.Application.get_default().spawn(self._prepare(self._order, key))
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
        """Make the table's rows tracks, from the top.

        Replacing the rows in one splice would make the view destroy the widgets of rows
        whose tracks it no longer has and build new ones. Inserted at the front instead, the
        new rows push the old ones down with the view following them; moving the view back to
        the top leaves the old rows' widgets out of view, where the view recycles them for the
        new rows; removing the old rows then removes rows without widgets.
        """
        if tracks == self._shown:
            return
        old = self._rows.get_n_items()
        self._rows.splice(0, 0, tracks)
        if tracks:
            self.column_view.scroll_to(0, None, Gtk.ListScrollFlags.NONE, None)
        self._rows.splice(len(tracks), old, [])
        self._shown = tracks

    def _update_state(self, *_args):
        total = len(self._ordered)
        if total:
            name = 'items'
        elif self._library.state == 'loading' or not self._library.songs_ready:
            name = 'loading'
        else:
            name = 'empty'
        self.stack.set_visible_child_name(name)
        self.filter_entry.set_visible(name == 'items')
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
        """Filter the table by `text`, as typing it in the header's entry would."""
        self.filter_entry.set_text(text)

    @Gtk.Template.Callback()
    def on_filter_changed(self, entry):
        search = fold(entry.get_text())
        if search != self._search:
            self._search = search
            self._filter()

    @Gtk.Template.Callback()
    def on_stop_search(self, entry):
        entry.set_text('')

    @Gtk.Template.Callback()
    def on_activate(self, _column_view, position):
        track = self._rows.get_item(position)
        if track is not None:
            self.get_root().play_request(track.play, start_with=track.index)

    # Cells. Text columns are Gtk.Inscriptions: their size comes from their line count, not
    # their text, so rebinding a row redraws it without laying it out again.

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

    def _bind_row(self, _factory, row):
        track = row.get_item()
        row.set_accessible_label(self._row_format.format(
            title=track.title, artist=track.artist or '', album=track.album or ''))
        row.set_accessible_description(track.duration_label or '')

    def _setup_title(self, _factory, cell):
        cell.set_child(SongTitle())

    def _bind_title(self, _factory, cell):
        cell.get_child().bind(cell.get_item())
        if not self._bound:
            self._bound = True
            mark_bound(self)

    def _unbind_title(self, _factory, cell):
        cell.get_child().unbind()
