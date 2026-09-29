"""The sidebar's destinations, in the order and grouping of music.apple.com.

Each section has a stable id (the window finds the Playlists section by PLAYLISTS_SECTION,
whatever its title or order), and the keys the window handles by name are constants here.
"""

from dataclasses import dataclass
from gettext import gettext as _

# The section whose items the library's folders and playlists follow, and its two fixed
# destinations: the first shows every playlist, the second the Favourite Songs playlist.
PLAYLISTS_SECTION = 'playlists'
ALL_PLAYLISTS = 'all-playlists'
FAVOURITE_SONGS = 'favourite-songs'
# The destination shown when the one asked for is gone.
HOME = 'home'


@dataclass(frozen=True)
class Destination:
    key: str
    title: str
    icon_name: str


@dataclass(frozen=True)
class Section:
    """One sidebar section: its id, its title (None for the first, untitled one) and its
    destinations in order."""

    id: str
    title: str
    destinations: tuple


def sidebar_sections():
    """The sections, as [Section, …].

    Built on call rather than at import so the titles are translated after
    the launcher has set up gettext.
    """
    return [
        Section('browse', None, (
            Destination('search', _('Search'), 'system-search-symbolic'),
            Destination(HOME, _('Home'), 'go-home-symbolic'),
            Destination('new', _('New'), 'view-grid-symbolic'),
            Destination('radio', _('Radio'), 'broadcast-symbolic'),
        )),
        Section('library', _('Library'), (
            Destination('recently-added', _('Recently Added'), 'document-open-recent-symbolic'),
            Destination('artists', _('Artists'), 'audio-input-microphone-symbolic'),
            Destination('albums', _('Albums'), 'media-optical-cd-audio-symbolic'),
            Destination('songs', _('Songs'), 'music-note-symbolic'),
            Destination('music-videos', _('Music Videos'), 'video-display-symbolic'),
            Destination('made-for-you', _('Made for You'), 'avatar-default-symbolic'),
        )),
        Section(PLAYLISTS_SECTION, _('Playlists'), (
            Destination(ALL_PLAYLISTS, _('All Playlists'), 'view-app-grid-symbolic'),
            Destination(FAVOURITE_SONGS, _('Favourite Songs'), 'starred-symbolic'),
        )),
    ]
