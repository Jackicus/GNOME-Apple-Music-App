"""AppleMusicShelf: a titled, horizontally scrolling row of tiles."""

from gettext import gettext as _

from gi.repository import GObject, Gtk

from .hero_tile import HeroTile
from .tile import Tile


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/shelf.ui')
class Shelf(Gtk.Box):
    """A shelf of Items (a library.Shelf, bound with bind_shelf()) as a row of tiles under its
    title, an optional subtitle and an optional "See All" button (`see-all`), which opens the
    shelf as a grid (window.open_shelf). Activating a tile opens its item (window.open_item).

    The row is a horizontal Gtk.ListView, the scrolled window's own child, so its tiles are
    recycled as it scrolls, whatever the page around it does. With `hero`, the tiles are
    260 px AppleMusicHeroTiles, the cover over a band in its colour; otherwise 160 px
    AppleMusicTiles, round for artists.

    Scrolling: the row's scrolled window scrolls only horizontally, and GtkScrolledWindow
    handles a scroll event only along an axis it can scroll, so a vertical wheel over the row
    goes on to the page's scrolled window, and a horizontal one (a touchpad, a tilting wheel,
    Shift and the wheel) moves the row. A row that fits scrolls neither way.

    It may be built by GtkBuilder, which does not call __init__, so its state starts as class
    attributes.
    """

    __gtype_name__ = 'AppleMusicShelf'

    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()
    see_all_button = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    list_view = Gtk.Template.Child()

    shelf = None  # the library.Shelf shown
    _hero = False
    _accessible_format = None

    def _get_hero(self):
        return self._hero

    def _set_hero(self, hero):
        if hero == self._hero:
            return
        self._hero = hero
        if hero:
            self.list_view.add_css_class('hero')
        else:
            self.list_view.remove_css_class('hero')
        # New rows, built with the other kind of tile.
        factory = self.list_view.get_factory()
        self.list_view.set_factory(None)
        self.list_view.set_factory(factory)

    hero = GObject.Property(type=bool, default=False, getter=_get_hero, setter=_set_hero,
                            nick='Hero', blurb='Whether the tiles are large cards')

    def _get_see_all(self):
        return self.see_all_button.get_visible()

    def _set_see_all(self, see_all):
        self.see_all_button.set_visible(see_all)

    see_all = GObject.Property(type=bool, default=False, getter=_get_see_all,
                               setter=_set_see_all, nick='See All',
                               blurb='Whether the shelf offers to open its items as a grid')

    def bind_shelf(self, shelf, subtitle=None):
        """Show a library.Shelf: its title, and its items, which the row follows as they change."""
        if shelf is self.shelf:
            return
        self.shelf = shelf
        self.title_label.set_label(shelf.title)
        self.title_label.set_visible(bool(shelf.title))
        self.subtitle_label.set_label(subtitle or '')
        self.subtitle_label.set_visible(bool(subtitle))
        self.list_view.update_property([Gtk.AccessibleProperty.LABEL], [shelf.title])
        self.list_view.set_model(Gtk.NoSelection(model=shelf.items))
        self.scrolled_window.get_hadjustment().set_value(0)  # a new shelf starts at its start

    @Gtk.Template.Callback()
    def on_setup(self, _factory, list_item):
        list_item.set_child(HeroTile() if self._hero else Tile())

    @Gtk.Template.Callback()
    def on_bind(self, _factory, list_item):
        item = list_item.get_item()
        tile = list_item.get_child()
        if not self._hero:
            tile.set_artist(item.kind == 'artist')
        tile.bind(item)
        if item.kind == 'artist' or not item.subtitle:
            label = item.title
        else:
            if Shelf._accessible_format is None:  # looked up once: gettext is slow when hot
                Shelf._accessible_format = _('{title}, {subtitle}')
            label = Shelf._accessible_format.format(title=item.title, subtitle=item.subtitle)
        list_item.set_accessible_label(label)

    @Gtk.Template.Callback()
    def on_unbind(self, _factory, list_item):
        list_item.get_child().unbind()

    @Gtk.Template.Callback()
    def on_activate(self, list_view, position):
        item = list_view.get_model().get_item(position)
        if item is not None:
            self.get_root().open_item(item)

    @Gtk.Template.Callback()
    def on_see_all_clicked(self, _button):
        if self.shelf is not None:
            self.get_root().open_shelf(self.shelf)
