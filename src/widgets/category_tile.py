# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicCategoryTile: a search category (Rock, Chill, the decades…) as a landscape tile."""

from gi.repository import Gio, Graphene, Gtk

from ..remote import fetch_thumb, needs_thumb
from . import artwork
from .util import connect_weak

# The picture's edge in logical pixels (category_tile.blp).
ART_SIZE = 84


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/category_tile.ui')
class CategoryTile(Gtk.Overlay):
    """The category's name over its colour (`art_color`, Apple's colour for the curator's
    artwork, drawn in do_snapshot as the hero cards' bands are; white or black text by
    artwork.is_dark, the colour made readable by artwork.band_colour) with its picture at the
    right: the curator's square artwork, which shares that colour, fetched into
    <cache>/remote-art/ when the tile is first shown.

    bind(item) takes an Item of kind 'category' in remote.remote_item's shape (`thumb` the
    file the picture is fetched to, `thumbUrl` where from). The landing's tiles are made
    once each (a Gtk.FlowBox over the categories), not recycled; the picture is fetched while
    the tile is mapped, and decoded at its size and let go of when it is unmapped by an
    artwork.ArtworkSlot.
    """

    __gtype_name__ = 'AppleMusicCategoryTile'

    picture = Gtk.Template.Child()
    title_label = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.item = None
        self._colour = None
        self._fetch = None
        self._slot = artwork.ArtworkSlot(self._set_art, ART_SIZE)
        self._slot.follow_scale(self)

    def bind(self, item):
        self.item = item
        # The Item outlives the tile (the landing's store): it holds the tile weakly. A tile
        # is bound once, and goes with its flow box child.
        connect_weak(item, 'notify', self._on_item_notify)
        self.title_label.set_text(item.title)
        self._show_colour(item)
        self._slot.set_paths(item.thumb)
        if self.get_mapped():
            self._fetch_art()

    def _show_colour(self, item):
        colour = artwork.art_colour(item.art_color)
        if colour is None:
            # No colour of its own: style.css's tint of the text colour, and the theme's text
            # (white text would vanish on the light theme's page).
            self._colour = None
            self.remove_css_class('dark-art')
            self.remove_css_class('light-art')
        else:
            # Made deeper (or lighter) where the name would not read on it (band_colour).
            self._colour, dark = artwork.band_colour(colour)
            self.add_css_class('dark-art' if dark else 'light-art')
            self.remove_css_class('light-art' if dark else 'dark-art')
        self.queue_draw()

    def _on_item_notify(self, item, pspec):
        name = pspec.name
        if name == 'title':
            self.title_label.set_text(item.title)
        elif name == 'art-color':
            self._show_colour(item)
        elif name == 'thumb' and not self._slot.set_paths(item.thumb):
            self._slot.refresh()

    def do_map(self):
        Gtk.Overlay.do_map(self)
        self._slot.map(self.get_scale_factor())
        if self.item is not None:
            self._fetch_art()

    def do_unmap(self):
        self._slot.unmap()
        Gtk.Overlay.do_unmap(self)

    def _fetch_art(self):
        """The picture, when it is not on disk yet (asked in the fetch's thread)."""
        if needs_thumb(self.item) and (self._fetch is None or self._fetch.done()):
            self._fetch = Gio.Application.get_default().spawn(self._fetch_then_show(self.item))

    async def _fetch_then_show(self, item):
        if await fetch_thumb(item) and self.item is item:
            self._slot.refresh()

    def _set_art(self, paintable, _found):
        # Never None: an empty paintable its size (artwork.empty()) until the picture comes.
        self.picture.set_paintable(paintable)

    def do_snapshot(self, snapshot):
        # Under the children, over the CSS background, inside the rounded clip of `overflow`.
        if self._colour is not None:
            bounds = Graphene.Rect().init(0, 0, self.get_width(), self.get_height())
            snapshot.append_color(self._colour, bounds)
        Gtk.Overlay.do_snapshot(self, snapshot)
