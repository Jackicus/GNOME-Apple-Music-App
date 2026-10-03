# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicArtistRow: an artist in the Artists page's list, their round portrait beside
their name."""

from gi.repository import Gtk

from . import artwork
from .util import connect_weak

# The portrait's edge in logical pixels.
PORTRAIT_SIZE = 32


class ArtistRow(Gtk.Box):
    """A recycled row of the Artists list: bind(item) and unbind() from the list's factory.

    A Gtk.ListView makes about 200 of these whatever it shows, so a row is two cheap widgets:
    the portrait a Gtk.Image at a fixed pixel size, round through style.css
    (`.artist-portrait`), which never changes size whatever it shows (not an Adw.Avatar, which
    cost the Artists page 40 ms to make by the 200), and the name a Gtk.Inscription, which is
    not measured again when it changes. The picture is the artist's thumbnail (a library
    artist's is their first album's cover) through an artwork.ArtworkSlot, asked for only
    while the row is on screen, the tinted circle alone until it comes. While bound, the row
    follows the Item (a reload's new name or picture: a list does not rebind an Item that
    changed in place). The Item is the row's `context_item`, whose menu the list offers
    (widgets/context_menu.py).
    """

    __gtype_name__ = 'AppleMusicArtistRow'

    def __init__(self, **kwargs):
        super().__init__(spacing=12, margin_top=6, margin_bottom=6, **kwargs)
        self._item = None
        self._handler = None
        self.portrait = Gtk.Image(pixel_size=PORTRAIT_SIZE, overflow=Gtk.Overflow.HIDDEN,
                                  valign=Gtk.Align.CENTER, css_classes=['artist-portrait'],
                                  accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.name = Gtk.Inscription(hexpand=True, xalign=0, valign=Gtk.Align.CENTER,
                                    nat_chars=12,
                                    text_overflow=Gtk.InscriptionOverflow.ELLIPSIZE_END)
        self.append(self.portrait)
        self.append(self.name)
        self._slot = artwork.ArtworkSlot(self._set_art, PORTRAIT_SIZE)
        self._slot.follow_scale(self)

    @property
    def context_item(self):
        """The artist shown, for its context menu."""
        return self._item

    def bind(self, item):
        if self._item is not None:
            self.unbind()
        self._item = item
        self._handler = connect_weak(item, 'notify', self._on_item_notify)
        self.name.set_text(item.title)
        self._slot.set_paths(item.thumb or item.art)

    def unbind(self):
        if self._handler is not None:
            self._item.disconnect(self._handler)
            self._handler = None
        self._item = None
        self._slot.set_paths()

    def _on_item_notify(self, item, pspec):
        if pspec.name == 'title':
            self.name.set_text(item.title)
        elif pspec.name in ('thumb', 'art') and not self._slot.set_paths(item.thumb or item.art):
            self._slot.refresh()

    def do_map(self):
        Gtk.Box.do_map(self)
        self._slot.map(self.get_scale_factor())

    def do_unmap(self):
        self._slot.unmap()
        Gtk.Box.do_unmap(self)

    def _set_art(self, paintable, _found):
        # A texture, or artwork.empty(): the tinted circle alone until the picture comes.
        self.portrait.set_from_paintable(paintable)
