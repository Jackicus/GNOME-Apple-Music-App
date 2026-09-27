"""AppleMusicArtistPage: an artist's portrait and name over their albums."""

from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from ..library import Item
from ..widgets import artwork
from ..widgets.tile import Tile


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/artist.ui')
class ArtistPage(Adw.NavigationPage):
    """An artist Item's page: a round portrait, the name and details, the biography, then the
    albums as tiles, newest first, each opening its album's page.

    Each group of an artist Item is one of their albums (the backend README), named after it
    and playing it: {kind: album, id}. The album comes from the library by that id; one the
    library does not have is made up from the group, so its page still lists the tracks.
    """

    __gtype_name__ = 'AppleMusicArtistPage'

    avatar = Gtk.Template.Child()
    name_label = Gtk.Template.Child()
    caption_label = Gtk.Template.Child()
    summary_label = Gtk.Template.Child()
    albums_label = Gtk.Template.Child()
    flow_box = Gtk.Template.Child()
    status_page = Gtk.Template.Child()

    def __init__(self, library, item):
        super().__init__(title=item.title)
        self.item = item
        self._library = library
        self._token = None
        self._accessible_format = _('{title}, {subtitle}')

        self.avatar.set_text(item.title)
        self.name_label.set_label(item.title)
        details = [item.genre, item.count_label]
        self.caption_label.set_label(' · '.join(detail for detail in details if detail))
        self.caption_label.set_visible(any(details))
        self.summary_label.set_label(item.summary or '')
        self.summary_label.set_visible(bool(item.summary))

        self._albums = Gio.ListStore(item_type=Item)
        self._albums.splice(0, 0, self._resolve_albums(item))
        self.flow_box.bind_model(self._albums, self._create_tile)
        has_albums = self._albums.get_n_items() > 0
        self.albums_label.set_visible(has_albums)
        self.flow_box.set_visible(has_albums)
        self.status_page.set_visible(not item.groups)

    def _resolve_albums(self, item):
        albums = []
        for group in item.groups:
            play = group.play
            album = self._library.by_id(play.get('kind'), play.get('id'))
            if album is None:
                entry = group.entries.get_item(0) if group.entries.get_n_items() else None
                album = Item({'id': play.get('id'), 'kind': 'album', 'title': group.name,
                              'subtitle': item.title, 'thumb': entry.thumb if entry else None,
                              'play': play, 'groups': [group.raw]})
            albums.append(album)
        albums.sort(key=lambda album: album.year, reverse=True)  # stable: ties keep their order
        return albums

    def _create_tile(self, album):
        tile = Tile()
        tile.bind(album)
        child = Gtk.FlowBoxChild(child=tile)
        label = (self._accessible_format.format(title=album.title, subtitle=album.subtitle)
                 if album.subtitle else album.title)
        child.update_property([Gtk.AccessibleProperty.LABEL], [label])
        return child

    @Gtk.Template.Callback()
    def on_album_activated(self, _flow_box, child):
        album = self._albums.get_item(child.get_index())
        if album is not None:
            self.get_root().open_item(album)

    # The portrait is decoded while the page is shown, like any artwork.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        loader = artwork.get_default()
        path = self.item.art or self.item.thumb
        texture = loader.get(path) if path else None
        if texture is not None:
            self.avatar.set_custom_image(texture)
        elif path:
            self._token = loader.request(path, self._on_texture)

    def do_unmap(self):
        artwork.get_default().cancel(self._token)
        self._token = None
        self.avatar.set_custom_image(None)
        Adw.NavigationPage.do_unmap(self)

    def _on_texture(self, texture):
        self._token = None
        self.avatar.set_custom_image(texture)
