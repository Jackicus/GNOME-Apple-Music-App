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
from ..widgets.tile import Tile
from . import mark_bound


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


def columns_for(width):
    """The columns a grid of tiles `width` px wide shows, or one more when its padding (left
    out here) tips it: never fewer, so a grid told this many as max-columns lays out as it
    would with no limit."""
    return max(2, width // COLUMN_WIDTH)


def estimate_columns():
    """How many columns the content pane fits (columns_for): from its width, or before the
    window has one (the page restored at startup) from the window's default size less the
    sidebar's, or None without a window.

    Gtk.GridView keeps tiles for max-columns columns by about 30 rows (its anchor), however
    few columns it shows: 385 tiles with the default 12, which made opening Artists or All
    Playlists take 200 ms. Told the columns that fit, it makes 30 rows of those: 129 tiles
    at 1100×760, and those pages open in 70 ms."""
    window = Gio.Application.get_default().get_active_window()
    view = getattr(window, 'navigation_view', None)
    if view is None:
        return None
    width = view.get_width()
    if width <= 0:
        width = window.get_default_size()[0]
        split_view = getattr(window, 'split_view', None)
        if split_view is not None and not split_view.get_collapsed():
            width -= split_view.get_max_sidebar_width()
    return columns_for(width) if width > 0 else None


# The sort orders a page can offer: key -> (label, sorter factory). Labels are looked up when a
# page is built, after the launcher has set up gettext. An Item's subtitle is its artist.
SORTS = {
    'title': (lambda: _('Title'), lambda: _string('title')),
    'artist': (lambda: _('Artist'),
               lambda: _chain(_string('subtitle'), _number('year'), _string('title'))),
    'year': (lambda: _('Year'), lambda: _chain(_number('year', descending=True), _string('title'))),
}


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/grid.ui')
class GridPage(Adw.NavigationPage):
    """A destination's grid, or a shelf's (See All).

    `model` is the Gio.ListModel of Items to show, or a function returning it (asked again
    whenever the library changes, for models that a load replaces, like a shelf's; None shows
    nothing). `sorts` are SORTS keys: the first is the initial order, and with more than one the
    header offers them; none keeps the model's own order. `artist` makes round portrait tiles.
    `root` is false for a page pushed over another, which shows its title in the header bar
    too, as detail pages do. The in-content title follows the page's `title`. The loading and
    empty states follow the library's state and the model's item count. A playlist folder's
    page is one too (pages.folder()): its folders are tiles with a folder icon.
    """

    __gtype_name__ = 'AppleMusicGridPage'

    header_bar = Gtk.Template.Child()
    sort_dropdown = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    overlay = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    grid_view = Gtk.Template.Child()
    title_label = Gtk.Template.Child()

    def __init__(self, library, title, model, sorts=(), artist=False, root=True, icon_name=None,
                 empty_title=None, empty_description=None):
        super().__init__(title=title)
        self._library = library
        self._get_model = model if callable(model) else lambda: model
        self._artist = artist
        self._library_handlers = []
        self._title_offset = 0
        self._bound = False  # a tile has been bound (the startup timing's mark)
        self._nothing = Gio.ListStore(item_type=Item)  # shown while `model` returns None
        # Looked up once: gettext searches the disk on every call, and binding is hot.
        self._accessible_format = _('{title}, {subtitle}')

        # The page's title, which a folder's page changes when the folder is renamed.
        self.bind_property('title', self.title_label, 'label', GObject.BindingFlags.SYNC_CREATE)
        self.header_bar.set_show_title(not root)
        self.empty_page.set_icon_name(icon_name)
        self.empty_page.set_title(empty_title or title)
        self.empty_page.set_description(empty_description)

        self._sorters = [SORTS[key][1]() for key in sorts]
        self._sorted = Gtk.SortListModel(
            model=self._model(), sorter=self._sorters[0] if self._sorters else None)
        self._sorted.connect('items-changed', self._update_state)

        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self._on_setup)
        factory.connect('bind', self._on_bind)
        factory.connect('unbind', self._on_unbind)
        self.grid_view.set_factory(factory)
        # Before the model: the grid makes tiles for max-columns columns until it has a
        # width of its own (estimate_columns); on_title_position keeps it fitting after.
        self._columns_idle = None
        self._set_columns(estimate_columns())
        self.grid_view.set_model(Gtk.NoSelection(model=self._sorted))
        context_menu.attach(self.grid_view)
        # After the grid has its model: setting the drop-down's selects its first choice,
        # which scrolls the grid (on_sort_selected).
        if len(sorts) > 1:
            self.sort_dropdown.set_model(Gtk.StringList.new([SORTS[key][0]() for key in sorts]))

        self.scrolled_window.get_vadjustment().connect('value-changed', self._on_scrolled)
        self._update_state()

    def _model(self):
        model = self._get_model()
        return self._nothing if model is None else model

    def _set_columns(self, columns):
        self._columns_idle = None
        if columns is not None and columns != self.grid_view.get_max_columns():
            self.grid_view.set_max_columns(columns)
        return GLib.SOURCE_REMOVE

    # The library outlives the window, so the page listens to it only while it is shown.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._library_handlers = [
            self._library.connect('notify::state', self._update_state),
            self._library.connect('changed', self._on_library_changed),
        ]
        self._on_library_changed()

    def do_unmap(self):
        for handler in self._library_handlers:
            self._library.disconnect(handler)
        self._library_handlers = []
        Adw.NavigationPage.do_unmap(self)

    def _on_library_changed(self, *_args):
        model = self._model()
        if model is not self._sorted.get_model():
            self._sorted.set_model(model)
        self._update_state()

    def _update_state(self, *_args):
        if self._sorted.get_n_items():
            name = 'items'
        elif self._library.state == 'loading':
            name = 'loading'
        else:
            name = 'empty'
        self.stack.set_visible_child_name(name)
        self.sort_dropdown.set_visible(name == 'items' and len(self._sorters) > 1)

    def _on_setup(self, _factory, list_item):
        list_item.set_child(Tile(artist=self._artist))

    def _on_bind(self, _factory, list_item):
        item = list_item.get_item()
        list_item.get_child().bind(item)
        if self._artist or not item.subtitle:
            label = item.title
        else:
            label = self._accessible_format.format(title=item.title, subtitle=item.subtitle)
        list_item.set_accessible_label(label)
        if not self._bound:
            self._bound = True
            mark_bound(self)

    def _on_unbind(self, _factory, list_item):
        list_item.get_child().unbind()

    @Gtk.Template.Callback()
    def on_sort_selected(self, dropdown, _pspec):
        position = dropdown.get_selected()
        if position < len(self._sorters):
            self._sorted.set_sorter(self._sorters[position])
            # Back to the top: the grid would otherwise follow the item it showed to wherever the
            # new order puts it.
            if self._sorted.get_n_items():
                self.grid_view.scroll_to(0, Gtk.ListScrollFlags.NONE, None)

    @Gtk.Template.Callback()
    def on_activate(self, _grid_view, position):
        item = self._sorted.get_item(position)
        if item is not None:
            self.get_root().open_item(item)

    def _on_scrolled(self, adjustment):
        # Only while the title is in view: past it, scrolling needs no layout from here.
        offset = min(adjustment.get_value(), self._title_height())
        if offset != self._title_offset:
            self._title_offset = offset
            self.overlay.queue_allocate()

    def _title_height(self):
        return self.title_label.measure(Gtk.Orientation.VERTICAL, -1)[1]

    @Gtk.Template.Callback()
    def on_title_position(self, overlay, widget, allocation):
        """Place the title at the top of the overlay, as far up as the grid has scrolled.

        Called at each layout of the overlay, so also where the page learns its width: the
        grid's max-columns follows it (after the layout, in an idle), so the grid makes
        tiles for the columns that fit rather than for twelve."""
        allocation.x = 0
        allocation.y = -round(self._title_offset)
        allocation.width = overlay.get_width()
        allocation.height = self._title_height()
        columns = columns_for(overlay.get_width())
        if columns != self.grid_view.get_max_columns() and self._columns_idle is None:
            self._columns_idle = GLib.idle_add(self._set_columns, columns,
                                               priority=GLib.PRIORITY_HIGH_IDLE)
        return True


