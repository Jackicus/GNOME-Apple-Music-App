# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Where Go to Album and Go to Artist go (actions.py): the album a song is on, the artist a
song, a music video or an album is by.

    has_album(obj), has_artist(obj)   whether a menu offers Go to Album, Go to Artist for obj
    library_album(library, obj)       the library's album Item a Track or song Item is on
    library_artist(library, obj)      the library's artist Item obj is by
    catalog_target(obj)               (kind, catalog id) that Engine.related() looks obj up by
    from_answer(library, answer, kind, name)   the Item to open from related()'s answer
    shows(here, obj, kind)            whether the page showing `here` is obj's album or artist
    same_page(here, item)             whether `item` is the page showing `here`

The library answers first, at once and without the engine (the demo's only answer): the album
a track's group plays (an album page's, the Songs page's), else one of the library's albums of
that title (case and accents aside, fold()) by the song's artist, or one that holds the song (a
compilation's); the artist of that name. A song's artist may credit several ("A feat. B", "A
& B", "A, B and C"): the first is the one (JOINER). The library's artists are its albums'
artists, so an album's is always there. What the library cannot place, the engine looks up in
the catalog by its catalog id, an Item of the library's preferred when it is the same album
(its catalog id) or artist (its name).

GLib, GObject and Gio only, no GTK: the unit tests run it with a stand-in library.
"""

import re

from .backend.api import is_library_id
from .library import Item, Track, fold
from .remote import remote_item

# What may come between an artist's name and another's in a song's artist: "A feat. B",
# "A & B", "A, B and C", "A x B", "A with B", "A vs. B". A word joiner after a space only:
# "Max" is not "Ma" and "x".
JOINER = re.compile(r'\s*[,&+/;]|\s+(?:feat|ft|featuring|with|and|x|vs)\b')

# The kinds an artist is offered for.
ARTIST_KINDS = ('song', 'video', 'album')


def _key(text):
    """text as names are matched: its runs of white space one space, folded."""
    return fold(' '.join(str(text or '').split()))


def _credits(name_key, artist_key):
    """Whether a song's artist (`name_key`, as _key() makes it) credits the artist
    (`artist_key`): the same name, or the artist's first before a joiner ("A feat. B")."""
    if not artist_key or not name_key.startswith(artist_key):
        return False
    return len(name_key) == len(artist_key) or bool(JOINER.match(name_key, len(artist_key)))


def album_name(obj):
    """The name of the album obj is on: a Track's `album`, a song Item's (an artist's Top
    Songs carry it), else ''."""
    if isinstance(obj, Track):
        return obj.album or ''
    if isinstance(obj, Item) and obj.kind == 'song' and isinstance(obj.raw, dict):
        return str(obj.raw.get('album') or '')
    return ''


def artist_name(obj):
    """The name of the artist obj is by: a Track's `artist`; a song's, video's or album's
    artistName (an artist page's albums are subtitled with their year), else its subtitle;
    else ''."""
    if isinstance(obj, Track):
        return obj.artist or ''
    if isinstance(obj, Item) and obj.kind in ARTIST_KINDS:
        named = obj.raw.get('artistName') if isinstance(obj.raw, dict) else None
        return str(named or obj.subtitle or '')
    return ''


def catalog_target(obj):
    """(kind, catalog id) Engine.related() looks obj up by: a song or a music video (a
    Track's by Track.kind) by its catalog id, else its own when that is a catalog one; an
    album by the same rule. None for anything else, or without a catalog id."""
    if isinstance(obj, Track):
        kind = 'video' if obj.kind == 'video' else 'song'
    elif isinstance(obj, Item) and obj.kind in ARTIST_KINDS:
        kind = obj.kind
    else:
        return None
    item_id = obj.catalog_id or obj.id
    if not item_id or is_library_id(item_id):
        return None
    return kind, str(item_id)


def has_album(obj):
    """Whether Go to Album can go somewhere for obj: a song (not a music video) with an
    album to look for in the library, or a catalog id to ask the engine by."""
    if isinstance(obj, Track):
        if obj.kind == 'video':
            return False
    elif not (isinstance(obj, Item) and obj.kind == 'song'):
        return False
    play = obj.play or {}
    return bool(album_name(obj) or play.get('kind') == 'album' or catalog_target(obj))


def has_artist(obj):
    """Whether Go to Artist can go somewhere for obj: a Track, or a song, video or album
    Item, with an artist name or a catalog id."""
    if not (isinstance(obj, Track) or (isinstance(obj, Item) and obj.kind in ARTIST_KINDS)):
        return False
    # Not folded: a row's link asks at every bind, and only emptiness matters here.
    return bool(artist_name(obj).strip()) or catalog_target(obj) is not None


def _holds(album, ids):
    """Whether one of an album's tracks has one of `ids` (its own or its catalog id)."""
    for group in album.groups:
        entries = group.entries
        for position in range(entries.get_n_items()):
            track = entries.get_item(position)
            if track.id in ids or track.catalog_id in ids:
                return True
    return False


def library_album(library, obj):
    """The library's album Item a Track or song Item is on, or None: the album a track's
    group plays, else the best of the library's of the same title: by the song's artist and
    holding the song, then holding it (a compilation's, by Various Artists), then by the
    song's artist exactly, then by the first it credits ("A feat. B": A's), the first of
    equals in the library's order."""
    if isinstance(obj, Track):
        play = obj.play or {}
        if play.get('kind') == 'album' and play.get('id'):
            album = library.by_id('album', play['id'])
            if album is not None:
                return album
    title = _key(album_name(obj))
    if not title:
        return None
    candidates = [album for album in library.albums if _key(album.title) == title]
    if not candidates:
        return None
    artist = _key(artist_name(obj))
    ids = {value for value in (obj.id, obj.catalog_id) if value}

    def rank(album):
        subtitle = _key(album.subtitle)
        holds = bool(ids) and _holds(album, ids)
        exact = bool(artist) and subtitle == artist
        return (holds and exact, holds, exact, bool(artist) and _credits(artist, subtitle))

    ranks = [rank(album) for album in candidates]
    best = max(range(len(candidates)), key=ranks.__getitem__)  # the first of equals
    return candidates[best] if any(ranks[best]) else None


def artist_named(library, name):
    """The library's artist called `name` (case and accents aside), else the one whose name
    `name` starts with before a joiner, the longest such ("A feat. B" is A's); or None."""
    key = _key(name)
    if not key:
        return None
    best, best_length = None, 0
    for artist in library.artists:
        artist_key = _key(artist.title)
        if artist_key == key:
            return artist
        if len(artist_key) > best_length and _credits(key, artist_key):
            best, best_length = artist, len(artist_key)
    return best


def library_artist(library, obj):
    """The library's artist Item obj (a Track, a song, video or album Item) is by, or None
    (artist_named)."""
    if not has_artist(obj):
        return None
    return artist_named(library, artist_name(obj))


def from_answer(library, answer, kind, name=''):
    """The Item Go to Album (`kind` 'album') or Go to Artist ('artist') opens from
    Engine.related()'s answer, or None when it names none: the library's own when it has the
    same album (by catalog id) or the artist (by name), else the answer's, as a search hit's
    Item (remote.remote_item: its artwork fetched where the app fetches it). Of several
    artists, the one called `name`, else the first."""
    if not isinstance(answer, dict):
        return None
    if kind == 'album':
        found = answer.get('album')
        if not isinstance(found, dict) or not found.get('id'):
            return None
        catalog_id = str(found['id'])
        own = next((album for album in library.albums
                    if album.catalog_id == catalog_id or album.id == catalog_id), None)
        return own if own is not None else Item(remote_item(found))
    artists = [artist for artist in answer.get('artists') or ()
               if isinstance(artist, dict) and artist.get('id')]
    if not artists:
        return None
    key = _key(name)
    found = next((artist for artist in artists if key and _key(artist.get('title')) == key),
                 artists[0])
    own = artist_named(library, found.get('title'))
    if own is not None and _key(own.title) == _key(found.get('title')):
        return own
    return Item(remote_item(found))


def shows(here, obj, kind):
    """Whether `here` (the Item of the page shown, or None) is already where Go to Album
    (`kind` 'album') or Go to Artist ('artist') would take obj: an album page's own tracks,
    an album page's songs by name too when they carry no group (the item playing's), an
    artist page's own songs and albums (by name, the first credited); the menu leaves the
    item out there."""
    if not isinstance(here, Item):
        return False
    if kind == 'album':
        if here.kind != 'album':
            return False
        play = (obj.play if isinstance(obj, (Track, Item)) else None) or {}
        if play.get('kind') == 'album':
            return play.get('id') == here.id
        # A track without its album's group (the item playing, Up Next's): by its album's
        # name and artist, or the page's album holding it.
        title = _key(album_name(obj))
        if not title or title != _key(here.title):
            return False
        ids = {value for value in (obj.id, obj.catalog_id) if value}
        return (_credits(_key(artist_name(obj)), _key(here.subtitle))
                or (bool(ids) and _holds(here, ids)))
    if here.kind != 'artist':
        return False
    return _credits(_key(artist_name(obj)), _key(here.title))


def is_artist(artist, name):
    """Whether the artist Item is the one called `name` (case and accents aside)."""
    return bool(_key(name)) and _key(artist.title) == _key(name)


def same_page(here, target):
    """Whether `target` (an Item Go to opens) is the page shown, whose Item is `here`: the
    same object, the same kind and id, or an artist of the same name."""
    if not isinstance(here, Item) or not isinstance(target, Item) or here.kind != target.kind:
        return False
    if here is target or (here.id and here.id == target.id):
        return True
    return here.kind == 'artist' and is_artist(here, target.title)
