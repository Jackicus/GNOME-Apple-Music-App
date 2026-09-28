"""The cache directory (config.cache_dir()): what it holds, what it weighs, clearing it, and the
JSON answers kept in it. No GTK; every function blocks, so call it in a thread.

    size = await asyncio.to_thread(cache_size, config.cache_dir())
    removed = await asyncio.to_thread(clear, config.cache_dir())
    answer = await asyncio.to_thread(read_kept, path)   # a kept answer under a day old, or None

Every file in it is written through backend/store.py; what is here reads and removes.
"""

import json
import logging
import os
import shutil

from .backend import normalize, store

log = logging.getLogger(__name__)

# What the cache directory holds, all of it fetched again as needed: the library, its artwork
# (covers, thumbnails, remote art), lyrics, and the day-long answers (landing, categories, the
# New page, Made for You). `items` and `library.lock` are what older versions kept there
# (items' answers, the sync's lock), cleared with the rest.
CACHE_ENTRIES = ('library.json', 'art', 'thumb', 'remote-art', 'lyrics', 'landing.json',
                 'categories', 'browse.json', 'made-for-you.json', 'items', 'library.lock')


def cache_size(path):
    """The bytes the files under `path` hold (their sizes; symlinks are not followed), 0 when
    it is not there. Everything counts, a crash's temporary files too."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total


def clear(path):
    """Delete what the cache at `path` holds: CACHE_ENTRIES, and the temporary files of writes
    that never finished (`*.tmp`, store.py's and older versions'), but nothing else that may
    be there. Answers how many entries went; what cannot be deleted is logged and left. The
    caller bumps the cache's generation first (store.bump_cache_generation), so no write in
    flight puts anything back."""
    path = str(path)
    names = list(CACHE_ENTRIES)
    try:
        names += sorted(name for name in os.listdir(path)
                        if store.is_temp(name) and name not in CACHE_ENTRIES)
    except OSError:
        pass  # not there: nothing to clear
    removed = 0
    for name in names:
        target = os.path.join(path, name)
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            elif os.path.lexists(target):
                os.remove(target)
            else:
                continue
            removed += 1
        except OSError as error:
            log.warning('could not remove %s: %s', target, error)
    log.info('cache cleared: %d entries were there', removed)
    return removed


def read_json(path):
    """The JSON at path, or None when it is not there or not JSON."""
    try:
        with open(path, encoding='utf-8') as file:
            return json.load(file)
    except (OSError, ValueError):
        return None


def write_json(path, data, root, generation=None):
    """`data` as JSON at path (under the cache `root`), atomically, the directory made first,
    unless the cache was cleared since `generation`; a failure is logged."""
    try:
        store.atomic_write(path, lambda file: json.dump(data, file, ensure_ascii=False),
                           root=root, text=True, generation=generation)
    except (OSError, ValueError) as error:
        log.warning('could not keep %s: %s', path, error)


def read_kept(path, max_age=normalize.ANSWER_MAX_AGE):
    """The answer kept at path (normalize.write_answer's, stamped `cached`) when it is younger
    than `max_age` seconds, else None."""
    answer = normalize.read_answer(path, max_age)
    return answer if isinstance(answer, dict) else None
