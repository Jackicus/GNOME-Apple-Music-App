# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicSearchPage: the Search destination.

Two modes, an Adw.ToggleGroup beside the entry: Apple Music, which searches the catalog
through the engine (suggestions as you type, shelves of results on Enter, Apple's browse
categories before anything is typed), and Your Library, which filters the library's cached
models as you type (Gtk.FilterListModel over the albums, artists, playlists and songs) and
works with the engine down.

Requests are debounced (DEBOUNCE_MS after the last keystroke) and numbered: an answer that
arrives after a newer request was made, or after a switch to Your Library, is dropped. The
engine's failures become a status page with the button that helps (widgets/engine_status.py:
Start Engine, Sign In, Try Again) and a way into Your Library; the page watches the engine
while shown and asks again once it is up or signed in. Status titles and a search's results
are announced to assistive technology.
"""

import logging
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..backend.errors import EngineError
from ..library import Item, Track, fold
from ..remote import fetch_shelf_art, fetch_thumb, needs_thumb, remote_item, remote_shelves
from ..widgets import context_menu
from ..widgets.category_tile import CategoryTile
from ..widgets.cover import Cover
from ..widgets.engine_status import EngineStatus
from ..widgets.shelf import ShelfColumn  # also registers $AppleMusicShelf for the template
from ..widgets.labels import accessible_label, flow_child, track_label
from ..widgets.track_row import TrackRow
from ..widgets.util import MappedHandlers, connect_weak, weak_method
from . import app

log = logging.getLogger(__name__)

DEBOUNCE_MS = 250
SONG_LIMIT = 25   # songs listed in Your Library results; See All opens the Songs page
MODES = ('music', 'library')


def kind_names():
    """What a top hit's row calls its kind, translated on call."""
    return {
        'album': _('Album'), 'artist': _('Artist'), 'playlist': _('Playlist'),
        'song': _('Song'), 'station': _('Station'), 'video': _('Music Video'),
        'category': _('Category'),
    }


class Suggestion(GObject.Object):
    """A row of the suggestions list: a term Apple would complete the typed one to (`term`,
    shown as `display`), or one of its top hits for it (`item`, an Item)."""

    __gtype_name__ = 'AppleMusicSuggestion'

    def __init__(self, term=None, display=None, item=None):
        super().__init__()
        self.term = term
        self.display = display or term
        self.item = item


class LibraryShelf:
    """What a shelf widget binds in Your Library mode: a title over a filtered library store
    (bind_shelf() and open_shelf() only need `key`, `title` and `items`; the row shows the
    first shelf.ROW_LIMIT of them, See All the rest)."""

    def __init__(self, key, title, items):
        self.key = key
        self.title = title
        self.items = items


def _item_filter():
    """A filter matching an Item whose title or subtitle holds the search, case ignored:
    an Adw.AnyFilter of one Gtk.StringFilter per property (its `set_search` is set on both)."""
    filters = [Gtk.StringFilter(expression=Gtk.PropertyExpression.new(Item, None, name),
                                match_mode=Gtk.StringFilterMatchMode.SUBSTRING, ignore_case=True)
               for name in ('title', 'subtitle')]
    any_filter = Gtk.AnyFilter()
    for string_filter in filters:
        any_filter.append(string_filter)
    return any_filter, filters


def _song_filter():
    """One Gtk.StringFilter over a Track's folded search key (title, artist and album, as the
    Songs page matches them; the search is folded too, so the filter need not fold): one
    pass with one Python call per song, rather than three filters over three properties."""
    expression = Gtk.ClosureExpression.new(GObject.TYPE_STRING,
                                           lambda track: track.search_key, None)
    return Gtk.StringFilter(expression=expression,
                            match_mode=Gtk.StringFilterMatchMode.SUBSTRING, ignore_case=False)


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/search.ui')
class SearchPage(Adw.NavigationPage):
    """The Search destination (see the module). Every change of mode or text numbers a new
    request, so an answer that arrives after it (a suggestion, a search, the landing, or its
    failure) is dropped whatever the page shows now, Your Library included. Sign-out drops the
    page (window.forget_account_pages()): its handlers are connected weakly."""

    __gtype_name__ = 'AppleMusicSearchPage'

    title_label = Gtk.Template.Child()
    search_row = Gtk.Template.Child()
    search_entry = Gtk.Template.Child()
    mode_toggle = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    status_page = Gtk.Template.Child()
    status_button = Gtk.Template.Child()
    status_library_button = Gtk.Template.Child()
    categories_box = Gtk.Template.Child()
    suggestions_list = Gtk.Template.Child()
    results_box = Gtk.Template.Child()
    albums_shelf = Gtk.Template.Child()
    artists_shelf = Gtk.Template.Child()
    playlists_shelf = Gtk.Template.Child()
    songs_box = Gtk.Template.Child()
    songs_count_label = Gtk.Template.Child()
    songs_see_all_button = Gtk.Template.Child()
    songs_list = Gtk.Template.Child()

    def __init__(self, library, title, icon_name=None):
        super().__init__(title=title)
        self._library = library
        self._icon_name = icon_name or 'system-search-symbolic'
        self._serial = 0  # the number of the latest request; older answers are dropped
        self._debounce = None  # the GLib source waiting for the typing to pause
        self._setting_text = False  # the page is putting a term in the entry, not the user
        self._status = None  # what the status page says, or None
        self._announced = None  # the status title last announced
        self._retry = None  # what to do again once the engine is up or signed in
        self._focus_on_map = False  # focus_entry() asked while the page was not shown
        self._categories = Gio.ListStore(item_type=Item)
        self._landing_loaded = False
        self._suggestions = Gio.ListStore(item_type=Suggestion)
        self._column = ShelfColumn(self.results_box)  # a search's answer, as shelves
        self._art_task = None
        # What the page says when the engine cannot answer, and what its button does; its
        # retry asks again for what failed, in Apple Music mode only.
        texts = {
            'engine-down': _('Start the engine to search Apple Music'),
            'not-signed-in': (_('Sign In to Search Apple Music'),
                              _('Apple Music’s catalogue is searched once you sign in')),
            'failed': _('Search Failed'),
        }
        self._engine_status = EngineStatus(app(), self._show_engine_status,
                                           self._retry_request, texts)
        self.title_label.set_label(title)

        # What a child holds calls the page weakly (widgets/util.py): sign-out drops the
        # page, and a bound method would keep it alive.
        connect_weak(self.search_entry, 'changed', self._on_entry_changed)
        connect_weak(self.search_entry, 'activate', self._on_entry_activated)
        connect_weak(self.search_entry, 'stop-search', self._on_stop_search)
        connect_weak(self.mode_toggle, 'notify::active-name', self._on_mode_changed)
        connect_weak(self.status_button, 'clicked', self._on_status_clicked)
        connect_weak(self.status_library_button, 'clicked', self._on_search_library_clicked)
        connect_weak(self.categories_box, 'child-activated', self._on_category_activated)
        connect_weak(self.suggestions_list, 'row-activated', self._on_suggestion_activated)
        connect_weak(self.songs_list, 'activate', self._on_song_activated)
        connect_weak(self.songs_see_all_button, 'clicked', self._on_songs_see_all_clicked)
        self.categories_box.bind_model(self._categories, weak_method(self._create_category_tile))
        self.suggestions_list.bind_model(self._suggestions,
                                         weak_method(self._create_suggestion_row))

        # Your Library: the filtered models, bound once; the search text is set on the filters.
        # Each is over its store only while there is a text: with none, a filter matches
        # everything, and the stack measures the hidden results even so, so the rows would
        # build their tiles for nothing (the search page cost 250 ms to open).
        self._item_filters = []
        self._library_models = []  # (Gtk.FilterListModel, the store it filters)
        for widget, key, title, store in (
                (self.albums_shelf, 'albums', _('Albums'), library.albums),
                (self.artists_shelf, 'artists', _('Artists'), library.artists),
                (self.playlists_shelf, 'playlists', _('Playlists'), library.playlists)):
            any_filter, filters = _item_filter()
            self._item_filters.extend(filters)
            model = Gtk.FilterListModel(model=None, filter=any_filter)
            connect_weak(model, 'items-changed', self._on_library_results_changed)
            self._library_models.append((model, store))
            widget.bind_shelf(LibraryShelf(key, title, model))
        self._song_filter = _song_filter()
        self._songs = Gtk.FilterListModel(model=None, filter=self._song_filter)
        connect_weak(self._songs, 'items-changed', self._on_library_results_changed)
        self._library_models.append((self._songs, library.songs))
        self._songs_shown = Gtk.SliceListModel(model=self._songs, offset=0, size=SONG_LIMIT)
        factory = Gtk.SignalListItemFactory()
        connect_weak(factory, 'setup', self._on_song_setup)
        connect_weak(factory, 'bind', self._on_song_bind)
        connect_weak(factory, 'unbind', self._on_song_unbind)
        self.songs_list.set_factory(factory)
        self.songs_list.set_model(Gtk.NoSelection(model=self._songs_shown))
        context_menu.attach(self.songs_list, drag=True)
        context_menu.attach(self.suggestions_list)  # the top hits' rows
        # The library outlives the window: followed only while the page is shown.
        self._handlers = MappedHandlers(self)
        self._handlers.add(library, 'notify::state', self._on_library_results_changed)
        self._handlers.add(library, 'notify::songs-ready', self._on_library_results_changed)

    @property
    def mode(self):
        return self.mode_toggle.get_active_name() or MODES[0]

    def set_mode(self, mode):
        """'music' or 'library' (the toggle; a change refreshes what is shown)."""
        self.mode_toggle.set_active_name(mode)

    @property
    def text(self):
        return ' '.join(self.search_entry.get_text().split())

    def focus_entry(self):
        """Put the cursor in the entry with its text selected (win.search): now if the page
        is shown, else as it is shown."""
        if not self.get_mapped():
            self._focus_on_map = True
            return
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    # The engine and the library outlive the page: watched only while it is shown.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        if self._stale():
            self._refresh()
        self._engine_status.watch()
        if self._focus_on_map:
            self._focus_on_map = False
            self.focus_entry()

    def do_unmap(self):
        self._engine_status.unwatch()
        Adw.NavigationPage.do_unmap(self)

    def _stale(self):
        """Whether what the page shows should be asked for again as it is shown: nothing
        yet, a failure the engine may have recovered from, or a filter that may have
        missed a library load. Results and suggestions on screen stay (coming back from
        an item's page must not replace them)."""
        shown = self.stack.get_visible_child_name()
        if shown == 'loading' or self._engine_status.status in ('engine-down', 'not-signed-in',
                                                                'failed'):
            return True
        if self.mode == 'library':
            return bool(self.text)
        return not self.text and not self._landing_loaded

    # Typing.

    def _on_entry_changed(self, _entry):
        if self._setting_text:
            return
        self._cancel_debounce()
        self._debounce = GLib.timeout_add(DEBOUNCE_MS, weak_method(self._on_typing_paused))

    def _cancel_debounce(self):
        if self._debounce is not None:
            GLib.source_remove(self._debounce)
            self._debounce = None

    def _on_typing_paused(self):
        self._debounce = None
        self._refresh()
        return GLib.SOURCE_REMOVE

    def _on_entry_activated(self, _entry):
        """Enter: the full search (Apple Music); what the text calls for otherwise (the
        library filtered, which is live already; the landing, for no text)."""
        self._cancel_debounce()
        if self.mode == 'library' or not self.text:
            self._refresh()
        else:
            self._search(self.text)

    def _on_stop_search(self, entry):
        entry.set_text('')

    def _on_mode_changed(self, _toggle, _pspec):
        self.search_entry.set_placeholder_text(
            _('Search Your Library') if self.mode == 'library' else _('Search Apple Music'))
        self._cancel_debounce()
        self._refresh()

    def _refresh(self):
        """Show what the mode and the text call for: the landing, suggestions or results
        (Apple Music), or the filtered library. Whatever was asked before is dropped."""
        self._next_serial()
        self._retry = None
        text = self.text
        if self.mode == 'library':
            self._filter_library(text)
        elif not text:
            self._show_landing()
        else:
            self._suggest(text)

    # Apple Music: the landing, suggestions and results, through the engine.

    def _next_serial(self):
        self._serial += 1
        return self._serial

    def _current(self, serial):
        """Whether an answer to request `serial` is still wanted: nothing newer was asked
        and the page is in Apple Music mode."""
        return serial == self._serial and self.mode == 'music'

    def _show_landing(self):
        if self._landing_loaded:
            self._show('landing')
            return
        self._retry = self._show_landing
        serial = self._next_serial()
        self._show('loading')
        app().spawn(self._load_landing(serial))

    async def _load_landing(self, serial):
        try:
            answer = await app().engine.landing()
        except EngineError as error:
            if self._current(serial):
                self._fail(error, self._show_landing)
            return
        if not self._current(serial):
            return
        categories = [Item(remote_item(data))
                      for data in answer.get('categories') or []
                      if isinstance(data, dict) and data.get('id')]
        self._categories.splice(0, self._categories.get_n_items(), categories)
        self._landing_loaded = True
        if categories:
            self._show('landing')
        else:
            self._show_status('empty-landing')

    def _suggest(self, text):
        self._retry = lambda: self._suggest(text)
        serial = self._next_serial()
        app().spawn(self._load_suggestions(text, serial))
        if self.stack.get_visible_child_name() not in ('suggestions', 'results'):
            self._show('suggestions' if self._suggestions.get_n_items() else 'loading')

    async def _load_suggestions(self, text, serial):
        try:
            answer = await app().engine.suggest(text)
        except EngineError as error:
            if self._current(serial):
                self._fail(error, lambda: self._suggest(text))
            return
        if not self._current(serial):
            return  # typed on since, or switched to Your Library
        rows = [Suggestion(term=term.get('term'), display=term.get('display'))
                for term in answer.get('terms') or [] if term.get('term')]
        rows += [Suggestion(item=Item(remote_item(data)))
                 for data in answer.get('items') or [] if isinstance(data, dict)]
        self._suggestions.splice(0, self._suggestions.get_n_items(), rows)
        if rows:
            self._show('suggestions')
        else:
            self._show_status('no-suggestions')

    def _search(self, text):
        """Search Apple Music for `text` (Enter, or a suggested term)."""
        self._retry = lambda: self._search(text)
        serial = self._next_serial()
        self._show('loading')
        app().spawn(self._load_results(text, serial))

    async def _load_results(self, text, serial):
        try:
            answer = await app().engine.search(text)
        except EngineError as error:
            if self._current(serial):
                self._fail(error, lambda: self._search(text))
            return
        if not self._current(serial):
            return
        self._show_results(answer.get('shelves') or [])

    def _show_results(self, dicts):
        shelves = remote_shelves(dicts)
        if self._art_task is not None and not self._art_task.done():
            self._art_task.cancel()
        # Top Results, Apple's best few hits of any kind, first and as cards.
        self._column.show(shelves, hero_first=bool(shelves) and shelves[0].key == 'top')
        if shelves:
            self._show('results')
            self._art_task = app().spawn(fetch_shelf_art(shelves))
            count = len(shelves)
            # Translators: said to a screen reader when a search's results show: how many
            # shelves of them (Top Results, Artists, Albums…) there are.
            text = ngettext('{count} shelf of results', '{count} shelves of results', count)
            self._announce(text.format(count=count))
        else:
            self._show_status('no-results')

    def _fail(self, error, retry):
        log.info('search: %s', error)
        self._retry = retry
        self._engine_status.fail(error)

    def _retry_request(self):
        """EngineStatus's retry: ask again for what failed (Apple Music mode only)."""
        if self.mode == 'music' and self._retry is not None:
            self._retry()

    # Your Library: the filtered models.

    def _filter_library(self, text):
        if text and not self._library.songs_ready:
            app().spawn(self._library.build_songs())
        for string_filter in self._item_filters:
            string_filter.set_search(text)
        self._song_filter.set_search(fold(text))
        for model, store in self._library_models:
            wanted = store if text else None
            if model.get_model() is not wanted:
                model.set_model(wanted)
        self._update_library_state()

    def _on_library_results_changed(self, *_args):
        if self.mode == 'library' and self.get_mapped():
            self._update_library_state()

    def _update_library_state(self):
        if not self.text:
            self._show_status('library-empty')
            return
        counts = []
        for widget in (self.albums_shelf, self.artists_shelf, self.playlists_shelf):
            count = widget.shelf.items.get_n_items()
            widget.set_visible(count > 0)
            counts.append(count)
        songs = self._songs.get_n_items()
        self.songs_box.set_visible(songs > 0)
        self.songs_count_label.set_label(
            ngettext('{count} song', '{count} songs', songs).format(count=f'{songs:n}'))
        self.songs_see_all_button.set_visible(songs > SONG_LIMIT)
        if songs or any(counts):
            self._show('library')
        elif self._library.state == 'loading' or (
                not self._library.songs_ready and self._library.song_count()):
            self._show('loading')
        else:
            self._show_status('no-results')

    # The stack.

    def _show(self, name):
        self._status = None
        self._announced = None
        self._engine_status.clear()
        self.stack.set_visible_child_name(name)

    def _show_status(self, status):
        """The status page for one of the page's own states: 'no-results', 'no-suggestions',
        'empty-landing', 'library-empty' (Your Library before typing)."""
        self._engine_status.clear()
        icon = self._icon_name
        if status == 'no-results':
            icon = 'edit-find-symbolic'
            title, description = _('No Results Found'), _('Try a different search')
        elif status == 'no-suggestions':
            icon = 'edit-find-symbolic'
            title, description = _('No Suggestions'), _('Press Enter to search anyway')
        elif status == 'empty-landing':
            title, description = _('Search Apple Music'), _('Type to search the catalogue')
        else:
            title = _('Search Your Library')
            description = _('Albums, artists, playlists and songs in your library')
        self._draw_status(status, icon, title, description, None)

    def _show_engine_status(self, status, title, description, button):
        """EngineStatus's show: the spinner, or why Apple Music cannot answer (with a way
        into Your Library, which works regardless)."""
        if status == 'loading':
            self._status = None
            self._announced = None
            self.stack.set_visible_child_name('loading')
            return
        self._draw_status(status, self._icon_name, title, description, button)

    def _draw_status(self, status, icon, title, description, button):
        self._status = status
        self.status_page.set_icon_name(icon)
        self.status_page.set_title(title)
        self.status_page.set_description(description)
        self.status_button.set_label(button or '')
        self.status_button.set_visible(bool(button))
        self.status_library_button.set_visible(status in ('engine-down', 'not-signed-in',
                                                          'demo'))
        self.stack.set_visible_child_name('status')
        if title != self._announced:  # once, not at every keystroke that keeps it
            self._announced = title
            self._announce(title)

    def _announce(self, text):
        """Tell assistive technology what the page shows now, through the window (GTK drops
        an announcement from a widget no client has asked about yet)."""
        root = self.get_root()
        if root is not None and self.get_mapped():
            root.announce(text, Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def _on_status_clicked(self, _button):
        self._engine_status.activate()

    def _on_search_library_clicked(self, _button):
        self.set_mode('library')
        self.focus_entry()

    # The landing's tiles.

    def _create_category_tile(self, item):
        tile = CategoryTile()
        tile.bind(item)
        return flow_child(tile, accessible_label(item))

    def _on_category_activated(self, _flow_box, child):
        item = self._categories.get_item(child.get_index())
        if item is not None:
            self.get_root().open_item(item)

    # The suggestions' rows.

    def _create_suggestion_row(self, suggestion):
        row = Adw.ActionRow(activatable=True)
        if suggestion.item is None:
            row.set_title(GLib.markup_escape_text(suggestion.display))
            row.add_prefix(Gtk.Image(icon_name='edit-find-symbolic',
                                     accessible_role=Gtk.AccessibleRole.PRESENTATION))
        else:
            item = suggestion.item
            row.set_title(GLib.markup_escape_text(item.title))
            kind = kind_names().get(item.kind, '')
            subtitle = ' · '.join(part for part in (kind, item.subtitle) if part)
            row.set_subtitle(GLib.markup_escape_text(subtitle))
            cover = Cover(size=40, valign=Gtk.Align.CENTER)
            cover.add_css_class('small')
            cover.set_paths(item.thumb, item.art)
            row.add_prefix(cover)
            row.context_item = item  # its context menu (widgets/context_menu.py)
            if needs_thumb(item):
                app().spawn(_fetch_row_art(item, cover))
        return row

    def _on_suggestion_activated(self, _list_box, row):
        suggestion = self._suggestions.get_item(row.get_index())
        if suggestion is None:
            return
        if suggestion.item is not None:
            self.get_root().open_item(suggestion.item)
            return
        # A term: searched for as if typed and entered (the entry's own change is ignored).
        self._cancel_debounce()
        self._setting_text = True
        try:
            self.search_entry.set_text(suggestion.term)
            self.search_entry.set_position(-1)
        finally:
            self._setting_text = False
        self._search(suggestion.term)

    # Your Library's songs.

    def _on_song_setup(self, _factory, list_item):
        list_item.set_child(TrackRow())

    def _on_song_bind(self, _factory, list_item):
        track = list_item.get_item()
        list_item.get_child().bind(track)
        list_item.set_accessible_label(track_label(track, show_album=False))

    def _on_song_unbind(self, _factory, list_item):
        list_item.get_child().unbind()

    def _on_song_activated(self, _list_view, position):
        track = self._songs_shown.get_item(position)
        if isinstance(track, Track):
            self.get_root().play_request(track.play, start_with=track.index,
                                         start_id=track.id)

    def _on_songs_see_all_clicked(self, _button):
        self.get_root().open_songs(self.text)


async def _fetch_row_art(item, cover):
    if await fetch_thumb(item):
        cover.refresh()
