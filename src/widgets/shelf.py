"""AppleMusicShelf: a titled, horizontally scrolling row of tiles; ShelfColumn: a page's
shelves, one under another."""

from gi.repository import Adw, Gio, GObject, Gtk

from . import context_menu
from .hero_tile import HeroTile
from .labels import bind_label, unbind_label
from .tile import Tile
from .util import connect_weak, next_frame

# The tiles a shelf's row shows at most: See All opens the whole shelf. A horizontal
# Gtk.ListView makes a tile for every item up to about 200, whatever is on screen (100 items
# made 100 tiles, 2.4 MB and 15-20 ms), and music.apple.com's rows show about this many.
ROW_LIMIT = 30

# How long a click on a paging arrow takes to scroll the row, in milliseconds.
PAGE_DURATION = 250


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/shelf.ui')
class Shelf(Gtk.Box):
    """A shelf of Items (a library.ShelfModel, or any object with `key`, `title` and `items`,
    bound with bind_shelf()) as a row of tiles under its title, with paging arrows while the
    row overflows and a "See All" button (`see-all`: offered, and shown when the row does not
    show everything: more items than ROW_LIMIT, or wider than the window), which opens the
    shelf as a grid (window.open_shelf). Activating a tile opens its item (window.open_item);
    a right click, a long press or the Menu key opens its context menu (context_menu.py).

    The row is a horizontal Gtk.ListView of the first ROW_LIMIT items, the scrolled window's
    own child. It builds a tile for each of them, but only the tiles on screen are mapped and
    ask for their artwork. With `hero`, the tiles are 260 px AppleMusicHeroTiles, the cover
    over a band in its colour; otherwise 160 px AppleMusicTiles, round for artists. The shelf
    follows its ShelfModel's title and its items as they change.

    Scrolling: the row's scrolled window scrolls only horizontally, and GtkScrolledWindow
    handles a scroll event only along an axis it can scroll, so a vertical wheel over the row
    goes on to the page's scrolled window, and a horizontal one (a touchpad, a tilting wheel,
    Shift and the wheel) moves the row. A row that fits scrolls neither way.

    GtkBuilder builds some (radio.blp, search.blp): it calls __init__ without arguments and
    sets the properties after it, where a Python caller's are set before __init__'s body
    runs, so the state the property setters read starts as class attributes.
    """

    __gtype_name__ = 'AppleMusicShelf'

    title_label = Gtk.Template.Child()
    previous_button = Gtk.Template.Child()
    next_button = Gtk.Template.Child()
    see_all_button = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    list_view = Gtk.Template.Child()

    shelf = None  # the ShelfModel shown
    _hero = False
    _see_all = False
    _followed = ()  # (object, handler id) pairs on the shelf shown
    _animation = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Every signal of a child or of an object the shelf holds is connected weakly
        # (util.py): a bound method would keep a shelf that a page drops alive.
        factory = Gtk.SignalListItemFactory()
        connect_weak(factory, 'setup', self._on_setup)
        connect_weak(factory, 'bind', self._on_bind)
        connect_weak(factory, 'unbind', self._on_unbind)
        self.list_view.set_factory(factory)
        connect_weak(self.list_view, 'activate', self._on_activate)
        connect_weak(self.see_all_button, 'clicked', self._on_see_all_clicked)
        connect_weak(self.previous_button, 'clicked', self._on_page_clicked, -1)
        connect_weak(self.next_button, 'clicked', self._on_page_clicked, 1)
        adjustment = self.scrolled_window.get_hadjustment()
        for signal in ('notify::value', 'notify::upper', 'notify::page-size'):
            connect_weak(adjustment, signal, self._on_adjustment_changed)
        context_menu.attach(self.list_view)

    def _get_hero(self):
        return self._hero

    def _set_hero(self, hero):
        if hero == self._hero:
            return
        self._hero = hero
        # New rows, built with the other kind of tile (none before __init__ has made the
        # factory, when a Python caller passes hero=True).
        factory = self.list_view.get_factory()
        if factory is not None:
            self.list_view.set_factory(None)
            self.list_view.set_factory(factory)

    hero = GObject.Property(type=bool, default=False, getter=_get_hero, setter=_set_hero,
                            nick='Hero', blurb='Whether the tiles are large cards')

    def _get_see_all(self):
        return self._see_all

    def _set_see_all(self, see_all):
        self._see_all = see_all
        self._update_controls()

    see_all = GObject.Property(type=bool, default=False, getter=_get_see_all,
                               setter=_set_see_all, nick='See All',
                               blurb='Whether the shelf offers to open its items as a grid')

    def bind_shelf(self, shelf):
        """Show a shelf: its title, and its first ROW_LIMIT items, which the row follows as
        they change (and the title too, for a ShelfModel)."""
        if shelf is self.shelf:
            return
        self.clear()
        self.shelf = shelf
        followed = [(shelf.items, connect_weak(shelf.items, 'items-changed',
                                               self._on_items_changed))]
        if isinstance(shelf, GObject.Object):
            followed.append((shelf, connect_weak(shelf, 'notify::title', self._on_title)))
        self._followed = followed
        self._show_title()
        self.list_view.set_model(Gtk.NoSelection(
            model=Gtk.SliceListModel(model=shelf.items, offset=0, size=ROW_LIMIT)))
        self.scrolled_window.get_hadjustment().set_value(0)  # a new shelf starts at its start
        self._update_controls()

    def clear(self):
        """Show nothing, and follow nothing (a spare widget a page keeps for later)."""
        for obj, handler in self._followed:
            obj.disconnect(handler)
        self._followed = ()
        self.shelf = None
        self.list_view.set_model(None)

    def _show_title(self):
        title = self.shelf.title if self.shelf is not None else ''
        self.title_label.set_label(title)
        self.title_label.set_visible(bool(title))
        self.list_view.update_property([Gtk.AccessibleProperty.LABEL], [title])

    def _on_title(self, _shelf, _pspec):
        self._show_title()

    def _on_items_changed(self, _items, _position, _removed, _added):
        self._update_controls()

    def _on_adjustment_changed(self, _adjustment, _pspec):
        self._update_controls()

    def _update_controls(self):
        """See All while the row does not show everything (and the shelf offers it); the
        arrows while the row overflows, each sensitive while there is more that way."""
        adjustment = self.scrolled_window.get_hadjustment()
        value, upper = adjustment.get_value(), adjustment.get_upper()
        page = adjustment.get_page_size()
        overflows = page > 0 and upper - page > 1
        truncated = self.shelf is not None and (
            self.shelf.items.get_n_items() > ROW_LIMIT or overflows)
        self.see_all_button.set_visible(self._see_all and truncated)
        self.previous_button.set_visible(overflows)
        self.next_button.set_visible(overflows)
        self.previous_button.set_sensitive(value > 1)
        self.next_button.set_sensitive(value < upper - page - 1)

    def _on_page_clicked(self, _button, direction):
        """Scroll the row one width that way, then focus the first tile wholly shown."""
        adjustment = self.scrolled_window.get_hadjustment()
        page = adjustment.get_page_size()
        target = min(max(adjustment.get_value() + direction * page, adjustment.get_lower()),
                     adjustment.get_upper() - page)
        if self._animation is not None:
            self._animation.skip()
        animation = Adw.TimedAnimation.new(
            self.scrolled_window, adjustment.get_value(), target, PAGE_DURATION,
            Adw.PropertyAnimationTarget.new(adjustment, 'value'))
        animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        connect_weak(animation, 'done', self._on_paged)
        self._animation = animation
        animation.play()

    def _on_paged(self, _animation):
        self._animation = None
        child = self.list_view.get_first_child()
        while child is not None:
            found, bounds = child.compute_bounds(self.scrolled_window)
            if child.get_visible() and found and bounds.get_x() >= -1:
                child.grab_focus()
                return
            child = child.get_next_sibling()

    def _on_setup(self, _factory, list_item):
        list_item.set_child(HeroTile() if self._hero else Tile())

    def _on_bind(self, _factory, list_item):
        item = list_item.get_item()
        tile = list_item.get_child()
        if not self._hero:
            tile.set_artist(item.kind == 'artist')
        tile.bind(item)
        bind_label(list_item, item)

    def _on_unbind(self, _factory, list_item):
        list_item.get_child().unbind()
        unbind_label(list_item)

    def _on_activate(self, list_view, position):
        item = list_view.get_model().get_item(position)
        if item is not None:
            self.get_root().open_item(item)

    def _on_see_all_clicked(self, _button):
        if self.shelf is not None:
            self.get_root().open_shelf(self.shelf)


# The shelves a ShelfColumn binds before the page is drawn: the hero cards and what fits
# under them. The rest are bound one a frame after that, below the fold: a library with 21
# shelves made 317 tiles at once, and Home took 210 ms to open.
FIRST_SHELVES = 3


class ShelfColumn:
    """The shelves of a page, one under another in `box` after `anchor` (a child of the box
    before them, such as the page's title, or None), each an AppleMusicShelf.

    show(shelves, hero_first) shows ShelfModels in that order, the first as hero cards with
    `hero_first`. A shelf shown before keeps its widget, moved into place (nothing rebuilt,
    its row's scroll position kept); the others are bound to spare or new widgets, the first
    FIRST_SHELVES at once and the rest one a frame (next_frame()), placed as they are bound,
    so the column never shows a shelf twice or one that went. Widgets left over are hidden
    and kept for the next show(). `make_shelf(hero)` makes a widget (a Shelf offering See
    All by default).
    """

    def __init__(self, box, anchor=None, make_shelf=None):
        self._box = box
        self._anchor = anchor
        self._make_shelf = make_shelf or (lambda hero: Shelf(hero=hero, see_all=True))
        self._widgets = {}  # a ShelfModel shown -> its widget
        self._spare = []  # widgets hidden, for later
        self._task = None  # the staged binding under way
        self.shelves = []  # what show() was last asked for, in order

    @property
    def widgets(self):
        """The widgets showing shelves, in the column's order."""
        return [self._widgets[shelf] for shelf in self.shelves if shelf in self._widgets]

    def show(self, shelves, hero_first=False):
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        shelves = list(shelves)
        heroes = {shelf: hero_first and position == 0 for position, shelf in enumerate(shelves)}
        for shelf, widget in list(self._widgets.items()):
            if shelf not in heroes or widget.props.hero != heroes[shelf]:
                del self._widgets[shelf]
                self._retire(widget)
        self.shelves = shelves
        pending = [shelf for shelf in shelves if shelf not in self._widgets]
        app = Gio.Application.get_default()
        spawn = getattr(app, 'spawn', None)
        now = pending if spawn is None else pending[:FIRST_SHELVES]
        for shelf in now:
            self._bind(shelf, heroes[shelf])
        self._arrange()
        rest = pending[len(now):]
        if rest:
            self._task = spawn(self._bind_rest(rest, heroes))

    async def _bind_rest(self, shelves, heroes):
        for shelf in shelves:
            await next_frame(self._box)
            self._bind(shelf, heroes[shelf])
            self._arrange()

    def _bind(self, shelf, hero):
        widget = next((spare for spare in self._spare if spare.props.hero == hero), None)
        if widget is None:
            widget = self._make_shelf(hero)
            self._box.append(widget)
        else:
            self._spare.remove(widget)
        widget.bind_shelf(shelf)
        widget.set_visible(True)
        self._widgets[shelf] = widget

    def _retire(self, widget):
        widget.set_visible(False)
        widget.clear()
        self._spare.append(widget)

    def _arrange(self):
        """Put the widgets in the shelves' order, right after the anchor."""
        previous = self._anchor
        for widget in self.widgets:
            if widget.get_prev_sibling() is not previous:
                self._box.reorder_child_after(widget, previous)
            previous = widget
