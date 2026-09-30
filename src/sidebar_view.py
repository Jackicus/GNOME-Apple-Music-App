# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The window's sidebar: its sections and items, the Playlists section following the library,
the selection, the folders' expansion, the playlists' context menu and drop target, and the
rows' accessible names.

    sidebar = SidebarController(window, sidebar_widget, library, settings, item_actions)
    sidebar.select(key)               # select key's item (Home's when there is none), show its page
    sidebar.select(key, show=False)   # select it and show nothing (the caller does)
    sidebar.reveal_selected()         # scroll the selected row into view (after the above)
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

Folders: a click (or Enter) on a folder shows its page and, in the sidebar mode, opens or
closes it; in the page mode (the collapsed layout) it only drills down, and the folder's
arrow is hidden (the row has one of its own). Right and Left on a focused folder open and
close it without changing the page, Left on a nested item moves to its folder. A track
dragged over a closed folder opens it after a moment (SPRING_DELAY_MS), so a playlist inside
can take the drop. The open folders are kept in the expanded-folders setting, written a
moment after the last change (EXPANDED_WRITE_DELAY_MS) and when the window closes.
"""

import logging
import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import sections, shortcuts
from .library import TrackRef
from .sections import FAVOURITE_SONGS, HOME, PLAYLISTS_SECTION
from .sidebar import (SidebarEntry, SidebarItem, is_shown, plan_update, playlist_entries,
                      reveal)
from .widgets.util import descendants

log = logging.getLogger(__name__)

# How long a track dragged over a closed folder hovers before the folder opens.
SPRING_DELAY_MS = 500
# How long after the last change the expanded folders are written to the settings.
EXPANDED_WRITE_DELAY_MS = 1000
# How often, and how many times at most, reveal_selected() looks again for the rows to be
# laid out (a fresh window, a mode change building new rows).
REVEAL_RETRY_MS = 50
REVEAL_TRIES = 60


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
        self._expanded_write = None  # the timeout that will write expanded-folders
        self._spring = None  # the timeout that will open the folder a drag hovers over
        self._reveal = None  # the idle scrolling the selected row into view, and its tries
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
        # The page mode (the collapsed layout) builds new rows: named again once built,
        # and the folders' arrows shown only in the sidebar mode.
        self._sidebar.connect('notify::mode', self._on_mode_changed)
        self._sidebar.connect('activated', self._on_activated)
        self._sidebar.connect('setup-menu', self._on_setup_menu)
        # Tracks dragged from a list (widgets/context_menu.py) drop onto playlists.
        self._sidebar.setup_drop_target(Gdk.DragAction.COPY, [TrackRef])
        self._sidebar.connect('drop-enter', self._on_drop_enter)
        self._sidebar.connect('drop', self._on_drop)
        motion = Gtk.DropControllerMotion()
        motion.connect('leave', lambda *_: self._cancel_spring())
        self._sidebar.add_controller(motion)
        # Right and Left on a focused row (shortcuts.FOLDER_OPEN and FOLDER_CLOSE): a folder
        # opens and closes, a nested item goes to its folder. Only with the focus in the
        # sidebar (a bubble-phase controller).
        keys = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.BUBBLE)
        for accel, expand in ((shortcuts.FOLDER_OPEN, True), (shortcuts.FOLDER_CLOSE, False)):
            keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(accel),
                Gtk.CallbackAction.new(lambda _widget, _args, expand=expand: self._on_arrow(
                    expand))))
        self._sidebar.add_controller(keys)

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

    def _page_mode(self):
        return self._sidebar.get_mode() == Adw.SidebarMode.PAGE

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
            self._schedule_expanded_write()
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
        did, unless it was selected while a restored page waited for the library), back at
        its root when it is the page shown already in the wide layout (the pages pushed on
        it are popped, as the web player's sidebar does), show the content when the layout
        is collapsed, and in the sidebar mode open or close a folder (in the page mode a
        folder only drills down: its page lists what it holds)."""
        item = sidebar.get_item(index)
        key = self.key_of(item)
        collapsed = self._window.split_view.get_collapsed()
        if key is not None:
            if key != self._window.shown:
                self._window.show_root(key)
            elif not collapsed:
                self._window.show_root(key, pop=True)
        self._window.split_view.set_show_content(True)
        if (isinstance(item, SidebarItem) and item.entry.kind == 'folder'
                and not self._page_mode()):
            folder_id = item.entry.folder_id
            self._set_expanded(folder_id, folder_id not in self._expanded)

    def _on_arrow(self, expand):
        """Right (expand) or Left on the focused row: a folder opens or closes without a
        page change; Left on a nested item moves to its folder. True when handled."""
        focus = self._window.get_focus()
        rows = self.rows()
        if focus is None or focus not in rows:
            return False
        item = self._sidebar.get_items().get_item(rows.index(focus))
        if not isinstance(item, SidebarItem):
            return False
        entry = item.entry
        if entry.kind == 'folder' and (entry.folder_id in self._expanded) != expand:
            self._set_expanded(entry.folder_id, expand)
            return True
        if not expand and entry.ancestors:
            parent = self.item_for(f'folder:{entry.ancestors[-1]}')
            row = self.row(parent.get_index()) if parent is not None else None
            return row is not None and row.grab_focus()
        return False

    def focus(self):
        """The focus on the selected row, or on the sidebar's first focusable widget when
        there is none. Always the selected row, not wherever the focus was in the sidebar
        before: the collapsed layout brings its page back with the row last focused there."""
        row = self.row(self._sidebar.get_selected())
        if row is None or not row.grab_focus():
            self._sidebar.child_focus(Gtk.DirectionType.TAB_FORWARD)
        return GLib.SOURCE_REMOVE

    def reveal_selected(self):
        """Scroll the selected row into view, once the rows are laid out and after the
        sidebar's own scroll on map (which centres the row, moving the list even when the
        row was in view, so the first rows were cut off in a fresh window): the list back at
        the top when the row is in view there, else the least scroll that shows it.

        The rows are measured after a paint (an idle can run between a change and the
        layout that follows it, and see a row's old place), and the scroll happens in an
        idle after libadwaita's (which centres the row as its list maps). A look that had
        to scroll looks again after the next paint, since the rows may still be settling
        (a folder's rows appearing, the page mode's lists being built), and stops at a
        look that finds the row where it should be; while the sidebar has no frame clock
        or the rows are not laid out, it tries again every REVEAL_RETRY_MS. REVEAL_TRIES
        looks at most.
        """
        if self._reveal is None:
            self._reveal = [None, 0]  # [the pending source, the tries so far]
            self._reveal_after_paint()

    def _reveal_after_paint(self):
        clock = self._sidebar.get_frame_clock()
        if clock is None:
            self._reveal_later()
            return GLib.SOURCE_REMOVE
        handler = None

        def painted(_clock):
            clock.disconnect(handler)
            self._reveal[0] = GLib.idle_add(self._reveal_now,
                                            priority=GLib.PRIORITY_DEFAULT_IDLE + 1)

        handler = clock.connect('after-paint', painted)
        self._sidebar.queue_draw()  # a frame, if none was coming
        return GLib.SOURCE_REMOVE

    def _reveal_later(self):
        if self._reveal[1] < REVEAL_TRIES:
            self._reveal[1] += 1
            self._reveal[0] = GLib.timeout_add(REVEAL_RETRY_MS, self._reveal_after_paint)
        else:
            self._reveal = None

    def _reveal_now(self):
        row = self.row(self._sidebar.get_selected())
        viewport = row.get_ancestor(Gtk.Viewport) if row is not None else None
        found, bounds, page = False, None, 0
        if row is not None and viewport is not None and row.get_mapped():
            found, bounds = row.compute_bounds(viewport)
            page = viewport.get_vadjustment().get_page_size()
        if not found or page < 0.5 or bounds.get_height() <= 0:
            self._reveal_later()
            return GLib.SOURCE_REMOVE
        adjustment = viewport.get_vadjustment()
        value = adjustment.get_value()
        top = bounds.get_y() + value  # in the list, whatever it is scrolled to
        bottom = top + bounds.get_height()
        if bottom <= page and value != 0:
            adjustment.set_value(0)
        elif top < value or bottom > value + page:
            viewport.scroll_to(row, None)
        else:
            self._reveal = None  # where it should be: done
            return GLib.SOURCE_REMOVE
        self._reveal_later()  # look again once that has been painted
        return GLib.SOURCE_REMOVE

    # -- the Playlists section -----------------------------------------------------------

    def _create_playlist_item(self, entry):
        item = SidebarItem(entry)
        item.set_visible(is_shown(entry, self._expanded))
        item.set_expanded(entry.folder_id in self._expanded)
        item.set_arrow_visible(not self._page_mode())
        return item

    def _on_mode_changed(self, *_args):
        page_mode = self._page_mode()
        for position in range(self._fixed_count, self._playlist_store.get_n_items()):
            self._playlist_section.get_item(position).set_arrow_visible(not page_mode)
        GLib.idle_add(self.update_accessibility)  # once the mode's rows are built
        self.reveal_selected()  # the new rows, centred again by libadwaita as they map

    def update_playlists(self):
        """Make the Playlists section list the library's folders and playlists, with the
        fewest changes (sidebar.plan_update): entries at the same place keep their sidebar
        item (so the selection and the focus survive a reload), with their Items swapped for
        the new load's and a renamed one retitled in place; only what moved, came or went
        is spliced (bind_model makes a new item for every entry spliced in, and the
        selection is lost when the selected item goes: the window reselects by key)."""
        started = time.perf_counter()
        entries = playlist_entries(self._library.playlist_tree())
        fixed = self._fixed_count
        old = [self._playlist_store.get_item(position)
               for position in range(fixed, self._playlist_store.get_n_items())]
        retitles, splices = plan_update(old, entries)
        new_by_key = {entry.key: entry for entry in entries}
        for kept in old:
            entry = new_by_key.get(kept.key)
            if entry is not None:
                kept.item = entry.item
        for index, entry in retitles:
            item = self._playlist_section.get_item(fixed + index)
            if isinstance(item, SidebarItem):
                item.retitle(entry.title)
            else:
                old[index].title = entry.title
        if splices:
            self._quiet = True
            try:
                for index, count, inserted in splices:
                    self._playlist_store.splice(fixed + index, count, inserted)
            finally:
                self._quiet = False
            self._positions = {entry.key: position
                               for position, entry in enumerate(self._playlist_store)}
        if splices or retitles:
            self.update_accessibility()
        if splices or retitles or log.isEnabledFor(logging.DEBUG):
            folders = [entry.folder_id for entry in entries if entry.kind == 'folder']
            log.debug('Sidebar: %d folders, %d playlists (%d retitled, %d splices) in %.1f ms; '
                      'expanded: %s', len(folders), len(entries) - len(folders), len(retitles),
                      len(splices), (time.perf_counter() - started) * 1000,
                      ', '.join(folder for folder in folders if folder in self._expanded)
                      or 'none')

    # -- the folders' expansion ----------------------------------------------------------

    def _set_expanded(self, folder_id, expanded):
        """Open or close a folder: its contents shown or hidden, the setting written soon."""
        if expanded:
            self._expanded.add(folder_id)
        else:
            self._expanded.discard(folder_id)
        self._apply_expanded()
        self._schedule_expanded_write()

    def _apply_expanded(self):
        """Show or hide the items the expanded folders hold, and turn their arrows."""
        for position in range(self._fixed_count, self._playlist_store.get_n_items()):
            entry = self._playlist_store.get_item(position)
            item = self._playlist_section.get_item(position)
            item.set_visible(is_shown(entry, self._expanded))
            item.set_expanded(entry.folder_id in self._expanded)
        self.update_accessibility()

    def _schedule_expanded_write(self):
        """Write the expanded folders EXPANDED_WRITE_DELAY_MS after the last change (a
        burst of clicks is one write; the window's close writes what is pending)."""
        if self._expanded_write is not None:
            GLib.source_remove(self._expanded_write)
        self._expanded_write = GLib.timeout_add(EXPANDED_WRITE_DELAY_MS,
                                                self._on_expanded_timeout)

    def _on_expanded_timeout(self):
        self._expanded_write = None
        self.write_expanded()
        return GLib.SOURCE_REMOVE

    def write_expanded(self):
        """Write the expanded folders to the settings now: once the library has loaded,
        only the folders it has, so the ids of folders deleted in Apple Music (or another
        library's) do not pile up in the setting."""
        if self._expanded_write is not None:
            GLib.source_remove(self._expanded_write)
            self._expanded_write = None
        if self._library.state == 'ready':
            tree = self._library.playlist_tree()
            if tree is not None:
                self._expanded &= {node.id for node in tree.folders()}
        self._settings.set_strv(self._expanded_key, sorted(self._expanded))

    def forget_expanded(self):
        """Close every folder (a sign-out: the next account's folders start closed)."""
        if self._expanded:
            self._expanded.clear()
            self._apply_expanded()
            self._schedule_expanded_write()

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
        """A drag over the item at index: the action a drop would do (COPY onto a playlist
        that takes songs, nothing elsewhere), and a closed folder starts opening
        (SPRING_DELAY_MS) so a playlist inside can take the drop."""
        self._cancel_spring()
        item = self._sidebar.get_item(index)
        if (isinstance(item, SidebarItem) and item.entry.kind == 'folder'
                and item.entry.folder_id not in self._expanded):
            self._spring = GLib.timeout_add(SPRING_DELAY_MS, self._spring_open,
                                            item.entry.folder_id)
        return Gdk.DragAction.COPY if self._drop_playlist(index) is not None else 0

    def _spring_open(self, folder_id):
        self._spring = None
        if folder_id not in self._expanded and self.item_for(f'folder:{folder_id}') is not None:
            self._set_expanded(folder_id, True)
        return GLib.SOURCE_REMOVE

    def _cancel_spring(self):
        if self._spring is not None:
            GLib.source_remove(self._spring)
            self._spring = None

    def _on_drop(self, _sidebar, index, value, _action):
        self._cancel_spring()
        playlist = self._drop_playlist(index)
        if playlist is None:
            return False
        return self._item_actions.drop(playlist, value)

    # -- the rows, for their accessible names and states ---------------------------------
    # AdwSidebar builds a row per item, hidden ones too, in item order (one Gtk.ListBox in
    # the sidebar mode, one boxed list per section in the page mode, which it builds anew
    # when the mode changes) and names none of them in the sidebar mode, nor says whether a
    # folder is expanded or how deep an item is nested. Found by walking its widgets; if
    # they ever stop matching the items one to one, nothing is set.

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
        """Name each sidebar row after its item; give the Playlists section's rows their
        level (a nested item's folder is a level up) and folders their expanded state."""
        items = self._sidebar.get_items()
        for index, row in enumerate(self.rows()):
            item = items.get_item(index)
            row.update_property([Gtk.AccessibleProperty.LABEL], [item.get_title()])
            if isinstance(item, SidebarItem):
                row.update_property([Gtk.AccessibleProperty.LEVEL], [item.entry.depth + 1])
                if item.entry.kind == 'folder':
                    # An int: the state is "true, false or undefined".
                    row.update_state([Gtk.AccessibleState.EXPANDED],
                                     [int(item.entry.folder_id in self._expanded)])
        return GLib.SOURCE_REMOVE
