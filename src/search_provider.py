# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""GNOME Shell's search provider: the library searched from the Activities overview.

    provider = SearchProvider(app)                # Application.do_dbus_register
    provider.register(connection, object_path)    # org.gnome.Shell.SearchProvider2 exported
    provider.unregister()                         # Application.do_dbus_unregister

The Shell finds the service through data/<app id>.search-provider.ini.in's installed copy
(share/gnome-shell/search-providers: the bus name, this object's path, the desktop file whose
name and icon head the results) and calls the object as the user types. GetInitialResultSet
and GetSubsearchResultSet answer the ids of the matching artists, albums, playlists and
songs (result_id(): `<kind>:<id>`), RESULTS_PER_KIND of each kind at most, best matches
first; a subsearch is searched afresh, since a handful per kind is no subset of the handful
before. GetResultMetas answers each id's name, description and icon: a `gicon` of the
thumbnail in the cache's thumb/ when the file is there, else the kind's symbolic icon.
ActivateResult presents the window and opens the item's page, or plays a song (the
window's open_item and play_request); LaunchSearch shows the Search page in Your Library
mode with the terms (window.search_library).

Everything is answered from the library in memory, never from the engine: a search cannot
start Chrome. Started by the bus for a search (the D-Bus service file's
--gapplication-service: no window until a result is activated), the app has the library's
read under way, and an answer waits for it (Application.load_library), LOAD_WAIT at most.
Matching is the Search page's Your Library rule, an item whose title or subtitle holds the
search (a song whose search_key does), case and accents folded (library.fold), term by term
as the Shell splits what is typed, and ranked by rank(): the title that is the search, then
one starting with it, then one holding it, then a subtitle match, each in the store's order.
Songs come from the Songs store only once it is built (library.songs_ready: the Songs or
the Search page asked); a search never builds it, which takes a second on a big library.
The stores are scanned SCAN_STEP items at a time with a yield to the frame clock between,
so a window open meanwhile keeps painting.

The app is held while a call is answered and released after, so a service start lingers for
Application.SERVICE_LINGER_MS after its last call and then quits.
"""

import asyncio
import logging
import os
from gettext import gettext as _

from gi.repository import Gio, GLib

from .library import Track, fold, yield_to_frames

log = logging.getLogger(__name__)

INTERFACE = 'org.gnome.Shell.SearchProvider2'
# The object's name under the application's own object path (the .ini names the whole path).
OBJECT_NAME = 'SearchProvider'
RESULTS_PER_KIND = 5
# How long an answer waits for the library's first load (the app started by the bus for
# this search) before answering from what is loaded.
LOAD_WAIT = 10.0
# Items looked at between two yields to the frame clock while a store is scanned.
SCAN_STEP = 2000
# The kinds searched, in the order results of equal rank are listed, and the system icon
# theme's symbol for a result without a thumbnail (the Shell draws from that theme, not from
# the app's bundled icons).
KINDS = ('artist', 'album', 'playlist', 'song')
ICONS = {'artist': 'audio-input-microphone-symbolic', 'album': 'media-optical-cd-audio-symbolic',
         'playlist': 'view-list-symbolic', 'song': 'audio-x-generic-symbolic'}

INTROSPECTION_XML = """
<node>
  <interface name="org.gnome.Shell.SearchProvider2">
    <method name="GetInitialResultSet">
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetSubsearchResultSet">
      <arg type="as" name="previous_results" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="as" name="results" direction="out"/>
    </method>
    <method name="GetResultMetas">
      <arg type="as" name="identifiers" direction="in"/>
      <arg type="aa{sv}" name="metas" direction="out"/>
    </method>
    <method name="ActivateResult">
      <arg type="s" name="identifier" direction="in"/>
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="LaunchSearch">
      <arg type="as" name="terms" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
  </interface>
</node>
"""


def kind_names():
    """What a result's description calls its kind, translated on call (the Search page's
    words for the same kinds)."""
    return {'album': _('Album'), 'artist': _('Artist'), 'playlist': _('Playlist'),
            'song': _('Song')}


def search_terms(terms):
    """The Shell's terms (what was typed, split on spaces) folded for matching, blank ones
    dropped."""
    return [fold(term.strip()) for term in terms if term and term.strip()]


def rank(title, subtitle, terms):
    """How well something matches the folded `terms`, by its folded `title` and `subtitle`:
    None when a term is in neither (no match), else a rank, the lower the better: 0 the
    title is the search, 1 it starts with it, 2 every term starts a word of it, 3 every
    term is in it, 4 a term is only in the subtitle. The Search page's Your Library filter
    matches the same (the title or the subtitle holding the text), in the store's order."""
    if not terms:
        return None
    search = ' '.join(terms)
    if title == search:
        return 0
    if title.startswith(search):
        return 1
    if all(term in title for term in terms):
        words = title.split()
        if all(any(word.startswith(term) for word in words) for term in terms):
            return 2
        return 3
    if all(term in title or term in subtitle for term in terms):
        return 4
    return None


def item_rank(item, terms):
    """rank() for an Item, by its title and subtitle."""
    return rank(fold(item.title or ''), fold(item.subtitle or ''), terms)


def song_rank(track, terms):
    """rank() for a Track, by its search_key: the folded title, then the artist and the
    album on lines of their own."""
    title, _separator, rest = track.search_key.partition('\n')
    return rank(title, rest, terms)


def result_id(kind, item_id):
    """The identifier a result goes to the Shell as."""
    return f'{kind}:{item_id}'


def parse_id(identifier):
    """An identifier's (kind, id), or None for one not made by result_id()."""
    kind, separator, item_id = identifier.partition(':')
    if not separator or kind not in KINDS or not item_id:
        return None
    return kind, item_id


def description(kind, obj):
    """A result's second line: its kind, then an album's artist, an artist's or a playlist's
    count, a song's artist and album, each part that is there, ' · ' between."""
    name = kind_names().get(kind, '')
    if kind == 'song':
        parts = (name, obj.artist, obj.album)
    elif kind == 'album':
        parts = (name, obj.subtitle)
    else:
        parts = (name, obj.count_label)
    return ' · '.join(part for part in parts if part)


def existing_files(paths):
    """Those of `paths` that are files with something in them. It asks the file system:
    call it in a thread."""
    found = set()
    for path in paths:
        try:
            if os.path.getsize(path) > 0:
                found.add(path)
        except (OSError, TypeError, ValueError):
            pass
    return found


def meta(identifier, kind, obj, thumb_on_disk):
    """The meta dict (name -> Variant) the Shell shows for a result: its id, name,
    description and `gicon`, the thumbnail file when `thumb_on_disk`, else the kind's
    symbolic icon."""
    if thumb_on_disk:
        icon = Gio.FileIcon.new(Gio.File.new_for_path(obj.thumb))
    else:
        icon = Gio.ThemedIcon.new(ICONS[kind])
    data = {
        'id': GLib.Variant('s', identifier),
        'name': GLib.Variant('s', obj.title or ''),
        'gicon': GLib.Variant('s', icon.to_string()),
    }
    text = description(kind, obj)
    if text:
        data['description'] = GLib.Variant('s', text)
    return data


class SearchProvider:
    """The search provider over the app's library. See the module."""

    def __init__(self, app):
        """`app` gives `library` (once started), `load_library()`, `hold()`, `release()`,
        `spawn()`, `activate()` and `get_active_window()`; a test passes a stand-in."""
        self._app = app
        self._node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self._connection = None
        self._registration = 0
        self._found = {}  # identifier -> the Item or Track of the last result set

    # -- lifecycle -----------------------------------------------------------------------

    def register(self, connection, object_path):
        """Export the object at `object_path` on `connection` (the application's, before its
        name is owned, so a call never finds the name without the object)."""
        self._connection = connection
        self._registration = connection.register_object_with_closures2(
            object_path, self._node.interfaces[0], self._on_method_call, None, None)
        log.debug('search provider: %s at %s', INTERFACE, object_path)

    def unregister(self):
        if self._connection is not None and self._registration:
            self._connection.unregister_object(self._registration)
        self._registration = 0
        self._connection = None

    @property
    def registered(self):
        """Whether the object is on the bus (a test asks)."""
        return self._connection is not None

    # -- D-Bus ---------------------------------------------------------------------------

    def _on_method_call(self, _connection, _sender, _path, _interface, method, parameters,
                        invocation):
        handler = getattr(self, METHODS.get(method, ''), None)
        if handler is None:
            invocation.return_dbus_error('org.freedesktop.DBus.Error.UnknownMethod',
                                         f'{INTERFACE}.{method} is not known')
            return
        self._app.hold()  # until the answer is on the bus: a service start stays for it
        self._app.spawn(self._answer(method, handler, parameters.unpack(), invocation))

    async def _answer(self, method, handler, arguments, invocation):
        try:
            await self._library_loaded()
            invocation.return_value(await handler(*arguments))
        except Exception:
            log.exception('search provider: %s failed', method)
            invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed',
                                         f'{method} failed')
        finally:
            self._app.release()

    async def _library_loaded(self):
        """The library's first load finished, or LOAD_WAIT passed (what is loaded by then
        answers); its failure is logged where it ran, and the stores hold what they hold."""
        task = self._app.load_library()
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), LOAD_WAIT)
        except TimeoutError:
            log.warning('search provider: the library is still loading after %g s', LOAD_WAIT)
        except Exception:
            pass

    # -- the methods ---------------------------------------------------------------------

    async def get_initial_result_set(self, terms):
        return GLib.Variant('(as)', (await self.search(terms),))

    async def get_subsearch_result_set(self, _previous_results, terms):
        return GLib.Variant('(as)', (await self.search(terms),))

    async def get_result_metas(self, identifiers):
        return GLib.Variant('(aa{sv})', (await self.metas(identifiers),))

    async def activate_result(self, identifier, _terms, _timestamp):
        self.activate(identifier)
        return None

    async def launch_search(self, terms, _timestamp):
        self.launch(terms)
        return None

    async def search(self, terms):
        """The identifiers of the best matches for `terms`: RESULTS_PER_KIND per kind, by
        rank, then by kind (KINDS' order), then as the stores list them. What each names is
        kept for the metas and the activation that follow."""
        terms = search_terms(terms)
        found = {}
        ranked = []  # (rank, kind order, store position, identifier)
        if terms:
            for order, kind in enumerate(KINDS):
                store = self._store(kind)
                if store is None:
                    continue
                matches = await self._scan(store, kind, terms)
                for score, position, obj in matches[:RESULTS_PER_KIND]:
                    identifier = result_id(kind, obj.id)
                    found[identifier] = obj
                    ranked.append((score, order, position, identifier))
        self._found = found
        ranked.sort()
        return [identifier for *_order, identifier in ranked]

    def _store(self, kind):
        library = self._app.library
        if library is None:
            return None
        if kind == 'song':
            return library.songs if library.songs_ready else None
        return getattr(library, kind + 's', None)

    async def _scan(self, store, kind, terms):
        """A store's matches as (rank, position, object), best first, the frame clock given
        its turn every SCAN_STEP items (a store that changes meanwhile is read as it is)."""
        matches = []
        position = 0
        while position < store.get_n_items():
            if position and position % SCAN_STEP == 0:
                await yield_to_frames()
            obj = store.get_item(position)
            if obj is not None:
                score = song_rank(obj, terms) if kind == 'song' else item_rank(obj, terms)
                if score is not None:
                    matches.append((score, position, obj))
            position += 1
        matches.sort(key=lambda match: match[:2])
        return matches

    async def metas(self, identifiers):
        """A meta dict per identifier, in their order (the Shell wants one for each, with a
        name): the thumbnails' files looked for in a thread."""
        objects = [(identifier, self._lookup(identifier)) for identifier in identifiers]
        thumbs = {obj.thumb for _identifier, obj in objects if obj is not None and obj.thumb}
        on_disk = await asyncio.to_thread(existing_files, thumbs) if thumbs else set()
        metas = []
        for identifier, obj in objects:
            parsed = parse_id(identifier)
            if obj is None or parsed is None:
                metas.append({'id': GLib.Variant('s', identifier),
                              'name': GLib.Variant('s', _('Not in the library any more'))})
                continue
            metas.append(meta(identifier, parsed[0], obj, obj.thumb in on_disk))
        return metas

    def _lookup(self, identifier):
        """What an identifier names: the object of the last result set, else the library's
        of that kind and id (a song by a scan of the Songs store), else None."""
        obj = self._found.get(identifier)
        if obj is not None:
            return obj
        parsed = parse_id(identifier)
        library = self._app.library
        if parsed is None or library is None:
            return None
        kind, item_id = parsed
        if kind != 'song':
            return library.by_id(kind, item_id)
        if not library.songs_ready:
            return None
        return next((track for track in library.songs if track.id == item_id), None)

    def activate(self, identifier):
        """Open what `identifier` names: the window presented (built, with the engine's
        autostart, when there is none), an item's page pushed, a song played."""
        obj = self._lookup(identifier)
        if obj is None:
            log.warning('search provider: %s is not in the library', identifier)
        window = self._present()
        if window is None or obj is None:
            return
        if isinstance(obj, Track):
            window.play_request(obj.play, start_with=obj.index, start_id=obj.id)
        else:
            window.open_item(obj)

    def launch(self, terms):
        """The Search page in Your Library mode with the terms typed."""
        window = self._present()
        if window is not None:
            window.search_library(' '.join(term for term in terms if term))

    def _present(self):
        """The window, presented: the app activated (do_activate builds it when there is
        none, and starts the engine when the settings ask); None while the app quits."""
        self._app.activate()
        window = self._app.get_active_window()
        if window is None:
            log.warning('search provider: no window to show the result in')
        return window


# The interface's methods, each the coroutine method answering it with the reply Variant.
METHODS = {
    'GetInitialResultSet': 'get_initial_result_set',
    'GetSubsearchResultSet': 'get_subsearch_result_set',
    'GetResultMetas': 'get_result_metas',
    'ActivateResult': 'activate_result',
    'LaunchSearch': 'launch_search',
}
