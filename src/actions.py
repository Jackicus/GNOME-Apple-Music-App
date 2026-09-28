"""What can be done to an album, playlist, song, station, artist or video: the window's item
actions, the menus that offer them, and what a track dragged onto a sidebar playlist does.

    actions = ItemActions(window, app)     # made once by the window; adds the win.* actions
    menu = actions.menu_for(obj)           # a Gio.Menu for an Item or a Track, or None
    actions.fill_sidebar_menu(menu, item)  # a sidebar playlist's Play, Play Next, Open in Browser
    actions.drop(playlist, ref)            # a TrackRef dropped on a sidebar playlist

The actions, each on the window (win.*) with a GLib.Variant target "(ss)", the (kind, id) of
what it acts on (an Item's kind and id, or 'song' and a Track's id):

    item-play            the item, or a track's album or playlist from the track
    item-play-next       queued after the item playing (mk.playNext)
    item-play-later      queued at the end (mk.playLater)
    item-love            loved: a song goes into Favourite Songs
    item-unlove          the love taken back
    item-add-to-library  a catalog item added to the library
    item-open-in-browser its music.apple.com page, in the default browser (Gtk.UriLauncher)
    item-copy-link       that page's address, on the clipboard

and item-add-to-playlist, "(sss)" (playlist id, kind, id): a song added to a library playlist.
A target names an object the menu was built for (remembered, the last REMEMBERED of them) or
one the library has; failing both, the target's kind and id are used as they are. Each action
awaits the engine (started first when it is down and the account is signed in, as a play
request does) and confirms with a toast, or toasts the EngineError (app.report).

The menus are built in code, per kind (build_menu): Play, Play Next, Play Later; Favourite or
Remove from Favourites, Add to Library, Add to Playlist (the library's playlists that take
songs, folders flattened); Open in Browser, Copy Link; each only where it applies. Whether an
item is loved is not in the library: a menu shows what the engine said last (its `rated`
signal), and asks again (engine.rating) as it opens, swapping the item when the answer differs.

Only Gio, GLib and GObject at import time besides Gtk's UriLauncher, used when a page is
opened: tests build the menus and run the actions with a stand-in window, app and engine.
"""

import logging
from collections import OrderedDict
from gettext import gettext as _

from gi.repository import Gio, GLib, GObject

from .backend.errors import EngineError
from .engine import is_library_id
from .library import Item, Track

log = logging.getLogger(__name__)

TARGET = GLib.VariantType.new('(ss)')
PLAYLIST_TARGET = GLib.VariantType.new('(sss)')

# How many of the objects menus were built for are remembered for their actions.
REMEMBERED = 64

WEB = 'https://music.apple.com'
# The page of a catalog item without a URL: music.apple.com/<storefront>/<path>/<id>, which
# Apple redirects to the canonical address with the item's name in it.
WEB_PATHS = {'album': 'album', 'playlist': 'playlist', 'song': 'song', 'station': 'station',
             'artist': 'artist', 'video': 'music-video'}
# The web player's own routes for library items (as its sidebar links and its router name
# them): a library playlist's and a library album's pages.
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
    """The menu's labels, by action, translated on call (after gettext is set up)."""
    return {
        'item-play': _('Play'),
        'item-play-next': _('Play Next'),
        'item-play-later': _('Play Later'),
        'item-love': _('Favourite'),
        'item-unlove': _('Remove from Favourites'),
        'item-add-to-library': _('Add to Library'),
        'item-add-to-playlist': _('Add to Playlist'),
        'item-open-in-browser': _('Open in Browser'),
        'item-copy-link': _('Copy Link'),
    }


class TrackRef(GObject.Object):
    """What a dragged track carries (Gdk.ContentProvider.new_for_value), and what a sidebar
    playlist accepts: the song's id, a library "i." id or a catalog one, and its title."""

    __gtype_name__ = 'AppleMusicTrackRef'

    song_id = GObject.Property(type=str, default='')
    title = GObject.Property(type=str, default='')

    @classmethod
    def for_object(cls, obj):
        """The TrackRef of a Track or a song Item, or None for anything else."""
        song_id = playlist_song(obj)
        return cls(song_id=song_id, title=obj.title or '') if song_id else None


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
    rating: a song by its catalog id (the heart's too), an album, playlist, station or video
    by its own id. Not Favourite Songs itself, nor an item the library made up."""
    if isinstance(obj, Track) or (isinstance(obj, Item) and obj.kind == 'song'):
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
        return ('song', obj.id) if obj.id and not is_library_id(obj.id) else None
    if isinstance(obj, Item) and obj.kind in LIBRARY_KINDS and obj.id and not is_library_id(
            obj.id):
        return obj.kind, obj.id
    return None


def playlist_song(obj):
    """The song id Add to Playlist and a drop add: a Track's or a song Item's own id (a
    library "i." id, or a catalog one), or None."""
    if isinstance(obj, Track) or (isinstance(obj, Item) and obj.kind == 'song'):
        return obj.id or obj.catalog_id or None
    return None


def web_url(obj, storefront=None):
    """The music.apple.com page of obj, or None: the Item's own URL; the web player's page of
    a library playlist or album; a catalog item's page by its id; a track's song page by its
    catalog id."""
    storefront = storefront or 'us'
    if isinstance(obj, Track):
        song_id = obj.catalog_id or (obj.id if not is_library_id(obj.id) else None)
        return f'{WEB}/{storefront}/song/{song_id}' if song_id else None
    if not isinstance(obj, Item) or not obj.id:
        return None
    if obj.url:
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


def favourite_item(target, loved):
    return menu_item('item-unlove' if loved else 'item-love', target)


def build_menu(obj, playlists=(), loved=False, storefront=None):
    """The menu for obj (an Item or a Track), or None when nothing applies (a folder, a
    category). `playlists` are the (id, title) pairs the Add to Playlist submenu lists;
    `loved` whether obj is known to be loved (Remove from Favourites instead of Favourite)."""
    named = describe(obj)
    if named is None or (isinstance(obj, Item) and obj.kind not in MENU_KINDS):
        return None
    target = GLib.Variant('(ss)', named)
    sections = [Gio.Menu(), Gio.Menu(), Gio.Menu()]
    play, keep, share = sections
    if can_play(obj):
        play.append_item(menu_item('item-play', target))
    if queue_target(obj) is not None:
        play.append_item(menu_item('item-play-next', target))
        play.append_item(menu_item('item-play-later', target))
    if rating_target(obj) is not None:
        keep.append_item(favourite_item(target, loved))
    if library_target(obj) is not None:
        keep.append_item(menu_item('item-add-to-library', target))
    if playlist_song(obj) and playlists:
        submenu = Gio.Menu()
        for playlist_id, title in playlists:
            submenu.append_item(menu_item(
                'item-add-to-playlist', GLib.Variant('(sss)', (playlist_id, *named)),
                label=title or _('Untitled Playlist')))
        keep.append_submenu(action_labels()['item-add-to-playlist'], submenu)
    if web_url(obj, storefront):
        share.append_item(menu_item('item-open-in-browser', target))
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


def set_favourite(menu, target, loved):
    """Swap the Favourite / Remove from Favourites item of a menu built by build_menu for the
    other one, where the loved state it shows is not `loved`. True when it changed."""
    wanted = 'win.item-unlove' if loved else 'win.item-love'
    for position in range(menu.get_n_items()):
        section = menu.get_item_link(position, Gio.MENU_LINK_SECTION)
        if section is None:
            continue
        for index in range(section.get_n_items()):
            action = section.get_item_attribute_value(index, Gio.MENU_ATTRIBUTE_ACTION)
            action = action.get_string() if action is not None else None
            if action not in ('win.item-love', 'win.item-unlove'):
                continue
            if action == wanted:
                return False
            section.remove(index)
            section.insert_item(index, favourite_item(target, loved))
            return True
    return False


def love_messages(kind):
    """(loved, unloved) toasts for an item of kind: a song goes into Favourite Songs."""
    if kind == 'song':
        return _('Added to Favourite Songs'), _('Removed from Favourite Songs')
    return _('Added to Favourites'), _('Removed from Favourites')


class ItemActions:
    """The window's item actions. `window` gives toast(), play_request(), get_clipboard()
    and add_action(); `app` the engine, the player, the library, spawn(), report() and demo.
    See the module."""

    def __init__(self, window, app):
        self.window = window
        self.app = app
        self._remembered = OrderedDict()  # (kind, id) -> the object a menu was built for
        self._loved = {}  # rating_target -> bool, as the engine last said
        app.engine.connect('rated', self._on_rated)
        handlers = {
            'item-play': self._on_play,
            'item-play-next': self._on_play_next,
            'item-play-later': self._on_play_later,
            'item-love': self._on_love,
            'item-unlove': self._on_unlove,
            'item-add-to-library': self._on_add_to_library,
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

    def menu_for(self, obj):
        """The menu for obj (build_menu), obj remembered for its actions; None when nothing
        applies. With the engine up, whether obj is loved is asked as the menu opens."""
        named = describe(obj)
        if named is None:
            return None
        rated = rating_target(obj)
        loved = bool(self._loved.get(rated)) if rated else False
        menu = build_menu(obj, self.playlists() if playlist_song(obj) else (), loved,
                          self._storefront())
        if menu is None:
            return None
        self._remember(named, obj)
        if rated is not None and self._engine_ready():
            self.app.spawn(self._refine(menu, named, rated, loved))
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

    async def _refine(self, menu, named, rated, shown):
        try:
            value = await self.engine.rating(*rated)
        except EngineError as error:
            log.debug('rating of %s %s: %s', *rated, error)
            return
        loved = value == 1
        if loved != shown:
            set_favourite(menu, GLib.Variant('(ss)', named), loved)

    def _on_rated(self, _engine, kind, item_id, value):
        self._loved[(kind, item_id)] = value == 1

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

    def _engine_ready(self):
        return (not self.app.demo and self.engine.state == 'up'
                and bool(self.engine.authorized))

    # -- running -----------------------------------------------------------------------------

    def _run(self, method, args, message=None):
        """await method(*args) with the engine up (started when it is down and the account
        signed in), then toast message; an EngineError is toasted instead. The task, or None
        in demo mode."""
        if self.app.demo:
            self.window.toast(_('Not available with the demo library'))
            return None

        async def run():
            try:
                await self.app.player.ensure_engine()
                await method(*args)
            except EngineError as error:
                self.app.report(error)
                return
            if message:
                self.window.toast(message)

        return self.app.spawn(run())

    def _on_play(self, _action, parameter):
        obj, kind, item_id = self._unpack(parameter)
        if isinstance(obj, Track):
            if obj.play.get('kind') and obj.play.get('id'):
                self.window.play_request(obj.play, start_with=obj.index)
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
            self.window.toast(_('This cannot be queued'))
            return None
        title = obj.title if obj is not None else ''
        if later:
            message = (_('“{title}” will play later').format(title=title) if title
                       else _('Playing later'))
            return self._run(self.app.player.play_later, target, message)
        message = (_('“{title}” will play next').format(title=title) if title
                   else _('Playing next'))
        return self._run(self.app.player.play_next, target, message)

    def _on_play_next(self, _action, parameter):
        return self._queue(parameter, later=False)

    def _on_play_later(self, _action, parameter):
        return self._queue(parameter, later=True)

    def _rate(self, parameter, love):
        obj, kind, item_id = self._unpack(parameter)
        target = rating_target(obj) if obj is not None else (kind, item_id)
        if target is None:
            self.window.toast(_('This cannot be a favourite'))
            return None
        loved, unloved = love_messages(target[0])
        if love:
            return self._run(self.engine.love, target, loved)
        return self._run(self.engine.unlove, target, unloved)

    def _on_love(self, _action, parameter):
        return self._rate(parameter, love=True)

    def _on_unlove(self, _action, parameter):
        return self._rate(parameter, love=False)

    def _on_add_to_library(self, _action, parameter):
        obj, kind, item_id = self._unpack(parameter)
        target = library_target(obj) if obj is not None else (kind, item_id)
        if target is None:
            self.window.toast(_('This is in your library already'))
            return None
        title = obj.title if obj is not None else ''
        message = (_('Added “{title}” to your library').format(title=title) if title
                   else _('Added to your library'))
        return self._run(self.engine.add_to_library, target, message)

    def _on_add_to_playlist(self, _action, parameter):
        playlist_id, kind, item_id = parameter.unpack()
        obj = self._resolve(kind, item_id)
        song_id = playlist_song(obj) if obj is not None else item_id
        return self.add_to_playlist(playlist_id, song_id, obj.title if obj is not None else '')

    def add_to_playlist(self, playlist_id, song_id, title=''):
        """Add the song to the library playlist, confirming with a toast that names both."""
        if not song_id:
            self.window.toast(_('Only songs can be added to a playlist'))
            return None
        playlist = self.library.by_id('playlist', playlist_id)
        name = playlist.title if playlist is not None else ''
        if title and name:
            message = _('Added “{title}” to “{playlist}”').format(title=title, playlist=name)
        elif name:
            message = _('Added to “{playlist}”').format(playlist=name)
        else:
            message = _('Added to the playlist')
        return self._run(self.engine.add_to_playlist, (playlist_id, song_id), message)

    def can_drop(self, playlist):
        """Whether a track may be dropped on this sidebar playlist Item (one that takes
        songs; not a folder, not a fixed item, not Favourite Songs)."""
        return isinstance(playlist, Item) and playlist.editable

    def drop(self, playlist, ref):
        """A TrackRef dropped on a sidebar playlist: added to it. False when the playlist
        takes nothing (the drop is refused) or the ref names no song."""
        if not self.can_drop(playlist) or not isinstance(ref, TrackRef) or not ref.song_id:
            return False
        self.add_to_playlist(playlist.id, ref.song_id, ref.title)
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
            if url:
                return url
        return web_url(obj, self._storefront())

    def _on_open_in_browser(self, _action, parameter):
        obj, kind, item_id = self._unpack(parameter)
        return self.app.spawn(self._open(obj, kind, item_id))

    async def _open(self, obj, kind, item_id):
        url = await self.link(obj, kind, item_id)
        if not url:
            self.window.toast(_('This has no page to open'))
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
            self.window.toast(_('Could not open the browser'))

    def _on_copy_link(self, _action, parameter):
        obj, kind, item_id = self._unpack(parameter)
        return self.app.spawn(self._copy(obj, kind, item_id))

    async def _copy(self, obj, kind, item_id):
        url = await self.link(obj, kind, item_id)
        if not url:
            self.window.toast(_('This has no link to copy'))
            return
        self.window.get_clipboard().set(url)
        self.window.toast(_('Link copied'))
