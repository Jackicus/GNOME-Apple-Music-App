"""The library as GObject models the UI binds to, loaded from the cache's library.json.

library.json holds the Item and Track shapes src/backend/README.md describes. Library.load()
parses it in a thread and wraps it on the main thread in batches, so pages fill while frames
keep painting. Items become Item objects in one Gio.ListStore per section; an Item's `groups`
and the Songs store are wrapped only when first asked for. SongOrder sorts the Songs table.
GLib, GObject and Gio only, no GTK, so the model works in tests without a display.

Besides the README's shape, library.json may hold `folders`, the user's playlist folders (an
optional key, like `sections.songs` and `sections.videos`; the demo and the sync write it): a
list of {id, title, parent, children}, `parent` a folder id or null and `children` a list of
{kind: "folder" | "playlist", id} in Apple's order. The entry with id "root" lists what is in
no folder. PlaylistTree reads it. `sections.songs` holds Track dicts: the library's loose songs,
which the Songs store takes after the albums' tracks, by id. `sections.videos` holds Items of
kind 'video'.

load() makes new objects for everything; reload() (after a sync) reads the file again and brings
the models up to date in place: an Item or Track still in the library keeps its object, so open
pages and scroll positions survive.
"""

import asyncio
import difflib
import json
import locale
import logging
import time
import unicodedata

from gi.repository import Gio, GLib, GObject

from .backend import config

log = logging.getLogger(__name__)

# The sections of library.json and the stores they fill.
SECTIONS = ('albums', 'artists', 'playlists', 'radio', 'videos')

BATCH_SIZE = 500

# How long wrapping the Songs store runs before it lets GTK paint a frame, in seconds.
FRAME_BUDGET = 0.008

# The key in a playlist's raw `attributes` that marks it as Favourite Songs, the playlist of the
# songs the user loves. Not part of the README's Item shape: the demo library sets it, and the
# sync maps Apple's own flag onto it.
FAVOURITES = 'isFavourites'

# The id of the folders entry that lists the top level: the playlists and folders in no folder.
ROOT_FOLDER = 'root'


def _text(value):
    """A string property's value: None stays None (JSON null), anything else becomes a str."""
    return value if value is None or isinstance(value, str) else str(value)


def _number(value):
    """An int property's value: JSON null (or anything not a number) is 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def model_property(name, type, default=None):
    """A read-write GObject property kept in the plain attribute `_<name>`.

    Wrapping assigns the attributes directly: handing properties to GObject.Object.__init__
    crosses into C and back into Python for each one, which made a Track cost about 50 µs
    instead of under 10 (a second and a half for 30,000 songs). Reads from GTK (a
    Gtk.PropertyExpression) and later writes (which notify) go through the getter and setter.
    """
    attribute = '_' + name

    def getter(self):
        return getattr(self, attribute)

    def setter(self, value):
        setattr(self, attribute, value)

    return GObject.Property(type=type, default=default, getter=getter, setter=setter)


class Track(GObject.Object):
    """One entry of a group: the Track shape.

    `thumb` is the track's own thumbnail (a playlist's rows) or else its album's, and `play` is
    its group's play target, the queue `index` counts in: a row plays play with start-with index.
    """

    __gtype_name__ = 'AppleMusicTrack'

    id = model_property('id', str)
    catalog_id = model_property('catalog_id', str)
    title = model_property('title', str, '')
    artist = model_property('artist', str, '')
    album = model_property('album', str, '')
    track_number = model_property('track_number', int, 0)
    disc_number = model_property('disc_number', int, 0)
    duration_ms = model_property('duration_ms', int, 0)
    duration_label = model_property('duration_label', str, '')
    explicit = model_property('explicit', bool, False)
    index = model_property('index', int, 0)
    thumb = model_property('thumb', str)

    def __init__(self, data, play=None, thumb=None):
        super().__init__()
        self._id = _text(data.get('id'))
        self._catalog_id = _text(data.get('catalogId'))
        self._title = _text(data.get('title')) or ''
        self._artist = _text(data.get('artist')) or ''
        self._album = _text(data.get('album')) or ''
        self._track_number = _number(data.get('trackNumber'))
        self._disc_number = _number(data.get('discNumber'))
        self._duration_ms = _number(data.get('durationMs'))
        self._duration_label = _text(data.get('durationLabel')) or ''
        self._explicit = bool(data.get('explicit'))
        self._index = _number(data.get('index'))
        self._thumb = _text(data.get('thumb')) or thumb
        self.raw = data
        self.play = play or {}
        self._search_key = None

    @property
    def search_key(self):
        """Title, artist and album folded (fold()) on lines of their own: what the Songs filter
        looks for a folded search in. A search cannot hold a line break, so no match spans two
        of them. Made on first use and kept."""
        if self._search_key is None:
            self._search_key = fold(f'{self._title}\n{self._artist}\n{self._album}')
        return self._search_key


class Group(GObject.Object):
    """A disc of an album, a playlist's one list, an album of an artist: {name, play, entries}.

    `entries` is a Gio.ListStore of Track.
    """

    __gtype_name__ = 'AppleMusicGroup'

    name = model_property('name', str, '')

    def __init__(self, data, thumb=None):
        super().__init__()
        self._name = _text(data.get('name')) or ''
        self.raw = data
        self.play = data.get('play') or {}
        self.entries = Gio.ListStore(item_type=Track)
        self.entries.splice(0, 0, [Track(entry, self.play, thumb)
                                   for entry in _dicts(data.get('entries'))])


class Item(GObject.Object):
    """An album, artist, playlist, station, video or song: the Item shape.

    `play` is the dict a play command takes and `raw` the source dict. `groups` is a list of
    Group, wrapped from raw on first access. A playlist folder is an Item too, of kind 'folder'
    (made by PlaylistTree from a `folders` entry, with no artwork and no groups), so a grid of a
    folder's contents shows folders and playlists alike.
    """

    __gtype_name__ = 'AppleMusicItem'

    id = model_property('id', str)
    kind = model_property('kind', str)
    title = model_property('title', str, '')
    subtitle = model_property('subtitle', str, '')
    year = model_property('year', int, 0)
    genre = model_property('genre', str)
    summary = model_property('summary', str)
    art = model_property('art', str)
    thumb = model_property('thumb', str)
    art_color = model_property('art_color', str)
    count_label = model_property('count_label', str, '')
    explicit = model_property('explicit', bool, False)
    catalog_id = model_property('catalog_id', str)
    url = model_property('url', str)

    def __init__(self, data):
        super().__init__()
        self._id = _text(data.get('id'))
        self._kind = _text(data.get('kind'))
        self._title = _text(data.get('title')) or ''
        self._subtitle = _text(data.get('subtitle')) or ''
        self._year = _number(data.get('year'))
        self._genre = _text(data.get('genre'))
        self._summary = _text(data.get('summary'))
        self._art = _text(data.get('art'))
        self._thumb = _text(data.get('thumb'))
        self._art_color = _text(data.get('artColor'))
        self._count_label = _text(data.get('countLabel')) or ''
        self._explicit = bool(data.get('explicit'))
        self._catalog_id = _text(data.get('catalogId'))
        self._url = _text(data.get('url'))
        self.raw = data
        self.play = data.get('play') or {}
        self._groups = None

    @property
    def favourites(self):
        """Whether this is the Favourite Songs playlist: attributes.isFavourites in the raw Item."""
        attributes = self.raw.get('attributes')
        return isinstance(attributes, dict) and attributes.get(FAVOURITES) is True

    @property
    def groups(self):
        if self._groups is None:
            # An album's tracks draw the album's cover; an artist's groups are albums, whose
            # covers the artist Item does not have.
            thumb = self._thumb if self._kind == 'album' else None
            self._groups = [Group(group, thumb) for group in _dicts(self.raw.get('groups'))]
        return self._groups

    # The properties an `item` answer refreshes, with the raw key each reads.
    _MERGED = (('title', 'title'), ('subtitle', 'subtitle'), ('genre', 'genre'),
               ('summary', 'summary'), ('art', 'art'), ('thumb', 'thumb'),
               ('art_color', 'artColor'), ('count_label', 'countLabel'),
               ('catalog_id', 'catalogId'), ('url', 'url'))

    def merge(self, data, replace=False):
        """Take a fresh Item dict for this item into this object: the raw dict updated (or
        replaced by `data`, for a reload's complete dict), the properties that changed set (so
        bound widgets follow), and the groups wrapped again on the next access when they
        differ from the ones in hand (the same groups keep their Group and Track objects).
        An engine item() answer (the same shape, with `groups`) merges; a sync's dict replaces.
        True when a property changed."""
        groups_changed = 'groups' in data and data.get('groups') != self.raw.get('groups')
        if replace:
            self.raw = data
        else:
            for key, value in data.items():
                if key in ('id', 'kind'):
                    continue
                self.raw[key] = value
        changed = False
        for name, key in self._MERGED:
            if replace or key in data:
                value = _text(data.get(key))
                if name in ('title', 'subtitle', 'count_label'):
                    value = value or ''
                if value != getattr(self, '_' + name):
                    setattr(self, name, value)  # the property's setter: it notifies
                    changed = True
        if (replace or 'year' in data) and _number(data.get('year')) != self._year:
            self.year = _number(data.get('year'))
            changed = True
        if (replace or 'explicit' in data) and bool(data.get('explicit')) != self._explicit:
            self.explicit = bool(data.get('explicit'))
            changed = True
        if data.get('play'):
            self.play = data['play']
        if groups_changed:
            self._groups = None
        return changed


class Shelf(GObject.Object):
    """A titled row of Items: Apple's home page recommendations, Heavy Rotation, Recently Added.

    Its GType is AppleMusicShelfModel: AppleMusicShelf is the widget that shows one
    (widgets/shelf.py).
    """

    __gtype_name__ = 'AppleMusicShelfModel'

    key = model_property('key', str, '')
    title = model_property('title', str, '')

    def __init__(self, key, title, items):
        super().__init__()
        self._key = key
        self._title = title
        self.items = Gio.ListStore(item_type=Item)
        self.items.splice(0, 0, items)

    def update(self, title, items):
        """A reload's version of this shelf: the title set if it changed, the store brought
        to `items` with the fewest changes (the same objects in the same order change
        nothing)."""
        if title != self._title:
            self.title = title
        apply_diff(self.items, items)


class TreeNode:
    """A folder or a playlist in the PlaylistTree.

    `item` is its Item: the playlist's own, from the playlists section, or the folder's (kind
    'folder'). `depth` is 0 at the top level (the root's is -1), `parent` the TreeNode of the
    folder holding it (None for the root), and a folder's `children` are its nodes in Apple's
    order, as `store` holds their Items for a grid.
    """

    __slots__ = ('item', 'depth', 'parent', 'children', 'store')

    def __init__(self, item, depth, parent):
        self.item = item
        self.depth = depth
        self.parent = parent
        self.children = []
        self.store = None

    @property
    def kind(self):
        return self.item.kind

    @property
    def id(self):
        return self.item.id

    def ancestors(self):
        """The ids of the folders holding this node, outermost first, the root left out."""
        ids = []
        node = self.parent
        while node is not None and node.parent is not None:
            ids.append(node.item.id)
            node = node.parent
        ids.reverse()
        return ids

    def __repr__(self):
        return f'TreeNode({self.kind}, {self.id!r}, depth={self.depth})'


class PlaylistTree:
    """The playlists and their folders, as Apple's sidebar nests them.

    `root` is the nested tree: a TreeNode for the folder with id ROOT_FOLDER, the top level.
    `flat` is every node below it, depth first (each folder followed by its contents), each
    with its `depth`: the order a sidebar lists them in. The children lists of library.json's
    `folders` decide the nesting and order. What they do not reach is added to the top level,
    at the end: folders (with their contents) in the list's order, then playlists in the
    playlists section's order; so a library without `folders` is every playlist at the top
    level. A child naming nothing in the library is left out, and nothing is listed twice.
    Without a root entry, the top level is the folders whose parent is null. Built by
    Library.load() (the model: GObject only, no GTK).
    """

    def __init__(self, folders=(), playlists=(), existing=None):
        """folders: library.json's `folders` dicts; playlists: the playlist Items; existing:
        a {(kind, id): Item} index whose folder Items are reused (a reload keeps them)."""
        folders = [raw for raw in folders if _text(raw.get('id'))]
        raw_by_id = {}
        for raw in folders:
            raw_by_id.setdefault(_text(raw.get('id')), raw)
        playlist_by_id = {}
        for item in playlists:
            playlist_by_id.setdefault(item.id, item)
        root_raw = raw_by_id.get(ROOT_FOLDER)
        if root_raw is None:
            root_raw = {'id': ROOT_FOLDER, 'title': '', 'parent': None, 'children': [
                {'kind': 'folder', 'id': _text(raw.get('id'))} for raw in folders
                if raw.get('parent') is None]}
        existing = existing or {}

        def folder_item(raw):
            return _folder_item(raw, existing.get(('folder', _text(raw.get('id')))))

        self.root = TreeNode(folder_item(root_raw), -1, None)
        self.flat = []
        self._folders = {ROOT_FOLDER: self.root}
        placed = set()  # (kind, id) already in the tree

        def fill(node, children):
            for child in _dicts(children):
                kind, child_id = child.get('kind'), _text(child.get('id'))
                if (kind, child_id) in placed:
                    continue
                if kind == 'folder' and child_id in raw_by_id:
                    add(node, folder_item(raw_by_id[child_id]), raw_by_id[child_id])
                elif kind == 'playlist' and child_id in playlist_by_id:
                    add(node, playlist_by_id[child_id])

        def add(parent, item, raw=None):
            placed.add((item.kind, item.id))
            child = TreeNode(item, parent.depth + 1, parent)
            parent.children.append(child)
            self.flat.append(child)
            if item.kind == 'folder':
                self._folders[item.id] = child
                fill(child, raw.get('children'))

        # A folder's contents are added right after it, so `flat` fills depth first, and what
        # nothing reached goes at the end of the top level, which is the end of `flat` too.
        placed.add(('folder', ROOT_FOLDER))
        fill(self.root, root_raw.get('children'))
        for raw in folders:
            if ('folder', _text(raw.get('id'))) not in placed:
                add(self.root, folder_item(raw), raw)
        for item in playlists:
            if ('playlist', item.id) not in placed:
                add(self.root, item)
        for node in self._folders.values():
            node.store = Gio.ListStore(item_type=Item)
            node.store.splice(0, 0, [child.item for child in node.children])

    def folder(self, folder_id):
        """The TreeNode of the folder with that id (ROOT_FOLDER for the top level), or None."""
        return self._folders.get(folder_id)

    def folders(self):
        """Every folder's TreeNode but the root's, depth first."""
        return [node for node in self.flat if node.kind == 'folder']


def _folder_item(raw, existing=None):
    """A folders entry as an Item of kind 'folder': `existing` brought up to date, or a new one."""
    folder_id = _text(raw.get('id'))
    data = {'id': folder_id, 'kind': 'folder', 'title': _text(raw.get('title')) or '',
            'play': {}, 'groups': [], 'parent': raw.get('parent'),
            'children': raw.get('children')}
    if existing is not None and existing.kind == 'folder' and existing.id == folder_id:
        existing.merge(data, replace=True)
        return existing
    return Item(data)


def apply_diff(store, items):
    """Bring a Gio.ListStore to hold `items` (objects) in that order with the fewest splices:
    runs of the same objects in the same order are left alone, so a view over the store keeps
    its rows and its scroll position through a reload that changed little. 23,000 items diff
    in about 25 ms."""
    old = [store.get_item(position) for position in range(store.get_n_items())]
    if len(old) == len(items) and all(a is b for a, b in zip(old, items)):
        return
    matcher = difflib.SequenceMatcher(None, [id(item) for item in old],
                                      [id(item) for item in items], autojunk=False)
    for tag, start, end, new_start, new_end in reversed(matcher.get_opcodes()):
        if tag != 'equal':
            store.splice(start, end - start, items[new_start:new_end])


class _Superseded(Exception):
    """A newer load() started while this one waited."""


class Library(GObject.Object):
    """The whole library: one store per section, the shelves, and the Songs store.

    The stores are created once and filled in place, so a page can bind them before anything
    is loaded. `state` is 'empty' (nothing loaded, or no library.json), 'loading' or 'ready';
    'changed' is emitted when a load() finishes, whatever it found. `songs-ready` is true once
    the Songs store has been filled (build_songs()).
    """

    __gtype_name__ = 'AppleMusicLibrary'

    __gsignals__ = {
        'changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    state = GObject.Property(type=str, default='empty')
    generated = GObject.Property(type=str)
    storefront = GObject.Property(type=str)
    songs_ready = GObject.Property(type=bool, default=False)

    def __init__(self):
        super().__init__()
        self.albums = Gio.ListStore(item_type=Item)
        self.artists = Gio.ListStore(item_type=Item)
        self.playlists = Gio.ListStore(item_type=Item)
        self.radio = Gio.ListStore(item_type=Item)
        self.videos = Gio.ListStore(item_type=Item)
        self.shelves = []
        self.batch_size = BATCH_SIZE
        self._songs = Gio.ListStore(item_type=Track)
        self._songs_wanted = False  # build_songs() was called: every load() refills the store
        self._song_count = 0
        self._index = {}  # (kind, id) -> Item
        self._loose = []  # sections.songs: the loose songs' Track dicts
        self._loose_tracks = {}  # id -> Track, for the loose songs wrapped so far
        self._tree = PlaylistTree()
        self._generation = 0

    @property
    def songs(self):
        """The Songs store: every album's tracks, then the loose songs (sections.songs),
        deduplicated by id, in album order.

        It stays empty until build_songs() fills it (`songs-ready`); from then on every load()
        refills it.
        """
        return self._songs

    def song_count(self):
        """How many tracks `songs` holds or will hold, without building it."""
        return self._songs.get_n_items() if self.songs_ready else self._song_count

    async def build_songs(self):
        """Fill the Songs store, unless it is filled or being filled already.

        Wrapping 30,000 tracks takes about 300 ms, so it runs a few albums at a time with a
        pause for GTK to paint in between, and the store is spliced in one go at the end, so a
        view over it sorts once. When a load() is running, that load fills the store as it
        finishes; a load() that overtakes this build fills it instead.
        """
        if self._songs_wanted:
            return
        self._songs_wanted = True
        if self.state == 'loading':
            return
        try:
            await self._fill_songs(self._generation)
        except _Superseded:
            pass

    def by_id(self, kind, item_id):
        """The Item of that kind and id, from a section, a shelf or the playlist folders
        (kind 'folder'), or None."""
        return self._index.get((kind, item_id))

    def shelf(self, key):
        """The shelf with that key ('recently-added', 'heavy-rotation'…), or None."""
        return next((shelf for shelf in self.shelves if shelf.key == key), None)

    def playlist_tree(self):
        """The playlists and folders as a PlaylistTree: `root` nested, `flat` depth first.

        Built by each load() (a new tree, with new folder Items, each time); before the first,
        an empty one.
        """
        return self._tree

    def folder_items(self, folder_id):
        """A Gio.ListStore of the Items (folders and playlists) in the folder with that id, in
        Apple's order, or None when there is no such folder. ROOT_FOLDER is the top level. A
        load() makes new stores: follow the folder by its id."""
        node = self._tree.folder(folder_id)
        return node.store if node is not None else None

    def favourite_songs(self):
        """The Favourite Songs playlist (Item.favourites), or None when the library has none."""
        return next((item for item in self.playlists if item.favourites), None)

    def track_at(self, play, index):
        """The Track at queue position index of what play ({kind, id}) names, or None.

        play is an Item's or a Group's play target, as track rows and Play buttons pass it: the
        album or playlist is looked up by kind and id, and its groups sharing that target are
        searched for the entry whose `index` it is (an album's discs count on from each other).
        """
        item = self.by_id(play.get('kind'), play.get('id')) if isinstance(play, dict) else None
        if item is None:
            return None
        for group in item.groups:
            if group.play == play:
                for track in group.entries:
                    if track.index == index:
                        return track
        return None

    async def load(self):
        """Read library.json from the cache directory and fill the models from it.

        The file is read and parsed in a thread; wrapping happens here, a batch at a time, with
        a pause for GTK to paint between batches. The stores keep their identity and are
        refilled in place, with new Item objects. A missing or unreadable file leaves the
        library empty. A load that another load() overtakes gives up at its next pause.
        """
        await self._load(keep=False)

    async def reload(self):
        """Read library.json again (after a sync) and bring the models up to date in place.

        Everything is matched by kind and id: an Item still in the library keeps its object
        (its properties set where they changed, its groups and Tracks kept unless they
        changed), what is gone leaves the stores, what is new goes in at its place, and the
        shelves and folders keep theirs too. A page showing an Item, a grid's scroll position
        and the sidebar's selection all survive. As load() otherwise.
        """
        await self._load(keep=True)

    async def _load(self, keep):
        self._generation += 1
        generation = self._generation
        started = time.monotonic()
        self._set_state('loading')
        try:
            data, song_count = await asyncio.to_thread(
                _read_library, config.cache_dir() / 'library.json')
            self._check(generation)
            await self._fill(data or {}, generation, keep)
        except _Superseded:
            return
        except Exception:
            self._set_state('empty')  # a page waiting on 'loading' would wait forever
            self.emit('changed')
            raise
        self._song_count = song_count
        self._set_state('ready' if data is not None else 'empty')
        log.debug('Library %s in %.0f ms: %s, %d shelves, %d songs%s', self.state,
                  (time.monotonic() - started) * 1000,
                  ', '.join(f'{getattr(self, name).get_n_items()} {name}' for name in SECTIONS),
                  len(self.shelves), song_count, ' (in place)' if keep else '')
        self.emit('changed')

    async def _fill(self, data, generation, keep=False):
        self.generated = _text(data.get('generated'))
        self.storefront = _text(data.get('storefront'))
        sections = data.get('sections') if isinstance(data.get('sections'), dict) else {}
        existing = self._index if keep else {}
        index = {}
        changed = []  # kept Items whose properties changed: their views are told

        def wrap(raw):
            key = (_text(raw.get('kind')), _text(raw.get('id')))
            item = existing.get(key)
            if item is None:
                item = Item(raw)
            elif item.merge(raw, replace=True):
                changed.append(item)
            index.setdefault(key, item)
            return item

        for name in SECTIONS:
            await self._splice(getattr(self, name), _dicts(sections.get(name)), wrap, generation,
                               keep)
        if changed:
            self._notify_changed(changed)

        def shelf_item(raw):
            # The same album or playlist on a shelf and in a section is one object.
            return index.get((raw.get('kind'), raw.get('id'))) or wrap(raw)

        shelves = []
        kept = {shelf.key: shelf for shelf in self.shelves} if keep else {}
        for raw in _dicts(data.get('shelves')):
            key, title = _text(raw.get('key')) or '', _text(raw.get('title')) or ''
            items = [shelf_item(item) for item in _dicts(raw.get('items'))]
            shelf = kept.pop(key, None)
            if shelf is None:
                shelf = Shelf(key, title, items)
            else:
                shelf.update(title, items)
            shelves.append(shelf)
        tree = PlaylistTree(_dicts(data.get('folders')), list(self.playlists), existing)
        for node in tree.folders():
            index.setdefault(('folder', node.id), node.item)
        index.setdefault(('folder', ROOT_FOLDER), tree.root.item)
        self._loose = _dicts(sections.get('songs'))
        if not keep:
            self._loose_tracks = {}
        self.shelves = shelves
        self._tree = tree
        self._index = index
        if self._songs_wanted:
            await self._fill_songs(generation)

    def _notify_changed(self, items):
        """Tell the stores' views about kept Items whose properties changed, so bound rows are
        rebound: each is spliced over itself where it sits."""
        positions = {}
        for name in SECTIONS:
            store = getattr(self, name)
            for position in range(store.get_n_items()):
                positions[store.get_item(position)] = (store, position)
        for item in items:
            found = positions.get(item)
            if found is not None:
                store, position = found
                store.splice(position, 1, [item])

    async def _splice(self, store, dicts, wrap, generation, keep=False):
        """Replace the store's contents with the wrapped dicts, a batch at a time.

        Each batch overwrites the old items at its position, so a reload never shows an empty
        store; whatever old items are left over go at the end. Keeping (reload), the wrapped
        items are diffed against the store instead, so unchanged runs are not touched.
        """
        if keep:
            items = []
            for start in range(0, len(dicts), self.batch_size):
                items.extend(wrap(raw) for raw in dicts[start:start + self.batch_size])
                await yield_to_frames()
                self._check(generation)
            apply_diff(store, items)
            return
        position = 0
        for start in range(0, len(dicts), self.batch_size):
            batch = [wrap(raw) for raw in dicts[start:start + self.batch_size]]
            stale = min(len(batch), store.get_n_items() - position)
            store.splice(position, stale, batch)
            position += len(batch)
            await yield_to_frames()
            self._check(generation)
        store.splice(position, store.get_n_items() - position, [])

    async def _fill_songs(self, generation):
        seen = set()
        tracks = []
        started = paused = time.monotonic()
        for item in list(self.albums):
            for group in item.groups:
                for track in group.entries:
                    if track.id not in seen:
                        seen.add(track.id)
                        tracks.append(track)
            if time.monotonic() - paused > FRAME_BUDGET:
                await yield_to_frames()
                self._check(generation)
                paused = time.monotonic()
        # The loose songs after the albums', by id; a loose song wrapped before keeps its Track
        # while its dict is the same.
        loose_tracks = {}
        for raw in self._loose:
            track_id = _text(raw.get('id'))
            if not track_id or track_id in seen:
                continue
            seen.add(track_id)
            track = self._loose_tracks.get(track_id)
            if track is None or track.raw != raw:
                track = Track(raw, {'kind': 'song', 'id': track_id})
            loose_tracks[track_id] = track
            tracks.append(track)
        self._loose_tracks = loose_tracks
        current = [self._songs.get_item(position) for position in range(self._songs.get_n_items())]
        if len(current) != len(tracks) or any(a is not b for a, b in zip(current, tracks)):
            self._songs.splice(0, self._songs.get_n_items(), tracks)
        if not self.songs_ready:
            self.songs_ready = True
        log.debug('Songs built in %.0f ms: %d songs', (time.monotonic() - started) * 1000,
                  len(tracks))

    def _check(self, generation):
        if generation != self._generation:
            raise _Superseded

    def _set_state(self, state):
        if self.state != state:
            self.state = state


class SongOrder:
    """The Songs table's orders, sorted in Python.

    A column's order is its own key, ascending or descending, with ties broken by the other
    columns, always ascending (by artist, the albums and their tracks stay in order either
    way). GTK's sorters are too slow here: they read a Track's properties from C, which costs
    about 3 µs a read into Python, and a Gtk.ColumnViewSorter has no sort keys, so a
    Gtk.SortListModel sorting by it compares pairs and reads both tracks for every comparison:
    1.6 to 6 s a click on 30,000 songs. Here each key is computed once per track (collation
    once per distinct string) and cached, and Python sorts positions: on 30,000 songs a
    column's first order takes 15 to 60 ms (titles, nearly all distinct, collate slowest), any
    order after that 5 to 8 ms.
    """

    # Column -> the keys that break its ties, most significant first.
    TIES = {
        'title': ('artist', 'album', 'track'),
        'artist': ('album', 'track'),
        'album': ('artist', 'track'),
        'time': ('title', 'artist'),
    }

    def __init__(self, tracks):
        self._tracks = list(tracks)
        self._keys = {}  # key name -> one key per track
        self._ties = {}  # column -> positions in the order of its tie-breakers

    def tracks(self, column, descending=False):
        """The tracks in column's order: 'title', 'artist', 'album' or 'time'."""
        ties = self._ties.get(column)
        if ties is None:
            ties = list(range(len(self._tracks)))
            for name in reversed(self.TIES[column]):  # stable sorts, least significant first
                ties.sort(key=self._key(name).__getitem__)
            self._ties[column] = ties
        order = sorted(ties, key=self._key(column).__getitem__, reverse=descending)
        return [self._tracks[position] for position in order]

    def _key(self, name):
        keys = self._keys.get(name)
        if keys is None:
            if name == 'time':
                keys = [track._duration_ms for track in self._tracks]
            elif name == 'track':
                keys = [(track._disc_number, track._track_number) for track in self._tracks]
            else:
                attribute = '_' + name
                collated = {}
                keys = []
                for track in self._tracks:
                    text = getattr(track, attribute)
                    key = collated.get(text)
                    if key is None:
                        key = collated[text] = collation_key(text)
                    keys.append(key)
            self._keys[name] = keys
        return keys


def fold(text):
    """text for matching as people type: case and accents ignored ("Beyoncé" is "beyonce")."""
    if not text.isascii():
        text = ''.join(char for char in unicodedata.normalize('NFKD', text)
                       if not unicodedata.combining(char))
    return text.casefold()


def collation_key(text):
    """A key that sorts text as the locale collates it, ignoring case, as a Gtk.StringSorter
    does. The app's locale is set by GTK; without it (tests) this is code point order."""
    try:
        return locale.strxfrm(text.casefold())
    except ValueError:  # an embedded NUL
        return text.casefold()


def _dicts(value):
    """The dicts of a JSON list, or nothing when it is not one."""
    return [raw for raw in value if isinstance(raw, dict)] if isinstance(value, list) else []


def _read_library(path):
    """Parse library.json (in a thread). Returns (data or None, the Songs page's count)."""
    try:
        with open(path, 'rb') as file:
            data = json.load(file)
    except FileNotFoundError:
        log.info('No library at %s', path)
        return None, 0
    except (OSError, ValueError) as error:
        log.warning('Cannot read %s: %s', path, error)
        return None, 0
    if not isinstance(data, dict):
        log.warning('Cannot read %s: not a JSON object', path)
        return None, 0
    sections = data.get('sections') if isinstance(data.get('sections'), dict) else {}
    songs = {entry.get('id')
             for album in _dicts(sections.get('albums'))
             for group in _dicts(album.get('groups'))
             for entry in _dicts(group.get('entries'))}
    songs |= {entry.get('id') for entry in _dicts(sections.get('songs')) if entry.get('id')}
    return data, len(songs)


async def yield_to_frames():
    """Pause so GTK can paint before the next batch.

    A bare `await asyncio.sleep(0)` is not enough on the GLib loop: asyncio's callbacks run at
    G_PRIORITY_DEFAULT, above the frame clock's redraw, so a task that only yields to itself
    starves painting until it ends. Running the next step at idle priority lets the frame in
    first. On a plain asyncio loop (tests) nothing paints and sleep(0) is all there is.
    """
    task = asyncio.current_task()
    set_priority = getattr(task, 'set_priority', None)  # gi.events.GLibTask
    if set_priority is None:
        await asyncio.sleep(0)
        return
    set_priority(GLib.PRIORITY_DEFAULT_IDLE)
    try:
        await asyncio.sleep(0)
    finally:
        set_priority(GLib.PRIORITY_DEFAULT)
