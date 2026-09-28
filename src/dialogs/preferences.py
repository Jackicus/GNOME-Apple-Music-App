"""AppleMusicPreferencesDialog: app.preferences (Ctrl+,).

General: background playback (background-playback), how often the library refreshes
(sync-interval, with when it last did), Refresh Now (app.sync), and the cache's size,
measured in a thread, with Clear (asked first; Application.clear_cache()). Engine: the
engine's state with Start and Stop, the browser command (browser-command), whether Chrome runs
hidden (engine-headless) and starts with the app (engine-autostart), and Sign Out
(app.sign-out). The rows are bound to their settings with
Gio.Settings.bind (the refresh interval by hand: a choice of four values); what the engine
reads applies when it next starts (Application._make_engine). Everything the dialog connects
to outside itself is let go when it closes.
"""

import asyncio
import logging
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ..backend import config
from ..backend.errors import EngineError
from ..engine import cache_size
from ..sync import INTERVALS, interval_index, last_sync_text
from ..widgets.util import connect_weak

log = logging.getLogger(__name__)

# The rows bound one to one to their settings: (template child, property, key).
BINDINGS = (
    ('background_row', 'active', 'background-playback'),
    ('browser_row', 'text', 'browser-command'),
    ('headless_row', 'active', 'engine-headless'),
    ('autostart_row', 'active', 'engine-autostart'),
)


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/preferences.ui')
class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = 'AppleMusicPreferencesDialog'

    general_page = Gtk.Template.Child()
    engine_page = Gtk.Template.Child()
    background_row = Gtk.Template.Child()
    interval_row = Gtk.Template.Child()
    refresh_row = Gtk.Template.Child()
    cache_row = Gtk.Template.Child()
    clear_button = Gtk.Template.Child()
    engine_row = Gtk.Template.Child()
    engine_button = Gtk.Template.Child()
    browser_row = Gtk.Template.Child()
    headless_row = Gtk.Template.Child()
    autostart_row = Gtk.Template.Child()
    account_group = Gtk.Template.Child()
    sign_out_row = Gtk.Template.Child()

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._settings = settings = app.settings
        self._engine = engine = app.engine
        self._closed = False
        self._quiet = False  # the interval row is being set from the setting
        self._engine_busy = False  # a Start or Stop from here is under way
        self._clearing = False
        # The rows and toasts that run app.* actions: a dialog shown as a window of its own
        # (the parent neither maximized nor tiled) is not the application's, and would find
        # none of them.
        self.insert_action_group('app', app)

        for child, prop, key in BINDINGS:
            settings.bind(key, getattr(self, child), prop, Gio.SettingsBindFlags.DEFAULT)
        # Start or Stop, as the engine's state says (the button is off while it changes).
        start, stop = _('_Start'), _('_Stop')  # one button, one mnemonic: Alt+S
        self._label_binding = engine.bind_property(
            'state', self.engine_button, 'label', GObject.BindingFlags.SYNC_CREATE,
            lambda _binding, state: start if state == 'down' else stop)

        self._handlers = [
            (settings, settings.connect('changed::sync-interval', self._update_interval)),
            (settings, settings.connect('changed::last-sync', self._on_last_sync)),
            (settings, settings.connect('changed::signed-in', self._update_account)),
            (settings, settings.connect('changed::account-name', self._update_account)),
            (engine, engine.connect('notify::state', self._update_engine)),
            (engine, engine.connect('notify::authorized', self._update_engine)),
            (engine, engine.connect('notify::headless', self._update_engine)),
        ]
        # The dialog's own rows and buttons are connected weakly (widgets/util.py): a bound
        # method would keep every closed dialog alive.
        connect_weak(self.interval_row, 'notify::selected', self._on_interval_selected)
        connect_weak(self.clear_button, 'clicked', self._on_clear_clicked)
        connect_weak(self.engine_button, 'clicked', self._on_engine_clicked)
        connect_weak(self.sign_out_row, 'activated', self._on_sign_out_activated)
        self.connect('closed', self._on_closed)

        self._update_interval()
        self._update_engine()
        self._update_account()
        self._measure()

    def _on_closed(self, _dialog):
        """Let go of the settings and the engine, which outlive the dialog."""
        self._closed = True
        for source, handler in self._handlers:
            source.disconnect(handler)
        self._handlers = []
        self._label_binding.unbind()
        for child, prop, _key in BINDINGS:
            Gio.Settings.unbind(getattr(self, child), prop)

    # -- the library -----------------------------------------------------------------------

    def _update_interval(self, *_args):
        self._quiet = True
        self.interval_row.set_selected(interval_index(self._settings.get_int('sync-interval')))
        self._quiet = False
        self.interval_row.set_subtitle(last_sync_text(self._settings.get_string('last-sync')))

    def _on_interval_selected(self, row, _pspec):
        if self._quiet:
            return
        index = row.get_selected()
        if 0 <= index < len(INTERVALS) and index != interval_index(
                self._settings.get_int('sync-interval')):
            self._settings.set_int('sync-interval', INTERVALS[index])

    def _on_last_sync(self, *_args):
        self._update_interval()
        if not self._clearing:
            self._measure()  # a sync has finished: the cache has grown

    def _measure(self):
        self._app.spawn(self._measure_cache())

    async def _measure_cache(self):
        """The cache's size, added up in a thread, as the Cache row's subtitle."""
        size = await asyncio.to_thread(cache_size, config.cache_dir())
        if not self._closed and not self._clearing:
            self.cache_row.set_subtitle(GLib.format_size(size) if size else _('Empty'))

    def _on_clear_clicked(self, _button):
        if self._app.demo:
            self.add_toast(Adw.Toast(title=_('Not available with the demo library')))
            return
        if self._settings.get_boolean('signed-in'):
            body = _('The library, artwork and lyrics kept on this computer are removed, '
                     'then your library is fetched again from Apple Music.')
        else:
            body = _('The library, artwork and lyrics kept on this computer are removed.')
        dialog = Adw.AlertDialog(heading=_('Clear the Cache?'), body=body)
        dialog.add_response('cancel', _('_Cancel'))
        dialog.add_response('clear', _('C_lear'))
        dialog.set_response_appearance('clear', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        connect_weak(dialog, 'response', self._on_clear_response)
        dialog.present(self)

    def _on_clear_response(self, _dialog, response):
        if response == 'clear':
            self._app.spawn(self.clear())

    async def clear(self):
        """Clear the cache (Application.clear_cache: a sync follows when signed in), then
        measure it again."""
        self._clearing = True
        self.clear_button.set_sensitive(False)
        self.cache_row.set_subtitle(_('Clearing…'))
        try:
            cleared = await self._app.clear_cache()
        except OSError as error:  # clear_cache() logs what it cannot remove; this is worse
            log.warning('clearing the cache: %s', error)
            cleared = False
        finally:
            self._clearing = False
            self.clear_button.set_sensitive(True)
        if self._closed:
            return
        if cleared:
            self.add_toast(Adw.Toast(title=_('Cache cleared')))
        await self._measure_cache()

    # -- the engine ------------------------------------------------------------------------

    def _update_engine(self, *_args):
        engine = self._engine
        state = engine.state
        if self._app.demo:
            subtitle = _('Not used with the demo library')
        elif state == 'starting':
            subtitle = _('Starting…')
        elif state == 'signing-in':
            subtitle = _('Signing in…')
        elif state != 'up':
            subtitle = _('Not running')
        elif not engine.authorized:
            subtitle = _('Running, not signed in')
        elif engine.headless:
            subtitle = _('Running hidden')
        else:
            subtitle = _('Running in a window')
        self.engine_row.set_subtitle(subtitle)
        # Start while it is down, Stop while it is up; nothing while it changes, or while
        # the sign-in dialog has it.
        self.engine_button.set_sensitive(
            not self._app.demo and not self._engine_busy and state in ('down', 'up'))

    def _on_engine_clicked(self, _button):
        engine = self._engine
        if engine.state == 'down':
            command = engine.start()
        elif engine.state == 'up':
            command = engine.stop()
        else:
            return
        self._engine_busy = True
        self._update_engine()
        self._app.spawn(self._engine_command(command))

    async def _engine_command(self, command):
        try:
            await command
        except EngineError as error:
            self._app.report(error)  # a toast, here while the dialog is open
        finally:
            self._engine_busy = False
            if not self._closed:
                self._update_engine()

    def _on_sign_out_activated(self, _row):
        self._app.activate_action('sign-out')  # asks first, over this dialog

    def _update_account(self, *_args):
        signed_in = self._settings.get_boolean('signed-in') and not self._app.demo
        name = self._settings.get_string('account-name')
        if self._app.demo:
            description = _('Not used with the demo library')
        elif not signed_in:
            description = _('Not signed in')
        elif name:
            description = _('Signed in as {name}').format(name=name)
        else:
            description = _('Signed in')
        self.account_group.set_description(description)
        self.sign_out_row.set_sensitive(signed_in)
