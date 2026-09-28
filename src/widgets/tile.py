"""AppleMusicTile: an Item as a grid tile, its artwork, title and subtitle."""

from gi.repository import Adw, GLib, Gtk

from . import artwork

# The subtitle's look inside the tile's markup: about the caption size, at libadwaita's
# dim-label opacity (its --dim-opacity: 55%, and 90% with the system's high-contrast setting,
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
    over a centred name.

    A playlist folder (an Item of kind 'folder') has no cover: its tile shows a folder icon.
    bind(item) and unbind() are called by a list factory as tiles are recycled; the Item bound
    is the tile's `context_item`, whose menu the view shows (widgets/context_menu.py). The cover is
    asked for only while the tile is mapped, which in a Gtk.GridView means on screen, and let
    go of when it is unmapped, so the textures alive are about the ones on screen plus the
    Artwork cache. Asked for in an idle after the frame, not as the tile is mapped: a grid
    creates and maps over a hundred tiles at once and unmaps all but the visible dozen in its
    first layout, and a tile scrolling past is rebound many times a second; either way the
    request is made once the tile has settled on an item on screen.
    The texture is decoded at the size drawn (ART_SIZE times the scale factor).
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
        self._item = None
        self._path = None
        self._token = None
        self._idle = None  # the idle that will ask for the cover
        self._artwork = artwork.get_default()
        self.avatar = None  # the artist variant's portrait, made when first wanted
        self.set_artist(artist)

    def set_artist(self, artist):
        """Switch between the square cover and the artist's round portrait. A grid page's tiles
        are one or the other; a shelf, which can mix artists with albums, switches a tile as it
        binds it (before bind())."""
        if artist == self._artist:
            return
        self._release_art()
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
        self.label.set_min_lines(1 if artist else 3)

    @property
    def context_item(self):
        """The Item shown, for its context menu (widgets/context_menu.py)."""
        return self._item

    def bind(self, item):
        self._item = item
        self._path = item.thumb or item.art
        folder = item.kind == 'folder'
        if folder != self._folder:  # only then: setting an icon costs a relayout
            self._folder = folder
            icon_name, size = FOLDER if folder else PLACEHOLDER
            self.placeholder_icon.set_from_icon_name(icon_name)
            self.placeholder_icon.set_pixel_size(size)
        if self._artist or not item.subtitle:
            self.label.set_text(item.title)
        else:
            self.label.set_markup(GLib.markup_escape_text(item.title) + '\n'
                                  + subtitle_markup().format(
                                      GLib.markup_escape_text(item.subtitle)))
        if self.get_mapped():
            self._show_art_soon()

    def unbind(self):
        self._release_art()
        self._item = None
        self._path = None

    def do_map(self):
        Gtk.Box.do_map(self)
        if self._item is not None:
            self._show_art_soon()

    def do_unmap(self):
        self._release_art()
        Gtk.Box.do_unmap(self)

    def _show_art_soon(self):
        """Show what the cache has now, and ask for the rest once the frame has settled."""
        self._artwork.cancel(self._token)
        self._token = None
        size = ART_SIZE * self.get_scale_factor()
        texture = self._artwork.get(self._path, size) if self._path else None
        self._set_texture(texture)
        if texture is None and self._path and self._idle is None:
            self._idle = GLib.idle_add(self._show_art, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _show_art(self):
        self._idle = None
        if self._path and self.get_mapped():
            self._artwork.cancel(self._token)
            self._token = self._artwork.request(self._path, self._on_texture,
                                                ART_SIZE * self.get_scale_factor())
        return GLib.SOURCE_REMOVE

    def _release_art(self):
        if self._idle is not None:
            GLib.source_remove(self._idle)
            self._idle = None
        self._artwork.cancel(self._token)
        self._token = None
        self._set_texture(None)

    def _on_texture(self, texture):
        self._token = None
        self._set_texture(texture)

    def _set_texture(self, texture):
        if self._artist:
            self.avatar.set_custom_image(texture)
        else:
            # Never None, and the icon faded rather than hidden: either would lay the grid
            # out again (artwork.empty()).
            self.picture.set_paintable(
                texture or artwork.empty(ART_SIZE * self.get_scale_factor()))
            self.placeholder_icon.set_opacity(0 if texture else 1)
