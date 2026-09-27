import sys
from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw, Gio, Gtk

from .window import Window

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
        # One schema for every profile, so a Devel build shares the release's settings.
        self.settings = Gio.Settings.new(base_id)

        self._add_action('quit', lambda *_: self.quit(), ['<primary>q'])
        self._add_action('about', self._on_about)
        self._add_action('shortcuts', self._on_shortcuts, ['<primary>question'])
        self.set_accels_for_action('window.close', ['<primary>w'])

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            window = Window(application=self)
            if self.profile == 'development':
                window.add_css_class('devel')
        window.present()

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


def main(version, app_id, base_id, profile):
    app = Application(version, app_id, base_id, profile)
    return app.run(sys.argv)
