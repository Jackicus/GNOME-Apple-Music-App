"""AppleMusicSearchPage: the Search destination.

Two modes, an Adw.ToggleGroup beside the entry: Apple Music, which searches the catalog
through the engine (suggestions as you type, shelves of results on Enter, Apple's browse
categories before anything is typed), and Your Library, which filters the library's cached
models as you type (Gtk.FilterListModel over the albums, artists, playlists and songs) and
works with the engine down.

Requests are debounced (DEBOUNCE_MS after the last keystroke) and numbered: an answer that
arrives after a newer request was made is dropped. The engine's failures become a status
page with the button that helps (Start Engine, Sign In, Try Again); the page watches the
engine while shown and asks again once it is up or signed in.
"""

import logging
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..backend.errors import EngineError
from ..library import Item, Track, fold
from ..widgets import artwork, context_menu
from ..widgets.category_tile import CategoryTile
from ..widgets.cover import Cover
from ..widgets.shelf import Shelf  # noqa: F401  registers $AppleMusicShelf for the template
from ..widgets.track_row import TrackRow
from .shelves import fetch_shelf_art, remote_shelves

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
    (bind_shelf() and open_shelf() only need `key`, `title` and `items`)."""

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


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/search.ui')
class SearchPage(Adw.NavigationPage):
    __gtype_name__ = 'AppleMusicSearchPage'

    title_label = Gtk.Template.Child()
    search_entry = Gtk.Template.Child()
    mode_toggle = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    status_page = Gtk.Template.Child()
    status_button = Gtk.Template.Child()
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
        self._retry = None  # what to do again once the engine is up or signed in
        self._categories = Gio.ListStore(item_type=Item)
        self._landing_loaded = False
        self._suggestions = Gio.ListStore(item_type=Suggestion)
        self._results = []  # the library.Shelf objects of the last search
        self._result_widgets = []
        self._art_task = None
        self._engine_handlers = []
        self._library_handlers = []
        self._accessible_format = _('{title}, {subtitle}')
        self.title_label.set_label(title)

        self.categories_box.bind_model(self._categories, self._create_category_tile)
        self.suggestions_list.bind_model(self._suggestions, self._create_suggestion_row)

        # Your Library: the filtered models, bound once; the search text is set on the filters.
        self._item_filters = []
        for widget, key, title, store in (
                (self.albums_shelf, 'albums', _('Albums'), library.albums),
                (self.artists_shelf, 'artists', _('Artists'), library.artists),
                (self.playlists_shelf, 'playlists', _('Playlists'), library.playlists)):
            any_filter, filters = _item_filter()
            self._item_filters.extend(filters)
            model = Gtk.FilterListModel(model=store, filter=any_filter)
            model.connect('items-changed', self._on_library_results_changed)
            widget.bind_shelf(LibraryShelf(key, title, model))
        self._song_filter = _song_filter()
        self._songs = Gtk.FilterListModel(model=library.songs, filter=self._song_filter)
        self._songs.connect('items-changed', self._on_library_results_changed)
        self._songs_shown = Gtk.SliceListModel(model=self._songs, offset=0, size=SONG_LIMIT)
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self._on_song_setup)
        factory.connect('bind', self._on_song_bind)
        factory.connect('unbind', self._on_song_unbind)
        self.songs_list.set_factory(factory)
        self.songs_list.set_model(Gtk.NoSelection(model=self._songs_shown))
        context_menu.attach(self.songs_list, drag=True)
        context_menu.attach(self.suggestions_list)  # the top hits' rows

    @staticmethod
    def _app():
        return Gio.Application.get_default()

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
        """Put the cursor in the entry with its text selected (win.search)."""
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    # The engine and the library outlive the page: watched only while it is shown.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        engine = self._app().engine
        self._engine_handlers = [
            engine.connect('notify::state', self._on_engine_changed),
            engine.connect('notify::authorized', self._on_engine_changed),
        ]
        self._library_handlers = [
            self._library.connect('notify::songs-ready', self._on_library_results_changed),
        ]
        if self._stale():
            self._refresh()

    def do_unmap(self):
        engine = self._app().engine
        for handler in self._engine_handlers:
            engine.disconnect(handler)
        self._engine_handlers = []
        for handler in self._library_handlers:
            self._library.disconnect(handler)
        self._library_handlers = []
        Adw.NavigationPage.do_unmap(self)

    def _stale(self):
        """Whether what the page shows should be asked for again as it is shown: nothing
        yet, a failure the engine may have recovered from, or a filter that may have
        missed a library load. Results and suggestions on screen stay (coming back from
        an item's page must not replace them)."""
        shown = self.stack.get_visible_child_name()
        if shown == 'loading' or self._status in ('engine-down', 'not-signed-in', 'api',
                                                   'timeout', 'usage'):
            return True
        if self.mode == 'library':
            return bool(self.text)
        return not self.text and not self._landing_loaded

    # Typing.

    @Gtk.Template.Callback()
    def on_entry_changed(self, _entry):
        if self._setting_text:
            return
        if self._debounce is not None:
            GLib.source_remove(self._debounce)
        self._debounce = GLib.timeout_add(DEBOUNCE_MS, self._on_typing_paused)

    def _on_typing_paused(self):
        self._debounce = None
        self._refresh()
        return GLib.SOURCE_REMOVE

    @Gtk.Template.Callback()
    def on_entry_activated(self, _entry):
        """Enter: the full search (Apple Music); the filter is live already (Your Library)."""
        if self._debounce is not None:
            GLib.source_remove(self._debounce)
            self._debounce = None
        if self.mode == 'library':
            self._refresh()
        elif self.text:
            self._search(self.text)

    @Gtk.Template.Callback()
    def on_stop_search(self, entry):
        entry.set_text('')

    @Gtk.Template.Callback()
    def on_mode_changed(self, _toggle, _pspec):
        self.search_entry.set_placeholder_text(
            _('Search Your Library') if self.mode == 'library' else _('Search Apple Music'))
        self._refresh()

    def _refresh(self):
        """Show what the mode and the text call for: the landing, suggestions or results
        (Apple Music), or the filtered library."""
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

    def _show_landing(self):
        if self._landing_loaded:
            self._show('landing')
            return
        self._retry = self._show_landing
        serial = self._next_serial()
        if self._categories.get_n_items() == 0:
            self._show('loading')
        self._app().spawn(self._load_landing(serial))

    async def _load_landing(self, serial):
        try:
            answer = await self._app().engine.landing()
        except EngineError as error:
            if serial == self._serial:
                self._fail(error, self._show_landing)
            return
        if serial != self._serial:
            return
        categories = [Item(artwork.remote_item(data))
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
        self._app().spawn(self._load_suggestions(text, serial))
        if self.stack.get_visible_child_name() not in ('suggestions', 'results'):
            self._show('suggestions' if self._suggestions.get_n_items() else 'loading')

    async def _load_suggestions(self, text, serial):
        try:
            answer = await self._app().engine.suggest(text)
        except EngineError as error:
            if serial == self._serial:
                self._fail(error, lambda: self._suggest(text))
            return
        if serial != self._serial:
            return  # typed on since: a newer request answers
        rows = [Suggestion(term=term.get('term'), display=term.get('display'))
                for term in answer.get('terms') or [] if term.get('term')]
        rows += [Suggestion(item=Item(artwork.remote_item(data)))
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
        self._app().spawn(self._load_results(text, serial))

    async def _load_results(self, text, serial):
        try:
            answer = await self._app().engine.search(text)
        except EngineError as error:
            if serial == self._serial:
                self._fail(error, lambda: self._search(text))
            return
        if serial != self._serial:
            return
        self._show_results(answer.get('shelves') or [])

    def _show_results(self, dicts):
        shelves = remote_shelves(dicts)
        if self._art_task is not None and not self._art_task.done():
            self._art_task.cancel()
        for position, shelf in enumerate(shelves):
            if position < len(self._result_widgets):
                widget = self._result_widgets[position]
            else:
                widget = Shelf(see_all=True)
                self.results_box.append(widget)
                self._result_widgets.append(widget)
            widget.set_property('hero', shelf.key == 'top')
            widget.bind_shelf(shelf)
        for widget in self._result_widgets[len(shelves):]:
            self.results_box.remove(widget)
        del self._result_widgets[len(shelves):]
        self._results = shelves
        if shelves:
            self._show('results')
            self._art_task = self._app().spawn(fetch_shelf_art(shelves))
        else:
            self._show_status('no-results')

    def _fail(self, error, retry):
        log.info('search: %s', error)
        self._retry = retry
        self._show_status(error.code, getattr(error, 'message', ''))

    # Your Library: the filtered models.

    def _filter_library(self, text):
        if text and not self._library.songs_ready:
            self._app().spawn(self._library.build_songs())
        for string_filter in self._item_filters:
            string_filter.set_search(text)
        self._song_filter.set_search(fold(text))
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
        self.stack.set_visible_child_name(name)

    def _show_status(self, status, message=''):
        """The status page for `status`: an EngineError code with the button that helps
        ('engine-down': Start Engine, unless the engine is starting already; 'not-signed-in':
        Sign In; another failure: Try Again), or one of the page's own: 'no-results',
        'no-suggestions', 'empty-landing', 'library-empty' (Your Library before typing)."""
        self._status = status
        app = self._app()
        if status == 'engine-down' and app.engine.state == 'starting':
            self.stack.set_visible_child_name('loading')
            return
        icon, button = self._icon_name, None
        if status == 'engine-down':
            title = _('Engine Not Running')
            if app.demo:
                description = _('Not available with the demo library')
            else:
                description = _('Start the engine to search Apple Music')
                button = _('Start Engine')
        elif status == 'not-signed-in':
            title = _('Sign In to Search Apple Music')
            description = _('Apple Music’s catalogue is searched once you sign in')
            button = _('Sign In')
        elif status == 'no-results':
            icon = 'edit-find-symbolic'
            title, description = _('No Results Found'), _('Try a different search')
        elif status == 'no-suggestions':
            icon = 'edit-find-symbolic'
            title, description = _('No Suggestions'), _('Press Enter to search anyway')
        elif status == 'empty-landing':
            title, description = _('Search Apple Music'), _('Type to search the catalogue')
        elif status == 'library-empty':
            title = _('Search Your Library')
            description = _('Albums, artists, playlists and songs in your library')
        else:
            title, description, button = _('Search Failed'), message, _('Try Again')
        self.status_page.set_icon_name(icon)
        self.status_page.set_title(title)
        self.status_page.set_description(description)
        self.status_button.set_label(button or '')
        self.status_button.set_visible(bool(button))
        self.stack.set_visible_child_name('status')

    def _on_engine_changed(self, engine, _pspec):
        if self.mode != 'music' or self._retry is None:
            return
        if self._status == 'engine-down':
            if engine.state == 'up':
                self._retry()
            elif engine.state == 'down':
                self._show_status('engine-down')  # a start that failed: the button is back
        elif self._status == 'not-signed-in' and engine.authorized:
            self._retry()

    @Gtk.Template.Callback()
    def on_status_clicked(self, _button):
        app = self._app()
        if self._status == 'not-signed-in':
            app.activate_action('sign-in')
        elif self._status == 'engine-down':
            self._show('loading')
            app.spawn(self._start_engine())
        elif self._retry is not None:
            self._retry()

    async def _start_engine(self):
        app = self._app()
        try:
            await app.engine.start()
        except EngineError as error:
            app.report(error)
            self._show_status(error.code, error.message)
            return
        if self._retry is not None:
            self._retry()

    # The landing's tiles.

    def _create_category_tile(self, item):
        tile = CategoryTile()
        tile.bind(item)
        child = Gtk.FlowBoxChild(child=tile)
        label = (self._accessible_format.format(title=item.title, subtitle=item.subtitle)
                 if item.subtitle else item.title)
        child.update_property([Gtk.AccessibleProperty.LABEL], [label])
        return child

    @Gtk.Template.Callback()
    def on_category_activated(self, _flow_box, child):
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
            if artwork.thumb_missing(item):
                self._app().spawn(self._fetch_row_art(item, cover))
        return row

    async def _fetch_row_art(self, item, cover):
        if await artwork.get_default().fetch_thumb(item):
            cover.refresh()

    @Gtk.Template.Callback()
    def on_suggestion_activated(self, _list_box, row):
        suggestion = self._suggestions.get_item(row.get_index())
        if suggestion is None:
            return
        if suggestion.item is not None:
            self.get_root().open_item(suggestion.item)
            return
        # A term: searched for as if typed and entered (the entry's own change is ignored).
        if self._debounce is not None:
            GLib.source_remove(self._debounce)
            self._debounce = None
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
        list_item.set_accessible_label(
            self._accessible_format.format(title=track.title, subtitle=track.artist)
            if track.artist else track.title)

    def _on_song_unbind(self, _factory, list_item):
        list_item.get_child().unbind()

    @Gtk.Template.Callback()
    def on_song_activated(self, _list_view, position):
        track = self._songs_shown.get_item(position)
        if isinstance(track, Track):
            self.get_root().play_request(track.play, start_with=track.index)

    @Gtk.Template.Callback()
    def on_songs_see_all_clicked(self, _button):
        self.get_root().open_songs(self.text)
