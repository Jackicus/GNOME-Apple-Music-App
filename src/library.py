"""The library as GObject models the UI binds to, loaded from the cache's library.json.

library.json holds the Item and Track shapes src/backend/README.md describes. Library.load()
parses it in a thread and wraps it on the main thread in batches, so pages fill while frames
keep painting. Items become Item objects in one Gio.ListStore per section; an Item's `groups`
and the Songs store are wrapped only when first asked for. GLib, GObject and Gio only, no GTK, so
the model works in tests without a display.
"""

import asyncio
import json
import logging
import time

from gi.repository import Gio, GLib, GObject

from .backend import config

log = logging.getLogger(__name__)

# The sections of library.json and the stores they fill.
SECTIONS = ('albums', 'artists', 'playlists', 'radio', 'videos')

BATCH_SIZE = 500


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
    Group, wrapped from raw on first access.
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
    def groups(self):
        if self._groups is None:
            # An album's tracks draw the album's cover; an artist's groups are albums, whose
            # covers the artist Item does not have.
            thumb = self._thumb if self._kind == 'album' else None
            self._groups = [Group(group, thumb) for group in _dicts(self.raw.get('groups'))]
        return self._groups


class Shelf(GObject.Object):
    """A titled row of Items: Apple's home page recommendations, Heavy Rotation, Recently Added."""

    __gtype_name__ = 'AppleMusicShelf'

    key = model_property('key', str, '')
    title = model_property('title', str, '')

    def __init__(self, key, title, items):
        super().__init__()
        self._key = key
        self._title = title
        self.items = Gio.ListStore(item_type=Item)
        self.items.splice(0, 0, items)


class _Superseded(Exception):
    """A newer load() started while this one waited."""


class Library(GObject.Object):
    """The whole library: one store per section, the shelves, and the Songs store.

    The stores are created once and filled in place, so a page can bind them before anything
    is loaded. `state` is 'empty' (nothing loaded, or no library.json), 'loading' or 'ready';
    'changed' is emitted when a load() finishes, whatever it found.
    """

    __gtype_name__ = 'AppleMusicLibrary'

    __gsignals__ = {
        'changed': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    state = GObject.Property(type=str, default='empty')
    generated = GObject.Property(type=str)
    storefront = GObject.Property(type=str)

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
        self._songs_built = False
        self._song_count = 0
        self._index = {}  # (kind, id) -> Item
        self._generation = 0

    @property
    def songs(self):
        """Every album's tracks, deduplicated by id: built on first access, then kept current."""
        if not self._songs_built:
            self._songs_built = True
            self._build_songs()
        return self._songs

    def song_count(self):
        """How many tracks `songs` holds or will hold, without building it."""
        return self._songs.get_n_items() if self._songs_built else self._song_count

    def by_id(self, kind, item_id):
        """The Item of that kind and id, from a section or a shelf, or None."""
        return self._index.get((kind, item_id))

    def shelf(self, key):
        """The shelf with that key ('recently-added', 'heavy-rotation'…), or None."""
        return next((shelf for shelf in self.shelves if shelf.key == key), None)

    async def load(self):
        """Read library.json from the cache directory and fill the models from it.

        The file is read and parsed in a thread; wrapping happens here, a batch at a time, with
        a pause for GTK to paint between batches. The stores keep their identity and are
        refilled in place. A missing or unreadable file leaves the library empty. A load that
        another load() overtakes gives up at its next pause.
        """
        self._generation += 1
        generation = self._generation
        started = time.monotonic()
        self._set_state('loading')
        try:
            data, song_count = await asyncio.to_thread(
                _read_library, config.cache_dir() / 'library.json')
            self._check(generation)
            await self._fill(data or {}, generation)
        except _Superseded:
            return
        except Exception:
            self._set_state('empty')  # a page waiting on 'loading' would wait forever
            self.emit('changed')
            raise
        self._song_count = song_count
        self._set_state('ready' if data is not None else 'empty')
        log.debug('Library %s in %.0f ms: %s, %d shelves, %d songs', self.state,
                  (time.monotonic() - started) * 1000,
                  ', '.join(f'{getattr(self, name).get_n_items()} {name}' for name in SECTIONS),
                  len(self.shelves), song_count)
        self.emit('changed')

    async def _fill(self, data, generation):
        self.generated = _text(data.get('generated'))
        self.storefront = _text(data.get('storefront'))
        sections = data.get('sections') if isinstance(data.get('sections'), dict) else {}
        index = {}

        def wrap(raw):
            item = Item(raw)
            index.setdefault((item.kind, item.id), item)
            return item

        for name in SECTIONS:
            await self._splice(getattr(self, name), _dicts(sections.get(name)), wrap, generation)

        def shelf_item(raw):
            # The same album or playlist on a shelf and in a section is one object.
            return index.get((raw.get('kind'), raw.get('id'))) or wrap(raw)

        shelves = []
        for raw in _dicts(data.get('shelves')):
            items = [shelf_item(item) for item in _dicts(raw.get('items'))]
            shelves.append(Shelf(_text(raw.get('key')) or '', _text(raw.get('title')) or '', items))
        self.shelves = shelves
        self._index = index
        if self._songs_built:
            self._build_songs()

    async def _splice(self, store, dicts, wrap, generation):
        """Replace the store's contents with the wrapped dicts, a batch at a time.

        Each batch overwrites the old items at its position, so a reload never shows an empty
        store; whatever old items are left over go at the end.
        """
        position = 0
        for start in range(0, len(dicts), self.batch_size):
            batch = [wrap(raw) for raw in dicts[start:start + self.batch_size]]
            stale = min(len(batch), store.get_n_items() - position)
            store.splice(position, stale, batch)
            position += len(batch)
            await yield_to_frames()
            self._check(generation)
        store.splice(position, store.get_n_items() - position, [])

    def _build_songs(self):
        seen = set()
        tracks = []
        for item in self.albums:
            for group in item.groups:
                for track in group.entries:
                    if track.id not in seen:
                        seen.add(track.id)
                        tracks.append(track)
        self._songs.splice(0, self._songs.get_n_items(), tracks)

    def _check(self, generation):
        if generation != self._generation:
            raise _Superseded

    def _set_state(self, state):
        if self.state != state:
            self.state = state


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
