# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicArtistPage: an artist as music.apple.com shows one, laid out the GNOME way: the
portrait and name, the latest release beside the top songs, the catalog's shelves in Apple's
order, About, and Similar Artists; the albums in the library when the catalog cannot be had."""

import logging
from gettext import gettext as _
from gettext import pgettext as C_

from gi.repository import Adw, GLib, Gtk

from ..actions import can_play
from ..backend.api import is_library_id
from ..backend.errors import EngineError
from ..library import Item, ShelfModel, fold
from ..related import credits
from ..remote import fetch_cover, fetch_shelf_art, fetch_thumb, remote_item
from ..widgets import artwork
from ..widgets.cover import Cover  # noqa: F401  registers $AppleMusicCover for the template
from ..widgets.engine_status import EngineStatus
from ..widgets.labels import accessible_label
from ..widgets.shelf import ShelfColumn
from ..widgets.song_shelf import SongShelf  # noqa: F401  registers $AppleMusicSongShelf
from ..widgets.util import HeaderTitle, MappedHandlers, connect_weak, weak_method
from . import app, show_notes

log = logging.getLogger(__name__)

# The shelf that shows an artist's albums in large cards with Apple's line about each.
HERO_SHELF = 'featured-albums'
# The shelf that comes after About, as on music.apple.com.
AFTER_ABOUT = ('similar-artists',)
# How many of a library artist's songs are asked for their catalog artist, at most.
SONGS_ASKED = 3


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


def catalog_id(item):
    """The catalog artist an artist Item is, when it says: a catalog artist's own id, or the
    catalog id a library artist carries (None for the library's made-up artists, which the
    engine finds through their songs: song_ids())."""
    if item.id and not is_library_id(item.id):
        return item.id
    return item.catalog_id or None


def song_ids(item, library=None, limit=SONGS_ASKED):
    """Up to `limit` catalog song ids from an artist's albums, one an album first: what the
    engine asks for their artist (Engine.catalog_artist), a song the artist is credited on
    first (a compilation's first is someone else's). A group whose album the library has is
    empty (the library drops the artist's copy of its tracks as it loads: library.py), so its
    tracks are the library album's."""
    ids = []
    for group in item.groups:
        stores = [group.entries]
        if not group.entries.get_n_items() and library is not None:
            album = library.by_id(group.play.get('kind'), group.play.get('id'))
            stores = [disc.entries for disc in album.groups] if album is not None else []
        tracks = [track for store in stores for position in range(store.get_n_items())
                  for track in (store.get_item(position),) if track.catalog_id]
        track = next((track for track in tracks if credits(track.artist, item.title)),
                     tracks[0] if tracks else None)
        if track is not None:
            ids.append(track.catalog_id)
        if len(ids) >= limit:
            break
    return ids


def release_date(text):
    """Apple's release date ('2026-09-24') in the reader's words ("24 Sept 2026"), or ''."""
    try:
        year, month, day = (int(part) for part in str(text or '').split('-')[:3])
        date = GLib.DateTime.new_local(year, month, day, 0, 0, 0)
    except (TypeError, ValueError):
        return ''
    if date is None:
        return ''
    # Translators: a release's date on an artist's page, as GLib.DateTime.format() takes it:
    # "%-d %b %Y" is "24 Sept 2026".
    return date.format(C_('release date', '%-d %b %Y')) or ''


def view_title(data):
    """A view's title as Apple gives it ("Artist Playlists", in the account's language): the
    keys are Apple's view names, not the search's shelves, which remote.shelf_title() names."""
    title = data.get('title') if isinstance(data, dict) else None
    return title if isinstance(title, str) else ''


def page_shelves(dicts):
    """ShelfModels of an artist page's shelves ({key, title, items, more}), titled as Apple
    titles them, their items with their artwork where the app fetches it (remote_item)."""
    shelves = []
    for data in dicts or []:
        if not isinstance(data, dict):
            continue
        items = [Item(remote_item(entry)) for entry in data.get('items') or []
                 if isinstance(entry, dict) and entry.get('id') and entry.get('kind')]
        if items:
            shelf = ShelfModel(str(data.get('key') or ''), view_title(data), items)
            shelf.more = bool(data.get('more'))
            shelves.append(shelf)
    return shelves



@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/artist.ui')
class ArtistPage(Adw.NavigationPage):
    """An artist Item's page: a round portrait, the name and genre, Play (the catalog
    artist's top songs, actions.can_play()) and the artist's menu; then what Apple's catalog
    has for the artist (Engine.artist_page, kept for a day): the latest or featured release
    beside the top songs, the catalog's shelves in Apple's order and titled as Apple titles
    them (Essential Albums as large cards), About (the biography, three lines and More, and
    where the artist is from, when born or formed, and the genre) and Similar Artists. The
    header bar shows the name once the big one has scrolled away.

    Which catalog artist: a catalog artist's own id, or the catalog id a library artist
    carries; a library artist the sync made up from its songs' names is found through its
    songs (Engine.catalog_artist; song_ids(), from the library's albums). The albums the
    library has of the artist's (the library artist's, or for a catalog artist the library's
    of the same name) come first, as In Your Library, beside the catalog's shelves (the same
    album may be on both: a library album carries no catalog id to tell), and alone without
    the engine or with no catalog artist found. The catalog is asked once while the page is
    shown (and again when hidden then shown); what stops it shows in the status page
    (EngineStatus), under the library's albums when there are some. The page follows the
    artist Item and the library while it is shown.

    Each group of an artist Item is one of their albums (the backend README), named after it
    and playing it: {kind: album, id}. The album comes from the library by that id; one the
    library does not have is a stand-in Item without groups, so its page fetches the whole
    album, as a shelf's album does.
    """

    __gtype_name__ = 'AppleMusicArtistPage'

    header_bar = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    content_box = Gtk.Template.Child()
    avatar = Gtk.Template.Child()
    name_label = Gtk.Template.Child()
    caption_label = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    more_button = Gtk.Template.Child()
    top_row = Gtk.Template.Child()
    release_box = Gtk.Template.Child()
    release_title = Gtk.Template.Child()
    release_button = Gtk.Template.Child()
    release_cover = Gtk.Template.Child()
    release_date = Gtk.Template.Child()
    release_name = Gtk.Template.Child()
    release_count = Gtk.Template.Child()
    top_songs = Gtk.Template.Child()
    status_page = Gtk.Template.Child()
    status_button = Gtk.Template.Child()
    about_box = Gtk.Template.Child()
    about_title = Gtk.Template.Child()
    summary_label = Gtk.Template.Child()
    more_notes_button = Gtk.Template.Child()
    facts_box = Gtk.Template.Child()
    origin_fact = Gtk.Template.Child()
    origin_heading = Gtk.Template.Child()
    origin_label = Gtk.Template.Child()
    born_fact = Gtk.Template.Child()
    born_heading = Gtk.Template.Child()
    born_label = Gtk.Template.Child()
    genre_fact = Gtk.Template.Child()
    genre_label = Gtk.Template.Child()

    def __init__(self, library, item):
        super().__init__(title=item.title)
        self.item = item
        self._library = library
        self._answer = None  # Engine.artist_page's answer, once it has come
        self._asked = False  # the catalog has been asked while the page is shown
        self._fetch_task = None
        self._art_task = None
        self._stand_ins = {}  # album id -> the stand-in Item made for an album not in the library
        self._catalog_artist = None  # the answer's artist, as an Item: its portrait and bio
        self._release = None  # the release card's Item
        self._shelves = []  # the catalog's ShelfModels, as shown
        self._top = None  # the top songs' ShelfModel
        # What the status page says when the engine cannot answer, and what its button does.
        self._engine_status = EngineStatus(app(), self._show_status, self._refetch, {
            'engine-down': _('Start the engine to see the rest of this artist’s music'),
            'not-signed-in': _('The rest of this artist’s music appears once you sign in '
                               'to Apple Music'),
            'failed': _('Could Not Load the Artist'),
        })
        self._status = None  # what the status page shows: 'loading', a failure, or None
        self._library_shelf = ShelfModel('library', _('In Your Library'), [])
        connect_weak(self.status_button, 'clicked', self._on_status_clicked)
        connect_weak(self.play_button, 'clicked', self._on_play_clicked)
        connect_weak(self.more_notes_button, 'clicked', self._on_more_notes_clicked)
        connect_weak(self.release_button, 'clicked', self._on_release_clicked)
        self.more_button.set_create_popup_func(weak_method(self._on_more_popup))
        self._header_title = HeaderTitle(self.header_bar, self.name_label, self.scrolled_window)
        self._painted = None  # (frame clock, handler): the biography's More follows each paint
        # The shelves before About, after the top row; Similar Artists after About.
        self._column = ShelfColumn(self.content_box, self.top_row)
        self._after = ShelfColumn(self.content_box, self.about_box)
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

    # What the page shows.

    def _show(self, *_args):
        self._show_hero()
        self._library_shelf.update(self._library_shelf.title, self._library_albums())
        self._show_sections()

    def _show_hero(self):
        item = self.item
        catalog = self._catalog_artist
        if catalog is not None:
            # The catalog's portrait once it is on disk (a library artist's picture is its
            # first album's cover), the library's meanwhile.
            self._portrait.set_paths(catalog.art, catalog.thumb, item.art, item.thumb)
        else:
            self._portrait.set_paths(item.art, item.thumb)
        self.avatar.set_text(item.title)
        self.name_label.set_label(item.title)
        genre = item.genre or (catalog.genre if catalog is not None else None)
        details = [genre, item.count_label]
        self.caption_label.set_label(' · '.join(detail for detail in details if detail))
        self.caption_label.set_visible(any(details))
        self.play_button.set_visible(self._play_target() is not None)
        self._show_about()

    def _play_target(self):
        """What Play plays: the catalog artist (its top songs), when the page knows it: at
        once when the artist carries its catalog id, else once its songs have found it."""
        if can_play(self.item):
            return self.item.play
        if self._catalog_artist is not None and can_play(self._catalog_artist):
            return self._catalog_artist.play
        artist_id = catalog_id(self.item)
        return {'kind': 'artist', 'id': artist_id} if artist_id else None

    def _show_about(self):
        item = self.item
        catalog = self._catalog_artist
        raw = catalog.raw if catalog is not None else {}
        summary = (catalog.summary if catalog is not None else None) or item.summary or ''
        origin, born = raw.get('origin') or '', raw.get('bornOrFormed') or ''
        genre = item.genre or (catalog.genre if catalog is not None else None) or ''
        # Translators: the heading of an artist's biography: "About Taylor Swift".
        self.about_title.set_label(_('About {name}').format(name=item.title))
        self.summary_label.set_label(summary)
        self.summary_label.set_visible(bool(summary))
        # Translators: where an artist is from, over the place on their page ("From").
        self.origin_heading.set_label(_('From'))
        self.origin_label.set_label(origin)
        self.origin_fact.set_visible(bool(origin))
        # Translators: over the date a band was formed (Formed) or a person born (Born).
        self.born_heading.set_label(_('Formed') if raw.get('isGroup') else _('Born'))
        self.born_label.set_label(born)
        self.born_fact.set_visible(bool(born))
        self.genre_label.set_label(genre)
        self.genre_fact.set_visible(bool(genre) and catalog is not None)
        self.facts_box.set_visible(bool(origin or born) or (bool(genre) and catalog is not None))
        self.about_box.set_visible(bool(summary) or self.facts_box.get_visible())

    def _library_albums(self):
        """The artist's albums the library has, newest first: the library's Items, or
        stand-ins (kept, so a reload keeps their tiles). A catalog artist's are the library
        artist's of the same name, when there is one."""
        item = self.item
        if not item.groups and not is_library_id(item.id):
            item = self._library_artist() or item
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

    def _library_artist(self):
        """The library's artist named as this one is (a catalog artist's page), or None."""
        name = fold(self.item.title)
        artists = self._library.artists
        for position in range(artists.get_n_items()):
            artist = artists.get_item(position)
            if fold(artist.title) == name:
                return artist
        return None

    def _show_sections(self):
        """The top row, the shelves and the status page, from the answer as it stands."""
        answer = self._answer
        release = self._release
        self.release_box.set_visible(release is not None)
        if release is not None:
            latest = answer.get('latest') or {}
            self.release_title.set_label(view_title(latest))
            self.release_cover.set_paths(release.thumb, release.art)
            self.release_date.set_label(release_date(release.raw.get('releaseDate')))
            self.release_date.set_visible(bool(self.release_date.get_label()))
            self.release_name.set_label(release.title)
            self.release_count.set_label(release.count_label)
            self.release_count.set_visible(bool(release.count_label))
            self.release_button.update_property([Gtk.AccessibleProperty.LABEL],
                                                [accessible_label(release)])
        if self._top is not None:
            self.top_songs.bind_shelf(self._top)
        self.top_songs.set_visible(self._top is not None)
        self.top_row.set_visible(release is not None or self._top is not None)

        before, after = [], []
        for shelf in self._shelves:
            (after if shelf.key in AFTER_ABOUT else before).append(shelf)
        if self._library_shelf.items.get_n_items():
            before.insert(0, self._library_shelf)
        heroes = [shelf for shelf in before if shelf.key == HERO_SHELF]
        self._column.show(before, heroes=heroes)
        self._after.show(after)

        self._update_status()

    def _update_status(self):
        """The status page while the catalog is asked, or for what stopped it (under the
        library's albums, when there are some); "No Albums" once the page has nothing else to
        show, and the demo's notice only then too."""
        nothing = self._answer is None and not self._library_shelf.items.get_n_items()
        if self._status == 'empty' and not nothing:
            self._status = None
        elif self._status is None and nothing and self._asked:  # asked, and answered
            self._show_status('empty', _('No Albums'), '', None)
            return
        quiet = self._status in ('empty', 'demo')
        self.status_page.set_visible(self._status is not None and (nothing or not quiet))

    # Asking the catalog, once while the page is shown.

    def _fetching(self):
        return self._fetch_task is not None and not self._fetch_task.done()

    def _fetch(self):
        if self._fetching():
            self._fetch_task.cancel()
        self._asked = True
        self._engine_status.loading()
        self._fetch_task = app().spawn(self._fetch_page())

    def _refetch(self):
        """EngineStatus's retry (Try Again, the engine up, signed in): ask again."""
        self._fetch()

    async def _fetch_page(self):
        engine = app().engine
        try:
            artist_id = catalog_id(self.item)
            songs = song_ids(self.item, self._library) if artist_id is None else []
            if songs:
                artist_id = await engine.catalog_artist(self.item.title, songs)
            if artist_id is None:
                log.info('artist %s: no catalog artist found', self.item.id)
                self._engine_status.clear()
                self._status = None
                self._update_status()
                return
            answer = await engine.artist_page(artist_id)
        except EngineError as error:
            log.info('artist %s: %s', self.item.id, error)
            self._engine_status.fail(error)
            return
        self._engine_status.clear()
        self._status = None
        self._take(answer)

    def _take(self, answer):
        """Show Engine.artist_page's answer, and fetch the artwork it names."""
        self._answer = answer
        artist = answer.get('artist')
        self._catalog_artist = Item(remote_item(artist)) if isinstance(artist, dict) else None
        latest = answer.get('latest') or {}
        release = latest.get('item') if isinstance(latest, dict) else None
        self._release = Item(remote_item(release)) if isinstance(release, dict) else None
        top = page_shelves([answer.get('topSongs')])
        self._top = top[0] if top else None
        self._shelves = page_shelves(answer.get('shelves'))
        for shelf in self._shelves + ([self._top] if self._top else []):
            shelf.complete = self._completer(answer.get('id'), shelf)
        self._show_hero()
        self._show_sections()
        if self._art_task is not None and not self._art_task.done():
            self._art_task.cancel()
        self._art_task = app().spawn(self._fetch_art())

    async def _fetch_art(self):
        """The portrait, the release's cover, and the thumbnails the shelves name."""
        for item, refresh in ((self._catalog_artist, self._portrait.refresh),
                              (self._release, self.release_cover.refresh)):
            if item is None:
                continue
            if await fetch_thumb(item):
                refresh()
            if await fetch_cover(item):
                refresh()
        await fetch_shelf_art(list(self._shelves) + ([self._top] if self._top else []))

    def _completer(self, artist_id, shelf):
        """See All's coroutine function for a shelf Apple has more of: the whole view
        (Engine.artist_view), the Items already shown kept."""
        if not artist_id or not getattr(shelf, 'more', False):
            return None

        async def complete():
            try:
                dicts = await app().engine.artist_view(artist_id, shelf.key)
            except EngineError as error:
                log.info('artist %s, %s: %s', artist_id, shelf.key, error)
                return
            shown = {item.id: item for item in shelf.items}
            items = [shown.get(data.get('id')) or Item(remote_item(data)) for data in dicts
                     if isinstance(data, dict) and data.get('id') and data.get('kind')]
            if items:
                shelf.more = False
                shelf.update(shelf.title, items)
                await fetch_shelf_art([shelf])
        return complete

    def _show_status(self, status, title, description, button):
        """The status page: the spinner ('loading'), EngineStatus's states, or 'empty' (the
        artist has no albums)."""
        self._status = status
        if status == 'loading':
            self.status_page.set_paintable(Adw.SpinnerPaintable.new(self.status_page))
            title, description, button = _('Loading…'), '', None
        else:
            self.status_page.set_icon_name('media-optical-cd-audio-symbolic')
        self.status_page.set_title(title)
        self.status_page.set_description(description)
        self.status_button.set_label(button or '')
        self.status_button.set_visible(bool(button))
        if status != 'empty':
            self._update_status()
        else:
            self.status_page.set_visible(True)

    def _on_status_clicked(self, _button):
        self._engine_status.activate()

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
        if self._answer is None and not self._asked:
            self._fetch()

    def do_unmap(self):
        self._engine_status.unwatch()
        if self._painted is not None:
            clock, handler = self._painted
            clock.disconnect(handler)
            self._painted = None
        Adw.NavigationPage.do_unmap(self)

    def do_hidden(self):
        # Left (popped, covered, or another destination shown), not just unmapped (a push
        # maps, unmaps and maps a page again): the fetch stops, asked again when shown.
        for task in (self._fetch_task, self._art_task):
            if task is not None and not task.done():
                task.cancel()
        if self._fetching() or self._answer is None:
            self._asked = False
            self._status = None
            self._engine_status.clear()
        self._fetch_task = self._art_task = None
        Adw.NavigationPage.do_hidden(self)

    # Play, the release, the menu and the biography.

    def _on_play_clicked(self, _button):
        play = self._play_target()
        if play is not None:
            self.get_root().play_request(play, shuffle=False)

    def _on_release_clicked(self, _button):
        if self._release is not None:
            self.get_root().open_item(self._release)

    def _on_more_popup(self, button):
        """The artist's menu, made as it opens."""
        actions = getattr(self.get_root(), 'item_actions', None)
        button.set_menu_model(actions.menu_for(self.item) if actions is not None else None)

    def _on_more_notes_clicked(self, _button):
        text = self.summary_label.get_label()
        show_notes(self, self.item.title, text)

    def _on_painted(self, _clock):
        """More under the biography, while it is cut to its three lines."""
        layout = self.summary_label.get_layout()
        cut = self.summary_label.get_mapped() and layout is not None and layout.is_ellipsized()
        if cut != self.more_notes_button.get_visible():
            self.more_notes_button.set_visible(cut)

    async def _fetch_cover(self, item):
        if await fetch_cover(item) and self.item is item:
            self._portrait.refresh()

    def _set_portrait(self, paintable, found):
        # Without a picture, the avatar shows the artist's initials.
        self.avatar.set_custom_image(paintable if found else None)
