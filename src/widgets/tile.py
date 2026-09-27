"""AppleMusicTile: an Item as a grid tile, its artwork, title and subtitle."""

from gi.repository import GLib, Gtk

from . import artwork

# The subtitle's look inside the tile's markup: libadwaita's dim-label opacity and about its
# caption size.
SUBTITLE = '<span size="smaller" alpha="55%">{}</span>'


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/tile.ui')
class Tile(Gtk.Box):
    """A 160 px square cover over a title and a dim subtitle; with artist=True, a round portrait
    over a centred name.

    bind(item) and unbind() are called by a list factory as tiles are recycled. The cover is
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
        self._artist = artist
        self._item = None
        self._path = None
        self._token = None
        self._artwork = artwork.get_default()
        if artist:
            self.cover.set_visible(False)
            self.avatar.set_visible(True)
            self.label.set_xalign(0.5)
            self.label.set_min_lines(1)

    def bind(self, item):
        self._item = item
        self._path = item.thumb or item.art
        if self._artist or not item.subtitle:
            self.label.set_text(item.title)
        else:
            self.label.set_markup(GLib.markup_escape_text(item.title) + '\n'
                                  + SUBTITLE.format(GLib.markup_escape_text(item.subtitle)))
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
