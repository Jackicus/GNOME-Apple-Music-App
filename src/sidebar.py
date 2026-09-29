# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The sidebar's Playlists section as a model: All Playlists and Favourite Songs, then the user's
folders and playlists in Apple's order, and the decisions the window's sidebar controller
(sidebar_view.py) applies, as functions of plain data so that tests need no display.

The window binds the section to a Gio.ListStore of SidebarEntry (AdwSidebarSection.bind_model),
whose create function makes a SidebarItem for each. AdwSidebar cannot indent, so a folder's
contents follow it in order and its arrow says whether they are shown; the contents of a
collapsed folder are hidden items (AdwSidebarItem:visible). Keys name what an entry shows, and are
what the `last-page` setting keeps: a destination's key, "playlist:<id>" or "folder:<id>".

    plan_update(old, new)                    what a changed tree does to the section's entries
    restore_target(key, entries, ready)      what to select and show for the page last shown
    reveal(key, entries)                     the folders to expand for an entry to be listed
    stale_roots(root_keys, entries, shown)   the root pages of playlists and folders now gone
"""

from difflib import SequenceMatcher

from gi.repository import Adw, GLib, GObject, Gtk

from .sections import ALL_PLAYLISTS, HOME

FOLDER_ICON = 'folder-symbolic'
PLAYLIST_ICON = 'playlist-symbolic'  # bundled (src/icons)


def playlist_key(playlist_id):
    return f'playlist:{playlist_id}'


def folder_key(folder_id):
    return f'folder:{folder_id}'


def parse_key(key):
    """(kind, id) for a "playlist:<id>" or "folder:<id>" key; None for anything else."""
    kind, sep, item_id = (key or '').partition(':')
    if sep and item_id and kind in ('playlist', 'folder'):
        return kind, item_id
    return None


class SidebarEntry(GObject.Object):
    """One item of the Playlists section.

    `kind` is 'fixed' (a destination of sections.py: All Playlists, Favourite Songs), 'folder'
    or 'playlist'; `key` what it shows (see the module); `depth` its nesting, 0 at the top
    level; `item` the folder's or playlist's library Item (None for a fixed entry),
    `ancestors` the ids of the folders it is in, outermost first, and `parent_title` the name
    of the folder holding it ('' at the top level), shown as a nested item's subtitle since
    the sidebar cannot indent.
    """

    __gtype_name__ = 'AppleMusicSidebarEntry'

    kind = GObject.Property(type=str, default='fixed')
    key = GObject.Property(type=str, default='')
    title = GObject.Property(type=str, default='')
    icon_name = GObject.Property(type=str)
    depth = GObject.Property(type=int, default=0)

    def __init__(self, kind, key, title, icon_name, depth=0, item=None, ancestors=(),
                 parent_title=''):
        super().__init__(kind=kind, key=key, title=title, icon_name=icon_name, depth=depth)
        self.item = item
        self.ancestors = tuple(ancestors)
        self.parent_title = parent_title

    @classmethod
    def fixed(cls, destination):
        return cls('fixed', destination.key, destination.title, destination.icon_name)

    @property
    def folder_id(self):
        """The folder's id, for a folder entry; None otherwise."""
        return self.item.id if self.kind == 'folder' else None

    def place(self):
        """Where the entry sits in the tree, without its title: two entries of the same
        place show the same thing (plan_update keeps the sidebar item of one for the
        other), whatever the thing is called now."""
        return (self.kind, self.key, self.depth, self.ancestors)

    def shape(self):
        """What the sidebar shows of the entry: its place and its title."""
        return (self.kind, self.key, self.title, self.depth, self.ancestors)


def playlist_entries(tree):
    """The entries for a library.PlaylistTree's folders and playlists, depth first.

    Favourite Songs is left out wherever it is: the fixed entry of that name shows it.
    """
    entries = []
    for node in tree.flat:
        item = node.item
        parent = node.parent
        parent_title = parent.item.title if parent is not None and parent.parent is not None else ''
        if node.kind == 'folder':
            entries.append(SidebarEntry('folder', folder_key(item.id), item.title, FOLDER_ICON,
                                        node.depth, item, node.ancestors(), parent_title))
        elif not item.favourites:
            entries.append(SidebarEntry('playlist', playlist_key(item.id), item.title,
                                        PLAYLIST_ICON, node.depth, item, node.ancestors(),
                                        parent_title))
    return entries


def is_shown(entry, expanded):
    """Whether the entry is listed: every folder it is in is expanded (ids in `expanded`)."""
    return all(folder_id in expanded for folder_id in entry.ancestors)


def plan_update(old, new):
    """What makes the section list `new` in place of `old` (two lists of SidebarEntry) with
    the fewest changes: (retitles, splices).

    Entries at the same place (SidebarEntry.place) are the same thing: the old entry keeps
    its sidebar item, so the selection and the focus survive a reload, and only takes the
    new title when that changed. `retitles` is [(index, entry)]: old's entry at index takes
    entry's title. `splices` is [(index, count, entries)], last first: `count` of old's
    entries from index make way for `entries`. Apply the retitles first (their indexes are
    old's), then the splices in the order given, so each leaves the earlier indexes as they
    were. Two lists of the same places and titles need nothing.
    """
    matcher = SequenceMatcher(None, [entry.place() for entry in old],
                              [entry.place() for entry in new], autojunk=False)
    retitles = []
    splices = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            retitles.extend((i1 + n, new[j1 + n]) for n in range(i2 - i1)
                            if old[i1 + n].title != new[j1 + n].title)
        else:
            splices.append((i1, i2 - i1, new[j1:j2]))
    splices.reverse()
    return retitles, splices


def restore_target(key, entries_by_key, library_ready):
    """What to select and show for the page last shown (`key`, the last-page setting) as the
    window opens: (the key to select, the key to show).

    The key's own when the sidebar has it (`entries_by_key` maps every key it has). A
    playlist's or folder's key while the library has not loaded is shown under All
    Playlists, selected meanwhile: the library's load selects it, or goes Home when it is
    gone. A key nobody has otherwise (a playlist gone, a destination that is no more) is
    Home.
    """
    if key in entries_by_key:
        return key, key
    if parse_key(key) is not None and not library_ready:
        return ALL_PLAYLISTS, key
    return HOME, HOME


def reveal(key, entries_by_key):
    """The ids of the folders to expand for key's entry to be listed: the folders it is in,
    outermost first; none for a key the section has not got, or an entry at the top."""
    entry = entries_by_key.get(key)
    return tuple(getattr(entry, 'ancestors', ()))


def stale_roots(root_keys, entries_by_key, shown):
    """The playlist and folder keys among `root_keys` (the root pages kept) whose entries
    the library has no more, except the one shown: its page says that it is gone."""
    return [key for key in root_keys
            if parse_key(key) is not None and key not in entries_by_key and key != shown]


def tooltip_markup(title):
    """A playlist's or folder's title as the row's tooltip, which is Pango markup: escaped,
    so "A & B <C>" reads as it is. The whole name, where the row ellipsizes it."""
    return GLib.markup_escape_text(title or '')


class SidebarItem(Adw.SidebarItem):
    """A sidebar item made from a SidebarEntry, which it keeps as `entry`. A folder's has a
    disclosure arrow as its suffix, pointing down while expanded, hidden in the page mode
    (where a folder only drills down and the row has an arrow of its own); a nested item
    names its folder as its subtitle; a playlist's or folder's tooltip is its whole name."""

    __gtype_name__ = 'AppleMusicSidebarItem'

    def __init__(self, entry):
        # Not activated by a drag hovering over it: a track is dropped onto a playlist, and
        # switching pages under the drag would take the list it came from away.
        super().__init__(title=entry.title, icon_name=entry.icon_name,
                         subtitle=entry.parent_title or None, drag_motion_activate=False)
        self.entry = entry
        self._arrow = None
        if entry.kind != 'fixed':
            self.set_tooltip(tooltip_markup(entry.title))
        if entry.kind == 'folder':
            self._arrow = Gtk.Image(icon_name='pan-end-symbolic',
                                    accessible_role=Gtk.AccessibleRole.PRESENTATION)
            self.set_suffix(self._arrow)

    def retitle(self, title):
        """The item's title (and tooltip) after its playlist or folder was renamed."""
        self.entry.title = title
        self.set_title(title)
        if self.entry.kind != 'fixed':
            self.set_tooltip(tooltip_markup(title))

    def set_expanded(self, expanded):
        if self._arrow is not None:
            self._arrow.set_from_icon_name('pan-down-symbolic' if expanded else 'pan-end-symbolic')

    def set_arrow_visible(self, visible):
        if self._arrow is not None:
            self._arrow.set_visible(visible)
