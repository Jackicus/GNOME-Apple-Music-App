# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicArtistsPage: the Artists destination, as Apple Music's library has it: the
library's artists in a list beside what the library holds of the one chosen."""

from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from ..widgets import context_menu
from ..widgets.artist_row import ArtistRow
from ..widgets.labels import bind_label, unbind_label
from ..widgets.util import MappedHandlers, connect_weak, first_descendant, next_frame
from . import SignInOffer, app, mark_bound
from .grid import make_sorter
from .library_artist import LibraryArtistPage

# The artists the list is given before the page's first frame (a tall window's worth of
# rows), then MORE_ROWS a frame until it has ALL_ROWS (a Gtk.ListView makes no more rows
# past about 200), then the rest at once.
FIRST_ROWS = 24
MORE_ROWS = 44
ALL_ROWS = 200


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/artists.ui')
class ArtistsPage(Adw.NavigationPage):
    """The Artists root page: an Adw.NavigationSplitView of the library's artists, A to Z, as
    a list (a Gtk.ListView of recycled ArtistRows, with the artists' context menu) beside a
    LibraryArtistPage of the artist selected, as GNOME's apps lay out a list and its detail.
    The first artist is selected to begin with, and the selection is kept while the page
    lives (the window keeps a destination's page for good) and through a reload (the store
    keeps its Items). The list has its first FIRST_ROWS artists for the page's first frame
    and the rest over the next few (_grow()).

    Narrower than two columns of albums beside the list (600sp), the split view collapses:
    the list alone, and a click or Enter on an artist shows their albums, with a back button
    to the list (Escape, Alt+Left: can_go_back() and go_back(), which the window's Back
    asks first, and `panes-changed` when the answer may have changed). The loading and empty
    states follow the library as the grid pages' do, Sign In… while signed out.

    The window's seams for a page whose content is split: focus_content() (Ctrl+2: the list,
    or the albums when they are the pane shown) and banner_host() (the toolbar view under
    whose header bar the window's banners go: the albums', or the list's while it is the
    pane shown).
    """

    __gtype_name__ = 'AppleMusicArtistsPage'

    __gsignals__ = {
        'panes-changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    split_view = Gtk.Template.Child()
    list_page = Gtk.Template.Child()
    list_toolbar = Gtk.Template.Child()
    list_view = Gtk.Template.Child()

    def __init__(self, library, title, icon_name=None):
        super().__init__(title=title)
        self._library = library
        self._bound = False  # a row has been bound (the startup timing's mark)
        self.list_page.set_title(title)
        if icon_name:
            self.empty_page.set_icon_name(icon_name)
        self._sign_in = SignInOffer(self.empty_page)

        self.detail = LibraryArtistPage(library, tag='detail')
        self.split_view.set_content(self.detail)

        sorter, _backwards = make_sorter('title')
        self._sorted = Gtk.SortListModel(model=library.artists, sorter=sorter)
        # The list's first rows at once, the rest a few frames apart (_grow()).
        self._slice = Gtk.SliceListModel(model=self._sorted, offset=0, size=FIRST_ROWS)
        self._growing = None  # the task giving the list the rest, once the page has mapped
        self._selection = Gtk.SingleSelection(model=self._slice, autoselect=True)
        connect_weak(self._selection, 'notify::selected-item', self._on_selected)
        connect_weak(self._sorted, 'items-changed', self._update_state)

        factory = Gtk.SignalListItemFactory()
        connect_weak(factory, 'setup', self._on_setup)
        connect_weak(factory, 'bind', self._on_bind)
        connect_weak(factory, 'unbind', self._on_unbind)
        self.list_view.set_factory(factory)
        self.list_view.set_model(self._selection)
        context_menu.attach(self.list_view)
        connect_weak(self.list_view, 'activate', self._on_activate)
        # A click shows the albums in the collapsed layout, as a tap on a row of GNOME's lists
        # does (the click has selected the row by then). Not single-click-activate, which
        # selects on hover.
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        connect_weak(click, 'released', self._on_clicked)
        self.list_view.add_controller(click)

        for name in ('collapsed', 'show-content'):
            connect_weak(self.split_view, f'notify::{name}', self._on_panes)
        connect_weak(self.stack, 'notify::visible-child', self._on_panes)

        # The library outlives the page: followed while it is shown.
        self._handlers = MappedHandlers(self)
        self._handlers.add(library, 'notify::state', self._update_state)
        self._handlers.add(library, 'notify::syncing', self._update_state)
        self._update_state()
        self._on_selected()

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._update_state()
        if self._growing is None:
            application = app()
            if application is not None:
                self._growing = application.spawn(self._grow())
            else:
                self._growing = True
                self._slice.set_size(GLib.MAXUINT32)

    async def _grow(self):
        """Give the list the rest of the artists, MORE_ROWS a frame: a Gtk.ListView makes a
        row for each item up to about 200 whatever it shows, and 200 rows at once made the
        page's first frame 35 ms longer. Past ALL_ROWS it makes no more rows, so the rest
        comes at once ("the rest": MAXUINT32, gtk-notes.md)."""
        size = FIRST_ROWS
        while size < min(ALL_ROWS, self._sorted.get_n_items()):
            await next_frame(self.list_view)
            size += MORE_ROWS
            self._slice.set_size(size)
        self._slice.set_size(GLib.MAXUINT32)

    def _update_state(self, *_args):
        library = self._library
        if self._sorted.get_n_items():
            name = 'artists'
        elif library.state == 'loading' or (library.state == 'empty' and library.syncing):
            name = 'loading'  # the first sync is on its way
        else:
            name = 'empty'
        if self.stack.get_visible_child_name() != name:
            self.stack.set_visible_child_name(name)

    # The list.

    def _on_selected(self, *_args):
        self.detail.set_artist(self._selection.get_selected_item())

    def _on_setup(self, _factory, list_item):
        list_item.set_child(ArtistRow())

    def _on_bind(self, _factory, list_item):
        item = list_item.get_item()
        list_item.get_child().bind(item)
        bind_label(list_item, item, True)
        if not self._bound:
            self._bound = True
            mark_bound(self)

    def _on_unbind(self, _factory, list_item):
        list_item.get_child().unbind()
        unbind_label(list_item)

    def _on_activate(self, _list_view, position):
        """Enter on an artist (or a double click): their albums shown, and the focus in them,
        whichever the layout."""
        self._selection.set_selected(position)
        self._show_detail(focus=True)

    def _on_clicked(self, _gesture, _n_press, _x, _y):
        if self.split_view.get_collapsed() and self._selection.get_selected_item() is not None:
            self._show_detail()

    def _show_detail(self, focus=False):
        if self.split_view.get_collapsed():
            self.split_view.set_show_content(True)
        if focus:
            GLib.idle_add(self._focus_detail)

    def _focus_detail(self):
        self.detail.focus_content()
        return GLib.SOURCE_REMOVE

    # The window's seams.

    def _on_panes(self, *_args):
        self.emit('panes-changed')

    def _detail_shown(self):
        return (self.stack.get_visible_child_name() == 'artists'
                and self.split_view.get_collapsed() and self.split_view.get_show_content())

    def can_go_back(self):
        """Whether Back goes from the albums to the list (the collapsed layout, the albums
        shown)."""
        return self._detail_shown()

    def go_back(self):
        """Back to the list from the albums: True when there was somewhere to go."""
        if not self._detail_shown():
            return False
        self.split_view.set_show_content(False)
        return True

    def focus_content(self):
        """Ctrl+2: the focus on the albums when they are the pane shown, else on the list's
        selected artist (on the page's own content while it shows no list). Nothing moves
        when the focus is in the list or the albums already; from a header bar it moves in."""
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        detail = first_descendant(self.detail, Adw.ToolbarView).get_content()
        if focus is not None and any(focus is widget or focus.is_ancestor(widget)
                                     for widget in (self.list_view, detail)):
            return
        if self.stack.get_visible_child_name() != 'artists':
            self.stack.get_visible_child().child_focus(Gtk.DirectionType.TAB_FORWARD)
        elif self._detail_shown():
            self._focus_detail()
        else:
            position = self._selection.get_selected()
            if position != Gtk.INVALID_LIST_POSITION:
                self.list_view.scroll_to(position, Gtk.ListScrollFlags.FOCUS, None)
            else:
                self.list_view.child_focus(Gtk.DirectionType.TAB_FORWARD)

    def banner_host(self):
        """The Adw.ToolbarView the window's banners go in: the albums' (or the list's, while
        the list is the pane shown); the state's own while there is no list."""
        if self.stack.get_visible_child_name() != 'artists':
            return first_descendant(self.stack.get_visible_child(), Adw.ToolbarView)
        if self.split_view.get_collapsed() and not self.split_view.get_show_content():
            return self.list_toolbar
        return first_descendant(self.detail, Adw.ToolbarView)
