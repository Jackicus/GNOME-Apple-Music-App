"""Syncing the library: Apple's answers, through the engine, into library.json and the models.

    counts = await sync_library(app.engine, app.library, progress)

fetches, in this order, the library's songs (with their albums, which is what the Albums and
Artists sections are built from), the playlists and each one's tracks, the playlist folders,
the music videos, the recently played radio stations and the Home shelves (Apple's
recommendations, Heavy Rotation, Recently Added); normalises them with the backend's pure
functions in a thread; fetches the thumbnails that are missing (covers are fetched on demand
by the pages that show them: widgets.artwork.Artwork.fetch_cover); writes library.json
atomically; prunes the artwork nothing names; and finally has the
Library reload() itself in place. `progress(section, done, total)` is called as it goes
(section one of PROGRESS_SECTIONS; total None until known). Every failure is an EngineError.

What the web player asks for, found by watching its requests (2026-09-28):

- the sidebar's playlists and folders: `/v1/me/library/playlist-folders/p.playlistsroot/
  children` (with `extend[library-playlists]=tags`, `include=catalog`, `omit[resource]=autos`,
  `platform=web`), then each folder's `/children`. The children come in Apple's sidebar
  order, playlists and folders mixed, `type` `library-playlists` or
  `library-playlist-folders`. The root folder is `p.playlistsroot`, which library.json calls
  `root` (library.ROOT_FOLDER).
- the favourites playlist: with `extend=tags` a library playlist's `attributes.tags` holds
  `"favorited"` for Favourite Songs (the one the user cannot edit or delete). It becomes
  `attributes: {isFavourites: true}` in the Item (library.FAVOURITES), as the demo has it.
  A playlist whose `attributes.canEdit` is false (Favourite Songs, one of Apple's added to the
  library) gets `canEdit: false` there too (library.EDITABLE).
- Songs: `/v1/me/library/songs?limit=100&offset=N` (the answer carries `meta.total` and
  `next`); Music Videos: `/v1/me/library/music-videos?limit=100&offset=N`; Recently Added:
  `/v1/me/library/recently-added?limit=25`, paged by `next` (no total).

The Item dicts written here carry one key beyond the README's shape: `artUrl`, the cover's
URL at config.COVER_SIZE, which fetch_cover() downloads to the item's `art` path.

When to sync: sync_due(last_sync, hours) (the last-sync and sync-interval settings), and the
choices Preferences offers for the interval (INTERVALS; interval_index()), with
last_sync_text() saying how long ago the last one was.
"""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from gettext import gettext as _
from gettext import ngettext

import gi

gi.require_version('GdkPixbuf', '2.0')

from gi.repository import GdkPixbuf  # noqa: E402

from .backend import config  # noqa: E402
from .backend import normalize, store  # noqa: E402
from .backend.errors import EngineError  # noqa: E402
from .library import EDITABLE, FAVOURITES, ROOT_FOLDER  # noqa: E402

log = logging.getLogger(__name__)

# The progress sections, in the order they are reported.
PROGRESS_SECTIONS = ('songs', 'playlists', 'folders', 'videos', 'radio', 'shelves', 'artwork')

SONGS_ENDPOINT = '/v1/me/library/songs'
PLAYLISTS_ENDPOINT = '/v1/me/library/playlists'
FOLDERS_ENDPOINT = '/v1/me/library/playlist-folders'
VIDEOS_ENDPOINT = '/v1/me/library/music-videos'
RADIO_ENDPOINT = '/v1/me/recent/radio-stations'
RECOMMENDATIONS_ENDPOINT = '/v1/me/recommendations'
APPLE_ROOT_FOLDER = 'p.playlistsroot'
FAVOURITE_TAG = 'favorited'
PLAYLIST_PARAMS = {'extend': 'tags,hasCollaboration'}
FOLDER_PARAMS = {'omit[resource]': 'autos', 'platform': 'web'}
PAGE = 100
PLAYLIST_CONCURRENCY = 4   # playlists whose tracks are fetched at once
FOLDER_TYPE = 'library-playlist-folders'
PLAYLIST_TYPE = 'library-playlists'
# The fixed shelves after Apple's recommendations: (key, title, endpoint, page size, at most).
# Each endpoint has a page cap of its own (a bigger `limit` is a 400, not a clamp).
SHELF_DEFS = (
    ('heavy-rotation', 'Heavy Rotation', '/v1/me/history/heavy-rotation', 10, 10),
    ('recently-added', 'Recently Added', '/v1/me/library/recently-added', 25, 100),
)


# The sync-interval choices Preferences offers, in hours, in its order: every hour, every
# 6 hours, every day, manually (0: only when asked).
INTERVALS = (1, 6, 24, 0)
DEFAULT_INTERVAL = 6


def interval_index(hours):
    """The INTERVALS position for a sync-interval value; one set outside the choices (the
    key takes 0 to 168) shows as the nearest automatic one, 0 as manual."""
    if hours in INTERVALS:
        return INTERVALS.index(hours)
    if hours <= 0:
        return INTERVALS.index(0)
    automatic = [value for value in INTERVALS if value > 0]
    return INTERVALS.index(min(automatic, key=lambda value: (abs(value - hours), value)))


def parse_stamp(stamp):
    """A last-sync value (ISO 8601) as an aware datetime, None when empty or not one; a
    stamp without a zone is UTC."""
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def sync_due(stamp, hours, now=None):
    """Whether the library should be synced now: never yet (no stamp, or not one), or the
    last sync (`stamp`) is older than `hours`; 0 hours means only when asked."""
    if hours <= 0:
        return False
    last = parse_stamp(stamp)
    if last is None:
        return True
    now = now or datetime.now(UTC)
    return (now - last).total_seconds() > hours * 3600


def last_sync_text(stamp, now=None):
    """How long ago the last sync was, as Preferences words it."""
    last = parse_stamp(stamp)
    if last is None:
        return _('Not refreshed yet')
    now = now or datetime.now(UTC)
    minutes = int((now - last).total_seconds() // 60)
    if minutes < 1:
        return _('Last refreshed just now')
    if minutes < 60:
        return ngettext('Last refreshed {count} minute ago', 'Last refreshed {count} minutes ago',
                        minutes).format(count=minutes)
    hours = minutes // 60
    if hours < 24:
        return ngettext('Last refreshed {count} hour ago', 'Last refreshed {count} hours ago',
                        hours).format(count=hours)
    days = hours // 24
    return ngettext('Last refreshed {count} day ago', 'Last refreshed {count} days ago',
                    days).format(count=days)


def scale_image(src_path, dest_path, size):
    """A JPEG at most size x size from the image at src_path: the backend's thumbnail
    scaler, so a thumbnail whose cover is on disk is scaled rather than fetched."""
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(src_path, size, size, True)
    pixbuf.savev(dest_path, 'jpeg', ['quality'], ['85'])


def install_scaler():
    """Hand the backend the scaler, and the artwork sizes the cache was built at (once, at
    startup: a tiny file read)."""
    normalize.scale_image = scale_image
    normalize.load_art_sizes(str(config.cache_dir()))


async def sync_library(engine, library, progress=None):
    """The whole sync (see the module). Returns the counts: {albums, artists, playlists,
    songs, videos, radio, folders, shelves, art: {wanted, fetched, failed}}."""
    report = progress or (lambda section, done, total: None)
    generation = store.cache_generation()  # a wipe from here on leaves this sync's files out
    status = await engine.status()
    if not status.get('authorized'):
        raise EngineError('not-signed-in', 'sign in to Apple Music to sync your library')
    storefront = str(status.get('storefront') or 'us')
    cache_dir = str(config.cache_dir())
    # The sizes this sync builds at, before anything names a file (a changed thumbnail size
    # wipes the old thumbnails), and what library.json holds now: a fetch that fails keeps
    # its old entry rather than emptying its section.
    await asyncio.to_thread(normalize.apply_art_sizes, cache_dir, config.COVER_SIZE,
                            config.THUMB_SIZE, generation)
    previous = await asyncio.to_thread(_previous_library, cache_dir)
    old_sections = previous.get('sections') if isinstance(previous.get('sections'), dict) else {}
    old_shelves = [shelf for shelf in previous.get('shelves') or [] if isinstance(shelf, dict)]

    # 1. The songs, each with its album: the Albums and Artists sections come from them.
    report('songs', 0, None)
    raw_songs = await engine.api_pages(
        SONGS_ENDPOINT, {'include': 'albums'}, page=PAGE,
        progress=lambda done, total: report('songs', done, total))

    # 2. The playlists, then each one's tracks (a few at a time).
    report('playlists', 0, None)
    raw_playlists = await engine.api_pages(PLAYLISTS_ENDPOINT, PLAYLIST_PARAMS, page=PAGE)
    playlist_tracks = await _fetch_playlist_tracks(engine, raw_playlists, report)

    # 3. The playlist folders; 4. the music videos; 5. the stations; 6. the shelves. None of
    # these stops the sync: what fails keeps last time's entry.
    try:
        folders = await _fetch_folders(engine, report)
    except EngineError as error:
        _keep_going(error)
        folders = [entry for entry in previous.get('folders') or [] if isinstance(entry, dict)]
    report('videos', 0, None)
    try:
        raw_videos = await engine.api_pages(
            VIDEOS_ENDPOINT, page=PAGE,
            progress=lambda done, total: report('videos', done, total))
    except EngineError as error:
        _keep_going(error)
        raw_videos = None
    report('radio', 0, 1)
    try:
        raw_stations = (await engine.api(RADIO_ENDPOINT)).get('data') or []
    except EngineError as error:
        _keep_going(error)
        raw_stations = None
    report('radio', 1, 1)
    shelves_raw = await _fetch_shelves(engine, report)

    # 7. Everything into the Item shapes, the missing thumbnails fetched, the file written and
    # the artwork pruned: all in a thread, the artwork's progress relayed to this loop.
    loop = asyncio.get_running_loop()
    stop = {'cancelled': False}

    def art_progress(done, total):
        loop.call_soon_threadsafe(report, 'artwork', done, total)

    build = asyncio.to_thread(
        _build_and_write, cache_dir, storefront, raw_songs, raw_playlists, playlist_tracks,
        folders, raw_videos, raw_stations, shelves_raw, old_sections, old_shelves,
        art_progress, lambda: stop['cancelled'], generation)
    try:
        counts = await build
    except asyncio.CancelledError:
        stop['cancelled'] = True  # the thread gives up its fetches at the next one
        raise
    # 8. The models follow, in place.
    await library.reload()
    return counts


def _keep_going(error):
    if error.code == 'engine-down':
        raise error
    log.warning('sync: %s', error)


async def _fetch_playlist_tracks(engine, raw_playlists, report):
    """{playlist id: its raw tracks, or None when they could not be fetched}, a few playlists
    at a time; progress per playlist done."""
    tracks = {}
    total = len(raw_playlists)
    done = 0
    semaphore = asyncio.Semaphore(PLAYLIST_CONCURRENCY)

    async def fetch(raw):
        nonlocal done
        playlist_id = str(raw.get('id') or '')
        async with semaphore:
            try:
                answer = await engine.api_pages(f'{PLAYLISTS_ENDPOINT}/{playlist_id}/tracks',
                                                page=PAGE)
            except EngineError as error:
                _keep_going(error)
                answer = None
        tracks[playlist_id] = answer
        done += 1
        report('playlists', done, total)

    report('playlists', 0, total)
    await asyncio.gather(*(fetch(raw) for raw in raw_playlists if raw.get('id')))
    return tracks


async def _fetch_folders(engine, report):
    """library.json's `folders` from Apple's playlist folders: the root's children, then each
    folder's, breadth first, in Apple's order. Apple's root is called ROOT_FOLDER here."""
    folders = []
    queue = [(APPLE_ROOT_FOLDER, None, '')]
    report('folders', 0, None)
    while queue:
        apple_id, parent, title = queue.pop(0)
        children = await engine.api_pages(f'{FOLDERS_ENDPOINT}/{apple_id}/children',
                                          FOLDER_PARAMS, page=PAGE)
        entry = {'id': _folder_id(apple_id), 'title': title, 'parent': parent, 'children': []}
        for child in children:
            child_id = str(child.get('id') or '')
            if not child_id:
                continue
            if child.get('type') == FOLDER_TYPE:
                entry['children'].append({'kind': 'folder', 'id': _folder_id(child_id)})
                name = (child.get('attributes') or {}).get('name') or ''
                queue.append((child_id, entry['id'], str(name)))
            elif child.get('type') == PLAYLIST_TYPE:
                entry['children'].append({'kind': 'playlist', 'id': child_id})
        folders.append(entry)
        report('folders', len(folders), len(folders) + len(queue))
    return folders


def _folder_id(apple_id):
    return ROOT_FOLDER if apple_id == APPLE_ROOT_FOLDER else apple_id


async def _fetch_shelves(engine, report):
    """The raw makings of the shelves: Apple's recommendations (or None when they failed) and
    each fixed shelf's items (or None)."""
    total = 1 + len(SHELF_DEFS)
    report('shelves', 0, total)
    try:
        recommendations = (await engine.api(RECOMMENDATIONS_ENDPOINT, {'limit': 25})).get('data')
        recommendations = recommendations if isinstance(recommendations, list) else []
    except EngineError as error:
        _keep_going(error)
        recommendations = None
    report('shelves', 1, total)
    fixed = {}
    for number, (key, _title, endpoint, page, limit) in enumerate(SHELF_DEFS, 2):
        try:
            fixed[key] = await engine.api_pages(endpoint, page=page, limit=limit)
        except EngineError as error:
            _keep_going(error)
            fixed[key] = None
        report('shelves', number, total)
    return {'recommendations': recommendations, 'fixed': fixed}


def _build_and_write(cache_dir, storefront, raw_songs, raw_playlists, playlist_tracks, folders,
                     raw_videos, raw_stations, shelves_raw, old_sections, old_shelves,
                     art_progress, cancelled, generation=None):
    """In a thread: normalise, fetch the missing thumbnails, write library.json, prune."""
    counts = {}
    albums, artists = normalize.group_songs_into_albums_and_artists(raw_songs, cache_dir)
    sections = {'albums': albums, 'artists': artists}
    sections['songs'] = loose_songs(raw_songs, cache_dir)

    old_playlists = {entry.get('id'): entry for entry in old_sections.get('playlists') or []
                     if isinstance(entry, dict)}
    playlists = []
    for raw in raw_playlists:
        playlist_id = str(raw.get('id') or '')
        tracks = playlist_tracks.get(playlist_id)
        if tracks is None:
            old = old_playlists.get(playlist_id)
            if old and old.get('groups'):
                playlists.append(old)  # its tracks from last time, not an empty playlist
                continue
            tracks = []
        item = normalize.normalize_playlist(raw, cache_dir, tracks=tracks)
        flag_favourites(item, raw)
        playlists.append(item)
    sections['playlists'] = playlists

    if raw_videos is None:
        sections['videos'] = old_sections.get('videos') or []
    else:
        sections['videos'] = [normalize.normalize_item(raw, cache_dir, include_groups=False)
                              for raw in raw_videos]
    if raw_stations is None:
        sections['radio'] = old_sections.get('radio') or []
    else:
        sections['radio'] = [normalize.normalize_station(raw, cache_dir) for raw in raw_stations]

    shelves = []
    if shelves_raw['recommendations'] is None:
        shelves.extend(shelf for shelf in old_shelves
                       if str(shelf.get('key', '')).startswith('rec-'))
    else:
        shelves.extend(normalize.recommendation_shelves(shelves_raw['recommendations'], cache_dir))
    for key, title, _endpoint, _page, _limit in SHELF_DEFS:
        raw_items = shelves_raw['fixed'].get(key)
        if raw_items is None:
            items = next((shelf.get('items') or [] for shelf in old_shelves
                          if shelf.get('key') == key), [])
        else:
            items = [normalize.normalize_item(raw, cache_dir, include_groups=False)
                     for raw in raw_items]
        shelves.append({'key': key, 'title': title, 'items': items})

    library_data = {
        'version': 1,
        'generated': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'storefront': storefront,
        'sections': sections,
        'shelves': shelves,
        'folders': folders,
    }
    add_art_urls(library_data)
    for name in ('albums', 'artists', 'playlists', 'songs', 'videos', 'radio'):
        counts[name] = len(sections[name])
    counts['folders'] = max(0, len(folders) - 1)  # the root is not a folder of the user's
    counts['shelves'] = sum(len(shelf['items']) for shelf in shelves)

    # The thumbnails first, and the listing only once they are on disk, so tiles never show
    # placeholders for artwork that is about to arrive. A failure is logged, nothing more.
    try:
        counts['art'] = normalize.download_art(thumb_urls(library_data, cache_dir), cache_dir,
                                               progress=art_progress, cancelled=cancelled,
                                               generation=generation)
    except store.CacheGone:
        raise  # the cache was cleared: nothing more is written
    except Exception as error:
        log.warning('sync: artwork: %s', error)
        counts['art'] = {'wanted': 0, 'fetched': 0, 'failed': 0}
    normalize.save_library(library_data, cache_dir, indent=None,  # compact: a third the size
                           generation=generation)
    normalize.prune_art(library_data, cache_dir)
    normalize.prune_remote_art(cache_dir)
    log.info('library synced: %s', ', '.join(
        f'{counts[name]} {name}' for name in ('albums', 'artists', 'playlists', 'songs',
                                               'videos', 'radio', 'folders')))
    return counts


def loose_songs(raw_songs, cache_dir):
    """The Track dicts of the library's loose songs (sections.songs): the songs that belong
    to no library album (their `albums` relationship is empty; the Albums section holds them
    under a stand-in album), in the library's order. The Songs store merges them by id after
    the albums' tracks, and plays each as a song."""
    loose = []
    for raw in raw_songs:
        if not str(raw.get('id') or ''):
            continue
        albums = ((raw.get('relationships') or {}).get('albums') or {}).get('data')
        if not albums:
            loose.append(normalize.normalize_track(raw, index=0, cache_dir=cache_dir))
    return loose


def flag_favourites(item, raw):
    """Mark the playlist Item as Favourite Songs when Apple's tags say so, and as one that
    cannot be added to when Apple's canEdit is false (Favourite Songs, a playlist of Apple's
    or someone else's added to the library): the Add to Playlist menu leaves those out."""
    attributes = raw.get('attributes') or {}
    flags = {}
    tags = attributes.get('tags')
    if isinstance(tags, list) and FAVOURITE_TAG in tags:
        flags[FAVOURITES] = True
    if attributes.get('canEdit') is False:
        flags[EDITABLE] = False
    if flags:
        item['attributes'] = flags


def add_art_urls(library_data):
    """Give every Item with artwork its cover's URL (`artUrl`), so the cover can be fetched
    on demand by a later process, when the backend's ART_URLS is empty."""
    for item in _every_item(library_data):
        art = item.get('art')
        url = normalize.ART_URLS.get(art) if art else None
        if url:
            item['artUrl'] = url


def thumb_urls(library_data, cache_dir):
    """{thumbnail path: url} for every thumbnail the library names: what a sync fetches (the
    covers wait for the pages that show them)."""
    thumb_dir = os.path.join(cache_dir, 'thumb')
    return {path: url for path, url in normalize.collect_art_urls(library_data).items()
            if os.path.dirname(path) == thumb_dir}


def _every_item(library_data):
    for name, items in (library_data.get('sections') or {}).items():
        if name == 'songs':
            continue  # Track dicts: their thumbnails are fetched, covers they have none
        for item in items:
            if isinstance(item, dict):
                yield item
    for shelf in library_data.get('shelves') or []:
        for item in shelf.get('items') or []:
            if isinstance(item, dict):
                yield item


def _previous_library(cache_dir):
    """What library.json holds now, or {}."""
    try:
        with open(os.path.join(cache_dir, 'library.json'), encoding='utf-8') as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
