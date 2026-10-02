# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Syncing the library: Apple's answers, through the engine, into library.json and the models.

    app.library_sync = LibrarySync(app)      # the app's one: start(), cancel(), `running`
    task = app.library_sync.start()          # a sync as a task, unless one runs
    await app.library_sync.cancel()          # stopped, and nothing more of it written
    counts = await sync_library(app.engine, app.library, progress)   # the sync itself
    app.library_sync.start(quick=True)       # the short pass after a library write

fetches, in this order, the library's songs (with their albums, which is what the Albums and
Artists sections are built from), the playlists and each one's tracks, the playlist folders,
the music videos, the recently played radio stations and the Home shelves (Apple's
recommendations, Heavy Rotation, Recently Added); normalises them with the backend's pure
functions in a thread; fetches the thumbnails that are missing (covers are fetched on demand
by the pages that show them: remote.fetch_cover); writes library.json
atomically; prunes the artwork nothing names and the caches that only grow; and finally has
the Library reload() itself in place. `progress(section, done, total)` is called as it goes
(section one of PROGRESS_SECTIONS; total None until known). The songs and playlists listings
must be fetched; anything else that fails keeps last time's entry (a playlist's tracks
included: its listing is still the fresh one), read from library.json only then, as are the
tracks of a playlist the listing says are unchanged (below). Apple's 404
for a playlist's tracks or a folder's children means there are none. A failure is an
EngineError; a sync cancelled, or whose cache was wiped under it, raises store.Cancelled.
The listings are read with `unique` (api_pages): an item met twice while the library changed
under the read is kept once.

A `quick` pass (sync_library(quick=True), LibrarySync.start(quick=True)) is the one after a
library write, where a full pass would re-read everything to find one song: it fetches the
songs, the playlist listing and the shelves, and keeps last time's playlist tracks, folders,
videos and stations, none of which adding to the library changes. It writes library.json and
reloads the models as a full pass does, but stamps no last-sync and says nothing when it ends:
it is no substitute for the full one the clock waits for.

A full pass reads a playlist's tracks again only when its listing says they may have changed.
Each playlist Item records `modified`, Apple's `lastModifiedDate` as the listing had it when
the tracks were last read; a playlist listed again with that date, and with no other
`trackCount` than the tracks kept, keeps them (playlist_unchanged()). One listed without a
date, one whose tracks last time's file does not hold, and every playlist of a file from
before the key are read as before. Nothing narrows the songs listing: Apple's library API
has no changes feed, no since filter and no sort the app could stop at, and MusicKit's
`music()` hands back the body alone, so an ETag can be neither seen nor sent (docs/notes.md).

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

The Item dicts written here carry two keys beyond the normalisers' shape (both in the
README): `artUrl`, the cover's URL at config.COVER_SIZE, which fetch_cover() downloads to the
item's `art` path; and a playlist's `modified`, the stamp its kept tracks are checked against.

LibrarySync runs sync_library() for the app, one at a time: it says whether one is `running`
and relays its `progress(section, done, total)` (progress_text() words it for the window's
banner), and toasts how it ended.

When to sync: sync_due(last_sync, hours) (the last-sync and sync-interval settings), and the
choices Preferences offers for the interval (INTERVALS; interval_index()), with
last_sync_text() saying how long ago the last one was.
"""

import asyncio
import contextlib
import logging
import os
import time
from datetime import UTC, datetime
from gettext import gettext as _
from gettext import ngettext

import gi

gi.require_version('GdkPixbuf', '2.0')

from gi.repository import GdkPixbuf, GLib, GObject  # noqa: E402

from .backend import config  # noqa: E402
from .backend import normalize, store  # noqa: E402
from .backend.errors import EngineError  # noqa: E402
from .library import EDITABLE, FAVOURITES, ROOT_FOLDER, parse  # noqa: E402

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
PLAYLIST_PARAMS = {'extend': 'tags'}
FOLDER_PARAMS = {'omit[resource]': 'autos', 'platform': 'web'}
PAGE = 100
PLAYLIST_CONCURRENCY = 4   # playlists whose tracks are fetched at once
FOLDER_TYPE = 'library-playlist-folders'
PLAYLIST_TYPE = 'library-playlists'
# The fixed shelves after Apple's recommendations: (key, endpoint, page size, at most), titled
# by the app (library.FIXED_SHELF_TITLES). Each endpoint has a page cap of its own (a bigger
# `limit` is a 400, not a clamp).
SHELF_DEFS = (
    ('heavy-rotation', '/v1/me/history/heavy-rotation', 10, 10),
    ('recently-added', '/v1/me/library/recently-added', 25, 100),
)


# library.json's format: 2 since each album's tracks are numbered across its discs. A file of
# an older version is synced again at once (sync_due's library_ok).
LIBRARY_VERSION = 2

# The sync-interval choices Preferences offers, in hours, in its order: every hour, every
# 6 hours, every day, manually (0: only when asked).
INTERVALS = (1, 6, 24, 0)

# The scheduler (LibrarySync.schedule()): it looks at the clock at most CHECK_MAX seconds apart
# (a laptop that slept, a clock that moved) and at least CHECK_MIN; after a failed sync, and
# once after a sync some of whose thumbnails could not be fetched, it waits RETRY_DELAY.
CHECK_MIN = 60
CHECK_MAX = 60 * 60
RETRY_DELAY = 15 * 60

# The failures a sync reports as any command would (errors.error_message: the engine missing,
# down or kept from its keyring, the account signed out), with the button that helps; any
# other is "Could not sync your library" with Retry.
REPORTED = ('no-browser', 'engine-down', 'no-keyring', 'not-signed-in')


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


def sync_due(stamp, hours, now=None, library_ok=True):
    """Whether the library should be synced now: whatever the interval when the library on
    disk is not one to keep (`library_ok` false: missing, unreadable, or from before
    LIBRARY_VERSION); else when it never was (no stamp, or not one), when the last sync
    (`stamp`) is in the future (a clock set back) or `hours` old. 0 hours: only when asked."""
    if not library_ok:
        return True
    return next_sync_delay(stamp, hours, now) == 0


def next_sync_delay(stamp, hours, now=None):
    """Seconds until the library is due a sync by the clock (the last-sync stamp and the
    sync-interval setting): 0 when it is due now, None when the interval is manual (0)."""
    if hours <= 0:
        return None
    last = parse_stamp(stamp)
    now = now or datetime.now(UTC)
    if last is None or last > now:
        return 0
    return max(0.0, hours * 3600 - (now - last).total_seconds())


def library_ok(library):
    """Whether the library on disk is one to keep until the interval says otherwise: read,
    and written at LIBRARY_VERSION or later (an empty one too: an account with no music). A
    library not read yet counts as one (its load's `changed` asks again)."""
    state = library.props.file_state
    return state == '' or (state == 'ok' and library.props.version >= LIBRARY_VERSION)


def last_sync_text(stamp, now=None):
    """How long ago the last sync was, as the subtitle of Preferences' Last Refreshed row."""
    last = parse_stamp(stamp)
    if last is None:
        # Translators: Preferences' Last Refreshed row, while the library never was.
        return _('Never')
    now = now or datetime.now(UTC)
    minutes = int((now - last).total_seconds() // 60)
    if minutes < 1:
        # Translators: Preferences' Last Refreshed row, under a minute after a refresh.
        return _('Just now')
    if minutes < 60:
        # Translators: Preferences' Last Refreshed row: how long ago, in minutes.
        return ngettext('{count} minute ago', '{count} minutes ago',
                        minutes).format(count=minutes)
    hours = minutes // 60
    if hours < 24:
        # Translators: Preferences' Last Refreshed row: how long ago, in hours.
        return ngettext('{count} hour ago', '{count} hours ago', hours).format(count=hours)
    days = hours // 24
    # Translators: Preferences' Last Refreshed row: how long ago, in days.
    return ngettext('{count} day ago', '{count} days ago', days).format(count=days)


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


async def sync_library(engine, library, progress=None, quick=False):
    """The whole sync (see the module). Returns the counts: {albums, artists, playlists,
    loose, videos, radio, folders, shelves, art: {wanted, fetched, failed}}.

    `quick`: the pass after a library write. It reads the songs, the playlist listing and the
    shelves, and leaves the rest of last time's file alone — the playlists' tracks, the
    folders, the videos and the stations, none of which adding to the library changes. A song
    added is then in Songs and in Recently Added without the per-playlist reads a full pass
    makes. It is not a sync: the caller does not stamp last-sync for it."""
    report = progress or (lambda section, done, total: None)
    generation = store.cache_generation()  # a wipe from here on leaves this sync's files out
    status = await engine.status()
    if not status.get('authorized'):
        raise EngineError('not-signed-in', 'sign in to Apple Music to sync your library')
    storefront = str(status.get('storefront') or 'us')
    cache_dir = str(config.cache_dir())
    # The sizes this sync builds at, before anything names a file (the URLs carry them). The
    # marker, and the wipe of the thumbnails a changed size needs, wait for the artwork step:
    # a sync that fails before it keeps the thumbnails there are.
    normalize.ART_SIZES.update(normalize.wanted_art_sizes(config.COVER_SIZE, config.THUMB_SIZE))
    raw = {}  # Apple's answers, for the build, which lets go of them once they are used
    previous = _Previous(cache_dir)  # last time's file, read (in a thread) when first asked

    # 1. The songs, each with its album: the Albums and Artists sections come from them.
    report('songs', 0, None)
    raw['songs'] = await engine.api_pages(
        SONGS_ENDPOINT, {'include': 'albums'}, page=PAGE, unique=True,
        progress=lambda done, total: report('songs', done, total))

    # 2. The playlists, then each one's tracks (a few at a time), bar those whose listing
    # says nothing changed since last time's file read them. _playlists() takes last time's
    # groups and counts for a playlist whose tracks are not here: the expensive read
    # skipped, which a quick pass does for every playlist.
    report('playlists', 0, None)
    raw['playlists'] = await engine.api_pages(PLAYLISTS_ENDPOINT, PLAYLIST_PARAMS, page=PAGE,
                                              unique=True)
    if quick:
        raw['tracks'] = {}
    else:
        stamps = await asyncio.to_thread(previous.playlist_stamps)
        raw['tracks'] = await _fetch_playlist_tracks(engine, raw['playlists'], report, stamps)

    # 3. The playlist folders; 4. the music videos; 5. the stations; 6. the shelves. None of
    # these stops the sync: what fails is None here, and keeps last time's entry — which is
    # also how a quick pass leaves the three a library write cannot have changed.
    if quick:
        raw['folders'] = raw['videos'] = raw['stations'] = None
        raw['shelves'] = await _fetch_shelves(engine, report)
        return await _build(cache_dir, storefront, raw, library, report, generation, previous)
    try:
        raw['folders'] = await _fetch_folders(engine, report)
    except EngineError as error:
        _keep_going(error)
        raw['folders'] = None
    report('videos', 0, None)
    try:
        raw['videos'] = await engine.api_pages(
            VIDEOS_ENDPOINT, page=PAGE, unique=True,
            progress=lambda done, total: report('videos', done, total))
    except EngineError as error:
        _keep_going(error)
        raw['videos'] = None
    report('radio', 0, 1)
    try:
        raw['stations'] = (await engine.api(RADIO_ENDPOINT)).get('data') or []
    except EngineError as error:
        _keep_going(error)
        raw['stations'] = None
    report('radio', 1, 1)
    raw['shelves'] = await _fetch_shelves(engine, report)
    return await _build(cache_dir, storefront, raw, library, report, generation, previous)


async def _build(cache_dir, storefront, raw, library, report, generation, previous=None):
    """The end of either pass: everything into the Item shapes, the missing thumbnails
    fetched, the file written and the caches pruned, all in a thread with the artwork's
    progress relayed to this loop; then the models follow, in place. The thread cannot be
    stopped from here: cancelled, the sync tells it to give up and waits for it, so that once
    the sync's task has ended nothing more of it is written."""
    loop = asyncio.get_running_loop()
    stop = {'cancelled': False}

    def art_progress(done, total):
        loop.call_soon_threadsafe(report, 'artwork', done, total)

    build = asyncio.ensure_future(asyncio.to_thread(
        _build_and_write, cache_dir, storefront, raw, art_progress,
        lambda: stop['cancelled'], generation, previous))
    del raw  # the build's now
    try:
        await asyncio.wait([build])  # cancelling this wait leaves the build running
    except asyncio.CancelledError:
        stop['cancelled'] = True  # the thread gives up at its next fetch, or before writing
        await _wait_out(build)
        raise
    counts = build.result()
    # 8. The models follow, in place.
    await library.reload()
    return counts


async def _wait_out(future):
    """Wait until `future` has ended, however often the waiting task is cancelled meanwhile
    (the caller raises its CancelledError after); its outcome is only logged."""
    while not future.done():
        try:
            await asyncio.wait([future])
        except asyncio.CancelledError:
            continue
    if not future.cancelled() and future.exception() is not None:
        log.debug('sync: the build ended with %r', future.exception())


def _keep_going(error):
    if error.code == 'engine-down':
        raise error
    log.warning('sync: %s', error)


async def _fetch_playlist_tracks(engine, raw_playlists, report, stamps=None):
    """{playlist id: its raw tracks, or None when they could not be fetched}, a few playlists
    at a time; progress per playlist done. Apple answers 404 for a playlist with no songs:
    that is an empty list. A playlist unchanged since last time's file read its tracks
    (playlist_unchanged, against `stamps`, _Previous.playlist_stamps') is not read and has
    no entry here: the build keeps last time's tracks for it."""
    tracks = {}
    total = len(raw_playlists)
    done = 0
    kept = 0
    stamps = stamps or {}
    semaphore = asyncio.Semaphore(PLAYLIST_CONCURRENCY)

    async def fetch(raw):
        nonlocal done, kept
        playlist_id = str(raw.get('id') or '')
        if playlist_unchanged(raw, stamps.get(playlist_id)):
            kept += 1
            done += 1
            report('playlists', done, total)
            return
        async with semaphore:
            try:
                answer = await engine.api_pages(f'{PLAYLISTS_ENDPOINT}/{playlist_id}/tracks',
                                                page=PAGE)
            except EngineError as error:
                if error.status == 404:
                    answer = []
                else:
                    _keep_going(error)
                    answer = None
        tracks[playlist_id] = answer
        done += 1
        report('playlists', done, total)

    report('playlists', 0, total)
    await asyncio.gather(*(fetch(raw) for raw in raw_playlists if raw.get('id')))
    if kept:
        log.info('sync: %d of %d playlists unchanged since their tracks were last read: kept',
                 kept, total)
    return tracks


def playlist_unchanged(raw, stamp):
    """Whether a playlist as the listing has it (`raw`) can keep the tracks read last time:
    `stamp` is (modified, trackCount) from last time's file (_Previous.playlist_stamps; None
    when the file holds no tracks or no stamp for it), and the listing carries that
    `lastModifiedDate`, and either no `trackCount` or that one. Anything less (no date
    listed, a file from before the stamp, a count that differs) reads the tracks again."""
    if not stamp:
        return False
    modified, count = stamp
    attributes = raw.get('attributes') or {}
    listed = attributes.get('lastModifiedDate')
    if not modified or not isinstance(listed, str) or listed != modified:
        return False
    listed_count = attributes.get('trackCount')
    if listed_count is None:
        return True
    return not isinstance(listed_count, bool) and listed_count == count


async def _fetch_folders(engine, report):
    """library.json's `folders` from Apple's playlist folders: the root's children, then each
    folder's, breadth first, in Apple's order. Apple's root is called ROOT_FOLDER here. A
    folder answering 404 has nothing in it; one listed again (in a folder of its own) is left
    out the second time, so the tree stays a tree."""
    folders = []
    seen = {APPLE_ROOT_FOLDER}
    queue = [(APPLE_ROOT_FOLDER, None, '')]
    report('folders', 0, None)
    while queue:
        apple_id, parent, title = queue.pop(0)
        try:
            children = await engine.api_pages(f'{FOLDERS_ENDPOINT}/{apple_id}/children',
                                              FOLDER_PARAMS, page=PAGE, unique=True)
        except EngineError as error:
            if error.status != 404:
                raise
            children = []
        entry = {'id': _folder_id(apple_id), 'title': title, 'parent': parent, 'children': []}
        for child in children:
            child_id = str(child.get('id') or '')
            if not child_id:
                continue
            if child.get('type') == FOLDER_TYPE:
                if child_id in seen:
                    log.warning('sync: a playlist folder is listed twice (in a folder of its '
                                'own?); left out the second time')
                    continue
                seen.add(child_id)
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
    for number, (key, endpoint, page, limit) in enumerate(SHELF_DEFS, 2):
        try:
            fixed[key] = await engine.api_pages(endpoint, page=page, limit=limit)
        except EngineError as error:
            _keep_going(error)
            fixed[key] = None
        report('shelves', number, total)
    return {'recommendations': recommendations, 'fixed': fixed}


def _build_and_write(cache_dir, storefront, raw, art_progress, cancelled, generation=None,
                     previous=None):
    """In a thread: normalise Apple's answers (`raw`, emptied once used), fetch the missing
    thumbnails, write library.json, prune. What could not be fetched (None in `raw`), or was
    not asked for, keeps last time's entry, read from library.json (`previous`) only then."""
    counts = {}
    art_urls = {}  # every artwork path named here, and its URL
    previous = previous if previous is not None else _Previous(cache_dir)
    albums, artists = normalize.group_songs_into_albums_and_artists(raw['songs'], cache_dir,
                                                                    art_urls)
    sections = {'albums': albums, 'artists': artists}
    counts['loose'] = loose_count(raw['songs'])
    sections['playlists'] = _playlists(raw['playlists'], raw['tracks'], cache_dir, art_urls,
                                       previous)
    if raw['videos'] is None:
        sections['videos'] = previous.section('videos')
    else:
        sections['videos'] = [normalize.normalize_item(item, cache_dir, include_groups=False,
                                                       art_urls=art_urls)
                              for item in raw['videos']]
    if raw['stations'] is None:
        sections['radio'] = previous.section('radio')
    else:
        sections['radio'] = [normalize.normalize_station(item, cache_dir, art_urls=art_urls)
                             for item in raw['stations']]
    shelves = _shelves(raw['shelves'], cache_dir, art_urls, previous)
    folders = raw['folders'] if raw['folders'] is not None else previous.folders()
    raw.clear()  # the answers are not needed for the long artwork step
    previous.forget()

    library_data = {
        'version': LIBRARY_VERSION,
        'generated': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'storefront': storefront,
        'sections': sections,
        'shelves': shelves,
        'folders': folders,
    }
    add_art_urls(library_data, art_urls)
    for name in ('albums', 'artists', 'playlists', 'videos', 'radio'):
        counts[name] = len(sections[name])
    counts['folders'] = max(0, len(folders) - 1)  # the root is not a folder of the user's
    counts['shelves'] = sum(len(shelf['items']) for shelf in shelves)

    # The thumbnails first, and the listing only once they are on disk, so tiles never show
    # placeholders for artwork that is about to arrive. A failure is logged, nothing more.
    # The sizes' marker is written now: a changed thumbnail size wipes thumb/ here, not before.
    _stop_if(cancelled, generation, cache_dir)
    normalize.apply_art_sizes(cache_dir, config.COVER_SIZE, config.THUMB_SIZE, generation)
    try:
        counts['art'] = normalize.download_art(thumb_urls(library_data, cache_dir, art_urls),
                                               cache_dir,
                                               progress=art_progress, cancelled=cancelled,
                                               generation=generation)
    except normalize.Cancelled:
        raise  # cancelled, or the cache was cleared: nothing more is written
    except Exception as error:
        log.warning('sync: artwork: %s', error)
        counts['art'] = {'wanted': 0, 'fetched': 0, 'failed': 0}
    _stop_if(cancelled, generation, cache_dir)  # with nothing to fetch, download_art did not ask
    normalize.save_library(library_data, cache_dir, indent=None,  # compact: a third the size
                           generation=generation)
    _stop_if(cancelled, generation, cache_dir)
    normalize.prune_art(library_data, cache_dir)
    normalize.prune_caches(cache_dir)
    log.info('library synced: %s', ', '.join(
        f'{counts[name]} {label}' for name, label in (
            ('albums', 'albums'), ('artists', 'artists'), ('playlists', 'playlists'),
            ('loose', 'loose songs'), ('videos', 'videos'), ('radio', 'stations'),
            ('folders', 'folders'))))
    return counts


def _playlists(raw_playlists, playlist_tracks, cache_dir, art_urls, previous):
    """The playlist Items, each from the listing just fetched (its title, artwork, tags). One
    whose tracks were not fetched (unchanged, a quick pass, a failed read) keeps last time's
    tracks and counts, or, new since, has no groups yet, which its page fetches when it is
    shown: never an empty list that would read as an empty playlist.

    `modified` is the listing's `lastModifiedDate` when the tracks were read now, and stays
    last time's when they were kept: it says which listing the tracks in hand belong to, so
    a later pass compares the right dates (playlist_unchanged)."""
    playlists = []
    for raw in raw_playlists:
        playlist_id = str(raw.get('id') or '')
        attributes = raw.get('attributes') or {}
        tracks = playlist_tracks.get(playlist_id)
        item = normalize.normalize_playlist(raw, cache_dir, tracks=tracks or [],
                                            art_urls=art_urls)
        flag_favourites(item, raw)
        if tracks is None:
            old = previous.playlist(playlist_id)
            if old is not None and old.get('groups'):
                item['groups'] = old['groups']
                counted = old
                if isinstance(old.get('modified'), str):
                    item['modified'] = old['modified']
            else:
                item['groups'] = []
                counted = {'trackCount': attributes.get('trackCount')}
            for key in ('trackCount', 'durationMs', 'countLabel'):
                if counted.get(key) is not None:
                    item[key] = counted[key]
                else:
                    item.pop(key, None)
        elif isinstance(attributes.get('lastModifiedDate'), str):
            item['modified'] = attributes['lastModifiedDate']
        playlists.append(item)
    return playlists


def _shelves(shelves_raw, cache_dir, art_urls, previous):
    """The Home shelves: Apple's recommendations, then the fixed shelves (SHELF_DEFS), each
    keeping last time's when it could not be fetched. The app titles them by key."""
    shelves = []
    if shelves_raw['recommendations'] is None:
        shelves.extend(shelf for shelf in previous.shelves()
                       if str(shelf.get('key', '')).startswith('rec-'))
    else:
        shelves.extend(normalize.recommendation_shelves(shelves_raw['recommendations'],
                                                        cache_dir, art_urls))
    for key, _endpoint, _page, _limit in SHELF_DEFS:
        raw_items = shelves_raw['fixed'].get(key)
        if raw_items is None:
            items = next((shelf.get('items') or [] for shelf in previous.shelves()
                          if shelf.get('key') == key), [])
        else:
            items = [normalize.normalize_item(item, cache_dir, include_groups=False,
                                              art_urls=art_urls)
                     for item in raw_items]
        shelves.append({'key': key, 'title': '', 'items': items})
    return shelves


class _Previous:
    """Last time's library.json, read (in the build's thread, a list element at a time:
    library.parse) only when something could not be fetched and falls back to it."""

    def __init__(self, cache_dir):
        self._path = os.path.join(cache_dir, 'library.json')
        self._data = None
        self._playlists = None

    def _read(self):
        if self._data is None:
            try:
                with open(self._path, encoding='utf-8') as file:
                    data = parse(file.read())
            except (OSError, ValueError, RecursionError):
                data = {}
            self._data = data if isinstance(data, dict) else {}
        return self._data

    def section(self, name):
        sections = self._read().get('sections')
        entries = sections.get(name) if isinstance(sections, dict) else None
        return [entry for entry in entries or [] if isinstance(entry, dict)]

    def shelves(self):
        return [shelf for shelf in self._read().get('shelves') or [] if isinstance(shelf, dict)]

    def folders(self):
        return [entry for entry in self._read().get('folders') or [] if isinstance(entry, dict)]

    def playlist(self, playlist_id):
        if self._playlists is None:
            self._playlists = {entry.get('id'): entry for entry in self.section('playlists')}
        return self._playlists.get(playlist_id)

    def playlist_stamps(self):
        """{playlist id: (modified, trackCount)} for every playlist whose tracks the file
        holds under a stamp: what playlist_unchanged() checks a listing against. Reads the
        file: call it in a thread."""
        return {entry.get('id'): (entry['modified'], entry.get('trackCount'))
                for entry in self.section('playlists')
                if entry.get('groups') and isinstance(entry.get('modified'), str)}

    def forget(self):
        self._data = self._playlists = None


def _stop_if(cancelled, generation, cache_dir):
    """Raise Cancelled when the sync was cancelled, CacheGone when the cache was wiped since
    it began (store.py): the build writes and prunes nothing more."""
    if not store.current(generation):
        raise store.CacheGone(cache_dir)
    if cancelled():
        raise normalize.Cancelled('sync')


def loose_count(raw_songs):
    """How many of the library's songs are in no library album (their `albums` relationship
    is empty): the Albums section holds each under a stand-in album, which plays its songs."""
    return sum(1 for raw in raw_songs
               if str(raw.get('id') or '')
               and not ((raw.get('relationships') or {}).get('albums') or {}).get('data'))


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


def add_art_urls(library_data, art_urls):
    """Give every Item with artwork its cover's URL (`artUrl`, from the build's registry,
    `art_urls`), so a page can fetch the cover when it first shows it."""
    for item in _every_item(library_data):
        art = item.get('art')
        url = art_urls.get(art) if art else None
        if url:
            item['artUrl'] = url


def thumb_urls(library_data, cache_dir, art_urls):
    """{thumbnail path: url} for every thumbnail the library names: what a sync fetches (the
    covers wait for the pages that show them)."""
    thumb_dir = os.path.join(cache_dir, 'thumb')
    return {path: url for path, url in normalize.collect_art_urls(library_data, art_urls).items()
            if os.path.dirname(path) == thumb_dir}


def _every_item(library_data):
    for name, items in (library_data.get('sections') or {}).items():
        if name == 'songs':
            continue  # an older sync's Track dicts: thumbnails only
        for item in items:
            if isinstance(item, dict):
                yield item
    for shelf in library_data.get('shelves') or []:
        for item in shelf.get('items') or []:
            if isinstance(item, dict):
                yield item


# -- the app's sync ----------------------------------------------------------------------


def progress_texts():
    """{section: (the sentence with a count, the one without)} for each of PROGRESS_SECTIONS,
    translated on call, after gettext is set up; `{done}` and `{total}` are filled in with
    the numbers as the locale writes them."""
    # Translators: the sync banner, while the stations played last and Apple's
    # recommendations for the Home page are fetched.
    recommendations = _('Syncing recommendations…')
    return {
        # Translators: the sync banner, while the library's songs are fetched: "Syncing
        # songs: 300 of 2,000".
        'songs': (_('Syncing songs: {done} of {total}'), _('Syncing songs…')),
        # Translators: the sync banner, while each playlist's songs are fetched.
        'playlists': (_('Syncing playlists: {done} of {total}'), _('Syncing playlists…')),
        # Translators: the sync banner, while the folders the playlists are in are fetched.
        'folders': (_('Syncing playlist folders: {done} of {total}'),
                    _('Syncing playlist folders…')),
        # Translators: the sync banner, while the library's music videos are fetched.
        'videos': (_('Syncing music videos: {done} of {total}'), _('Syncing music videos…')),
        # A step or two each: no count.
        'radio': (recommendations, recommendations),
        'shelves': (recommendations, recommendations),
        # Translators: the sync banner, while the covers' small versions are downloaded.
        'artwork': (_('Downloading artwork: {done} of {total}'), _('Downloading artwork…')),
    }


def progress_text(section, done, total):
    """The banner's sentence for a progress report (section one of PROGRESS_SECTIONS, '' as
    the sync starts; total None until known): "Syncing songs: 300 of 2,000"."""
    texts = progress_texts().get(section)
    if texts is None:
        return _('Syncing your library…')
    counted, uncounted = texts
    if not total:
        return uncounted
    return counted.format(done=f'{done:n}', total=f'{total:n}')


def phase_text(phases, ended):
    """The seconds each phase of a run took, for the log: `phases` is [(section, started)]
    at each section's first progress report, in order ('' as the run starts: the engine's
    start), each lasting until the next section's first report, the last until `ended`. The
    normalising lands in the last fetched section's time, the write and the reload in the
    artwork's: "engine 0.1, songs 12.1, playlists 13.0, …"."""
    if not phases:
        return ''
    ends = [started for _section, started in phases[1:]] + [ended]
    return ', '.join(f'{section or "engine"} {end - started:.1f}'
                     for (section, started), end in zip(phases, ends, strict=True))


class LibrarySync(GObject.Object):
    """The app's sync: sync_library() through `app.engine` into `app.library`, one at a time.

    `running` is true from start() until the run has ended, and the library's `syncing` while
    it runs; `progress(section, done, total)` relays the run's reports (section '' as it
    starts, total None until known). cancel()
    returns once nothing more of the run will be written, and hold() keeps any run from
    starting (signing in or out, clearing the cache) until release().

    schedule() starts the timed refresh: a sync starts when one is due (due(): the
    sync-interval setting, or a library on disk not to keep) and can run without starting
    anything (check(): signed in, the engine up and authorized), looked at when the timer
    fires, when the engine comes up or is signed in, and when the library has been read. A
    timer never starts Chrome. After a failed sync the timer waits RETRY_DELAY; a sync some
    of whose thumbnails failed is tried once more RETRY_DELAY later.

    `app` gives the engine, the library, the settings, `demo`, `spawn()`, `toast()`,
    `report()` and `refuse_in_demo()`; `add_timeout(seconds, callback)` and
    `remove_timeout(id)` are GLib's unless a test passes its own, as `clock` (seconds)."""

    __gtype_name__ = 'AppleMusicLibrarySync'

    __gsignals__ = {
        'progress': (GObject.SignalFlags.RUN_FIRST, None, (str, int, object)),
    }

    running = GObject.Property(type=bool, default=False)

    def __init__(self, app, add_timeout=None, remove_timeout=None, clock=time.monotonic):
        super().__init__()
        self._app = app
        self._add_timeout = add_timeout or GLib.timeout_add_seconds
        self._remove_timeout = remove_timeout or GLib.source_remove
        self._clock = clock
        self._task = None
        self._holds = 0
        self._scheduled = False  # schedule() has been called
        self._timer = None  # the scheduler's next look at the clock
        self._retry = None  # the one retry after thumbnails failed
        self._failed_at = None  # when the last sync failed (clock), until one succeeds

    def hold(self):
        """Start no sync until release(): the account or the cache is changing under it."""
        self._holds += 1

    def release(self):
        self._holds = max(0, self._holds - 1)
        self._arm()

    @contextlib.contextmanager
    def held(self):
        """hold() for the length of a `with` block."""
        self.hold()
        try:
            yield
        finally:
            self.release()

    def due(self):
        """Whether the library should be synced now (sync_due): the last sync older than the
        sync-interval setting (hours; 0 only when asked), or no library on disk to keep."""
        app = self._app
        return sync_due(app.settings.get_string(app.account_key('last-sync')),
                        app.settings.get_int('sync-interval'), library_ok=library_ok(app.library))

    # -- running one -----------------------------------------------------------------------

    def start(self, retry=False, quick=False):
        """Sync the library through the engine, starting it if it is down, unless a sync is
        running already or held. Returns the task, or None. Signed out, nothing starts and
        the sign-in is offered (app.report). `retry`: the one retry after thumbnails failed.
        `quick`: the pass after a library write (sync_library), which neither stamps
        last-sync nor says it synced, being no substitute for a full one."""
        app = self._app
        if app.refuse_in_demo():
            return None
        if not app.settings.get_boolean(app.account_key('signed-in')):
            app.report(EngineError('not-signed-in', 'sign in to Apple Music to sync'))
            return None
        if self._holds:
            log.debug('no sync while the account or the cache changes')
            return None
        if self._task is not None and not self._task.done():
            log.debug('a sync is running already')
            return None
        self.running = True
        task = self._task = app.spawn(self._run(retry, quick))
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task):
        if task is self._task:
            self.running = False  # however it ended, cancelled before it began included
            self._arm()

    async def cancel(self):
        """Stop the sync running, if one is, and wait for it to end: its task ends only
        once its thread has, so nothing more of it is written after this returns."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])

    async def _run(self, retry=False, quick=False):
        app = self._app
        engine = app.engine
        started = time.monotonic()
        live = [True]
        phases = []  # (section, when its first report came), for the log's timings

        def progress(section, done, total):
            # A report the build thread queued before the run ended arrives after it: the
            # banner is not shown again for it.
            if live[0]:
                if not phases or phases[-1][0] != section:
                    phases.append((section, time.monotonic()))
                self.emit('progress', section, done, total)

        app.library.syncing = True  # an empty library is on its way, not empty (the pages)
        try:
            progress('', 0, None)  # the banner, while Chrome may take seconds to come up
            await engine.start()  # a start under way is joined; a running engine is kept
            counts = await sync_library(engine, app.library, progress, quick=quick)
        except store.Cancelled as error:
            log.info('sync stopped: %s', error)  # the cache was cleared under it
            return
        except EngineError as error:
            self._failed_at = self._clock()
            if error.code in REPORTED:
                app.report(error)  # its sentence, and the button that helps
            else:
                log.warning('sync: %s', error)
                app.toast(_('Could not sync your library'), _('Retry'), 'app.sync')
            return
        except Exception:
            self._failed_at = self._clock()
            log.exception('sync failed')
            app.toast(_('Could not sync your library'), _('Retry'), 'app.sync')
            return
        finally:
            live[0] = False
            app.library.syncing = False
        self._failed_at = None
        if quick:
            # No last-sync stamp and no toast: the write's own toast has been shown, and a
            # quick pass must not put off the full one the clock is waiting for.
            log.info('quick sync done in %.0f s (%s)', time.monotonic() - started,
                     phase_text(phases, time.monotonic()))
            return
        app.settings.set_string(app.account_key('last-sync'),
                                datetime.now(UTC).isoformat(timespec='seconds'))
        log.info('sync done in %.0f s (%s)', time.monotonic() - started,
                 phase_text(phases, time.monotonic()))
        failed = (counts.get('art') or {}).get('failed', 0)
        if failed and not retry:
            log.info('%d thumbnails could not be fetched: trying again in %d minutes', failed,
                     RETRY_DELAY // 60)
            self._retry_later()
        albums, playlists = counts.get('albums', 0), counts.get('playlists', 0)
        songs = app.library.song_count()
        summary = ', '.join([
            ngettext('{count} album', '{count} albums', albums).format(count=f'{albums:n}'),
            ngettext('{count} playlist', '{count} playlists', playlists).format(
                count=f'{playlists:n}'),
            ngettext('{count} song', '{count} songs', songs).format(count=f'{songs:n}'),
        ])
        # Translators: the toast when a refresh of the library (Refresh Library) ends, with
        # what the library holds: "Library synced: 120 albums, 8 playlists, 1,400 songs".
        app.toast(_('Library synced: {summary}').format(summary=summary))

    # -- the timed refresh -------------------------------------------------------------------

    def schedule(self):
        """Look after the library from now on (see the class): the app calls it once, in
        do_startup, except in demo mode."""
        if self._scheduled:
            return
        self._scheduled = True
        app = self._app
        app.settings.connect('changed::sync-interval', self._arm)
        app.settings.connect('changed::' + app.account_key('last-sync'), self._arm)
        app.engine.connect('notify::state', self._on_changed)
        app.engine.connect('notify::authorized', self._on_changed)
        app.library.connect('changed', self._on_changed)
        self._arm()

    def check(self, *_args):
        """Start a sync when one is due and can run without starting anything."""
        if self._can_run() and self.due():
            log.info('the library is due a sync')
            self.start()

    def _can_run(self):
        """Signed in, the engine up and authorized, no sync held or running, no failure in
        the last RETRY_DELAY seconds."""
        app = self._app
        engine = app.engine
        return (not app.demo and not self._holds and not self.running
                and app.settings.get_boolean(app.account_key('signed-in'))
                and engine.state == 'up' and engine.authorized
                and not self._backing_off())

    def _backing_off(self):
        return (self._failed_at is not None
                and self._clock() - self._failed_at < RETRY_DELAY)

    def _on_changed(self, *_args):
        """The engine came up or went down, was signed in or out, or the library was read:
        a sync if one is due now, and the timer set again."""
        self.check()
        self._arm()

    def _arm(self, *_args):
        """The scheduler's next look: when the library will be due (next_sync_delay; at once
        for a library not to keep), no sooner than CHECK_MIN nor later than CHECK_MAX
        seconds from now, and not before a failed sync's RETRY_DELAY is over. None while the
        interval is manual and the library fine, and while the engine is not up or the
        account not signed in (their change sets it again)."""
        if not self._scheduled:
            return
        if self._timer is not None:
            self._remove_timeout(self._timer)
            self._timer = None
        app = self._app
        if (app.engine.state != 'up'
                or not app.settings.get_boolean(app.account_key('signed-in'))):
            return
        delay = next_sync_delay(app.settings.get_string(app.account_key('last-sync')),
                                app.settings.get_int('sync-interval'))
        if not library_ok(app.library):
            delay = 0
        if delay is None:
            return
        if self._backing_off():
            delay = max(delay, RETRY_DELAY - (self._clock() - self._failed_at))
        self._timer = self._add_timeout(int(min(max(delay, CHECK_MIN), CHECK_MAX)),
                                        self._on_timer)

    def _on_timer(self):
        self._timer = None
        self.check()
        self._arm()
        return GLib.SOURCE_REMOVE

    def _retry_later(self):
        if self._retry is not None:
            self._remove_timeout(self._retry)
        self._retry = self._add_timeout(RETRY_DELAY, self._on_retry)

    def _on_retry(self):
        """The one retry of a sync whose thumbnails failed, when it can run now; either way
        the interval rules after it."""
        self._retry = None
        if self._can_run():
            self.start(retry=True)
        return GLib.SOURCE_REMOVE
