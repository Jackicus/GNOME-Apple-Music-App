"""Widget lifetimes (C012): a page pushed and popped, a Shelf removed and a dialog closed are
freed, with the rows they bound; the handlers connected weakly still run; and connect_weak()
itself.

A widget that connects its children's signals to its own bound methods (or declares Blueprint
`=> $handler()` callbacks) is never freed: the cycle runs through C (widgets/util.py). Each
lifetime test builds the widget in a presented window (a stand-in for the app's: an
Adw.NavigationView under a root page), lets it bind its rows, drops it, lets the tasks it
spawned finish, collects, and asserts that the widget, and one of its rows, has been
finalized: a GObject weak reference (GObject.Object.weak_ref()) to each has died. Not a Python
weakref, which with PyGObject 3.56 dies with the Python wrapper, as soon as Python lets go of
it, whether or not the widget itself lives on (widgets/util.py).

The handler tests let go of the Python wrapper and collect before they emit, as the app does
(a pushed page is held by the navigation view alone), so a handler that held its widget
through the first wrapper would be found dead there. The pages and dialogs reach the app
through Gio.Application.get_default() and get_root(), so the window and the application here
are stand-ins that record what they are asked (open_item, play_request, an engine start),
which the handler tests read.
"""

import asyncio
import gc
import os
import tempfile
import time
import unittest
import weakref
from unittest import mock

from tests.gtk import SCHEMA_ID, pump, requires_gtk

from gi.repository import Gio, GObject

from applemusic.backend.errors import EngineError
from applemusic.widgets.util import connect_weak, weak_method


class _Emitter(GObject.Object):
    __gsignals__ = {'ask': (GObject.SignalFlags.RUN_LAST, bool, (int,))}


class _Target:
    def __init__(self):
        self.calls = []

    def on_ask(self, emitter, value, *extra):
        self.calls.append((emitter, value, extra))
        return value > 0


class _GTarget(GObject.Object):
    """A GObject with Python state, as a widget is."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def on_ask(self, _emitter, value, *extra):
        self.calls.append((value, extra))
        return value > 0

    def double(self, value):
        return value * 2


class ConnectWeakTest(unittest.TestCase):
    def test_the_methods_result_is_the_handlers(self):
        emitter, target = _Emitter(), _Target()
        connect_weak(emitter, 'ask', target.on_ask, 'extra')
        self.assertTrue(emitter.emit('ask', 1))
        self.assertFalse(emitter.emit('ask', -1))
        self.assertEqual(target.calls, [(emitter, 1, ('extra',)), (emitter, -1, ('extra',))])

    def test_the_connection_does_not_keep_the_target(self):
        emitter, target = _Emitter(), _Target()
        handler = connect_weak(emitter, 'ask', target.on_ask)
        calls = target.calls
        ref = weakref.ref(target)
        del target
        gc.collect()
        self.assertIsNone(ref())
        self.assertTrue(emitter.handler_is_connected(handler))
        self.assertFalse(emitter.emit('ask', 1))  # nothing to call: the default answer
        self.assertFalse(emitter.handler_is_connected(handler))  # and gone after that
        self.assertEqual(calls, [])

    def test_a_gobject_target_outlives_its_python_wrapper(self):
        # Held by C alone (a list store, as a widget by its parent), the target's wrapper
        # goes and a new one is made on demand: the handler follows the GObject, not the
        # wrapper it was connected with.
        emitter, target = _Emitter(), _GTarget()
        handler = connect_weak(emitter, 'ask', target.on_ask, 'extra')
        call = weak_method(target.double)
        holder = Gio.ListStore(item_type=_GTarget)
        holder.append(target)
        finalized = []
        target.weak_ref(lambda: finalized.append(True))
        del target
        gc.collect()
        self.assertTrue(emitter.emit('ask', 1))
        self.assertEqual(call(4), 8)
        self.assertEqual(holder.get_item(0).calls, [(1, ('extra',))])

        holder.remove_all()
        gc.collect()
        self.assertEqual(finalized, [True])  # the connection kept nothing
        self.assertFalse(emitter.emit('ask', 1))
        self.assertFalse(emitter.handler_is_connected(handler))
        self.assertIsNone(call(4))


# -- the widget tests' stand-ins, made once GTK is known to work ---------------------------

_stand_ins = {}


def _classes():
    if _stand_ins:
        return _stand_ins
    from gi.repository import Adw, Gtk

    class Engine(GObject.Object):
        """What the pages and dialogs ask of app.engine: down, and it says so."""

        state = GObject.Property(type=str, default='down')
        authorized = GObject.Property(type=bool, default=False)
        headless = GObject.Property(type=bool, default=True)

        def __init__(self):
            super().__init__()
            self.calls = []

        async def start(self, visible=None):
            self.calls.append('start')
            self.state = 'up'

        async def stop(self):
            self.calls.append('stop')

        async def restart(self, visible=None):
            self.calls.append('restart')
            await asyncio.get_running_loop().create_future()  # a sign-in that waits

        async def item(self, kind, item_id):
            self.calls.append('item')
            raise EngineError('engine-down')

    class LibrarySync(GObject.Object):
        """The app's sync as the dialogs see it: never running."""

        running = GObject.Property(type=bool, default=False)

        async def cancel(self):
            pass

        def hold(self):
            pass

        def release(self):
            pass

    class App(Adw.Application):
        """Gio.Application.get_default() while the tests run: spawn(), the engine, the
        settings (memory backend), and a record of what the widgets asked."""

        signing_in = GObject.Property(type=bool, default=False)
        signing_out = GObject.Property(type=bool, default=False)

        def account_key(self, name):
            return name  # the release build's keys

        def __init__(self):
            super().__init__(application_id='io.github.jackicus.AppleMusic.LifetimeTest',
                             flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.set_default()  # the pages' app, whichever application another test made first
            self.engine = Engine()
            self.library_sync = LibrarySync()
            self.settings = Gio.Settings.new(SCHEMA_ID)
            self.demo = False
            self.profile = 'default'
            self.tasks = []
            self.actions = []
            self.reported = []
            self.cleared = 0

        def spawn(self, coro):
            task = asyncio.get_running_loop().create_task(coro)
            self.tasks.append(task)
            return task

        def report(self, error):
            self.reported.append(error.code)

        def refuse_in_demo(self):
            return self.demo

        def start_engine(self):
            return self.spawn(self.engine.start())

        def toast(self, _title):
            pass

        def activate_action(self, name, _parameter=None):
            self.actions.append(name)

        async def clear_cache(self):
            self.cleared += 1
            return True

        def start_sync(self):
            pass

    class Window(Adw.Window):
        """The app's window as the pages see it (get_root()): a navigation view whose root
        page holds a box (for a Shelf on its own), and the seams they call, recorded."""

        def __init__(self):
            super().__init__(default_width=800, default_height=600)
            self.navigation_view = Adw.NavigationView()
            self.root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.root_page = Adw.NavigationPage(title='Root', child=self.root_box)
            self.navigation_view.add(self.root_page)
            self.navigation_view.replace([self.root_page])
            self.set_content(self.navigation_view)
            self.opened = []
            self.shelves_opened = []
            self.played = []

        def open_item(self, item):
            self.opened.append(item)

        def open_shelf(self, shelf):
            self.shelves_opened.append(shelf)

        def play_request(self, play, start_with=None, shuffle=False):
            self.played.append((play, start_with, shuffle))

    _stand_ins.update(Engine=Engine, App=App, Window=Window)
    return _stand_ins


def _find(widget, cls):
    """The first widget of class cls in widget's tree (widget included), depth first."""
    if isinstance(widget, cls):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find(child, cls)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def _controllers(widget):
    model = widget.observe_controllers()
    return [model.get_item(position) for position in range(model.get_n_items())]


def _again(ref):
    """The widget a GObject weak reference names, as a new Python wrapper, once the old one
    has been collected: call it with no other reference to that wrapper left (`page =
    _again(page.weak_ref())` keeps one, in `page`, until the call returns; rebind first:
    `page = page.weak_ref()`, then `page = _again(page)`), so that what runs next finds the
    widget as the app's code would, through GTK (see the module)."""
    gc.collect()
    return ref()


def _track(album_id, number):
    return {'id': f'{album_id}.t{number}', 'catalogId': None, 'title': f'Song {number}',
            'artist': 'Invented Artist', 'album': album_id, 'trackNumber': number + 1,
            'discNumber': 1, 'durationMs': 180000, 'durationLabel': '3:00',
            'explicit': False, 'index': number, 'thumb': None}


def _album(number, tracks=3, year=None):
    album_id = f'l.album{number:03d}'
    play = {'kind': 'album', 'id': album_id}
    return {'id': album_id, 'kind': 'album', 'title': f'Album {number:03d}',
            'subtitle': 'Invented Artist', 'year': year or 2000 + number % 20, 'genre': 'Pop',
            'art': None, 'thumb': None, 'countLabel': f'{tracks} songs', 'play': play,
            'groups': [{'name': 'Disc 1', 'play': play,
                        'entries': [_track(album_id, i) for i in range(tracks)]}] if tracks
            else []}


def _artist(albums):
    groups = [{'name': album['title'], 'play': album['play'],
               'entries': album['groups'][0]['entries'] if album['groups'] else []}
              for album in albums]
    return {'id': 'l.artist001', 'kind': 'artist', 'title': 'Invented Artist', 'art': None,
            'thumb': None, 'groups': groups}


@requires_gtk
class WidgetTestCase(unittest.IsolatedAsyncioTestCase):
    """One presented window and one stand-in application for every class; animations off."""

    @classmethod
    def setUpClass(cls):
        from gi.repository import Gtk

        from applemusic.library import Library

        classes = _classes()
        cls.gtk_settings = Gtk.Settings.get_default()
        cls.animations = cls.gtk_settings.props.gtk_enable_animations
        cls.gtk_settings.props.gtk_enable_animations = False
        if 'app' not in classes:  # one per process: its ID is registered on the bus
            classes['app'] = classes['App']()
            classes['app'].register(None)  # Preferences inserts its actions: a registered app's
        cls.app = classes['app']
        cls.library = Library()
        cls.window = classes['Window']()
        cls.window.present()
        deadline = time.monotonic() + 2
        while not cls.window.get_mapped() and time.monotonic() < deadline:
            pump(20)
            time.sleep(0.005)

    @classmethod
    def tearDownClass(cls):
        cls.window.destroy()
        pump()
        cls.gtk_settings.props.gtk_enable_animations = cls.animations
        del cls.window, cls.app, cls.library

    def setUp(self):
        self.window.opened.clear()
        self.window.shelves_opened.clear()
        self.window.played.clear()
        self.app.engine.calls.clear()
        self.app.engine.state = 'down'
        self.app.actions.clear()
        self.app.reported.clear()
        self.app.demo = False

    async def asyncTearDown(self):
        dialog = self.window.get_visible_dialog()
        while dialog is not None:
            dialog.force_close()
            pump(10)
            dialog = self.window.get_visible_dialog()
        self.window.navigation_view.replace([self.window.root_page])
        child = self.window.root_box.get_first_child()
        while child is not None:
            self.window.root_box.remove(child)
            child = self.window.root_box.get_first_child()
        await self.settle()

    async def until(self, predicate, timeout=1.0, interval=0.005):
        """Run GTK's main context and the asyncio loop until predicate() holds (True), or
        timeout seconds have gone (False), asking every interval seconds."""
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            pump(10)
            await asyncio.sleep(interval)
        return True

    async def turn(self):
        """Let what is pending run: GTK's main context, and the tasks up to their next
        wait."""
        for _round in range(5):
            pump(10)
            await asyncio.sleep(0)

    async def settle(self):
        """Let every task spawned so far finish (cancelling one still waiting after half a
        second), and drop them (a finished task's exception holds the frames it went
        through, and their widgets)."""
        for _round in range(5):
            pump(10)
            tasks, self.app.tasks = self.app.tasks, []
            if not tasks:
                await asyncio.sleep(0)
                continue
            await asyncio.wait(tasks, timeout=0.5)
            for task in tasks:
                if not task.done():
                    task.cancel()
            del tasks
        pump(10)

    async def push(self, page, *row_types):
        """Push page and wait for it to be shown (and to have bound a widget of each
        row_type)."""
        self.window.navigation_view.push(page)
        shown = await self.until(lambda: page.get_mapped() and all(
            _find(page, cls) is not None for cls in row_types))
        self.assertTrue(shown, f'{type(page).__name__} not shown')

    async def assert_freed(self, *refs):
        """Assert that the GObject weak references die once the tasks have finished and the
        collector has run. GTK may hold a widget it has just removed until its next frame,
        so this looks again for a while before failing."""
        await self.settle()

        def freed():
            gc.collect()
            return all(ref() is None for ref in refs)

        if not await self.until(freed, timeout=0.5, interval=0.02):
            for ref in refs:
                self.assertIsNone(ref(), f'still alive: {ref()!r}')


class PageLifetimeTest(WidgetTestCase):
    """The widget is made in the call, so that the test holds no reference to it."""

    async def pushed_and_popped(self, page, *row_types):
        """Push page, wait for a widget of each row_type in it, pop it: GObject weak
        references to the page and to those rows."""
        await self.push(page, *row_types)
        refs = [page.weak_ref()] + [_find(page, cls).weak_ref() for cls in row_types]
        self.window.navigation_view.pop()
        self.assertTrue(await self.until(lambda: page.get_parent() is None))
        return refs

    async def added_and_removed(self, widget, row_type):
        """Add widget to the root page, wait for a row_type in it, remove it: GObject weak
        references to the widget and to that row."""
        self.window.root_box.append(widget)
        self.assertTrue(await self.until(lambda: _find(widget, row_type) is not None))
        refs = widget.weak_ref(), _find(widget, row_type).weak_ref()
        self.window.root_box.remove(widget)
        return refs

    async def test_grid_page(self):
        from applemusic.library import Item
        from applemusic.pages.grid import GridPage
        from applemusic.widgets.tile import Tile

        store = Gio.ListStore(item_type=Item)
        store.splice(0, 0, [Item(_album(number)) for number in range(24)])
        refs = await self.pushed_and_popped(
            GridPage(self.library, 'Invented Shelf', store, sorts=('title', 'year'),
                     root=False), Tile)
        await self.assert_freed(*refs)

    async def test_detail_page(self):
        from applemusic.library import Item
        from applemusic.pages.detail import DetailPage
        from applemusic.widgets.track_row import TrackRow

        refs = await self.pushed_and_popped(
            DetailPage(self.library, Item(_album(1, tracks=12))), TrackRow)
        await self.assert_freed(*refs)

    async def test_artist_page(self):
        from applemusic.library import Item
        from applemusic.pages.artist import ArtistPage
        from applemusic.widgets.tile import Tile

        refs = await self.pushed_and_popped(
            ArtistPage(self.library, Item(_artist([_album(n) for n in range(4)]))), Tile)
        await self.assert_freed(*refs)

    async def test_shelves_page(self):
        from applemusic.pages.shelves import ShelvesPage
        from applemusic.widgets.shelf import Shelf
        from applemusic.widgets.tile import Tile

        async def fetch(refresh):
            return {'shelves': [
                {'key': key, 'title': key.title(),
                 'items': [dict(_album(n, tracks=0), id=f'{key}{n}') for n in range(6)]}
                for key in ('first', 'second')]}

        refs = await self.pushed_and_popped(ShelvesPage('Invented Page', fetch, root=False),
                                            Shelf, Tile)
        await self.assert_freed(*refs)

    async def test_shelf_widget(self):
        from applemusic.library import Item, ShelfModel
        from applemusic.widgets.shelf import Shelf
        from applemusic.widgets.tile import Tile

        model = ShelfModel('invented', 'Invented', [Item(_album(n)) for n in range(6)])

        def shelf():
            widget = Shelf(see_all=True)
            widget.bind_shelf(model)
            return widget

        refs = await self.added_and_removed(shelf(), Tile)
        await self.assert_freed(*refs)


class DialogLifetimeTest(WidgetTestCase):
    async def presented_and_closed(self, dialog):
        """Present dialog over the window and close it as Escape would: a GObject weak
        reference to it."""
        closed = []
        dialog.connect('closed', lambda _dialog: closed.append(True))
        dialog.present(self.window)
        self.assertTrue(await self.until(dialog.get_mapped))
        await self.turn()
        dialog.force_close()
        self.assertTrue(await self.until(lambda: closed))
        return dialog.weak_ref()

    async def test_preferences_dialog(self):
        from applemusic.dialogs.preferences import PreferencesDialog

        with tempfile.TemporaryDirectory() as cache, \
                mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': cache}):
            ref = await self.presented_and_closed(PreferencesDialog(self.app))
            await self.assert_freed(ref)

    async def test_sign_in_dialog(self):
        from applemusic.dialogs.signin import SignInDialog

        # Closed as the sign-in waits for the engine: the task is cancelled, the engine
        # stopped.
        ref = await self.presented_and_closed(SignInDialog(self.app))
        await self.assert_freed(ref)
        self.assertEqual(self.app.engine.calls, ['restart', 'stop'])


class HandlerTest(WidgetTestCase):
    """The handlers now connected weakly (they were template callbacks or bound-method
    connects) still do their work, with the widget's first Python wrapper gone."""

    async def shown(self, page, *row_types):
        """Push page, wait for it to show its rows, and answer it again (_again)."""
        await self.push(page, *row_types)
        page = page.weak_ref()
        return _again(page)

    async def presented(self, dialog):
        """Present dialog over the window, and answer it again (_again)."""
        dialog.present(self.window)
        self.assertTrue(await self.until(dialog.get_mapped))
        await self.turn()
        dialog = dialog.weak_ref()
        return _again(dialog)

    async def test_grid_sort_activation_and_title(self):
        from applemusic.library import Item
        from applemusic.pages.grid import GridPage, columns_for
        from applemusic.widgets.tile import Tile

        store = Gio.ListStore(item_type=Item)
        store.splice(0, 0, [Item(_album(number, year=1990 + number)) for number in range(60)])
        page = await self.shown(GridPage(self.library, 'Invented Grid', store,
                                         sorts=('title', 'year'), root=False), Tile)
        view_model = page.grid_view.get_model()
        self.assertEqual(view_model.get_item(0).title, 'Album 000')
        from gi.repository import GLib

        page.activate_action('page.sort', GLib.Variant('s', 'year'))  # newest first
        self.assertEqual(view_model.get_item(0).title, 'Album 059')
        page.activate_action('page.sort', GLib.Variant('s', 'title'))
        self.assertEqual(view_model.get_item(0).title, 'Album 000')

        page.grid_view.emit('activate', 2)
        self.assertEqual(self.window.opened, [view_model.get_item(2)])

        # get-child-position: the grid fits its columns to the width it was laid out at,
        # and the title moves up as the grid scrolls (value-changed).
        page = page.weak_ref()
        page = _again(page)
        self.assertTrue(await self.until(
            lambda: page.grid_view.get_max_columns() == columns_for(page.overlay.get_width())))

        def title_y():
            return page.title_label.compute_bounds(page.overlay)[1].get_y()

        before = title_y()
        page.scrolled_window.get_vadjustment().set_value(30)
        self.assertTrue(await self.until(lambda: title_y() < before))

    async def test_detail_page_buttons_rows_and_tab(self):
        from gi.repository import Gdk, Gtk

        from applemusic.library import Item
        from applemusic.pages.detail import DetailPage
        from applemusic.widgets.track_row import TrackRow

        item = Item(_album(2, tracks=4))
        page = await self.shown(DetailPage(self.library, item), TrackRow)
        page.play_button.emit('clicked')
        page.shuffle_button.emit('clicked')
        page.list_view.emit('activate', 2)  # the hero is row 0: the second track
        track = item.groups[0].entries.get_item(1)
        self.assertEqual(self.window.played, [(item.play, None, False), (item.play, None, True),
                                              (track.play, track.index, False)])

        # Tab from Shuffle goes on into the tracks (the list's capture key controller).
        page.shuffle_button.grab_focus()
        keys = next(controller for controller in _controllers(page.list_view)
                    if isinstance(controller, Gtk.EventControllerKey))
        page = page.weak_ref()
        page = _again(page)
        self.assertTrue(keys.emit('key-pressed', Gdk.KEY_Tab, 0, Gdk.ModifierType(0)))

    def signed_in(self):
        """The account signed in (the engine-down pages offer Start Engine, not Sign In)."""
        self.app.settings.set_boolean('signed-in', True)
        self.addCleanup(self.app.settings.reset, 'signed-in')

    async def test_detail_page_status_button(self):
        from applemusic.library import Item
        from applemusic.pages.detail import DetailPage

        self.signed_in()

        # Fetched, with the engine down: Start Engine, then fetch again.
        page = await self.shown(DetailPage(self.library, Item(_album(3, tracks=0))))
        self.assertTrue(await self.until(lambda: page.status_button.get_visible()))
        page.status_button.emit('clicked')
        await self.settle()
        self.assertEqual(self.app.engine.calls, ['item', 'start', 'item'])

    async def test_artist_page_album_and_status(self):
        from applemusic.library import Item
        from applemusic.pages.artist import ArtistPage
        from applemusic.widgets.tile import Tile

        self.signed_in()

        page = await self.shown(
            ArtistPage(self.library, Item(_artist([_album(n) for n in range(3)]))), Tile)
        page.flow_box.emit('child-activated', page.flow_box.get_child_at_index(1))
        self.assertEqual([album.title for album in self.window.opened], ['Album 001'])
        self.window.navigation_view.pop()

        page = await self.shown(ArtistPage(self.library, Item(_artist([]))))  # engine down
        self.assertTrue(await self.until(lambda: page.status_button.get_visible()))
        page.status_button.emit('clicked')
        await self.settle()
        self.assertEqual(self.app.engine.calls, ['item', 'start', 'item'])

    async def test_shelf_activation_and_see_all(self):
        from applemusic.library import Item, ShelfModel
        from applemusic.widgets.hero_tile import HeroTile
        from applemusic.widgets.shelf import Shelf

        model = ShelfModel('invented', 'Invented', [Item(_album(n)) for n in range(4)])
        shelf = Shelf(hero=True, see_all=True)
        shelf.bind_shelf(model)
        self.window.root_box.append(shelf)
        self.assertTrue(await self.until(lambda: _find(shelf, HeroTile) is not None))
        shelf = shelf.weak_ref()
        shelf = _again(shelf)
        shelf.list_view.emit('activate', 1)
        shelf.see_all_button.emit('clicked')
        self.assertEqual(self.window.opened, [model.items.get_item(1)])
        self.assertEqual(self.window.shelves_opened, [model])

    async def test_shelves_page_refresh_and_status(self):
        from applemusic.pages.shelves import ShelvesPage

        self.signed_in()

        asked = []

        async def fetch(refresh):
            asked.append(refresh)
            if len(asked) == 1:
                raise EngineError('engine-down')
            return {'shelves': []}

        page = await self.shown(ShelvesPage('Invented Page', fetch, root=False))
        self.assertTrue(await self.until(lambda: page.status_button.get_visible()))
        page.status_button.emit('clicked')  # Start Engine, then load
        await self.settle()
        page.refresh_button.emit('clicked')
        await self.settle()
        self.assertEqual(self.app.engine.calls, ['start'])
        self.assertEqual(asked, [False, False, True])

    async def test_preferences_rows(self):
        from applemusic.dialogs.preferences import PreferencesDialog
        from applemusic.sync import INTERVALS

        with tempfile.TemporaryDirectory() as cache, \
                mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': cache}):
            dialog = await self.presented(PreferencesDialog(self.app))
            try:
                dialog.interval_row.set_selected(0)
                self.assertEqual(self.app.settings.get_int('sync-interval'), INTERVALS[0])
                dialog.engine_button.emit('clicked')
                dialog.sign_out_row.emit('activated')
                dialog.clear_button.emit('clicked')  # asks first
                self.assertTrue(await self.until(
                    lambda: self.window.get_visible_dialog() is not dialog))
                alert = _again(self.window.get_visible_dialog().weak_ref())
                alert.emit('response', 'clear')
                del alert
                await self.settle()
                self.assertEqual(self.app.engine.calls, ['start'])
                self.assertEqual(self.app.actions, ['sign-out'])
                self.assertEqual(self.app.cleared, 1)
            finally:
                self.app.settings.reset('sync-interval')
                await self.asyncTearDown()  # the dialogs closed with the cache still patched

    async def test_sign_in_cancel(self):
        from applemusic.dialogs.signin import SignInDialog

        dialog = await self.presented(SignInDialog(self.app))  # the sign-in waits
        closed = []
        dialog.connect('closed', lambda _dialog: closed.append(True))
        dialog.cancel_button.emit('clicked')
        await self.settle()
        self.assertTrue(await self.until(lambda: closed))
        self.assertEqual(self.app.engine.calls, ['restart', 'stop'])


if __name__ == '__main__':
    unittest.main()
