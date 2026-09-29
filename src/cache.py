# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The cache directory (config.cache_dir()): what it holds, what it weighs, clearing it, and the
JSON answers kept in it; and removing a directory Chrome may still be writing to (the profile, at
sign-out). No GTK; every function blocks, so call it in a thread.

    size = await asyncio.to_thread(cache_size, config.cache_dir())
    removed = await asyncio.to_thread(clear, config.cache_dir())
    answer = await asyncio.to_thread(read_kept, path)   # a kept answer under a day old, or None
    answer = await asyncio.to_thread(read_kept, path, allow_stale=True)   # older: `stale`
    await asyncio.to_thread(remove_trees, engine.profile_dir)   # a directory, retried

Every file in it is written through backend/store.py (normalize.write_answer for the kept
answers and lyrics); what is here reads and removes. normalize.prune_caches trims it.
"""

import logging
import math
import os
import shutil
import time

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


def read_kept(path, max_age=normalize.ANSWER_MAX_AGE, touch=False, allow_stale=False):
    """The answer kept at path (normalize.write_answer's, stamped `cached`) when it is younger
    than `max_age` seconds, else None. With `allow_stale`, an older one too, marked
    `stale: True` (what the Engine answers when Apple cannot be asked). With `touch`, a hit
    sets the file's mtime to now, so the pruner keeps what was used last (the lyrics)."""
    answer = normalize.read_answer(path, max_age)
    if answer is None and allow_stale:
        answer = normalize.read_answer(path, math.inf)
        if isinstance(answer, dict):
            answer['stale'] = True
    if not isinstance(answer, dict):
        return None
    if touch:
        try:
            os.utime(path)
        except OSError as error:
            log.debug('kept answer %s: %s', path, error)
    return answer


def remove_trees(*paths, attempts=4, pause=0.5):
    """Delete directories (in a thread), leaving anything that cannot be deleted. Chrome's
    helper processes write to the profile for a moment after the browser process has exited
    (its network service re-created `Default/Network Persistent State` in one run), so a
    directory that comes back is removed again, a few times, `pause` seconds apart."""
    for path in paths:
        if not path:
            continue
        for attempt in range(attempts):
            if not os.path.isdir(path):
                break
            if attempt:
                time.sleep(pause)
            log.info('removing %s', path)
            shutil.rmtree(path, ignore_errors=True)
        if os.path.isdir(path):
            log.warning('%s could not be removed entirely', path)
