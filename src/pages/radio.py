# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicRadioPage: the library's radio stations, the first few as large cards."""

from gettext import gettext as _
from types import SimpleNamespace

from gi.repository import Adw, GLib, Gtk

from ..widgets import context_menu
from ..widgets.labels import accessible_label, flow_child
from ..widgets.shelf import Shelf  # noqa: F401  registers $AppleMusicShelf for the template
from ..widgets.tile import Tile
from ..widgets.util import HeaderTitle, MappedHandlers, connect_weak, weak_method

# How many of the stations the cards at the top show.
HERO_COUNT = 4


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/radio.ui')
class RadioPage(Adw.NavigationPage):
    """library.radio: the stations the user played recently, most recent first (the sync
    asks Apple for /v1/me/recent/radio-stations; each an Item of kind station, no groups).
    The first HERO_COUNT are a shelf of large cards, "Recently Played", and the rest a grid of
    tiles under "More Stations". Their subtitles are the station's provider or curator, and
    mostly "Apple Music Radio", so they are not grouped by it. Activating a station plays it
    (window.open_item → play_request).

    Both are slices of the library's own store, bound once: a reload's changes (stations
    that came, went or moved, a renamed one) reach them as they happen, and the cards keep
    their place. The header bar shows the title while the big one is out of view.
    """

    __gtype_name__ = 'AppleMusicRadioPage'

    header_bar = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    hero_shelf = Gtk.Template.Child()
    more_box = Gtk.Template.Child()
    flow_box = Gtk.Template.Child()

    def __init__(self, library, title, icon_name=None):
        super().__init__(title=title)
        self._library = library
        self.title_label.set_label(title)
        self.empty_page.set_icon_name(icon_name)
        self.hero_shelf.bind_shelf(SimpleNamespace(
            key='radio', title=_('Recently Played'),
            items=Gtk.SliceListModel(model=library.radio, offset=0, size=HERO_COUNT)))
        # The rest: offset and size must not add up past G_MAXUINT, or the slice's end wraps
        # round and it ignores what comes after the cards.
        self._more = Gtk.SliceListModel(model=library.radio, offset=HERO_COUNT,
                                        size=GLib.MAXUINT32 - HERO_COUNT)
        # Shown while there are more stations than cards.
        connect_weak(self._more, 'items-changed', self._on_more_changed)
        self.more_box.set_visible(self._more.get_n_items() > 0)
        self.flow_box.bind_model(self._more, weak_method(self._create_tile))
        context_menu.attach(self.flow_box)
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

    def _on_more_changed(self, more, _position, _removed, _added):
        self.more_box.set_visible(more.get_n_items() > 0)

    def _update(self, *_args):
        library = self._library
        if library.radio.get_n_items():
            name = 'items'
        elif library.state == 'loading' or (library.state == 'empty' and library.syncing):
            name = 'loading'  # the first sync is on its way
        else:
            name = 'empty'
        self.stack.set_visible_child_name(name)

    def _create_tile(self, station):
        tile = Tile(halign=Gtk.Align.START)
        tile.bind(station)
        return flow_child(tile, accessible_label(station))

    @Gtk.Template.Callback()
    def on_station_activated(self, _flow_box, child):
        station = self._more.get_item(child.get_index())
        if station is not None:
            self.get_root().open_item(station)
