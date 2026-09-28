"""Writing the cache: every file through one atomic write.

    store.atomic_write(path, lambda file: json.dump(data, file), text=True)
    store.atomic_create(path, lambda temp: scale(src, temp))   # a writer that wants a path

A write goes to a temporary file beside its target, with a name of its own that starts with
a dot (so two writers of one path never share one, and the artwork pruners leave a file
alone while it is written), is flushed to the disk, then renamed over the target: a reader
sees the old file or the new one, never half of one. Whatever fails, the temporary file goes
and the error is the caller's. A temporary file older than TEMP_MAX_AGE is a crash's
leftover (is_stale_temp()), which the pruners remove.

The standard library only, and blocking: call these in a thread.
"""

import logging
import os
import tempfile
import time

log = logging.getLogger(__name__)

TEMP_PREFIX = '.'
TEMP_SUFFIX = '.tmp'
TEMP_MAX_AGE = 60 * 60  # a temporary file this old is no write in progress


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


def atomic_write(path, write, text=False, fsync=True):
    """Write `path` atomically: `write(file)` fills a temporary file beside it (opened for
    bytes, or for UTF-8 text with `text`), which is flushed, fsync'd (unless `fsync` is off:
    for files that are only a copy, such as artwork) and renamed over `path`. Returns `path`.
    An exception from `write`, or from the disk, is raised after the temporary file is
    removed; `path` is then as it was."""
    def fill(temp, fd):
        with open(fd, 'w' if text else 'wb', **({'encoding': 'utf-8'} if text else {})) as file:
            write(file)
            file.flush()
            if fsync:
                os.fsync(file.fileno())
    return _replace(path, fill)


def atomic_create(path, make, fsync=False):
    """Write `path` atomically through a writer that wants a file name: `make(temp_path)`
    writes the temporary file (a thumbnail scaler, say), which is then renamed over `path`,
    as atomic_write does. Not fsync'd unless asked. Returns `path`."""
    def fill(temp, fd):
        os.close(fd)
        make(temp)
        if fsync:
            _fsync_path(temp)
    return _replace(path, fill)


def _replace(path, fill):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX)
    try:
        fill(temp, fd)
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
