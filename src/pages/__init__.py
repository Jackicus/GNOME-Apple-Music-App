"""The content pane's root pages, one per sidebar destination that has one.

create(destination, library) builds a destination's page, or returns None for a destination that
has none yet (the window shows a placeholder for it). The window builds each page on its first
visit and keeps it.
"""

from gettext import gettext as _

from .grid import GridPage


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


def _all_playlists(destination, library):
    return GridPage(library, destination.title, lambda: library.playlists, sorts=('title',),
                    icon_name=destination.icon_name,
                    empty_title=_('No Playlists'),
                    empty_description=_('Playlists in your library appear here'))


def _music_videos(destination, library):
    return GridPage(library, destination.title, lambda: library.videos,
                    sorts=('title', 'artist', 'year'), icon_name=destination.icon_name,
                    empty_title=_('No Music Videos'),
                    empty_description=_('Music videos in your library appear here'))


PAGES = {
    'albums': _albums,
    'artists': _artists,
    'recently-added': _recently_added,
    'all-playlists': _all_playlists,
    'music-videos': _music_videos,
}


def create(destination, library):
    """A new root page for the destination, or None when it has no page yet."""
    factory = PAGES.get(destination.key)
    return factory(destination, library) if factory else None
