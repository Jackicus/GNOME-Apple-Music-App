"""AppleMusicCategoryTile: a search category (Rock, Chill, the decades…) as a landscape tile."""

from gi.repository import Gio, Graphene, Gtk

from ..remote import fetch_thumb, needs_thumb
from . import artwork


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/category_tile.ui')
class CategoryTile(Gtk.Overlay):
    """The category's name over its colour (`art_color`, Apple's colour for the curator's
    artwork, drawn in do_snapshot as the hero cards' bands are; white or black text by
    artwork.is_dark, the colour made readable by artwork.band_colour) with its picture at the
    right: the curator's square artwork, which shares that colour, fetched into
    <cache>/remote-art/ when the tile is first shown.

    bind(item) takes an Item of kind 'category' in remote.remote_item's shape (`thumb` the
    file the picture is fetched to, `thumbUrl` where from). The landing's tiles are made
    once each (a Gtk.FlowBox over the categories), not recycled, so the picture is fetched
    and decoded while the tile is mapped and let go of when it is unmapped.
    """

    __gtype_name__ = 'AppleMusicCategoryTile'

    picture = Gtk.Template.Child()
    title_label = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.item = None
        self._colour = None
        self._token = None
        self._fetch = None

    def bind(self, item):
        self.item = item
        self.title_label.set_text(item.title)
        colour = artwork.art_colour(item.art_color)
        # Made deeper (or lighter) where the name would not read on it (band_colour).
        self._colour, dark = (artwork.band_colour(colour) if colour is not None
                              else (None, True))
        if dark:
            self.add_css_class('dark-art')
            self.remove_css_class('light-art')
        else:
            self.add_css_class('light-art')
            self.remove_css_class('dark-art')
        self.queue_draw()
        if self.get_mapped():
            self._show_art()

    def do_map(self):
        Gtk.Overlay.do_map(self)
        if self.item is not None:
            self._show_art()

    def do_unmap(self):
        self._release_art()
        Gtk.Overlay.do_unmap(self)

    def _show_art(self):
        loader = artwork.get_default()
        loader.cancel(self._token)
        self._token = None
        item = self.item
        path = item.thumb if item is not None else None
        if not path:
            self.picture.set_paintable(None)
            return
        texture = loader.get(path)
        self.picture.set_paintable(texture)
        if texture is not None:
            return
        self._token = loader.request(path, self._on_texture)
        if needs_thumb(item) and (self._fetch is None or self._fetch.done()):
            self._fetch = Gio.Application.get_default().spawn(self._fetch_then_show(item))

    async def _fetch_then_show(self, item):
        if await fetch_thumb(item) and self.item is item and self.get_mapped():
            self._show_art()

    def _release_art(self):
        artwork.get_default().cancel(self._token)
        self._token = None
        self.picture.set_paintable(None)

    def _on_texture(self, texture):
        self._token = None
        self.picture.set_paintable(texture)

    def do_snapshot(self, snapshot):
        # Under the children, over the CSS background, inside the rounded clip of `overflow`.
        if self._colour is not None:
            bounds = Graphene.Rect().init(0, 0, self.get_width(), self.get_height())
            snapshot.append_color(self._colour, bounds)
        Gtk.Overlay.do_snapshot(self, snapshot)
