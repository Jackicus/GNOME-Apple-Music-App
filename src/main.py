"""The application: app actions, settings, logging and the asyncio-on-GLib bootstrap."""

import asyncio
import logging
import os
import sys
import warnings
from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .window import Window  # noqa: E402

log = logging.getLogger(__name__)

RESOURCE_PATH = '/io/github/jackicus/AppleMusic'


class Application(Adw.Application):
    def __init__(self, version, app_id, base_id, profile):
        super().__init__(
            application_id=app_id,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            resource_base_path=RESOURCE_PATH,
        )
        self.version = version
        self.profile = profile
        self._tasks = set()  # strong references: asyncio only keeps weak ones
        # One schema for every profile, so a Devel build shares the release's settings.
        self.settings = Gio.Settings.new(base_id)

        self._add_action('quit', lambda *_: self.quit(), ['<primary>q'])
        self._add_action('about', self._on_about)
        self._add_action('shortcuts', self._on_shortcuts, ['<primary>question'])
        self.set_accels_for_action('window.close', ['<primary>w'])

        self.add_main_option('debug', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _('Log debug messages'), None)

    def do_handle_local_options(self, options):
        if options.contains('debug'):
            logging.getLogger().setLevel(logging.DEBUG)
        return -1  # carry on with the default handling

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            window = Window(application=self)
            if self.profile == 'development':
                window.add_css_class('devel')
            self.spawn(self._log_event_loop())
        window.present()

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
        section.add(Adw.ShortcutsItem.new(_('Close Window'), '<primary>w'))
        section.add(Adw.ShortcutsItem.new(_('Quit'), '<primary>q'))
        dialog = Adw.ShortcutsDialog()
        dialog.add(section)
        dialog.present(self.get_active_window())


def use_glib_event_loop():
    """Make asyncio run on the GLib main loop, so coroutines and GTK share one thread.

    Python 3.14 deprecates event loop policies (removal in 3.16), but this is
    still how PyGObject hooks asyncio into GLib; if that changes, only this does.
    """
    warnings.filterwarnings('ignore', r"'asyncio\.\w*policy\w*' is deprecated", DeprecationWarning)
    from gi.events import GLibEventLoopPolicy

    asyncio.set_event_loop_policy(GLibEventLoopPolicy())


def main(version, app_id, base_id, profile):
    debug = os.environ.get('APPLE_MUSIC_DEBUG', '') not in ('', '0')
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format='%(levelname)s %(name)s: %(message)s')
    use_glib_event_loop()
    app = Application(version, app_id, base_id, profile)
    return app.run(sys.argv)
