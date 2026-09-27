"""Library synchronization, Apple Music API normalization, and artwork caching.

Transforms Apple Music API JSON into the Item and Track shapes README.md
describes, manages artwork caching and pruning, and builds library.json.
Python standard library only.
"""

from datetime import datetime, timezone
import hashlib
import html
import json
import os
import re

from . import config

# Only what normalising an answer needs is imported here. This module is
# loaded by every am.py command — a search, the player's half-minute poll —
# and PyGObject alone is fifty milliseconds of a run that should take ten;
# the thread pool, the fetch and the lock are imported where they are used.

# Scaling a cached cover down to its thumbnail, without fetching it again,
# takes an image library, and nothing in the backend imports gi. The app
# installs one here: a callable scale_image(src_path, dest_path, size) that
# writes a JPEG at most size x size to dest_path and raises on failure
# (GdkPixbuf, in the app). Without one, thumbnails are downloaded instead.
scale_image = None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_duration(duration_ms: int | None) -> str:
    """Format milliseconds into 'm:ss' or 'h:mm:ss'.

    e.g. 216000 -> '3:36', 3661000 -> '1:01:01', 0 -> '0:00'.
    """
    if duration_ms is None or duration_ms <= 0:
        return "0:00"

    total_seconds = round(duration_ms / 1000)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_count_label(song_count: int | None, total_duration_ms: int | None = None) -> str:
    """Format song count and total duration into a count label.

    e.g. '1 song' (no duration given), '12 songs, 43 min', '20 songs, 1 hr 15 min'.
    """
    count = max(0, int(song_count)) if song_count is not None else 0

    song_word = "song" if count == 1 else "songs"
    songs_part = f"{count} {song_word}"

    if total_duration_ms is None:
        return songs_part

    duration = max(0, int(total_duration_ms))

    if duration == 0:
        dur_part = "0 min"
    else:
        total_minutes = round(duration / 60000)
        if total_minutes == 0:
            total_minutes = 1

        hours = total_minutes // 60
        minutes = total_minutes % 60

        if hours > 0:
            if minutes > 0:
                dur_part = f"{hours} hr {minutes} min"
            else:
                dur_part = f"{hours} hr"
        else:
            dur_part = f"{minutes} min"

    return f"{songs_part}, {dur_part}"


def format_color(bg_color: str | None) -> str | None:
    """Normalize hex color string to #rrggbb or None."""
    if not bg_color:
        return None
    bg_color = bg_color.strip()
    if not bg_color:
        return None
    if not bg_color.startswith("#"):
        return f"#{bg_color}"
    return bg_color


def strip_html(text: str | None) -> str | None:
    """Convert HTML snippet to plain text or None."""
    if not text:
        return None
    cleaned = re.sub(r"<[^>]+>", "", text)
    cleaned = html.unescape(cleaned).strip()
    return cleaned if cleaned else None


# ---------------------------------------------------------------------------
# Artwork templating, caching, and pruning
# ---------------------------------------------------------------------------


def template_artwork_url(url_template: str, width: int | None = None, height: int | None = None) -> str:
    """Format an Apple Music artwork URL template by replacing dimensions and formats.

    Replaces {w} with width, {h} with height (the cover's size unless given).
    Also {f} with 'jpg', {c} with 'bb' if present.
    """
    if not url_template:
        return ""
    width = width or ART_SIZES["cover"]
    height = height or ART_SIZES["cover"]
    return (
        url_template.replace("{w}x{h}", f"{width}x{height}")
        .replace("{w}", str(width))
        .replace("{h}", str(height))
        .replace("{f}", "jpg")
        .replace("{c}", "bb")
    )


def format_artwork_url(artwork_obj, width: int | None = None, height: int | None = None) -> str | None:
    """Convenience wrapper for dict artwork object or string template."""
    if not artwork_obj:
        return None
    if isinstance(artwork_obj, dict):
        url = artwork_obj.get("url")
    else:
        url = str(artwork_obj)
    if not url:
        return None
    return template_artwork_url(url, width, height)


def artwork_filename(url: str) -> str:
    """Return the SHA-1 cache filename for an artwork URL."""
    if not url:
        return ""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return f"{digest}.jpg"


def artwork_cache_path(url: str, cache_dir: str) -> str:
    """Return the absolute path in the cache directory for an artwork URL."""
    return os.path.join(cache_dir, "art", artwork_filename(url))


# The two sizes artwork is kept at, in pixels. The cover is the hero in the
# detail pane; the thumbnail is the copy the tiles and the track rows draw —
# a cover is drawn at around a hundred logical pixels on a tile and a few
# dozen on a row, and the shell decodes a background image whole, on the
# compositor thread, the first time a tile is painted, so a page of tiles
# costs a page of decodes at this size. Named after the same URL as the
# full-size file, so <cache>/thumb/<x>.jpg is the thumbnail of
# <cache>/art/<x>.jpg. These are the defaults, from config; a sync sets
# them with apply_art_sizes, and the marker file beside the covers tells
# every other command what a cache was built at.
DEFAULT_ART_SIZES = {"cover": config.COVER_SIZE, "thumb": config.THUMB_SIZE}
ART_SIZES = dict(DEFAULT_ART_SIZES)
ART_SIZE_LIMITS = {"cover": (256, 1024), "thumb": (96, 512)}


def _art_sizes_marker(cache_dir: str) -> str:
    return os.path.join(cache_dir, "art", ".sizes")


def _read_art_sizes_marker(cache_dir: str) -> dict:
    """What the cache was built at, or the defaults where nothing says."""
    sizes = dict(DEFAULT_ART_SIZES)
    try:
        with open(_art_sizes_marker(cache_dir), "r", encoding="utf-8") as f:
            marker = json.load(f)
        for key in sizes:
            if isinstance(marker.get(key), int) and marker[key] > 0:
                sizes[key] = marker[key]
    except Exception:
        pass
    return sizes


def load_art_sizes(cache_dir: str) -> dict:
    """The sizes the cache at `cache_dir` was built at, from its marker,
    taken as this process's ART_SIZES so every URL it names agrees with
    the files on disk. A tiny file read, nothing more."""
    ART_SIZES.update(_read_art_sizes_marker(cache_dir))
    return dict(ART_SIZES)


def apply_art_sizes(cache_dir: str, cover, thumb) -> dict:
    """Set the sizes a sync builds at, and record them in the marker. A
    thumbnail size that differs from the marker's wipes <cache>/thumb/:
    the files keep their names whatever the size, so nothing else would
    tell a stale one from a right one, and they are rebuilt from the
    covers without a fetch. A cover size that differs needs nothing: the
    new size is a new URL, hence a new name, and the old files go with
    the next prune as unreferenced."""
    wanted = {}
    for key, value in (("cover", cover), ("thumb", thumb)):
        low, high = ART_SIZE_LIMITS[key]
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = ART_SIZES[key]
        wanted[key] = max(low, min(high, value))
    previous = _read_art_sizes_marker(cache_dir)
    if wanted["thumb"] != previous["thumb"]:
        thumb_dir = os.path.join(cache_dir, "thumb")
        try:
            for entry in os.listdir(thumb_dir):
                try:
                    os.remove(os.path.join(thumb_dir, entry))
                except OSError:
                    pass
        except OSError:
            pass
    marker = _art_sizes_marker(cache_dir)
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        tmp = marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(wanted, f)
        os.replace(tmp, marker)
    except OSError:
        pass
    ART_SIZES.update(wanted)
    return dict(ART_SIZES)


def thumb_cache_path(url: str, cache_dir: str) -> str:
    """The thumbnail's path for a full-size artwork URL."""
    return os.path.join(cache_dir, "thumb", artwork_filename(url))


def make_thumbnail(src_path: str, dest_path: str, size: int | None = None) -> bool:
    """Scale the cover at `src_path` down to `dest_path`, atomically. False
    without a scale_image installed, or when the source is not an image."""
    if scale_image is None:
        return False
    import tempfile
    size = size or ART_SIZES["thumb"]
    temp_path = None
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(dest_path), delete=False, suffix=".tmp") as f:
            temp_path = f.name
        scale_image(src_path, temp_path, size)
        os.replace(temp_path, dest_path)
        temp_path = None
        return True
    except Exception:
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def cache_thumbnail(url: str, cache_dir: str, dest_path: str) -> str | None:
    """The thumbnail at `dest_path`: scaled from the cached full-size cover
    when that is on disk, fetched at the thumbnail size from `url` otherwise."""
    if not url or not dest_path:
        return None
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path
    full = os.path.join(cache_dir, "art", os.path.basename(dest_path))
    if not _art_missing(full) and make_thumbnail(full, dest_path):
        return dest_path
    return cache_artwork(url, cache_dir, dest_path=dest_path)


def cache_artwork(url_or_obj, cache_dir: str, timeout: float = 10.0, dest_path: str | None = None) -> str | None:
    """Download artwork via urllib.request and save it atomically to <cache_dir>/art/
    (or to `dest_path`, for a thumbnail).

    Accepts either an artwork URL string or an Apple Music artwork dictionary.
    Writes to a temporary file in the art directory, flushes, fsyncs, and replaces atomically.
    Returns the absolute local file path on success, or None on error.
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
    art_dir = os.path.dirname(dest_path)

    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path

    try:
        os.makedirs(art_dir, exist_ok=True)
    except OSError:
        return None

    import tempfile
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AppleMusicGNOME/1.0"}
    )

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=art_dir, delete=False, suffix=".tmp") as f:
            temp_path = f.name
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                if status != 200:
                    return None
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, dest_path)
        temp_path = None
        return dest_path
    except Exception:
        return None
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


# Every artwork path handed out by _extract_artwork, and the URL it came from:
# the cover-sized one for a cover, the thumbnail-sized one for its thumbnail.
# Normalisation only names the file; nothing is fetched until download_art()
# runs over a finished library (or download_item_art() over a single
# on-demand item), so a sync's hundreds of downloads happen together, in
# threads, rather than one at a time in the middle of building each item.
ART_URLS: dict[str, str] = {}


def _is_thumb_path(path: str, cache_dir: str) -> bool:
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(os.path.join(cache_dir, "thumb"))


def _art_missing(path: str) -> bool:
    try:
        return os.path.getsize(path) <= 0
    except OSError:
        return True


def collect_art_urls(library_data: dict) -> dict[str, str]:
    """{path: url} for every artwork the library refers to that ART_URLS knows."""
    return {p: ART_URLS[p] for p in collect_art_paths(library_data) if p in ART_URLS}


def download_art(library_data_or_urls, cache_dir: str, workers: int = 8, log=None) -> dict:
    """Fetch every artwork the library refers to that is not in the cache yet.

    Takes a library dict (paths resolved through ART_URLS) or a {path: url}
    map. Returns {"wanted", "fetched", "failed"}. Failures are logged through
    `log` (a callable taking a string) and otherwise ignored: the UI treats a
    path that is not on disk as no artwork.
    """
    urls = library_data_or_urls if isinstance(library_data_or_urls, dict) and "sections" not in library_data_or_urls \
        else collect_art_urls(library_data_or_urls)
    todo = {p: u for p, u in urls.items() if _art_missing(p)}
    counts = {"wanted": len(urls), "fetched": 0, "failed": 0}
    if not todo:
        return counts
    import concurrent.futures
    os.makedirs(os.path.join(cache_dir, "art"), exist_ok=True)
    os.makedirs(os.path.join(cache_dir, "thumb"), exist_ok=True)
    # The covers first, the thumbnails after: a thumbnail is scaled from its
    # cover when that is on disk, and fetched only when it is not.
    covers = {p: u for p, u in todo.items() if not _is_thumb_path(p, cache_dir)}
    thumbs = {p: u for p, u in todo.items() if _is_thumb_path(p, cache_dir)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for batch, fetch in ((covers, lambda p, u: cache_artwork(u, cache_dir)),
                             (thumbs, lambda p, u: cache_thumbnail(u, cache_dir, p))):
            futures = {pool.submit(fetch, path, url): url for path, url in batch.items()}
            for fut in concurrent.futures.as_completed(futures):
                ok = False
                try:
                    ok = bool(fut.result())
                except Exception:
                    ok = False
                if ok:
                    counts["fetched"] += 1
                else:
                    counts["failed"] += 1
                    if log:
                        log(f"artwork: could not fetch {futures[fut]}")
    return counts


def download_item_art(item: dict, cache_dir: str) -> dict:
    """Fetch what one item refers to and lacks: its cover, its thumbnail and
    its rows' thumbnails (a playlist's), in threads, in place."""
    if not isinstance(item, dict):
        return item
    paths = _item_art_paths(item)
    download_art({p: ART_URLS[p] for p in paths if p in ART_URLS}, cache_dir)
    return item


def _item_art_paths(item: dict) -> set[str]:
    """Every artwork path an item refers to: its own, and its rows' thumbnails."""
    paths = set()
    for key in ("art", "thumb"):
        if item.get(key):
            paths.add(os.path.abspath(item[key]))
    for group in item.get("groups") or []:
        for entry in group.get("entries") or []:
            if isinstance(entry, dict) and entry.get("thumb"):
                paths.add(os.path.abspath(entry["thumb"]))
    return paths


def collect_art_paths(library_data: dict) -> set[str]:
    """Collect all referenced local artwork paths — covers and thumbnails —
    from a library data dict."""
    paths = set()
    for sec_items in library_data.get("sections", {}).values():
        for item in sec_items:
            paths |= _item_art_paths(item)
    for shelf in library_data.get("shelves", []):
        for item in shelf.get("items", []):
            paths |= _item_art_paths(item)
    return paths


def prune_art(library_data: dict, cache_dir: str) -> int:
    """Remove artwork files in <cache_dir>/art/ and <cache_dir>/thumb/ that
    the library no longer refers to. Returns the count of pruned files."""
    if not cache_dir:
        return 0

    norm_refs = set()
    for p in collect_art_paths(library_data):
        norm_refs.add(os.path.abspath(p))
        norm_refs.add(os.path.basename(p))

    pruned = 0
    for folder in ("art", "thumb"):
        art_dir = os.path.join(cache_dir, folder)
        if not os.path.isdir(art_dir):
            continue
        try:
            for entry in os.listdir(art_dir):
                # The sizes marker lives here too, and is nobody's artwork.
                if entry.startswith("."):
                    continue
                file_path = os.path.join(art_dir, entry)
                abs_path = os.path.abspath(file_path)
                if abs_path not in norm_refs and entry not in norm_refs:
                    try:
                        if os.path.isfile(file_path) or os.path.islink(file_path):
                            os.remove(file_path)
                            pruned += 1
                    except OSError:
                        pass
        except OSError:
            pass

    return pruned


# ---------------------------------------------------------------------------
# Internal extraction helpers
# ---------------------------------------------------------------------------


def _extract_year(attrs: dict, raw_item: dict) -> int | None:
    """Extract release year from Apple Music attributes or raw item."""
    rel_date = attrs.get("releaseDate") or raw_item.get("releaseDate")
    if rel_date and isinstance(rel_date, str) and len(rel_date) >= 4:
        try:
            return int(rel_date[:4])
        except ValueError:
            pass

    year = attrs.get("year") or raw_item.get("year")
    if year is not None:
        try:
            return int(year)
        except (ValueError, TypeError):
            pass

    # Playlists have no release date; fall back to when they were last
    # modified so the UI still has a year to show.
    mod_date = attrs.get("lastModifiedDate") or raw_item.get("lastModifiedDate")
    if mod_date and isinstance(mod_date, str) and len(mod_date) >= 4:
        try:
            return int(mod_date[:4])
        except ValueError:
            pass

    return None


def _extract_summary(attrs: dict, raw_item: dict) -> str | None:
    """Extract editorial notes or description as plain text."""
    ed = attrs.get("editorialNotes") or raw_item.get("editorialNotes")
    desc = attrs.get("description") or raw_item.get("description")
    summary = None

    if isinstance(ed, dict):
        summary = ed.get("standard") or ed.get("short") or ed.get("name")
    elif isinstance(ed, str):
        summary = ed
    elif isinstance(desc, dict):
        summary = desc.get("standard") or desc.get("short")
    elif isinstance(desc, str):
        summary = desc
    elif raw_item.get("summary"):
        summary = raw_item["summary"]

    return strip_html(summary)


def _extract_genre(attrs: dict, raw_item: dict) -> str | None:
    """The first genre name, from the attributes or the raw item."""
    genre_names = attrs.get("genreNames") or raw_item.get("genreNames")
    if isinstance(genre_names, list) and genre_names:
        return genre_names[0]
    if isinstance(genre_names, str):
        return genre_names
    return raw_item.get("genre") or None


def _extract_artwork(attrs: dict, raw_item: dict, cache_dir: str | None) -> tuple[str | None, str | None, str | None]:
    """Extract the local cached cover path, its thumbnail's, and the hex
    background color."""
    art = None
    thumb = None
    art_color = None

    if raw_item.get("art"):
        art = raw_item["art"]
    if raw_item.get("thumb"):
        thumb = raw_item["thumb"]
    if raw_item.get("artColor"):
        art_color = format_color(raw_item["artColor"])

    artwork = attrs.get("artwork") or raw_item.get("artwork")
    if isinstance(artwork, dict):
        url = artwork.get("url")
        if url and cache_dir:
            art, thumb = _register_artwork(url, cache_dir)
        bg = artwork.get("bgColor")
        if bg and not art_color:
            art_color = format_color(bg)

    return art, thumb, art_color


def _register_artwork(url_template: str, cache_dir: str) -> tuple[str, str]:
    """Name the cover and thumbnail files for an artwork URL template and
    record what each is fetched from."""
    cover, small = ART_SIZES["cover"], ART_SIZES["thumb"]
    full = template_artwork_url(url_template, cover, cover)
    art = artwork_cache_path(full, cache_dir)
    thumb = thumb_cache_path(full, cache_dir)
    ART_URLS[art] = full
    ART_URLS[thumb] = template_artwork_url(url_template, small, small)
    return art, thumb


def _extract_catalog_id(attrs: dict, raw_item: dict, resource_type: str) -> str | None:
    """Extract catalog ID from a catalog or library resource."""
    item_id = str(raw_item.get("id") or "")
    if raw_item.get("type") == resource_type:
        return item_id if item_id else None

    if "relationships" in raw_item:
        cat_data = raw_item["relationships"].get("catalog", {}).get("data", [])
        if cat_data and isinstance(cat_data, list) and len(cat_data) > 0:
            cid = cat_data[0].get("id")
            if cid:
                return str(cid)

    play_params = attrs.get("playParams") or {}
    if "catalogId" in play_params:
        return str(play_params["catalogId"])

    if raw_item.get("catalogId"):
        return str(raw_item["catalogId"])

    # If item_id does not look like a library id (l.*, i.*, p.*, r.*), treat as catalog ID
    if item_id and not (item_id.startswith("l.") or item_id.startswith("i.") or item_id.startswith("p.") or item_id.startswith("r.")):
        return item_id

    return None


# ---------------------------------------------------------------------------
# Item and Track Normalizers
# ---------------------------------------------------------------------------


def normalize_track(raw_track: dict, index: int = 0, cache_dir: str | None = None) -> dict:
    """Turn an Apple Music API track into the Track shape.

    Track = {
      "id": "...", "catalogId": "..." | null, "title": "...", "artist": "...",
      "album": "...", "trackNumber": int, "discNumber": int, "durationMs": int,
      "durationLabel": "3:36", "explicit": bool, "index": int,
      "thumb": "<cache>/thumb/<x>.jpg" | null
    }

    `thumb` is named only when `cache_dir` is given: a playlist's rows show
    their own artwork, an album's tracks share the album's.
    """
    attrs = raw_track.get("attributes") or {}
    track_id = str(raw_track.get("id") or "")
    thumb = raw_track.get("thumb") or None
    artwork = attrs.get("artwork")
    if cache_dir and isinstance(artwork, dict) and artwork.get("url"):
        _, thumb = _register_artwork(artwork["url"], cache_dir)

    catalog_id = _extract_catalog_id(attrs, raw_track, "songs")

    title = attrs.get("name") or raw_track.get("title") or ""
    artist = attrs.get("artistName") or attrs.get("artist") or raw_track.get("artist") or ""
    album = attrs.get("albumName") or attrs.get("album") or raw_track.get("album") or ""

    track_number = attrs.get("trackNumber") if attrs.get("trackNumber") is not None else raw_track.get("trackNumber", 1)
    disc_number = attrs.get("discNumber") if attrs.get("discNumber") is not None else raw_track.get("discNumber", 1)
    duration_ms = attrs.get("durationInMillis") if attrs.get("durationInMillis") is not None else raw_track.get("durationMs", 0)

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

    content_rating = attrs.get("contentRating") or raw_track.get("contentRating")
    is_explicit = (
        content_rating == "explicit"
        or attrs.get("explicit") is True
        or raw_track.get("explicit") is True
    )

    return {
        "id": track_id,
        "catalogId": catalog_id,
        "title": title,
        "artist": artist,
        "album": album,
        "trackNumber": track_number,
        "discNumber": disc_number,
        "durationMs": duration_ms,
        "durationLabel": duration_label,
        "explicit": is_explicit,
        "index": int(index),
        "thumb": thumb,
    }


def normalize_album(raw_album: dict, cache_dir: str | None = None, tracks: list[dict] | None = None) -> dict:
    """Turn an Apple Music API album into the Item shape with kind='album'.

    `tracks` are the album's songs when the caller fetched them separately;
    otherwise the album's own `tracks` relationship is read. Tracks are
    grouped by disc: groups = [{"name": "Disc 1", "play": {...}, "entries": [...]}].
    """
    attrs = raw_album.get("attributes") or {}
    item_id = str(raw_album.get("id") or "")
    title = attrs.get("name") or raw_album.get("title") or ""
    subtitle = attrs.get("artistName") or raw_album.get("subtitle") or "Apple Music"
    year = _extract_year(attrs, raw_album)
    genre = _extract_genre(attrs, raw_album)
    summary = _extract_summary(attrs, raw_album)
    art, thumb, art_color = _extract_artwork(attrs, raw_album, cache_dir)
    catalog_id = _extract_catalog_id(attrs, raw_album, "albums")
    url = attrs.get("url") or raw_album.get("url")

    # Resolve track list
    raw_tracks = tracks
    if raw_tracks is None:
        rel_tracks = raw_album.get("relationships", {}).get("tracks", {}).get("data")
        if isinstance(rel_tracks, list):
            raw_tracks = rel_tracks
        else:
            raw_tracks = []

    def track_sort_key(t):
        t_attrs = t.get("attributes") or {}
        d = t_attrs.get("discNumber") if t_attrs.get("discNumber") is not None else t.get("discNumber", 1)
        tr = t_attrs.get("trackNumber") if t_attrs.get("trackNumber") is not None else t.get("trackNumber", 1)
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

    # Group raw tracks by discNumber first, so each group's tracks are
    # indexed from 0 within that group (a group is played on its own, so
    # "index" is the position in *that* group's queue, not the whole album).
    raw_discs: dict[int, list[dict]] = {}
    for t in sorted_tracks:
        t_attrs = t.get("attributes") or {}
        d = t_attrs.get("discNumber") if t_attrs.get("discNumber") is not None else t.get("discNumber", 1)
        try:
            d_int = int(d) if d is not None else 1
        except (ValueError, TypeError):
            d_int = 1
        raw_discs.setdefault(d_int, []).append(t)

    normalized_tracks = []
    groups = []
    for d in sorted(raw_discs.keys()):
        disc_label = f"Disc {d if d > 0 else 1}"
        disc_entries = [
            normalize_track(t, index=idx)
            for idx, t in enumerate(raw_discs[d])
        ]
        normalized_tracks.extend(disc_entries)
        groups.append({
            "name": disc_label,
            "play": {"kind": "album", "id": item_id},
            "entries": disc_entries,
        })

    song_count = len(normalized_tracks)
    # No tracks in hand (a shelf item, normalised without its list): the
    # count alone, rather than a "0 min" that reads as an empty album.
    total_duration_ms = sum(t["durationMs"] for t in normalized_tracks) if normalized_tracks else None
    if song_count == 0 and attrs.get("trackCount"):
        try:
            song_count = int(attrs["trackCount"])
        except (ValueError, TypeError):
            song_count = 0

    count_label = format_count_label(song_count, total_duration_ms)

    content_rating = attrs.get("contentRating") or raw_album.get("contentRating")
    is_explicit = (
        content_rating == "explicit"
        or attrs.get("explicit") is True
        or raw_album.get("explicit") is True
        or any(t.get("explicit") for t in normalized_tracks)
    )

    return {
        "id": item_id,
        "kind": "album",
        "title": title,
        "subtitle": subtitle,
        "year": year,
        "genre": genre,
        "summary": summary,
        "art": art,
        "thumb": thumb,
        "artColor": art_color,
        "countLabel": count_label,
        "explicit": is_explicit,
        "catalogId": catalog_id,
        "url": url,
        "play": {"kind": "album", "id": item_id},
        "groups": groups,
    }


def normalize_artist(raw_artist: dict, cache_dir: str | None = None, albums: list[dict] | None = None) -> dict:
    """Turn an Apple Music API artist into the Item shape with kind='artist'.

    `albums` are the artist's albums (raw, or already normalized) when the
    caller fetched them; otherwise the `albums` relationship is read.
    subtitle='Artist', groups=[one group per album].
    """
    attrs = raw_artist.get("attributes") or {}
    item_id = str(raw_artist.get("id") or "")
    title = attrs.get("name") or raw_artist.get("title") or ""
    subtitle = "Artist"
    year = None
    genre = _extract_genre(attrs, raw_artist)
    summary = _extract_summary(attrs, raw_artist)
    art, thumb, art_color = _extract_artwork(attrs, raw_artist, cache_dir)
    catalog_id = _extract_catalog_id(attrs, raw_artist, "artists")
    url = attrs.get("url") or raw_artist.get("url")

    raw_albums = albums
    if raw_albums is None:
        rel_albums = raw_artist.get("relationships", {}).get("albums", {}).get("data")
        if isinstance(rel_albums, list):
            raw_albums = rel_albums
        else:
            raw_albums = []

    groups = []
    has_explicit = False
    for alb in raw_albums:
        if alb.get("kind") == "album" and "groups" in alb and "play" in alb:
            all_entries = []
            for g in alb.get("groups", []):
                all_entries.extend(g.get("entries", []))
            groups.append({
                "name": alb.get("title", ""),
                "play": alb.get("play", {"kind": "album", "id": alb.get("id")}),
                "entries": all_entries,
            })
            if alb.get("explicit"):
                has_explicit = True
        else:
            norm_alb = normalize_album(alb, cache_dir=cache_dir)
            all_entries = []
            for g in norm_alb.get("groups", []):
                all_entries.extend(g.get("entries", []))
            groups.append({
                "name": norm_alb.get("title", ""),
                "play": norm_alb.get("play", {"kind": "album", "id": norm_alb.get("id")}),
                "entries": all_entries,
            })
            if norm_alb.get("explicit"):
                has_explicit = True

    album_count = len(groups)
    count_label = f"{album_count} album" if album_count == 1 else f"{album_count} albums"

    return {
        "id": item_id,
        "kind": "artist",
        "title": title,
        "subtitle": subtitle,
        "year": year,
        "genre": genre,
        "summary": summary,
        "art": art,
        "thumb": thumb,
        "artColor": art_color,
        "countLabel": count_label,
        "explicit": has_explicit,
        "catalogId": catalog_id,
        "url": url,
        "play": {"kind": "artist", "id": item_id},
        "groups": groups,
    }


def normalize_playlist(raw_playlist: dict, cache_dir: str | None = None, tracks: list[dict] | None = None) -> dict:
    """Turn an Apple Music API playlist into the Item shape with kind='playlist'.

    `tracks` are the playlist's songs when the caller fetched them; otherwise
    the `tracks` relationship is read.
    groups=[{"name": "Tracks", "play": {"kind": "playlist", "id": item_id}, "entries": [...]}]
    """
    attrs = raw_playlist.get("attributes") or {}
    item_id = str(raw_playlist.get("id") or "")
    title = attrs.get("name") or raw_playlist.get("title") or ""
    subtitle = (
        attrs.get("curatorName")
        or attrs.get("artistName")
        or raw_playlist.get("subtitle")
        or "Apple Music"
    )
    year = _extract_year(attrs, raw_playlist)
    genre = _extract_genre(attrs, raw_playlist)
    summary = _extract_summary(attrs, raw_playlist)
    art, thumb, art_color = _extract_artwork(attrs, raw_playlist, cache_dir)
    catalog_id = _extract_catalog_id(attrs, raw_playlist, "playlists")
    url = attrs.get("url") or raw_playlist.get("url")

    raw_tracks = tracks
    if raw_tracks is None:
        rel_tracks = raw_playlist.get("relationships", {}).get("tracks", {}).get("data")
        if isinstance(rel_tracks, list):
            raw_tracks = rel_tracks
        else:
            raw_tracks = []

    normalized_tracks = [
        normalize_track(t, index=idx, cache_dir=cache_dir)
        for idx, t in enumerate(raw_tracks)
    ]

    groups = [
        {
            "name": "Tracks",
            "play": {"kind": "playlist", "id": item_id},
            "entries": normalized_tracks,
        }
    ]

    song_count = len(normalized_tracks)
    # No tracks in hand (a shelf item, normalised without its list): the
    # count alone, rather than a "0 min" that reads as an empty album.
    total_duration_ms = sum(t["durationMs"] for t in normalized_tracks) if normalized_tracks else None
    if song_count == 0 and attrs.get("trackCount"):
        try:
            song_count = int(attrs["trackCount"])
        except (ValueError, TypeError):
            song_count = 0

    count_label = format_count_label(song_count, total_duration_ms)

    content_rating = attrs.get("contentRating") or raw_playlist.get("contentRating")
    is_explicit = (
        content_rating == "explicit"
        or attrs.get("explicit") is True
        or raw_playlist.get("explicit") is True
        or any(t.get("explicit") for t in normalized_tracks)
    )

    return {
        "id": item_id,
        "kind": "playlist",
        "title": title,
        "subtitle": subtitle,
        "year": year,
        "genre": genre,
        "summary": summary,
        "art": art,
        "thumb": thumb,
        "artColor": art_color,
        "countLabel": count_label,
        "explicit": is_explicit,
        "catalogId": catalog_id,
        "url": url,
        "play": {"kind": "playlist", "id": item_id},
        "groups": groups,
    }


def normalize_station(raw_station: dict, cache_dir: str | None = None) -> dict:
    """Turn an Apple Music API station into the Item shape with kind='station'.

    groups=[]
    """
    attrs = raw_station.get("attributes") or {}
    item_id = str(raw_station.get("id") or "")
    title = attrs.get("name") or raw_station.get("title") or ""
    subtitle = (
        attrs.get("stationProviderName")
        or attrs.get("curatorName")
        or attrs.get("artistName")
        or raw_station.get("subtitle")
        or "Apple Music Radio"
    )
    year = None
    genre = _extract_genre(attrs, raw_station)
    summary = _extract_summary(attrs, raw_station)
    art, thumb, art_color = _extract_artwork(attrs, raw_station, cache_dir)
    catalog_id = _extract_catalog_id(attrs, raw_station, "stations")
    url = attrs.get("url") or raw_station.get("url")

    content_rating = attrs.get("contentRating") or raw_station.get("contentRating")
    is_explicit = (
        content_rating == "explicit"
        or attrs.get("explicit") is True
        or raw_station.get("explicit") is True
    )

    return {
        "id": item_id,
        "kind": "station",
        "title": title,
        "subtitle": subtitle,
        "year": year,
        "genre": genre,
        "summary": summary,
        "art": art,
        "thumb": thumb,
        "artColor": art_color,
        "countLabel": None,
        "explicit": is_explicit,
        "catalogId": catalog_id,
        "url": url,
        "play": {"kind": "station", "id": item_id},
        "groups": [],
    }


def normalize_song_as_item(raw_song: dict, cache_dir: str | None = None) -> dict:
    """Normalize an Apple Music song into the Item shape (e.g. for search results)."""
    attrs = raw_song.get("attributes") or {}
    item_id = str(raw_song.get("id") or "")
    catalog_id = _extract_catalog_id(attrs, raw_song, "songs")

    art, thumb, art_color = _extract_artwork(attrs, raw_song, cache_dir)
    duration_ms = attrs.get("durationInMillis") if attrs.get("durationInMillis") is not None else raw_song.get("durationMs", 0)
    try:
        duration_ms = int(duration_ms)
    except (ValueError, TypeError):
        duration_ms = 0

    year = _extract_year(attrs, raw_song)
    genre = _extract_genre(attrs, raw_song)

    return {
        "id": item_id,
        "kind": "song",
        "title": attrs.get("name") or raw_song.get("title") or "",
        "subtitle": attrs.get("artistName") or raw_song.get("artist") or "",
        "year": year,
        "genre": genre,
        "summary": None,
        "art": art,
        "thumb": thumb,
        "artColor": art_color,
        "countLabel": format_duration(duration_ms),
        "explicit": attrs.get("contentRating") == "explicit" or attrs.get("explicit") is True,
        "catalogId": catalog_id,
        "url": attrs.get("url") or raw_song.get("url"),
        "play": {"kind": "song", "id": item_id},
        "groups": [],
    }


def normalize_item(raw_item: dict, cache_dir: str | None = None, include_groups: bool = True) -> dict:
    """Normalize any Apple Music resource into an Item."""
    raw_type = str(raw_item.get("type", ""))
    raw_kind = str(raw_item.get("kind", ""))

    if raw_type in ("albums", "library-albums") or raw_kind == "album":
        item = normalize_album(raw_item, cache_dir=cache_dir)
    elif raw_type in ("playlists", "library-playlists") or raw_kind == "playlist":
        item = normalize_playlist(raw_item, cache_dir=cache_dir)
    elif raw_type in ("artists", "library-artists") or raw_kind == "artist":
        item = normalize_artist(raw_item, cache_dir=cache_dir)
    elif raw_type in ("stations", "radio-stations", "apple-curators") or raw_kind == "station":
        item = normalize_station(raw_item, cache_dir=cache_dir)
    elif raw_type in ("songs", "library-songs") or raw_kind == "song":
        item = normalize_song_as_item(raw_item, cache_dir=cache_dir)
    elif raw_type in ("music-videos", "library-music-videos") or raw_kind == "video":
        # A music video is a song with a picture: the same fields, played
        # as MusicKit's own `musicVideo` queue kind.
        item = normalize_song_as_item(raw_item, cache_dir=cache_dir)
        item["kind"] = "video"
        item["play"] = {"kind": "musicVideo", "id": item["id"]}
    else:
        attrs = raw_item.get("attributes") or {}
        if "trackCount" in attrs or "artistName" in attrs:
            item = normalize_album(raw_item, cache_dir=cache_dir)
        else:
            item = normalize_station(raw_item, cache_dir=cache_dir)

    if not include_groups:
        item["groups"] = []
    return item


# The shelves a search can answer with, as MusicKit names them, and what
# each is called. `topResults` is the catalog's own pick of its best few
# hits across every kind (asked for `with=topResults`; the library's search
# has no such thing), which Apple Music's own search page puts first.
SEARCH_SHELF_TITLES = {
    "topResults": "Top Results",
    "artists": "Artists",
    "library-artists": "Artists",
    "albums": "Albums",
    "library-albums": "Albums",
    "songs": "Songs",
    "library-songs": "Songs",
    "playlists": "Playlists",
    "library-playlists": "Playlists",
    "music-videos": "Music Videos",
    "stations": "Stations",
}
# The order the shelves take when the answer does not say: Apple's own for
# a search of this kind, as its `meta.results.order` has it.
SEARCH_SHELF_ORDER = [
    "topResults", "artists", "library-artists", "songs", "library-songs",
    "albums", "library-albums", "playlists", "library-playlists", "music-videos", "stations",
]


def search_results(raw: dict | None, cache_dir: str) -> dict:
    """`am.py search`'s answer from MusicKit's: `shelves`, one per kind that
    answered, each `{key, title, items}`, in the order Apple's own search
    page shows them (`meta.results.order`, or SEARCH_SHELF_ORDER without
    it) with Top Results first; and `items`, the same hits as one flat list
    with no repeats (a top result is also among its kind's), for the
    overview's own search provider.

    A search never waits on a download, so a hit's `art` is its cached
    cover when the sync has fetched it and a small catalog URL otherwise,
    which the shell fetches on its own; its `thumb` only ever names a file
    that is on disk."""
    results = (raw or {}).get("results") or {}
    order = ((raw or {}).get("meta") or {}).get("results", {}).get("order")
    if not isinstance(order, list):
        order = []
    keys = [k for k in order if k in SEARCH_SHELF_TITLES]
    keys += [k for k in SEARCH_SHELF_ORDER if k not in keys]
    shelves = []
    items = []
    seen = set()
    for key in keys:
        section = results.get(key)
        if not isinstance(section, dict):
            continue
        hits = []
        for raw_item in section.get("data") or []:
            if not isinstance(raw_item, dict):
                continue
            item = normalize_item(raw_item, cache_dir, include_groups=False)
            _settle_search_art(item, raw_item)
            hits.append(item)
            if (item["kind"], item["id"]) not in seen:
                seen.add((item["kind"], item["id"]))
                items.append(item)
        if hits:
            shelf_key = "top" if key == "topResults" else key.removeprefix("library-")
            shelves.append({"key": shelf_key, "title": SEARCH_SHELF_TITLES[key], "items": hits})
    return {"shelves": shelves, "items": items}


def search_suggestions(raw: dict | None, cache_dir: str) -> dict:
    """`am.py suggest`'s answer from MusicKit's `search/suggestions`:
    `terms`, the few searches Apple would complete the typed one to, each
    `{term, display}` — `term` what to search for, `display` as Apple
    shows it — with no repeats; and `items`, its best few hits for what is
    typed so far, as `search_results` has its hits (no groups, art as it
    stands)."""
    suggestions = ((raw or {}).get("results") or {}).get("suggestions") or []
    terms = []
    items = []
    seen_terms = set()
    seen_items = set()
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        kind = suggestion.get("kind")
        if kind == "terms":
            term = str(suggestion.get("searchTerm") or suggestion.get("displayTerm") or "").strip()
            if not term or term.lower() in seen_terms:
                continue
            seen_terms.add(term.lower())
            terms.append({"term": term, "display": str(suggestion.get("displayTerm") or term).strip()})
        elif kind == "topResults":
            content = suggestion.get("content")
            if not isinstance(content, dict):
                continue
            item = normalize_item(content, cache_dir, include_groups=False)
            _settle_search_art(item, content)
            if (item["kind"], item["id"]) in seen_items:
                continue
            seen_items.add((item["kind"], item["id"]))
            items.append(item)
    return {"terms": terms, "items": items}


def search_landing(raw: dict | None, cache_dir: str) -> dict:
    """`am.py landing`'s answer from Apple's search-landing recommendations:
    `categories`, the rooms Apple Music's own search page offers to browse
    before anything is typed (Rock, Hip-Hop, Chill, the decades…), in
    Apple's order across every recommendation in the set, each
    `{id, kind: "category", title, subtitle, art, artColor, url}` — `title`
    the short name on the tile ("Rock"), `subtitle` the curator's own
    ("Apple Music Rock"), `art` a small catalog URL the shell fetches on
    its own, `artColor` the tile's colour behind it. Only Apple's curators
    are categories: an editorial item in the set is a banner with nothing
    behind it, and is left out."""
    categories = []
    seen = set()
    for rec in (raw or {}).get("data") or []:
        if not isinstance(rec, dict):
            continue
        contents = ((rec.get("relationships") or {}).get("contents") or {}).get("data") or []
        for content in contents:
            if not isinstance(content, dict) or content.get("type") != "apple-curators":
                continue
            category = normalize_category(content)
            if not category or category["id"] in seen:
                continue
            seen.add(category["id"])
            categories.append(category)
    return {"categories": categories}


def normalize_category(raw: dict) -> dict | None:
    """An Apple curator as a category tile. None without a name."""
    attrs = raw.get("attributes") or {}
    item_id = str(raw.get("id") or "")
    name = str(attrs.get("name") or "").strip()
    short = str(attrs.get("shortName") or "").strip()
    if not item_id or not (name or short):
        return None
    artwork = attrs.get("artwork") if isinstance(attrs.get("artwork"), dict) else {}
    url = artwork.get("url")
    return {
        "id": item_id,
        "kind": "category",
        "title": short or name,
        "subtitle": name if name and name != (short or name) else None,
        "art": template_artwork_url(url, CATEGORY_ART_SIZE, CATEGORY_ART_SIZE) if url else None,
        "artColor": format_color(artwork.get("bgColor")) if artwork.get("bgColor") else None,
        "url": attrs.get("url"),
    }


# A category tile's picture, wide but not big: the shell draws it cropped
# over the tile's colour.
CATEGORY_ART_SIZE = 320


def category_page(raw: dict | None, cache_dir: str) -> dict:
    """`am.py category`'s answer from a curator with its grouping: the
    category's `id` and `title`, and its `shelves` — the grouping's one
    tab's editorial elements, each `{key, title, items}` in Apple's order,
    items normalised as a search hit is (no groups, art as it stands);
    an element with no title or nothing in it is left out."""
    data = (raw or {}).get("data") or []
    curator = data[0] if data and isinstance(data[0], dict) else {}
    attrs = curator.get("attributes") or {}
    title = str(attrs.get("shortName") or attrs.get("name") or "").strip()
    shelves = []
    groupings = ((curator.get("relationships") or {}).get("grouping") or {}).get("data") or []
    for grouping in groupings:
        if not isinstance(grouping, dict):
            continue
        tabs = ((grouping.get("relationships") or {}).get("tabs") or {}).get("data") or []
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            children = ((tab.get("relationships") or {}).get("children") or {}).get("data") or []
            for index, element in enumerate(children):
                if not isinstance(element, dict):
                    continue
                element_attrs = element.get("attributes") or {}
                shelf_title = str(element_attrs.get("title") or element_attrs.get("name") or "").strip()
                contents = ((element.get("relationships") or {}).get("contents") or {}).get("data") or []
                items = []
                for raw_item in contents:
                    if not isinstance(raw_item, dict) or not (raw_item.get("attributes") or {}).get("name"):
                        continue
                    item = normalize_item(raw_item, cache_dir, include_groups=False)
                    _settle_search_art(item, raw_item)
                    items.append(item)
                if shelf_title and items:
                    shelves.append({"key": f"cat-{element.get('id') or index}", "title": shelf_title, "items": items})
    return {"id": str(curator.get("id") or ""), "title": title, "shelves": shelves}


def _settle_search_art(item: dict, raw_item: dict) -> None:
    """A search hit's artwork as it stands: the sync's files where they
    exist, a thumbnail-sized catalog URL for the cover otherwise, and no thumb
    at all rather than the name of one that was never fetched."""
    if not (item.get("thumb") and os.path.exists(item["thumb"])):
        item["thumb"] = None
    if not (item.get("art") and os.path.exists(item["art"])):
        url = ((raw_item.get("attributes") or {}).get("artwork") or {}).get("url")
        small = ART_SIZES["thumb"]
        item["art"] = template_artwork_url(url, small, small) if url else None


def recommendation_shelves(raw_recs: list, cache_dir: str | None = None) -> list[dict]:
    """Apple's home page as shelves: one per recommendation, in the order
    the API sends them, titled as Apple titles it ("New Releases for You",
    "Stations for You", "More from …", a genre, a decade). A group
    recommendation is its members, each a shelf of its own. Items are
    normalised without their track lists, as a shelf's are; a
    recommendation with nothing in it is left out.

    Shelf = {"key": "rec-<id>", "title": "…", "items": [Item]}
    """
    shelves = []

    def walk(rec):
        if not isinstance(rec, dict):
            return
        attrs = rec.get("attributes") or {}
        rel = rec.get("relationships") or {}
        members = (rel.get("recommendations") or {}).get("data") or []
        if members:
            for member in members:
                walk(member)
            return
        contents = (rel.get("contents") or {}).get("data") or []
        items = [normalize_item(it, cache_dir, include_groups=False) for it in contents if isinstance(it, dict)]
        items = [it for it in items if it and it.get("title")]
        if not items:
            return
        title = attrs.get("title") or {}
        title = (title.get("stringForDisplay") if isinstance(title, dict) else str(title)) or "For You"
        shelves.append({"key": f"rec-{rec.get('id')}", "title": title, "items": items})

    for rec in raw_recs or []:
        walk(rec)
    return shelves


def group_songs_into_albums_and_artists(songs: list[dict], cache_dir: str | None = None) -> tuple[list[dict], list[dict]]:
    """Group songs from /v1/me/library/songs?include=albums into albums and artists."""
    albums_map: dict[str, dict] = {}
    for s in songs:
        s_attrs = s.get("attributes") or {}
        album_name = s_attrs.get("albumName", "Unknown Album")
        artist_name = s_attrs.get("artistName", "Unknown Artist")

        rel_albums = s.get("relationships", {}).get("albums", {}).get("data", [])
        if rel_albums:
            alb_rel = rel_albums[0]
            alb_id = alb_rel.get("id")
            # A song can point at a library album that no longer answers
            # (the relationship survives the album resource): a stub with
            # no attributes. The song itself knows the album's name, artist
            # and artwork, so the stub is filled in from it rather than
            # becoming a nameless tile.
            alb_attrs = dict(alb_rel.get("attributes") or {})
            if not alb_attrs.get("name"):
                alb_attrs.setdefault("name", album_name)
                alb_attrs.setdefault("artistName", artist_name)
                alb_attrs.setdefault("artwork", s_attrs.get("artwork"))
                alb_attrs.setdefault("releaseDate", s_attrs.get("releaseDate"))
                alb_attrs.setdefault("genreNames", s_attrs.get("genreNames", []))
                alb_attrs.setdefault("contentRating", s_attrs.get("contentRating"))
            alb_obj = dict(alb_rel, attributes=alb_attrs)
        else:
            alb_id = f"l.alb_{hashlib.md5(f'{album_name}:{artist_name}'.encode('utf-8')).hexdigest()[:12]}"
            alb_obj = {
                "id": alb_id,
                "type": "library-albums",
                "attributes": {
                    "name": album_name,
                    "artistName": artist_name,
                    "artwork": s_attrs.get("artwork"),
                    "releaseDate": s_attrs.get("releaseDate"),
                    "genreNames": s_attrs.get("genreNames", []),
                    "contentRating": s_attrs.get("contentRating"),
                },
            }

        if alb_id not in albums_map:
            albums_map[alb_id] = {
                "album": alb_obj,
                "songs": [],
            }
        albums_map[alb_id]["songs"].append(s)

    albums_list = []
    artists_map: dict[str, list[dict]] = {}
    for alb_id, entry in albums_map.items():
        alb_norm = normalize_album(entry["album"], cache_dir=cache_dir, tracks=entry["songs"])
        albums_list.append(alb_norm)

        art_name = alb_norm["subtitle"] or "Unknown Artist"
        artists_map.setdefault(art_name, []).append(alb_norm)

    albums_list.sort(key=lambda a: a.get("title", "").lower())

    artists_list = []
    for art_name, art_albums in sorted(artists_map.items(), key=lambda x: x[0].lower()):
        art_id = f"l.art_{hashlib.md5(art_name.encode('utf-8')).hexdigest()[:12]}"
        first_art = art_albums[0]["art"] if art_albums and art_albums[0].get("art") else None
        first_thumb = art_albums[0].get("thumb") if art_albums and art_albums[0].get("art") else None
        first_color = art_albums[0].get("artColor") if art_albums else None

        art_obj = {
            "id": art_id,
            "type": "library-artists",
            "attributes": {
                "name": art_name,
                "genreNames": [art_albums[0].get("genre")] if art_albums and art_albums[0].get("genre") else [],
            },
        }
        artist_norm = normalize_artist(art_obj, cache_dir=cache_dir, albums=art_albums)
        # A library artist carries no artwork of its own: it takes its first
        # album's — the thumbnail with the cover, or the tile would decode
        # the full cover for want of one.
        if not artist_norm["art"]:
            artist_norm["art"] = first_art
            artist_norm["thumb"] = first_thumb
            artist_norm["artColor"] = first_color
        artists_list.append(artist_norm)

    return albums_list, artists_list


def save_library(library_data: dict, cache_dir: str, only: str | None = None) -> None:
    """Atomically save library.json under flock, merging sections if only is specified."""
    import fcntl
    os.makedirs(cache_dir, exist_ok=True)
    lock_path = os.path.join(cache_dir, "library.lock")
    lib_path = os.path.join(cache_dir, "library.json")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            target_data = library_data
            if only and os.path.exists(lib_path):
                try:
                    with open(lib_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if only in ("albums", "artists", "playlists", "radio"):
                        existing.setdefault("sections", {})[only] = library_data.get("sections", {}).get(only, [])
                    elif only == "shelves":
                        existing["shelves"] = library_data.get("shelves", [])
                    existing["generated"] = library_data.get("generated", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                    target_data = existing
                except Exception:
                    target_data = library_data

            tmp_path = lib_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(target_data, f, indent=2)
            os.replace(tmp_path, lib_path)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# The other caches: what the shell fetches on its own, and answers kept
# ---------------------------------------------------------------------------


def prune_remote_art(cache_dir: str, max_bytes: int = 32 * 1024 * 1024) -> int:
    """Trim <cache_dir>/remote-art/ — the covers the shell fetches itself
    (a search hit's, the player's, a category's picture) — to `max_bytes`,
    keeping the newest by mtime. Nothing else ever removes them. Returns
    how many went."""
    folder = os.path.join(cache_dir, "remote-art")
    try:
        entries = []
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if os.path.isfile(path):
                entries.append((st.st_mtime, st.st_size, path))
    except OSError:
        return 0
    entries.sort(reverse=True)
    kept = 0
    pruned = 0
    for _mtime, size, path in entries:
        if kept + size <= max_bytes:
            kept += size
            continue
        try:
            os.remove(path)
            pruned += 1
        except OSError:
            pass
    return pruned


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value or ""))


def item_cache_path(cache_dir: str, kind: str, item_id: str) -> str:
    """Where `am.py item` keeps the full item it answered with, for the
    shell to read back without a process: <cache>/items/<kind>-<id>.json."""
    return os.path.join(cache_dir, "items", f"{_safe_id(kind)}-{_safe_id(item_id)}.json")


def landing_cache_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "landing.json")


def category_cache_path(cache_dir: str, category_id: str) -> str:
    return os.path.join(cache_dir, "categories", f"{_safe_id(category_id)}.json")


def write_answer(path: str, answer: dict) -> dict:
    """Keep `answer` at `path`, atomically, stamped `cached` with when.
    Best effort: a cache that cannot be written is only a cache."""
    answer = dict(answer)
    answer["cached"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(answer, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return answer


def read_answer(path: str, max_age_seconds: float) -> dict | None:
    """The answer kept at `path`, or None when there is none, it cannot be
    read, or it was stamped longer than `max_age_seconds` ago."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            answer = json.load(f)
        stamp = answer.get("cached") if isinstance(answer, dict) else None
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    if (datetime.now(timezone.utc) - when).total_seconds() > max_age_seconds:
        return None
    return answer
