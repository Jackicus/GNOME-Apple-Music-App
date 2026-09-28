"""AppleMusicShelvesPage: a page of shelves the engine answers with: New (engine.browse()),
Made for You (engine.made_for_you()) and a search category (engine.category(id)).

Unlike Home, whose shelves are the library's, these pages own their Items: the engine's
shelf dicts are wrapped here (remote_shelves), their artwork pointed at <cache>/remote-art/
(widgets.artwork.remote_item) and fetched in the background (fetch_shelf_art), each tile
rebound as its thumbnail arrives. The page shows a spinner while the answer is on its way,
a status page with the fitting button when the engine is down or signed out (or the answer
failed), and the shelves otherwise; a refresh button in the header bar asks Apple again past
the day-long cache.
"""

import asyncio
import logging
from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from ..backend.errors import EngineError
from ..library import Item, Shelf as ShelfModel
from ..widgets import artwork
from ..widgets.shelf import Shelf

log = logging.getLogger(__name__)

# Thumbnails fetched at once for a page of shelves (the downloads run in threads).
ART_CONCURRENCY = 6


def remote_shelves(dicts):
    """library.Shelf objects for the engine's shelf dicts ({key, title, items}), the items
    wrapped as Items with their artwork under remote-art; a shelf with nothing in it, or an
    item without an id and a kind, is left out."""
    shelves = []
    for data in dicts or []:
        if not isinstance(data, dict):
            continue
        items = [Item(artwork.remote_item(entry)) for entry in data.get('items') or []
                 if isinstance(entry, dict) and entry.get('id') and entry.get('kind')]
        if items:
            shelves.append(ShelfModel(str(data.get('key') or ''), str(data.get('title') or ''),
                                      items))
    return shelves


async def fetch_shelf_art(shelves):
    """Fetch the thumbnails of the shelves' items that are not on disk, a few at a time, and
    rebind each item's tile as its file arrives (the item spliced over itself in its shelf's
    store, which is how a reload rebinds changed Items too)."""
    loader = artwork.get_default()
    gate = asyncio.Semaphore(ART_CONCURRENCY)

    async def fetch(store, item):
        async with gate:
            if await loader.fetch_thumb(item):
                found, position = store.find(item)
                if found:
                    store.splice(position, 1, [item])

    await asyncio.gather(*(fetch(shelf.items, item) for shelf in shelves
                           for item in list(shelf.items) if artwork.thumb_missing(item)))


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/shelves.ui')
class ShelvesPage(Adw.NavigationPage):
    """ShelvesPage(title, fetch, …): `fetch(refresh)` is a coroutine function answering
    {shelves: [{key, title, items}]} (an Engine method), called when the page is first
    shown and by the refresh button (with refresh=True). `root` false for a page pushed over
    another (a category), which shows its title in the header bar too. `hero` makes the
    first shelf large cards, as Home's. `icon_name` and the empty texts are the status
    page's. The page watches the engine while mapped: an engine-down or signed-out state
    loads itself once the engine is up or signed in.
    """

    __gtype_name__ = 'AppleMusicShelvesPage'

    header_bar = Gtk.Template.Child()
    refresh_button = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    status_page = Gtk.Template.Child()
    status_button = Gtk.Template.Child()
    shelves_box = Gtk.Template.Child()
    title_label = Gtk.Template.Child()

    def __init__(self, title, fetch, root=True, icon_name=None, hero=True, empty_title=None,
                 empty_description=None):
        super().__init__(title=title)
        self.item = None  # a category page's category, for the window's double-push guard
        self._fetch = fetch
        self._hero = hero
        self._icon_name = icon_name or 'view-grid-symbolic'
        self._empty = (empty_title or _('Nothing Here'), empty_description or '')
        self._loaded = False
        self._task = None  # the fetch running
        self._art_task = None  # the thumbnails being fetched for the shelves shown
        self._shelves = []  # the library.Shelf objects shown
        self._widgets = []  # their AppleMusicShelf widgets, in order
        self._status = None  # what the status page says, or None while shelves show
        self._engine_handlers = []
        self.header_bar.set_show_title(not root)
        self.title_label.set_label(title)

    @staticmethod
    def _app():
        return Gio.Application.get_default()

    # The engine outlives the page: it is watched only while the page is shown.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        engine = self._app().engine
        self._engine_handlers = [
            engine.connect('notify::state', self._on_engine_changed),
            engine.connect('notify::authorized', self._on_engine_changed),
        ]
        if not self._loaded and (self._task is None or self._task.done()):
            self.load()

    def do_unmap(self):
        engine = self._app().engine
        for handler in self._engine_handlers:
            engine.disconnect(handler)
        self._engine_handlers = []
        Adw.NavigationPage.do_unmap(self)

    def load(self, refresh=False):
        """Ask for the shelves (again, past the cache, with `refresh`)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = self._app().spawn(self._load(refresh))

    async def _load(self, refresh):
        if not self._shelves:
            self._set_status('loading')
        self.refresh_button.set_sensitive(False)
        try:
            answer = await self._fetch(refresh)
        except EngineError as error:
            log.info('%s: %s', self.get_title(), error)
            if self._shelves:
                self._app().report(error)  # a refresh that failed keeps what is shown
            else:
                self._set_status(error.code, error.message)
            return
        finally:
            self.refresh_button.set_sensitive(True)
        self._loaded = True
        self._show(answer.get('shelves') if isinstance(answer, dict) else [])

    def _show(self, dicts):
        shelves = remote_shelves(dicts)
        if self._art_task is not None and not self._art_task.done():
            self._art_task.cancel()
        for position, shelf in enumerate(shelves):
            if position < len(self._widgets):
                widget = self._widgets[position]
            else:
                widget = Shelf(hero=self._hero and position == 0, see_all=True)
                self.shelves_box.append(widget)
                self._widgets.append(widget)
            widget.bind_shelf(shelf)
        for widget in self._widgets[len(shelves):]:
            self.shelves_box.remove(widget)
        del self._widgets[len(shelves):]
        self._shelves = shelves
        if shelves:
            self._status = None
            self.stack.set_visible_child_name('items')
            self._art_task = self._app().spawn(fetch_shelf_art(shelves))
        else:
            self._set_status('empty')

    def _set_status(self, status, message=''):
        """The status page for `status`: 'loading' (the spinner), an EngineError code with
        a button that helps ('engine-down': Start Engine, unless the engine is starting
        already, when the spinner stays; 'not-signed-in': Sign In; anything else: Try
        Again), or 'empty'."""
        self._status = status
        app = self._app()
        if status == 'loading' or (status == 'engine-down' and app.engine.state == 'starting'):
            self.stack.set_visible_child_name('loading')
            return
        self.status_page.set_icon_name(self._icon_name)
        if status == 'engine-down':
            title = _('Engine Not Running')
            if app.demo:
                description, button = _('Not available with the demo library'), None
            else:
                description, button = _('Start the engine to load this page'), _('Start Engine')
        elif status == 'not-signed-in':
            title = _('Sign In to Load This')
            description = _('This page appears once you sign in to Apple Music')
            button = _('Sign In')
        elif status == 'empty':
            (title, description), button = self._empty, None
        else:
            title, description, button = _('Could Not Load This Page'), message, _('Try Again')
        self.status_page.set_title(title)
        self.status_page.set_description(description)
        self.status_button.set_label(button or '')
        self.status_button.set_visible(bool(button))
        self.stack.set_visible_child_name('status')

    def _on_engine_changed(self, engine, _pspec):
        if self._status == 'engine-down':
            if engine.state == 'up':
                self.load()
            elif engine.state == 'down':
                self._set_status('engine-down')  # a start that failed: the button is back
        elif self._status == 'not-signed-in' and engine.authorized:
            self.load()

    @Gtk.Template.Callback()
    def on_refresh_clicked(self, _button):
        self.load(refresh=True)

    @Gtk.Template.Callback()
    def on_status_clicked(self, _button):
        app = self._app()
        if self._status == 'not-signed-in':
            app.activate_action('sign-in')
        elif self._status == 'engine-down':
            self._set_status('loading')
            app.spawn(self._start_and_load())
        else:
            self.load()

    async def _start_and_load(self):
        app = self._app()
        try:
            await app.engine.start()
        except EngineError as error:
            app.report(error)
            self._set_status(error.code, error.message)
            return
        self.load()


def category_page(item):
    """A category's page (a tile of the search landing, a banner of New), pushed over the
    page it was opened from: the curator's grouping as shelves."""
    app = Gio.Application.get_default()
    page = ShelvesPage(item.title,
                       lambda refresh: app.engine.category(item.id, refresh=refresh),
                       root=False, icon_name='view-grid-symbolic', hero=False,
                       empty_title=_('Nothing Here'),
                       empty_description=_('This category has nothing to show right now'))
    page.item = item
    return page
