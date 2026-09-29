"""AppleMusicShelvesPage: a page of shelves the engine answers with: New (engine.browse()),
Made for You (engine.made_for_you()) and a search category (engine.category(id)).

Unlike Home, whose shelves are the library's, these pages own their Items: the engine's
shelf dicts are wrapped (remote.remote_shelves), their artwork pointed at <cache>/remote-art/
(remote.remote_item) and fetched in the background (remote.fetch_shelf_art), each tile
following its Item as its thumbnail arrives. The page shows a spinner while the answer is on
its way, a status page with the fitting button when the engine cannot answer
(widgets/engine_status.py), and the shelves otherwise; a refresh button in the header bar
asks Apple again past the day-long cache.
"""

import logging
from gettext import gettext as _

from gi.repository import Adw, Gtk

from ..backend.errors import EngineError
from ..remote import fetch_shelf_art, remote_shelves
from ..widgets.engine_status import EngineStatus
from ..widgets.shelf import Shelf
from ..widgets.util import connect_weak
from . import app

log = logging.getLogger(__name__)


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
        self._shelves = []  # the ShelfModel objects shown
        self._widgets = []  # their AppleMusicShelf widgets, in order
        # What the page says when the engine cannot answer, and what its button does.
        self._engine_status = EngineStatus(app(), self._show_status, self.load, {
            'engine-down': _('Start the engine to load this page'),
            'not-signed-in': _('This page appears once you sign in to Apple Music'),
            'failed': _('Could Not Load This Page'),
        })
        self.header_bar.set_show_title(not root)
        self.title_label.set_label(title)
        # Connected weakly (widgets/util.py): a bound method would keep a popped page alive.
        connect_weak(self.refresh_button, 'clicked', self._on_refresh_clicked)
        connect_weak(self.status_button, 'clicked', self._on_status_clicked)

    # The engine outlives the page: it is watched only while the page is shown.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._engine_status.watch()  # may load again: the engine came up meanwhile
        if not self._loaded and (self._task is None or self._task.done()):
            self.load()

    def do_unmap(self):
        self._engine_status.unwatch()
        Adw.NavigationPage.do_unmap(self)

    def load(self, refresh=False):
        """Ask for the shelves (again, past the cache, with `refresh`)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = app().spawn(self._load(refresh))

    async def _load(self, refresh):
        if not self._shelves:
            self._engine_status.loading()
        self.refresh_button.set_sensitive(False)
        try:
            answer = await self._fetch(refresh)
        except EngineError as error:
            log.info('%s: %s', self.get_title(), error)
            if self._shelves:
                app().report(error)  # a refresh that failed keeps what is shown
            else:
                self._engine_status.fail(error)
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
        self._engine_status.clear()
        if shelves:
            self.stack.set_visible_child_name('items')
            self._art_task = app().spawn(fetch_shelf_art(shelves))
        else:
            title, description = self._empty
            self._show_status('empty', title, description, None)

    def _show_status(self, status, title, description, button):
        """The spinner ('loading'), or the status page: EngineStatus's states, or 'empty'."""
        if status == 'loading':
            self.stack.set_visible_child_name('loading')
            return
        self.status_page.set_icon_name(self._icon_name)
        self.status_page.set_title(title)
        self.status_page.set_description(description)
        self.status_button.set_label(button or '')
        self.status_button.set_visible(bool(button))
        self.stack.set_visible_child_name('status')

    def _on_refresh_clicked(self, _button):
        self.load(refresh=True)

    def _on_status_clicked(self, _button):
        self._engine_status.activate()


def category_page(item):
    """A category's page (a tile of the search landing, a banner of New), pushed over the
    page it was opened from: the curator's grouping as shelves."""
    page = ShelvesPage(item.title,
                       lambda refresh: app().engine.category(item.id, refresh=refresh),
                       root=False, icon_name='view-grid-symbolic', hero=False,
                       empty_title=_('Nothing Here'),
                       empty_description=_('This category has nothing to show right now'))
    page.item = item
    return page
