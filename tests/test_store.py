"""backend/store.py: the one atomic write every cache file goes through, and what the cache's
writers and pruners do with it."""

import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend import normalize, store


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = pathlib.Path(tmp.name) / 'cache'
        self.cache.mkdir()

    def names(self, folder=None):
        return sorted(os.listdir(folder or self.cache))

    def age(self, path, seconds):
        when = time.time() - seconds
        os.utime(path, (when, when))


class AtomicWriteTest(StoreTest):
    def test_a_write_replaces_the_file(self):
        path = self.cache / 'answer.json'
        path.write_text('old')
        self.assertEqual(store.atomic_write(path, lambda f: f.write('new'), text=True), path)
        self.assertEqual(path.read_text(), 'new')
        self.assertEqual(self.names(), ['answer.json'])

    def test_a_failing_write_leaves_the_old_file_and_no_temp(self):
        path = self.cache / 'answer.json'
        path.write_text('old')

        def half(file):
            file.write('{"half": ')
            raise OSError(28, 'No space left on device')
        with self.assertRaises(OSError):
            store.atomic_write(path, half, text=True)
        self.assertEqual(path.read_text(), 'old')
        self.assertEqual(self.names(), ['answer.json'])

    def test_a_failing_create_leaves_no_temp(self):
        def scaler(temp):
            pathlib.Path(temp).write_bytes(b'part')
            raise ValueError('not an image')
        with self.assertRaises(ValueError):
            store.atomic_create(self.cache / 'thumb' / 'x.jpg', scaler)
        self.assertEqual(self.names(self.cache / 'thumb'), [])

    def test_the_temporary_file_is_a_dot_temp_beside_the_target(self):
        seen = []

        def write(file):
            seen.extend(os.listdir(self.cache))
            file.write(b'x')
        store.atomic_write(self.cache / 'x.jpg', write, fsync=False)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith('.') and store.is_temp(seen[0]))

    def test_concurrent_writers_of_one_path_never_mix(self):
        path = self.cache / 'answer.json'
        writers = 8
        barrier = threading.Barrier(writers)
        errors = []

        def write(number):
            def fill(file):
                barrier.wait()  # every writer has its temporary file open at once
                for _ in range(200):
                    file.write(f'{number}' * 50)
            try:
                store.atomic_write(path, fill, text=True)
            except Exception as error:  # reported below
                errors.append(error)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        text = path.read_text()
        self.assertEqual(len(set(text)), 1)  # one writer's, whole
        self.assertEqual(len(text), 200 * 50)
        self.assertEqual(self.names(), ['answer.json'])

    def test_stale_temps(self):
        fresh = self.cache / '.fresh.tmp'
        old = self.cache / '.old.tmp'
        other = self.cache / 'library.json'
        for path in (fresh, old, other):
            path.write_text('x')
        self.age(old, store.TEMP_MAX_AGE + 60)
        self.age(other, store.TEMP_MAX_AGE + 60)
        self.assertFalse(store.is_stale_temp(fresh))
        self.assertTrue(store.is_stale_temp(old))
        self.assertFalse(store.is_stale_temp(other))
        self.assertFalse(store.is_stale_temp(self.cache / '.gone.tmp'))


class GenerationTest(StoreTest):
    """Nothing a job of a wiped cache writes lands, not even a directory."""

    def old_generation(self):
        generation = store.cache_generation()
        store.bump_cache_generation()
        return generation

    def test_the_bump(self):
        generation = store.cache_generation()
        self.assertTrue(store.current(generation))
        self.assertTrue(store.current(None))  # a writer that does not ask
        self.assertEqual(store.bump_cache_generation(), generation + 1)
        self.assertFalse(store.current(generation))
        self.assertTrue(store.current(store.cache_generation()))

    def test_a_current_write_lands(self):
        path = self.cache / 'lyrics' / '1.json'
        generation = store.cache_generation()
        self.assertEqual(store.atomic_write(path, lambda f: f.write('{}'), text=True,
                                            generation=generation), path)
        self.assertEqual(path.read_text(), '{}')

    def test_an_old_write_makes_nothing(self):
        generation = self.old_generation()
        path = self.cache / 'lyrics' / '1.json'
        self.assertIsNone(store.atomic_write(path, lambda f: f.write('{}'), text=True,
                                             generation=generation))
        self.assertIsNone(store.atomic_create(self.cache / 'thumb' / 'x.jpg', lambda temp: None,
                                              generation=generation))
        self.assertFalse(store.make_dirs(self.cache / 'art', generation))
        self.assertEqual(self.names(), [])

    def test_a_bump_during_the_write_drops_it(self):
        path = self.cache / 'answer.json'
        path.write_text('old')
        generation = store.cache_generation()

        def write(file):
            file.write('new')
            store.bump_cache_generation()  # Clear Cache, while this one was writing
        self.assertIsNone(store.atomic_write(path, write, text=True, generation=generation))
        self.assertEqual(path.read_text(), 'old')
        self.assertEqual(self.names(), ['answer.json'])  # and no temporary file

    def test_a_directory_wiped_under_the_write(self):
        generation = store.cache_generation()
        folder = self.cache / 'categories'
        real = store.os.makedirs

        def makedirs_then_wipe(path, exist_ok=False):
            real(path, exist_ok=exist_ok)
            store._generation += 1  # the bump, and the wipe right after it
            folder.rmdir()
        with mock.patch.object(store.os, 'makedirs', makedirs_then_wipe):
            self.assertIsNone(store.atomic_write(folder / 'c1.json', lambda f: None,
                                                 generation=generation))
        self.assertEqual(self.names(), [])

    def test_the_cache_writers_after_a_wipe(self):
        generation = self.old_generation()
        cache = str(self.cache)
        answer = normalize.write_answer(str(self.cache / 'landing.json'), {'categories': []},
                                        generation=generation)
        self.assertIn('cached', answer)  # the answer is the caller's, only not kept
        with self.assertRaises(store.CacheGone):
            normalize.save_library({'version': 1}, cache, generation=generation)
        with mock.patch('urllib.request.urlopen') as urlopen:
            self.assertIsNone(normalize.cache_artwork('https://x.invalid/a.jpg', cache,
                                                      generation=generation))
            with self.assertRaises(store.CacheGone):
                normalize.download_art({str(self.cache / 'thumb' / 'b.jpg'): 'https://x/b.jpg'},
                                       cache, generation=generation)
        urlopen.assert_not_called()
        self.assertEqual(self.names(), [])

    def test_a_download_the_wipe_overtakes(self):
        generation = store.cache_generation()

        class Answer:
            status = 200

            def __init__(self):
                self.chunks = [b'part of a cover', b'']

            def read(self, size=-1):
                store.bump_cache_generation()  # signed out while the cover came in
                return self.chunks.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        with mock.patch('urllib.request.urlopen', lambda request, timeout: Answer()):
            self.assertIsNone(normalize.cache_artwork('https://x.invalid/a.jpg', str(self.cache),
                                                      generation=generation))
        self.assertEqual(self.names(self.cache / 'art'), [])


class WritersTest(StoreTest):
    """The cache's writers go through the one write."""

    def test_save_library_that_fails_leaves_the_old_library(self):
        normalize.save_library({'version': 1, 'sections': {}}, str(self.cache))
        before = (self.cache / 'library.json').read_text()

        def half_dump(data, file, **kwargs):
            file.write('{"version": 1, "sect')
            raise OSError(28, 'No space left on device')
        with mock.patch.object(normalize.json, 'dump', half_dump):
            with self.assertRaises(OSError):
                normalize.save_library({'version': 1, 'sections': {'albums': []}},
                                       str(self.cache))
        self.assertEqual((self.cache / 'library.json').read_text(), before)
        self.assertEqual(self.names(), ['library.json'])

    def test_a_kept_answer_that_fails_is_logged_and_leaves_no_temp(self):
        path = self.cache / 'landing.json'

        def half_dump(data, file, **kwargs):
            file.write('{"categ')
            raise OSError(28, 'No space left on device')
        with mock.patch.object(normalize.json, 'dump', half_dump):
            with self.assertLogs('applemusic.backend.normalize', 'WARNING'):
                answer = normalize.write_answer(str(path), {'categories': []})
        self.assertIn('cached', answer)  # the answer is still the caller's
        self.assertEqual(self.names(), [])

    def test_the_sizes_marker(self):
        normalize.apply_art_sizes(str(self.cache), 640, 320)
        self.addCleanup(normalize.ART_SIZES.update, normalize.DEFAULT_ART_SIZES)
        marker = self.cache / 'art' / '.sizes'
        self.assertEqual(json.loads(marker.read_text()), {'cover': 640, 'thumb': 320})
        self.assertEqual(self.names(self.cache / 'art'), ['.sizes'])


class PruneTempsTest(StoreTest):
    """A temporary file is a download in progress until it is an hour old."""

    def test_prune_art_keeps_a_fresh_temp_and_removes_an_old_one(self):
        art = self.cache / 'art'
        art.mkdir()
        fresh, old, marker = art / '.a1b2.tmp', art / '.c3d4.tmp', art / '.sizes'
        for path in (fresh, old, marker):
            path.write_text('x')
        self.age(old, store.TEMP_MAX_AGE + 60)
        self.age(marker, store.TEMP_MAX_AGE + 60)
        library = {'sections': {}, 'shelves': []}
        self.assertEqual(normalize.prune_art(library, str(self.cache)), 1)
        self.assertEqual(self.names(art), ['.a1b2.tmp', '.sizes'])

    def test_prune_remote_art_does_the_same(self):
        remote = self.cache / 'remote-art'
        remote.mkdir()
        fresh, old, cover = remote / '.a1b2.tmp', remote / '.c3d4.tmp', remote / 'x.jpg'
        for path in (fresh, old, cover):
            path.write_bytes(b'x' * 10)
        self.age(old, store.TEMP_MAX_AGE + 60)
        self.assertEqual(normalize.prune_remote_art(str(self.cache), max_bytes=100), 1)
        self.assertEqual(self.names(remote), ['.a1b2.tmp', 'x.jpg'])


if __name__ == '__main__':
    unittest.main()
