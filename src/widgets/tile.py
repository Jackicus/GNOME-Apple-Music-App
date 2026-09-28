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


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/tile.ui')
class Tile(Gtk.Box):
    """A 160 px square cover over a title and a dim subtitle; with artist=True, a round portrait
    over a centred name.

    A playlist folder (an Item of kind 'folder') has no cover: its tile shows a folder icon.
    bind(item) and unbind() are called by a list factory as tiles are recycled; the Item bound
    is the tile's `context_item`, whose menu the view shows (widgets/context_menu.py). The cover is
    asked for only while the tile is mapped, which in a Gtk.GridView means on screen (the grid
    binds many more tiles than it shows), and let go of when it is unmapped, so the textures
    alive are about the ones on screen plus the Artwork cache.
    """

    __gtype_name__ = 'AppleMusicTile'

    cover = Gtk.Template.Child()
    placeholder_icon = Gtk.Template.Child()
    picture = Gtk.Template.Child()
    avatar = Gtk.Template.Child()
    label = Gtk.Template.Child()

    def __init__(self, artist=False, **kwargs):
        super().__init__(**kwargs)
        self._artist = False
        self._folder = False
        self._item = None
        self._path = None
        self._token = None
        self._artwork = artwork.get_default()
        self.set_artist(artist)

    def set_artist(self, artist):
        """Switch between the square cover and the artist's round portrait. A grid page's tiles
        are one or the other; a shelf, which can mix artists with albums, switches a tile as it
        binds it (before bind())."""
        if artist == self._artist:
            return
        self._release_art()
        self._artist = artist
        self.cover.set_visible(not artist)
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
            self._show_art()

    def unbind(self):
        self._release_art()
        self._item = None
        self._path = None

    def do_map(self):
        Gtk.Box.do_map(self)
        if self._item is not None:
            self._show_art()

    def do_unmap(self):
        self._release_art()
        Gtk.Box.do_unmap(self)

    def _show_art(self):
        self._artwork.cancel(self._token)
        self._token = None
        texture = self._artwork.get(self._path) if self._path else None
        self._set_texture(texture)
        if texture is None and self._path:
            self._token = self._artwork.request(self._path, self._on_texture)

    def _release_art(self):
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
            self.picture.set_paintable(texture)
            self.placeholder_icon.set_visible(texture is None)
