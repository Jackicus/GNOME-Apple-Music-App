"""remote.py: artwork URLs and paths, the shared downloads, and the engine's shelves.

Runs under asyncio.run, without GTK: the downloads are normalize.cache_artwork's, replaced by a
stand-in that writes a few bytes (or fails, for a URL with "missing" in it).
"""

import asyncio
import os
import tempfile
import threading
import unittest
from unittest import mock

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic import remote
from applemusic.backend import store
from applemusic.library import Item


def run(coroutine):
    return asyncio.run(coroutine)


class RemoteTestCase(unittest.TestCase):
    """A cache of its own, and cache_artwork replaced: `fetched` lists the URLs asked for."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.temp_dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fetched = []
        self.generations = []
        self.threads = []
        self.gate = None  # a threading.Event a download waits for, when set

        def fake_cache_artwork(url, cache_dir, timeout=10.0, dest_path=None, generation=None):
            self.fetched.append(url)
            self.generations.append(generation)
            self.threads.append(threading.current_thread().name)
            if self.gate is not None:
                self.gate.wait(5)
            if 'missing' in url:
                return None
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, 'wb') as file:
                file.write(b'jpeg')
            return dest_path

        patcher = mock.patch.object(remote.normalize, 'cache_artwork', fake_cache_artwork)
        patcher.start()
        self.addCleanup(patcher.stop)

    def path(self, *parts):
        return os.path.join(self.temp_dir.name, *parts)


class UrlTest(RemoteTestCase):
    def test_sized_url(self):
        self.assertEqual(remote.sized_url('https://x.invalid/a/256x256bb.jpg', 640),
                         'https://x.invalid/a/640x640bb.jpg')
        self.assertEqual(remote.sized_url('https://x.invalid/a/{w}x{h}bb.jpg', 320),
                         'https://x.invalid/a/320x320bb.jpg')
        self.assertEqual(remote.sized_url('https://x.invalid/a/600x600bb-60.webp', 640),
                         'https://x.invalid/a/640x640bb-60.webp')
        self.assertEqual(remote.sized_url('https://x.invalid/a/cover.jpg', 640),
                         'https://x.invalid/a/cover.jpg')
        self.assertIsNone(remote.sized_url(None, 640))
        self.assertIsNone(remote.sized_url('', 640))

    def test_only_https_is_a_url(self):
        self.assertTrue(remote.is_url('https://x.invalid/a.jpg'))
        for value in ('http://x.invalid/a.jpg', '/cache/art/a.jpg', 'file:///a.jpg', '', None, 3):
            self.assertFalse(remote.is_url(value), value)

    def test_remote_item_names_the_remote_art_files(self):
        hit = {'id': '1', 'kind': 'album', 'title': 'A', 'thumb': None,
               'art': 'https://x.invalid/a/320x320bb.jpg'}
        item = remote.remote_item(hit)
        self.assertIsNot(item, hit)
        self.assertEqual(item['thumbUrl'], 'https://x.invalid/a/320x320bb.jpg')
        self.assertEqual(item['artUrl'], 'https://x.invalid/a/640x640bb.jpg')
        self.assertEqual(item['thumb'], remote.remote_art_path(hit['art'], 320))
        self.assertEqual(item['art'], remote.remote_art_path(hit['art'], 640))
        self.assertEqual(os.path.dirname(item['thumb']), self.path('remote-art'))
        self.assertNotEqual(item['thumb'], item['art'])
        # A thumbnail the sync has on disk is kept; the cover still comes from the URL.
        kept = remote.remote_item(dict(hit, thumb='/on/disk.jpg'))
        self.assertEqual(kept['thumb'], '/on/disk.jpg')
        self.assertEqual(kept['art'], item['art'])
        # Artwork on disk, or none, or not fetched from an https URL: the dict as it is.
        for data in ({'art': '/cache/art/x.jpg', 'thumb': '/cache/thumb/x.jpg'},
                     {'art': 'http://x.invalid/a/320x320bb.jpg'},
                     {'art': None, 'thumb': None}, {}, None):
            self.assertIs(remote.remote_item(data), data)

    def test_needs_thumb_asks_no_disk(self):
        item = Item(remote.remote_item({'id': '1', 'kind': 'album', 'title': 'A',
                                        'art': 'https://x.invalid/a/320x320bb.jpg'}))
        self.assertTrue(remote.needs_thumb(item))
        self.assertFalse(remote.needs_thumb(Item({'id': '2', 'kind': 'album', 'title': 'B',
                                                  'thumb': '/cache/thumb/b.jpg'})))
        with mock.patch.object(remote.os.path, 'getsize') as getsize:
            remote.needs_thumb(item)
        getsize.assert_not_called()

    def test_on_disk(self):
        present, empty = self.path('present.jpg'), self.path('empty.jpg')
        with open(present, 'wb') as file:
            file.write(b'x')
        open(empty, 'wb').close()
        self.assertTrue(remote.on_disk(present))
        self.assertFalse(remote.on_disk(empty))
        self.assertFalse(remote.on_disk(self.path('absent.jpg')))
        self.assertFalse(remote.on_disk(None))


class FetchRemoteTest(RemoteTestCase):
    """fetch_remote: the URL re-sized, the file under <cache>/remote-art/, shared fetches."""

    def test_fetches_into_remote_art_once(self):
        url = 'https://x.invalid/a/256x256bb.jpg'

        async def go():
            first, second = await asyncio.gather(remote.fetch_remote(url, 640),
                                                 remote.fetch_remote(url, 640))
            self.assertEqual(first, second)
            self.assertEqual(os.path.dirname(first), self.path('remote-art'))
            self.assertTrue(os.path.exists(first))
            self.assertEqual(self.fetched, ['https://x.invalid/a/640x640bb.jpg'])
            # On disk already: answered without a download.
            self.assertEqual(await remote.fetch_remote(url, 640), first)
            self.assertEqual(len(self.fetched), 1)
            # Another size is another file.
            other = await remote.fetch_remote(url, 320)
            self.assertNotEqual(other, first)
            self.assertEqual(self.fetched[-1], 'https://x.invalid/a/320x320bb.jpg')
        run(go())

    def test_a_fetch_writes_for_the_cache_it_began_in(self):
        generation = store.cache_generation()
        run(remote.fetch_remote('https://x.invalid/a/256x256bb.jpg', 640))
        self.assertEqual(self.generations, [generation])

    def test_failures_answer_none(self):
        async def go():
            self.assertIsNone(await remote.fetch_remote(None, 640))
            self.assertIsNone(await remote.fetch_remote('https://x.invalid/missing/1x1.jpg', 640))
        run(go())

    def test_only_https_is_fetched(self):
        async def go():
            for url in ('http://x.invalid/a/256x256bb.jpg', '/home/someone/a.jpg'):
                self.assertIsNone(await remote.fetch_remote(url, 640))
        run(go())
        self.assertEqual(self.fetched, [])

    def test_downloads_run_in_their_own_threads(self):
        run(remote.fetch_remote('https://x.invalid/a/256x256bb.jpg', 640))
        self.assertEqual(len(self.threads), 1)
        self.assertTrue(self.threads[0].startswith('art-fetch'), self.threads)


class FetchCoverTest(RemoteTestCase):
    """fetch_cover: True only when this call brought the cover."""

    def cover_item(self, name='a', url=True):
        raw = {'id': name, 'kind': 'album', 'title': name.upper(),
               'art': self.path('art', f'{name}.jpg'), 'thumb': self.path('thumb', f'{name}.jpg')}
        if url:
            raw['artUrl'] = f'https://x.invalid/{name}/640x640bb.jpg'
        return Item(raw)

    def test_true_when_this_call_downloaded(self):
        item = self.cover_item()
        self.assertTrue(run(remote.fetch_cover(item)))
        self.assertTrue(os.path.exists(item.art))
        self.assertEqual(self.fetched, ['https://x.invalid/a/640x640bb.jpg'])
        # On disk now: False, and nothing fetched.
        self.assertFalse(run(remote.fetch_cover(item)))
        self.assertEqual(len(self.fetched), 1)

    def test_false_without_a_cover_url(self):
        self.assertFalse(run(remote.fetch_cover(self.cover_item(url=False))))
        self.assertFalse(run(remote.fetch_cover(Item({'id': 'b', 'kind': 'album', 'title': 'B',
                                                      'artUrl': 'https://x.invalid/b.jpg'}))))
        self.assertEqual(self.fetched, [])

    def test_concurrent_calls_share_one_download(self):
        item = self.cover_item()

        async def go():
            return await asyncio.gather(remote.fetch_cover(item), remote.fetch_cover(item))

        self.assertEqual(run(go()), [True, True])
        self.assertEqual(len(self.fetched), 1)

    def test_a_failure_is_logged_and_false(self):
        item = self.cover_item('missing')
        self.assertFalse(run(remote.fetch_cover(item)))
        self.assertFalse(os.path.exists(item.art))

        def broken(*_args, **_kwargs):
            raise OSError('the disk is full')

        with mock.patch.object(remote.normalize, 'cache_artwork', broken), \
                self.assertLogs(remote.log, 'ERROR'):
            self.assertFalse(run(remote.fetch_cover(self.cover_item('b'))))

    def test_a_cancelled_caller_leaves_the_download_to_the_others(self):
        item = self.cover_item()
        self.gate = threading.Event()

        async def go():
            first = asyncio.ensure_future(remote.fetch_cover(item))
            second = asyncio.ensure_future(remote.fetch_cover(item))
            await asyncio.sleep(0.05)
            first.cancel()
            self.gate.set()
            return await second

        self.assertTrue(run(go()))
        self.assertEqual(len(self.fetched), 1)


class FetchThumbTest(RemoteTestCase):
    def test_fetch_thumb(self):
        item = Item(remote.remote_item({'id': '1', 'kind': 'album', 'title': 'A',
                                        'art': 'https://x.invalid/a/320x320bb.jpg'}))
        bare = Item({'id': '2', 'kind': 'album', 'title': 'B'})
        failing = Item(remote.remote_item({'id': '3', 'kind': 'album', 'title': 'C',
                                           'art': 'https://x.invalid/missing/320x320bb.jpg'}))

        async def go():
            self.assertTrue(await remote.fetch_thumb(item))
            self.assertTrue(os.path.exists(item.thumb))
            self.assertEqual(self.fetched, ['https://x.invalid/a/320x320bb.jpg'])
            self.assertFalse(await remote.fetch_thumb(item))  # on disk: nothing brought
            self.assertEqual(len(self.fetched), 1)
            self.assertFalse(await remote.fetch_thumb(bare))
            self.assertFalse(await remote.fetch_thumb(failing))
        run(go())

    def test_a_kept_thumbnail_is_fetched_where_the_item_names_it(self):
        kept = self.path('thumb', 'kept.jpg')
        item = Item(remote.remote_item({'id': '1', 'kind': 'album', 'title': 'A', 'thumb': kept,
                                        'art': 'https://x.invalid/a/320x320bb.jpg'}))
        self.assertTrue(run(remote.fetch_thumb(item)))
        self.assertTrue(os.path.exists(kept))


class ShelfTest(RemoteTestCase):
    """The engine's shelves: titled in the app's words, their items' artwork fetched."""

    def setUp(self):
        super().setUp()
        # Every word through gettext: marked, so a missed one shows.
        patcher = mock.patch.object(remote, '_', lambda text: f'<{text}>')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_app_names_what_the_backend_keys(self):
        title = remote.shelf_title
        self.assertEqual(title({'key': 'top', 'title': ''}), '<Top Results>')
        self.assertEqual(title({'key': 'music-videos', 'title': ''}), '<Music Videos>')
        self.assertEqual(title({'key': 'new-banners', 'title': '', 'featured': True}),
                         '<Featured>')
        # Apple's own titles come in the account's language, and stay.
        self.assertEqual(title({'key': 'new-best', 'title': 'Best New Songs'}), 'Best New Songs')
        # A recommendation Apple left untitled (Made for You's).
        self.assertEqual(title({'key': 'rec-1', 'title': ''}), '<Made for You>')

    def test_remote_shelves_are_titled_by_it(self):
        shelves = remote.remote_shelves([
            {'key': 'albums', 'title': '', 'items': [{'id': '1', 'kind': 'album', 'title': 'A'}]},
            {'key': 'empty', 'title': 'Nothing', 'items': []},
            {'key': 'bad', 'title': 'Bad', 'items': [{'title': 'no id'}, 'not a dict']},
            'not a dict',
        ])
        self.assertEqual([(shelf.key, shelf.title) for shelf in shelves], [('albums', '<Albums>')])

    def test_shelf_art_is_fetched_for_the_items_that_name_it(self):
        shelves = remote.remote_shelves([{'key': 'albums', 'title': '', 'items': [
            {'id': str(n), 'kind': 'album', 'title': f'A{n}',
             'art': f'https://x.invalid/{n}/320x320bb.jpg'} for n in range(3)] + [
            {'id': 'local', 'kind': 'album', 'title': 'On disk', 'art': '/cache/art/l.jpg'}]}])
        items = list(shelves[0].items)
        run(remote.fetch_shelf_art(shelves))
        self.assertEqual(sorted(self.fetched), [f'https://x.invalid/{n}/320x320bb.jpg'
                                                for n in range(3)])
        for item in items[:3]:
            self.assertTrue(os.path.exists(item.thumb))
        # Again: all on disk, nothing fetched.
        run(remote.fetch_shelf_art(shelves))
        self.assertEqual(len(self.fetched), 3)


if __name__ == '__main__':
    unittest.main()
