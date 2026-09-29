"""AppleMusicArtistPage: an artist's portrait and name over their albums."""

import logging
from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from ..backend.errors import EngineError
from ..library import Item
from ..remote import fetch_cover
from ..widgets import artwork, context_menu
from ..widgets.engine_status import EngineStatus
from ..widgets.tile import Tile
from ..widgets.util import connect_weak, weak_method
from . import app

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
        self._accessible_format = _('{title}, {subtitle}')
        self._fetching = False
        # What the status page says when the engine cannot answer, and what its button does.
        self._engine_status = EngineStatus(app(), self._show_status, self._fetch, {
            'engine-down': _('Start the engine to load the albums'),
            'not-signed-in': _('The albums appear once you sign in to Apple Music'),
            'failed': _('Could Not Load the Albums'),
        })
        self._albums = Gio.ListStore(item_type=Item)
        # What a child holds calls the page weakly (widgets/util.py): a bound method would
        # keep the page alive once popped.
        self.flow_box.bind_model(self._albums, weak_method(self._create_tile))
        connect_weak(self.flow_box, 'child-activated', self._on_album_activated)
        connect_weak(self.status_button, 'clicked', self._on_status_clicked)
        context_menu.attach(self.flow_box)
        # The portrait is decoded while the page is shown, like any artwork: the 640 px cover,
        # and the thumbnail meanwhile, or for good when the cover cannot be had.
        self._portrait = artwork.ArtworkSlot(self._set_portrait, self.avatar.get_size())
        self._portrait.attach(self.avatar)
        self._show()

    def _show(self):
        item = self.item
        self._portrait.set_paths(item.art, item.thumb)
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
        self._engine_status.loading()
        app().spawn(self._fetch_groups())

    async def _fetch_groups(self):
        item = self.item
        try:
            answer = await app().engine.item(item.kind, item.id)
        except EngineError as error:
            log.info('albums of artist %s: %s', item.id, error)
            self._fetching = False
            self._engine_status.fail(error)
            return
        self._fetching = False
        item.merge(answer)
        self._show()

    def _show_status(self, status, title, description, button):
        """The status page: the spinner ('loading'), or EngineStatus's states."""
        if status == 'loading':
            self.status_page.set_paintable(Adw.SpinnerPaintable.new(self.status_page))
            title, description, button = _('Loading…'), '', None
        else:
            self.status_page.set_icon_name('media-optical-cd-audio-symbolic')
        self.status_page.set_title(title)
        self.status_page.set_description(description)
        self.status_button.set_label(button or '')
        self.status_button.set_visible(bool(button))

    def _on_status_clicked(self, _button):
        self._engine_status.activate()

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

    def _on_album_activated(self, _flow_box, child):
        album = self._albums.get_item(child.get_index())
        if album is not None:
            self.get_root().open_item(album)

    # The portrait: the slot decodes it while the avatar is mapped.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._engine_status.watch()
        if self.item.raw.get('artUrl'):
            # A sync fetches thumbnails only: the portrait's full size comes now.
            app().spawn(self._fetch_cover(self.item))

    def do_unmap(self):
        self._engine_status.unwatch()
        Adw.NavigationPage.do_unmap(self)

    async def _fetch_cover(self, item):
        if await fetch_cover(item) and self.item is item:
            self._portrait.refresh()

    def _set_portrait(self, paintable, found):
        # Without a picture, the avatar shows the artist's initials.
        self.avatar.set_custom_image(paintable if found else None)
