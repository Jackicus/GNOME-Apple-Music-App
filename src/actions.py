# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""What can be done to an album, playlist, song, station, artist or video: the window's item
actions, the menus that offer them, and what a track dragged onto a sidebar playlist does.

    actions = ItemActions(window, app)     # made once by the window; adds the win.* actions
    menu = actions.menu_for(obj)           # a Gio.Menu for an Item or a Track, or None
    menu = actions.menu_for(track, queued=True)   # the item playing's, or Up Next's
    actions.go_to(obj, 'album')            # obj's album's page ('artist': its artist's)
    actions.show_artist(obj)               # a link's artist: the library's page of them
    actions.fill_sidebar_menu(menu, item)  # a sidebar playlist's or folder's menu
    actions.drop(playlist, ref)            # a library.TrackRef dropped on a sidebar playlist

The actions, each on the window (win.*) with a GLib.Variant target "(ss)", the (kind, id) of
what it acts on (an Item's kind and id, or 'song' and a Track's id):

    item-play            the item, or a track's album or playlist from the track
    item-play-next       queued after the item playing (mk.playNext)
    item-play-later      queued at the end (mk.playLater)
    item-love            loved: a song goes into Favourite Songs
    item-unlove          the love taken back
    item-add-to-library  a catalog item added to the library
    item-go-to-album     the page of the album a song is on (related.py finds it)
    item-go-to-artist    the page of the artist a song, video or album is by
    item-open-in-browser its music.apple.com page, in the default browser (Gtk.UriLauncher)
    item-copy-link       that page's address, on the clipboard

and item-add-to-playlist, "(sss)" (playlist id, kind, id): a song or a music video added to a
library playlist. The playlists' own, each through a dialog (dialogs/playlist.py):

    item-new-playlist    a playlist named in a dialog: holding the song or video of the
                         target, or in the folder it names ('folder', 'root': the top level)
    item-rename          a playlist's name and description, or a folder's name
    item-delete          a playlist, or a folder with what is in it, once a dialog confirms

and item-remove-from-playlist, "(ssi)" (playlist id, track id, the track's index): one entry
of a playlist taken out. A target names an object the menu was built for (remembered, the
last REMEMBERED of them) or one the library has; failing both, the target's kind and id are
used as they are. Each action awaits the engine (started first when it is down and the account is
signed in, as a play request does) and confirms with a toast, or reports the EngineError
(app.report: signed out, that opens the sign-in). After a song was added to a playlist, or a
song loved or unloved, the playlist (Favourite Songs) is fetched again and merged into the
library's Item, so an open page shows the change at once.

The menus are built in code, per kind (build_menu): Play, Play Next, Play Later; Go to Album,
Go to Artist (not where the page shown is that album or artist already); Favourite and
Remove from Favourites (both, of which the menu shows the one whose action is enabled: the
actions' state says whether the item is loved), Add to Library, Add to Playlist (the
library's playlists that take songs, folders flattened, then New Playlist…), Remove from
Playlist (a track shown in a playlist of the user's: `container`); Open in Browser, Copy Link
(only an address anyone can open: not the web player's library routes; a library artist's is
the catalog artist's page, which the engine finds through their songs); Rename… and Delete
Playlist… or Delete Folder… (a playlist of the user's, Item.editable; a folder of theirs);
each only where it applies.

After a playlist or folder was made, renamed or deleted, the window leaves the pages of what
is gone (Window.leave), the sidebar shows a new name at once, and the library follows Apple's
listing once it shows the change (wait_listed: a new playlist takes seconds to be listed),
through the playlists pass of a sync (sync.py). A track removed has its playlist fetched again,
as an add does.
The menus of the player's queue (the item playing's in the Now Playing sheet and the player
bar, Up Next's) have no Play, which would replace the queue: activating the row plays it.
Whether an item is loved is not in the library: a menu shows what the engine said last (its
`rated` signal), and asks again (engine.rating) as it opens.

Only Gio, GLib and GObject at import time besides Gtk's UriLauncher, used when a page is
opened: tests build the menus and run the actions with a stand-in window, app and engine.
"""

import asyncio
import logging
from collections import OrderedDict
from gettext import gettext as _
from gettext import ngettext
from urllib.parse import urlsplit

from gi.repository import Gio, GLib

from . import related
from .backend.errors import EngineError
from .backend.api import is_library_id
from .library import ROOT_FOLDER, TRACK_KINDS, Item, Track, TrackRef

log = logging.getLogger(__name__)

TARGET = GLib.VariantType.new('(ss)')
PLAYLIST_TARGET = GLib.VariantType.new('(sss)')
ENTRY_TARGET = GLib.VariantType.new('(ssi)')

# After a playlist write, how long to wait before each look at Apple's listing, in seconds,
# until it shows the change: a new playlist was listed 4 to 6.5 s after it was made, a
# rename or a deletion within a second (2026-10-03). Then the library follows regardless.
SETTLE_DELAYS = (0.5, 1, 1, 1, 2, 2, 3, 3)

# How many of the objects menus were built for are remembered for their actions.
REMEMBERED = 64

WEB = 'https://music.apple.com'
# The hosts of Apple Music's pages: the only addresses the app opens or copies.
APPLE_HOSTS = frozenset({'music.apple.com', 'geo.music.apple.com'})
# The page of a catalog item without a URL: music.apple.com/<storefront>/<path>/<id>, which
# Apple redirects to the canonical address with the item's name in it.
WEB_PATHS = {'album': 'album', 'playlist': 'playlist', 'song': 'song', 'station': 'station',
             'artist': 'artist', 'video': 'music-video'}
# The web player's own routes for library items (as its sidebar links and its router name
# them): a library playlist's and a library album's pages, which open for the owner only.
LIBRARY_ROUTES = {'playlist': 'library/playlist', 'album': 'library/albums'}

# Ids the library makes up for what Apple gave none (backend.normalize groups songs into albums
# and artists): nothing of Apple's answers to them.
SYNTHETIC_PREFIXES = ('l.alb_', 'l.art_')

MENU_KINDS = ('album', 'playlist', 'song', 'station', 'artist', 'video', 'folder')
QUEUE_KINDS = ('album', 'playlist')
RATED_KINDS = ('album', 'playlist', 'station', 'video')
LIBRARY_KINDS = ('song', 'album', 'playlist', 'video')

# A sidebar playlist's menu (the section's menu model): these, in this order.
SIDEBAR_ACTIONS = ('item-play', 'item-play-next', 'item-open-in-browser')


def action_labels():
    """The menu's labels, by action, translated on call (after gettext is set up). Each has
    a mnemonic (GtkPopoverMenu shows them with use-underline), no two the same letter."""
    return {
        'item-play': _('_Play'),
        'item-play-next': _('Play _Next'),
        'item-play-later': _('Play _Later'),
        'item-love': _('_Favourite'),
        'item-unlove': _('_Remove from Favourites'),
        'item-add-to-library': _('_Add to Library'),
        'item-add-to-playlist': _('Add to Pla_ylist'),
        'item-go-to-album': _('_Go to Album'),
        'item-go-to-artist': _('Go to Ar_tist'),
        'item-open-in-browser': _('_Open in Browser'),
        'item-copy-link': _('_Copy Link'),
        'item-remove-from-playlist': _('Remo_ve from Playlist'),
        'item-new-playlist': _('Ne_w Playlist…'),
        'item-rename': _('Rena_me…'),
        'item-delete': _('_Delete Playlist…'),
    }


def delete_label(kind):
    """Delete's label for a playlist, or for a folder (kind 'folder')."""
    return _('_Delete Folder…') if kind == 'folder' else action_labels()['item-delete']


def _synthetic(item_id):
    return str(item_id or '').startswith(SYNTHETIC_PREFIXES)


def describe(obj):
    """(kind, id) naming obj in an action target: an Item's kind and id, 'song' and a
    Track's id; None for anything else, or anything without an id."""
    if isinstance(obj, Track):
        item_id = obj.id or obj.catalog_id
        return ('song', item_id) if item_id else None
    if isinstance(obj, Item) and obj.kind and obj.id:
        return obj.kind, obj.id
    return None


def _song_id(obj):
    """The id a song is queued and rated by: its catalog id, else its own."""
    return obj.catalog_id or obj.id


def can_play(obj):
    """Whether obj has something to play: a track, or an album, playlist, song or station
    with a play target, or a catalog artist (its top songs; a library artist's id is the
    library's own invention)."""
    if isinstance(obj, Track):
        return bool(obj.id or obj.catalog_id)
    if not isinstance(obj, Item) or not obj.play.get('kind') or not obj.play.get('id'):
        return False
    if obj.kind == 'artist':
        return not is_library_id(obj.id)
    return obj.kind in ('album', 'playlist', 'song', 'station')


def queue_target(obj):
    """(kind, id) that Play Next and Play Later queue, or None: a song by its catalog id, an
    album or playlist by its play target."""
    if isinstance(obj, Track) or (isinstance(obj, Item) and obj.kind == 'song'):
        song_id = _song_id(obj)
        return ('song', song_id) if song_id else None
    if isinstance(obj, Item) and obj.kind in QUEUE_KINDS and not _synthetic(obj.id):
        kind, item_id = obj.play.get('kind'), obj.play.get('id')
        return (kind, item_id) if kind and item_id else None
    return None


def rating_target(obj):
    """(kind, id) that love, unlove and rating() take for obj, or None when it has no
    rating: a song by its catalog id (the heart's too), a track that is a music video as a
    video (Track.kind), an album, playlist, station or video by its own id. Not Favourite
    Songs itself, nor an item the library made up."""
    if isinstance(obj, Track):
        song_id = _song_id(obj)
        return (obj.kind or 'song', song_id) if song_id else None
    if isinstance(obj, Item) and obj.kind == 'song':
        song_id = _song_id(obj)
        return ('song', song_id) if song_id else None
    if (isinstance(obj, Item) and obj.kind in RATED_KINDS and obj.id
            and not _synthetic(obj.id) and not obj.favourites):
        return obj.kind, obj.id
    return None


def library_target(obj):
    """(kind, id) that Add to Library adds, or None: only what is not the library's already
    (a catalog id), a song, album, playlist or video."""
    if isinstance(obj, Track):
        return (obj.kind or 'song', obj.id) if obj.id and not is_library_id(obj.id) else None
    if isinstance(obj, Item) and obj.kind in LIBRARY_KINDS and obj.id and not is_library_id(
            obj.id):
        return obj.kind, obj.id
    return None


def playlist_track(obj):
    """(kind, id) that Add to Playlist and a drop add, or None: a Track's or a song or music
    video Item's own id (a library "i." id, or a catalog one), as a 'song' or a 'video'
    (library.TrackRef.for_object's rule)."""
    ref = TrackRef.for_object(obj)
    return (ref.kind, ref.song_id) if ref is not None else None


def playlist_song(obj):
    """The id Add to Playlist and a drop add (playlist_track), or None."""
    track = playlist_track(obj)
    return track[1] if track is not None else None


def is_user_folder(obj):
    """Whether obj is one of the user's playlist folders (not the top level, ROOT_FOLDER):
    one that can be renamed and deleted."""
    return (isinstance(obj, Item) and obj.kind == 'folder' and bool(obj.id)
            and obj.id != ROOT_FOLDER)


def is_manageable(obj):
    """Whether obj can be renamed and deleted: a playlist of the user's (Item.editable: not
    Favourite Songs, nor one of Apple's or someone else's), or a folder of theirs."""
    return (isinstance(obj, Item) and obj.kind == 'playlist' and obj.editable) or (
        is_user_folder(obj))


def entry_target(container, obj):
    """The item-remove-from-playlist target for the Track obj shown in the playlist Item
    `container` (one of the user's): (playlist id, track id, its index), or None."""
    if not isinstance(obj, Track) or not is_manageable(container) or not obj.id:
        return None
    if container.kind != 'playlist':
        return None
    return GLib.Variant('(ssi)', (container.id, obj.id, obj.index))


def manage_items(obj):
    """Rename… and Delete Playlist… (or Delete Folder…) for obj, when it is manageable."""
    if not is_manageable(obj):
        return []
    target = GLib.Variant('(ss)', (obj.kind, obj.id))
    return [menu_item('item-rename', target),
            menu_item('item-delete', target, label=delete_label(obj.kind))]


def is_apple_music_url(url):
    """Whether url is a page of music.apple.com over https: the only addresses the app
    opens or copies. An Item's `url` comes from Apple's answers, but is checked all the
    same; anything else falls back to the address made from the item's id."""
    try:
        parts = urlsplit(url or '')
    except ValueError:
        return False
    return parts.scheme == 'https' and (parts.hostname or '').lower() in APPLE_HOSTS


def is_shareable(url):
    """Whether an address opens for anyone: a music.apple.com page that is not one of the
    web player's library routes (those open only for the account signed in)."""
    return is_apple_music_url(url) and not url.startswith(f'{WEB}/library/')


def web_url(obj, storefront=None):
    """The music.apple.com page of obj, or None: the Item's own URL (when it is one of
    Apple's, is_apple_music_url); the web player's page of a library playlist or album; a
    catalog item's page by its id; a track's song page by its catalog id."""
    storefront = storefront or 'us'
    if isinstance(obj, Track):
        song_id = obj.catalog_id or (obj.id if not is_library_id(obj.id) else None)
        path = WEB_PATHS[obj.kind or 'song']
        return f'{WEB}/{storefront}/{path}/{song_id}' if song_id else None
    if not isinstance(obj, Item) or not obj.id:
        return None
    if obj.url and is_apple_music_url(obj.url):
        return obj.url
    if is_library_id(obj.id):
        route = LIBRARY_ROUTES.get(obj.kind)
        if route and not _synthetic(obj.id):
            return f'{WEB}/{route}/{obj.id}'
        if obj.kind == 'song' and obj.catalog_id:
            return f'{WEB}/{storefront}/song/{obj.catalog_id}'
        return None
    path = WEB_PATHS.get(obj.kind)
    return f'{WEB}/{storefront}/{path}/{obj.id}' if path else None


def share_url(obj, storefront=None):
    """The address Copy Link copies, or None: obj's page when anyone can open it
    (is_shareable), never a library route."""
    url = web_url(obj, storefront)
    return url if is_shareable(url) else None


def needs_catalog_url(obj):
    """Whether obj's link is better asked of the engine: a library album (real, not made up)
    has a catalog page others can open, where its library route opens only for the owner."""
    return (isinstance(obj, Item) and obj.kind == 'album' and not obj.url
            and is_library_id(obj.id) and not _synthetic(obj.id))


def needs_catalog_artist(obj):
    """Whether obj's page is found through the engine: an artist the library made up from
    its songs' names (no catalog id, no URL), whose catalog artist the engine finds through
    those songs (Engine.catalog_artist, related.artist_song_ids)."""
    return (isinstance(obj, Item) and obj.kind == 'artist' and not obj.url
            and related.artist_catalog_id(obj) is None)


def menu_item(action, target, label=None):
    """A Gio.MenuItem running win.<action> with target, labelled as action_labels() says."""
    item = Gio.MenuItem.new(label or action_labels()[action], None)
    item.set_action_and_target_value(f'win.{action}', target)
    return item


def favourite_items(target):
    """Favourite and Remove from Favourites, of which a menu shows the one whose action is
    enabled (GMenu's hidden-when): ItemActions keeps the two actions' state as the item's
    loved state, so the menu follows an answer without its items being replaced."""
    items = []
    for action in ('item-love', 'item-unlove'):
        item = menu_item(action, target)
        item.set_attribute_value('hidden-when', GLib.Variant('s', 'action-disabled'))
        items.append(item)
    return items


def mnemonic_escaped(title):
    """A name of the user's as a menu label: its underscores doubled, since the label's
    underscores are mnemonics."""
    return title.replace('_', '__')


def build_menu(obj, playlists=(), storefront=None, here=None, queued=False, container=None):
    """The menu for obj (an Item or a Track), or None when nothing applies (the top level
    of the folders, a category). `playlists` are the (id, title) pairs the Add to Playlist
    submenu lists; `here` is the Item of the page shown (Go to Album and Go to Artist are
    left out where they would go nowhere: related.shows); `queued`, obj is in the player's
    queue (no Play); `container`, the playlist Item the track obj is shown in (Remove from
    Playlist, when it is one of the user's)."""
    named = describe(obj)
    if named is None or (isinstance(obj, Item) and obj.kind not in MENU_KINDS):
        return None
    target = GLib.Variant('(ss)', named)
    sections = [Gio.Menu(), Gio.Menu(), Gio.Menu(), Gio.Menu(), Gio.Menu()]
    play, go, keep, share, manage = sections
    if can_play(obj) and not queued:
        play.append_item(menu_item('item-play', target))
    if queue_target(obj) is not None:
        play.append_item(menu_item('item-play-next', target))
        play.append_item(menu_item('item-play-later', target))
    if related.has_album(obj) and not related.shows(here, obj, 'album'):
        go.append_item(menu_item('item-go-to-album', target))
    if related.has_artist(obj) and not related.shows(here, obj, 'artist'):
        go.append_item(menu_item('item-go-to-artist', target))
    if rating_target(obj) is not None:
        for item in favourite_items(target):
            keep.append_item(item)
    if library_target(obj) is not None:
        keep.append_item(menu_item('item-add-to-library', target))
    if playlist_song(obj):
        # The playlists that take songs, then New Playlist…, one holding this song.
        submenu = Gio.Menu()
        listed = Gio.Menu()
        for playlist_id, title in playlists:
            listed.append_item(menu_item(
                'item-add-to-playlist', GLib.Variant('(sss)', (playlist_id, *named)),
                label=mnemonic_escaped(title or _('Untitled Playlist'))))
        if listed.get_n_items():
            submenu.append_section(None, listed)
        created = Gio.Menu()
        created.append_item(menu_item('item-new-playlist', target))
        submenu.append_section(None, created)
        keep.append_submenu(action_labels()['item-add-to-playlist'], submenu)
    removed = entry_target(container, obj)
    if removed is not None:
        keep.append_item(menu_item('item-remove-from-playlist', removed))
    if web_url(obj, storefront) or needs_catalog_artist(obj):
        share.append_item(menu_item('item-open-in-browser', target))
        # Copy Link: an address anyone can open, or a library album's or artist's catalog
        # page, which the engine can say when the link is asked for.
        if share_url(obj, storefront) or needs_catalog_url(obj) or needs_catalog_artist(obj):
            share.append_item(menu_item('item-copy-link', target))
    for item in manage_items(obj):
        manage.append_item(item)
    menu = Gio.Menu()
    for section in sections:
        if section.get_n_items():
            menu.append_section(None, section)
    return menu if menu.get_n_items() else None


def fill_sidebar_menu(menu, item):
    """Make `menu` a sidebar item's: a playlist's Play, Play Next, Open in Browser
    (SIDEBAR_ACTIONS), then Rename… and Delete Playlist… for one of the user's; a folder's
    New Playlist… (in it), Rename… and Delete Folder…; All Playlists' (the folder Item
    ROOT_FOLDER) New Playlist…. Empty when `item` is None."""
    menu.remove_all()
    named = describe(item)
    if named is None:
        return
    target = GLib.Variant('(ss)', named)
    first = Gio.Menu()
    if item.kind == 'folder':
        first.append_item(menu_item('item-new-playlist', target))
    elif item.kind == 'playlist':
        for action in SIDEBAR_ACTIONS:
            if action == 'item-play' and not can_play(item):
                continue
            if action == 'item-play-next' and queue_target(item) is None:
                continue
            if action == 'item-open-in-browser' and not web_url(item):
                continue
            first.append_item(menu_item(action, target))
    manage = Gio.Menu()
    for entry in manage_items(item):
        manage.append_item(entry)
    for section in (first, manage):
        if section.get_n_items():
            menu.append_section(None, section)


def now_playing_track(now_playing):
    """An entry of the player's queue (a player.NowPlaying: the item playing, an Up Next
    row's) as a Track a menu is built for, or None for what has no menu (a station's segment,
    an ad: NowPlaying.kind ''). Its `type` is an API type of its kind (MusicKit names its own
    items 'song'), so it is rated, added and opened as what it is; it has no group to play."""
    if now_playing is None or now_playing.kind not in ('song', 'video'):
        return None
    raw = getattr(now_playing, 'raw', None)
    data = dict(raw) if isinstance(raw, dict) else {}
    data.update(id=now_playing.id, catalogId=now_playing.catalog_id or None,
                title=now_playing.title, artist=now_playing.artist, album=now_playing.album)
    if TRACK_KINDS.get(data.get('type')) != now_playing.kind:
        data['type'] = 'music-videos' if now_playing.kind == 'video' else 'songs'
    track = Track(data)
    return track if track.id or track.catalog_id else None


def not_found_message(kind):
    """The toast when Go to Album (`kind` 'album') or Go to Artist finds nothing."""
    return _('Could not find the album') if kind == 'album' else _('Could not find the artist')


def love_messages(kind):
    """(loved, unloved) toasts for an item of kind: a song goes into Favourite Songs."""
    if kind == 'song':
        return _('Added to Favourite Songs'), _('Removed from Favourite Songs')
    return _('Added to Favourites'), _('Removed from Favourites')


class ItemActions:
    """The window's item actions. `window` gives play_request(), get_clipboard() and
    add_action(); `app` the engine, the player, the library, spawn(), toast(), report() and
    refuse_in_demo(). See the module."""

    def __init__(self, window, app):
        self.window = window
        self.app = app
        self._remembered = OrderedDict()  # (kind, id) -> the object a menu was built for
        self._loved = {}  # rating_target -> bool, as the engine last said
        self._menu_rated = None  # the rating target of the last menu built
        app.engine.connect('rated', self._on_rated)
        handlers = {
            'item-play': self._on_play,
            'item-play-next': self._on_play_next,
            'item-play-later': self._on_play_later,
            'item-love': self._on_love,
            'item-unlove': self._on_unlove,
            'item-add-to-library': self._on_add_to_library,
            'item-go-to-album': self._on_go_to_album,
            'item-go-to-artist': self._on_go_to_artist,
            'item-open-in-browser': self._on_open_in_browser,
            'item-copy-link': self._on_copy_link,
            'item-new-playlist': self._on_new_playlist,
            'item-rename': self._on_rename,
            'item-delete': self._on_delete,
        }
        self.actions = {}
        for name, handler in handlers.items():
            self._add(name, TARGET, handler)
        self._add('item-add-to-playlist', PLAYLIST_TARGET, self._on_add_to_playlist)
        self._add('item-remove-from-playlist', ENTRY_TARGET, self._on_remove_from_playlist)

    def _add(self, name, parameter_type, handler):
        action = Gio.SimpleAction.new(name, parameter_type)
        action.connect('activate', handler)
        self.window.add_action(action)
        self.actions[name] = action

    @property
    def engine(self):
        return self.app.engine

    @property
    def library(self):
        return self.app.library

    # -- menus -----------------------------------------------------------------------------

    def menu_for(self, obj, queued=False):
        """The menu for obj (build_menu), obj remembered for its actions; None when nothing
        applies. `queued`: obj is in the player's queue (now_playing_track()). The favourite
        actions' state shows whether obj is loved, as last known; with the engine up, it is
        asked again as the menu opens."""
        named = describe(obj)
        if named is None:
            return None
        menu = build_menu(obj, self.playlists() if playlist_song(obj) else (),
                          self._storefront(), here=self._shown(), queued=queued,
                          container=self.container(obj))
        if menu is None:
            return None
        self._remember(named, obj)
        rated = rating_target(obj)
        self._menu_rated = rated
        self._show_loved(bool(self._loved.get(rated)) if rated else False)
        if rated is not None and self._engine_ready():
            self.app.spawn(self._refine(rated))
        return menu

    def fill_sidebar_menu(self, menu, item):
        """The sidebar's menu for a playlist or a folder (None: emptied). See
        fill_sidebar_menu()."""
        fill_sidebar_menu(menu, item)
        named = describe(item)
        if named is not None:
            self._remember(named, item)

    def container(self, obj):
        """The playlist of the user's that the Track obj is shown in (its group plays that
        playlist: a playlist's page), or None: where Remove from Playlist applies."""
        if not isinstance(obj, Track) or obj.play.get('kind') != 'playlist':
            return None
        playlist = self.library.by_id('playlist', obj.play.get('id'))
        return playlist if is_manageable(playlist) else None

    def playlists(self):
        """(id, title) of the library playlists that take songs, in the sidebar's order with
        the folders flattened (Favourite Songs and the ones Apple says cannot be edited left
        out)."""
        tree = self.library.playlist_tree()
        return [(node.item.id, node.item.title) for node in tree.flat
                if node.kind == 'playlist' and node.item.editable]

    def _show_loved(self, loved):
        """The favourite actions' state: a menu shows Favourite while the item is not
        loved, Remove from Favourites while it is."""
        self.actions['item-love'].set_enabled(not loved)
        self.actions['item-unlove'].set_enabled(loved)

    async def _refine(self, rated):
        try:
            await self.engine.rating(*rated)  # its `rated` signal brings the answer
        except EngineError as error:
            log.debug('rating of %s %s: %s', *rated, error)

    def _on_rated(self, _engine, kind, item_id, value):
        self._loved[(kind, item_id)] = value == 1
        if (kind, item_id) == self._menu_rated:
            self._show_loved(value == 1)

    def _remember(self, named, obj):
        self._remembered[named] = obj
        self._remembered.move_to_end(named)
        while len(self._remembered) > REMEMBERED:
            self._remembered.popitem(last=False)

    def _resolve(self, kind, item_id):
        """The object a target names: one a menu was built for, else the library's."""
        obj = self._remembered.get((kind, item_id))
        if obj is None and kind != 'song':
            obj = self.library.by_id(kind, item_id)
        return obj

    def _unpack(self, parameter):
        kind, item_id = parameter.unpack()
        return self._resolve(kind, item_id), kind, item_id

    def _storefront(self):
        return getattr(self.library, 'storefront', None) or 'us'

    def _shown(self):
        """The Item of the page shown (window.shown_item), or None."""
        shown_item = getattr(self.window, 'shown_item', None)
        return shown_item() if shown_item is not None else None

    def _engine_ready(self):
        return (not self.app.demo and self.engine.state == 'up'
                and bool(self.engine.authorized))

    # -- running -----------------------------------------------------------------------------

    def _run(self, method, args, message=None, ensure=True, after=None):
        """await method(*args), with the engine up first when `ensure` (started when it is
        down and the account signed in; the Player's own commands do that themselves), then
        toast message and await after(); an EngineError is reported instead (app.report).
        The task, or None with the demo library (which says so)."""
        if self.app.refuse_in_demo():
            return None

        async def run():
            try:
                if ensure:
                    await self.app.player.ensure_engine()
                await method(*args)
            except EngineError as error:
                self.app.report(error)
                return
            if message:
                self.app.toast(message)
            if after is not None:
                await after()

        return self.app.spawn(run())

    def _on_play(self, _action, parameter):
        obj, kind, item_id = self._unpack(parameter)
        if isinstance(obj, Track):
            if obj.play.get('kind') and obj.play.get('id'):
                self.window.play_request(obj.play, start_with=obj.index, start_id=obj.id)
            else:
                self.window.play_request({'kind': 'song', 'id': _song_id(obj)})
        elif isinstance(obj, Item):
            self.window.play_request(obj.play)
        else:
            self.window.play_request({'kind': kind, 'id': item_id})

    def _queue(self, parameter, later):
        obj, kind, item_id = self._unpack(parameter)
        target = queue_target(obj) if obj is not None else (kind, item_id)
        if target is None:
            self.app.toast(_('This cannot be queued'))
            return None
        title = obj.title if obj is not None else ''
        if later:
            message = (_('“{title}” will play later').format(title=title) if title
                       else _('Playing later'))
            return self._run(self.app.player.play_later, target, message, ensure=False)
        message = (_('“{title}” will play next').format(title=title) if title
                   else _('Playing next'))
        return self._run(self.app.player.play_next, target, message, ensure=False)

    def _on_play_next(self, _action, parameter):
        return self._queue(parameter, later=False)

    def _on_play_later(self, _action, parameter):
        return self._queue(parameter, later=True)

    def _rate(self, parameter, love):
        obj, kind, item_id = self._unpack(parameter)
        target = rating_target(obj) if obj is not None else (kind, item_id)
        if target is None:
            self.app.toast(_('This cannot be a favourite'))
            return None
        loved, unloved = love_messages(target[0])
        # A song loved goes into Favourite Songs: that playlist is fetched again.
        after = self._refresh_favourites if target[0] == 'song' else None
        if love:
            return self._run(self.engine.love, target, loved, after=after)
        return self._run(self.engine.unlove, target, unloved, after=after)

    def _on_love(self, _action, parameter):
        return self._rate(parameter, love=True)

    def _on_unlove(self, _action, parameter):
        return self._rate(parameter, love=False)

    def _on_add_to_library(self, _action, parameter):
        obj, kind, item_id = self._unpack(parameter)
        target = library_target(obj) if obj is not None else (kind, item_id)
        if target is None:
            self.app.toast(_('This is in your library already'))
            return None
        title = obj.title if obj is not None else ''
        message = (_('Added “{title}” to your library').format(title=title) if title
                   else _('Added to your library'))
        return self._run(self.engine.add_to_library, target, message,
                         after=self._refresh_library)

    def _on_go_to_album(self, _action, parameter):
        return self.go_to(self._unpack(parameter)[0], 'album')

    def _on_go_to_artist(self, _action, parameter):
        return self.go_to(self._unpack(parameter)[0], 'artist')

    def go_to(self, obj, kind):
        """Open the page of the album obj is on (`kind` 'album') or of the artist it is by
        ('artist'). An artist's is Apple Music's while the engine is up and obj has a catalog
        id (the engine names the artist exactly; their page shows the library's albums of
        theirs too); else, and for an album, the library's (related.library_album,
        library_artist) at once, else the catalog's, which the engine looks up (started first
        if need be, as for the other actions). Nothing is opened when that page is the one
        shown. A toast says when there is none. The engine's task, or None."""
        if obj is None:
            self.app.toast(not_found_message(kind))
            return None
        target = related.catalog_target(obj)
        if kind == 'artist' and target is not None and self._engine_ready():
            return self.app.spawn(self._go_to_catalog(obj, kind, target))
        if self._go_to_library(obj, kind):
            return None
        if target is None or (kind == 'album' and target[0] == 'album'):
            self.app.toast(not_found_message(kind))
            return None
        if self.app.refuse_in_demo():
            return None
        return self.app.spawn(self._go_to_catalog(obj, kind, target))

    async def _go_to_catalog(self, obj, kind, target):
        try:
            await self.app.player.ensure_engine()
            answer = await self.engine.related(*target)
        except EngineError as error:
            if self._go_to_library(obj, kind):
                pass  # the library's page, rather than nothing
            elif error.status == 404:  # gone from the storefront's catalog: nowhere to go
                self.app.toast(not_found_message(kind))
            else:
                self.app.report(error)
            return
        found = related.from_answer(self.library, answer, kind, related.artist_name(obj))
        if found is not None:
            self._show_page(found)
        elif not self._go_to_library(obj, kind):
            self.app.toast(not_found_message(kind))

    def _go_to_library(self, obj, kind):
        """Show the library's album or artist of obj's: True when it has one."""
        found = (related.library_album(self.library, obj) if kind == 'album'
                 else related.library_artist(self.library, obj))
        if found is not None:
            self._show_page(found)
        return found is not None

    def _show_page(self, item):
        """Show item's page, unless it is the page shown already: an artist's is Apple
        Music's (window.open_artist_page), for an artist of the library's too."""
        if related.same_page(self._shown(), item):
            return
        if item.kind == 'artist':
            self.window.open_artist_page(item)
        else:
            self.window.open_item(item)

    def show_artist(self, obj):
        """A link's artist (a row's, an album page's): what the library holds of the artist
        obj is by when the library has them (related.library_artist, window.open_item), else
        where Go to Artist goes."""
        found = related.library_artist(self.library, obj)
        if found is not None:
            self.window.open_item(found)
            return None
        return self.go_to(obj, 'artist')

    async def _refresh_library(self):
        """Show what was just added: a quick sync (sync.py), which reads the songs and the
        shelves and keeps the rest of last time's file, so the item is in Songs and in
        Recently Added at once. Apple answers the write with no body, so there is nothing to
        merge in as _refresh_playlist() does; the item has to be read back."""
        self.app.start_sync(quick=True)

    def _on_add_to_playlist(self, _action, parameter):
        playlist_id, kind, item_id = parameter.unpack()
        obj = self._resolve(kind, item_id)
        track = playlist_track(obj) if obj is not None else (kind, item_id)
        if track is None:
            track = (kind, item_id)
        return self.add_to_playlist(playlist_id, track[1], obj.title if obj is not None else '',
                                    kind=track[0])

    def add_to_playlist(self, playlist_id, song_id, title='', kind='song'):
        """Add the song (or music video, `kind` 'video') to the library playlist, confirming
        with a toast that names both, and fetch the playlist again so its page shows it."""
        if not song_id:
            self.app.toast(_('Only songs can be added to a playlist'))
            return None
        playlist = self.library.by_id('playlist', playlist_id)
        name = playlist.title if playlist is not None else ''
        if title and name:
            message = _('Added “{title}” to “{playlist}”').format(title=title, playlist=name)
        elif name:
            message = _('Added to “{playlist}”').format(playlist=name)
        else:
            message = _('Added to the playlist')
        return self._run(self.engine.add_to_playlist, (playlist_id, song_id, kind), message,
                         after=lambda: self._refresh_playlist(playlist_id))

    async def _refresh_playlist(self, playlist_id):
        """Bring a library playlist up to date after it changed on Apple's side (a song
        added, Favourite Songs loved into): the engine's full Item merged into the
        library's, whose groups-changed re-shows an open page. A failure is only logged:
        the next sync brings the change anyway."""
        playlist = self.library.by_id('playlist', playlist_id)
        if playlist is None:
            return
        try:
            answer = await self.engine.item('playlist', playlist_id)
        except EngineError as error:
            log.debug('playlist %s not refreshed: %s', playlist_id, error)
            return
        if isinstance(answer, dict):
            playlist.merge(answer)

    async def _refresh_favourites(self):
        favourites = self.library.favourite_songs()
        if favourites is not None and favourites.id:
            await self._refresh_playlist(favourites.id)

    def can_drop(self, playlist):
        """Whether a track may be dropped on this sidebar playlist Item (one that takes
        songs; not a folder, not a fixed item, not Favourite Songs)."""
        return isinstance(playlist, Item) and playlist.editable

    def drop(self, playlist, ref):
        """A library.TrackRef dropped on a sidebar playlist: added to it. False when the
        playlist takes nothing (the drop is refused) or the ref names no song."""
        if not self.can_drop(playlist) or not isinstance(ref, TrackRef) or not ref.song_id:
            return False
        self.add_to_playlist(playlist.id, ref.song_id, ref.title, kind=ref.kind or 'song')
        return True

    # -- managing playlists and folders ------------------------------------------------------

    def ask_name(self, heading, confirm, name='', description=None, done=None):
        """The name dialog over the window (dialogs/playlist.py's NameDialog): `name` and
        `description` filled in (None: no description to edit); done(name, description) when
        it is confirmed. The dialog."""
        from .dialogs.playlist import NameDialog

        dialog = NameDialog(heading, confirm, name, description, done)
        dialog.present(self.window)
        return dialog

    def ask_confirm(self, heading, body, confirm, done):
        """The confirmation before something is deleted, over the window
        (dialogs/playlist.py's confirm()): done() when it is confirmed. The dialog."""
        from .dialogs.playlist import confirm as confirm_dialog

        return confirm_dialog(self.window, heading, body, confirm, done)

    def _on_new_playlist(self, _action, parameter):
        """A new playlist, named in a dialog: in the folder the target names, or holding the
        song or video it names."""
        obj, kind, item_id = self._unpack(parameter)
        if kind == 'folder':
            tracks, folder_id, title = (), item_id, ''
        else:
            track = playlist_track(obj) if obj is not None else None
            tracks = (track or (kind, item_id),)
            folder_id, title = None, obj.title if obj is not None else ''
        return self.ask_name(
            _('New Playlist'), _('_Create'), '', '',
            lambda name, description: self.create_playlist(name, description, tracks,
                                                           folder_id, title))

    def create_playlist(self, name, description='', tracks=(), folder_id=None, title=''):
        """Make the playlist (Engine.create_playlist), confirm with a toast and follow Apple's
        listing; one made empty (New Playlist) is then shown. The task, or None."""
        if self.app.refuse_in_demo():
            return None
        if title:
            message = _('Added “{title}” to the new playlist “{playlist}”').format(
                title=title, playlist=name)
        else:
            message = _('Created “{playlist}”').format(playlist=name)

        async def run():
            try:
                await self.app.player.ensure_engine()
                new_id = await self.engine.create_playlist(name, description, tracks,
                                                           folder_id)
            except EngineError as error:
                self.app.report(error)
                return
            self.app.toast(message)
            await self._follow_listing(folder_id or ROOT_FOLDER, new_id, name)
            if not tracks and self.library.by_id('playlist', new_id) is not None:
                self.window.select_page(f'playlist:{new_id}')  # sidebar.playlist_key

        return self.app.spawn(run())

    def _on_rename(self, _action, parameter):
        obj = self._unpack(parameter)[0]
        if not is_manageable(obj):
            self.app.toast(_('This cannot be renamed'))
            return None
        if obj.kind == 'folder':
            return self.ask_name(_('Rename Folder'), _('_Rename'), obj.title, None,
                                 lambda name, _description: self.rename(obj, name))
        return self.ask_name(_('Rename Playlist'), _('_Rename'), obj.title, obj.summary or '',
                             lambda name, description: self.rename(obj, name, description))

    def rename(self, obj, name, description=None):
        """Give a playlist of the user's its name (and its description, unless None), or a
        folder its name: the Item and the sidebar follow at once, the library once Apple
        lists it. The task, or None."""
        if obj.kind == 'folder':
            method, args = self.engine.rename_folder, (obj.id, name)
        else:
            method, args = self.engine.edit_playlist, (obj.id, name, description)
        parent = self._parent_of(obj)

        async def after():
            changes = {'title': name}
            if description is not None:
                changes['summary'] = description
            obj.merge(changes)
            self.library.place_playlists()  # the sidebar retitles and places it now
            await self._follow_listing(parent, obj.id, name)

        return self._run(method, args, after=after)

    def _on_delete(self, _action, parameter):
        obj = self._unpack(parameter)[0]
        if not is_manageable(obj):
            self.app.toast(_('This cannot be deleted'))
            return None
        name = obj.title
        if obj.kind == 'folder':
            count = sum(1 for kind, _id in self._inside(obj) if kind == 'playlist')
            heading = _('Delete Folder?')
            if count:
                body = ngettext(
                    '“{name}” and the playlist in it will be deleted from your library on all '
                    'your devices. This cannot be undone.',
                    '“{name}” and the {count} playlists in it will be deleted from your '
                    'library on all your devices. This cannot be undone.',
                    count).format(name=name, count=count)
            else:
                body = _('“{name}” will be deleted from your library on all your devices. '
                         'This cannot be undone.').format(name=name)
        else:
            heading = _('Delete Playlist?')
            body = _('“{name}” will be deleted from your library on all your devices. This '
                     'cannot be undone.').format(name=name)
        return self.ask_confirm(heading, body, _('_Delete'), lambda: self.delete(obj))

    def delete(self, obj):
        """Delete a playlist of the user's, or a folder with what is in it: the window leaves
        their pages, and the library follows once Apple's listing has them gone. The task,
        or None."""
        if obj.kind == 'folder':
            method = self.engine.delete_folder
        else:
            method = self.engine.delete_playlist
        parent = self._parent_of(obj)
        gone = {(obj.kind, obj.id), *self._inside(obj)}

        async def after():
            self.window.leave(gone, parent)
            await self._follow_listing(parent, obj.id, None)

        return self._run(method, (obj.id,), _('Deleted “{name}”').format(name=obj.title),
                         after=after)

    def _on_remove_from_playlist(self, _action, parameter):
        playlist_id, track_id, index = parameter.unpack()
        track = self._remembered.get(('song', track_id))
        title = track.title if track is not None else ''
        playlist = self.library.by_id('playlist', playlist_id)
        name = playlist.title if playlist is not None else ''
        if title and name:
            message = _('Removed “{title}” from “{playlist}”').format(title=title,
                                                                    playlist=name)
        else:
            message = _('Removed from the playlist')
        return self._run(self.engine.remove_from_playlist, (playlist_id, track_id, index),
                         message, after=lambda: self._refresh_playlist(playlist_id))

    def _tree_node(self, obj):
        """obj's node in the library's PlaylistTree, or None."""
        for node in self.library.playlist_tree().flat:
            if node.kind == obj.kind and node.id == obj.id:
                return node
        return None

    def _parent_of(self, obj):
        """The id of the folder holding a playlist or folder (ROOT_FOLDER: the top level)."""
        node = self._tree_node(obj)
        if node is None or node.parent is None or node.parent.parent is None:
            return ROOT_FOLDER
        return node.parent.id

    def _inside(self, obj):
        """(kind, id) of everything in a folder, however deep; nothing for a playlist."""
        node = self._tree_node(obj) if obj.kind == 'folder' else None
        found = []
        stack = list(node.children) if node is not None else []
        while stack:
            child = stack.pop()
            found.append((child.kind, child.id))
            stack.extend(child.children)
        return found

    async def _follow_listing(self, folder_id, item_id, name):
        """After a playlist write: wait until Apple lists it (wait_listed), then bring the
        library up to date with the playlists pass of a sync, and wait for that."""
        await self.wait_listed(folder_id, item_id, name)
        task = self.app.start_sync(playlists=True)
        if task is not None:
            await asyncio.wait([task])

    async def wait_listed(self, folder_id, item_id, name):
        """Wait until Apple lists the playlist or folder item_id in the folder folder_id
        under `name` (None: not at all), looking again after each of SETTLE_DELAYS. True
        once it does; False when it never did, or the engine could not say."""
        for delay in SETTLE_DELAYS:
            await asyncio.sleep(delay)
            try:
                children = await self.engine.folder_children(folder_id)
            except EngineError as error:
                log.debug('playlist listing not read: %s', error)
                return False
            found = next((child for child in children if child['id'] == item_id), None)
            if name is None and found is None:
                return True
            if name is not None and found is not None and found['name'] == name:
                return True
        log.info('Apple does not list the change yet: the library follows its listing')
        return False

    # -- links -------------------------------------------------------------------------------

    async def link(self, obj, kind=None, item_id=None):
        """obj's music.apple.com page (web_url), a library album's catalog page when the
        engine can say (needs_catalog_url), a library artist's catalog page, which only the
        engine can find (needs_catalog_artist: started first if need be; its EngineError is
        raised), or None."""
        if obj is None:
            obj = Item({'kind': kind, 'id': item_id})
        if needs_catalog_artist(obj):
            return await self._catalog_artist_link(obj)
        if needs_catalog_url(obj) and self._engine_ready():
            try:
                url = await self.engine.catalog_url(obj.kind, obj.id)
            except EngineError as error:
                log.debug('catalog page of %s: %s', obj.id, error)
                url = None
            if url and is_apple_music_url(url):
                return url
        return web_url(obj, self._storefront())

    async def _catalog_artist_link(self, obj):
        songs = related.artist_song_ids(obj, self.library)
        if not songs:
            return None
        await self.app.player.ensure_engine()
        artist_id = await self.engine.catalog_artist(obj.title, songs)
        if not artist_id:
            return None
        return web_url(Item({'kind': 'artist', 'id': artist_id}), self._storefront())

    async def _asked_link(self, obj, kind, item_id):
        """link(), or False when the user has been told why there is none: the demo has no
        engine to ask, or the engine's failure was reported."""
        if needs_catalog_artist(obj) and self.app.refuse_in_demo():
            return False
        try:
            return await self.link(obj, kind, item_id)
        except EngineError as error:
            self.app.report(error)
            return False

    def _on_open_in_browser(self, _action, parameter):
        obj, kind, item_id = self._unpack(parameter)
        return self.app.spawn(self._open(obj, kind, item_id))

    async def _open(self, obj, kind, item_id):
        url = await self._asked_link(obj, kind, item_id)
        if url is False:
            return
        if not url:
            self.app.toast(_('This has no page to open'))
            return
        self.launch(url)

    def launch(self, url):
        """Open url in the default browser (Gtk.UriLauncher), a failure toasted."""
        from gi.repository import Gtk

        log.info('opening %s', url)
        Gtk.UriLauncher.new(url).launch(self.window, None, self._on_launched)

    def _on_launched(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            log.warning('could not open %s: %s', launcher.get_uri(), error.message)
            self.app.toast(_('Could not open the browser'))

    def _on_copy_link(self, _action, parameter):
        obj, kind, item_id = self._unpack(parameter)
        return self.app.spawn(self._copy(obj, kind, item_id))

    async def _copy(self, obj, kind, item_id):
        """The link on the clipboard, when it is one anyone can open (a library album's or
        artist's catalog page comes from the engine; without it, the album has only its
        owner's)."""
        url = await self._asked_link(obj, kind, item_id)
        if url is False:
            return
        if not is_shareable(url):
            self.app.toast(_('This has no link to copy'))
            return
        self.window.get_clipboard().set(url)
        self.app.toast(_('Link copied'))
