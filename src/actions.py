# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""What can be done to an album, playlist, song, station, artist or video: the window's item
actions, the menus that offer them, and what a track dragged onto a sidebar playlist does.

    actions = ItemActions(window, app)     # made once by the window; adds the win.* actions
    menu = actions.menu_for(obj)           # a Gio.Menu for an Item or a Track, or None
    menu = actions.menu_for(track, queued=True)   # the item playing's, or Up Next's
    actions.go_to(obj, 'album')            # obj's album's page ('artist': its artist's)
    actions.fill_sidebar_menu(menu, item)  # a sidebar playlist's Play, Play Next, Open in Browser
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
library playlist. A target names an object the menu was built for (remembered, the last
REMEMBERED of them) or one the library has; failing both, the target's kind and id are used
as they are. Each action awaits the engine (started first when it is down and the account is
signed in, as a play request does) and confirms with a toast, or reports the EngineError
(app.report: signed out, that opens the sign-in). After a song was added to a playlist, or a
song loved or unloved, the playlist (Favourite Songs) is fetched again and merged into the
library's Item, so an open page shows the change at once.

The menus are built in code, per kind (build_menu): Play, Play Next, Play Later; Go to Album,
Go to Artist (not where the page shown is that album or artist already); Favourite and
Remove from Favourites (both, of which the menu shows the one whose action is enabled: the
actions' state says whether the item is loved), Add to Library, Add to Playlist (the
library's playlists that take songs, folders flattened); Open in Browser, Copy Link (only an
address anyone can open: not the web player's library routes); each only where it applies.
The menus of the player's queue (the item playing's in the Now Playing sheet and the player
bar, Up Next's) have no Play, which would replace the queue: activating the row plays it.
Whether an item is loved is not in the library: a menu shows what the engine said last (its
`rated` signal), and asks again (engine.rating) as it opens.

Only Gio, GLib and GObject at import time besides Gtk's UriLauncher, used when a page is
opened: tests build the menus and run the actions with a stand-in window, app and engine.
"""

import logging
from collections import OrderedDict
from gettext import gettext as _
from urllib.parse import urlsplit

from gi.repository import Gio, GLib

from . import related
from .backend.errors import EngineError
from .backend.api import is_library_id
from .library import TRACK_KINDS, Item, Track, TrackRef

log = logging.getLogger(__name__)

TARGET = GLib.VariantType.new('(ss)')
PLAYLIST_TARGET = GLib.VariantType.new('(sss)')

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

MENU_KINDS = ('album', 'playlist', 'song', 'station', 'artist', 'video')
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
    }


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


def build_menu(obj, playlists=(), storefront=None, here=None, queued=False):
    """The menu for obj (an Item or a Track), or None when nothing applies (a folder, a
    category). `playlists` are the (id, title) pairs the Add to Playlist submenu lists;
    `here` is the Item of the page shown (Go to Album and Go to Artist are left out where
    they would go nowhere: related.shows); `queued`, obj is in the player's queue (no
    Play)."""
    named = describe(obj)
    if named is None or (isinstance(obj, Item) and obj.kind not in MENU_KINDS):
        return None
    target = GLib.Variant('(ss)', named)
    sections = [Gio.Menu(), Gio.Menu(), Gio.Menu(), Gio.Menu()]
    play, go, keep, share = sections
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
    if playlist_song(obj) and playlists:
        submenu = Gio.Menu()
        for playlist_id, title in playlists:
            submenu.append_item(menu_item(
                'item-add-to-playlist', GLib.Variant('(sss)', (playlist_id, *named)),
                label=mnemonic_escaped(title or _('Untitled Playlist'))))
        keep.append_submenu(action_labels()['item-add-to-playlist'], submenu)
    if web_url(obj, storefront):
        share.append_item(menu_item('item-open-in-browser', target))
        # Copy Link: an address anyone can open, or a library album's catalog page, which
        # the engine can say when the link is asked for.
        if share_url(obj, storefront) or needs_catalog_url(obj):
            share.append_item(menu_item('item-copy-link', target))
    menu = Gio.Menu()
    for section in sections:
        if section.get_n_items():
            menu.append_section(None, section)
    return menu if menu.get_n_items() else None


def fill_sidebar_menu(menu, playlist):
    """Make `menu` a sidebar playlist's: Play, Play Next, Open in Browser (SIDEBAR_ACTIONS)
    on the playlist, or empty when `playlist` is None (a folder, All Playlists)."""
    menu.remove_all()
    named = describe(playlist)
    if named is None:
        return
    target = GLib.Variant('(ss)', named)
    for action in SIDEBAR_ACTIONS:
        if action == 'item-play' and not can_play(playlist):
            continue
        if action == 'item-play-next' and queue_target(playlist) is None:
            continue
        if action == 'item-open-in-browser' and not web_url(playlist):
            continue
        menu.append_item(menu_item(action, target))


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
        }
        self.actions = {}
        for name, handler in handlers.items():
            self._add(name, TARGET, handler)
        self._add('item-add-to-playlist', PLAYLIST_TARGET, self._on_add_to_playlist)

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
                          self._storefront(), here=self._shown(), queued=queued)
        if menu is None:
            return None
        self._remember(named, obj)
        rated = rating_target(obj)
        self._menu_rated = rated
        self._show_loved(bool(self._loved.get(rated)) if rated else False)
        if rated is not None and self._engine_ready():
            self.app.spawn(self._refine(rated))
        return menu

    def fill_sidebar_menu(self, menu, playlist):
        """The sidebar's menu for a playlist (None: emptied). See fill_sidebar_menu()."""
        fill_sidebar_menu(menu, playlist)
        named = describe(playlist)
        if named is not None:
            self._remember(named, playlist)

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
        ('artist'): the library's (related.library_album, library_artist) at once, else the
        catalog's, which the engine looks up (started first if need be, as for the other
        actions). Nothing is opened when that page is the one shown. A toast says when there
        is none. The engine's task, or None."""
        if obj is None:
            self.app.toast(not_found_message(kind))
            return None
        found = (related.library_album(self.library, obj) if kind == 'album'
                 else related.library_artist(self.library, obj))
        if found is not None:
            self._show_page(found)
            return None
        target = related.catalog_target(obj)
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
            self.app.report(error)
            return
        found = related.from_answer(self.library, answer, kind, related.artist_name(obj))
        if found is None:
            self.app.toast(not_found_message(kind))
            return
        self._show_page(found)

    def _show_page(self, item):
        """Show item's page, unless it is the page shown already."""
        if not related.same_page(self._shown(), item):
            self.window.open_item(item)

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

    # -- links -------------------------------------------------------------------------------

    async def link(self, obj, kind=None, item_id=None):
        """obj's music.apple.com page (web_url), a library album's catalog page when the
        engine can say (needs_catalog_url), or None."""
        if obj is None:
            obj = Item({'kind': kind, 'id': item_id})
        if needs_catalog_url(obj) and self._engine_ready():
            try:
                url = await self.engine.catalog_url(obj.kind, obj.id)
            except EngineError as error:
                log.debug('catalog page of %s: %s', obj.id, error)
                url = None
            if url and is_apple_music_url(url):
                return url
        return web_url(obj, self._storefront())

    def _on_open_in_browser(self, _action, parameter):
        obj, kind, item_id = self._unpack(parameter)
        return self.app.spawn(self._open(obj, kind, item_id))

    async def _open(self, obj, kind, item_id):
        url = await self.link(obj, kind, item_id)
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
        """The link on the clipboard, when it is one anyone can open (a library album's
        catalog page comes from the engine; without it, the album has only its owner's)."""
        url = await self.link(obj, kind, item_id)
        if not is_shareable(url):
            self.app.toast(_('This has no link to copy'))
            return
        self.window.get_clipboard().set(url)
        self.app.toast(_('Link copied'))
