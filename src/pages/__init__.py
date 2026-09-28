"""The content pane's root pages, one per sidebar destination that has one.

create(destination, library) builds a destination's page, or returns None for a destination that
has none yet (the window shows a placeholder for it). The window builds each page on its first
visit and keeps it. The sidebar's playlists and folders get theirs from playlist() and folder(),
which a folder's tiles use too.

Each page's module is imported when its first page is built, not here: the app starts with one
page, and importing every page module (their templates and widgets) cost about 20 ms of startup.
"""

from gettext import gettext as _

from gi.repository import Gio

from ..library import ROOT_FOLDER
from ..sidebar import FOLDER_ICON, PLAYLIST_ICON


def mark_bound(page):
    """Note a page's first tile or row being bound, for the startup timing
    (Application.mark: '<tag>-bound', and '<tag>-painted' at the end of that frame), when
    the page is a root page (the window tags those with their key)."""
    tag = page.get_tag()
    mark = getattr(Gio.Application.get_default(), 'mark', None)
    if tag and mark is not None:
        mark(f'{tag}-bound', painted=f'{tag}-painted')


def _search(destination, library):
    from .search import SearchPage

    return SearchPage(library, destination.title, icon_name=destination.icon_name)


def _home(destination, library):
    from .home import HomePage

    return HomePage(library, destination.title, icon_name=destination.icon_name)


def _new(destination, library):
    from . import new

    return new.create(destination)


def _made_for_you(destination, library):
    from . import made_for_you

    return made_for_you.create(destination)


def _radio(destination, library):
    from .radio import RadioPage

    return RadioPage(library, destination.title, icon_name=destination.icon_name)


def _albums(destination, library):
    from .grid import GridPage

    return GridPage(library, destination.title, lambda: library.albums,
                    sorts=('title', 'artist', 'year'), icon_name=destination.icon_name,
                    empty_title=_('No Albums'),
                    empty_description=_('Albums in your library appear here'))


def _artists(destination, library):
    from .grid import GridPage

    return GridPage(library, destination.title, lambda: library.artists, sorts=('title',),
                    artist=True, icon_name=destination.icon_name,
                    empty_title=_('No Artists'),
                    empty_description=_('Artists in your library appear here'))


def _recently_added(destination, library):
    # Apple's order, newest first: not sorted here.
    from .grid import GridPage

    def model():
        shelf = library.shelf('recently-added')
        return shelf.items if shelf else None

    return GridPage(library, destination.title, model, icon_name=destination.icon_name,
                    empty_title=_('Nothing Recently Added'),
                    empty_description=_('Music you add to your library appears here'))


def _songs(destination, library):
    from .songs import SongsPage

    return SongsPage(library, destination.title, icon_name=destination.icon_name)


def _all_playlists(destination, library):
    # The top level of the playlist folders: folders and the playlists in none, in Apple's order.
    from .grid import GridPage

    return GridPage(library, destination.title, lambda: library.folder_items(ROOT_FOLDER),
                    icon_name=destination.icon_name,
                    empty_title=_('No Playlists'),
                    empty_description=_('Playlists in your library appear here'))


def _favourite_songs(destination, library):
    # The flagged playlist's page, as a root page that follows the library.
    from .detail import DetailPage

    return DetailPage(library, find=library.favourite_songs, root=True, title=destination.title,
                      icon_name=destination.icon_name,
                      empty_title=_('No Favourite Songs'),
                      empty_description=_('Songs you mark as favourites appear here'))


def _music_videos(destination, library):
    from .grid import GridPage

    return GridPage(library, destination.title, lambda: library.videos,
                    sorts=('title', 'artist', 'year'), icon_name=destination.icon_name,
                    empty_title=_('No Music Videos'),
                    empty_description=_('Music videos in your library appear here'))


def playlist(library, playlist_id, title):
    """A sidebar playlist's root page: its detail page, following the library by the id (each
    load makes new Items), with an empty state once the playlist is gone."""
    from .detail import DetailPage

    return DetailPage(library, find=lambda: library.by_id('playlist', playlist_id), root=True,
                      title=title, icon_name=PLAYLIST_ICON,
                      empty_title=_('Playlist Not Found'),
                      empty_description=_('This playlist is no longer in your library'))


def folder(library, folder_id, title, root=True):
    """A folder's page: its folders and playlists as tiles, in Apple's order, following the
    library by the folder's id. A sidebar folder's is a root page; a folder opened from a tile
    is pushed (root=False) over the page it was in."""
    from .grid import GridPage

    return GridPage(library, title, lambda: library.folder_items(folder_id), root=root,
                    icon_name=FOLDER_ICON, empty_title=_('Empty Folder'),
                    empty_description=_('Playlists you put in this folder appear here'))


PAGES = {
    'search': _search,
    'home': _home,
    'new': _new,
    'radio': _radio,
    'made-for-you': _made_for_you,
    'albums': _albums,
    'artists': _artists,
    'recently-added': _recently_added,
    'songs': _songs,
    'all-playlists': _all_playlists,
    'favourite-songs': _favourite_songs,
    'music-videos': _music_videos,
}


def create(destination, library):
    """A new root page for the destination, or None when it has no page yet."""
    factory = PAGES.get(destination.key)
    return factory(destination, library) if factory else None
