# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Writing the cache: every file through one atomic write.

    store.atomic_write(path, lambda file: json.dump(data, file), root=cache_dir, text=True)
    store.atomic_create(path, lambda temp: scale(src, temp), root=cache_dir)   # wants a path

A write goes to a temporary file beside its target, with a name of its own that starts with
a dot (so two writers of one path never share one, and the artwork pruners leave a file
alone while it is written), is flushed to the disk, then renamed over the target: a reader
sees the old file or the new one, never half of one. Whatever fails, the temporary file goes
and the error is the caller's. A temporary file older than TEMP_MAX_AGE is a crash's
leftover (is_stale_temp()), which the pruners remove.

The cache has a generation (cache_generation()). Clear Cache and sign-out bump it
(bump_cache_generation()) before they delete anything. A job that writes the cache (a sync,
an item's artwork, a kept answer, a cover the pages fetch) takes the generation on the main
thread when it starts and passes it to every write; a write whose generation has moved is
dropped (None) and makes no directory, so nothing fetched for the old cache lands in the new
one. The check and the rename hold one lock, as does the bump: once the bump returns, no
write of an older generation can land. A job that must stop there raises CacheGone.

Every write is confined to the cache: `root`, the cache directory, is given with each one,
and a path outside it (`..` included) is refused with ValueError, so a path that came from an
answer can never name a file elsewhere. Directories are made 0700 and files 0600, whatever
the umask would give, so the library and its artwork stay the user's own.

The standard library only, and blocking: call these in a thread (the generation's two
functions are cheap anywhere).
"""

import logging
import os
import tempfile
import threading
import time

log = logging.getLogger(__name__)

_lock = threading.Lock()
_generation = 0

TEMP_PREFIX = '.'
TEMP_SUFFIX = '.tmp'
TEMP_MAX_AGE = 60 * 60  # a temporary file this old is no write in progress
DIR_MODE = 0o700  # and files 0600: mkstemp's


class Cancelled(Exception):
    """A job that writes the cache gave up before it was done: asked to stop, or its cache
    was wiped (CacheGone). Nothing more of it is written."""


class CacheGone(Cancelled):
    """The cache was wiped while a job ran (its generation moved): the job stops."""


def cache_generation():
    """The cache's generation now: a job takes it when it starts, on the main thread, and
    passes it to its writes."""
    return _generation


def bump_cache_generation():
    """A new generation, before the cache is wiped: from the moment this returns, no write of
    an older generation lands and none makes a directory. Returns the new generation."""
    global _generation
    with _lock:
        _generation += 1
        return _generation


def current(generation):
    """Whether a job of `generation` may still write (None: a writer that does not ask)."""
    return generation is None or generation == _generation


def inside(path, root, or_root=False):
    """Whether `path` is under `root` (both made absolute and normalised, so `..` cannot
    climb out); with `or_root`, `root` itself counts too."""
    path, root = os.path.abspath(path), os.path.abspath(root)
    return os.path.commonpath([path, root]) == root and (or_root or path != root)


def make_dirs(directory, *, root, generation=None):
    """Make `directory` (the cache `root` or a folder under it) and whatever of it is missing,
    each DIR_MODE, unless `generation` has moved: False then, and nothing made (a job of a
    wiped cache must not bring its directories back). ValueError outside `root`."""
    directory, root = os.path.abspath(directory), os.path.abspath(root)
    if not inside(directory, root, or_root=True):
        raise ValueError(f'{directory} is outside the cache')
    with _lock:
        if not current(generation):
            return False
        missing = []
        path = directory
        while not os.path.isdir(path):
            missing.append(path)
            if path == root:
                os.makedirs(os.path.dirname(root), exist_ok=True)  # the cache's parents
                break
            path = os.path.dirname(path)
        for path in reversed(missing):
            try:
                os.mkdir(path, DIR_MODE)
            except FileExistsError:
                pass
    return True


def is_temp(name):
    """Whether a file name is a temporary file's (one of atomic_write's, or an older
    version's `<name>.tmp`)."""
    return name.endswith(TEMP_SUFFIX)


def is_stale_temp(path, now=None):
    """Whether `path` is a temporary file left behind: a temp by name, older than
    TEMP_MAX_AGE (its mtime). A missing file is not."""
    if not is_temp(os.path.basename(path)):
        return False
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return False
    return (now if now is not None else time.time()) - mtime > TEMP_MAX_AGE


def atomic_write(path, write, *, root, text=False, fsync=True, generation=None):
    """Write `path`, under the cache `root`, atomically: `write(file)` fills a temporary file
    beside it (opened for bytes, or for UTF-8 text with `text`), which is flushed, fsync'd
    (unless `fsync` is off: for files that are only a copy, such as artwork) and renamed over
    `path`. Returns `path`, or None when `generation` moved first (nothing written). An
    exception from `write`, or from the disk, is raised after the temporary file is removed;
    `path` is then as it was. ValueError for a path outside `root`."""
    def fill(temp, fd):
        with open(fd, 'w' if text else 'wb', **({'encoding': 'utf-8'} if text else {})) as file:
            write(file)
            file.flush()
            if fsync:
                os.fsync(file.fileno())
    return _replace(path, fill, root, generation)


def atomic_create(path, make, *, root, fsync=False, generation=None):
    """Write `path` atomically through a writer that wants a file name: `make(temp_path)`
    writes the temporary file (a thumbnail scaler, say), which is then renamed over `path`,
    as atomic_write does. Not fsync'd unless asked. Returns `path`, or None when
    `generation` moved."""
    def fill(temp, fd):
        os.close(fd)
        make(temp)
        if fsync:
            _fsync_path(temp)
    return _replace(path, fill, root, generation)


def _replace(path, fill, root, generation):
    if not inside(path, root):
        raise ValueError(f'{path} is outside the cache')
    directory = os.path.dirname(os.path.abspath(path))
    if not make_dirs(directory, root=root, generation=generation):
        log.debug('%s: the cache was cleared, not written', path)
        return None
    try:
        fd, temp = tempfile.mkstemp(dir=directory, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX)
    except FileNotFoundError:
        if current(generation):
            raise
        log.debug('%s: the cache was cleared, not written', path)
        return None  # wiped between the two
    try:
        fill(temp, fd)
        with _lock:
            if not current(generation):
                log.debug('%s: the cache was cleared, not written', path)
                return None
            os.replace(temp, path)
        return path
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass  # renamed: the usual end
        except OSError as error:
            log.debug('temporary file %s: %s', temp, error)


def _fsync_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
