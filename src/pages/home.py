# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicHomePage: the library's shelves, one under another, as on music.apple.com's Home."""

from gi.repository import Adw, Gtk

from ..widgets.shelf import ShelfColumn
from ..widgets.util import HeaderTitle, MappedHandlers


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/home.ui')
class HomePage(Adw.NavigationPage):
    """Every shelf of the library with items in it, in library.json's order (Apple's
    recommendations, then Heavy Rotation, Recently Added…), the first as large cards, each
    offering See All.

    The shelves are a ShelfColumn under the title: a reload keeps each shelf's widget (moved
    into place, its tiles rebound rather than rebuilt), and new ones are bound a frame apart
    after the first few. The loading and empty states follow the library's state, as the grid
    pages' do (the first sync filling an empty library is loading). The header bar shows the
    title while the big one is out of view.
    """

    __gtype_name__ = 'AppleMusicHomePage'

    header_bar = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    shelves_box = Gtk.Template.Child()
    title_label = Gtk.Template.Child()

    def __init__(self, library, title, icon_name=None):
        super().__init__(title=title)
        self._library = library
        self._column = ShelfColumn(self.shelves_box, anchor=self.title_label)
        self.title_label.set_label(title)
        self.empty_page.set_icon_name(icon_name)
        self._header_title = HeaderTitle(self.header_bar, self.title_label, self.scrolled_window)
        # The library outlives the window: the page follows it only while it is shown.
        self._handlers = MappedHandlers(self)
        self._handlers.add(library, 'notify::state', self._update)
        self._handlers.add(library, 'notify::syncing', self._update)
        self._handlers.add(library, 'changed', self._update)
        self._update()

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._update()

    def _update(self, *_args):
        shelves = [shelf for shelf in self._library.shelves if shelf.items.get_n_items()]
        if shelves != self._column.shelves:
            self._column.show(shelves, hero_first=True)
        library = self._library
        if shelves:
            name = 'items'
        elif library.state == 'loading' or (library.state == 'empty' and library.syncing):
            name = 'loading'  # the first sync is on its way
        else:
            name = 'empty'
        self.stack.set_visible_child_name(name)
