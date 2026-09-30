# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicArtistPage: an artist's portrait and name over their albums and biography."""

import logging
from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from ..actions import can_play
from ..backend.errors import EngineError
from ..library import Item, apply_diff
from ..remote import fetch_cover
from ..widgets import artwork, context_menu
from ..widgets.engine_status import EngineStatus
from ..widgets.labels import accessible_label, flow_child
from ..widgets.tile import Tile
from ..widgets.util import HeaderTitle, MappedHandlers, connect_weak, weak_method
from . import app, show_notes

log = logging.getLogger(__name__)


def stand_in(artist, group):
    """An Item for an artist's album the library does not have: its name, the artist, the
    first track's thumbnail and no groups, so that its page fetches the whole album (discs,
    year, notes and cover), as a shelf's album's does."""
    entry = group.entries.get_item(0) if group.entries.get_n_items() else None
    data = {'id': group.play.get('id'), 'kind': 'album', 'title': group.name,
            'subtitle': artist.title, 'thumb': entry.thumb if entry else None,
            'play': group.play, 'groups': []}
    art_url = entry.raw.get('artUrl') if entry is not None else None
    if art_url:
        data['artUrl'] = art_url
    return Item(data)


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/artist.ui')
class ArtistPage(Adw.NavigationPage):
    """An artist Item's page: a round portrait, the name and details, Play (a catalog
    artist's top songs, actions.can_play()) and the artist's menu, then the albums as tiles,
    newest first, each opening its album's page, and the biography under About (three lines,
    More for the whole). The header bar shows the name once the big one has scrolled away.

    Each group of an artist Item is one of their albums (the backend README), named after it
    and playing it: {kind: album, id}. The album comes from the library by that id; one the
    library does not have (a catalog artist's) is a stand-in Item without groups, so its page
    fetches the whole album (discs, year, notes, cover) as a shelf's album does. An artist
    that came without groups (a shelf's) has them fetched once, the status page saying so
    meanwhile, or why not (EngineStatus), or that there are none. The page follows the
    artist Item and the library while it is shown, as the detail page does.
    """

    __gtype_name__ = 'AppleMusicArtistPage'

    header_bar = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    avatar = Gtk.Template.Child()
    name_label = Gtk.Template.Child()
    caption_label = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    more_button = Gtk.Template.Child()
    summary_box = Gtk.Template.Child()
    summary_label = Gtk.Template.Child()
    more_notes_button = Gtk.Template.Child()
    albums_label = Gtk.Template.Child()
    flow_box = Gtk.Template.Child()
    status_page = Gtk.Template.Child()
    status_button = Gtk.Template.Child()

    def __init__(self, library, item):
        super().__init__(title=item.title)
        self.item = item
        self._library = library
        self._fetched = None  # the Item this page asked the engine for (once)
        self._fetch_task = None
        self._stand_ins = {}  # album id -> the stand-in Item made for an album not in the library
        # What the status page says when the engine cannot answer, and what its button does.
        self._engine_status = EngineStatus(app(), self._show_status, self._refetch, {
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
        connect_weak(self.play_button, 'clicked', self._on_play_clicked)
        connect_weak(self.more_notes_button, 'clicked', self._on_more_notes_clicked)
        self.more_button.set_create_popup_func(weak_method(self._on_more_popup))
        self._header_title = HeaderTitle(self.header_bar, self.name_label, self.scrolled_window)
        self._painted = None  # (frame clock, handler): the biography's More follows each paint
        context_menu.attach(self.flow_box)
        # The portrait is decoded while the page is shown, like any artwork: the 640 px cover,
        # and the thumbnail meanwhile, or for good when the cover cannot be had.
        self._portrait = artwork.ArtworkSlot(self._set_portrait, self.avatar.get_size())
        self._portrait.attach(self.avatar)
        # The library (its albums) and the artist, followed while the page is shown.
        self._handlers = MappedHandlers(self)
        self._handlers.add(library, 'changed', self._show)
        self._handlers.add(item, 'groups-changed', self._show)
        self._handlers.add(item, 'notify', self._on_item_notify)
        self._show()

    def _on_item_notify(self, _item, _pspec):
        self._show_hero()

    def _show_hero(self):
        item = self.item
        self._portrait.set_paths(item.art, item.thumb)
        self.avatar.set_text(item.title)
        self.name_label.set_label(item.title)
        details = [item.genre, item.count_label]
        self.caption_label.set_label(' · '.join(detail for detail in details if detail))
        self.caption_label.set_visible(any(details))
        self.summary_label.set_label(item.summary or '')
        self.summary_box.set_visible(bool(item.summary))
        self.play_button.set_visible(can_play(item))

    def _show(self, *_args):
        item = self.item
        self._show_hero()
        apply_diff(self._albums, self._resolve_albums(item))
        has_albums = self._albums.get_n_items() > 0
        self.albums_label.set_visible(has_albums)
        self.flow_box.set_visible(has_albums)
        if not item.groups and self._fetched is not item:
            self._fetch()
        elif not item.groups and self._engine_status.status is None:
            self._engine_status.clear()
            self._show_status('empty', _('No Albums'), '', None)
        self.status_page.set_visible(not item.groups)

    # Fetching the albums of an artist that came without them, once.

    def _fetch(self):
        self._fetched = self.item
        if self._fetch_task is not None and not self._fetch_task.done():
            self._fetch_task.cancel()
        self._engine_status.loading()
        self._fetch_task = app().spawn(self._fetch_groups(self.item))

    def _refetch(self):
        """EngineStatus's retry (Try Again, the engine up, signed in): ask again."""
        self._fetched = None
        self._fetch()

    async def _fetch_groups(self, item):
        try:
            answer = await app().engine.item(item.kind, item.id)
        except EngineError as error:
            log.info('albums of artist %s: %s', item.id, error)
            self._engine_status.fail(error)
            return
        self._engine_status.clear()
        item.merge(answer)  # new groups emit groups-changed, which shows them while mapped
        self._show()

    def _show_status(self, status, title, description, button):
        """The status page: the spinner ('loading'), EngineStatus's states, or 'empty' (the
        artist has no albums)."""
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
        """The artist's albums, newest first: the library's Items, or stand-ins (kept, so a
        reload keeps their tiles)."""
        albums = []
        for group in item.groups:
            play = group.play
            album = self._library.by_id(play.get('kind'), play.get('id'))
            if album is None:
                album = self._stand_ins.get(play.get('id'))
                if album is None:
                    album = self._stand_ins[play.get('id')] = stand_in(item, group)
            albums.append(album)
        albums.sort(key=lambda album: album.year, reverse=True)  # stable: ties keep their order
        return albums

    def _create_tile(self, album):
        tile = Tile(halign=Gtk.Align.START)
        tile.bind(album)
        return flow_child(tile, accessible_label(album))

    def _on_album_activated(self, _flow_box, child):
        album = self._albums.get_item(child.get_index())
        if album is not None:
            self.get_root().open_item(album)

    # The portrait: the slot decodes it while the avatar is mapped.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._show()  # what a reload changed while the page was hidden
        self._engine_status.watch()
        clock = self.get_frame_clock()
        if clock is not None and self._painted is None:
            self._painted = (clock, connect_weak(clock, 'after-paint', self._on_painted))
        if self.item.raw.get('artUrl'):
            # A sync fetches thumbnails only: the portrait's full size comes now.
            app().spawn(self._fetch_cover(self.item))

    def do_unmap(self):
        self._engine_status.unwatch()
        if self._painted is not None:
            clock, handler = self._painted
            clock.disconnect(handler)
            self._painted = None
        Adw.NavigationPage.do_unmap(self)

    # Play, the menu and the biography.

    def _on_play_clicked(self, _button):
        self.get_root().play_request(self.item.play, shuffle=False)

    def _on_more_popup(self, button):
        """The artist's menu, made as it opens."""
        actions = getattr(self.get_root(), 'item_actions', None)
        button.set_menu_model(actions.menu_for(self.item) if actions is not None else None)

    def _on_more_notes_clicked(self, _button):
        show_notes(self, self.item.title, self.item.summary or '')

    def _on_painted(self, _clock):
        """More under the biography, while it is cut to its three lines."""
        layout = self.summary_label.get_layout()
        cut = self.summary_label.get_mapped() and layout is not None and layout.is_ellipsized()
        if cut != self.more_notes_button.get_visible():
            self.more_notes_button.set_visible(cut)

    def do_hidden(self):
        # Left (popped, covered, or another destination shown), not just unmapped (a push
        # maps, unmaps and maps a page again): the fetch stops, asked again when shown.
        if self._fetch_task is not None and not self._fetch_task.done():
            self._fetch_task.cancel()
            self._fetch_task = None
            self._fetched = None
            self._engine_status.clear()
        Adw.NavigationPage.do_hidden(self)

    async def _fetch_cover(self, item):
        if await fetch_cover(item) and self.item is item:
            self._portrait.refresh()

    def _set_portrait(self, paintable, found):
        # Without a picture, the avatar shows the artist's initials.
        self.avatar.set_custom_image(paintable if found else None)
