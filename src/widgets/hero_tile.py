# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicHeroTile: a shelf's large card, the cover over a band in the artwork's colour."""

from gi.repository import Gio, Graphene, Gtk

from ..backend import config
from ..remote import fetch_cover
from . import artwork
from .cover import Cover  # noqa: F401  registers $AppleMusicCover for the template
from .util import connect_weak

# The subtitle's opacity on the band (style.css, `.hero-caption > .dimmed`): the band is
# made dark or light enough for it to read (artwork.band_colour).
SUBTITLE_OPACITY = 0.75


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/hero_tile.ui')
class HeroTile(Gtk.Box):
    """A 260 px cover over a two-line caption, the title and the subtitle, on a band of the
    Item's art_color (Apple's colour for the artwork's background) in white or black text,
    whichever reads better on it (artwork.is_dark), the colour made deeper (or lighter) where
    the dimmed subtitle would not read on it at WCAG's AA contrast (artwork.band_colour);
    without one, on the card colour. Apple's "Top Picks for You" cards, for the first shelf
    of Home and Radio's first stations.

    bind(item) and unbind() are called by a list factory, as for AppleMusicTile, and the card
    follows its Item's notify signals while bound, as a tile does. The cover is an
    AppleMusicCover of (art, thumb): the 640 px cover when it is on disk, else the 320 px
    thumbnail the sync fetched. When the card draws more pixels than the thumbnail has (260 px
    at a scale factor of 2) and the Item has the cover's URL, the cover is fetched while the
    card is shown (remote.fetch_cover), and shown once it arrives. The band is drawn here
    rather than by CSS, which cannot take a colour per item; the grid tiles do not pay for
    this Python snapshot.
    """

    __gtype_name__ = 'AppleMusicHeroTile'

    cover = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()

    _colour = None  # the item's colour
    _band = None  # what is drawn: that colour, made readable
    _item = None
    _handler = None  # the bound Item's notify handler

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # A class function, not a bound method: a handler on the card itself holding the card
        # would be a cycle.
        self.connect('notify::scale-factor', HeroTile._on_scale_factor)

    @property
    def context_item(self):
        """The Item shown, for its context menu (widgets/context_menu.py)."""
        return self._item

    def bind(self, item):
        if self._item is not None:
            self.unbind()
        self._item = item
        # The Item outlives the card: it holds the card weakly.
        self._handler = connect_weak(item, 'notify', self._on_item_notify)
        # 260 px at a scale of 2 wants the 640 px art.
        self.cover.set_paths(item.art, item.thumb)
        self.title_label.set_text(item.title)
        self.subtitle_label.set_text(item.subtitle)
        self._set_colour(artwork.art_colour(item.art_color))
        if self.get_mapped():
            self._fetch_cover()

    def do_map(self):
        Gtk.Box.do_map(self)
        if self._item is not None:
            self._fetch_cover()

    def _on_scale_factor(self, _pspec):
        if self.get_mapped() and self._item is not None:
            self._fetch_cover()

    def _fetch_cover(self):
        """Fetch the cover when the thumbnail is too small for the card and the Item says
        where from; whether it is on disk already is asked in the fetch's thread."""
        item = self._item
        raw = item.raw if isinstance(item.raw, dict) else {}
        if (item.art and raw.get('artUrl')
                and self.cover.size * self.get_scale_factor() > config.THUMB_SIZE):
            Gio.Application.get_default().spawn(self._fetch_then_show(item))

    async def _fetch_then_show(self, item):
        if await fetch_cover(item) and self._item is item:
            self.cover.refresh()

    def unbind(self):
        if self._handler is not None:
            self._item.disconnect(self._handler)
            self._handler = None
        self._item = None
        self.cover.set_paths()

    def _on_item_notify(self, item, pspec):
        """Follow the Item (a reload's new values, a fetched thumbnail): a list does not
        rebind an item that changed in place."""
        name = pspec.name
        if name == 'title':
            self.title_label.set_text(item.title)
        elif name == 'subtitle':
            self.subtitle_label.set_text(item.subtitle)
        elif name in ('art', 'thumb'):
            if not self.cover.set_paths(item.art, item.thumb):
                self.cover.refresh()
        elif name == 'art-color':
            self._set_colour(artwork.art_colour(item.art_color))

    def _set_colour(self, colour):
        if colour is None and self._colour is None:
            return
        if colour is not None and self._colour is not None and colour.equal(self._colour):
            return
        self._colour = colour
        self._band, dark = (artwork.band_colour(colour, SUBTITLE_OPACITY) if colour is not None
                            else (None, False))
        # The text colour for the band: white on a dark one, black on a light one.
        if dark:
            self.add_css_class('dark-art')
        else:
            self.remove_css_class('dark-art')
        if colour is not None and not dark:
            self.add_css_class('light-art')
        else:
            self.remove_css_class('light-art')
        self.queue_draw()

    def do_snapshot(self, snapshot):
        # Under the children, over the CSS background, inside the rounded clip of `overflow`.
        if self._band is not None:
            bounds = Graphene.Rect().init(0, 0, self.get_width(), self.get_height())
            snapshot.append_color(self._band, bounds)
        Gtk.Box.do_snapshot(self, snapshot)
