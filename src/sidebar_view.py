"""The window's sidebar: its sections and items, the Playlists section following the library,
the selection, the folders' expansion, the playlists' context menu and drop target, and the
rows' accessible names.

    sidebar = SidebarController(window, sidebar_widget, library, settings, item_actions)
    sidebar.select(key)               # select key's item (Home's when there is none), show its page
    sidebar.select(key, show=False)   # select it and show nothing (the caller does)
    sidebar.selected_key()            # the key of the item selected, or None
    sidebar.entries_by_key()          # key -> its Destination or SidebarEntry (for sidebar.py)
    sidebar.destination(key)          # a fixed destination's Destination, or None
    sidebar.entry(key)                # a playlist's or folder's SidebarEntry, or None
    sidebar.update_playlists()        # the Playlists section follows the library (after `changed`)
    sidebar.focus()                   # the focus on the selected row
    sidebar.forget_expanded()         # every folder closed (sign-out)
    sidebar.write_expanded()          # the expanded folders written now (the window closing)

The first sections are fixed items; the Playlists section is bound to a store of SidebarEntry
(sidebar.py): its two destinations, then the library's folders and playlists. The window shows
pages: the controller calls `window.show_root(key)` when the user selects or activates an
item, and reads `window.shown` for the key whose page is at the bottom of the navigation
stack. The decisions (which entries a changed tree keeps, what a restored key selects) are
sidebar.py's functions; this applies them to the widget. The AdwSidebar behaviour this leans
on is listed in .claude/rules/sidebar.md.
"""

import logging
import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import sections
from .actions import TrackRef
from .sections import FAVOURITE_SONGS, HOME, PLAYLISTS_SECTION
from .sidebar import SidebarEntry, SidebarItem, is_shown, playlist_entries, reveal
from .widgets.util import descendants

log = logging.getLogger(__name__)


class SidebarController:
    """The window's sidebar. See the module."""

    def __init__(self, window, sidebar, library, settings, item_actions):
        self._window = window
        self._sidebar = sidebar
        self._library = library
        self._settings = settings
        self._item_actions = item_actions
        self._expanded_key = window.account_key('expanded-folders')
        self._destinations = {}  # the fixed sections' Adw.SidebarItem -> Destination
        self._destination_keys = {}  # key -> Destination, every fixed destination
        self._items_by_key = {}  # key -> Adw.SidebarItem, the fixed sections'
        self._positions = {}  # sidebar key -> position in the Playlists section
        self._expanded = set(settings.get_strv(self._expanded_key))
        self._quiet = False  # the selection changes, but not by the user: show nothing
        self._expanded_save = None  # the timeout that will write expanded-folders
        self._build()
        self.update_playlists()

    def _build(self):
        for spec in sections.sidebar_sections():
            section = Adw.SidebarSection(title=spec.title)
            for destination in spec.destinations:
                self._destination_keys[destination.key] = destination
            if spec.id == PLAYLISTS_SECTION:
                self._playlist_section = section
                self._playlist_store = Gio.ListStore(item_type=SidebarEntry)
                self._playlist_store.splice(
                    0, 0, [SidebarEntry.fixed(destination) for destination in spec.destinations])
                self._fixed_count = len(spec.destinations)
                self._positions = {destination.key: position
                                   for position, destination in enumerate(spec.destinations)}
                section.bind_model(self._playlist_store, self._create_playlist_item)
                # The playlists' context menu: filled for the item it opens on (setup-menu).
                self._sidebar_menu = Gio.Menu()
                section.set_menu_model(self._sidebar_menu)
            else:
                for destination in spec.destinations:
                    item = Adw.SidebarItem(title=destination.title,
                                           icon_name=destination.icon_name,
                                           drag_motion_activate=False)
                    section.append(item)
                    self._destinations[item] = destination
                    self._items_by_key[destination.key] = item
            self._sidebar.append(section)

        self._sidebar.connect('notify::selected-item', self._on_selected_item)
        # The page mode (the collapsed layout) builds new rows: named again once built.
        self._sidebar.connect('notify::mode', lambda *_: GLib.idle_add(
            self.update_accessibility))
        self._sidebar.connect('activated', self._on_activated)
        self._sidebar.connect('setup-menu', self._on_setup_menu)
        # Tracks dragged from a list (widgets/context_menu.py) drop onto playlists.
        self._sidebar.setup_drop_target(Gdk.DragAction.COPY, [TrackRef])
        self._sidebar.connect('drop-enter', self._on_drop_enter)
        self._sidebar.connect('drop', self._on_drop)

    # -- what the sidebar has ------------------------------------------------------------

    def destination(self, key):
        """The Destination of a fixed key, or None."""
        return self._destination_keys.get(key)

    def entry(self, key):
        """The SidebarEntry of a playlist's or folder's key (or a fixed entry of the
        Playlists section), or None."""
        position = self._positions.get(key)
        return self._playlist_store.get_item(position) if position is not None else None

    def entries_by_key(self):
        """Every key the sidebar has -> its Destination (a fixed item) or SidebarEntry (the
        Playlists section's)."""
        known = dict(self._destination_keys)
        known.update((entry.key, entry) for entry in self._playlist_store)
        return known

    def item_for(self, key):
        """The Adw.SidebarItem showing key, or None."""
        item = self._items_by_key.get(key)
        if item is None:
            position = self._positions.get(key)
            if position is not None:
                item = self._playlist_section.get_item(position)
        return item

    def key_of(self, item):
        """The key of a sidebar item: its destination's, or its entry's."""
        if isinstance(item, SidebarItem):
            return item.entry.key
        destination = self._destinations.get(item)
        return destination.key if destination is not None else None

    def selected_key(self):
        return self.key_of(self._sidebar.get_selected_item())

    # -- the selection -------------------------------------------------------------------

    def select(self, key, show=True, pop=False):
        """Select the sidebar item for key, or Home's when there is none, and show its page
        (`show`; else the caller does), over anything pushed on it when `pop` (the user
        asked for the page itself). The key selected.

        An item in a collapsed folder is shown first: its folders are expanded.
        """
        item = self.item_for(key)
        if item is None:
            key = HOME
            item = self._items_by_key[key]
        elif not item.get_visible():
            self._expanded.update(reveal(key, self.entries_by_key()))
            self._apply_expanded()
        self._set_selected(item.get_index())
        if show:
            self._window.show_root(key, pop=pop)
        return key

    def _set_selected(self, index):
        """Select the sidebar item at index (or none), showing nothing: the caller does."""
        self._quiet = True
        try:
            self._sidebar.set_selected(index)
        finally:
            self._quiet = False

    def _on_selected_item(self, sidebar, _pspec):
        if self._quiet:
            return
        key = self.key_of(sidebar.get_selected_item())
        if key is not None:
            self._window.show_root(key)

    def _on_activated(self, sidebar, index):
        """A click (or Enter) on an item, selected already or not: show its page (selecting it
        did, unless it was selected while a restored page waited for the library), show the
        content when the layout is collapsed, and open or close a folder."""
        item = sidebar.get_item(index)
        key = self.key_of(item)
        if key is not None and key != self._window.shown:
            self._window.show_root(key)
        self._window.split_view.set_show_content(True)
        if isinstance(item, SidebarItem) and item.entry.kind == 'folder':
            folder_id = item.entry.folder_id
            if folder_id in self._expanded:
                self._expanded.discard(folder_id)
            else:
                self._expanded.add(folder_id)
            self._apply_expanded()

    def focus(self):
        """The focus on the selected row (unless it is in the sidebar already), or on the
        sidebar's first focusable widget."""
        focus = self._window.get_focus()
        if focus is None or not focus.is_ancestor(self._sidebar):
            row = self.row(self._sidebar.get_selected())
            if row is None or not row.grab_focus():
                self._sidebar.child_focus(Gtk.DirectionType.TAB_FORWARD)
        return GLib.SOURCE_REMOVE

    # -- the Playlists section -----------------------------------------------------------

    def _create_playlist_item(self, entry):
        item = SidebarItem(entry)
        item.set_visible(is_shown(entry, self._expanded))
        item.set_expanded(entry.folder_id in self._expanded)
        return item

    def update_playlists(self):
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
        self.update_accessibility()
        folders = [entry.folder_id for entry in entries if entry.kind == 'folder']
        log.debug('Sidebar: %d folders, %d playlists in %.1f ms; expanded: %s', len(folders),
                  len(entries) - len(folders), (time.perf_counter() - started) * 1000,
                  ', '.join(folder for folder in folders if folder in self._expanded) or 'none')

    # -- the folders' expansion ----------------------------------------------------------

    def _apply_expanded(self):
        """Show or hide the items the expanded folders hold, and store the folders, a second
        after the last toggle (and when the window closes), rather than at every click."""
        if self._expanded_save is None:
            self._expanded_save = GLib.timeout_add_seconds(1, self._on_expanded_timeout)
        for position in range(self._fixed_count, self._playlist_store.get_n_items()):
            entry = self._playlist_store.get_item(position)
            item = self._playlist_section.get_item(position)
            item.set_visible(is_shown(entry, self._expanded))
            item.set_expanded(entry.folder_id in self._expanded)
        self.update_accessibility()

    def _on_expanded_timeout(self):
        self._expanded_save = None
        self.write_expanded()
        return GLib.SOURCE_REMOVE

    def write_expanded(self):
        """Write the expanded folders to the settings now."""
        if self._expanded_save is not None:
            GLib.source_remove(self._expanded_save)
            self._expanded_save = None
        self._settings.set_strv(self._expanded_key, sorted(self._expanded))

    def forget_expanded(self):
        """Close every folder (a sign-out: the next account's folders start closed)."""
        if self._expanded:
            self._expanded.clear()
            self._apply_expanded()

    # -- the playlists' context menu -----------------------------------------------------

    def _playlist_of(self, item):
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
        self._item_actions.fill_sidebar_menu(self._sidebar_menu, self._playlist_of(item))
        if not self._sidebar_menu.get_n_items():
            GLib.idle_add(self._close_menu, priority=GLib.PRIORITY_HIGH)

    def _close_menu(self):
        # The popover is the sidebar's own child (libadwaita 1.9 parents its context menu
        # to the sidebar itself); if a later version moves it, an empty menu shows instead.
        child = self._sidebar.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Popover) and child.get_visible():
                child.popdown()
            child = child.get_next_sibling()
        return GLib.SOURCE_REMOVE

    # -- drops ---------------------------------------------------------------------------

    def _drop_playlist(self, index):
        """The playlist a track dropped on the item at index goes to: only a playlist entry
        that takes songs (not a folder, not a fixed item)."""
        item = self._sidebar.get_item(index)
        if not isinstance(item, SidebarItem) or item.entry.kind != 'playlist':
            return None
        playlist = item.entry.item
        return playlist if self._item_actions.can_drop(playlist) else None

    def _on_drop_enter(self, _sidebar, index):
        return Gdk.DragAction.COPY if self._drop_playlist(index) is not None else 0

    def _on_drop(self, _sidebar, index, value, _action):
        playlist = self._drop_playlist(index)
        if playlist is None:
            return False
        return self._item_actions.drop(playlist, value)

    # -- the rows, for their accessible names and states ---------------------------------
    # AdwSidebar builds a row per item, hidden ones too, in item order (one Gtk.ListBox in
    # the sidebar mode, one boxed list per section in the page mode, which it builds anew
    # when the mode changes) and names none of them in the sidebar mode, nor says whether a
    # folder is expanded. Found by walking its widgets; if they ever stop matching the items
    # one to one, nothing is set.

    def rows(self):
        rows = []
        for box in descendants(self._sidebar, Gtk.ListBox):
            index = 0
            while (row := box.get_row_at_index(index)) is not None:
                rows.append(row)
                index += 1
        return rows if len(rows) == self._sidebar.get_items().get_n_items() else []

    def row(self, index):
        rows = self.rows()
        return rows[index] if 0 <= index < len(rows) else None

    def update_accessibility(self):
        """Name each sidebar row after its item, and give folders their expanded state."""
        items = self._sidebar.get_items()
        for index, row in enumerate(self.rows()):
            item = items.get_item(index)
            row.update_property([Gtk.AccessibleProperty.LABEL], [item.get_title()])
            if isinstance(item, SidebarItem) and item.entry.kind == 'folder':
                # An int: the state is "true, false or undefined".
                row.update_state([Gtk.AccessibleState.EXPANDED],
                                 [int(item.entry.folder_id in self._expanded)])
        return GLib.SOURCE_REMOVE
