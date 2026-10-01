# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicGridPage: a root page showing a model of Items as a grid of tiles.

The chain is the page's store → Gtk.SortListModel (the header's sort choice, or none) →
Gtk.NoSelection → Gtk.GridView, whose factory recycles AppleMusicTiles. Sorting uses
Gtk.StringSorter and Gtk.NumericSorter over Gtk.PropertyExpressions, which compute one key per
item and sort in C: about 10 ms for 2,000 albums, against 25 ms for a Python Gtk.CustomSorter,
whose cost grows with every comparison rather than every item.
"""

from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..library import Item
from ..widgets import context_menu
from ..widgets.labels import bind_label, unbind_label
from ..widgets.tile import ART_SIZE, Tile
from ..widgets.util import HeaderTitle, MappedHandlers, connect_weak
from . import SignInOffer, app, mark_bound


def _string(name):
    return Gtk.StringSorter(expression=Gtk.PropertyExpression.new(Item, None, name))


def _number(name, descending=False):
    return Gtk.NumericSorter(
        expression=Gtk.PropertyExpression.new(Item, None, name),
        sort_order=Gtk.SortType.DESCENDING if descending else Gtk.SortType.ASCENDING)


def _chain(*sorters):
    multi = Gtk.MultiSorter()
    for sorter in sorters:
        multi.append(sorter)
    return multi


# The width of a grid column: a tile (tile.blp, 160 px) and the padding Adwaita gives a grid
# view's child (3 px a side). Gtk.GridView divides its width, less its own CSS padding, by it.
COLUMN_WIDTH = 166
# The grid view's own padding at each side (style.css: gridview.tile-grid).
GRID_PADDING = 6
# The title's margin at its start (grid.blp), which puts it on the page's 24 px column.
TITLE_MARGIN = 24


def columns_for(width):
    """The columns a grid of tiles `width` px wide shows, or one more when its padding (left
    out here) tips it: never fewer, so a grid told this many as max-columns lays out as it
    would with no limit."""
    return max(2, width // COLUMN_WIDTH)


def cover_start(width, max_columns):
    """Where the first column's cover starts in a grid `width` px wide: the grid divides its
    width (less its padding) among the columns that fit, and a tile is centred in its
    column. The page's title starts there too."""
    inner = width - 2 * GRID_PADDING
    columns = max(2, min(max_columns, inner // COLUMN_WIDTH))
    return GRID_PADDING + (inner / columns - ART_SIZE) / 2


def estimate_columns():
    """How many columns the content pane fits (columns_for), from its width
    (Window.content_width(), an estimate before the window has laid it out: the page restored
    at startup), or None without a window.

    Gtk.GridView keeps tiles for max-columns columns by about 30 rows (its anchor), however
    few columns it shows: 385 tiles with the default 12, which made opening Artists or All
    Playlists take 200 ms. Told the columns that fit, it makes 30 rows of those: 129 tiles
    at 1100×760, and those pages open in 70 ms."""
    application = app()
    window = application.get_active_window() if application is not None else None
    content_width = getattr(window, 'content_width', None)
    width = content_width() if content_width is not None else 0
    return columns_for(width) if width and width > 0 else None


# The sort orders a page can offer: key -> (label, sorter factory). Labels are looked up when a
# page is built, after the launcher has set up gettext. An Item's subtitle is its artist.
SORTS = {
    'title': (lambda: _('Title'), lambda: _string('title')),
    'artist': (lambda: _('Artist'),
               lambda: _chain(_string('subtitle'), _number('year'), _string('title'))),
    'year': (lambda: _('Year'), lambda: _chain(_number('year', descending=True), _string('title'))),
}


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/grid.ui')
class GridPage(Adw.NavigationPage):
    """A destination's grid, or a shelf's (See All).

    `model` is the Gio.ListModel of Items to show, or a function returning it (asked again
    whenever the library changes, for models that a load replaces, like a shelf's; None
    shows nothing, as the missing state: `missing_title`, `missing_description`, a folder
    that is gone). `sorts` are SORTS keys: the first is the initial order, and with more than
    one the header's Sort By menu offers them (the page.sort action), the choice remembered
    per root page in the grid-sort setting; none keeps the model's own order. `artist` makes
    round portrait tiles. `root` is false for a page pushed over another. Root or pushed, the
    header bar shows the title while the big one is out of view (HeaderTitle). The
    in-content title follows the page's `title` and starts where the first column's cover
    does. The loading and empty states follow the library's state (the first sync filling an
    empty library is loading) and the model's item count. A playlist folder's page is one
    too (pages.folder()): its folders are tiles with a folder icon.
    """

    __gtype_name__ = 'AppleMusicGridPage'

    header_bar = Gtk.Template.Child()
    sort_button = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    overlay = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    grid_view = Gtk.Template.Child()
    title_label = Gtk.Template.Child()

    def __init__(self, library, title, model, sorts=(), artist=False, root=True, icon_name=None,
                 empty_title=None, empty_description=None, missing_title=None,
                 missing_description=None):
        super().__init__(title=title)
        self._library = library
        self._get_model = model if callable(model) else lambda: model
        self._artist = artist
        self._title_offset = 0
        self._bound = False  # a tile has been bound (the startup timing's mark)
        self._nothing = Gio.ListStore(item_type=Item)  # shown while `model` returns None
        self._missing = False  # `model` returned None
        self._empty = (empty_title or title, empty_description)
        self._missing_texts = (missing_title or self._empty[0],
                               missing_description or self._empty[1])

        # The page's title, which a folder's page changes when the folder is renamed.
        self.bind_property('title', self.title_label, 'label', GObject.BindingFlags.SYNC_CREATE)
        self.empty_page.set_icon_name(icon_name)
        self._sign_in = SignInOffer(self.empty_page)  # Sign In… while signed out

        self._sort_keys = tuple(sorts)
        self._sorters = {key: SORTS[key][1]() for key in sorts}
        self._sorted = Gtk.SortListModel(
            model=self._model(), sorter=self._sorters[sorts[0]] if sorts else None)
        # Every signal of a child or of an object the page holds is connected weakly
        # (widgets/util.py): a bound method would keep the page alive once popped.
        connect_weak(self._sorted, 'items-changed', self._update_state)

        factory = Gtk.SignalListItemFactory()
        connect_weak(factory, 'setup', self._on_setup)
        connect_weak(factory, 'bind', self._on_bind)
        connect_weak(factory, 'unbind', self._on_unbind)
        self.grid_view.set_factory(factory)
        connect_weak(self.grid_view, 'activate', self._on_activate)
        connect_weak(self.overlay, 'get-child-position', self._on_title_position)
        # Before the model: the grid makes tiles for max-columns columns until it has a
        # width of its own (estimate_columns); _on_title_position keeps it fitting after.
        self._columns_idle = None
        self._set_columns(estimate_columns())
        self.grid_view.set_model(Gtk.NoSelection(model=self._sorted))
        context_menu.attach(self.grid_view)

        # Sort By: a menu of the orders, a stateful page.sort action.
        self._sort_restored = False  # the remembered choice applied (once the page's tag is)
        self._sort_action = Gio.SimpleAction.new_stateful(
            'sort', GLib.VariantType.new('s'), GLib.Variant('s', sorts[0] if sorts else ''))
        connect_weak(self._sort_action, 'change-state', self._on_sort_chosen)
        actions = Gio.SimpleActionGroup()
        actions.add_action(self._sort_action)
        self.insert_action_group('page', actions)
        menu = Gio.Menu()
        for key in sorts:
            menu.append(SORTS[key][0](), f'page.sort::{key}')
        self.sort_button.set_menu_model(menu)

        connect_weak(self.scrolled_window.get_vadjustment(), 'value-changed', self._on_scrolled)
        self._header_title = HeaderTitle(self.header_bar, self.title_label, self.scrolled_window)
        # The library outlives the window: the page follows it only while it is shown.
        self._handlers = MappedHandlers(self)
        self._handlers.add(library, 'notify::state', self._update_state)
        self._handlers.add(library, 'notify::syncing', self._update_state)
        self._handlers.add(library, 'changed', self._on_library_changed)
        self._update_state()

    def _model(self):
        model = self._get_model()
        self._missing = model is None
        return self._nothing if model is None else model

    def _set_columns(self, columns):
        self._columns_idle = None
        if columns is not None and columns != self.grid_view.get_max_columns():
            self.grid_view.set_max_columns(columns)
        return GLib.SOURCE_REMOVE

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        if not self._sort_restored:
            self._sort_restored = True
            self._restore_sort()
        self._on_library_changed()

    def _on_library_changed(self, *_args):
        model = self._model()
        if model is not self._sorted.get_model():
            self._sorted.set_model(model)
        self._update_state()

    def _update_state(self, *_args):
        library = self._library
        if self._sorted.get_n_items():
            name = 'items'
        elif library.state == 'loading' or (library.state == 'empty' and library.syncing):
            name = 'loading'  # the first sync is on its way
        else:
            name = 'empty'
            title, description = self._missing_texts if self._missing else self._empty
            self.empty_page.set_title(title)
            self._sign_in.set_description(description)
        self.stack.set_visible_child_name(name)
        self.sort_button.set_visible(name == 'items' and len(self._sort_keys) > 1)

    # Sorting.

    def _on_sort_chosen(self, action, value):
        self._sort(value.get_string())
        tag = self.get_tag()
        settings = getattr(app(), 'settings', None)
        if tag and settings is not None:  # a root page: remembered
            sorts = dict(settings.get_value('grid-sort').unpack())
            sorts[tag] = value.get_string()
            settings.set_value('grid-sort', GLib.Variant('a{ss}', sorts))

    def _restore_sort(self):
        """The order last chosen on this root page (its tag), if it still offers it."""
        tag = self.get_tag()
        settings = getattr(app(), 'settings', None)
        if not tag or settings is None:
            return
        key = settings.get_value('grid-sort').unpack().get(tag)
        if key in self._sort_keys and key != self._sort_action.get_state().get_string():
            self._sort(key)

    def _sort(self, key):
        if key not in self._sorters:
            return
        self._sort_action.set_state(GLib.Variant('s', key))
        self._sorted.set_sorter(self._sorters[key])
        # Back to the top: the grid would otherwise follow the item it showed to wherever the
        # new order puts it.
        if self._sorted.get_n_items():
            self.grid_view.scroll_to(0, Gtk.ListScrollFlags.NONE, None)

    # The tiles.

    def _on_setup(self, _factory, list_item):
        list_item.set_child(Tile(artist=self._artist))

    def _on_bind(self, _factory, list_item):
        item = list_item.get_item()
        list_item.get_child().bind(item)
        bind_label(list_item, item, self._artist)
        if not self._bound:
            self._bound = True
            mark_bound(self)

    def _on_unbind(self, _factory, list_item):
        list_item.get_child().unbind()
        unbind_label(list_item)

    def _on_activate(self, _grid_view, position):
        item = self._sorted.get_item(position)
        if item is not None:
            self.get_root().open_item(item)

    # The title, laid over the grid's top padding.

    def _on_scrolled(self, adjustment):
        # Only while the title is in view: past it, scrolling needs no layout from here.
        offset = min(adjustment.get_value(), self._title_height())
        if offset != self._title_offset:
            self._title_offset = offset
            self.overlay.queue_allocate()

    def _title_height(self):
        return self.title_label.measure(Gtk.Orientation.VERTICAL, -1)[1]

    def _on_title_position(self, overlay, widget, allocation):
        """Place the title at the top of the overlay, as far up as the grid has scrolled,
        starting where the first column's cover does.

        Called at each layout of the overlay, so also where the page learns its width: the
        grid's max-columns follows it, so the grid makes tiles for the columns that fit
        rather than for twelve: more at once (a window that grew paints them in this frame),
        fewer after the layout, in an idle."""
        width = overlay.get_width()
        columns = columns_for(width)
        current = self.grid_view.get_max_columns()
        if columns > current:
            self.grid_view.set_max_columns(columns)
        elif columns < current and self._columns_idle is None:
            self._columns_idle = GLib.idle_add(self._set_columns, columns,
                                               priority=GLib.PRIORITY_HIGH_IDLE)
        allocation.x = round(cover_start(width, self.grid_view.get_max_columns()) - TITLE_MARGIN)
        allocation.y = -round(self._title_offset)
        allocation.width = width - allocation.x
        allocation.height = self._title_height()
        return True
