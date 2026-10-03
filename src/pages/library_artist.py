# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicLibraryArtistPage: what the library holds of an artist, as Apple Music's library
shows an artist: the name over the albums, laid out the GNOME way.

One implementation for two places: the Artists page's detail pane (pages/artists.py, which
shows the artist selected in its list with set_artist()) and a page of its own, pushed over the
page shown (window.open_item for an artist of the library's: a tile, a link, the artist page's
In Your Library See All).
"""

from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from .. import discography
from ..library import Item, apply_diff
from ..widgets import context_menu
from ..widgets.labels import bind_label, unbind_label
from ..widgets.tile import Tile
from ..widgets.util import MappedHandlers, connect_weak, weak_method
from . import app
from .grid import SORTS, ReversedModel, columns_for, estimate_columns, make_sorter

# The orders the Sort By menu offers: newest first, as Apple Music's library has an artist's
# albums, or by title.
ORDERS = ('year', 'title')
# The key the order chosen is remembered by, in the grid-sort setting (the grid pages' are
# their sidebar keys).
SORT_KEY = 'library-artist'


def _direction(descending):
    return 'descending' if descending else 'ascending'


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/library_artist.ui')
class LibraryArtistPage(Adw.NavigationPage):
    """An artist of the library's: a heading (the name, a link to Apple Music's page of the
    artist as Go to Artist opens it; Play and Shuffle, the artist's songs in the library as one
    queue, in the order shown; the artist's own menu) that stays put over a grid of the
    library's albums of theirs, each a tile with its year (discography.albums(): a song added
    without its album shows as its album, as Apple Music has it). Sort By orders them by year
    (newest first) or title, either way round; the order is remembered (grid-sort's
    SORT_KEY), the direction for the page's life.

    `artist` is the library's artist Item, or None for nothing yet (the Artists page's pane
    before its list has an artist). The page follows the artist and the library while it is
    mapped, and catches up when it maps again. It has no `item`: the window and the menus take
    a page's `item` to be the page Go to Artist opens (related.same_page, shows), and this one
    is not that page.
    """

    __gtype_name__ = 'AppleMusicLibraryArtistPage'

    header_bar = Gtk.Template.Child()
    sort_button = Gtk.Template.Child()
    heading_box = Gtk.Template.Child()
    name_button = Gtk.Template.Child()
    name_label = Gtk.Template.Child()
    buttons_box = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    shuffle_button = Gtk.Template.Child()
    more_button = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    grid_view = Gtk.Template.Child()

    def __init__(self, library, artist=None, **kwargs):
        super().__init__(**kwargs)
        self._library = library
        self.artist = None
        self._focused = False  # the page has moved the focus from the link to Play once
        self._made = {}  # the albums made up for groups the library has no album for
        self._store = Gio.ListStore(item_type=Item)

        order = (ORDERS[0], SORTS[ORDERS[0]][2])
        settings = getattr(app(), 'settings', None)
        if settings is not None:
            key = settings.get_value('grid-sort').unpack().get(SORT_KEY)
            if key in ORDERS:
                order = (key, SORTS[key][2])
        self._order = order
        sorter, backwards = make_sorter(*order)
        self._sorted = Gtk.SortListModel(model=self._store, sorter=sorter)
        self._shown = ReversedModel(self._sorted)
        self._shown.set_backwards(backwards)
        connect_weak(self._sorted, 'items-changed', self._update_state)

        # Every signal of a child or of an object the page holds is connected weakly
        # (widgets/util.py): a bound method would keep a pushed page alive once popped.
        factory = Gtk.SignalListItemFactory()
        connect_weak(factory, 'setup', self._on_setup)
        connect_weak(factory, 'bind', self._on_bind)
        connect_weak(factory, 'unbind', self._on_unbind)
        self.grid_view.set_factory(factory)
        connect_weak(self.grid_view, 'activate', self._on_activate)
        # The columns that fit, from the width the grid is given (its horizontal adjustment's
        # page): Gtk.GridView keeps tiles for max-columns columns by 30 rows (grid.py).
        self._columns_idle = None
        self._set_columns(estimate_columns())
        connect_weak(self.grid_view.get_hadjustment(), 'changed', self._on_width)
        self.grid_view.set_model(Gtk.NoSelection(model=self._shown))
        context_menu.attach(self.grid_view)

        connect_weak(self.name_button, 'clicked', self._on_name_clicked)
        connect_weak(self.play_button, 'clicked', self._on_play_clicked, False)
        connect_weak(self.shuffle_button, 'clicked', self._on_play_clicked, True)
        self.more_button.set_create_popup_func(weak_method(self._on_more_popup))

        # Sort By: radio items of the stateful page.sort and page.sort-order actions.
        self._sort_action = Gio.SimpleAction.new_stateful(
            'sort', GLib.VariantType.new('s'), GLib.Variant('s', order[0]))
        self._order_action = Gio.SimpleAction.new_stateful(
            'sort-order', GLib.VariantType.new('s'), GLib.Variant('s', _direction(order[1])))
        connect_weak(self._sort_action, 'change-state', self._on_sort_chosen)
        connect_weak(self._order_action, 'change-state', self._on_order_chosen)
        actions = Gio.SimpleActionGroup()
        actions.add_action(self._sort_action)
        actions.add_action(self._order_action)
        self.insert_action_group('page', actions)
        menu = Gio.Menu()
        keys = Gio.Menu()
        for key in ORDERS:
            keys.append(SORTS[key][0](), f'page.sort::{key}')
        menu.append_section(None, keys)
        directions = Gio.Menu()
        directions.append(_('Ascending'), 'page.sort-order::ascending')
        directions.append(_('Descending'), 'page.sort-order::descending')
        menu.append_section(None, directions)
        self.sort_button.set_menu_model(menu)

        # The library and the artist outlive the page: followed while it is mapped.
        self._handlers = MappedHandlers(self)
        self._handlers.add(library, 'changed', self._on_changed)
        self.set_artist(artist)

    # The artist shown.

    def set_artist(self, artist):
        """Show `artist` (a library artist Item, or None): the heading, and the albums,
        brought to the new artist's from the top."""
        if artist is self.artist:
            return
        if self.artist is not None:
            self._handlers.remove(self.artist)
        self.artist = artist
        self._focused = False
        if artist is not None:
            self._handlers.add(artist, 'groups-changed', self._on_changed)
            self._handlers.add(artist, 'notify::title', self._on_changed)
        self._made = {}
        self._show(replace=True)

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._show()  # what a reload changed while the page was hidden

    def do_shown(self):
        # Shown (pushed, or the Artists page's pane shown), the focus comes to the page's
        # first button, the name's link: Play is the heading's, as on an album's page. Once
        # an artist: the focus put back on the link after a page over this one is popped
        # stays there.
        if not self._focused:
            self._focused = True
            root = self.get_root()
            focus = root.get_focus() if root is not None else None
            if focus is not None and (focus is self.name_button
                                      or focus.is_ancestor(self.name_button)):
                self.focus_content()
        Adw.NavigationPage.do_shown(self)

    def focus_content(self):
        """The focus on Play, or the heading's first button while there is nothing to play."""
        if not (self.play_button.get_sensitive() and self.play_button.grab_focus()):
            self.heading_box.child_focus(Gtk.DirectionType.TAB_FORWARD)

    def _on_changed(self, *_args):
        self._show()

    def _show(self, replace=False):
        artist = self.artist
        title = artist.title if artist is not None else ''
        self.set_title(title)
        self.name_label.set_label(title)  # the link's name; its tooltip, its description
        albums = discography.albums(self._library, artist, self._made)
        if replace:
            # A new artist: the old albums out, then the new ones in. Not the other way round,
            # which rebinds the tiles rather than making new ones (performance.md) but leaves
            # the grid's keyboard focus on whichever new album took an old one's place: Tab
            # into the grid comes to the first album. An artist has a handful.
            self._store.remove_all()
            self._store.splice(0, 0, albums)
        else:
            apply_diff(self._store, albums)
        self._update_state()

    def _update_state(self, *_args):
        some = self._sorted.get_n_items() > 0
        self.stack.set_visible_child_name('items' if some else 'empty')
        self.heading_box.set_visible(self.artist is not None)
        self.play_button.set_sensitive(some)
        self.shuffle_button.set_sensitive(some)
        self.sort_button.set_visible(self._sorted.get_n_items() > 1)

    def shown_albums(self):
        """The albums in the order shown."""
        return [self._shown.get_item(position) for position in range(self._shown.get_n_items())]

    # The heading's buttons.

    def _on_name_clicked(self, _button):
        if self.artist is not None:
            self.get_root().open_artist_page(self.artist)

    def _on_play_clicked(self, _button, shuffle):
        play = discography.play_target(self.shown_albums())
        if play is not None:
            self.get_root().play_request(play, shuffle=shuffle)

    def _on_more_popup(self, button):
        """The artist's menu, made as it opens."""
        actions = getattr(self.get_root(), 'item_actions', None)
        button.set_menu_model(actions.menu_for(self.artist)
                              if actions is not None and self.artist is not None else None)

    # Sorting.

    def _on_sort_chosen(self, _action, value):
        key = value.get_string()
        self._sort(key, SORTS[key][2])
        settings = getattr(app(), 'settings', None)
        if settings is not None:
            sorts = dict(settings.get_value('grid-sort').unpack())
            sorts[SORT_KEY] = key
            settings.set_value('grid-sort', GLib.Variant('a{ss}', sorts))

    def _on_order_chosen(self, _action, value):
        self._sort(self._order[0], value.get_string() == 'descending')

    def _sort(self, key, descending):
        if key not in ORDERS:
            return
        if (key, descending) != self._order:
            self._order = (key, descending)
            self._sort_action.set_state(GLib.Variant('s', key))
            self._order_action.set_state(GLib.Variant('s', _direction(descending)))
            sorter, backwards = make_sorter(key, descending)
            self._shown.set_backwards(backwards)
            self._sorted.set_sorter(sorter)
        if self._sorted.get_n_items():
            self.grid_view.scroll_to(0, Gtk.ListScrollFlags.NONE, None)

    # The grid.

    def _set_columns(self, columns):
        self._columns_idle = None
        if columns is not None and columns != self.grid_view.get_max_columns():
            self.grid_view.set_max_columns(columns)
        return GLib.SOURCE_REMOVE

    def _on_width(self, adjustment):
        """The grid's width changed: its max-columns follows, from an idle (the grid is
        being laid out)."""
        width = adjustment.get_page_size()
        if width > 0 and self._columns_idle is None:
            columns = columns_for(int(width))
            if columns != self.grid_view.get_max_columns():
                self._columns_idle = GLib.idle_add(self._set_columns, columns,
                                                   priority=GLib.PRIORITY_HIGH_IDLE)

    def _on_setup(self, _factory, list_item):
        list_item.set_child(Tile(year=True))

    def _on_bind(self, _factory, list_item):
        item = list_item.get_item()
        list_item.get_child().bind(item)
        bind_label(list_item, item)

    def _on_unbind(self, _factory, list_item):
        list_item.get_child().unbind()
        unbind_label(list_item)

    def _on_activate(self, _grid_view, position):
        item = self._shown.get_item(position)
        if item is not None:
            self.get_root().open_item(item)
