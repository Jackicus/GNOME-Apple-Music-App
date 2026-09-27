"""The sidebar's destinations, in the order and grouping of music.apple.com."""

from dataclasses import dataclass
from gettext import gettext as _


@dataclass(frozen=True)
class Destination:
    key: str
    title: str
    icon_name: str


def sidebar_sections():
    """Return ``[(section_title_or_None, [Destination, …]), …]``.

    Built on call rather than at import so the titles are translated after
    the launcher has set up gettext.
    """
    return [
        (None, [
            Destination('search', _('Search'), 'system-search-symbolic'),
            Destination('home', _('Home'), 'go-home-symbolic'),
            Destination('new', _('New'), 'view-grid-symbolic'),
            Destination('radio', _('Radio'), 'broadcast-symbolic'),
        ]),
        (_('Library'), [
            Destination('recently-added', _('Recently Added'), 'document-open-recent-symbolic'),
            Destination('artists', _('Artists'), 'audio-input-microphone-symbolic'),
            Destination('albums', _('Albums'), 'media-optical-cd-audio-symbolic'),
            Destination('songs', _('Songs'), 'music-note-symbolic'),
            Destination('music-videos', _('Music Videos'), 'video-display-symbolic'),
            Destination('made-for-you', _('Made for You'), 'avatar-default-symbolic'),
        ]),
        (_('Playlists'), [
            Destination('all-playlists', _('All Playlists'), 'view-app-grid-symbolic'),
            Destination('favourite-songs', _('Favourite Songs'), 'starred-symbolic'),
        ]),
    ]
