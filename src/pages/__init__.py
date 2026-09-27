"""The content pane's root pages, one per sidebar destination that has one.

create(destination, library) builds a destination's page, or returns None for a destination that
has none yet (the window shows a placeholder for it). The window builds each page on its first
visit and keeps it. The sidebar's playlists and folders get theirs from playlist() and folder(),
which a folder's tiles use too.
"""

from gettext import gettext as _

from ..library import ROOT_FOLDER
from ..sidebar import FOLDER_ICON, PLAYLIST_ICON

from .detail import DetailPage
from .grid import GridPage
from .home import HomePage
from .radio import RadioPage
from .songs import SongsPage


def _home(destination, library):
    return HomePage(library, destination.title, icon_name=destination.icon_name)


def _radio(destination, library):
    return RadioPage(library, destination.title, icon_name=destination.icon_name)


def _albums(destination, library):
    return GridPage(library, destination.title, lambda: library.albums,
                    sorts=('title', 'artist', 'year'), icon_name=destination.icon_name,
                    empty_title=_('No Albums'),
                    empty_description=_('Albums in your library appear here'))


def _artists(destination, library):
    return GridPage(library, destination.title, lambda: library.artists, sorts=('title',),
                    artist=True, icon_name=destination.icon_name,
                    empty_title=_('No Artists'),
                    empty_description=_('Artists in your library appear here'))


def _recently_added(destination, library):
    # Apple's order, newest first: not sorted here.
    def model():
        shelf = library.shelf('recently-added')
        return shelf.items if shelf else None

    return GridPage(library, destination.title, model, icon_name=destination.icon_name,
                    empty_title=_('Nothing Recently Added'),
                    empty_description=_('Music you add to your library appears here'))


def _songs(destination, library):
    return SongsPage(library, destination.title, icon_name=destination.icon_name)


def _all_playlists(destination, library):
    # The top level of the playlist folders: folders and the playlists in none, in Apple's order.
    return GridPage(library, destination.title, lambda: library.folder_items(ROOT_FOLDER),
                    icon_name=destination.icon_name,
                    empty_title=_('No Playlists'),
                    empty_description=_('Playlists in your library appear here'))


def _favourite_songs(destination, library):
    # The flagged playlist's page, as a root page that follows the library.
    return DetailPage(library, find=library.favourite_songs, root=True, title=destination.title,
                      icon_name=destination.icon_name,
                      empty_title=_('No Favourite Songs'),
                      empty_description=_('Songs you mark as favourites appear here'))


def _music_videos(destination, library):
    return GridPage(library, destination.title, lambda: library.videos,
                    sorts=('title', 'artist', 'year'), icon_name=destination.icon_name,
                    empty_title=_('No Music Videos'),
                    empty_description=_('Music videos in your library appear here'))


def playlist(library, playlist_id, title):
    """A sidebar playlist's root page: its detail page, following the library by the id (each
    load makes new Items), with an empty state once the playlist is gone."""
    return DetailPage(library, find=lambda: library.by_id('playlist', playlist_id), root=True,
                      title=title, icon_name=PLAYLIST_ICON,
                      empty_title=_('Playlist Not Found'),
                      empty_description=_('This playlist is no longer in your library'))


def folder(library, folder_id, title, root=True):
    """A folder's page: its folders and playlists as tiles, in Apple's order, following the
    library by the folder's id. A sidebar folder's is a root page; a folder opened from a tile
    is pushed (root=False) over the page it was in."""
    return GridPage(library, title, lambda: library.folder_items(folder_id), root=root,
                    icon_name=FOLDER_ICON, empty_title=_('Empty Folder'),
                    empty_description=_('Playlists you put in this folder appear here'))


PAGES = {
    'home': _home,
    'radio': _radio,
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
