# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""What the page tests share: stand-ins for the application, the engine and the window the
pages reach (pages.app() and get_root()), invented library data, and PageTestCase, a presented
window over an asyncio loop on GLib's (as the app runs).

The engine stand-in answers each request from `engine.answers[name]`: a value, an exception
to raise, or a function of the request's arguments (a coroutine function is awaited, so a
test can hold an answer back); a request without an answer raises engine-down, as the demo
engine does. `engine.calls` lists the requests in order. The application records what it is
asked (actions, reports, toasts) and runs spawn()ed coroutines as tasks that settle() waits for.

The application here is not registered and has an ID of its own, so the lifetime tests'
(test_page_lifetime.py) can live beside it; stand_in_app() makes it the default application,
which is how a page finds it. Any widget test that builds a page calls that, PageTestCase and
test_tiles alike.
"""

import asyncio
import copy
import inspect
import time
import unittest

from tests.gtk import SCHEMA_ID, pump, requires_gtk

from gi.repository import Gio, GObject

from applemusic.backend.errors import EngineError

_stand_ins = {}


def classes():
    """The stand-in classes, made once GTK is known to work: Engine, App, Window."""
    if _stand_ins:
        return _stand_ins
    from gi.repository import Adw, Gtk

    class Engine(GObject.Object):
        state = GObject.Property(type=str, default='down')
        authorized = GObject.Property(type=bool, default=False)
        headless = GObject.Property(type=bool, default=True)

        def __init__(self):
            super().__init__()
            self.calls = []
            self.answers = {}
            self.start_error = None  # an EngineError a start raises

        async def _answer(self, name, *args):
            self.calls.append(name)
            answer = self.answers.get(name, EngineError('engine-down'))
            if callable(answer) and not isinstance(answer, BaseException):
                answer = answer(*args)
                if inspect.isawaitable(answer):
                    answer = await answer
            if isinstance(answer, BaseException):
                raise answer
            return copy.deepcopy(answer)

        async def start(self, visible=None):
            self.calls.append('start')
            self.state = 'starting'
            await asyncio.sleep(0)
            if self.start_error is not None:
                self.state = 'down'
                raise self.start_error
            self.state = 'up'

        async def item(self, kind, item_id):
            return await self._answer('item', kind, item_id)

        async def suggest(self, text):
            return await self._answer('suggest', text)

        async def search(self, text):
            return await self._answer('search', text)

        async def landing(self, refresh=False):
            return await self._answer('landing')

        async def browse(self, refresh=False):
            return await self._answer('browse', refresh)

        async def made_for_you(self, refresh=False):
            return await self._answer('made_for_you', refresh)

        async def category(self, category_id, refresh=False):
            return await self._answer('category', category_id, refresh)

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id='io.github.jackicus.MusicSleeve.PageTest',
                             flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.engine = Engine()
            self.settings = Gio.Settings.new(SCHEMA_ID)
            self.demo = False
            self.tasks = []
            self.actions = []
            self.reported = []
            self.toasts = []

        def reset(self):
            self.engine = Engine()
            self.demo = False
            self.tasks = []
            self.actions = []
            self.reported = []
            self.toasts = []
            self.settings.set_boolean('signed-in', True)

        def account_key(self, name):
            return name

        def spawn(self, coro):
            task = asyncio.get_running_loop().create_task(coro)
            self.tasks.append(task)
            return task

        def report(self, error):
            self.reported.append(error.code)

        def toast(self, title, *_args):
            self.toasts.append(title)

        def refuse_in_demo(self):
            return self.demo

        def activate_action(self, name, _parameter=None):
            self.actions.append(name)

        def start_engine(self):
            if self.demo:
                return None

            async def start():
                try:
                    await self.engine.start()
                except EngineError as error:
                    self.report(error)

            return self.spawn(start())

    class ItemActions:
        def __init__(self):
            self.asked = []

        def menu_for(self, obj):
            self.asked.append(obj)
            menu = Gio.Menu()
            menu.append('Invented Action', 'win.invented')
            return menu

    class Window(Adw.Window):
        def __init__(self):
            super().__init__(default_width=1000, default_height=700)
            self.navigation_view = Adw.NavigationView()
            self.root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.root_page = Adw.NavigationPage(title='Root', child=self.root_box)
            self.navigation_view.add(self.root_page)
            self.navigation_view.replace([self.root_page])
            self.set_content(self.navigation_view)
            self.item_actions = ItemActions()
            self.reset()

        def reset(self):
            self.opened = []
            self.shelves_opened = []
            self.played = []
            self.songs_opened = []

        def content_width(self):
            return self.navigation_view.get_width()

        def open_item(self, item):
            self.opened.append(item)

        def open_shelf(self, shelf):
            self.shelves_opened.append(shelf)

        def open_songs(self, text=''):
            self.songs_opened.append(text)

        def play_request(self, play, start_with=None, shuffle=None):
            self.played.append((play, start_with, shuffle))

    _stand_ins.update(Engine=Engine, App=App, Window=Window)
    return _stand_ins


def stand_in_app():
    """The stand-in application, made once and made the default one, which is how a page
    reaches it (pages.app() is Gio.Application.get_default()). Any widget test that builds a
    page calls this: a module that leans on the default application another module left behind
    passes in the suite and fails on its own."""
    stand_ins = classes()
    if 'app' not in stand_ins:
        stand_ins['app'] = stand_ins['App']()
    stand_ins['app'].set_default()
    return stand_ins['app']


def find(widget, cls):
    """The first widget of class cls in widget's tree (widget included), depth first."""
    if isinstance(widget, cls):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = find(child, cls)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def find_all(widget, cls):
    """Every widget of class cls in widget's tree, depth first."""
    found = [widget] if isinstance(widget, cls) else []
    child = widget.get_first_child()
    while child is not None:
        found.extend(find_all(child, cls))
        child = child.get_next_sibling()
    return found


# -- invented data -----------------------------------------------------------------------

def track(album_id, number, disc=1, title=None):
    return {'id': f'{album_id}.t{disc}.{number}', 'catalogId': None,
            'title': title or f'Song {number}', 'artist': 'Invented Artist', 'album': album_id,
            'trackNumber': number + 1, 'discNumber': disc, 'durationMs': 180000,
            'durationLabel': '3:00', 'explicit': False, 'index': number, 'thumb': None}


def album(number, tracks=3, year=None, discs=1, kind='album'):
    album_id = f'l.{kind}{number:03d}'
    play = {'kind': kind, 'id': album_id}
    groups = [{'name': '', 'play': play,
               'entries': [track(album_id, i, disc) for i in range(tracks)]}
              for disc in range(1, discs + 1)] if tracks else []
    return {'id': album_id, 'kind': kind, 'title': f'Invented {kind.title()} {number:03d}',
            'subtitle': 'Invented Artist', 'year': year or 2000 + number % 20,
            'genre': 'Pop', 'art': None, 'thumb': None, 'play': play, 'groups': groups}


def artist(albums, artist_id='l.artist001'):
    groups = [{'name': entry['title'], 'play': entry['play'],
               'entries': entry['groups'][0]['entries'] if entry['groups'] else []}
              for entry in albums]
    return {'id': artist_id, 'kind': 'artist', 'title': 'Invented Artist', 'art': None,
            'thumb': None, 'groups': groups, 'play': {}}


@requires_gtk
class PageTestCase(unittest.IsolatedAsyncioTestCase):
    """A presented stand-in window, the stand-in application as the default one, animations
    off; each test starts with a fresh engine, signed in, not in demo mode."""

    @classmethod
    def setUpClass(cls):
        from gi.repository import Gtk

        cls.app = stand_in_app()
        cls.gtk_settings = Gtk.Settings.get_default()
        cls.animations = cls.gtk_settings.props.gtk_enable_animations
        cls.gtk_settings.props.gtk_enable_animations = False
        cls.window = classes()['Window']()
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
        del cls.window

    def setUp(self):
        from applemusic.library import Library

        self.app.set_default()  # the pages' app (pages.app()), whichever was made first
        self.app.reset()
        self.window.reset()
        self.library = Library()
        self._roots = []  # the pages show_root() added to the navigation view

    async def asyncTearDown(self):
        self.window.navigation_view.replace([self.window.root_page])
        for page in self._roots:
            self.window.navigation_view.remove(page)
        child = self.window.root_box.get_first_child()
        while child is not None:
            self.window.root_box.remove(child)
            child = self.window.root_box.get_first_child()
        await self.settle()
        self.app.settings.reset('signed-in')

    async def until(self, predicate, timeout=1.0, interval=0.005):
        """Run GTK's main context and the asyncio loop until predicate() holds (True), or
        timeout seconds have gone (False)."""
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            pump(10)
            await asyncio.sleep(interval)
        return True

    async def turn(self, rounds=5):
        """Let what is pending run: GTK's main context, and the tasks up to their next wait."""
        for _round in range(rounds):
            pump(10)
            await asyncio.sleep(0)

    async def settle(self):
        """Let every task spawned so far finish (cancelling one still waiting after half a
        second)."""
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
        pump(10)

    async def push(self, page):
        """Push page and wait for it to be shown."""
        self.window.navigation_view.push(page)
        self.assertTrue(await self.until(page.get_mapped), f'{type(page).__name__} not shown')
        return page

    async def show_root(self, page):
        """Show page as the navigation view's root, as the window shows a destination's."""
        self.window.navigation_view.add(page)
        self._roots.append(page)
        self.window.navigation_view.replace([page])
        self.assertTrue(await self.until(page.get_mapped), f'{type(page).__name__} not shown')
        return page
