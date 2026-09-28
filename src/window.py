import logging
import time
from collections import OrderedDict
from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import pages, sections, shortcuts
from .actions import ItemActions, TrackRef
from .backend.errors import EngineError
from .player_bar import PlayerBar  # noqa: F401  registers $AppleMusicPlayerBar for the template
from .widgets.now_playing import NowPlayingSheet  # noqa: F401  registers the sheet's type
from .sidebar import SidebarEntry, SidebarItem, is_shown, parse_key, playlist_entries

log = logging.getLogger(__name__)

# The first destination of the Playlists section, whose items the library's playlists follow.
PLAYLISTS = 'all-playlists'
# The fixed destination showing the Favourite Songs playlist, which has a playlist's menu too.
FAVOURITE_SONGS = 'favourite-songs'
# The sidebar playlists' and folders' root pages kept once shown: past this many, the least
# recently shown is dropped (and freed), and built again if it is shown again. The fixed
# destinations' pages are kept for good.
ROOT_LIMIT = 8


def _parse_keys(table):
    """{(keyval, modifiers): action} for a table of action -> accelerator strings."""
    keys = {}
    for name, accels in table.items():
        for accel in accels:
            ok, keyval, mods = Gtk.accelerator_parse(accel)
            if ok:
                keys[(keyval, int(mods))] = name
    return keys


# The playback keys (shortcuts.PLAYBACK), by (keyval, modifiers) -> app action. Not application
# accelerators: GTK 4 runs those in the window's capture phase, before the focus widget, so a
# bare Space would fire while typing in the Songs filter and Ctrl+Left would skip a track
# instead of a word. The window's own capture-phase key controller handles them instead, and
# leaves the widgets whose keys they are alone (on_key_pressed).
PLAYBACK_KEYS = _parse_keys(shortcuts.PLAYBACK)

# Where Space is the widget's own key: it toggles these, so it toggles them rather than
# playback. (Enter presses a button; Space on a plain button, a tile or a row plays or pauses.)
SPACE_TOGGLES = (Gtk.ToggleButton, Gtk.Switch, Gtk.CheckButton)
SPACE_KEYS = (Gdk.KEY_space, Gdk.KEY_KP_Space)


def sync_section_names():
    """What the sync banner calls each progress section (sync.PROGRESS_SECTIONS), translated
    on call, after gettext is set up."""
    return {
        'songs': _('songs'),
        'playlists': _('playlists'),
        'folders': _('folders'),
        'videos': _('music videos'),
        'radio': _('radio'),
        'shelves': _('shelves'),
        'artwork': _('artwork'),
    }


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/window.ui')
class Window(Adw.ApplicationWindow):
    __gtype_name__ = 'AppleMusicWindow'

    toast_overlay = Gtk.Template.Child()
    primary_menu_button = Gtk.Template.Child()
    bottom_sheet = Gtk.Template.Child()
    player_bar = Gtk.Template.Child()
    now_playing = Gtk.Template.Child()
    split_view = Gtk.Template.Child()
    sidebar = Gtk.Template.Child()
    content_page = Gtk.Template.Child()
    navigation_view = Gtk.Template.Child()
    account_stack = Gtk.Template.Child()
    account_avatar = Gtk.Template.Child()
    account_label = Gtk.Template.Child()
    sign_in_banner = Gtk.Template.Child()
    sync_banner = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._settings = self.get_application().settings
        self._library = self.get_application().library
        self._destinations = {}  # the fixed sections' Adw.SidebarItem -> Destination
        self._destination_keys = {}  # key -> Destination, every fixed destination
        self._items_by_key = {}  # key -> Adw.SidebarItem, the fixed sections'
        self._roots = {}  # sidebar key -> its root Adw.NavigationPage, once visited
        self._recent_roots = OrderedDict()  # playlist/folder keys in _roots, least recent first
        self._positions = {}  # sidebar key -> position in the Playlists section
        self._expanded = set(self._settings.get_strv('expanded-folders'))
        self._shown = None  # the key whose root page is at the bottom of the navigation stack
        self._quiet = False  # the selection changes, but not by the user: show nothing
        self._quitting = False
        self._expanded_save = None  # the timeout that will write expanded-folders

        # The win.item-* actions the context menus run (actions.py), before the sidebar,
        # whose playlists' menu is theirs.
        self.item_actions = ItemActions(self, self.get_application())
        self._build_sidebar()
        self._update_playlists()
        self.player_bar.set_player(self.get_application().player, self.get_application())
        self.now_playing.set_player(self.get_application().player, self.get_application(),
                                    self.bottom_sheet)
        self._library_handler = self._library.connect('changed', self._on_library_changed)
        self._restore_window_state()
        self._restore_page(self._settings.get_string('last-page'))

        # The account button and the banner follow the signed-in and account-name keys.
        self._settings_handlers = [
            self._settings.connect('changed::signed-in', self._update_account),
            self._settings.connect('changed::account-name', self._update_account),
        ]
        self._update_account()

        # The window's actions (their keys are shortcuts.ACCELS, set in main.py). Alt+Left:
        # the navigation views pop on their own only while the focus is in them; this goes
        # back from anywhere in the window. Ctrl+F: the Search page, with the cursor in its
        # entry. Ctrl+1, 2, 3: the focus into the sidebar, the page, the player bar. Each is
        # off while a dialog is open over the window (its keys are the dialog's then), and
        # back is off when there is nowhere to go or the Now Playing sheet is open, so the
        # keys reach the focus.
        self._actions = {}
        for name, callback in (('back', self._on_back), ('search', self._on_search),
                               ('focus-sidebar', self._on_focus_sidebar),
                               ('focus-content', self._on_focus_content),
                               ('focus-player', self._on_focus_player)):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            self.add_action(action)
            self._actions[name] = action
        self.navigation_view.connect('notify::visible-page', self._update_actions)
        self.split_view.connect('notify::collapsed', self._update_actions)
        self.split_view.connect('notify::show-content', self._update_actions)
        self.bottom_sheet.connect('notify::open', self._update_actions)
        self.connect('notify::visible-dialog', self._update_actions)
        self._update_actions()

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.on_key_pressed)
        self.add_controller(keys)

    def on_key_pressed(self, _controller, keyval, _keycode, state):
        """Space, Ctrl+Right and Ctrl+Left run the playback actions (PLAYBACK_KEYS) while
        something plays, unless the keys belong to the focus: an entry or a text view, a
        menu (a popover), a dialog over the window, and for Space a toggle (a toggle button,
        a switch, a check box). A key nothing handles goes on to the focus widget. F10 opens
        the primary menu when the sidebar holding it is hidden (GTK's own F10 needs it
        shown)."""
        mods = int(state & Gtk.accelerator_get_default_mod_mask())
        if keyval == Gdk.KEY_F10 and not mods:
            return self._show_primary_menu()
        name = PLAYBACK_KEYS.get((keyval, mods))
        if name is None:
            return False
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Editable, Gtk.TextView)):
            return False
        if keyval in SPACE_KEYS and isinstance(focus, SPACE_TOGGLES):
            return False
        if self.get_visible_dialog() is not None or _in_popover(focus):
            return False
        action = self.get_application().lookup_action(name)
        if action is None or not action.get_enabled():
            return False
        self.get_application().activate_action(name)
        return True

    def _show_primary_menu(self):
        """F10 while the primary menu's button is not shown (the collapsed layout showing a
        page): show the sidebar, then open the menu there. True when it did."""
        button = self.primary_menu_button
        if button.get_mapped() or self.get_visible_dialog() is not None:
            return False  # GTK's own F10 opens it
        if not self.split_view.get_collapsed():
            return False
        self._close_sheet()
        self.split_view.set_show_content(False)
        GLib.idle_add(self._popup_primary_menu)
        return True

    def _popup_primary_menu(self):
        self.primary_menu_button.popup()
        return GLib.SOURCE_REMOVE

    def toast(self, title):
        self.add_toast(Adw.Toast(title=title))

    def add_toast(self, toast):
        """Show a toast over the content, or inside the Now Playing sheet while that is open
        (the sheet is modal: the window's overlay is under it)."""
        if self.bottom_sheet.get_open():
            self.now_playing.add_toast(toast)
        else:
            self.toast_overlay.add_toast(toast)

    def toggle_now_playing(self):
        """Open the Now Playing sheet, or close it (app.now-playing); not while a dialog is
        open over the window."""
        if self.get_visible_dialog() is None:
            self.bottom_sheet.set_open(not self.bottom_sheet.get_open())

    def _close_sheet(self):
        if self.bottom_sheet.get_open():
            self.bottom_sheet.set_open(False)

    def _on_search(self, *_args):
        """win.search: select Search in the sidebar and put the cursor in its entry (the Now
        Playing sheet closed first)."""
        self._close_sheet()
        self._select('search')
        self.split_view.set_show_content(True)
        page = self._roots.get('search')
        if page is not None and hasattr(page, 'focus_entry'):
            page.focus_entry()

    def open_songs(self, search=''):
        """Show the Songs page filtered by `search` (Your Library results' See All)."""
        self._select('songs')
        self.split_view.set_show_content(True)
        page = self._roots.get('songs')
        if page is not None and hasattr(page, 'set_filter'):
            page.set_filter(search)

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

    # The sync's progress, on a banner over the content.

    def show_sync_progress(self, section, done, total):
        """Reveal the sync banner saying how far the sync is: "Syncing your library: songs
        300 of 2,000". section is one of sync.PROGRESS_SECTIONS, or None before the first."""
        name = sync_section_names().get(section)
        if name is None:
            title = _('Syncing your library…')
        elif total:
            title = _('Syncing your library: {section} {done} of {total}').format(
                section=name, done=f'{done:n}', total=f'{total:n}')
        elif done:
            title = _('Syncing your library: {section} {done}').format(
                section=name, done=f'{done:n}')
        else:
            title = _('Syncing your library: {section}…').format(section=name)
        self.sync_banner.set_title(title)
        self.sync_banner.set_revealed(True)

    def hide_sync_progress(self):
        self.sync_banner.set_revealed(False)

    def open_item(self, item):
        """Show an album, artist, playlist, folder, category, station, song or video: what
        activating a tile does.

        Albums and playlists push a DetailPage, artists an ArtistPage, playlist folders their
        grid of folders and playlists, search categories their page of shelves, over the page
        shown. A station or a song (a search hit, a Best New Songs tile) has no page: it plays,
        as on music.apple.com. A video toasts its title, until phase 12 decides what it does.
        """
        visible = self.navigation_view.get_visible_page()
        if getattr(visible, 'item', None) is item:
            return  # a double activation
        if item.kind in ('album', 'playlist'):
            from .pages.detail import DetailPage

            page = DetailPage(self._library, item)
        elif item.kind == 'artist':
            from .pages.artist import ArtistPage

            page = ArtistPage(self._library, item)
        elif item.kind == 'folder':  # a folder's tile in a folder's page
            page = pages.folder(self._library, item.id, item.title, root=False)
            page.item = item
        elif item.kind == 'category':
            from .pages.shelves import category_page

            page = category_page(item)
        elif item.kind in ('station', 'song'):
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
        from .pages.grid import GridPage

        # A shelf of artists (a search's, a category's) gets the round portraits.
        artist = (shelf.items.get_n_items() > 0
                  and all(item.kind == 'artist' for item in shelf.items))
        page = GridPage(self._library, shelf.title, model, root=False, artist=artist,
                        icon_name='view-grid-symbolic', empty_title=_('Nothing Here'),
                        empty_description=_('This shelf is empty now'))
        page.shelf = shelf
        self.navigation_view.push(page)

    def play_request(self, play, start_with=None, shuffle=False):
        """Play what play names ({kind, id}: an Item's or a Group's play target), from its entry at
        queue position start_with (a track row: track.play, track.index), or shuffled.

        The one way into playback from the pages (a Play button, a track row, a station's
        tile), as `am.py play <kind> <id> [--start-with N] [--shuffle]` was upstream: the
        Player plays it through the engine, starting that first if need be; signed out, the
        sign-in flow opens instead; a failure is toasted. The bar follows the engine's events.
        """
        app = self.get_application()
        if app.demo:
            self.toast(_('Not available with the demo library'))
            return
        if not play or not play.get('kind') or not play.get('id'):
            self.toast(_('This cannot be played'))
            return
        app.spawn(self._play(play, start_with, shuffle))

    async def _play(self, play, start_with, shuffle):
        app = self.get_application()
        try:
            await app.player.play(play, start_with=start_with, shuffle=shuffle)
        except EngineError as error:
            if error.code == 'not-signed-in' and not app.settings.get_boolean('signed-in'):
                log.info('play while signed out: opening sign-in')
                app.activate_action('sign-in')
            else:
                app.report(error)

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
                # The playlists' context menu: filled for the item it opens on (setup-menu).
                self._sidebar_menu = Gio.Menu()
                section.set_menu_model(self._sidebar_menu)
            else:
                for destination in destinations:
                    item = Adw.SidebarItem(title=destination.title,
                                           icon_name=destination.icon_name,
                                           drag_motion_activate=False)
                    section.append(item)
                    self._destinations[item] = destination
                    self._items_by_key[destination.key] = item
            self.sidebar.append(section)

        self.sidebar.connect('notify::selected-item', self._on_selected_item)
        # The page mode (the collapsed layout) builds new rows: named again once built.
        self.sidebar.connect('notify::mode', lambda *_: GLib.idle_add(
            self._update_sidebar_accessibility))
        self.sidebar.connect('activated', self._on_activated)
        self.sidebar.connect('setup-menu', self._on_setup_menu)
        # Tracks dragged from a list (widgets/context_menu.py) drop onto playlists.
        self.sidebar.setup_drop_target(Gdk.DragAction.COPY, [TrackRef])
        self.sidebar.connect('drop-enter', self._on_drop_enter)
        self.sidebar.connect('drop', self._on_drop)

    def _sidebar_playlist(self, item):
        """The playlist Item a sidebar item shows: a playlist entry's, or Favourite Songs';
        None for a folder, All Playlists and the other sections' items."""
        if not isinstance(item, SidebarItem):
            return None
        entry = item.entry
        if entry.kind == 'playlist':
            return entry.item
        if entry.kind == 'fixed' and entry.key == FAVOURITE_SONGS:
            return self._library.favourite_songs()
        return None

    def _on_setup_menu(self, _sidebar, item):
        """The Playlists section's context menu opens on item: point it at the playlist
        (Play, Play Next, Open in Browser). Nothing to offer (a folder, All Playlists): the
        menu is closed before it is drawn, since AdwSidebar would show it empty. None: it
        closed, before its action runs, so the menu is left as it is."""
        if item is None:
            return
        self.item_actions.fill_sidebar_menu(self._sidebar_menu, self._sidebar_playlist(item))
        if not self._sidebar_menu.get_n_items():
            GLib.idle_add(self._close_sidebar_menu, priority=GLib.PRIORITY_HIGH)

    def _close_sidebar_menu(self):
        child = self.sidebar.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Popover) and child.get_visible():
                child.popdown()
            child = child.get_next_sibling()
        return GLib.SOURCE_REMOVE

    def _drop_playlist(self, index):
        """The playlist a track dropped on the item at index goes to: only a playlist entry
        that takes songs (not a folder, not a fixed item)."""
        item = self.sidebar.get_item(index)
        if not isinstance(item, SidebarItem) or item.entry.kind != 'playlist':
            return None
        playlist = item.entry.item
        return playlist if self.item_actions.can_drop(playlist) else None

    def _on_drop_enter(self, _sidebar, index):
        return Gdk.DragAction.COPY if self._drop_playlist(index) is not None else 0

    def _on_drop(self, _sidebar, index, value, _action):
        playlist = self._drop_playlist(index)
        if playlist is None:
            return False
        return self.item_actions.drop(playlist, value)

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
            for kept, entry in zip(old, entries, strict=True):
                kept.item = entry.item
            return
        self._quiet = True
        try:
            self._playlist_store.splice(fixed, len(old), entries)
        finally:
            self._quiet = False
        self._positions = {entry.key: position
                           for position, entry in enumerate(self._playlist_store)}
        self._update_sidebar_accessibility()
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
                self._drop_root(key)
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
        """The root page for a sidebar key, built on its first visit and kept in the view: a
        fixed destination's for good, a playlist's or a folder's while it is among the
        ROOT_LIMIT most recently shown (_trim_roots)."""
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
        if parse_key(key) is not None:
            self._recent_roots[key] = None
            self._recent_roots.move_to_end(key)
            self._trim_roots(key)
        return page

    def _trim_roots(self, keep):
        """Drop the least recently shown playlist and folder root pages past ROOT_LIMIT, but
        never keep's, the one shown or one in the navigation stack."""
        surplus = len(self._recent_roots) - ROOT_LIMIT
        if surplus <= 0:
            return
        stack = self.navigation_view.get_navigation_stack()
        in_stack = [stack.get_item(position) for position in range(stack.get_n_items())]
        for key in list(self._recent_roots):
            if surplus <= 0:
                break
            if key in (keep, self._shown):
                continue
            page = self._roots[key]
            if any(page is shown for shown in in_stack):
                continue
            self._drop_root(key)
            surplus -= 1

    def _drop_root(self, key):
        """Remove a root page from the view and forget it: nothing else holds it, so it is
        freed (widgets/util.py)."""
        page = self._roots.pop(key)
        self._recent_roots.pop(key, None)
        self.navigation_view.remove(page)

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
        self._shown = key  # written to last-page when the window closes (_save_window_state)

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
        """Show or hide the items the expanded folders hold, and store the folders, a second
        after the last toggle (and when the window closes), rather than at every click.

        Ids of folders the library no longer has are kept: a demo run shares the settings with
        the real library, whose folders it lacks.
        """
        if self._expanded_save is None:
            self._expanded_save = GLib.timeout_add_seconds(1, self._on_expanded_timeout)
        for position in range(self._fixed_count, self._playlist_store.get_n_items()):
            entry = self._playlist_store.get_item(position)
            item = self._playlist_section.get_item(position)
            item.set_visible(is_shown(entry, self._expanded))
            item.set_expanded(entry.folder_id in self._expanded)
        self._update_sidebar_accessibility()

    # The sidebar's rows, for their accessible names and states: AdwSidebar builds a row per
    # item, hidden ones too, in item order (one Gtk.ListBox in the sidebar mode, one boxed list
    # per section in the page mode, which it builds anew when the mode changes) and names none
    # of them in the sidebar mode, nor says whether a folder is expanded. Found by walking its
    # widgets; if they ever stop matching the items one to one, nothing is set.

    def _sidebar_rows(self):
        rows = []
        for box in _descendants_of_type(self.sidebar, Gtk.ListBox):
            index = 0
            while (row := box.get_row_at_index(index)) is not None:
                rows.append(row)
                index += 1
        return rows if len(rows) == self.sidebar.get_items().get_n_items() else []

    def _sidebar_row(self, index):
        rows = self._sidebar_rows()
        return rows[index] if 0 <= index < len(rows) else None

    def _update_sidebar_accessibility(self):
        """Name each sidebar row after its item, and give folders their expanded state."""
        items = self.sidebar.get_items()
        for index, row in enumerate(self._sidebar_rows()):
            item = items.get_item(index)
            row.update_property([Gtk.AccessibleProperty.LABEL], [item.get_title()])
            if isinstance(item, SidebarItem) and item.entry.kind == 'folder':
                # An int: the state is "true, false or undefined".
                row.update_state([Gtk.AccessibleState.EXPANDED],
                                 [int(item.entry.folder_id in self._expanded)])
        return GLib.SOURCE_REMOVE

    def _can_go_back(self):
        return (len(self.navigation_view.get_navigation_stack()) > 1
                or (self.split_view.get_collapsed() and self.split_view.get_show_content()))

    def _update_actions(self, *_args):
        """The window's actions are off while a dialog is open over it (their keys go to the
        dialog, not to the pages behind it); back also while the Now Playing sheet is open
        (Escape closes that) or there is nowhere to go back to."""
        free = self.get_visible_dialog() is None
        for action in self._actions.values():
            action.set_enabled(free)
        self._actions['back'].set_enabled(
            free and not self.bottom_sheet.get_open() and self._can_go_back())

    # Ctrl+1, 2, 3: the focus into the sidebar, the page, the player bar.

    def _on_focus_sidebar(self, *_args):
        """win.focus-sidebar: the focus on the selected sidebar row (the sidebar shown first
        in the collapsed layout, the sheet closed)."""
        self._close_sheet()
        if self.split_view.get_collapsed() and self.split_view.get_show_content():
            self.split_view.set_show_content(False)
            GLib.idle_add(self._focus_sidebar)  # once the sidebar's page is shown
        else:
            self._focus_sidebar()

    def _focus_sidebar(self):
        focus = self.get_focus()
        if focus is None or not focus.is_ancestor(self.sidebar):
            row = self._sidebar_row(self.sidebar.get_selected())
            if row is None or not row.grab_focus():
                self.sidebar.child_focus(Gtk.DirectionType.TAB_FORWARD)
        return GLib.SOURCE_REMOVE

    def _on_focus_content(self, *_args):
        """win.focus-content: the focus on the page shown, its content first (a grid, a
        list, an entry) rather than its header bar (the content shown first in the collapsed
        layout, the sheet closed). Nothing moves when the focus is on the page already."""
        self._close_sheet()
        if self.split_view.get_collapsed() and not self.split_view.get_show_content():
            self.split_view.set_show_content(True)
            GLib.idle_add(self._focus_content)
        else:
            self._focus_content()

    def _focus_content(self):
        page = self.navigation_view.get_visible_page()
        if page is None:
            return GLib.SOURCE_REMOVE
        focus = self.get_focus()
        if focus is not None and focus.is_ancestor(page):
            return GLib.SOURCE_REMOVE
        toolbar = _descendant_of_type(page, Adw.ToolbarView)
        content = toolbar.get_content() if toolbar is not None else None
        if content is None or not content.child_focus(Gtk.DirectionType.TAB_FORWARD):
            page.child_focus(Gtk.DirectionType.TAB_FORWARD)
        return GLib.SOURCE_REMOVE

    def _on_focus_player(self, *_args):
        """win.focus-player: the focus on the play button, the Now Playing sheet's while it
        is open, else the bar's; with nothing playing, on the bar itself (it opens the
        sheet)."""
        if self.bottom_sheet.get_open():
            self.now_playing.focus_controls()
        elif not self.player_bar.play_button.grab_focus():
            self.player_bar.grab_bar_focus()

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

    def _on_expanded_timeout(self):
        self._expanded_save = None
        self._write_expanded()
        return GLib.SOURCE_REMOVE

    def _write_expanded(self):
        if self._expanded_save is not None:
            GLib.source_remove(self._expanded_save)
            self._expanded_save = None
        self._settings.set_strv('expanded-folders', sorted(self._expanded))

    def _save_window_state(self):
        """The settings the window keeps, written as it closes or hides: its size, the page
        shown (last-page) and the expanded folders. Written then rather than as they change
        (a click, a toggle): each write is a dconf round trip, and only the last matters."""
        width, height = self.get_default_size()
        self._settings.set_int('window-width', width)
        self._settings.set_int('window-height', height)
        self._settings.set_boolean('window-maximized', self.is_maximized())
        if self._shown is not None:
            self._settings.set_string('last-page', self._shown)
        self._write_expanded()

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

    def hide_for_background(self):
        """The window closes while the music plays on (background playback): its dialogs are
        closed, its state remembered, and it hides, to be presented again as it was."""
        dialog = self.get_visible_dialog()
        if dialog is not None:
            dialog.force_close()
        # A dialog shown as a window of its own (this one neither maximized nor tiled) is
        # not among this window's dialogs: its window is transient for this one.
        for toplevel in Gtk.Window.list_toplevels():
            if toplevel is not self and toplevel.get_transient_for() is self:
                toplevel.close()
        self._save_window_state()
        self.set_visible(False)

    def do_close_request(self):
        # Closing the last window quits, and quitting stops the engine first: the window
        # stays (hidden) until the app has, so the close is declined here. With background
        # playback on and something playing, the app hides the window instead
        # (Application.close_window).
        self.get_application().close_window(self)
        return True


def _descendants_of_type(widget, cls):
    """The descendants of widget that are a cls, depth first (not looking inside them)."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, cls):
            yield child
        else:
            yield from _descendants_of_type(child, cls)
        child = child.get_next_sibling()


def _descendant_of_type(widget, cls):
    """The first descendant of widget (depth first, itself included) that is a cls."""
    if isinstance(widget, cls):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _descendant_of_type(child, cls)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def _in_popover(widget):
    """Whether widget is inside a popover (a menu): its keys are the menu's."""
    while widget is not None:
        if isinstance(widget, Gtk.Popover):
            return True
        widget = widget.get_parent()
    return False
