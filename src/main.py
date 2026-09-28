"""The application: app actions, settings, the library and its sync, the engine's lifecycle,
sign-in and sign-out, Preferences and the cache, background playback, demo mode, logging and
the asyncio-on-GLib bootstrap."""

import asyncio
import logging
import os
import shutil
import signal
import sys
import time
import warnings
from datetime import datetime, timezone
from gettext import gettext as _
from gettext import ngettext

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .backend import config  # noqa: E402
from .backend.errors import EngineError  # noqa: E402
from .engine import Engine, clear_cache, engine_paths  # noqa: E402
from .library import Library  # noqa: E402
from .mpris import Mpris  # noqa: E402
from .player import Player  # noqa: E402
from .sync import install_scaler, sync_due, sync_library  # noqa: E402
from .widgets import artwork  # noqa: E402
from .window import Window  # noqa: E402

log = logging.getLogger(__name__)

RESOURCE_PATH = '/io/github/jackicus/AppleMusic'

# How long quitting waits for the engine to stop (its own SIGTERM grace is 5 s) before Chrome is
# killed outright and the app quits anyway.
QUIT_TIMEOUT = 6.0

# With the window closed for background playback, how long playback may look stopped (MusicKit
# passes through 'ended' and 'stopped' between items and queues) before the app quits.
BACKGROUND_GRACE = 10


class Application(Adw.Application):
    """The app. `demo_dir` is where --demo finds its invented library (the launcher passes the
    source tree's build/demo, where scripts/demo_library.py writes it)."""

    def __init__(self, version, app_id, base_id, profile, demo_dir=None):
        super().__init__(
            application_id=app_id,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            resource_base_path=RESOURCE_PATH,
        )
        self.version = version
        self.profile = profile
        self.demo_dir = demo_dir
        self.demo = False  # True under --demo: an invented library and no engine
        self.library = None  # created in do_startup
        self.engine = None  # created in do_startup
        self.player = None  # created in do_startup, after the engine
        self.mpris = None  # created in do_startup, after the player; released in do_shutdown
        self._tasks = set()  # strong references: asyncio only keeps weak ones
        self._quitting = None  # the task stopping the engine before the app quits
        self._signin = None  # the sign-in dialog while it is open
        self._preferences = None  # the Preferences dialog while it is open
        self._sync_task = None  # the sync running, if one is
        self._background = None  # the handlers while the window is closed and music plays on
        self._background_timer = None  # the grace before quitting, once playback stopped
        # One schema for every profile, so a Devel build shares the release's settings.
        self.settings = Gio.Settings.new(base_id)

        self._add_action('quit', self._on_quit, ['<primary>q'])
        self._add_action('about', self._on_about)
        self._add_action('shortcuts', self._on_shortcuts, ['<primary>question'])
        self._add_action('preferences', self._on_preferences, ['<primary>comma'])
        self._add_action('sign-in', self._on_sign_in)
        self._add_action('sign-out', self._on_sign_out)
        self._add_action('sync', self._on_sync, ['<primary>r'])
        self._add_action('now-playing', self._on_now_playing, ['<primary>n'])
        # Playback, enabled while something plays (the bar's buttons follow). Their keys
        # (Space, Ctrl+Right, Ctrl+Left) are the window's (window.PLAYBACK_KEYS), not
        # accelerators, which GTK 4 would fire before a focused entry gets them.
        self._playback_actions = [
            self._add_action('play-pause', self._on_play_pause),
            self._add_action('next', self._on_next),
            self._add_action('previous', self._on_previous),
            self._add_action('shuffle', self._on_shuffle),
            self._add_action('repeat', self._on_repeat),
        ]
        for action in self._playback_actions:
            action.set_enabled(False)
        self.set_accels_for_action('window.close', ['<primary>w'])
        self.set_accels_for_action('win.back', ['<alt>Left'])
        self.set_accels_for_action('win.search', ['<primary>f'])

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
        if not os.environ.get('APPLE_MUSIC_CACHE') and self.demo_dir:
            os.environ['APPLE_MUSIC_CACHE'] = str(self.demo_dir)
        log.info('Demo mode: the library in %s', config.cache_dir())

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.library = Library()
        self.engine = self._make_engine()
        self.player = Player(self)
        self.player.connect('notify::track', self._on_track_changed)
        self.player.connect('error', self._on_playback_error)
        self.mpris = Mpris(self)  # the media controls and keys, as this app
        self.mpris.start()
        install_scaler()  # thumbnails scaled from covers on disk, by GdkPixbuf
        # A terminal's Ctrl+C or a kill still stops Chrome: the launcher left SIGINT at its
        # default, which would end the process with Chrome running on (reclaimed next time).
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal, signum)

    def do_shutdown(self):
        if self.mpris is not None:
            self.mpris.stop()  # the name released before the bus connection goes
        Adw.Application.do_shutdown(self)

    def _make_engine(self):
        """The Engine on this build's profile directory and port (engine_paths: the .Devel build
        beside the release one; the environment overrides win), with the browser command and
        the preferred mode from the settings, then and whenever they change (Preferences):
        the browser, the port and the mode apply when Chrome next starts."""
        if self.demo:
            return Engine(demo=True)
        profile_dir, port = engine_paths(self.profile, self.settings.get_int('engine-port'))
        engine = Engine(profile_dir, port, self.settings.get_string('browser-command'))
        engine.prefer_headless = self.settings.get_boolean('engine-headless')
        self.settings.connect(
            'changed::browser-command',
            lambda settings, key: setattr(engine, 'browser_command', settings.get_string(key)))
        self.settings.connect(
            'changed::engine-port',
            lambda settings, key: engine.set_port(engine_paths(self.profile,
                                                               settings.get_int(key))[1]))
        self.settings.connect(
            'changed::engine-headless',
            lambda settings, key: setattr(engine, 'prefer_headless', settings.get_boolean(key)))
        log.debug('engine: profile %s, port %d, state %s', profile_dir, port, engine.state_file)
        return engine

    def _on_signal(self, signum):
        log.info('signal %d: quitting', signum)
        self.activate_action('quit')
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            self.spawn(self.library.load())
            window = Window(application=self)
            if self.profile == 'development':
                window.add_css_class('devel')
            self.spawn(self._log_event_loop())
            if self._autostart_wanted():
                self.spawn(self._autostart())
        elif self.engine.state == 'up' and self.engine.authorized and self.sync_due():
            self.start_sync()  # launched again: a sync when the last is old
        window.present()

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
        if self.sync_due():
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

    def _on_play_pause(self, *_args):
        self.player_command(self.player.toggle())

    def _on_next(self, *_args):
        self.player_command(self.player.next())

    def _on_previous(self, *_args):
        self.player_command(self.player.previous())

    def _on_shuffle(self, *_args):
        self.player_command(self.player.toggle_shuffle())

    def _on_repeat(self, *_args):
        self.player_command(self.player.cycle_repeat())

    # -- the sync ------------------------------------------------------------------------

    def sync_due(self):
        """Whether the library should be synced now: never yet, or the last sync is older
        than the sync-interval setting (hours; 0 means only when asked)."""
        return sync_due(self.settings.get_string('last-sync'),
                        self.settings.get_int('sync-interval'))

    def _on_sync(self, *_args):
        self.start_sync()

    def start_sync(self):
        """Sync the library through the engine, starting it if it is down, unless a sync is
        running already. Returns the task, or None."""
        if self.demo:
            self.toast(_('Not available with the demo library'))
            return None
        if self._sync_task is not None and not self._sync_task.done():
            log.debug('a sync is running already')
            return None
        self._sync_task = self.spawn(self._sync())
        return self._sync_task

    async def _sync(self):
        window = self.get_active_window()
        started = time.monotonic()
        try:
            if self.engine.state == 'down':
                await self.engine.start()
            if window is not None:
                window.show_sync_progress(None, 0, None)
            counts = await sync_library(self.engine, self.library, self._on_sync_progress)
        except EngineError as error:
            log.warning('sync: %s', error)
            if error.code == 'not-signed-in':
                self.report(error)
            else:
                self.toast(_('Could not sync your library: {message}').format(
                    message=error.message), _('Retry'), 'app.sync')
            return
        finally:
            window = self.get_active_window()
            if window is not None:
                window.hide_sync_progress()
        self.settings.set_string('last-sync', datetime.now(timezone.utc).isoformat(
            timespec='seconds'))
        log.info('sync done in %.0f s', time.monotonic() - started)
        albums, playlists = counts.get('albums', 0), counts.get('playlists', 0)
        songs = self.library.song_count()
        summary = ', '.join([
            ngettext('{count} album', '{count} albums', albums).format(count=f'{albums:n}'),
            ngettext('{count} playlist', '{count} playlists', playlists).format(
                count=f'{playlists:n}'),
            ngettext('{count} song', '{count} songs', songs).format(count=f'{songs:n}'),
        ])
        self.toast(_('Library synced: {summary}').format(summary=summary))

    def _on_sync_progress(self, section, done, total):
        window = self.get_active_window()
        if window is not None:
            window.show_sync_progress(section, done, total)

    async def clear_cache(self):
        """Clear the cache (engine.CACHE_ENTRIES: library.json, the artwork, items, lyrics
        and the day-long answers), after stopping a sync that is running; the library
        empties and `last-sync` is forgotten, and the library is synced again when signed
        in. Answers False (nothing done) in demo mode, whose library is the cache."""
        if self.demo:
            return False
        task = self._sync_task
        if task is not None and not task.done():
            task.cancel()  # it would write library.json and artwork into what is cleared
            await asyncio.wait([task])
        await asyncio.to_thread(clear_cache, config.cache_dir())
        artwork.get_default().clear()
        self.settings.set_string('last-sync', '')
        await self.library.load()  # nothing left to read: the models empty
        if self.settings.get_boolean('signed-in'):
            self.start_sync()
        return True

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

    # -- background playback -------------------------------------------------------------

    def close_window(self, window):
        """The window's close request (Window.do_close_request). With background playback
        on and something playing, the window hides and the app is held while the music
        plays on: MPRIS Raise or launching the app again shows it, and the app quits once
        playback has stopped for BACKGROUND_GRACE seconds. Otherwise the app quits, which
        stops the engine."""
        if (self._quitting is None and self.settings.get_boolean('background-playback')
                and self.player is not None and self.player.active):
            window.hide_for_background()
            self._enter_background(window)
        else:
            self.activate_action('quit')

    @property
    def in_background(self):
        """Whether the window is closed while the music plays on (the app held)."""
        return self._background is not None

    def _enter_background(self, window):
        if self._background is None:
            self.hold()
            self._background = [
                (self.player, self.player.connect('notify::state', self._check_background)),
                (self.player, self.player.connect('notify::track', self._check_background)),
                (window, window.connect('notify::visible', self._on_window_visible)),
            ]
            log.info('the window is closed; playing on in the background')
        self._check_background()

    def _leave_background(self):
        """The window is back (or the app is quitting): the hold released, nothing watched."""
        if self._background is None:
            return
        for source, handler in self._background:
            source.disconnect(handler)
        self._background = None
        if self._background_timer is not None:
            GLib.source_remove(self._background_timer)
            self._background_timer = None
        self.release()

    def _on_window_visible(self, window, _pspec):
        if window.get_visible():
            log.info('the window is shown again')
            self._leave_background()

    def _check_background(self, *_args):
        """Playback stopped with the window closed: quit after the grace, unless something
        plays (or is paused) again by then."""
        if self._background is None:
            return
        if not self.player.stopped:
            if self._background_timer is not None:
                GLib.source_remove(self._background_timer)
                self._background_timer = None
        elif self._background_timer is None:
            self._background_timer = GLib.timeout_add_seconds(
                BACKGROUND_GRACE, self._on_background_stopped)

    def _on_background_stopped(self):
        self._background_timer = None
        if self._background is not None and self.player.stopped:
            log.info('playback stopped with the window closed: quitting')
            self.activate_action('quit')
        return GLib.SOURCE_REMOVE

    # -- quitting ------------------------------------------------------------------------

    def _on_quit(self, *_args):
        """Stop the engine, then quit: Chrome must not outlive the app. Closing the last window
        comes here too (close_window, unless the music plays on in the background), as do
        MPRIS Quit and the end of background playback."""
        if self._quitting is None:
            self._quitting = self.spawn(self._quit())

    async def _quit(self):
        self._leave_background()
        for window in self.get_windows():
            if hasattr(window, 'prepare_quit'):  # a dialog's toplevel has none
                window.prepare_quit()  # remembers its state and hides at once
        if self._sync_task is not None and not self._sync_task.done():
            self._sync_task.cancel()  # its downloads give up at the next one
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

    # -- sign-in and sign-out ----------------------------------------------------------------

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

    def _on_preferences(self, *_args):
        self.show_preferences()

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

    def _on_sign_out_response(self, _dialog, response):
        if response == 'sign-out':
            self.spawn(self._sign_out())

    async def _sign_out(self):
        """Stop the engine, forget the account, wipe the Chrome profile and the cache, and
        empty the library."""
        if self.demo:
            return
        try:
            await self.engine.stop()
        except EngineError as error:
            log.warning('stopping the engine before signing out: %s', error)
        self.settings.set_boolean('signed-in', False)
        self.settings.set_string('account-name', '')
        await asyncio.to_thread(remove_trees, self.engine.profile_dir, config.cache_dir())
        await self.library.load()  # nothing left to read: the models empty
        self.toast(_('Signed out'))

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

    def _add_action(self, name, callback, accels=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect('activate', callback)
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f'app.{name}', accels)
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
        section = Adw.ShortcutsSection(title=_('General'))
        section.add(Adw.ShortcutsItem.new(_('Keyboard Shortcuts'), '<primary>question'))
        section.add(Adw.ShortcutsItem.new(_('Preferences'), '<primary>comma'))
        section.add(Adw.ShortcutsItem.new(_('Search'), '<primary>f'))
        section.add(Adw.ShortcutsItem.new(_('Refresh Library'), '<primary>r'))
        section.add(Adw.ShortcutsItem.new(_('Go Back'), '<alt>Left'))
        section.add(Adw.ShortcutsItem.new(_('Close Window'), '<primary>w'))
        section.add(Adw.ShortcutsItem.new(_('Quit'), '<primary>q'))
        playback = Adw.ShortcutsSection(title=_('Playback'))
        playback.add(Adw.ShortcutsItem.new(_('Play or Pause'), 'space'))
        playback.add(Adw.ShortcutsItem.new(_('Next'), '<primary>Right'))
        playback.add(Adw.ShortcutsItem.new(_('Previous'), '<primary>Left'))
        playback.add(Adw.ShortcutsItem.new(_('Now Playing'), '<primary>n'))
        dialog = Adw.ShortcutsDialog()
        dialog.add(section)
        dialog.add(playback)
        dialog.present(self.get_active_window())


def remove_trees(*paths, attempts=4, pause=0.5):
    """Delete directories (in a thread), leaving anything that cannot be deleted. Chrome's
    helper processes write to the profile for a moment after the browser process has exited
    (its network service re-created `Default/Network Persistent State` in one run), so a
    directory that comes back is removed again, a few times, `pause` seconds apart."""
    for path in paths:
        if not path:
            continue
        for attempt in range(attempts):
            if not os.path.isdir(path):
                break
            if attempt:
                time.sleep(pause)
            log.info('removing %s', path)
            shutil.rmtree(path, ignore_errors=True)
        if os.path.isdir(path):
            log.warning('%s could not be removed entirely', path)


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
