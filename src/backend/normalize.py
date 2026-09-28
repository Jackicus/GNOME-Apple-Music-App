"""Apple's answers as the library's data, and the artwork cache.

Pure functions turn Apple Music API answers into the Item, Track and shelf shapes that
README.md describes: normalize_album() and the other normalize_* functions, the search and
shelf shapers the engine's commands answer with, and group_songs_into_albums_and_artists() for
the sync. They write data, never words: counts are numbers (trackCount, durationMs,
albumCount), a shelf the app names is a key with an empty title, a missing name is '', and
the app makes and translates the words (library.py, the pages). Beside them, the artwork
cache (a file name for each artwork URL, fetching what is missing, pruning what nothing
names), library.json, and the answers the engine keeps for a day. The standard library only;
the thread pool and urllib are imported where they are used.
"""

import hashlib
import html
import json
import logging
import os
import re
import time
from datetime import UTC, datetime

from . import config, store

log = logging.getLogger(__name__)

# What download_art raises when it gives up (store.CacheGone, a wiped cache, is one too).
Cancelled = store.Cancelled

# How long a kept answer (landing, a category, the New page, Made for You) is answered from
# the cache before Apple is asked again, and a song's lyrics (Apple replaces plain lyrics with
# synced ones, and corrects them).
ANSWER_MAX_AGE = 24 * 60 * 60
LYRICS_MAX_AGE = 30 * 24 * 60 * 60

# What prune_caches() leaves of the caches that only grow: the covers the pages fetch
# themselves, and the lyrics of the songs played last.
REMOTE_ART_BUDGET = 32 * 1024 * 1024
LYRICS_KEEP = 2000
# The day-long answers in the cache's top folder, beside categories/.
KEPT_ANSWERS = ('landing.json', 'browse.json', 'made-for-you.json')

# Scaling a cached cover down to its thumbnail, without fetching it again,
# takes an image library, and nothing in the backend imports gi. The app
# installs one here: a callable scale_image(src_path, dest_path, size) that
# writes a JPEG at most size x size to dest_path and raises on failure
# (GdkPixbuf, in the app). Without one, thumbnails are downloaded instead.
scale_image = None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_duration(duration_ms):
    """Format milliseconds into 'm:ss' or 'h:mm:ss'.

    e.g. 216000 -> '3:36', 3661000 -> '1:01:01', 0 -> '0:00'.
    """
    if duration_ms is None or duration_ms <= 0:
        return '0:00'

    total_seconds = round(duration_ms / 1000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    if hours > 0:
        return f'{hours}:{minutes:02d}:{seconds:02d}'
    return f'{minutes}:{seconds:02d}'


def format_color(bg_color):
    """Normalize hex color string to #rrggbb or None."""
    if not bg_color:
        return None
    bg_color = bg_color.strip()
    if not bg_color:
        return None
    if not bg_color.startswith('#'):
        return f'#{bg_color}'
    return bg_color


def strip_html(text):
    """An HTML snippet (Apple's editorial notes) as plain text, or None: a <br> is a line
    break and a paragraph a blank line, so the words of two never run together."""
    if not text:
        return None
    cleaned = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'</?p(\s[^>]*)?>', '\n\n', cleaned, flags=re.IGNORECASE)
    cleaned = html.unescape(re.sub(r'<[^>]+>', '', cleaned))
    cleaned = re.sub(r'[ \t]*\n[ \t]*', '\n', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned if cleaned else None


# ---------------------------------------------------------------------------
# Artwork templating, caching, and pruning
# ---------------------------------------------------------------------------


def template_artwork_url(url_template, width=None, height=None):
    """Format an Apple Music artwork URL template by replacing dimensions and formats.

    Replaces {w} with width, {h} with height (the cover's size unless given).
    Also {f} with 'jpg', {c} with 'bb' if present.
    """
    if not url_template:
        return ''
    width = width or ART_SIZES['cover']
    height = height or ART_SIZES['cover']
    return (
        url_template.replace('{w}x{h}', f'{width}x{height}')
        .replace('{w}', str(width))
        .replace('{h}', str(height))
        .replace('{f}', 'jpg')
        .replace('{c}', 'bb')
    )


def format_artwork_url(artwork_obj, width=None, height=None):
    """Convenience wrapper for dict artwork object or string template."""
    if not artwork_obj:
        return None
    if isinstance(artwork_obj, dict):
        url = artwork_obj.get('url')
    else:
        url = str(artwork_obj)
    if not url:
        return None
    return template_artwork_url(url, width, height)


def artwork_filename(url):
    """Return the SHA-1 cache filename for an artwork URL."""
    if not url:
        return ''
    digest = hashlib.sha1(url.encode('utf-8')).hexdigest()
    return f'{digest}.jpg'


def artwork_cache_path(url, cache_dir):
    """Return the absolute path in the cache directory for an artwork URL."""
    return os.path.join(cache_dir, 'art', artwork_filename(url))


# The two sizes artwork is kept at, in pixels. The cover is the hero of a detail page; the
# thumbnail is the copy the tiles and the track rows draw, so a page of tiles decodes small
# files. Named after the same URL as the full-size file, so <cache>/thumb/<x>.jpg is the
# thumbnail of <cache>/art/<x>.jpg. These are the defaults, from config; a sync sets them
# with apply_art_sizes, and the marker file beside the covers records what a cache was built
# at (load_art_sizes reads it once, at startup).
DEFAULT_ART_SIZES = {'cover': config.COVER_SIZE, 'thumb': config.THUMB_SIZE}
ART_SIZES = dict(DEFAULT_ART_SIZES)
ART_SIZE_LIMITS = {'cover': (256, 1024), 'thumb': (96, 512)}


def _art_sizes_marker(cache_dir):
    return os.path.join(cache_dir, 'art', '.sizes')


def _read_art_sizes_marker(cache_dir):
    """What the cache was built at, or the defaults where nothing says."""
    sizes = dict(DEFAULT_ART_SIZES)
    try:
        with open(_art_sizes_marker(cache_dir), encoding='utf-8') as f:
            marker = json.load(f)
    except FileNotFoundError:
        return sizes
    except (OSError, ValueError) as error:
        log.debug('art sizes marker: %s', error)
        return sizes
    if isinstance(marker, dict):
        for key in sizes:
            if isinstance(marker.get(key), int) and marker[key] > 0:
                sizes[key] = marker[key]
    return sizes


def load_art_sizes(cache_dir):
    """The sizes the cache at `cache_dir` was built at, from its marker, taken as this
    process's ART_SIZES so every URL it names agrees with the files on disk. A tiny file read,
    once at startup (the app's install_scaler)."""
    ART_SIZES.update(_read_art_sizes_marker(cache_dir))
    return dict(ART_SIZES)


def apply_art_sizes(cache_dir, cover, thumb, generation=None):
    """Set the sizes a sync builds at, and record them in the marker. A
    thumbnail size that differs from the marker's wipes <cache>/thumb/:
    the files keep their names whatever the size, so nothing else would
    tell a stale one from a right one, and they are rebuilt from the
    covers without a fetch. A cover size that differs needs nothing: the
    new size is a new URL, hence a new name, and the old files go with
    the next prune as unreferenced."""
    wanted = {}
    for key, value in (('cover', cover), ('thumb', thumb)):
        low, high = ART_SIZE_LIMITS[key]
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = ART_SIZES[key]
        wanted[key] = max(low, min(high, value))
    previous = _read_art_sizes_marker(cache_dir)
    if wanted['thumb'] != previous['thumb']:
        thumb_dir = os.path.join(cache_dir, 'thumb')
        try:
            for entry in os.listdir(thumb_dir):
                try:
                    os.remove(os.path.join(thumb_dir, entry))
                except OSError as error:
                    log.debug('stale thumbnail: %s', error)
        except OSError as error:
            log.debug('stale thumbnails: %s', error)
    try:
        store.atomic_write(_art_sizes_marker(cache_dir), lambda f: json.dump(wanted, f),
                           root=cache_dir, text=True, generation=generation)
    except OSError as error:
        log.warning('could not write the art sizes marker: %s', error)
    ART_SIZES.update(wanted)
    return dict(ART_SIZES)


def thumb_cache_path(url, cache_dir):
    """The thumbnail's path for a full-size artwork URL."""
    return os.path.join(cache_dir, 'thumb', artwork_filename(url))


def make_thumbnail(src_path, dest_path, cache_dir, size=None, generation=None):
    """Scale the cover at `src_path` down to `dest_path` (in the cache at
    `cache_dir`), atomically. False without a scale_image installed, when the
    source is not an image, or when `generation` moved (store.py)."""
    if scale_image is None:
        return False
    size = size or ART_SIZES['thumb']
    try:
        return store.atomic_create(dest_path, lambda temp: scale_image(src_path, temp, size),
                                   root=cache_dir, generation=generation) is not None
    except Exception as error:  # the scaler's own errors (GLib.Error) as well as OSError
        log.debug('could not scale %s: %s', src_path, error)
        return False


def cache_thumbnail(url, cache_dir, dest_path, generation=None):
    """The thumbnail at `dest_path`: scaled from the cached full-size cover
    when that is on disk, fetched at the thumbnail size from `url` otherwise."""
    if not url or not dest_path:
        return None
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path
    full = os.path.join(cache_dir, 'art', os.path.basename(dest_path))
    if not _art_missing(full) and make_thumbnail(full, dest_path, cache_dir,
                                                 generation=generation):
        return dest_path
    return cache_artwork(url, cache_dir, dest_path=dest_path, generation=generation)


def cache_artwork(url_or_obj, cache_dir, timeout=10.0, dest_path=None, generation=None):
    """Download artwork via urllib.request and save it atomically to <cache_dir>/art/
    (or to `dest_path`, for a thumbnail; a `dest_path` outside the cache is refused).

    Accepts either an artwork URL string or an Apple Music artwork dictionary.
    Written through a temporary file renamed over the target (store.atomic_write), without an
    fsync: a copy of what is on Apple's servers, which counts as missing if a crash left it
    empty. Returns the absolute local file path on success, or None when it failed (logged,
    with the reason) or `generation` moved (the cache was cleared: store.py).
    """
    if not url_or_obj or not cache_dir:
        return None

    if isinstance(url_or_obj, dict):
        url = format_artwork_url(url_or_obj)
    else:
        url = str(url_or_obj)

    if not url:
        return None

    dest_path = os.path.abspath(dest_path or artwork_cache_path(url, cache_dir))
    if not store.inside(dest_path, cache_dir):
        log.warning('artwork: %s is outside the cache, not written', dest_path)
        return None

    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path
    if not store.current(generation):
        return None

    import shutil
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'AppleMusicGNOME/1.0'}
    )

    def fetch(file):
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', 200)
            if status != 200:
                raise _FetchError(f'HTTP {status}')
            shutil.copyfileobj(resp, file, 64 * 1024)

    try:
        return store.atomic_write(dest_path, fetch, root=cache_dir, fsync=False,
                                  generation=generation)
    except Exception as error:  # urllib's HTTPError, URLError and timeouts, OSError
        if isinstance(error, urllib.error.HTTPError):
            error.close()  # it holds the answer's connection
        log.warning('artwork: could not fetch %s: %s', url, error)
        return None


class _FetchError(Exception):
    """An answer that is not the image: its status."""


# The artwork registry, `art_urls`: every artwork path the normalisers hand out, and the URL
# it is fetched from (the cover-sized one for a cover, the thumbnail-sized one for its
# thumbnail). A job that fetches what it normalised (a sync, the engine's item()) makes one
# dict and passes it to the normalisers, then to collect_art_urls() or download_item_art();
# one that only names files passes nothing and a throwaway dict is used. Normalisation only
# names the file; nothing is fetched until download_art() runs over a finished library (or
# download_item_art() over one item), so a sync's hundreds of downloads happen together, in
# threads, rather than one at a time in the middle of building each item.


def place_in_remote_art(item, art_urls, cache_dir):
    """Point an item fetched on demand (the engine's item(): a catalog album, playlist or
    artist, mostly not in the library) at <cache>/remote-art/ for the artwork the library
    does not have on disk. art/ and thumb/ belong to library.json and are pruned against it
    after every sync, which would take the files from under an open page; remote-art/ is
    trimmed only to its budget. Each file is named after its URL's hash, as the pages' own
    remote art is (widgets.artwork.remote_art_path), so the player bar and a page share one.
    Rewrites the item's `art`, `thumb` and rows' `thumb` in place and returns the registry of
    what it points at: {path: url}, for download_item_art."""
    remote = os.path.join(cache_dir, 'remote-art')
    placed = {}

    def place(path):
        url = art_urls.get(path) if path else None
        if url is None:
            return path
        if not _art_missing(path):
            placed[path] = url  # the library's copy, on disk already
            return path
        moved = os.path.join(remote, artwork_filename(url))
        placed[moved] = url
        return moved

    for key in ('art', 'thumb'):
        if item.get(key):
            item[key] = place(item[key])
    for group in item.get('groups') or []:
        for entry in group.get('entries') or []:
            if isinstance(entry, dict) and entry.get('thumb'):
                entry['thumb'] = place(entry['thumb'])
    return placed


def _is_thumb_path(path, cache_dir):
    thumbs = os.path.abspath(os.path.join(cache_dir, 'thumb'))
    return os.path.dirname(os.path.abspath(path)) == thumbs


def _art_missing(path):
    try:
        return os.path.getsize(path) <= 0
    except OSError:
        return True


def collect_art_urls(library_data, art_urls):
    """{path: url} for every artwork the library refers to that `art_urls` knows."""
    return {p: art_urls[p] for p in collect_art_paths(library_data) if p in art_urls}


def download_art(urls, cache_dir, workers=8, progress=None, cancelled=None, generation=None):
    """Fetch the artwork of a {path: url} map (collect_art_urls') that is not in
    the cache yet.

    Returns {"wanted", "fetched", "failed"}. Failures are logged (by
    cache_artwork, with the reason) and otherwise ignored: the UI treats a
    path that is not on disk as no artwork. `progress(done, total)` is called
    (on this thread) after each fetch, and `cancelled()` is asked before each
    result is waited for: once it answers True, the fetches not started yet are
    given up, the running ones finish, and Cancelled is raised, so a caller
    never takes a cancelled download for a finished one. When the cache's
    generation moves from `generation` (it was cleared: store.py), the same,
    with store.CacheGone (a Cancelled), and the running ones write nothing.
    """
    todo = {p: u for p, u in urls.items() if _art_missing(p)}
    counts = {'wanted': len(urls), 'fetched': 0, 'failed': 0}
    if not todo:
        return counts
    import concurrent.futures
    for folder in ('art', 'thumb'):
        if not store.make_dirs(os.path.join(cache_dir, folder), root=cache_dir,
                               generation=generation):
            raise store.CacheGone(cache_dir)
    # The covers first, the thumbnails after: a thumbnail is scaled from its
    # cover when that is on disk, and fetched only when it is not.
    covers = {p: u for p, u in todo.items() if not _is_thumb_path(p, cache_dir)}
    thumbs = {p: u for p, u in todo.items() if _is_thumb_path(p, cache_dir)}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for batch, fetch in (
                (covers, lambda p, u: cache_artwork(u, cache_dir, generation=generation)),
                (thumbs, lambda p, u: cache_thumbnail(u, cache_dir, p, generation=generation))):
            if cancelled and cancelled():
                raise Cancelled('artwork')
            futures = {pool.submit(fetch, path, url): url for path, url in batch.items()}
            for fut in concurrent.futures.as_completed(futures):
                gone = not store.current(generation)
                if gone or (cancelled and cancelled()):
                    for pending in futures:
                        pending.cancel()  # the pool still waits for the running ones
                    raise store.CacheGone(cache_dir) if gone else Cancelled('artwork')
                ok = False
                try:
                    ok = bool(fut.result())
                except Exception as error:
                    log.warning('artwork: could not fetch %s: %s', futures[fut], error)
                if ok:
                    counts['fetched'] += 1
                else:
                    counts['failed'] += 1
                done += 1
                if progress:
                    progress(done, len(todo))
    return counts


def download_item_art(item, cache_dir, art_urls, generation=None):
    """Fetch what one item refers to and lacks (its cover, its thumbnail and
    its rows' thumbnails, a playlist's), from the URLs `art_urls` holds for
    them (the registry it was normalised with), in threads. Nothing more once
    the cache's generation moves from `generation` (store.py)."""
    if not isinstance(item, dict):
        return item
    paths = _item_art_paths(item)
    try:
        download_art({p: art_urls[p] for p in paths if p in art_urls}, cache_dir,
                     generation=generation)
    except store.CacheGone:
        log.debug('item %s: the cache was cleared, its artwork left', item.get('id'))
    return item


def _item_art_paths(item):
    """Every artwork path an item refers to: its own, and its rows' thumbnails."""
    paths = set()
    for key in ('art', 'thumb'):
        if item.get(key):
            paths.add(os.path.abspath(item[key]))
    for group in item.get('groups') or []:
        for entry in group.get('entries') or []:
            if isinstance(entry, dict) and entry.get('thumb'):
                paths.add(os.path.abspath(entry['thumb']))
    return paths


def collect_art_paths(library_data):
    """Collect all referenced local artwork paths — covers and thumbnails —
    from a library data dict."""
    paths = set()
    for sec_items in library_data.get('sections', {}).values():
        for item in sec_items:
            paths |= _item_art_paths(item)
    for shelf in library_data.get('shelves', []):
        for item in shelf.get('items', []):
            paths |= _item_art_paths(item)
    return paths


def prune_art(library_data, cache_dir):
    """Remove artwork files in <cache_dir>/art/ and <cache_dir>/thumb/ that
    the library no longer refers to. Returns the count of pruned files."""
    if not cache_dir:
        return 0

    norm_refs = set()
    for p in collect_art_paths(library_data):
        norm_refs.add(os.path.abspath(p))
        norm_refs.add(os.path.basename(p))

    pruned = 0
    for folder in ('art', 'thumb'):
        art_dir = os.path.join(cache_dir, folder)
        if not os.path.isdir(art_dir):
            continue
        try:
            for entry in os.listdir(art_dir):
                file_path = os.path.join(art_dir, entry)
                # The sizes marker lives here too, and is nobody's artwork; a temporary file
                # is a download in progress, or a crash's leftover once it is old.
                if entry.startswith('.'):
                    if store.is_stale_temp(file_path) and _remove(file_path):
                        pruned += 1
                    continue
                abs_path = os.path.abspath(file_path)
                if abs_path not in norm_refs and entry not in norm_refs:
                    try:
                        if os.path.isfile(file_path) or os.path.islink(file_path):
                            os.remove(file_path)
                            pruned += 1
                    except OSError as error:
                        log.debug('prune: %s', error)
        except OSError as error:
            log.debug('prune: %s', error)

    return pruned


# ---------------------------------------------------------------------------
# Internal extraction helpers
# ---------------------------------------------------------------------------


def _extract_year(attrs, raw_item):
    """Extract release year from Apple Music attributes or raw item."""
    rel_date = attrs.get('releaseDate') or raw_item.get('releaseDate')
    if rel_date and isinstance(rel_date, str) and len(rel_date) >= 4:
        try:
            return int(rel_date[:4])
        except ValueError:
            pass

    year = attrs.get('year') or raw_item.get('year')
    if year is not None:
        try:
            return int(year)
        except (ValueError, TypeError):
            pass

    # Playlists have no release date; fall back to when they were last
    # modified so the UI still has a year to show.
    mod_date = attrs.get('lastModifiedDate') or raw_item.get('lastModifiedDate')
    if mod_date and isinstance(mod_date, str) and len(mod_date) >= 4:
        try:
            return int(mod_date[:4])
        except ValueError:
            pass

    return None


def _extract_summary(attrs, raw_item):
    """Extract editorial notes or description as plain text."""
    ed = attrs.get('editorialNotes') or raw_item.get('editorialNotes')
    desc = attrs.get('description') or raw_item.get('description')
    summary = None

    if isinstance(ed, dict):
        summary = ed.get('standard') or ed.get('short') or ed.get('name')
    elif isinstance(ed, str):
        summary = ed
    elif isinstance(desc, dict):
        summary = desc.get('standard') or desc.get('short')
    elif isinstance(desc, str):
        summary = desc
    elif raw_item.get('summary'):
        summary = raw_item['summary']

    return strip_html(summary)


def _extract_genre(attrs, raw_item):
    """The first genre name, from the attributes or the raw item."""
    genre_names = attrs.get('genreNames') or raw_item.get('genreNames')
    if isinstance(genre_names, list) and genre_names:
        return genre_names[0]
    if isinstance(genre_names, str):
        return genre_names
    return raw_item.get('genre') or None


def _extract_artwork(attrs, raw_item, cache_dir, art_urls):
    """Extract the local cached cover path, its thumbnail's, and the hex
    background color; the files' URLs go into `art_urls`."""
    art = None
    thumb = None
    art_color = None

    if raw_item.get('art'):
        art = raw_item['art']
    if raw_item.get('thumb'):
        thumb = raw_item['thumb']
    if raw_item.get('artColor'):
        art_color = format_color(raw_item['artColor'])

    artwork = attrs.get('artwork') or raw_item.get('artwork')
    if isinstance(artwork, dict):
        url = artwork.get('url')
        if url and cache_dir:
            art, thumb = _register_artwork(url, cache_dir, art_urls)
        bg = artwork.get('bgColor')
        if bg and not art_color:
            art_color = format_color(bg)

    return art, thumb, art_color


def _register_artwork(url_template, cache_dir, art_urls):
    """Name the cover and thumbnail files for an artwork URL template and
    record in `art_urls` what each is fetched from."""
    cover, small = ART_SIZES['cover'], ART_SIZES['thumb']
    full = template_artwork_url(url_template, cover, cover)
    art = artwork_cache_path(full, cache_dir)
    thumb = thumb_cache_path(full, cache_dir)
    art_urls[art] = full
    art_urls[thumb] = template_artwork_url(url_template, small, small)
    return art, thumb


def _extract_catalog_id(attrs, raw_item, resource_type):
    """Extract catalog ID from a catalog or library resource."""
    item_id = str(raw_item.get('id') or '')
    if raw_item.get('type') == resource_type:
        return item_id if item_id else None

    if 'relationships' in raw_item:
        cat_data = raw_item['relationships'].get('catalog', {}).get('data', [])
        if cat_data and isinstance(cat_data, list) and len(cat_data) > 0:
            cid = cat_data[0].get('id')
            if cid:
                return str(cid)

    play_params = attrs.get('playParams') or {}
    if 'catalogId' in play_params:
        return str(play_params['catalogId'])

    if raw_item.get('catalogId'):
        return str(raw_item['catalogId'])

    # If item_id does not look like a library id (l.*, i.*, p.*, r.*), treat as catalog ID
    if item_id and not item_id.startswith(('l.', 'i.', 'p.', 'r.')):
        return item_id

    return None


# ---------------------------------------------------------------------------
# Item and Track Normalizers
# ---------------------------------------------------------------------------


def normalize_track(raw_track, index=0, cache_dir=None, art_urls=None):
    """Turn an Apple Music API track into the Track shape.

    Track = {
      "id": "...", "catalogId": "..." | null, "title": "...", "artist": "...",
      "album": "...", "trackNumber": int, "discNumber": int, "durationMs": int,
      "durationLabel": "3:36", "explicit": bool, "index": int,
      "thumb": "<cache>/thumb/<x>.jpg" | null,
      "type": "songs" | "library-songs" | "music-videos" | "library-music-videos" | ""
    }

    `type` is the API's, so a music video in a playlist is rated and added as one.

    `thumb` is named only when `cache_dir` is given: a playlist's rows show
    their own artwork, an album's tracks share the album's. `art_urls` is the
    artwork registry (see collect_art_urls).
    """
    attrs = raw_track.get('attributes') or {}
    track_id = str(raw_track.get('id') or '')
    thumb = raw_track.get('thumb') or None
    artwork = attrs.get('artwork')
    if cache_dir and isinstance(artwork, dict) and artwork.get('url'):
        _, thumb = _register_artwork(artwork['url'], cache_dir,
                                     {} if art_urls is None else art_urls)

    catalog_id = _extract_catalog_id(attrs, raw_track, 'songs')

    title = attrs.get('name') or raw_track.get('title') or ''
    artist = attrs.get('artistName') or attrs.get('artist') or raw_track.get('artist') or ''
    album = attrs.get('albumName') or attrs.get('album') or raw_track.get('album') or ''

    track_number = attrs.get('trackNumber')
    if track_number is None:
        track_number = raw_track.get('trackNumber', 1)
    disc_number = attrs.get('discNumber')
    if disc_number is None:
        disc_number = raw_track.get('discNumber', 1)
    duration_ms = attrs.get('durationInMillis')
    if duration_ms is None:
        duration_ms = raw_track.get('durationMs', 0)

    try:
        track_number = int(track_number)
    except (ValueError, TypeError):
        track_number = 1

    try:
        disc_number = int(disc_number)
    except (ValueError, TypeError):
        disc_number = 1

    try:
        duration_ms = int(duration_ms)
    except (ValueError, TypeError):
        duration_ms = 0

    duration_label = format_duration(duration_ms)

    content_rating = attrs.get('contentRating') or raw_track.get('contentRating')
    is_explicit = (
        content_rating == 'explicit'
        or attrs.get('explicit') is True
        or raw_track.get('explicit') is True
    )

    return {
        'id': track_id,
        'catalogId': catalog_id,
        'title': title,
        'artist': artist,
        'album': album,
        'trackNumber': track_number,
        'discNumber': disc_number,
        'durationMs': duration_ms,
        'durationLabel': duration_label,
        'explicit': is_explicit,
        'index': int(index),
        'thumb': thumb,
        'type': str(raw_track.get('type') or ''),
    }


def normalize_album(raw_album, cache_dir=None, tracks=None, art_urls=None):
    """Turn an Apple Music API album into the Item shape with kind='album'.

    `tracks` are the album's songs when the caller fetched them separately;
    otherwise the album's own `tracks` relationship is read. Tracks are
    grouped by disc: groups = [{"name": "Disc 1", "play": {...}, "entries": [...]}].
    """
    attrs = raw_album.get('attributes') or {}
    item_id = str(raw_album.get('id') or '')
    title = attrs.get('name') or raw_album.get('title') or ''
    subtitle = attrs.get('artistName') or raw_album.get('subtitle') or ''
    year = _extract_year(attrs, raw_album)
    genre = _extract_genre(attrs, raw_album)
    summary = _extract_summary(attrs, raw_album)
    art, thumb, art_color = _extract_artwork(attrs, raw_album, cache_dir,
                                             {} if art_urls is None else art_urls)
    catalog_id = _extract_catalog_id(attrs, raw_album, 'albums')
    url = attrs.get('url') or raw_album.get('url')

    # Resolve track list
    raw_tracks = tracks
    if raw_tracks is None:
        rel_tracks = raw_album.get('relationships', {}).get('tracks', {}).get('data')
        if isinstance(rel_tracks, list):
            raw_tracks = rel_tracks
        else:
            raw_tracks = []

    def track_sort_key(t):
        t_attrs = t.get('attributes') or {}
        d = t_attrs.get('discNumber')
        if d is None:
            d = t.get('discNumber', 1)
        tr = t_attrs.get('trackNumber')
        if tr is None:
            tr = t.get('trackNumber', 1)
        try:
            d_int = int(d)
        except (ValueError, TypeError):
            d_int = 1
        try:
            tr_int = int(tr)
        except (ValueError, TypeError):
            tr_int = 1
        return (d_int, tr_int)

    sorted_tracks = sorted(raw_tracks, key=track_sort_key)

    # One group per disc, the entries numbered on from the discs before: every
    # disc's group plays the whole album (setQueue({album, startWith}), which
    # MusicKit queues in disc and track order), so an entry's `index` is its
    # position in the album, not in its disc.
    raw_discs = {}
    for t in sorted_tracks:
        t_attrs = t.get('attributes') or {}
        d = t_attrs.get('discNumber')
        if d is None:
            d = t.get('discNumber', 1)
        try:
            d_int = int(d) if d is not None else 1
        except (ValueError, TypeError):
            d_int = 1
        raw_discs.setdefault(d_int, []).append(t)

    normalized_tracks = []
    groups = []
    for d in sorted(raw_discs.keys()):
        disc_label = f'Disc {d if d > 0 else 1}'
        disc_entries = [
            normalize_track(t, index=len(normalized_tracks) + idx)
            for idx, t in enumerate(raw_discs[d])
        ]
        normalized_tracks.extend(disc_entries)
        groups.append({
            'name': disc_label,
            'play': {'kind': 'album', 'id': item_id},
            'entries': disc_entries,
        })

    song_count = len(normalized_tracks)
    # No tracks in hand (a shelf item, normalised without its list): the
    # count alone, and no total time rather than a 0 that reads as empty.
    total_duration_ms = (sum(t['durationMs'] for t in normalized_tracks)
                         if normalized_tracks else None)
    if song_count == 0 and attrs.get('trackCount'):
        try:
            song_count = int(attrs['trackCount'])
        except (ValueError, TypeError):
            song_count = 0

    content_rating = attrs.get('contentRating') or raw_album.get('contentRating')
    is_explicit = (
        content_rating == 'explicit'
        or attrs.get('explicit') is True
        or raw_album.get('explicit') is True
        or any(t.get('explicit') for t in normalized_tracks)
    )

    return {
        'id': item_id,
        'kind': 'album',
        'title': title,
        'subtitle': subtitle,
        'year': year,
        'genre': genre,
        'summary': summary,
        'art': art,
        'thumb': thumb,
        'artColor': art_color,
        'trackCount': song_count,
        'durationMs': total_duration_ms,
        'explicit': is_explicit,
        'catalogId': catalog_id,
        'url': url,
        'play': {'kind': 'album', 'id': item_id},
        'groups': groups,
    }


def normalize_artist(raw_artist, cache_dir=None, albums=None, art_urls=None):
    """Turn an Apple Music API artist into the Item shape with kind='artist'.

    `albums` are the artist's albums (raw, or already normalized) when the
    caller fetched them; otherwise the `albums` relationship is read.
    subtitle '' (the data has no words: an artist is shown as one), groups=[one group per
    album].
    """
    attrs = raw_artist.get('attributes') or {}
    item_id = str(raw_artist.get('id') or '')
    title = attrs.get('name') or raw_artist.get('title') or ''
    subtitle = ''
    year = None
    genre = _extract_genre(attrs, raw_artist)
    summary = _extract_summary(attrs, raw_artist)
    art_urls = {} if art_urls is None else art_urls
    art, thumb, art_color = _extract_artwork(attrs, raw_artist, cache_dir, art_urls)
    catalog_id = _extract_catalog_id(attrs, raw_artist, 'artists')
    url = attrs.get('url') or raw_artist.get('url')

    raw_albums = albums
    if raw_albums is None:
        rel_albums = raw_artist.get('relationships', {}).get('albums', {}).get('data')
        if isinstance(rel_albums, list):
            raw_albums = rel_albums
        else:
            raw_albums = []

    groups = []
    has_explicit = False
    for alb in raw_albums:
        if alb.get('kind') == 'album' and 'groups' in alb and 'play' in alb:
            all_entries = []
            for g in alb.get('groups', []):
                all_entries.extend(g.get('entries', []))
            groups.append({
                'name': alb.get('title', ''),
                'play': alb.get('play', {'kind': 'album', 'id': alb.get('id')}),
                'entries': all_entries,
            })
            if alb.get('explicit'):
                has_explicit = True
        else:
            norm_alb = normalize_album(alb, cache_dir=cache_dir, art_urls=art_urls)
            all_entries = []
            for g in norm_alb.get('groups', []):
                all_entries.extend(g.get('entries', []))
            groups.append({
                'name': norm_alb.get('title', ''),
                'play': norm_alb.get('play', {'kind': 'album', 'id': norm_alb.get('id')}),
                'entries': all_entries,
            })
            if norm_alb.get('explicit'):
                has_explicit = True

    return {
        'id': item_id,
        'kind': 'artist',
        'title': title,
        'subtitle': subtitle,
        'year': year,
        'genre': genre,
        'summary': summary,
        'art': art,
        'thumb': thumb,
        'artColor': art_color,
        'albumCount': len(groups),
        'explicit': has_explicit,
        'catalogId': catalog_id,
        'url': url,
        'play': {'kind': 'artist', 'id': item_id},
        'groups': groups,
    }


def normalize_playlist(raw_playlist, cache_dir=None, tracks=None, art_urls=None):
    """Turn an Apple Music API playlist into the Item shape with kind='playlist'.

    `tracks` are the playlist's songs when the caller fetched them; otherwise
    the `tracks` relationship is read.
    groups=[{"name": "Tracks", "play": {"kind": "playlist", "id": item_id}, "entries": [...]}]
    """
    attrs = raw_playlist.get('attributes') or {}
    item_id = str(raw_playlist.get('id') or '')
    title = attrs.get('name') or raw_playlist.get('title') or ''
    subtitle = (
        attrs.get('curatorName')
        or attrs.get('artistName')
        or raw_playlist.get('subtitle')
        or ''  # the user's own playlists have no curator
    )
    year = _extract_year(attrs, raw_playlist)
    genre = _extract_genre(attrs, raw_playlist)
    summary = _extract_summary(attrs, raw_playlist)
    art_urls = {} if art_urls is None else art_urls
    art, thumb, art_color = _extract_artwork(attrs, raw_playlist, cache_dir, art_urls)
    catalog_id = _extract_catalog_id(attrs, raw_playlist, 'playlists')
    url = attrs.get('url') or raw_playlist.get('url')

    raw_tracks = tracks
    if raw_tracks is None:
        rel_tracks = raw_playlist.get('relationships', {}).get('tracks', {}).get('data')
        if isinstance(rel_tracks, list):
            raw_tracks = rel_tracks
        else:
            raw_tracks = []

    normalized_tracks = [
        normalize_track(t, index=idx, cache_dir=cache_dir, art_urls=art_urls)
        for idx, t in enumerate(raw_tracks)
    ]

    groups = [
        {
            'name': 'Tracks',
            'play': {'kind': 'playlist', 'id': item_id},
            'entries': normalized_tracks,
        }
    ]

    song_count = len(normalized_tracks)
    # No tracks in hand (a shelf item, normalised without its list): the
    # count alone, and no total time rather than a 0 that reads as empty.
    total_duration_ms = (sum(t['durationMs'] for t in normalized_tracks)
                         if normalized_tracks else None)
    if song_count == 0 and attrs.get('trackCount'):
        try:
            song_count = int(attrs['trackCount'])
        except (ValueError, TypeError):
            song_count = 0

    content_rating = attrs.get('contentRating') or raw_playlist.get('contentRating')
    is_explicit = (
        content_rating == 'explicit'
        or attrs.get('explicit') is True
        or raw_playlist.get('explicit') is True
        or any(t.get('explicit') for t in normalized_tracks)
    )

    return {
        'id': item_id,
        'kind': 'playlist',
        'title': title,
        'subtitle': subtitle,
        'year': year,
        'genre': genre,
        'summary': summary,
        'art': art,
        'thumb': thumb,
        'artColor': art_color,
        'trackCount': song_count,
        'durationMs': total_duration_ms,
        'explicit': is_explicit,
        'catalogId': catalog_id,
        'url': url,
        'play': {'kind': 'playlist', 'id': item_id},
        'groups': groups,
    }


def normalize_station(raw_station, cache_dir=None, art_urls=None):
    """Turn an Apple Music API station into the Item shape with kind='station'.

    groups=[]
    """
    attrs = raw_station.get('attributes') or {}
    item_id = str(raw_station.get('id') or '')
    title = attrs.get('name') or raw_station.get('title') or ''
    subtitle = (
        attrs.get('stationProviderName')
        or attrs.get('curatorName')
        or attrs.get('artistName')
        or raw_station.get('subtitle')
        or ''
    )
    year = None
    genre = _extract_genre(attrs, raw_station)
    summary = _extract_summary(attrs, raw_station)
    art, thumb, art_color = _extract_artwork(attrs, raw_station, cache_dir,
                                             {} if art_urls is None else art_urls)
    catalog_id = _extract_catalog_id(attrs, raw_station, 'stations')
    url = attrs.get('url') or raw_station.get('url')

    content_rating = attrs.get('contentRating') or raw_station.get('contentRating')
    is_explicit = (
        content_rating == 'explicit'
        or attrs.get('explicit') is True
        or raw_station.get('explicit') is True
    )

    return {
        'id': item_id,
        'kind': 'station',
        'title': title,
        'subtitle': subtitle,
        'year': year,
        'genre': genre,
        'summary': summary,
        'art': art,
        'thumb': thumb,
        'artColor': art_color,
        'explicit': is_explicit,
        'catalogId': catalog_id,
        'url': url,
        'play': {'kind': 'station', 'id': item_id},
        'groups': [],
    }


def normalize_song_as_item(raw_song, cache_dir=None, art_urls=None):
    """Normalize an Apple Music song into the Item shape (e.g. for search results)."""
    attrs = raw_song.get('attributes') or {}
    item_id = str(raw_song.get('id') or '')
    catalog_id = _extract_catalog_id(attrs, raw_song, 'songs')

    art, thumb, art_color = _extract_artwork(attrs, raw_song, cache_dir,
                                             {} if art_urls is None else art_urls)
    duration_ms = attrs.get('durationInMillis')
    if duration_ms is None:
        duration_ms = raw_song.get('durationMs', 0)
    try:
        duration_ms = int(duration_ms)
    except (ValueError, TypeError):
        duration_ms = 0

    year = _extract_year(attrs, raw_song)
    genre = _extract_genre(attrs, raw_song)

    return {
        'id': item_id,
        'kind': 'song',
        'title': attrs.get('name') or raw_song.get('title') or '',
        'subtitle': attrs.get('artistName') or raw_song.get('artist') or '',
        'year': year,
        'genre': genre,
        'summary': None,
        'art': art,
        'thumb': thumb,
        'artColor': art_color,
        'durationMs': duration_ms,
        'explicit': attrs.get('contentRating') == 'explicit' or attrs.get('explicit') is True,
        'catalogId': catalog_id,
        'url': attrs.get('url') or raw_song.get('url'),
        'play': {'kind': 'song', 'id': item_id},
        'groups': [],
    }


def normalize_item(raw_item, cache_dir=None, include_groups=True, art_urls=None):
    """Normalize any Apple Music resource into an Item (`art_urls`: the artwork
    registry, see collect_art_urls)."""
    raw_type = str(raw_item.get('type', ''))
    raw_kind = str(raw_item.get('kind', ''))

    if raw_type in ('albums', 'library-albums') or raw_kind == 'album':
        item = normalize_album(raw_item, cache_dir=cache_dir, art_urls=art_urls)
    elif raw_type in ('playlists', 'library-playlists') or raw_kind == 'playlist':
        item = normalize_playlist(raw_item, cache_dir=cache_dir, art_urls=art_urls)
    elif raw_type in ('artists', 'library-artists') or raw_kind == 'artist':
        item = normalize_artist(raw_item, cache_dir=cache_dir, art_urls=art_urls)
    elif raw_type in ('stations', 'radio-stations', 'apple-curators') or raw_kind == 'station':
        item = normalize_station(raw_item, cache_dir=cache_dir, art_urls=art_urls)
    elif raw_type in ('songs', 'library-songs') or raw_kind == 'song':
        item = normalize_song_as_item(raw_item, cache_dir=cache_dir, art_urls=art_urls)
    elif raw_type in ('music-videos', 'library-music-videos') or raw_kind == 'video':
        # A music video is a song with a picture: the same fields, played
        # as MusicKit's own `musicVideo` queue kind.
        item = normalize_song_as_item(raw_item, cache_dir=cache_dir, art_urls=art_urls)
        item['kind'] = 'video'
        item['play'] = {'kind': 'musicVideo', 'id': item['id']}
    else:
        attrs = raw_item.get('attributes') or {}
        if 'trackCount' in attrs or 'artistName' in attrs:
            item = normalize_album(raw_item, cache_dir=cache_dir, art_urls=art_urls)
        else:
            # Something the app has no page or queue for (an editorial item, an uploaded
            # video): its name and artwork, and nothing to play.
            item = normalize_station(raw_item, cache_dir=cache_dir, art_urls=art_urls)
            item['kind'] = 'unknown'
            item['play'] = {}

    if not include_groups:
        item['groups'] = []
    return item


# The shelves a search can answer with, as MusicKit names them, in the order they take when
# the answer does not say (Apple's own for a search of this kind, as its `meta.results.order`
# has it). Each is keyed by its kind without the "library-" (`top` for `topResults`, the
# catalog's own pick of its best few hits across every kind, asked for `with=topResults`,
# which Apple Music's own search page puts first) and untitled: the Search page has the
# words.
SEARCH_SHELF_ORDER = [
    'topResults', 'artists', 'library-artists', 'songs', 'library-songs',
    'albums', 'library-albums', 'playlists', 'library-playlists', 'music-videos', 'stations',
]


def search_results(raw, cache_dir):
    """Engine.search()'s answer from MusicKit's: `shelves`, one per kind that
    answered, each `{key, title: '', items}` (key `top`, `artists`,
    `albums`, `songs`, `playlists`, `music-videos` or `stations`), in the
    order Apple's own search page shows them (`meta.results.order`, or
    SEARCH_SHELF_ORDER without it) with the top results first.

    A search never waits on a download, so a hit's `art` is its cached
    cover when the sync has fetched it and a small catalog URL otherwise,
    which the Search page fetches on its own; its `thumb` only ever names a
    file that is on disk."""
    results = (raw or {}).get('results') or {}
    order = ((raw or {}).get('meta') or {}).get('results', {}).get('order')
    if not isinstance(order, list):
        order = []
    keys = [k for k in order if k in SEARCH_SHELF_ORDER]
    keys += [k for k in SEARCH_SHELF_ORDER if k not in keys]
    shelves = []
    for key in keys:
        section = results.get(key)
        if not isinstance(section, dict):
            continue
        hits = []
        for raw_item in section.get('data') or []:
            if not isinstance(raw_item, dict):
                continue
            item = normalize_item(raw_item, cache_dir, include_groups=False)
            _settle_search_art(item, raw_item)
            hits.append(item)
        if hits:
            shelf_key = 'top' if key == 'topResults' else key.removeprefix('library-')
            shelves.append({'key': shelf_key, 'title': '', 'items': hits})
    return {'shelves': shelves}


def search_suggestions(raw, cache_dir):
    """Engine.suggest()'s answer from MusicKit's `search/suggestions`:
    `terms`, the few searches Apple would complete the typed one to, each
    `{term, display}` — `term` what to search for, `display` as Apple
    shows it — with no repeats; and `items`, its best few hits for what is
    typed so far, as `search_results` has its hits (no groups, art as it
    stands)."""
    suggestions = ((raw or {}).get('results') or {}).get('suggestions') or []
    terms = []
    items = []
    seen_terms = set()
    seen_items = set()
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        kind = suggestion.get('kind')
        if kind == 'terms':
            term = str(suggestion.get('searchTerm') or suggestion.get('displayTerm') or '').strip()
            if not term or term.lower() in seen_terms:
                continue
            seen_terms.add(term.lower())
            display = str(suggestion.get('displayTerm') or term).strip()
            terms.append({'term': term, 'display': display})
        elif kind == 'topResults':
            content = suggestion.get('content')
            if not isinstance(content, dict):
                continue
            item = normalize_item(content, cache_dir, include_groups=False)
            _settle_search_art(item, content)
            if (item['kind'], item['id']) in seen_items:
                continue
            seen_items.add((item['kind'], item['id']))
            items.append(item)
    return {'terms': terms, 'items': items}


def search_landing(raw, cache_dir):
    """Engine.landing()'s answer from Apple's search-landing recommendations:
    `categories`, the rooms Apple Music's own search page offers to browse
    before anything is typed (Rock, Hip-Hop, Chill, the decades…), in
    Apple's order across every recommendation in the set, each
    `{id, kind: "category", title, subtitle, art, artColor, url}` — `title`
    the short name on the tile ("Rock"), `subtitle` the curator's own
    ("Apple Music Rock"), `art` a small catalog URL the Search page fetches
    on its own, `artColor` the tile's colour behind it. Only Apple's curators
    are categories: an editorial item in the set is a banner with nothing
    behind it, and is left out."""
    categories = []
    seen = set()
    for rec in (raw or {}).get('data') or []:
        if not isinstance(rec, dict):
            continue
        contents = ((rec.get('relationships') or {}).get('contents') or {}).get('data') or []
        for content in contents:
            if not isinstance(content, dict) or content.get('type') != 'apple-curators':
                continue
            category = normalize_category(content)
            if not category or category['id'] in seen:
                continue
            seen.add(category['id'])
            categories.append(category)
    return {'categories': categories}


def normalize_category(raw):
    """An Apple curator as a category tile. None without a name."""
    attrs = raw.get('attributes') or {}
    item_id = str(raw.get('id') or '')
    name = str(attrs.get('name') or '').strip()
    short = str(attrs.get('shortName') or '').strip()
    if not item_id or not (name or short):
        return None
    artwork = attrs.get('artwork') if isinstance(attrs.get('artwork'), dict) else {}
    url = artwork.get('url')
    return {
        'id': item_id,
        'kind': 'category',
        'title': short or name,
        'subtitle': name if name and name != (short or name) else None,
        'art': template_artwork_url(url, CATEGORY_ART_SIZE, CATEGORY_ART_SIZE) if url else None,
        'artColor': format_color(artwork.get('bgColor')) if artwork.get('bgColor') else None,
        'url': attrs.get('url'),
    }


# A category tile's picture, wide but not big: the Search page draws it cropped
# over the tile's colour.
CATEGORY_ART_SIZE = 320


def category_page(raw, cache_dir):
    """Engine.category()'s answer from a curator with its grouping: the
    category's `id` and `title`, and its `shelves` — the grouping's one
    tab's editorial elements, each `{key, title, items}` in Apple's order,
    items normalised as a search hit is (no groups, art as it stands);
    an element with no title or nothing in it is left out."""
    data = (raw or {}).get('data') or []
    curator = data[0] if data and isinstance(data[0], dict) else {}
    attrs = curator.get('attributes') or {}
    title = str(attrs.get('shortName') or attrs.get('name') or '').strip()
    shelves = []
    groupings = ((curator.get('relationships') or {}).get('grouping') or {}).get('data') or []
    for grouping in groupings:
        if isinstance(grouping, dict):
            shelves.extend(_grouping_shelves(grouping, cache_dir, 'cat'))
    return {'id': str(curator.get('id') or ''), 'title': title, 'shelves': shelves}


# The resource types an editorial shelf may hold, and the app can show; what
# else Apple puts on a page (uploaded videos, marketing items) is left out.
SHELF_RESOURCE_TYPES = {
    'albums', 'playlists', 'artists', 'stations', 'songs', 'music-videos',
    'library-albums', 'library-playlists', 'library-artists', 'library-songs',
    'library-music-videos',
}


def _shelf_item(raw_item, cache_dir, settle=True, curators=True, art_urls=None):
    """An editorial element's or a recommendation's content as a shelf item:
    an Apple curator as a category tile (its page is its grouping, as on the
    search page; None without `curators`), a known resource with a name
    without its groups (its art as a search hit's with `settle`, or the
    files a sync fetches, registered in `art_urls`); anything else None."""
    if not isinstance(raw_item, dict):
        return None
    if raw_item.get('type') == 'apple-curators':
        return normalize_category(raw_item) if curators else None
    if raw_item.get('type') not in SHELF_RESOURCE_TYPES:
        return None
    if not (raw_item.get('attributes') or {}).get('name'):
        return None
    item = normalize_item(raw_item, cache_dir, include_groups=False, art_urls=art_urls)
    if settle:
        _settle_search_art(item, raw_item)
    return item


def _element_contents(element):
    """What an editorial element holds: its contents, or, for one made of
    other elements (the banners at the top of the New page, each with one
    item), the contents of each of those in turn."""
    rel = element.get('relationships') or {}
    contents = (rel.get('contents') or {}).get('data') or []
    if contents:
        return [c for c in contents if isinstance(c, dict)]
    out = []
    for child in (rel.get('children') or {}).get('data') or []:
        if isinstance(child, dict):
            child_rel = child.get('relationships') or {}
            child_contents = (child_rel.get('contents') or {}).get('data') or []
            out.extend(c for c in child_contents if isinstance(c, dict))
    return out


def _grouping_shelves(grouping, cache_dir, prefix, featured=False):
    """The shelves of a grouping (a curator's, or the editorial one behind
    the New page): each editorial element of its tabs that has a title and
    something in it, as `{key, title, items}` in Apple's order, keyed
    `<prefix>-<element id>`. With `featured`, the first untitled element
    made of other elements (the banners at the top of the New page) becomes
    a shelf too, untitled and marked `featured: true`; untitled elements are
    otherwise left out, as are link rows and empty ones."""
    shelves = []
    tabs = ((grouping.get('relationships') or {}).get('tabs') or {}).get('data') or []
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        children = ((tab.get('relationships') or {}).get('children') or {}).get('data') or []
        for index, element in enumerate(children):
            if not isinstance(element, dict):
                continue
            element_attrs = element.get('attributes') or {}
            shelf_title = str(element_attrs.get('title') or element_attrs.get('name') or '').strip()
            own = ((element.get('relationships') or {}).get('contents') or {}).get('data') or []
            is_featured = not shelf_title
            if is_featured and (own or not featured):
                continue
            items = []
            seen = set()
            for raw_item in _element_contents(element):
                item = _shelf_item(raw_item, cache_dir)
                if item is None or (item['kind'], item['id']) in seen:
                    continue
                seen.add((item['kind'], item['id']))
                items.append(item)
            if items:
                shelf = {'key': f"{prefix}-{element.get('id') or index}", 'title': shelf_title,
                         'items': items}
                if is_featured:
                    shelf['featured'] = True
                    featured = False  # one featured shelf
                shelves.append(shelf)
    return shelves


def editorial_shelves(raw, cache_dir):
    """`browse`'s answer from the editorial groupings behind Apple Music's
    own New page (`/v1/editorial/<storefront>/groupings` with `name=music`,
    `platform=web`): `shelves`, the grouping's editorial elements in
    Apple's order — the featured banners first as one shelf marked
    `featured` (and untitled: the page names it), then Best New Songs, New
    Releases, playlists, stations, videos… — each `{key, title, items}` with
    items as `search` has them (no groups, art as it stands; a curator among
    them a category tile)."""
    shelves = []
    for grouping in (raw or {}).get('data') or []:
        if isinstance(grouping, dict):
            shelves.extend(_grouping_shelves(grouping, cache_dir, 'new', featured=True))
    return {'shelves': shelves}


def _is_made_for_you(raw_item):
    """A personal mix (Favourites Mix, Chill Mix, New Music Mix…, playlists
    of `playlistType` personal-mix) or a station: what Made for You holds."""
    if not isinstance(raw_item, dict):
        return False
    if raw_item.get('type') == 'stations':
        return True
    attrs = raw_item.get('attributes') or {}
    return raw_item.get('type') == 'playlists' and attrs.get('playlistType') == 'personal-mix'


def made_for_you_shelves(raw_recs, cache_dir=None):
    """The Made for You page from `/v1/me/recommendations`: the
    recommendations (a group's members each on their own, as
    recommendation_shelves has them) made up entirely of the personal mixes
    and stations, each a shelf titled as Apple titles it, in Apple's order;
    the rest of the recommendations (albums for you, recently played) are
    Home's, not this page's. Items as a shelf's, without their track lists.

    Shelf = {"key": "rec-<id>", "title": "…", "items": [Item]}
    """
    return _recommendation_shelves(
        raw_recs, cache_dir, accept=lambda contents: all(map(_is_made_for_you, contents)),
        settle=True)


def _recommendation_shelves(raw_recs, cache_dir, accept, settle, art_urls=None):
    """The shelves of Apple's recommendations, in Apple's order: one per
    recommendation whose contents `accept(contents)` takes, a group's members
    each a shelf of their own, its items through _shelf_item (no curators:
    a category tile is the Search page's) with `settle`; a recommendation
    with nothing in it is left out. Titled as Apple titles it (in the
    account's language), '' when it does not: the page has the words."""
    shelves = []

    def walk(rec):
        if not isinstance(rec, dict):
            return
        attrs = rec.get('attributes') or {}
        rel = rec.get('relationships') or {}
        members = (rel.get('recommendations') or {}).get('data') or []
        if members:
            for member in members:
                walk(member)
            return
        contents = [c for c in (rel.get('contents') or {}).get('data') or [] if isinstance(c, dict)]
        if not contents or not accept(contents):
            return
        items = [_shelf_item(raw_item, cache_dir, settle=settle, curators=False,
                             art_urls=art_urls) for raw_item in contents]
        items = [item for item in items if item is not None]
        if not items:
            return
        title = attrs.get('title') or {}
        title = title.get('stringForDisplay') if isinstance(title, dict) else str(title)
        key = rec.get('id') or len(shelves)
        shelves.append({'key': f'rec-{key}', 'title': title or '', 'items': items})

    for rec in raw_recs or []:
        walk(rec)
    return shelves


def _settle_search_art(item, raw_item):
    """A search hit's artwork as it stands: the sync's files where they
    exist, a thumbnail-sized catalog URL for the cover otherwise, and no thumb
    at all rather than the name of one that was never fetched."""
    if not (item.get('thumb') and os.path.exists(item['thumb'])):
        item['thumb'] = None
    if not (item.get('art') and os.path.exists(item['art'])):
        url = ((raw_item.get('attributes') or {}).get('artwork') or {}).get('url')
        small = ART_SIZES['thumb']
        item['art'] = template_artwork_url(url, small, small) if url else None


def recommendation_shelves(raw_recs, cache_dir=None, art_urls=None):
    """Apple's home page as shelves: one per recommendation, in the order
    the API sends them, titled as Apple titles it ("New Releases for You",
    "Stations for You", "More from …", a genre, a decade). A group
    recommendation is its members, each a shelf of its own. Items are
    normalised without their track lists, as a shelf's are, their artwork
    the files the sync fetches (registered in `art_urls`); what the app has
    no tile for (a curator, an editorial item) is left out, as is a
    recommendation with nothing in it.

    Shelf = {"key": "rec-<id>", "title": "…", "items": [Item]}
    """
    return _recommendation_shelves(raw_recs, cache_dir, accept=lambda contents: True,
                                   settle=False, art_urls=art_urls)


def group_songs_into_albums_and_artists(songs, cache_dir=None, art_urls=None):
    """Group songs from /v1/me/library/songs?include=albums into albums and artists."""
    albums_map = {}
    for s in songs:
        s_attrs = s.get('attributes') or {}
        # A missing name stays '': the app has the words for it (library.unknown_title).
        album_name = s_attrs.get('albumName') or ''
        artist_name = s_attrs.get('artistName') or ''

        rel_albums = s.get('relationships', {}).get('albums', {}).get('data', [])
        if rel_albums:
            alb_rel = rel_albums[0]
            alb_id = alb_rel.get('id')
            # A song can point at a library album that no longer answers
            # (the relationship survives the album resource): a stub with
            # no attributes. The song itself knows the album's name, artist
            # and artwork, so the stub is filled in from it rather than
            # becoming a nameless tile.
            alb_attrs = dict(alb_rel.get('attributes') or {})
            if not alb_attrs.get('name'):
                alb_attrs.setdefault('name', album_name)
                alb_attrs.setdefault('artistName', artist_name)
                alb_attrs.setdefault('artwork', s_attrs.get('artwork'))
                alb_attrs.setdefault('releaseDate', s_attrs.get('releaseDate'))
                alb_attrs.setdefault('genreNames', s_attrs.get('genreNames', []))
                alb_attrs.setdefault('contentRating', s_attrs.get('contentRating'))
            alb_obj = dict(alb_rel, attributes=alb_attrs)
        else:
            alb_id = f"l.alb_{hashlib.md5(f'{album_name}:{artist_name}'.encode()).hexdigest()[:12]}"
            alb_obj = {
                'id': alb_id,
                'type': 'library-albums',
                'attributes': {
                    'name': album_name,
                    'artistName': artist_name,
                    'artwork': s_attrs.get('artwork'),
                    'releaseDate': s_attrs.get('releaseDate'),
                    'genreNames': s_attrs.get('genreNames', []),
                    'contentRating': s_attrs.get('contentRating'),
                },
            }

        if alb_id not in albums_map:
            albums_map[alb_id] = {
                'album': alb_obj,
                'songs': [],
            }
        albums_map[alb_id]['songs'].append(s)

    albums_list = []
    artists_map = {}
    for entry in albums_map.values():
        alb_norm = normalize_album(entry['album'], cache_dir=cache_dir, tracks=entry['songs'],
                                   art_urls=art_urls)
        albums_list.append(alb_norm)

        art_name = alb_norm['subtitle']
        artists_map.setdefault(art_name, []).append(alb_norm)

    albums_list.sort(key=lambda a: a.get('title', '').lower())

    artists_list = []
    for art_name, art_albums in sorted(artists_map.items(), key=lambda x: x[0].lower()):
        art_id = f"l.art_{hashlib.md5(art_name.encode('utf-8')).hexdigest()[:12]}"
        first_art = art_albums[0]['art'] if art_albums and art_albums[0].get('art') else None
        first_thumb = (art_albums[0].get('thumb')
                       if art_albums and art_albums[0].get('art') else None)
        first_color = art_albums[0].get('artColor') if art_albums else None

        art_obj = {
            'id': art_id,
            'type': 'library-artists',
            'attributes': {
                'name': art_name,
                'genreNames': ([art_albums[0].get('genre')]
                               if art_albums and art_albums[0].get('genre') else []),
            },
        }
        artist_norm = normalize_artist(art_obj, cache_dir=cache_dir, albums=art_albums,
                                       art_urls=art_urls)
        # A library artist carries no artwork of its own: it takes its first
        # album's — the thumbnail with the cover, or the tile would decode
        # the full cover for want of one.
        if not artist_norm['art']:
            artist_norm['art'] = first_art
            artist_norm['thumb'] = first_thumb
            artist_norm['artColor'] = first_color
        artists_list.append(artist_norm)

    return albums_list, artists_list


def save_library(library_data, cache_dir, indent=2, generation=None):
    """Write library.json atomically and durably (store.atomic_write: a temporary file,
    fsync'd, renamed over the old one), so a reader sees the old file or the new one, never
    half of one, even after a crash. `indent` is json.dump's: None writes the compact form. A
    failure raises, and leaves the old file and no temporary one; store.CacheGone when the
    cache was cleared since `generation` (nothing written)."""
    lib_path = os.path.join(cache_dir, 'library.json')
    written = store.atomic_write(lib_path, lambda f: json.dump(library_data, f, indent=indent),
                                 root=cache_dir, text=True, generation=generation)
    if written is None:
        raise store.CacheGone(cache_dir)


# ---------------------------------------------------------------------------
# The other caches: artwork the pages fetch on their own, and answers kept
# ---------------------------------------------------------------------------


def prune_remote_art(cache_dir, max_bytes=REMOTE_ART_BUDGET):
    """Trim <cache_dir>/remote-art/ — the covers the pages fetch themselves
    (a search hit's, the player's, a category's picture) — to `max_bytes`,
    keeping the newest by mtime. Nothing else ever removes them. Returns
    how many went."""
    folder = os.path.join(cache_dir, 'remote-art')
    pruned = 0
    entries = []
    try:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name.startswith('.'):  # a download in progress, or a crash's leftover
                if store.is_stale_temp(path) and _remove(path):
                    pruned += 1
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if os.path.isfile(path):
                entries.append((st.st_mtime, st.st_size, path))
    except FileNotFoundError:
        return 0
    except OSError as error:
        log.debug('remote art: %s', error)
        return pruned
    entries.sort(reverse=True)
    kept = 0
    for _mtime, size, path in entries:
        if kept + size <= max_bytes:
            kept += size
            continue
        try:
            os.remove(path)
            pruned += 1
        except OSError as error:
            log.debug('remote art: %s', error)
    return pruned


def prune_caches(cache_dir, now=None, remote_bytes=REMOTE_ART_BUDGET, lyrics_keep=LYRICS_KEEP,
                 answer_age=ANSWER_MAX_AGE):
    """Trim the caches nothing else trims (in a thread, at startup and after a sync):
    remote-art/ to `remote_bytes` (prune_remote_art), lyrics/ to the
    `lyrics_keep` played last (by mtime: a cache hit touches the file), the kept answers
    (categories/, landing, New, Made for You) stamped longer than `answer_age` ago (by mtime,
    when they were written), an items/ folder older versions kept, and the temporary files
    of writes a crash cut short (store.is_stale_temp) in the cache and its folders. Returns
    {what: how many went}. art/ and thumb/ are the library's: prune_art keeps them."""
    now = time.time() if now is None else now
    gone = {'remote-art': prune_remote_art(cache_dir, remote_bytes), 'lyrics': 0,
            'answers': 0, 'items': 0, 'temps': 0}

    def files(folder):
        try:
            with os.scandir(folder) as entries:
                return [entry for entry in entries if entry.is_file(follow_symlinks=False)]
        except FileNotFoundError:
            return []
        except OSError as error:
            log.debug('prune: %s', error)
            return []

    for folder in (cache_dir, *(os.path.join(cache_dir, name) for name in
                                ('art', 'thumb', 'remote-art', 'lyrics', 'categories'))):
        for entry in files(folder):
            if store.is_stale_temp(entry.path, now) and _remove(entry.path):
                gone['temps'] += 1

    lyrics = [entry for entry in files(os.path.join(cache_dir, 'lyrics'))
              if not store.is_temp(entry.name)]
    lyrics.sort(key=_mtime, reverse=True)
    for entry in lyrics[max(0, lyrics_keep):]:
        if _remove(entry.path):
            gone['lyrics'] += 1

    answers = [entry for entry in files(os.path.join(cache_dir, 'categories'))
               if not store.is_temp(entry.name)]
    answers += [entry for entry in files(cache_dir) if entry.name in KEPT_ANSWERS]
    for entry in answers:
        if now - _mtime(entry) > answer_age and _remove(entry.path):
            gone['answers'] += 1

    items = os.path.join(cache_dir, 'items')
    if os.path.isdir(items) and not os.path.islink(items):
        import shutil
        try:
            gone['items'] = sum(len(names) for _root, _dirs, names in os.walk(items))
            shutil.rmtree(items)
        except OSError as error:
            log.debug('prune: %s', error)
    if any(gone.values()):
        log.info('caches pruned: %s', ', '.join(f'{count} {what}'
                                               for what, count in gone.items() if count))
    return gone


def _mtime(entry):
    try:
        return entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return 0


def _remove(path):
    """Delete a file the cache no longer wants; one already gone is fine. True if deleted."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        log.debug('prune: %s', error)
        return False


def _safe_id(value):
    return re.sub(r'[^A-Za-z0-9._-]', '_', str(value or ''))


def landing_cache_path(cache_dir):
    return os.path.join(cache_dir, 'landing.json')


def category_cache_path(cache_dir, category_id):
    return os.path.join(cache_dir, 'categories', f'{_safe_id(category_id)}.json')


def browse_cache_path(cache_dir):
    """Where the New page's shelves (editorial_shelves) are kept for a day."""
    return os.path.join(cache_dir, 'browse.json')


def made_for_you_cache_path(cache_dir):
    """Where the Made for You shelves (made_for_you_shelves) are kept for a day."""
    return os.path.join(cache_dir, 'made-for-you.json')


def write_answer(path, answer, cache_dir, generation=None):
    """Keep `answer` at `path` (in the cache at `cache_dir`), atomically,
    stamped `cached` with when; not once the cache's generation has moved from
    `generation` (store.py). Best effort: a cache that cannot be written is
    only a cache."""
    answer = dict(answer)
    answer['cached'] = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        store.atomic_write(path, lambda f: json.dump(answer, f), root=cache_dir, text=True,
                           generation=generation)
    except (OSError, ValueError) as error:
        log.warning('could not keep %s: %s', path, error)
    return answer


def read_answer(path, max_age_seconds):
    """The answer kept at `path`, or None when there is none, it cannot be
    read, or it was stamped longer than `max_age_seconds` ago."""
    try:
        with open(path, encoding='utf-8') as f:
            answer = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        log.debug('kept answer %s: %s', path, error)
        return None
    stamp = answer.get('cached') if isinstance(answer, dict) else None
    try:
        when = datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
    except (TypeError, ValueError):
        log.debug('kept answer %s: no stamp', path)
        return None
    if (datetime.now(UTC) - when).total_seconds() > max_age_seconds:
        return None
    return answer
