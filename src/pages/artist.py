"""AppleMusicArtistPage: an artist's portrait and name over their albums."""

import logging
from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from ..backend.errors import EngineError
from ..library import Item
from ..widgets import artwork, context_menu
from ..widgets.tile import Tile

log = logging.getLogger(__name__)


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/artist.ui')
class ArtistPage(Adw.NavigationPage):
    """An artist Item's page: a round portrait, the name and details, the biography, then the
    albums as tiles, newest first, each opening its album's page.

    Each group of an artist Item is one of their albums (the backend README), named after it
    and playing it: {kind: album, id}. The album comes from the library by that id; one the
    library does not have is made up from the group, so its page still lists the tracks. An
    artist that came without groups (a shelf's) gets them from the engine, the status page
    saying so meanwhile, or why not (as the detail page's status box does).
    """

    __gtype_name__ = 'AppleMusicArtistPage'

    avatar = Gtk.Template.Child()
    name_label = Gtk.Template.Child()
    caption_label = Gtk.Template.Child()
    summary_label = Gtk.Template.Child()
    albums_label = Gtk.Template.Child()
    flow_box = Gtk.Template.Child()
    status_page = Gtk.Template.Child()
    status_button = Gtk.Template.Child()

    def __init__(self, library, item):
        super().__init__(title=item.title)
        self.item = item
        self._library = library
        self._token = None
        self._accessible_format = _('{title}, {subtitle}')
        self._fetching = False
        self._status = None
        self._albums = Gio.ListStore(item_type=Item)
        self.flow_box.bind_model(self._albums, self._create_tile)
        context_menu.attach(self.flow_box)
        self._show()

    def _show(self):
        item = self.item
        self.avatar.set_text(item.title)
        self.name_label.set_label(item.title)
        details = [item.genre, item.count_label]
        self.caption_label.set_label(' · '.join(detail for detail in details if detail))
        self.caption_label.set_visible(any(details))
        self.summary_label.set_label(item.summary or '')
        self.summary_label.set_visible(bool(item.summary))

        self._albums.splice(0, self._albums.get_n_items(), self._resolve_albums(item))
        has_albums = self._albums.get_n_items() > 0
        self.albums_label.set_visible(has_albums)
        self.flow_box.set_visible(has_albums)
        if not item.groups and not self._fetching:
            self._fetch()
        self.status_page.set_visible(not item.groups)

    # Fetching the albums of an artist that came without them.

    def _fetch(self):
        self._fetching = True
        self._set_status('loading')
        Gio.Application.get_default().spawn(self._fetch_groups())

    async def _fetch_groups(self):
        app = Gio.Application.get_default()
        item = self.item
        try:
            answer = await app.engine.item(item.kind, item.id)
        except EngineError as error:
            log.info('albums of artist %s: %s', item.id, error)
            self._fetching = False
            self._set_status(error.code, error.message)
            return
        self._fetching = False
        item.merge(answer)
        self._show()
        if self.get_mapped():
            self._load_portrait()

    def _set_status(self, status, message=''):
        self._status = status
        if status == 'loading':
            self.status_page.set_paintable(Adw.SpinnerPaintable.new(self.status_page))
            title, description, button = _('Loading…'), '', None
        else:
            self.status_page.set_icon_name('media-optical-cd-audio-symbolic')
            if status == 'engine-down':
                title = _('Engine Not Running')
                description = _('Start the engine to load the albums')
                button = _('Start Engine')
            elif status == 'not-signed-in':
                title = _('Sign In to Load This')
                description = _('The albums appear once you sign in to Apple Music')
                button = _('Sign In')
            else:
                title = _('Could Not Load the Albums')
                description = message
                button = _('Try Again')
        self.status_page.set_title(title)
        self.status_page.set_description(description)
        self.status_button.set_label(button or '')
        self.status_button.set_visible(bool(button))

    @Gtk.Template.Callback()
    def on_status_clicked(self, _button):
        app = Gio.Application.get_default()
        if self._status == 'not-signed-in':
            app.activate_action('sign-in')
        elif self._status == 'engine-down':
            self._fetching = True
            self._set_status('loading')
            app.spawn(self._start_and_fetch())
        else:
            self._fetch()

    async def _start_and_fetch(self):
        app = Gio.Application.get_default()
        try:
            await app.engine.start()
        except EngineError as error:
            app.report(error)
            self._fetching = False
            self._set_status(error.code, error.message)
            return
        await self._fetch_groups()

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
        self._load_portrait()
        if self.item.raw.get('artUrl'):
            # A sync fetches thumbnails only: the portrait's full size comes now.
            Gio.Application.get_default().spawn(self._fetch_cover(self.item))

    async def _fetch_cover(self, item):
        if await artwork.get_default().fetch_cover(item) and self.get_mapped():
            self._load_portrait()

    def _load_portrait(self):
        loader = artwork.get_default()
        loader.cancel(self._token)
        self._token = None
        path = self.item.art or self.item.thumb
        size = self.avatar.get_size() * self.get_scale_factor()
        texture = loader.get(path, size) if path else None
        if texture is not None:
            self.avatar.set_custom_image(texture)
        elif path:
            self._token = loader.request(path, self._on_texture, size)

    def do_unmap(self):
        artwork.get_default().cancel(self._token)
        self._token = None
        self.avatar.set_custom_image(None)
        Adw.NavigationPage.do_unmap(self)

    def _on_texture(self, texture):
        self._token = None
        self.avatar.set_custom_image(texture)
