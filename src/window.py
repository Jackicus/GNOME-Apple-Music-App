import logging
import time
from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from . import pages, sections
from .pages.artist import ArtistPage
from .pages.detail import DetailPage
from .pages.grid import GridPage
from .player_bar import PlayerBar  # noqa: F401  registers $AppleMusicPlayerBar for the template
from .sidebar import SidebarEntry, SidebarItem, is_shown, parse_key, playlist_entries

log = logging.getLogger(__name__)

# The first destination of the Playlists section, whose items the library's playlists follow.
PLAYLISTS = 'all-playlists'


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/window.ui')
class Window(Adw.ApplicationWindow):
    __gtype_name__ = 'AppleMusicWindow'

    toast_overlay = Gtk.Template.Child()
    split_view = Gtk.Template.Child()
    sidebar = Gtk.Template.Child()
    content_page = Gtk.Template.Child()
    navigation_view = Gtk.Template.Child()
    account_stack = Gtk.Template.Child()
    account_avatar = Gtk.Template.Child()
    account_label = Gtk.Template.Child()
    sign_in_banner = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._settings = self.get_application().settings
        self._library = self.get_application().library
        self._destinations = {}  # the fixed sections' Adw.SidebarItem -> Destination
        self._destination_keys = {}  # key -> Destination, every fixed destination
        self._items_by_key = {}  # key -> Adw.SidebarItem, the fixed sections'
        self._roots = {}  # sidebar key -> its root Adw.NavigationPage, once visited
        self._positions = {}  # sidebar key -> position in the Playlists section
        self._expanded = set(self._settings.get_strv('expanded-folders'))
        self._shown = None  # the key whose root page is at the bottom of the navigation stack
        self._quiet = False  # the selection changes, but not by the user: show nothing
        self._quitting = False

        self._build_sidebar()
        self._update_playlists()
        self._library_handler = self._library.connect('changed', self._on_library_changed)
        self._restore_window_state()
        self._restore_page(self._settings.get_string('last-page'))

        # The account button and the banner follow the signed-in and account-name keys.
        self._settings_handlers = [
            self._settings.connect('changed::signed-in', self._update_account),
            self._settings.connect('changed::account-name', self._update_account),
        ]
        self._update_account()

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

    # The account.

    def _update_account(self, *_args):
        signed_in = self._settings.get_boolean('signed-in')
        name = self._settings.get_string('account-name')
        self.account_stack.set_visible_child_name('account' if signed_in else 'sign-in')
        self.account_label.set_label(name or _('Signed In'))
        self.account_avatar.set_text(name)
        self.account_avatar.set_show_initials(bool(name))
        self.sign_in_banner.set_revealed(not signed_in and not self.get_application().demo)

    @Gtk.Template.Callback()
    def on_banner_sign_in(self, _banner):
        self.get_application().activate_action('sign-in')

    def open_item(self, item):
        """Show an album, artist, playlist, folder, station or video: what activating a tile
        does.

        Albums and playlists push a DetailPage, artists an ArtistPage, playlist folders their
        grid of folders and playlists, over the page shown. A station has no page: it plays, as
        on music.apple.com. A video toasts its title, until phase 12 decides what it does.
        """
        visible = self.navigation_view.get_visible_page()
        if getattr(visible, 'item', None) is item:
            return  # a double activation
        if item.kind in ('album', 'playlist'):
            page = DetailPage(self._library, item)
        elif item.kind == 'artist':
            page = ArtistPage(self._library, item)
        elif item.kind == 'folder':  # a folder's tile in a folder's page
            page = pages.folder(self._library, item.id, item.title, root=False)
            page.item = item
        elif item.kind == 'station':
            self.play_request(item.play)
            return
        else:
            self.toast(item.title)
            return
        self.navigation_view.push(page)

    def open_shelf(self, shelf):
        """Show a shelf's items as a grid, pushed over the page shown: a shelf's See All.

        A shelf of the library's is followed by its key, so the page shows what a later load
        puts on it; any other (phase 15's search results) is shown as it is.
        """
        visible = self.navigation_view.get_visible_page()
        if getattr(visible, 'shelf', None) is shelf:
            return  # a double activation
        if shelf in self._library.shelves:
            key = shelf.key

            def model():
                found = self._library.shelf(key)
                return found.items if found else None
        else:
            model = shelf.items
        page = GridPage(self._library, shelf.title, model, root=False,
                        icon_name='view-grid-symbolic', empty_title=_('Nothing Here'),
                        empty_description=_('This shelf is empty now'))
        page.shelf = shelf
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

    # The sidebar. The first sections are fixed items; the Playlists section is bound to a
    # store of SidebarEntry: its two destinations, then the library's folders and playlists.

    def _build_sidebar(self):
        for title, destinations in sections.sidebar_sections():
            section = Adw.SidebarSection(title=title)
            for destination in destinations:
                self._destination_keys[destination.key] = destination
            if destinations[0].key == PLAYLISTS:
                self._playlist_section = section
                self._playlist_store = Gio.ListStore(item_type=SidebarEntry)
                self._playlist_store.splice(
                    0, 0, [SidebarEntry.fixed(destination) for destination in destinations])
                self._fixed_count = len(destinations)
                self._positions = {destination.key: position
                                   for position, destination in enumerate(destinations)}
                section.bind_model(self._playlist_store, self._create_playlist_item)
            else:
                for destination in destinations:
                    item = Adw.SidebarItem(title=destination.title,
                                           icon_name=destination.icon_name)
                    section.append(item)
                    self._destinations[item] = destination
                    self._items_by_key[destination.key] = item
            self.sidebar.append(section)

        self.sidebar.connect('notify::selected-item', self._on_selected_item)
        self.sidebar.connect('activated', self._on_activated)

    def _create_playlist_item(self, entry):
        item = SidebarItem(entry)
        item.set_visible(is_shown(entry, self._expanded))
        item.set_expanded(entry.folder_id in self._expanded)
        return item

    def _update_playlists(self):
        """Make the Playlists section list the library's folders and playlists.

        bind_model makes a new item for every entry spliced in, and the selection is lost when
        the selected item goes, so entries showing the same things as before (a reload of the
        same library) are kept, only their Items swapped for the new load's.
        """
        started = time.perf_counter()
        entries = playlist_entries(self._library.playlist_tree())
        fixed = self._fixed_count
        old = [self._playlist_store.get_item(position)
               for position in range(fixed, self._playlist_store.get_n_items())]
        if [entry.shape() for entry in old] == [entry.shape() for entry in entries]:
            for kept, entry in zip(old, entries):
                kept.item = entry.item
            return
        self._quiet = True
        try:
            self._playlist_store.splice(fixed, len(old), entries)
        finally:
            self._quiet = False
        self._positions = {entry.key: position
                           for position, entry in enumerate(self._playlist_store)}
        folders = [entry.folder_id for entry in entries if entry.kind == 'folder']
        log.debug('Sidebar: %d folders, %d playlists in %.1f ms; expanded: %s', len(folders),
                  len(entries) - len(folders), (time.perf_counter() - started) * 1000,
                  ', '.join(folder for folder in folders if folder in self._expanded) or 'none')

    def _on_library_changed(self, _library):
        self._update_playlists()
        # The page shown keeps showing, its item selected again (or for the first time, when
        # it was restored before the library had loaded), unless it is gone: then Home.
        if self._key(self.sidebar.get_selected_item()) != self._shown:
            self._select(self._shown)
        for key, page in list(self._roots.items()):
            if parse_key(key) is None:
                continue
            entry = self._entry(key)
            if entry is not None:
                page.set_title(entry.title)  # a renamed playlist or folder
            elif key != self._shown:
                self.navigation_view.remove(page)
                del self._roots[key]
        self.content_page.set_title(self._roots[self._shown].get_title())

    def _entry(self, key):
        position = self._positions.get(key)
        return self._playlist_store.get_item(position) if position is not None else None

    def _sidebar_item(self, key):
        item = self._items_by_key.get(key)
        if item is None:
            position = self._positions.get(key)
            if position is not None:
                item = self._playlist_section.get_item(position)
        return item

    def _key(self, item):
        """The key of a sidebar item: its destination's, or its entry's."""
        if isinstance(item, SidebarItem):
            return item.entry.key
        destination = self._destinations.get(item)
        return destination.key if destination is not None else None

    def _root(self, key):
        """The root page for a sidebar key, built on its first visit and kept in the view."""
        page = self._roots.get(key)
        if page is None:
            destination = self._destination_keys.get(key)
            if destination is not None:
                page = pages.create(destination, self._library)
                if page is None:
                    page = self._placeholder_page(destination)
            else:
                kind, item_id = parse_key(key)
                entry = self._entry(key)
                if kind == 'playlist':
                    # Restored before the library has loaded: titled once it has.
                    title = entry.title if entry is not None else _('Playlist')
                    page = pages.playlist(self._library, item_id, title)
                else:
                    title = entry.title if entry is not None else _('Folder')
                    page = pages.folder(self._library, item_id, title)
            page.set_tag(key)
            self.navigation_view.add(page)
            self._roots[key] = page
        return page

    def _placeholder_page(self, destination):
        status = Adw.StatusPage(
            icon_name=destination.icon_name,
            title=destination.title,
            description=_('Nothing here yet'),
        )
        toolbar = Adw.ToolbarView(content=status)
        toolbar.add_top_bar(Adw.HeaderBar(show_title=False))
        return Adw.NavigationPage(title=destination.title, child=toolbar)

    def _restore_page(self, key):
        """Show the page last-page names.

        A playlist's or folder's item exists only once the library has loaded: until then its
        page shows (loading) with All Playlists selected, and _on_library_changed selects its
        item, or goes Home when it is gone. Not with nothing selected: the sidebar's list
        selects the row with the focus when nothing is, which is its first (Search) as the
        window is shown, or when it becomes active.
        """
        if (self._sidebar_item(key) is None and parse_key(key) is not None
                and self._library.state != 'ready'):
            self._set_selected(self._sidebar_item(PLAYLISTS).get_index())
            self._show(key)
        else:
            self._select(key)

    def _select(self, key):
        """Select the sidebar item for key, or Home's when there is none, which shows its page.

        An item in a collapsed folder is shown first: its folders are expanded.
        """
        item = self._sidebar_item(key)
        if item is None:
            key = 'home'
            item = self._items_by_key[key]
        elif not item.get_visible():
            self._expanded.update(self._entry(key).ancestors)
            self._save_expanded()
        self._set_selected(item.get_index())
        self._show(key)

    def _set_selected(self, index):
        """Select the sidebar item at index (or none), showing nothing: the caller does."""
        self._quiet = True
        try:
            self.sidebar.set_selected(index)
        finally:
            self._quiet = False

    def _on_selected_item(self, sidebar, _pspec):
        if self._quiet:
            return
        key = self._key(sidebar.get_selected_item())
        if key is not None:
            self._show(key)

    def _show(self, key):
        """Make key's root page the navigation view's root. Pages pushed over it stay when it is
        the root already: a reload selecting the page shown again must not pop them."""
        root = self._root(key)
        stack = self.navigation_view.get_navigation_stack()
        if not stack.get_n_items() or stack.get_item(0) is not root:
            self.navigation_view.replace([root])
        self.content_page.set_title(root.get_title())
        self._shown = key
        self._settings.set_string('last-page', key)

    def _on_activated(self, sidebar, index):
        """A click (or Enter) on an item, selected already or not: show its page (selecting it
        did, unless it was selected while a restored page waited for the library), show the
        content when the layout is collapsed, and open or close a folder."""
        item = sidebar.get_item(index)
        key = self._key(item)
        if key is not None and key != self._shown:
            self._show(key)
        self.split_view.set_show_content(True)
        if isinstance(item, SidebarItem) and item.entry.kind == 'folder':
            folder_id = item.entry.folder_id
            if folder_id in self._expanded:
                self._expanded.discard(folder_id)
            else:
                self._expanded.add(folder_id)
            self._save_expanded()

    def _save_expanded(self):
        """Store the expanded folders and show or hide the items they hold.

        Ids of folders the library no longer has are kept: a demo run shares the settings with
        the real library, whose folders it lacks.
        """
        self._settings.set_strv('expanded-folders', sorted(self._expanded))
        for position in range(self._fixed_count, self._playlist_store.get_n_items()):
            entry = self._playlist_store.get_item(position)
            item = self._playlist_section.get_item(position)
            item.set_visible(is_shown(entry, self._expanded))
            item.set_expanded(entry.folder_id in self._expanded)

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

    def _save_window_state(self):
        width, height = self.get_default_size()
        self._settings.set_int('window-width', width)
        self._settings.set_int('window-height', height)
        self._settings.set_boolean('window-maximized', self.is_maximized())

    def prepare_quit(self):
        """The app is quitting (app.quit, or this window closing): remember the window's
        state and hide it now, so nothing shows while the engine stops."""
        if self._quitting:
            return
        self._quitting = True
        self._save_window_state()
        self._library.disconnect(self._library_handler)  # the library outlives the window
        for handler in self._settings_handlers:
            self._settings.disconnect(handler)
        self._settings_handlers = []
        self.set_visible(False)

    def do_close_request(self):
        # Closing the last window quits, and quitting stops the engine first: the window
        # stays (hidden) until the app has, so the close is declined here.
        self.get_application().activate_action('quit')
        return True
