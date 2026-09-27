from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, Gtk

from . import pages, sections
from .pages.artist import ArtistPage
from .pages.detail import DetailPage
from .player_bar import PlayerBar  # noqa: F401  registers $AppleMusicPlayerBar for the template

# The placeholder pages that count something: key -> (the count, its label). The labels are
# looked up when used, after the launcher has set up gettext. %s is the count, grouped as the
# locale groups digits.
COUNTED = {
    'radio': (lambda library: library.radio.get_n_items(),
              lambda n: ngettext('%s station', '%s stations', n)),
}


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/window.ui')
class Window(Adw.ApplicationWindow):
    __gtype_name__ = 'AppleMusicWindow'

    toast_overlay = Gtk.Template.Child()
    split_view = Gtk.Template.Child()
    sidebar = Gtk.Template.Child()
    content_page = Gtk.Template.Child()
    navigation_view = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._settings = self.get_application().settings
        self._library = self.get_application().library
        self._destinations = {}  # Adw.SidebarItem -> Destination
        self._items_by_key = {}
        self._roots = {}  # destination key -> its root Adw.NavigationPage, once visited
        self._placeholders = {}  # destination key -> Adw.StatusPage, for those without a page

        self._build_sidebar()
        self._library_handlers = [
            self._library.connect('notify::state', self._on_library_changed),
            self._library.connect('changed', self._on_library_changed),
        ]
        self._on_library_changed()
        self._restore_window_state()
        self._select(self._settings.get_string('last-page'))

        sign_in = Gio.SimpleAction.new('sign-in', None)
        sign_in.connect('activate', lambda *_args: self.toast(_('Signing in is not available yet')))
        self.add_action(sign_in)

        # Alt+Left (main.py): the navigation views pop on their own only while the focus is in
        # them; this goes back from anywhere in the window, and is off when there is nowhere to
        # go, so the keys reach the focus then.
        self._back = Gio.SimpleAction.new('back', None)
        self._back.connect('activate', self._on_back)
        self.add_action(self._back)
        self.navigation_view.connect('notify::visible-page', self._update_back)
        self.split_view.connect('notify::collapsed', self._update_back)
        self.split_view.connect('notify::show-content', self._update_back)
        self._update_back()

    def toast(self, title):
        self.toast_overlay.add_toast(Adw.Toast(title=title))

    def open_item(self, item):
        """Show an album, artist, playlist, station or video: what activating a tile does.

        Albums and playlists push a DetailPage, artists an ArtistPage, over the page shown.
        Stations and videos have no page: a toast, until phases 7 and 12 decide what they do.
        """
        visible = self.navigation_view.get_visible_page()
        if getattr(visible, 'item', None) is item:
            return  # a double activation
        if item.kind in ('album', 'playlist'):
            page = DetailPage(self._library, item)
        elif item.kind == 'artist':
            page = ArtistPage(self._library, item)
        else:
            self.toast(item.title)
            return
        self.navigation_view.push(page)

    def play_request(self, play, start_with=None, shuffle=False):
        """Play what play names ({kind, id}: an Item's or a Group's play target), from its entry at
        queue position start_with (a track row: track.play, track.index), or shuffled.

        The one way into playback, as `am.py play <kind> <id> [--start-with N] [--shuffle]` is
        upstream; phase 12 hands it to the engine. Until then a toast says what would play.
        """
        item = self._library.by_id(play.get('kind'), play.get('id')) if play else None
        track = self._library.track_at(play, start_with) if start_with is not None else None
        title = track.title if track is not None else item.title if item is not None else None
        if title is None:
            self.toast(_('Playback is not available yet'))
        elif shuffle:
            self.toast(_('Shuffling “{title}” is not available yet').format(title=title))
        else:
            self.toast(_('Playing “{title}” is not available yet').format(title=title))

    def _build_sidebar(self):
        for title, destinations in sections.sidebar_sections():
            section = Adw.SidebarSection(title=title)
            for destination in destinations:
                item = Adw.SidebarItem(title=destination.title, icon_name=destination.icon_name)
                section.append(item)
                self._destinations[item] = destination
                self._items_by_key[destination.key] = item
            self.sidebar.append(section)

        self.sidebar.connect('notify::selected-item', self._on_selected_item)
        self.sidebar.connect('activated', lambda *_: self.split_view.set_show_content(True))

    def _root(self, destination):
        """The destination's root page, built on its first visit and kept in the view."""
        page = self._roots.get(destination.key)
        if page is None:
            page = pages.create(destination, self._library)
            if page is None:
                page = self._placeholder_page(destination)
            page.set_tag(destination.key)
            self.navigation_view.add(page)
            self._roots[destination.key] = page
        return page

    def _placeholder_page(self, destination):
        status = Adw.StatusPage(
            icon_name=destination.icon_name,
            title=destination.title,
            description=_('Nothing here yet'),
        )
        self._placeholders[destination.key] = status
        self._on_library_changed()
        toolbar = Adw.ToolbarView(content=status)
        toolbar.add_top_bar(Adw.HeaderBar(show_title=False))
        return Adw.NavigationPage(title=destination.title, child=toolbar)

    def _on_library_changed(self, *_args):
        """Placeholder pages say what the library holds for them, until real pages exist."""
        state = self._library.state
        for key, (count, label) in COUNTED.items():
            if key not in self._placeholders:
                continue
            n = count(self._library) if state == 'ready' else 0
            if state == 'loading':
                description = _('Loading…')
            elif n:
                description = label(n) % f'{n:n}'
            else:
                description = _('Nothing here yet')
            self._placeholders[key].set_description(description)

    def _select(self, key):
        item = self._items_by_key.get(key) or self._items_by_key['home']
        self.sidebar.set_selected(item.get_index())
        if self.navigation_view.get_visible_page() is None:  # it was selected already
            self._on_selected_item(self.sidebar, None)

    def _on_selected_item(self, sidebar, _pspec):
        destination = self._destinations.get(sidebar.get_selected_item())
        if destination is None:
            return
        self.navigation_view.replace([self._root(destination)])
        self.content_page.set_title(destination.title)
        self._settings.set_string('last-page', destination.key)

    def _can_go_back(self):
        return (len(self.navigation_view.get_navigation_stack()) > 1
                or (self.split_view.get_collapsed() and self.split_view.get_show_content()))

    def _update_back(self, *_args):
        self._back.set_enabled(self._can_go_back())

    def _on_back(self, *_args):
        if len(self.navigation_view.get_navigation_stack()) > 1:
            self.navigation_view.pop()
        elif self.split_view.get_collapsed():
            self.split_view.set_show_content(False)

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
        for handler in self._library_handlers:
            self._library.disconnect(handler)  # the library outlives the window
        self._library_handlers = []
        return Adw.ApplicationWindow.do_close_request(self)
