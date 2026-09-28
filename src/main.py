"""The application: what the app is made of, its app.* actions and its lifecycle.

    app = Application(version, app_id, base_id, profile, demo_dir)   # the launcher's main()
    app.run(sys.argv)

do_startup makes the parts (a picture of them is in CLAUDE.md): the Library, whose first
load() starts reading library.json at once; the Engine (Chrome, stopped); the Player over it;
Mpris; LibrarySync (sync.py), which runs the syncs. do_activate builds the Window (imported
only then) and starts the engine when the settings ask. What happens elsewhere: the sync in
sync.py, signing out and clearing the cache in account.py, background playback in
background.py, the startup marks in timing.py, the Keyboard Shortcuts dialog in
dialogs/shortcuts.py.

Every coroutine the app starts goes through spawn(); every message for the user through
toast(), and every EngineError through report(). Quitting (app.quit, or closing the window
while nothing plays in the background) hides the window, stops the engine within
QUIT_TIMEOUT, and only then quits. --demo shows an invented library without the engine
(_use_demo).
"""

import asyncio
import logging
import os
import signal
import sys
import warnings
from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from .backend import config, normalize  # noqa: E402
from .backend.errors import EngineError  # noqa: E402
from .background import BackgroundPlayback  # noqa: E402
from .engine import Engine  # noqa: E402
from .library import Library  # noqa: E402
from .mpris import Mpris  # noqa: E402
from .player import Player  # noqa: E402
from .shortcuts import ACCELS  # noqa: E402
from .sync import LibrarySync, install_scaler  # noqa: E402
from .timing import PROCESS_START, StartupMarks  # noqa: E402, F401  (bench.py reads it here)

log = logging.getLogger(__name__)

RESOURCE_PATH = '/io/github/jackicus/AppleMusic'

# How long quitting waits for the engine to stop (its own SIGTERM grace is 5 s) before Chrome is
# killed outright and the app quits anyway.
QUIT_TIMEOUT = 6.0


class Application(Adw.Application):
    """The app. `demo_dir` is where --demo finds its invented library: the development
    launcher passes the source tree's build/demo, where scripts/demo_library.py writes it; a
    release launcher passes '' and --demo reads build/demo under the working directory.
    `signing-out` is true while account.sign_out() runs: the account's actions are off."""

    signing_out = GObject.Property(type=bool, default=False)

    def __init__(self, version, app_id, base_id, profile, demo_dir=None):
        super().__init__(
            application_id=app_id,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            resource_base_path=RESOURCE_PATH,
        )
        self.version = version
        self.profile = profile
        # The .Devel build's Chrome profile and cache sit beside the release build's.
        config.set_build_profile(profile)
        self.demo_dir = demo_dir
        self.demo = False  # True under --demo: an invented library and no engine
        self.library = None  # created in do_startup
        self.engine = None  # created in do_startup
        self.player = None  # created in do_startup, after the engine
        self.mpris = None  # created in do_startup, after the player; released in do_shutdown
        self.library_sync = None  # created in do_startup
        self.background = None  # created in do_startup: the window closed, the music on
        self._tasks = set()  # strong references: asyncio only keeps weak ones
        self._quitting = None  # the task stopping the engine before the app quits
        self._signin = None  # the sign-in dialog while it is open
        self._preferences = None  # the Preferences dialog while it is open
        self._first_load = None  # the library's first load(), reading since do_startup
        # Startup timing (timing.py): name -> GLib.get_monotonic_time(), for scripts/bench.py.
        self._timing = StartupMarks(self.get_active_window)
        self.marks = self._timing.marks
        # One schema for every profile, so a Devel build shares the release's settings.
        self.settings = Gio.Settings.new(base_id)

        self._add_action('quit', self._on_quit)
        self._add_action('about', self._on_about)
        self._add_action('shortcuts', self._on_shortcuts)
        self._add_action('preferences', lambda *_args: self.show_preferences())
        self._account_actions = [
            self._add_action('sign-in', self._on_sign_in),
            self._add_action('sign-out', self._on_sign_out),
        ]
        self.connect('notify::signing-out', self._on_signing_out)
        self._add_action('sync', lambda *_args: self.start_sync())
        self._add_action('now-playing', self._on_now_playing)
        # Playback, enabled while something plays (the bar's buttons follow). Their keys
        # (Space, Ctrl+Right, Ctrl+Left) are the window's (shortcuts.PLAYBACK), not
        # accelerators, which GTK 4 would fire before a focused entry gets them.
        self._playback_actions = [
            self._add_action('play-pause', lambda *_: self.player_command(self.player.toggle())),
            self._add_action('next', lambda *_: self.player_command(self.player.next())),
            self._add_action('previous', lambda *_: self.player_command(self.player.previous())),
            self._add_action('shuffle',
                             lambda *_: self.player_command(self.player.toggle_shuffle())),
            self._add_action('repeat',
                             lambda *_: self.player_command(self.player.cycle_repeat())),
        ]
        for action in self._playback_actions:
            action.set_enabled(False)
        # Every accelerator, app.* and win.* alike (shortcuts.py, which the Keyboard
        # Shortcuts dialog lists).
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, list(accels))

        self.add_main_option('debug', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _('Log debug messages'), None)
        self.add_main_option('demo', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _('Show an invented library instead of yours, without the engine'),
                             None)

    def do_handle_local_options(self, options):
        if options.contains('debug'):
            logging.getLogger().setLevel(logging.DEBUG)
        if options.contains('demo'):
            self._use_demo()
        return -1  # carry on with the default handling

    def _use_demo(self):
        """Point the backend's cache at the demo library, before anything reads it.

        An APPLE_MUSIC_CACHE already set wins, so a bigger generated library can be shown the
        same way. The demo runs as an instance of its own rather than raising a running app.
        """
        self.demo = True
        self.set_flags(self.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
        if not os.environ.get('APPLE_MUSIC_CACHE'):
            demo_dir = self.demo_dir or os.path.join('build', 'demo')
            os.environ['APPLE_MUSIC_CACHE'] = os.path.abspath(demo_dir)
        log.info('Demo mode: the library in %s', config.cache_dir())

    def do_startup(self):
        self.mark('startup')
        # The library is read and parsed from now on, in a thread, while GTK starts and the
        # window is mapped (C, mostly: the parse gets the GIL); do_activate awaits it.
        self.library = Library()
        self._first_load = self.library.load()
        Adw.Application.do_startup(self)
        self.mark('gtk-started')
        # What follows, to the window's present(), is Python, which would share the GIL
        # with the parse: it waits meanwhile (Library.hold_reading()).
        self.library.hold_reading()
        self.engine = self._make_engine()
        self.player = Player(self)
        self.player.connect('notify::track', self._on_track_changed)
        self.player.connect('error', self._on_playback_error)
        self.library_sync = LibrarySync(self)
        self.background = BackgroundPlayback(
            self.player, hold=self.hold, release=self.release,
            quit=lambda: self.activate_action('quit'))
        self.mpris = Mpris(self)  # the media controls and keys, as this app
        self.mpris.start()
        install_scaler()  # thumbnails scaled from covers on disk, by GdkPixbuf
        # A terminal's Ctrl+C or a kill still stops Chrome: the launcher left SIGINT at its
        # default, which would end the process at once, before the engine is stopped.
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal, signum)

    def do_shutdown(self):
        if self._first_load is not None:  # never activated: the read is dropped
            self.library.resume_reading()
            self._first_load.close()
            self._first_load = None
        if self.mpris is not None:
            self.mpris.stop()  # the name released before the bus connection goes
        if self.engine is not None and self.engine.pid:
            self.engine.kill()  # a quit that never stopped it: Chrome must not outlive the app
        Adw.Application.do_shutdown(self)

    def _make_engine(self):
        """The Engine on this build's Chrome profile (config.profile_dir()), with the browser
        command and the preferred mode from the settings, then and whenever they change
        (Preferences): both apply when Chrome next starts."""
        if self.demo:
            return Engine(demo=True)
        engine = Engine(browser_command=self.settings.get_string('browser-command'))
        engine.prefer_headless = self.settings.get_boolean('engine-headless')
        self.settings.connect(
            'changed::browser-command',
            lambda settings, key: setattr(engine, 'browser_command', settings.get_string(key)))
        self.settings.connect(
            'changed::engine-headless',
            lambda settings, key: setattr(engine, 'prefer_headless', settings.get_boolean(key)))
        return engine

    def _on_signal(self, signum):
        log.info('signal %d: quitting', signum)
        self.activate_action('quit')
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            from .window import Window

            self.mark('activate')
            self.spawn(self._load_library())
            window = Window(application=self)
            self.mark('window-built')
            if self.profile == 'development':
                window.add_css_class('devel')
            window.connect('realize', lambda _window: self.mark('window-realized'))
            window.connect('map', self._timing.on_window_mapped)
            self.spawn(self._log_event_loop())
            if self._autostart_wanted():
                self.spawn(self._autostart())
        elif (self.engine.state == 'up' and self.engine.authorized
              and self.library_sync.due()):
            self.start_sync()  # launched again: a sync when the last is old
        self.library.resume_reading()  # present() waits for the compositor
        window.present()

    def mark(self, name, painted=None):
        """A startup mark (timing.StartupMarks.mark): 'window-mapped', 'library-ready',
        'albums-bound'…, with `painted` a mark for the end of the frame being drawn.
        scripts/bench.py reads `marks`; --debug logs each."""
        self._timing.mark(name, painted)

    async def _load_library(self):
        load, self._first_load = self._first_load, None
        await (load or self.library.load())
        if self.library.state == 'ready':
            self.mark('library-ready')
        if not self.demo:
            # What only grows (remote art, lyrics, day-old answers, crash leftovers), trimmed
            # once the window is up; each sync trims it too.
            await asyncio.to_thread(normalize.prune_caches, str(config.cache_dir()))

    # -- the engine ----------------------------------------------------------------------

    def _autostart_wanted(self):
        return (not self.demo and self.settings.get_boolean('signed-in')
                and self.settings.get_boolean('engine-autostart'))

    async def _autostart(self):
        """Chrome headless on launch, as the settings ask, and a word when the sign-in it
        expected is gone."""
        try:
            await self.engine.start()
        except EngineError as error:
            self.report(error)
            return
        if not self.engine.authorized:
            log.warning('the engine is up but Apple Music is not signed in')
            self.toast(_('Apple Music is no longer signed in'), _('Sign In'), 'app.sign-in')
            return
        if self.library_sync.due():
            self.start_sync()
        if not self.settings.get_string('account-name'):
            # Sign-in may have missed the name (the page renders it late); try again now.
            try:
                name = await self.engine.account_name(wait=10)
            except EngineError as error:
                log.debug('account name after autostart: %s', error)
                return
            if name:
                self.settings.set_string('account-name', name)
                log.info('account name read from the page after autostart')

    # -- playback ------------------------------------------------------------------------

    def _on_track_changed(self, player, _pspec):
        playing = player.track is not None
        for action in self._playback_actions:
            action.set_enabled(playing)

    def _on_playback_error(self, _player, message):
        self.toast(_('Playback failed: {message}').format(message=message))

    def player_command(self, coro):
        """A Player command as a task, its EngineError toasted."""
        async def command():
            try:
                await coro
            except EngineError as error:
                self.report(error)
        return self.spawn(command())

    def _on_now_playing(self, *_args):
        window = self.get_active_window()
        if window is not None and hasattr(window, 'toggle_now_playing'):
            window.toggle_now_playing()

    # -- the library and the account --------------------------------------------------------

    def start_sync(self):
        """Sync the library (LibrarySync.start()): the task, or None."""
        return self.library_sync.start()

    async def clear_cache(self):
        """Clear the cache and sync again (account.clear_cache()); False in demo mode."""
        from . import account

        return await account.clear_cache(self)

    def _on_sign_in(self, *_args):
        if self.demo:
            self.toast(_('Not available with the demo library'))
            return
        if self._signin is not None:
            return  # the dialog is open already
        from .dialogs.signin import SignInDialog

        dialog = SignInDialog(self)
        self._signin = dialog
        dialog.connect('closed', self._on_signin_closed)
        dialog.present(self.get_active_window())

    def _on_signin_closed(self, _dialog):
        self._signin = None

    def _on_sign_out(self, *_args):
        if self.demo:
            return
        dialog = Adw.AlertDialog(
            heading=_('Sign Out of Apple Music?'),
            body=_('The engine stops, and your library, artwork and sign-in cached on this '
                   'computer are removed.'),
        )
        dialog.add_response('cancel', _('_Cancel'))
        dialog.add_response('sign-out', _('Sign _Out'))
        dialog.set_response_appearance('sign-out', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', self._on_sign_out_response)
        parent = self._preferences  # over Preferences, when it asks from there
        dialog.present(parent if parent is not None else self.get_active_window())

    def _on_signing_out(self, *_args):
        for action in self._account_actions:
            action.set_enabled(not self.signing_out)

    def _on_sign_out_response(self, _dialog, response):
        if response == 'sign-out':
            from . import account

            self.spawn(account.sign_out(self))

    def show_preferences(self, page=None):
        """Present the Preferences dialog (app.preferences), on `page` ('general',
        'engine') when given; the one open already if it is. Returns the dialog."""
        dialog = self._preferences
        if dialog is None:
            from .dialogs.preferences import PreferencesDialog

            dialog = PreferencesDialog(self)
            self._preferences = dialog
            dialog.connect('closed', self._on_preferences_closed)
            dialog.present(self.get_active_window())
        if page:
            dialog.set_visible_page_name(page)
        return dialog

    def _on_preferences_closed(self, _dialog):
        self._preferences = None

    # -- messages ------------------------------------------------------------------------

    def toast(self, title, button_label=None, action_name=None):
        """A toast on the active window, or in Preferences while that is open over it, with
        a button running an action when given."""
        target = self._preferences
        if target is None:
            target = self.get_active_window()
        if target is None:
            return
        toast = Adw.Toast(title=title)
        if button_label and action_name:
            toast.set_button_label(button_label)
            toast.set_action_name(action_name)
        target.add_toast(toast)

    def report(self, error):
        """An EngineError as a toast: a sentence for its code, never a traceback."""
        log.warning('engine: %s', error)
        code = getattr(error, 'code', 'api')
        if code == 'engine-down':
            self.toast(_('The engine is not running'))
        elif code == 'not-signed-in':
            self.toast(_('Sign in to Apple Music first'), _('Sign In'), 'app.sign-in')
        elif code == 'timeout':
            self.toast(_('Apple Music did not answer in time'))
        else:
            self.toast(_('Apple Music could not do that: {message}').format(
                message=getattr(error, 'message', error)))

    # -- background playback and quitting ------------------------------------------------

    def close_window(self, window):
        """The window's close request (Window.do_close_request). With background playback
        on and something playing, the window hides and the app is held while the music
        plays on (background.py): MPRIS Raise or launching the app again shows it.
        Otherwise the app quits, which stops the engine."""
        if (self._quitting is None and self.settings.get_boolean('background-playback')
                and self.player is not None and self.player.active):
            window.hide_for_background()
            self.background.enter(window)
        else:
            self.activate_action('quit')

    @property
    def in_background(self):
        """Whether the window is closed while the music plays on (the app held)."""
        return self.background is not None and self.background.active

    def _on_quit(self, *_args):
        """Stop the engine, then quit. Closing the last window comes here too (close_window,
        unless the music plays on in the background), as do MPRIS Quit and the end of
        background playback. Chrome must not outlive the app: this is the clean way; if the
        app dies otherwise, setpriv's parent-death signal (engine.with_pdeathsig) and the
        DevTools pipe closing end it, and do_shutdown kills one a quit left running."""
        if self._quitting is None:
            self._quitting = self.spawn(self._quit())

    async def _quit(self):
        if self.background is not None:
            self.background.leave()
        for window in self.get_windows():
            if hasattr(window, 'prepare_quit'):  # a dialog's toplevel has none
                window.prepare_quit()  # remembers its state and hides at once
        if self.library_sync is not None:
            self.spawn(self.library_sync.cancel())  # its downloads give up at the next one
        try:
            await asyncio.wait_for(self.engine.stop(), QUIT_TIMEOUT)
        except TimeoutError:
            log.warning('the engine took longer than %g s to stop', QUIT_TIMEOUT)
            self.engine.kill()
        except Exception:
            log.exception('stopping the engine failed')
            self.engine.kill()
        finally:
            Gio.Application.quit(self)

    # -- tasks, actions, dialogs ---------------------------------------------------------

    def spawn(self, coro):
        """Run a coroutine as a task on the GLib-backed asyncio loop.

        The task is referenced until it finishes, and an exception it raises is
        logged rather than lost. Returns the task, so callers can cancel it.
        """
        task = asyncio.get_event_loop().create_task(coro, name=getattr(coro, '__qualname__', None))
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error('Task %s failed', task.get_name(), exc_info=task.exception())

    async def _log_event_loop(self):
        await asyncio.sleep(0)
        log.debug('asyncio: %s', type(asyncio.get_running_loop()).__name__)

    def _add_action(self, name, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect('activate', callback)
        self.add_action(action)
        return action

    def _on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=_('Apple Music'),
            application_icon=self.get_application_id(),
            developer_name='Jack Tully',
            version=self.version,
            website='https://github.com/Jackicus/GNOME-Apple-Music-App',
            issue_url='https://github.com/Jackicus/GNOME-Apple-Music-App/issues',
            license_type=Gtk.License.GPL_2_0,
            copyright='© 2026 Jack Tully',
            comments=_('Not affiliated with Apple. Apple Music is a trademark of Apple Inc.'),
        )
        about.present(self.get_active_window())

    def _on_shortcuts(self, *_args):
        from .dialogs import shortcuts

        shortcuts.present(self.get_active_window())


def use_glib_event_loop():
    """Make asyncio run on the GLib main loop, so coroutines and GTK share one thread.

    Python 3.14 deprecates event loop policies (removal in 3.16), but this is
    still how PyGObject hooks asyncio into GLib; if that changes, only this does.
    """
    warnings.filterwarnings('ignore', r"'asyncio\.\w*policy\w*' is deprecated", DeprecationWarning)
    from gi.events import GLibEventLoopPolicy

    asyncio.set_event_loop_policy(GLibEventLoopPolicy())


def main(version, app_id, base_id, profile, demo_dir=None):
    debug = os.environ.get('APPLE_MUSIC_DEBUG', '') not in ('', '0')
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format='%(levelname)s %(name)s: %(message)s')
    use_glib_event_loop()
    app = Application(version, app_id, base_id, profile, demo_dir)
    return app.run(sys.argv)
