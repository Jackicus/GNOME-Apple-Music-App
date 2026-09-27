"""AppleMusicRadioPage: the library's radio stations, the first few as large cards."""

from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from ..library import Item, Shelf as ShelfModel
from ..widgets.shelf import Shelf  # noqa: F401  registers $AppleMusicShelf for the template
from ..widgets.tile import Tile

# How many of the stations the cards at the top show.
HERO_COUNT = 4


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/radio.ui')
class RadioPage(Adw.NavigationPage):
    """library.radio: the stations the user played recently, most recent first (the sync
    asks Apple for /v1/me/recent/radio-stations; each an Item of kind station, no groups).
    The first HERO_COUNT are a shelf of large cards, "Recently Played", and the rest a grid of
    tiles under "More Stations". Their subtitles are the station's provider or curator, and
    mostly "Apple Music Radio", so they are not grouped by it. Activating a station plays it
    (window.open_item → play_request).
    """

    __gtype_name__ = 'AppleMusicRadioPage'

    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    hero_shelf = Gtk.Template.Child()
    more_box = Gtk.Template.Child()
    flow_box = Gtk.Template.Child()

    def __init__(self, library, title, icon_name=None):
        super().__init__(title=title)
        self._library = library
        self._library_handlers = []
        self._stations = []  # the station Items shown, in order
        self._accessible_format = _('{title}, {subtitle}')
        self.title_label.set_label(title)
        self.empty_page.set_icon_name(icon_name)
        self._more = Gio.ListStore(item_type=Item)
        self.flow_box.bind_model(self._more, self._create_tile)
        self._update()

    # The library outlives the window, so the page listens to it only while it is shown.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._library_handlers = [
            self._library.connect('notify::state', self._update),
            self._library.connect('changed', self._update),
        ]
        self._update()

    def do_unmap(self):
        for handler in self._library_handlers:
            self._library.disconnect(handler)
        self._library_handlers = []
        Adw.NavigationPage.do_unmap(self)

    def _update(self, *_args):
        stations = list(self._library.radio)
        if stations != self._stations:
            self._stations = stations
            self.hero_shelf.bind_shelf(
                ShelfModel('radio', _('Recently Played'), stations[:HERO_COUNT]))
            self._more.splice(0, self._more.get_n_items(), stations[HERO_COUNT:])
            self.more_box.set_visible(len(stations) > HERO_COUNT)
        if stations:
            name = 'items'
        elif self._library.state == 'loading':
            name = 'loading'
        else:
            name = 'empty'
        self.stack.set_visible_child_name(name)

    def _create_tile(self, station):
        tile = Tile(halign=Gtk.Align.START)
        tile.bind(station)
        child = Gtk.FlowBoxChild(child=tile)
        label = (self._accessible_format.format(title=station.title, subtitle=station.subtitle)
                 if station.subtitle else station.title)
        child.update_property([Gtk.AccessibleProperty.LABEL], [label])
        return child

    @Gtk.Template.Callback()
    def on_station_activated(self, _flow_box, child):
        station = self._more.get_item(child.get_index())
        if station is not None:
            self.get_root().open_item(station)
