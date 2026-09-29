# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicTile: an Item as a grid tile, its artwork, title and subtitle."""

from gi.repository import Adw, GLib, Gtk

from . import artwork
from .util import connect_weak

# The subtitle's look inside the tile's markup: about the caption size, at libadwaita's
# .dimmed opacity (its --dim-opacity: 55%, and 90% with the system's high-contrast setting,
# which markup cannot follow by itself; subtitle_markup() does, for tiles bound after a change).
SUBTITLE = '<span size="smaller" alpha="{alpha}%">{{}}</span>'
DIM_OPACITY = 55
DIM_OPACITY_HIGH_CONTRAST = 90
_subtitle = None


def subtitle_markup():
    """The subtitle's markup format for the contrast setting now."""
    global _subtitle
    if _subtitle is None:
        manager = Adw.StyleManager.get_default()
        manager.connect('notify::high-contrast', _on_high_contrast)
        _on_high_contrast(manager)
    return _subtitle


def _on_high_contrast(manager, *_args):
    global _subtitle
    alpha = DIM_OPACITY_HIGH_CONTRAST if manager.get_high_contrast() else DIM_OPACITY
    _subtitle = SUBTITLE.format(alpha=alpha)

# The placeholder's icon and size: a music note until the cover arrives, and a playlist
# folder's big folder icon, which no cover replaces.
PLACEHOLDER = ('music-note-symbolic', 48)
FOLDER = ('folder-symbolic', 72)

# The cover's edge in logical pixels (tile.blp): the texture asked for is this times the
# scale factor.
ART_SIZE = 160


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/tile.ui')
class Tile(Gtk.Box):
    """A 160 px square cover over a title and a dim subtitle; with artist=True, a round portrait
    over a centred name, on two lines when it needs them.

    A playlist folder (an Item of kind 'folder') has no cover: its tile shows a folder icon.
    bind(item) and unbind() are called by a list factory as tiles are recycled; the Item bound
    is the tile's `context_item`, whose menu the view shows (widgets/context_menu.py). While
    bound, the tile follows the Item's notify signals: a list does not rebind an item that
    changed in place (a reload's new title or thumbnail, a fetched thumbnail). The cover is
    asked for by an artwork.ArtworkSlot: only while the tile is mapped, which in a Gtk.GridView
    means on screen, and let go of when it is unmapped, so the textures alive are about the
    ones on screen plus the Artwork cache; in an idle after the frame, not as the tile is
    mapped or bound (a grid creates and maps over a hundred tiles at once and unmaps all but
    the visible dozen in its first layout, and a tile scrolling past is rebound many times a
    second); and decoded at the size drawn (ART_SIZE times the scale factor).
    """

    __gtype_name__ = 'AppleMusicTile'

    cover = Gtk.Template.Child()
    placeholder_icon = Gtk.Template.Child()
    picture = Gtk.Template.Child()
    label = Gtk.Template.Child()

    def __init__(self, artist=False, **kwargs):
        super().__init__(**kwargs)
        self._artist = False
        self._folder = False
        self._marked = False  # the label holds markup's attributes (a subtitle's)
        self._item = None
        self._handler = None  # the bound Item's notify handler
        self.avatar = None  # the artist variant's portrait, made when first wanted
        self._slot = artwork.ArtworkSlot(self._set_art, ART_SIZE)
        self._slot.follow_scale(self)
        self.set_artist(artist)

    def set_artist(self, artist):
        """Switch between the square cover and the artist's round portrait. A grid page's tiles
        are one or the other; a shelf, which can mix artists with albums, switches a tile as it
        binds it (before bind())."""
        if artist == self._artist:
            return
        self._slot.set_paths()  # the one showing lets go of its texture
        self._artist = artist
        if artist and self.avatar is None:
            # A silhouette until the picture arrives. Not initials: they are a label that
            # would be laid out again at every rebind. Made only for artist tiles: an
            # Adw.Avatar costs 18 KB and a third of a tile's making, and most tiles are
            # covers.
            self.avatar = Adw.Avatar(size=ART_SIZE,
                                     accessible_role=Gtk.AccessibleRole.PRESENTATION)
            self.insert_child_after(self.avatar, self.cover)
        self.cover.set_visible(not artist)
        if self.avatar is not None:
            self.avatar.set_visible(artist)
        self.label.set_xalign(0.5 if artist else 0)
        # A name wraps to a second line, as an album's title does; no subtitle follows it.
        self.label.set_min_lines(2 if artist else 3)

    @property
    def context_item(self):
        """The Item shown, for its context menu (widgets/context_menu.py)."""
        return self._item

    def bind(self, item):
        if self._item is not None:
            self.unbind()
        self._item = item
        # The Item outlives the tile (the library's, a shelf's): it holds the tile weakly.
        self._handler = connect_weak(item, 'notify', self._on_item_notify)
        folder = item.kind == 'folder'
        if folder != self._folder:  # only then: setting an icon costs a relayout
            self._folder = folder
            icon_name, size = FOLDER if folder else PLACEHOLDER
            self.placeholder_icon.set_from_icon_name(icon_name)
            self.placeholder_icon.set_pixel_size(size)
        self._show_label(item)
        self._slot.set_paths(item.thumb or item.art)

    def _show_label(self, item):
        if self._artist or not item.subtitle:
            if self._marked:
                # set_text() keeps the attributes set_markup() gave the subtitle's bytes: the
                # new title would be drawn small and dim from where the old one ended.
                self.label.set_attributes(None)
                self._marked = False
            self.label.set_text(item.title)
        else:
            self.label.set_markup(GLib.markup_escape_text(item.title) + '\n'
                                  + subtitle_markup().format(
                                      GLib.markup_escape_text(item.subtitle)))
            self._marked = True

    def unbind(self):
        if self._handler is not None:
            self._item.disconnect(self._handler)
            self._handler = None
        self._item = None
        self._slot.set_paths()

    def _on_item_notify(self, item, pspec):
        """Follow the Item: a reload merges new values into it, and a fetch says a thumbnail
        has arrived (notify::thumb, the path unchanged); a list does not rebind for either."""
        name = pspec.name
        if name in ('title', 'subtitle'):
            self._show_label(item)
        elif name in ('thumb', 'art') and not self._slot.set_paths(item.thumb or item.art):
            self._slot.refresh()

    def do_map(self):
        Gtk.Box.do_map(self)
        self._slot.map(self.get_scale_factor())

    def do_unmap(self):
        self._slot.unmap()
        Gtk.Box.do_unmap(self)

    def _set_art(self, paintable, found):
        if self._artist:
            self.avatar.set_custom_image(paintable if found else None)
        else:
            # Never None, and the icon faded rather than hidden: either would lay the grid
            # out again (artwork.empty()).
            self.picture.set_paintable(paintable)
            self.placeholder_icon.set_opacity(0 if found else 1)
