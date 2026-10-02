# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicGridPage: a root page showing a model of Items as a grid of tiles.

The chain is the page's store → Gtk.SortListModel (the header's sort choice, or none) →
ReversedModel (that order backwards, for a descending text) → Gtk.NoSelection → Gtk.GridView,
whose factory recycles AppleMusicTiles. Sorting uses Gtk.StringSorter and Gtk.NumericSorter
over Gtk.PropertyExpressions, which compute one key per item and sort in C: about 10 ms for
2,000 albums, against 25 ms for a Python Gtk.CustomSorter, whose cost grows with every
comparison rather than every item.
"""

from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..library import Item
from ..widgets import context_menu
from ..widgets.labels import bind_label, unbind_label
from ..widgets.tile import Tile
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
# view's child (3 px a side). Gtk.GridView divides its width by it, and a tile sits at the
# start of its column, so the first column's cover is on the grid's margin (grid.blp: 21 px,
# the page's 24 less the child's padding) whatever the width.
COLUMN_WIDTH = 166


def columns_for(width):
    """The columns a grid of tiles in a page `width` px wide shows, or one more when the
    grid's margins (left out here) tip it: never fewer, so a grid told this many as
    max-columns lays out as it would with no limit."""
    return max(2, width // COLUMN_WIDTH)


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


# The sort orders a page can offer: key -> (label, the Item properties compared in turn,
# whether the key's own direction is descending). Labels are looked up when a page is built,
# after the launcher has set up gettext. An Item's subtitle is its artist; Year is newest
# first unless asked the other way.
SORTS = {
    'title': (lambda: _('Title'), ('title',), False),
    'artist': (lambda: _('Artist'), ('subtitle', 'year', 'title'), False),
    'year': (lambda: _('Year'), ('year', 'title'), True),
}
# The properties compared as numbers; the rest are text.
NUMBERS = frozenset({'year'})


def make_sorter(key, descending=False):
    """The Gtk.Sorter for SORTS[key] in one direction, and whether its order is to be shown
    backwards. Descending turns the first property round and leaves the rest breaking ties as
    they do ascending, as the Songs table's order does (library.SongOrder): a number's
    Gtk.NumericSorter takes the direction, but Gtk.StringSorter sorts one way, so a text's
    descending order is its ascending one shown backwards (ReversedModel), ties and all."""
    names = SORTS[key][1]
    backwards = descending and names[0] not in NUMBERS
    sorters = [_number(name, descending and name == names[0]) if name in NUMBERS
               else _string(name) for name in names]
    return (_chain(*sorters) if len(sorters) > 1 else sorters[0]), backwards


def _direction(descending):
    return 'descending' if descending else 'ascending'


class ReversedModel(GObject.Object, Gio.ListModel):
    """`model`'s items in its own order or, set `backwards`, from the last to the first: a
    grid's descending text order (make_sorter). Nothing is copied or sorted: a position here
    maps to the model's, and the model's changes to positions here."""

    def __init__(self, model):
        super().__init__()
        self._model = model
        self._backwards = False
        connect_weak(model, 'items-changed', self._on_items_changed)

    @property
    def backwards(self):
        return self._backwards

    def set_backwards(self, backwards):
        if backwards == self._backwards:
            return
        self._backwards = backwards
        count = self._model.get_n_items()
        if count > 1:
            self.items_changed(0, count, count)

    def do_get_item_type(self):
        return self._model.get_item_type()

    def do_get_n_items(self):
        return self._model.get_n_items()

    def do_get_item(self, position):
        if self._backwards:
            position = self._model.get_n_items() - 1 - position
        return self._model.get_item(position)

    def _on_items_changed(self, model, position, removed, added):
        if self._backwards:
            position = model.get_n_items() - position - added
        self.items_changed(position, removed, added)


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/grid.ui')
class GridPage(Adw.NavigationPage):
    """A destination's grid, or a shelf's (See All).

    `model` is the Gio.ListModel of Items to show, or a function returning it (asked again
    whenever the library changes, for models that a load replaces, like a shelf's; None
    shows nothing, as the missing state: `missing_title`, `missing_description`, a folder
    that is gone). `sorts` are SORTS keys: the first is the initial order, and with more than
    one the header's Sort By menu offers them (the page.sort action) and Ascending or
    Descending (page.sort-order): a key chosen comes its own way round (Year newest first,
    the rest A to Z) and the direction turns it; the key is remembered per root page in the
    grid-sort setting, the direction for the page's life. No keys keeps the model's own
    order. `artist` makes round portrait tiles. `root` is false for a page pushed over
    another. Root or pushed, the header bar shows the title while the big one is out of view
    (HeaderTitle). The in-content title follows the page's `title`, on the page's margin,
    where the first column's cover is. The loading and empty states follow the library's
    state (the first sync filling an empty library is loading) and the model's item count. A
    playlist folder's page is one too (pages.folder()): its folders are tiles with a folder
    icon.
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
        self._order = (sorts[0], SORTS[sorts[0]][2]) if sorts else None  # (key, descending)
        sorter, backwards = make_sorter(*self._order) if sorts else (None, False)
        self._sorted = Gtk.SortListModel(model=self._model(), sorter=sorter)
        self._shown = ReversedModel(self._sorted)
        self._shown.set_backwards(backwards)
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
        self.grid_view.set_model(Gtk.NoSelection(model=self._shown))
        context_menu.attach(self.grid_view)

        # Sort By: a menu of the orders and of the directions, as radio items of the stateful
        # page.sort and page.sort-order actions.
        self._sort_restored = False  # the remembered choice applied (once the page's tag is)
        self._sort_action = Gio.SimpleAction.new_stateful(
            'sort', GLib.VariantType.new('s'), GLib.Variant('s', sorts[0] if sorts else ''))
        self._order_action = Gio.SimpleAction.new_stateful(
            'sort-order', GLib.VariantType.new('s'), GLib.Variant('s', _direction(backwards)))
        connect_weak(self._sort_action, 'change-state', self._on_sort_chosen)
        connect_weak(self._order_action, 'change-state', self._on_order_chosen)
        actions = Gio.SimpleActionGroup()
        actions.add_action(self._sort_action)
        actions.add_action(self._order_action)
        self.insert_action_group('page', actions)
        menu = Gio.Menu()
        keys = Gio.Menu()
        for key in sorts:
            keys.append(SORTS[key][0](), f'page.sort::{key}')
        menu.append_section(None, keys)
        directions = Gio.Menu()
        directions.append(_('Ascending'), 'page.sort-order::ascending')
        directions.append(_('Descending'), 'page.sort-order::descending')
        menu.append_section(None, directions)
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

    def _on_order_chosen(self, action, value):
        self._sort(self._sort_action.get_state().get_string(),
                   descending=value.get_string() == 'descending')

    def _restore_sort(self):
        """The order last chosen on this root page (its tag), if it still offers it."""
        tag = self.get_tag()
        settings = getattr(app(), 'settings', None)
        if not tag or settings is None:
            return
        key = settings.get_value('grid-sort').unpack().get(tag)
        if key in self._sort_keys:
            self._sort(key)

    def _sort(self, key, descending=None):
        """Show the items by `key`, `descending` or not (None: the key's own direction)."""
        if key not in self._sort_keys:
            return
        if descending is None:
            descending = SORTS[key][2]
        if (key, descending) != self._order:
            self._order = (key, descending)
            self._sort_action.set_state(GLib.Variant('s', key))
            self._order_action.set_state(GLib.Variant('s', _direction(descending)))
            sorter, backwards = make_sorter(key, descending)
            self._shown.set_backwards(backwards)
            self._sorted.set_sorter(sorter)
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
        item = self._shown.get_item(position)
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
        """Place the title at the top of the overlay, as far up as the grid has scrolled; its
        own margins put it on the page's.

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
        allocation.x = 0
        allocation.y = -round(self._title_offset)
        allocation.width = width
        allocation.height = self._title_height()
        return True
