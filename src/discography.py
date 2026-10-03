# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""What the library holds of an artist, for the library's own artist view
(pages/library_artist.py): the albums, and what Play and Shuffle play.

    artist = library_artist(library, item)   the library's artist an artist Item stands for
    albums(library, artist, made)            the library's albums of theirs, in the artist's
                                             order (the page sorts them)
    play_target(albums)                      {'kind': 'songs', 'id': 'i.…,i.…'}: their songs

An artist of the library is the sync's: one per album artist, each of their albums a group
({name, play, entries}: backend/README.md). A song added without its album sits under an
album all the same, a stand-in the sync makes up for it (`l.alb_…`, playing its songs by id:
normalize.stand_in_album_id), so a single song shows as its album's tile, as Apple Music shows
it, and nothing the library holds of the artist's own is left out. A group whose album the
library does not have (a library.json from before the stand-ins, a test's) becomes an album of
its own tracks, made once (`made`, kept by the caller so a tile keeps its Item).

GLib, GObject and Gio only, no GTK: the unit tests run it with a stand-in library.
"""

from .backend.normalize import stand_in_album_id
from .library import Item, fold

# The songs Play and Shuffle queue at most: MusicKit looks a queue of songs up by id, and
# this many already play for a day.
PLAY_LIMIT = 300


def _dicts(value):
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def library_artist(library, item):
    """The library's artist that an artist Item is or stands for: itself (by id), else the
    library's of the same name, case and accents aside (a catalog artist's page); or None."""
    if item is None or item.kind != 'artist':
        return None
    own = library.by_id('artist', item.id) if item.id else None
    if own is not None:
        return own
    name = fold(item.title)
    return next((artist for artist in library.artists if fold(artist.title) == name), None)


def _album_of(library, artist, group):
    """The library's album Item an artist's group (a raw dict) is, or None."""
    play = group.get('play')
    if not isinstance(play, dict):
        return None
    if play.get('kind') == 'album':
        return library.by_id('album', play.get('id'))
    if play.get('kind') != 'songs':
        return None
    # Loose songs: the stand-in album made up for them, by its album's and artist's names
    # (the group's name and the artist's, the album's subtitle), as the sync made its id.
    name = group.get('name') if isinstance(group.get('name'), str) else ''
    found = library.by_id('album', stand_in_album_id(name, artist.raw.get('title') or ''))
    if found is not None:
        return found
    return next((album for album in library.albums if album.play == play), None)


def _made_up(artist, group):
    """An album Item of a group's own tracks, for a group whose album the library lacks."""
    play = dict(group.get('play') or {})
    entries = group.get('entries') if isinstance(group.get('entries'), list) else []
    first = entries[0] if entries else None
    album_id = play.get('id') if play.get('kind') == 'album' else None
    return Item({'id': album_id or stand_in_album_id(group.get('name') or '',
                                                     artist.raw.get('title') or ''),
                 'kind': 'album', 'title': group.get('name') or '',
                 'subtitle': artist.raw.get('title') or '', 'year': 0,
                 'thumb': first.get('thumb') if first is not None else None,
                 'play': play, 'groups': [{'name': '', 'play': play, 'entries': entries}]})


def albums(library, artist, made=None):
    """The library's albums of the artist's (an artist Item of the library's), each once, in
    the artist's order: the library's own Items, a stand-in album for loose songs among
    them; a group the library has no album for, an album of its own tracks (kept in `made`,
    a dict by play id, when given, so the same group gives the same Item); a group with
    neither is left out."""
    if artist is None:
        return []
    found, seen = [], set()
    for group in _dicts(artist.raw.get('groups')):
        album = _album_of(library, artist, group)
        if album is None:
            entries = group.get('entries')
            if not isinstance(entries, list) or not entries:
                continue  # nothing to show
            key = str((group.get('play') or {}).get('id') or group.get('name') or '')
            album = made.get(key) if made is not None else None
            if album is None:
                album = _made_up(artist, group)
                if made is not None:
                    made[key] = album
        if album.id in seen:
            continue
        seen.add(album.id)
        found.append(album)
    return found


def song_ids(albums, limit=PLAY_LIMIT):
    """The ids of the albums' songs, album by album in the order given, disc by disc, at
    most `limit`: what Play queues."""
    ids = []
    for album in albums:
        for group in album.groups:
            entries = group.entries
            for position in range(entries.get_n_items()):
                track_id = entries.get_item(position).id
                if track_id:
                    ids.append(track_id)
                    if len(ids) >= limit:
                        return ids
    return ids


def play_target(albums, limit=PLAY_LIMIT):
    """What Play and Shuffle play for the albums (in the order shown): their songs by id,
    {'kind': 'songs', 'id': 'i.…,i.…'} as the bridge takes them, or None without a song."""
    ids = song_ids(albums, limit)
    return {'kind': 'songs', 'id': ','.join(ids)} if ids else None
