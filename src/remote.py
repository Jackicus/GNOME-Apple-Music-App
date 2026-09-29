"""Remote artwork and the engine's shelves: where the artwork the library does not hold is
kept, and fetching it. No GTK here: the widgets decode what this puts on disk
(widgets/artwork.py).

A sync fetches only thumbnails; the covers (an Item's `art`, 640 px) are fetched here on
demand, by what shows one: `await fetch_cover(item)` downloads it into <cache>/art/ from the
`artUrl` the sync kept in the Item, and answers True when this call brought the file (the
widget then looks again). Artwork the library does not name (the item playing, a search hit)
comes through `await fetch_remote(url, size)`: the URL re-sized to `size` px square, fetched
into <cache>/remote-art/ (named by the sized URL's hash; the sync trims that directory to a
budget) and answered as its path.

The engine's search, suggest, category, browse and made-for-you answers name a small catalog
URL as an item's `art` when the sync has not fetched its cover (and no `thumb`):
`remote_item(data)` gives such a dict the paths fetch_remote() would use (`thumb` at
THUMB_SIZE, `art` at COVER_SIZE, with `thumbUrl` and `artUrl` to fetch them by), so a tile
shows the thumbnail once `await fetch_thumb(item)` has brought it and a detail page's
fetch_cover() the cover. remote_shelves() makes the engine's shelf dicts into library.Shelf
objects of such Items, titled in the app's words (shelf_title()), and fetch_shelf_art() brings
their thumbnails a few at a time.

Downloads run in a pool of their own (FETCH_WORKERS threads), not in asyncio's default
executor, where the artwork is decoded: a page's thumbnails downloading on a slow connection
never hold up the covers on screen. Whether a file is on disk already is asked there too, off
the main loop (on_disk()). Only https URLs are fetched, and normalize.cache_artwork writes only
inside the cache. Concurrent requests for one file share its download. Nothing here needs the
engine.
"""

import asyncio
import concurrent.futures
import logging
import os
import re
from gettext import gettext as _

from .backend import config, normalize, store
from .library import N_, Item, Shelf

log = logging.getLogger(__name__)

# Threads downloading artwork at once, for the whole app.
FETCH_WORKERS = 6
# Thumbnails one page of shelves asks for at once: the rest wait their turn outside the pool,
# so another page's (or the player's) downloads are not queued behind all of them.
ART_CONCURRENCY = 6

# The size at the end of an Apple artwork URL ("…/256x256bb.jpg", "…/600x600bb-60.jpg"), to
# re-size one; a template ("{w}x{h}") is filled by the backend first.
ART_SIZE_RE = re.compile(r'/(\d+)x(\d+)([a-z]{0,3}(?:-\d+)?)(\.[A-Za-z0-9]+)$')

# The headings of a search's shelves, by the key the engine gives each (normalize.search_results).
SEARCH_TITLES = {
    # Translators: the first shelf of a search's results: Apple's best few hits of any kind.
    'top': N_('Top Results'),
    'artists': N_('Artists'),
    'albums': N_('Albums'),
    'songs': N_('Songs'),
    'playlists': N_('Playlists'),
    'music-videos': N_('Music Videos'),
    'stations': N_('Stations'),
}


def is_url(value):
    """Whether `value` is an https URL: the only artwork this app fetches."""
    return isinstance(value, str) and value.startswith('https://')


def sized_url(url, size):
    """`url` naming a `size`×`size` px image: Apple's templates and sized URLs both take it;
    any other URL is left as it is."""
    if not url:
        return None
    url = normalize.template_artwork_url(str(url), size, size)
    return ART_SIZE_RE.sub(lambda match: f'/{size}x{size}{match.group(3)}{match.group(4)}', url)


def remote_art_path(url, size=config.COVER_SIZE):
    """Where fetch_remote keeps the image at `url` re-sized to `size`: under <cache>/remote-art/,
    named by the sized URL's hash. None without a URL."""
    url = sized_url(url, size)
    if not url:
        return None
    return os.path.join(str(config.cache_dir()), 'remote-art', normalize.artwork_filename(url))


def remote_item(data):
    """An Item dict from the engine's search, category, browse or made-for-you answers with
    its artwork where this app fetches it: when `art` is a catalog URL (the sync has not
    fetched the cover), a copy naming fetch_remote()'s files instead (`thumb` at THUMB_SIZE
    unless the dict names one, `art` at COVER_SIZE) and the URLs to fetch them by (`thumbUrl`,
    `artUrl`, for fetch_thumb() and fetch_cover()). A dict whose artwork is a file, or that
    has none, is returned as it is."""
    url = data.get('art') if isinstance(data, dict) else None
    if not is_url(url):
        return data
    item = dict(data)
    item['thumbUrl'] = sized_url(url, config.THUMB_SIZE)
    item['artUrl'] = sized_url(url, config.COVER_SIZE)
    if not item.get('thumb'):
        item['thumb'] = remote_art_path(url, config.THUMB_SIZE)
    item['art'] = remote_art_path(url, config.COVER_SIZE)
    return item


def needs_thumb(item):
    """Whether an Item (remote_item's) names a thumbnail it can fetch: a path and an https URL.
    Whether the file is on disk already is fetch_thumb()'s to find out, off the main loop."""
    raw = item.raw if isinstance(item.raw, dict) else {}
    return bool(item.thumb and is_url(raw.get('thumbUrl')))


def on_disk(path):
    """Whether an artwork file is on disk, and not empty. It asks the file system: call it
    in a thread."""
    try:
        return os.path.getsize(path) > 0
    except (OSError, TypeError, ValueError):
        return False


# -- fetching ------------------------------------------------------------------------------

_executor = None
_fetches = {}  # path -> the asyncio.Task fetching it


def _fetch_executor():
    global _executor
    if _executor is None:
        _executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=FETCH_WORKERS, thread_name_prefix='art-fetch')
    return _executor


async def fetch_cover(item):
    """The Item's cover (`art`) on disk: True when this call brought it (from the Item's
    `artUrl`), False when it was there already, the Item has no cover URL, or the fetch
    failed (logged; the page keeps the thumbnail). Concurrent calls for one cover share the
    download, and each answers True."""
    raw = item.raw if isinstance(item.raw, dict) else {}
    return await _fetch(item.art, raw.get('artUrl')) is True


async def fetch_thumb(item):
    """The thumbnail of an Item from a search or browse answer (remote_item()) on disk: True
    when this call brought it (from the Item's `thumbUrl`), False when it was there already,
    the Item names none, or the fetch failed."""
    if not needs_thumb(item):
        return False
    return await _fetch(item.thumb, item.raw.get('thumbUrl')) is True


async def fetch_remote(url, size=config.COVER_SIZE):
    """The image at `url` (an Apple artwork URL of any size), re-sized to `size` px square and
    on disk under <cache>/remote-art/: its path, once it is there (fetched when it was not), or
    None without an https URL or when the fetch failed (logged). Concurrent calls for one image
    share the download. The player bar asks at the cover size, so the Now Playing sheet and
    MPRIS find the same file."""
    path = remote_art_path(url, size)
    return path if await _fetch(path, sized_url(url, size)) is not None else None


async def _fetch(path, url):
    """Bring `url` to `path` (inside the cache): True when this call's download (or the one
    it shared) brought it, False when it was on disk, None when it could not (no path, not an
    https URL, or a failed download)."""
    if not path or not is_url(url):
        return None
    task = _fetches.get(path)
    if task is None:
        generation = store.cache_generation()  # a wipe from here on keeps this file out
        task = asyncio.get_running_loop().create_task(_download(path, url, generation))
        _fetches[path] = task

        def forget(done):
            if _fetches.get(path) is done:
                del _fetches[path]

        task.add_done_callback(forget)
    return await asyncio.shield(task)


async def _download(path, url, generation):
    def download():
        if on_disk(path):
            return False
        cached = normalize.cache_artwork(url, str(config.cache_dir()), dest_path=path,
                                         generation=generation)
        return True if cached is not None else None

    try:
        fetched = await asyncio.get_running_loop().run_in_executor(_fetch_executor(), download)
    except Exception:
        log.exception('Cannot fetch the artwork %s', path)
        return None
    if fetched:
        log.debug('fetched the artwork %s', path)
    return fetched


# -- the engine's shelves ------------------------------------------------------------------

def shelf_title(data):
    """A shelf's heading in the app's language: the name of its kind for a search's shelf
    (SEARCH_TITLES, by key), 'Featured' for the New page's banners, Apple's own title for the
    rest (it comes in the account's language), and 'Made for You' for a recommendation that
    has none."""
    if data.get('featured'):
        # Translators: the shelf of banners at the top of the New page.
        return _('Featured')
    title = SEARCH_TITLES.get(str(data.get('key') or ''))
    if title is not None:
        return _(title)
    return str(data.get('title') or '') or _('Made for You')


def remote_shelves(dicts):
    """library.Shelf objects for the engine's shelf dicts ({key, title, items}), titled by
    shelf_title(), the items wrapped as Items with their artwork under remote-art
    (remote_item()); a shelf with nothing in it, or an item without an id and a kind, is left
    out."""
    shelves = []
    for data in dicts or []:
        if not isinstance(data, dict):
            continue
        items = [Item(remote_item(entry)) for entry in data.get('items') or []
                 if isinstance(entry, dict) and entry.get('id') and entry.get('kind')]
        if items:
            shelves.append(Shelf(str(data.get('key') or ''), shelf_title(data), items))
    return shelves


async def fetch_shelf_art(shelves):
    """Fetch the thumbnails of the shelves' items that are not on disk, ART_CONCURRENCY at a
    time, and rebind each item's tile as its file arrives (the item spliced over itself in its
    shelf's store)."""
    gate = asyncio.Semaphore(ART_CONCURRENCY)

    async def fetch(store, item):
        async with gate:
            if await fetch_thumb(item):
                found, position = store.find(item)
                if found:
                    store.splice(position, 1, [item])

    await asyncio.gather(*(fetch(shelf.items, item) for shelf in shelves
                           for item in list(shelf.items) if needs_thumb(item)))
