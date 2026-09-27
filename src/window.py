from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from . import sections
from .player_bar import PlayerBar  # noqa: F401  registers $AppleMusicPlayerBar for the template


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/window.ui')
class Window(Adw.ApplicationWindow):
    __gtype_name__ = 'AppleMusicWindow'

    toast_overlay = Gtk.Template.Child()
    split_view = Gtk.Template.Child()
    sidebar = Gtk.Template.Child()
    content_page = Gtk.Template.Child()
    stack = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._settings = self.get_application().settings
        self._destinations = {}  # Adw.SidebarItem -> Destination
        self._items_by_key = {}

        self._build_sidebar()
        self._restore_window_state()
        self._select(self._settings.get_string('last-page'))

        sign_in = Gio.SimpleAction.new('sign-in', None)
        sign_in.connect('activate', lambda *_args: self.toast(_('Signing in is not available yet')))
        self.add_action(sign_in)

    def toast(self, title):
        self.toast_overlay.add_toast(Adw.Toast(title=title))

    def _build_sidebar(self):
        for title, destinations in sections.sidebar_sections():
            section = Adw.SidebarSection(title=title)
            for destination in destinations:
                item = Adw.SidebarItem(title=destination.title, icon_name=destination.icon_name)
                section.append(item)
                self._destinations[item] = destination
                self._items_by_key[destination.key] = item
                self.stack.add_named(self._placeholder_page(destination), destination.key)
            self.sidebar.append(section)

        self.sidebar.connect('notify::selected-item', self._on_selected_item)
        self.sidebar.connect('activated', lambda *_: self.split_view.set_show_content(True))

    def _placeholder_page(self, destination):
        return Adw.StatusPage(
            icon_name=destination.icon_name,
            title=destination.title,
            description=_('Nothing here yet'),
        )

    def _select(self, key):
        item = self._items_by_key.get(key) or self._items_by_key['home']
        self.sidebar.set_selected(item.get_index())

    def _on_selected_item(self, sidebar, _pspec):
        destination = self._destinations.get(sidebar.get_selected_item())
        if destination is None:
            return
        self.stack.set_visible_child_name(destination.key)
        self.content_page.set_title(destination.title)
        self._settings.set_string('last-page', destination.key)

    def _restore_window_state(self):
        self.set_default_size(
            self._settings.get_int('window-width'),
            self._settings.get_int('window-height'),
        )
        if self._settings.get_boolean('window-maximized'):
            self.maximize()

    def do_close_request(self):
        width, height = self.get_default_size()
        self._settings.set_int('window-width', width)
        self._settings.set_int('window-height', height)
        self._settings.set_boolean('window-maximized', self.is_maximized())
        return Adw.ApplicationWindow.do_close_request(self)
