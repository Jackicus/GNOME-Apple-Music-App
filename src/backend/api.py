# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The Apple Music API as the app knows it, apart from any connection to it: which ids are the
library's, the path that answers with one item, the resource type a kind is rated and added
under, the resources of one page of an answer, and Apple's error answers as an EngineError.
The standard library only; engine.py and actions.py use it.

    is_library_id('l.abc')              # True: under /v1/me/library, not the catalog
    item_endpoint('album', id, 'gb')    # '/v1/catalog/gb/albums/<id>?include=tracks,artists'
    resource_type('song', 'i.1')        # 'library-song' (the singular: callers add the "s")
    page_data(answer)                   # the page's resources: its `data` list's dicts
    api_error(answer, 'search')         # EngineError('api', 'search: 404 Not Found: …',
                                        # status=404) for Apple's {errors: [...]}, else None
"""

from .errors import EngineError

# Library ids ("l." albums and playlists, "p." playlists, "r." radio, "i." songs and music
# videos) live under /v1/me/library; anything else is the catalog's.
LIBRARY_PREFIXES = ('l.', 'p.', 'r.', 'i.')

# The Apple Music API's resource type for each kind an Item or a Track has (the singular: the
# bridge's rating() and addToLibrary() add the "s"), for the ratings and library writes. A
# library id takes the "library-" type. Artists have no ratings and cannot be added.
RESOURCE_TYPES = {
    'song': 'song',
    'album': 'album',
    'playlist': 'playlist',
    'station': 'station',
    'video': 'music-video',
    'musicVideo': 'music-video',
    'music-video': 'music-video',
}

# The New page: the editorial groupings behind music.apple.com's own (the request it makes,
# less its field selections and `format[resources]=map`, which flattens the answer).
BROWSE_ENDPOINT = '/v1/editorial/{storefront}/groupings'
BROWSE_PARAMS = {'name': 'music', 'platform': 'web', 'extend': 'editorialArtwork'}
# Made for You: the recommendations, of which the mixes and stations are kept.
RECOMMENDATIONS_ENDPOINT = '/v1/me/recommendations'
RECOMMENDATIONS_PARAMS = {'limit': 25}


def is_library_id(item_id):
    return str(item_id).startswith(LIBRARY_PREFIXES)


def item_endpoint(kind, item_id, storefront):
    """The API path that answers with one full item of `kind` (Engine.item()): library items
    under /v1/me/library, the rest under the catalog."""
    library = is_library_id(item_id)
    if kind == 'album':
        return (f'/v1/me/library/albums/{item_id}?include=tracks,artists' if library
                else f'/v1/catalog/{storefront}/albums/{item_id}?include=tracks,artists')
    if kind == 'playlist':
        return (f'/v1/me/library/playlists/{item_id}?include=tracks' if library
                else f'/v1/catalog/{storefront}/playlists/{item_id}?include=tracks')
    if kind == 'artist':
        return (f'/v1/me/library/artists/{item_id}?include=albums' if library
                else f'/v1/catalog/{storefront}/artists/{item_id}?include=albums')
    if kind == 'station':
        return f'/v1/catalog/{storefront}/stations/{item_id}'
    if kind == 'song':
        return (f'/v1/me/library/songs/{item_id}' if library
                else f'/v1/catalog/{storefront}/songs/{item_id}')
    if RESOURCE_TYPES.get(kind) == 'music-video':
        return (f'/v1/me/library/music-videos/{item_id}' if library
                else f'/v1/catalog/{storefront}/music-videos/{item_id}')
    return f'/v1/catalog/{storefront}/{kind}s/{item_id}'


def album_endpoint(album_id, storefront):
    """The API path of an album with its tracks: an artist's album, fetched for its page."""
    library = str(album_id).startswith(('l.', 'p.'))
    return (f'/v1/me/library/albums/{album_id}?include=tracks' if library
            else f'/v1/catalog/{storefront}/albums/{album_id}?include=tracks')


def resource_type(kind, item_id):
    """The API type (singular) the ratings of `kind` `item_id` live under: 'song' or
    'library-song', 'album' or 'library-album'… EngineError('usage') for a kind with none."""
    base = RESOURCE_TYPES.get(kind)
    if base is None or item_id in (None, ''):
        raise EngineError('usage', f'no rating for {kind or "nothing"} {item_id or ""}'.strip())
    return f'library-{base}' if is_library_id(item_id) and base != 'station' else base


def page_data(answer):
    """The resources of one page: its `data` list's dicts."""
    data = answer.get('data') if isinstance(answer, dict) else None
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def api_error(answer, what):
    """Apple's answer to a failed request, `{"errors": [...]}` (MusicKit hands it back as if it
    had worked), as EngineError('api', "<what>: <status> <title>: <detail>") with the first
    error's HTTP status as its `status` (None when it gave none); None for any other answer."""
    if not isinstance(answer, dict) or not answer.get('errors'):
        return None
    errors = answer['errors']
    first = errors[0] if isinstance(errors, list) and errors else {}
    if not isinstance(first, dict):
        first = {}
    return EngineError('api', f"{what}: {first.get('status', '?')} "
                              f"{first.get('title', 'error')}: "
                              f"{first.get('detail', '')}".strip(),
                       status=http_status(first.get('status')))


def http_status(value):
    """An HTTP status as Apple writes it in an error ('404', or 404) as an int; None for
    anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 100 <= value < 600 else None
    if isinstance(value, str) and value.strip().isdigit():
        return http_status(int(value.strip()))
    return None


def is_final(error):
    """Whether a failed API read would fail the same way again: Apple's 4xx, bar 429 (too many
    requests), and a timeout (MusicKit retries the network itself). A 5xx, a 429, an error
    without a status, and a request the page itself threw on are worth another try."""
    if error.code == 'timeout':
        return True
    return error.status is not None and 400 <= error.status < 500 and error.status != 429
