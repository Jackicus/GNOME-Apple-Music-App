"""src/cache.py: the cache directory's size, clearing it, and the JSON kept in it."""

import json
import os
import pathlib
import tempfile
import time
import unittest

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from applemusic import cache
from applemusic.backend import normalize


class CacheTest(unittest.TestCase):
    """cache_size() and clear() over an invented cache directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.cache = self.root / 'cache'
        files = {
            'library.json': 100, 'library.lock': 0, 'landing.json': 10, 'browse.json': 10,
            'made-for-you.json': 10, 'art/l.alb1.jpg': 1000, 'art/.sizes': 20,
            'thumb/l.alb1.jpg': 300, 'remote-art/abc.jpg': 400, 'items/album-1.json': 50,
            'lyrics/1000000001.json': 30, 'categories/c1.json': 40,
            # Writes that never finished: an older version's temporary name, and store.py's.
            'library.json.tmp': 60, '.a1b2c3.tmp': 7, 'lyrics/.d4e5.tmp': 3,
            # Not the app's.
            'notes.txt': 5,
        }
        for name, size in files.items():
            path = self.cache / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * size)
        self.total = sum(files.values())
        # A link to something outside: neither counted nor followed, only removed.
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        (self.outside / 'big.bin').write_bytes(b'x' * 5000)
        (self.cache / 'art' / 'elsewhere').symlink_to(self.outside)

    def test_size_adds_up_the_files_leftovers_included(self):
        self.assertEqual(cache.cache_size(self.cache), self.total)
        self.assertEqual(cache.cache_size(self.root / 'missing'), 0)

    def test_clear_removes_the_cache_and_its_leftovers_and_leaves_the_rest(self):
        removed = cache.clear(self.cache)
        self.assertEqual(removed, len(cache.CACHE_ENTRIES) + 2)  # the two temporary files
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()), ['notes.txt'])
        self.assertTrue((self.outside / 'big.bin').is_file())  # the link's target stays
        self.assertEqual(cache.cache_size(self.cache), 5)
        self.assertEqual(cache.clear(self.cache), 0)  # nothing left to clear
        self.assertEqual(cache.clear(self.root / 'missing'), 0)


class KeptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = pathlib.Path(tmp.name)

    def test_read_kept_ages_out(self):
        path = self.cache / 'landing.json'
        self.assertIsNone(cache.read_kept(path))
        normalize.write_answer(str(path), {'categories': []}, str(self.cache))
        self.assertEqual(cache.read_kept(path)['categories'], [])
        kept = json.loads(path.read_text())
        kept['cached'] = '2020-01-01T00:00:00Z'
        path.write_text(json.dumps(kept))
        self.assertIsNone(cache.read_kept(path))
        self.assertEqual(cache.read_kept(path, max_age=10 ** 10)['categories'], [])
        path.write_text('{not json')
        self.assertIsNone(cache.read_kept(path))

    def test_a_touched_hit_is_the_newest(self):
        path = self.cache / 'lyrics' / '1.json'
        normalize.write_answer(str(path), {'lines': [{'text': 'x'}]}, str(self.cache))
        os.utime(path, (1000, 1000))
        cache.read_kept(path)
        self.assertEqual(path.stat().st_mtime, 1000)  # a plain read leaves it
        cache.read_kept(path, touch=True)
        self.assertGreater(path.stat().st_mtime, time.time() - 60)


if __name__ == '__main__':
    unittest.main()
