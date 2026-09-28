"""The sidebar's Playlists section as a model: All Playlists and Favourite Songs, then the user's
folders and playlists in Apple's order.

The window binds the section to a Gio.ListStore of SidebarEntry (AdwSidebarSection.bind_model),
whose create function makes a SidebarItem for each. AdwSidebar cannot indent, so a folder's
contents follow it in order and its arrow says whether they are shown; the contents of a
collapsed folder are hidden items (AdwSidebarItem:visible). Keys name what an entry shows, and are
what the `last-page` setting keeps: a destination's key, "playlist:<id>" or "folder:<id>".
"""

from gi.repository import Adw, GObject, Gtk

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
    level; `item` the folder's or playlist's library Item (None for a fixed entry), and
    `ancestors` the ids of the folders it is in, outermost first.
    """

    __gtype_name__ = 'AppleMusicSidebarEntry'

    kind = GObject.Property(type=str, default='fixed')
    key = GObject.Property(type=str, default='')
    title = GObject.Property(type=str, default='')
    icon_name = GObject.Property(type=str)
    depth = GObject.Property(type=int, default=0)

    def __init__(self, kind, key, title, icon_name, depth=0, item=None, ancestors=()):
        super().__init__(kind=kind, key=key, title=title, icon_name=icon_name, depth=depth)
        self.item = item
        self.ancestors = tuple(ancestors)

    @classmethod
    def fixed(cls, destination):
        return cls('fixed', destination.key, destination.title, destination.icon_name)

    @property
    def folder_id(self):
        """The folder's id, for a folder entry; None otherwise."""
        return self.item.id if self.kind == 'folder' else None

    def shape(self):
        """What the sidebar shows of the entry: two lists of entries with the same shapes need
        no new sidebar items, only their `item`s swapped."""
        return (self.kind, self.key, self.title, self.depth, self.ancestors)


def playlist_entries(tree):
    """The entries for a library.PlaylistTree's folders and playlists, depth first.

    Favourite Songs is left out wherever it is: the fixed entry of that name shows it.
    """
    entries = []
    for node in tree.flat:
        item = node.item
        if node.kind == 'folder':
            entries.append(SidebarEntry('folder', folder_key(item.id), item.title, FOLDER_ICON,
                                        node.depth, item, node.ancestors()))
        elif not item.favourites:
            entries.append(SidebarEntry('playlist', playlist_key(item.id), item.title,
                                        PLAYLIST_ICON, node.depth, item, node.ancestors()))
    return entries


def is_shown(entry, expanded):
    """Whether the entry is listed: every folder it is in is expanded (ids in `expanded`)."""
    return all(folder_id in expanded for folder_id in entry.ancestors)


class SidebarItem(Adw.SidebarItem):
    """A sidebar item made from a SidebarEntry, which it keeps as `entry`. A folder's has a
    disclosure arrow as its suffix, pointing down while expanded."""

    __gtype_name__ = 'AppleMusicSidebarItem'

    def __init__(self, entry):
        # Not activated by a drag hovering over it: a track is dropped onto a playlist, and
        # switching pages under the drag would take the list it came from away.
        super().__init__(title=entry.title, icon_name=entry.icon_name,
                         drag_motion_activate=False)
        self.entry = entry
        self._arrow = None
        if entry.kind == 'folder':
            self._arrow = Gtk.Image(icon_name='pan-end-symbolic',
                                    accessible_role=Gtk.AccessibleRole.PRESENTATION)
            self.set_suffix(self._arrow)

    def set_expanded(self, expanded):
        if self._arrow is not None:
            self._arrow.set_from_icon_name('pan-down-symbolic' if expanded else 'pan-end-symbolic')
