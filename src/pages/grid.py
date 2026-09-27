"""AppleMusicGridPage: a root page showing a model of Items as a grid of tiles.

The chain is the page's store → Gtk.SortListModel (the header's sort choice, or none) →
Gtk.NoSelection → Gtk.GridView, whose factory recycles AppleMusicTiles. Sorting uses
Gtk.StringSorter and Gtk.NumericSorter over Gtk.PropertyExpressions, which compute one key per
item and sort in C: about 10 ms for 2,000 albums, against 25 ms for a Python Gtk.CustomSorter,
whose cost grows with every comparison rather than every item.
"""

from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from ..library import Item
from ..widgets.tile import Tile


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
    """A destination's grid.

    `model` is a function returning the Gio.ListModel of Items to show (asked again whenever the
    library changes, for models that a load replaces, like a shelf's; None shows nothing).
    `sorts` are SORTS keys: the first is the initial order, and with more than one the header
    offers them; none keeps the model's own order. `artist` makes round portrait tiles. The
    loading and empty states follow the library's state and the model's item count.
    """

    __gtype_name__ = 'AppleMusicGridPage'

    sort_dropdown = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    overlay = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    grid_view = Gtk.Template.Child()
    title_label = Gtk.Template.Child()

    def __init__(self, library, title, model, sorts=(), artist=False, icon_name=None,
                 empty_title=None, empty_description=None):
        super().__init__(title=title)
        self._library = library
        self._get_model = model
        self._artist = artist
        self._library_handlers = []
        self._title_offset = 0
        self._nothing = Gio.ListStore(item_type=Item)  # shown while `model` returns None
        # Looked up once: gettext searches the disk on every call, and binding is hot.
        self._accessible_format = _('{title}, {subtitle}')

        self.title_label.set_label(title)
        self.empty_page.set_icon_name(icon_name)
        self.empty_page.set_title(empty_title or title)
        self.empty_page.set_description(empty_description)

        self._sorters = [SORTS[key][1]() for key in sorts]
        self._sorted = Gtk.SortListModel(
            model=self._model(), sorter=self._sorters[0] if self._sorters else None)
        self._sorted.connect('items-changed', self._update_state)
        if len(sorts) > 1:
            self.sort_dropdown.set_model(Gtk.StringList.new([SORTS[key][0]() for key in sorts]))

        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self._on_setup)
        factory.connect('bind', self._on_bind)
        factory.connect('unbind', self._on_unbind)
        self.grid_view.set_factory(factory)
        self.grid_view.set_model(Gtk.NoSelection(model=self._sorted))

        self.scrolled_window.get_vadjustment().connect('value-changed', self._on_scrolled)
        self._update_state()

    def _model(self):
        model = self._get_model()
        return self._nothing if model is None else model

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
        """Place the title at the top of the overlay, as far up as the grid has scrolled."""
        allocation.x = 0
        allocation.y = -round(self._title_offset)
        allocation.width = overlay.get_width()
        allocation.height = self._title_height()
        return True


