# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicPreferencesDialog: app.preferences (Ctrl+,).

General: background playback (background-playback); how often the library refreshes
(sync-interval, with when it last did, or "Refreshing…" while it does); the cache's size,
measured in a thread and again after each sync, with Clear (asked first:
Application.clear_cache()); Refresh Now (app.sync, off while it cannot run). Engine: the
engine's state with Start and Stop; the browser program (browser-command, kept when it is
applied and found); whether Chrome runs hidden (engine-headless) and starts with the app
(engine-autostart); Sign In while signed out, Sign Out while signed in (app.sign-out).

The switches are bound to their settings with Gio.Settings.bind, the browser program one way
(the setting into the row: the row's text is kept only on apply), the refresh interval by
hand (a choice of four values). What the engine reads applies when it next starts
(Application._make_engine). A button whose work is under way (Start, Stop, Clear) stays
sensitive, so the focus stays on it, and says it is busy to assistive technologies; a click
meanwhile does nothing. Everything the dialog connects to outside itself is let go when it
closes.
"""

import asyncio
import logging
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..backend import config
from ..backend.errors import EngineError
from ..cache import cache_size
from ..sync import INTERVALS, interval_index, last_sync_text
from ..widgets.util import connect_weak


def named_list_factory(row, popup):
    """A factory for a combo row's list items, as libadwaita's own (a label; in the popup,
    a check mark on the item selected) but naming each item for assistive technology: the
    default leaves them unnamed, both in the popup and in the row's own display of the
    value, which is a list of one item too (the row itself is named by its title). The row
    is held weakly: the factory is the row's own, and a closure holding the row would keep a
    closed dialog alive."""
    row_ref = row.weak_ref()

    def setup(_factory, item):
        box = Gtk.Box()
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=20,
                          width_chars=1, valign=Gtk.Align.CENTER)
        box.append(label)
        if popup:
            box.append(Gtk.Image(icon_name='object-select-symbolic',
                                 accessible_role=Gtk.AccessibleRole.PRESENTATION))
        item.set_child(box)

    def bind(_factory, item):
        row = row_ref()
        if row is None:
            return
        text = item.get_item().get_string()
        box = item.get_child()
        box.get_first_child().set_label(text)
        item.set_accessible_label(text)
        if not popup:
            return
        check = box.get_last_child()

        def follow(*_args):
            current = row_ref()
            selected = current.get_selected_item() if current is not None else None
            check.set_opacity(1.0 if selected == item.get_item() else 0.0)

        item.follow_handler = row.connect('notify::selected-item', follow)
        follow()

    def unbind(_factory, item):
        row = row_ref()
        handler = getattr(item, 'follow_handler', None)
        if row is not None and handler is not None:
            row.disconnect(handler)
        item.follow_handler = None

    factory = Gtk.SignalListItemFactory()
    factory.connect('setup', setup)
    factory.connect('bind', bind)
    factory.connect('unbind', unbind)
    return factory

log = logging.getLogger(__name__)

# The rows bound one to one to their settings: (template child, property, key).
BINDINGS = (
    ('background_row', 'active', 'background-playback'),
    ('discord_row', 'active', 'discord-presence'),
    ('headless_row', 'active', 'engine-headless'),
    ('autostart_row', 'active', 'engine-autostart'),
)


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/preferences.ui')
class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = 'AppleMusicPreferencesDialog'

    general_page = Gtk.Template.Child()
    engine_page = Gtk.Template.Child()
    background_row = Gtk.Template.Child()
    discord_row = Gtk.Template.Child()
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
    sign_in_row = Gtk.Template.Child()
    sign_out_row = Gtk.Template.Child()

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._settings = settings = app.settings
        self._key = account_key = app.account_key  # this build's key for an account's setting
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
        settings.bind('browser-command', self.browser_row, 'text', Gio.SettingsBindFlags.GET)
        # Start or Stop, as the engine's state says.
        start, stop = _('_Start'), _('_Stop')  # one button, one mnemonic: Alt+S
        self._label_binding = engine.bind_property(
            'state', self.engine_button, 'label', GObject.BindingFlags.SYNC_CREATE,
            lambda _binding, state: start if state == 'down' else stop)

        self._handlers = [
            (settings, settings.connect('changed::sync-interval', self._update_interval)),
            (settings, settings.connect('changed::' + account_key('last-sync'),
                                        self._on_last_sync)),
            (settings, settings.connect('changed::' + account_key('signed-in'),
                                        self._update_account)),
            (settings, settings.connect('changed::' + account_key('account-name'),
                                        self._update_account)),
            (app, app.connect('notify::signing-out', self._update_account)),
            (app.library_sync, app.library_sync.connect('notify::running',
                                                         self._on_sync_running)),
            (engine, engine.connect('notify::state', self._update_engine)),
            (engine, engine.connect('notify::authorized', self._update_engine)),
            (engine, engine.connect('notify::headless', self._update_engine)),
        ]
        # The dialog's own rows and buttons are connected weakly (widgets/util.py): a bound
        # method would keep every closed dialog alive.
        self.interval_row.set_factory(named_list_factory(self.interval_row, popup=False))
        self.interval_row.set_list_factory(named_list_factory(self.interval_row, popup=True))
        connect_weak(self.interval_row, 'notify::selected', self._on_interval_selected)
        connect_weak(self.clear_button, 'clicked', self._on_clear_clicked)
        connect_weak(self.engine_button, 'clicked', self._on_engine_clicked)
        connect_weak(self.browser_row, 'apply', self._on_browser_apply)
        connect_weak(self.browser_row, 'changed', self._on_browser_changed)
        connect_weak(self.sign_in_row, 'activated', self._on_sign_in_activated)
        connect_weak(self.sign_out_row, 'activated', self._on_sign_out_activated)
        self.connect('closed', self._on_closed)

        self._update_interval()
        self._update_engine()
        self._update_account()
        self._measure()
        # "Last refreshed 5 minutes ago" moves on while the dialog is open.
        self._tick = GLib.timeout_add_seconds(60, self._on_tick)

    def _on_closed(self, _dialog):
        """Let go of the settings, the engine and the sync, which outlive the dialog."""
        self._closed = True
        GLib.source_remove(self._tick)
        for source, handler in self._handlers:
            source.disconnect(handler)
        self._handlers = []
        self._label_binding.unbind()
        for child, prop, _key in BINDINGS:
            Gio.Settings.unbind(getattr(self, child), prop)
        Gio.Settings.unbind(self.browser_row, 'text')

    @staticmethod
    def _busy(widget, busy):
        """Say that `widget`'s work is under way (or over), for assistive technologies."""
        widget.update_state([Gtk.AccessibleState.BUSY], [busy])

    # -- the library -----------------------------------------------------------------------

    def _update_interval(self, *_args):
        self._quiet = True
        self.interval_row.set_selected(interval_index(self._settings.get_int('sync-interval')))
        self._quiet = False
        if self._app.library_sync.props.running:
            subtitle = _('Refreshing…')
        else:
            subtitle = last_sync_text(self._settings.get_string(self._key('last-sync')))
        self.interval_row.set_subtitle(subtitle)

    def _on_tick(self):
        self._update_interval()
        return GLib.SOURCE_CONTINUE

    def _on_interval_selected(self, row, _pspec):
        if self._quiet:
            return
        index = row.get_selected()
        if 0 <= index < len(INTERVALS) and index != interval_index(
                self._settings.get_int('sync-interval')):
            self._settings.set_int('sync-interval', INTERVALS[index])

    def _on_last_sync(self, *_args):
        self._update_interval()

    def _on_sync_running(self, library_sync, _pspec):
        self._update_interval()
        if not library_sync.props.running and not self._clearing:
            self._measure()  # a sync has ended: the cache has grown

    def _measure(self):
        self._app.spawn(self._measure_cache())

    async def _measure_cache(self):
        """The cache's size, added up in a thread, as the Cache row's subtitle."""
        size = await asyncio.to_thread(cache_size, config.cache_dir())
        if not self._closed and not self._clearing:
            self.cache_row.set_subtitle(GLib.format_size(size) if size else _('Empty'))

    def _on_clear_clicked(self, _button):
        if self._clearing or self._app.refuse_in_demo():
            return
        if self._settings.get_boolean(self._key('signed-in')):
            body = _('The library, artwork and lyrics kept on this computer are removed, '
                     'then your library is fetched again from Apple Music.')
        else:
            body = _('The library, artwork and lyrics kept on this computer are removed.')
        dialog = Adw.AlertDialog(heading=_('Clear Cache?'), body=body)
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
        self._busy(self.clear_button, True)
        self.cache_row.set_subtitle(_('Clearing…'))
        try:
            cleared = await self._app.clear_cache()
        except OSError as error:  # clear_cache() logs what it cannot remove; this is worse
            log.warning('clearing the cache: %s', error)
            cleared = False
        finally:
            self._clearing = False
            self._busy(self.clear_button, False)
        if self._closed:
            return
        if cleared:
            self._app.toast(_('Cache cleared'))
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
        self.engine_button.set_sensitive(not self._app.demo)
        self._busy(self.engine_button, self._engine_busy or state in ('starting', 'signing-in'))

    def _on_engine_clicked(self, _button):
        """Start while it is down, Stop while it is up; nothing while it changes (the button
        says it is busy), or while the sign-in has it."""
        engine = self._engine
        if self._engine_busy:
            return
        if engine.state == 'down':
            command = self._app.start_engine()  # a task quitting cancels; it reports its errors
            if command is None:
                return
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

    def _on_browser_changed(self, row):
        row.remove_css_class('error')

    def _on_browser_apply(self, row):
        self._app.spawn(self._apply_browser(row.get_text().strip()))

    async def _apply_browser(self, text):
        """Keep the browser program when it is found (on the host, in a Flatpak sandbox);
        empty, the default again. One that is not found is not kept: the row says so."""
        settings = self._settings
        if not text:
            settings.reset('browser-command')
            return
        path = await self._engine.browser_path(text)
        if self._closed:
            return
        if path is None:
            self.browser_row.add_css_class('error')
            self._app.toast(_('No program called “{name}” was found').format(name=text))
            return
        self.browser_row.remove_css_class('error')
        settings.set_string('browser-command', text)
        self.browser_row.set_text(text)  # stripped, as kept

    # -- the account -----------------------------------------------------------------------

    def _on_sign_in_activated(self, _row):
        self.close()  # the sign-in shows over the window
        self._app.activate_action('sign-in')

    def _on_sign_out_activated(self, _row):
        self._app.activate_action('sign-out')  # asks first, over this dialog

    def _update_account(self, *_args):
        demo = self._app.demo
        signed_in = self._settings.get_boolean(self._key('signed-in')) and not demo
        name = self._settings.get_string(self._key('account-name'))
        if demo:
            description = _('Not used with the demo library')
        elif not signed_in:
            description = _('Not signed in')
        elif name:
            description = _('Signed in as {name}').format(name=name)
        else:
            description = _('Signed in')
        self.account_group.set_description(description)
        self.sign_in_row.set_visible(not signed_in and not demo)
        self.sign_out_row.set_visible(signed_in)
        self.sign_out_row.set_sensitive(not self._app.signing_out)
