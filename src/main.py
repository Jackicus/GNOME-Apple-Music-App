# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The application: what the app is made of, its app.* actions and its lifecycle.

    app = Application(version, app_id, base_id, profile, demo_dir)   # the launcher's main()
    app.run(sys.argv)

do_startup makes the parts (a picture of them is in CLAUDE.md): the Library, whose first
load() starts reading library.json at once; the Engine (Chrome, stopped); the Player over it;
LibrarySync (sync.py), which runs the syncs and schedules them; BackgroundPlayback; Mpris.
do_activate builds the Window (imported only then) and starts the engine when the settings
ask. What happens elsewhere: the sync in sync.py, signing in and out and clearing the cache
in account.py, background playback in background.py, the words for each error in errors.py,
the startup marks in timing.py, the About and Keyboard Shortcuts dialogs in dialogs/.

Every coroutine the app starts goes through spawn(); every message for the user through
toast(), and every EngineError through report(). The account's settings are read through
account_key() (the development build has its own). Quitting (app.quit, or closing the window
while nothing plays in the background) hides the window, stops the sync and the engine
within QUIT_TIMEOUT, and only then quits. --demo shows an invented library without the
engine, with settings of its own (_use_demo).
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

from gi.repository import Adw, Gio, GLib, GObject  # noqa: E402

from .backend import config, normalize  # noqa: E402
from .backend.errors import EngineError  # noqa: E402
from .background import BackgroundPlayback  # noqa: E402
from .engine import Engine  # noqa: E402
from .errors import error_message  # noqa: E402
from .library import Library  # noqa: E402
from .mpris import Mpris  # noqa: E402
from .player import Player  # noqa: E402
from .shortcuts import ACCELS  # noqa: E402
from .sync import LibrarySync, install_scaler  # noqa: E402
from .timing import PROCESS_START, StartupMarks  # noqa: E402, F401  (bench.py reads it here)

log = logging.getLogger(__name__)

RESOURCE_PATH = '/io/github/jackicus/MusicSleeve'

# How long quitting waits for the sync and the engine to stop before Chrome is killed outright
# and the app quits anyway; the engine's stop, Browser.close (up to 2 s) then SIGTERM, is
# given QUIT_GRACE after the SIGTERM (5 s otherwise) so that it fits.
QUIT_TIMEOUT = 6.0
QUIT_GRACE = 3.0

# The notification that stands in for a toast while the window is closed and the music plays.
BACKGROUND_NOTIFICATION = 'background-error'

# The settings that belong to a Chrome profile's sign-in and its library: the development
# build, which has a profile (and a cache) of its own, keeps them under keys of its own
# (account_key()).
ACCOUNT_KEYS = ('signed-in', 'account-name', 'last-sync', 'last-page', 'expanded-folders')

# How long quitting waits, after the engine's stop, for a sign-out under way to wipe the
# profile and the cache and forget the account.
SIGN_OUT_WAIT = 10.0


class Application(Adw.Application):
    """The app. `demo_dir` is where --demo finds its invented library: the development
    launcher passes the source tree's build/demo, where scripts/demo_library.py writes it; a
    release launcher passes '', and --demo then needs APPLE_MUSIC_CACHE.
    `signing-in` is true while the sign-in flow runs (account.sign_in(), to its end after the
    dialog has closed), `signing-out` while account.sign_out() runs: the account's actions and
    Refresh Library are off meanwhile."""

    signing_in = GObject.Property(type=bool, default=False)
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
        self._engine_start = None  # a start the app asked for (app.start-engine)
        self._autostart_task = None  # the engine's start at launch
        self._sign_out_task = None  # account.sign_out() while it runs
        self.revocation = None  # sign-out's revocation of the Apple session (account.py)
        self.sign_in_finish = None  # what follows a sign-in's commit (account.py)
        self._preferences = None  # the Preferences dialog while it is open
        self._first_load = None  # the library's first load(), reading since do_startup
        # Startup timing (timing.py): name -> GLib.get_monotonic_time(), for scripts/bench.py.
        self._timing = StartupMarks(self.get_active_window)
        self.marks = self._timing.marks
        # One schema for every profile; the sign-in's keys are each build's (account_key()).
        self.settings = Gio.Settings.new(base_id)

        self._add_action('quit', self._on_quit)
        self._add_action('about', self._on_about)
        self._add_action('shortcuts', self._on_shortcuts)
        self._add_action('preferences', lambda *_args: self.show_preferences())
        self._account_actions = [
            self._add_action('sign-in', self._on_sign_in),
            self._add_action('sign-out', self._on_sign_out),
        ]
        self.connect('notify::signing-in', self._on_account_changing)
        self.connect('notify::signing-out', self._on_account_changing)
        # Refresh Library: enabled while signed in, with no sync running (_update_sync_action).
        self._sync_action = self._add_action('sync', lambda *_args: self.start_sync())
        self._add_action('now-playing', self._on_now_playing)
        self._add_action('start-engine', lambda *_args: self.start_engine())
        self._add_action('show-engine-preferences',
                         lambda *_args: self.show_preferences('engine'))
        # Playback, enabled while something plays (the bar's buttons follow). Their keys
        # (Space, Ctrl+Right, Ctrl+Left) are the window's (shortcuts.PLAYBACK), not
        # accelerators, which GTK 4 would fire before a focused entry gets them.
        self._playback_actions = [
            self._add_action('play-pause', lambda *_: self.player_command(self.player.toggle())),
            self._add_action('next', lambda *_: self.player_command(self.player.next())),
            self._add_action('previous', lambda *_: self.player_command(self.player.previous())),
        ]
        for action in self._playback_actions:
            action.set_enabled(False)
        # Every accelerator, app.* and win.* alike (shortcuts.py, which the Keyboard
        # Shortcuts dialog lists).
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, list(accels))

        self.add_main_option('debug', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _('Log debug messages'), None)
        # --demo is for developers: listed in --help by the development build only.
        hidden = GLib.OptionFlags.NONE if profile == 'development' else GLib.OptionFlags.HIDDEN
        self.add_main_option('demo', 0, hidden, GLib.OptionArg.NONE,
                             _('Show an invented library instead of yours, without the engine'),
                             None)

    def do_handle_local_options(self, options):
        if options.contains('debug'):
            logging.getLogger().setLevel(logging.DEBUG)
        if options.contains('demo') and not self._use_demo():
            return 1
        return -1  # carry on with the default handling

    def _use_demo(self):
        """Show the demo library, before anything reads the cache: False (and a message)
        when there is none to show.

        The library is the development launcher's DEMO_DIR (the source tree's build/demo),
        or the one APPLE_MUSIC_CACHE names (a bigger generated library, say). The demo runs
        as an instance of its own rather than raising a running app, and keeps its settings
        in the demo library's directory (settings.ini), apart from the desktop's; settings
        on the memory backend (tests, the developer scripts) are kept as they are.
        """
        if not os.environ.get('APPLE_MUSIC_CACHE'):
            if not self.demo_dir:
                log.error('--demo needs a demo library: the development build has one, or '
                          'set APPLE_MUSIC_CACHE to a directory scripts/demo_library.py wrote')
                return False
            os.environ['APPLE_MUSIC_CACHE'] = os.path.abspath(self.demo_dir)
        self.demo = True
        self.set_flags(self.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
        backend = self.settings.props.backend
        if GObject.type_name(backend.__gtype__) != 'GMemorySettingsBackend':
            path = os.path.join(config.cache_dir(), 'settings.ini')
            self.settings = Gio.Settings.new_full(
                self.settings.props.settings_schema,
                Gio.keyfile_settings_backend_new(path, '/', None), None)
        log.info('Demo mode: the library in %s', config.cache_dir())
        return True

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
        self._make_parts()
        install_scaler()  # thumbnails scaled from covers on disk, by GdkPixbuf
        # A terminal's Ctrl+C or a kill still stops Chrome: the launcher left SIGINT at its
        # default, which would end the process at once, before the engine is stopped.
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal, signum)

    def _make_parts(self):
        """The engine, the player, the sync, background playback and MPRIS (the media
        controls and keys, as this app): not in demo mode, which would take the real app's
        name on the bus."""
        self.engine = self._make_engine()
        self.engine.connect('lost', self._on_engine_lost)
        self.player = Player(self)
        self.player.connect('notify::track', self._on_track_changed)
        self.player.connect('error', self._on_playback_error)
        self.library_sync = LibrarySync(self)
        self._follow_account()
        if not self.demo:
            self.library_sync.schedule()  # the timed refresh, and a sync when the engine is up
        self.background = BackgroundPlayback(
            self.player, hold=self.hold, release=self._release_background,
            quit=lambda: self.activate_action('quit'))
        if not self.demo:
            self.mpris = Mpris(self)
            self.mpris.start()

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
        if self._quitting is not None:
            return  # the window is going: nothing to show
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
                self._autostart_task = self.spawn(self._autostart())
        else:
            self.library_sync.check()  # launched again: a sync when one is due
        self.library.resume_reading()  # present() waits for the compositor
        window.present()

    def account_key(self, name):
        """The settings key that holds `name` (one of ACCOUNT_KEYS) for this build: the
        development build's `name`-devel, since its Chrome profile, which holds the sign-in,
        is not the release build's; `name` itself otherwise."""
        if self.profile == 'development' and name in ACCOUNT_KEYS:
            return name + '-devel'
        return name

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
        return (not self.demo and self.settings.get_boolean(self.account_key('signed-in'))
                and self.settings.get_boolean('engine-autostart'))

    async def _autostart(self):
        """Chrome on launch, as the settings ask (headless unless engine-headless is off),
        and a word when the sign-in it expected is gone."""
        try:
            await self.engine.start()
        except EngineError as error:
            self.report(error)
            return
        if not self.engine.authorized:
            log.warning('the engine is up but Apple Music is not signed in')
            self.toast(_('Apple Music is no longer signed in'), _('Sign In'), 'app.sign-in')
            return
        # The engine up started a sync if one was due (LibrarySync.schedule()).
        if not self.settings.get_string(self.account_key('account-name')):
            # Sign-in may have missed the name (the page renders it late); try again now.
            try:
                name = await self.engine.account_name(wait=10)
            except EngineError as error:
                log.debug('account name after autostart: %s', error)
                return
            if name:
                self.settings.set_string(self.account_key('account-name'), name)
                log.info('account name read from the page after autostart')

    def start_engine(self):
        """Start the engine (app.start-engine: a toast's Start or Restart) as a task that
        quitting cancels first, its failure reported; the task, or None in demo mode."""
        if self.refuse_in_demo():
            return None
        task = self._engine_start
        if task is None or task.done():
            task = self._engine_start = self.spawn(self._start_engine())
        return task

    async def _start_engine(self):
        try:
            await self.engine.start()
        except EngineError as error:
            self.report(error)

    # -- playback ------------------------------------------------------------------------

    def _on_track_changed(self, player, _pspec):
        playing = player.track is not None
        for action in self._playback_actions:
            action.set_enabled(playing)

    def _on_playback_error(self, _player, message):
        self.toast(message)  # the Player's sentence for MusicKit's code (playback_error_text)

    def player_command(self, coro, on_error=None):
        """A Player command as a task, its EngineError reported (a toast); `on_error()` then
        puts a widget back to the Player's state. The one runner of the Player's commands:
        the actions, the bar, the sheet and MPRIS all go through it."""
        async def command():
            try:
                await coro
            except EngineError as error:
                self.report(error)
                if on_error is not None:
                    on_error()
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
        if self.refuse_in_demo():
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
            body=_('You are signed out of Apple Music, and your library, artwork and sign-in '
                   'kept on this computer are removed.'),
        )
        dialog.add_response('cancel', _('_Cancel'))
        dialog.add_response('sign-out', _('Sign _Out'))
        dialog.set_response_appearance('sign-out', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', self._on_sign_out_response)
        parent = self._preferences  # over Preferences, when it asks from there
        dialog.present(parent if parent is not None else self.get_active_window())

    def _follow_account(self):
        """The actions that depend on the account and the sync follow them (do_startup,
        once the sync exists)."""
        self.settings.connect('changed::' + self.account_key('signed-in'),
                              self._update_sync_action)
        self.library_sync.connect('notify::running', self._update_sync_action)
        self._update_sync_action()

    def _update_sync_action(self, *_args):
        self._sync_action.set_enabled(
            not self.demo and not self.signing_in and not self.signing_out
            and not self.library_sync.props.running
            and self.settings.get_boolean(self.account_key('signed-in')))

    def _on_account_changing(self, *_args):
        for action in self._account_actions:
            action.set_enabled(not self.signing_in and not self.signing_out)
        if self.library_sync is not None:
            self._update_sync_action()

    def _on_sign_out_response(self, _dialog, response):
        if response == 'sign-out':
            from . import account

            self._sign_out_task = self.spawn(account.sign_out(self))

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
        """Tell the user something: a toast on the active window, or in Preferences while
        that is open over it, with a button running an action when given. The one way the
        app makes a toast; its title is plain text, never markup. With the window closed for
        background playback, a notification instead (the last one only), withdrawn when the
        window is back."""
        if self.background is not None and self.background.active:
            notification = Gio.Notification.new(title)
            if button_label and action_name:
                notification.add_button(button_label, action_name)
            self.send_notification(BACKGROUND_NOTIFICATION, notification)
            return
        target = self._preferences
        if target is None:
            target = self.get_active_window()
        if target is None:
            return
        toast = Adw.Toast(title=title, use_markup=False)
        if button_label and action_name:
            toast.set_button_label(button_label)
            toast.set_action_name(action_name)
        target.add_toast(toast)

    def report(self, error):
        """An EngineError for the user: the sentence and the button errors.error_message()
        gives its code, the detail in the log only, never a traceback. 'not-signed-in' opens
        the sign-in while the account is not signed in here; with the demo library, which
        has no engine, 'engine-down' says so."""
        log.warning('engine: %s', error)
        code = getattr(error, 'code', None)
        if self.demo and code == 'engine-down':
            self.refuse_in_demo()
            return
        signed_in = self.settings.get_boolean(self.account_key('signed-in'))
        if code == 'not-signed-in' and not signed_in:
            self.activate_action('sign-in')
            return
        self.toast(*error_message(code))

    def refuse_in_demo(self):
        """True, with a toast saying so, when the demo library is shown: what needs the
        engine or the account is not available then."""
        if self.demo:
            self.toast(_('Not available with the demo library'))
        return self.demo

    def _on_engine_lost(self, _engine, reason):
        """The engine went down on its own (Chrome crashed or was killed, the page crashed or
        stopped answering): a toast offering to start it again. Not while quitting or while
        the account changes, whose restarts are theirs."""
        if self._quitting is not None or self.signing_in or self.signing_out:
            return
        self.toast(_('The playback engine stopped'), _('Restart'), 'app.start-engine')

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

    def _release_background(self):
        """The window is back (or the app quits): what was said meanwhile goes with it."""
        self.withdraw_notification(BACKGROUND_NOTIFICATION)
        self.release()

    def _on_quit(self, *_args):
        """Stop the engine, then quit. Closing the last window comes here too (close_window,
        unless the music plays on in the background), as do MPRIS Quit and the end of
        background playback. Chrome must not outlive the app: this is the clean way; if the
        app dies otherwise, setpriv's parent-death signal (engine.with_pdeathsig) and the
        DevTools pipe closing end it, and do_shutdown kills one a quit left running."""
        if self._quitting is None:
            self._quitting = self.spawn(self._quit())

    async def _quit(self):
        """Everything shown goes at once; then a start the app asked for is cancelled (it
        would hold the engine's stop until Chrome is up), and the sync (its thread included)
        and the engine are stopped, QUIT_TIMEOUT at most; a sign-out under way is given
        SIGN_OUT_WAIT more to finish; then Chrome is killed if it still runs."""
        if self.background is not None:
            self.background.leave()
        if self._signin is not None:
            self._signin.force_close()  # cancels the sign-in, which stops the engine too
        for window in self.get_windows():
            if hasattr(window, 'prepare_quit'):  # a dialog's toplevel has none
                window.prepare_quit()  # remembers its state and hides at once
        # Nothing more starts: a start or a sign-in's finish under way is cancelled, and a
        # sign-out's revocation (its wipe goes on); no sync starts.
        for task in (self._autostart_task, self._engine_start, self.sign_in_finish,
                     self.revocation):
            if task is not None and not task.done():
                task.cancel()
        stopping = [self.engine.stop(grace=QUIT_GRACE)]
        if self.library_sync is not None:
            self.library_sync.hold()
            stopping.append(self.library_sync.cancel())
        try:
            await asyncio.wait_for(asyncio.gather(*stopping), QUIT_TIMEOUT)
        except TimeoutError:
            log.warning('the engine took longer than %g s to stop', QUIT_TIMEOUT)
        except Exception:
            log.exception('stopping the engine failed')
        finally:
            sign_out = self._sign_out_task
            if sign_out is not None and not sign_out.done():
                # It leaves nothing of the account behind: let it end, within a bound.
                await asyncio.wait([sign_out], timeout=SIGN_OUT_WAIT)
            if self.engine.pid:
                self.engine.kill()  # the stop ran out of time, or failed
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
        from .dialogs import about

        about.present(self, self.get_active_window())

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
