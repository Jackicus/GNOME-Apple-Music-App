# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The Shell's search provider (src/search_provider.py): the ranking, the result sets and
subsearches over an invented library, the metas (the thumbnail's gicon, the kind's icon),
the activation and the launched search against a stand-in window, the D-Bus dispatch over a
stand-in invocation (the app held and released, the library's load waited for), and the
object on a private bus, reached through GDBus. One test runs the whole Application as the
bus starts it for a search (tests/service_app.py, --gapplication-service, a stand-in engine):
it answers the search without a window and without starting the engine, and quits when idle.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import ROOT
from tests.gtk import requires_gtk
from tests.test_library import album, load, track, write_library
from tests.test_mpris import private_bus

from gi.events import GLibEventLoop
from gi.repository import Gio, GLib

from applemusic import search_provider
from applemusic.library import Library, Track
from applemusic.search_provider import (INTERFACE, RESULTS_PER_KIND, SearchProvider,
                                        description, parse_id, rank, result_id, search_terms)

OBJECT_PATH = '/io/github/jackicus/MusicSleeve/Test/SearchProvider'


# -- an invented library --------------------------------------------------------------------

def artist(artist_id, title, albums=()):
    data = {'id': artist_id, 'kind': 'artist', 'title': title, 'art': None, 'thumb': None,
            'play': {'kind': 'artist', 'id': artist_id},
            'groups': [{'name': entry['title'], 'play': entry['play'], 'entries': []}
                       for entry in albums]}
    if albums:
        data['albumCount'] = len(albums)
    return data


def playlist(playlist_id, title, thumb=None):
    return {'id': playlist_id, 'kind': 'playlist', 'title': title, 'subtitle': '',
            'art': None, 'thumb': thumb, 'trackCount': 2, 'durationMs': 360000,
            'play': {'kind': 'playlist', 'id': playlist_id},
            'groups': [{'name': '', 'play': {'kind': 'playlist', 'id': playlist_id},
                        'entries': [track(f'{playlist_id}.1', 'Tideline', 0),
                                    track(f'{playlist_id}.2', 'Shore', 1)]}]}


def titled_album(album_id, title, songs, subtitle='Test Artist', thumb=None):
    """test_library's album, its tracks titled as `songs` says and its artist `subtitle`."""
    data = album(album_id, title, [f'{album_id}.{n}' for n in range(len(songs))])
    data['subtitle'] = subtitle
    data['thumb'] = thumb
    for entry, name in zip(data['groups'][0]['entries'], songs, strict=True):
        entry['title'] = name
    return data


def write_invented_library(cache, thumb=None):
    """Albums, artists and playlists made to be found by 'tide' and 'harbour' in every
    rank; `thumb` is the first album's and the first playlist's thumbnail path."""
    albums = [
        titled_album('l.a1', 'High Tide', ['Harbour Lights', 'Low Water'],
                     subtitle='The Invented Band', thumb=thumb),
        titled_album('l.a2', 'Tide', ['Tide (Reprise)']),
        titled_album('l.a3', 'Riptide Nights', ['Riptide']),
        titled_album('l.a4', 'Harbour', ['The Tide Comes In'], subtitle='Tide Pool'),
        titled_album('l.a5', 'Quiet Streets', ['Quiet'], subtitle='Tidewater Quartet'),
        titled_album('l.a6', 'Nothing Here', ['Nothing']),
    ]
    artists = [artist('l.ar1', 'Mara Lind & The Tide', albums[:2]),
               artist('l.ar2', 'The Invented Band', albums[:1]),
               artist('l.ar3', 'Tidewater Quartet')]
    playlists = [playlist('p.1', 'Tide Pools', thumb=thumb),
                 playlist('p.2', 'Late Night Harbour'),
                 playlist('p.3', 'Études')]
    data = {'version': 1, 'generated': '2026-01-01T00:00:00Z', 'storefront': 'gb',
            'sections': {'albums': albums, 'artists': artists, 'playlists': playlists},
            'shelves': []}
    Path(cache, 'library.json').write_text(json.dumps(data), encoding='utf-8')


# -- stand-ins ------------------------------------------------------------------------------

class FakeWindow:
    def __init__(self):
        self.presented = 0
        self.opened = []
        self.played = []
        self.searched = []

    def present(self):
        self.presented += 1

    def open_item(self, item):
        self.opened.append(item)

    def play_request(self, play, start_with=None, shuffle=None, start_id=None):
        self.played.append((play, start_with, shuffle, start_id))

    def search_library(self, text):
        self.searched.append(text)


class FakeApp:
    """What the provider asks of the Application: the library, its first load as a task
    (`load` here: a future the test settles, or None for one long done), hold and release,
    spawn, activate and the window."""

    def __init__(self, library, load=None):
        self.library = library
        self.load = load
        self.held = 0
        self.released = 0
        self.activated = 0
        self.window = FakeWindow()
        self.quitting = False
        self.tasks = []

    def load_library(self):
        return self.load

    def hold(self):
        self.held += 1

    def release(self):
        self.released += 1

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    def activate(self):
        self.activated += 1

    def get_active_window(self):
        return None if self.quitting else self.window


class FakeInvocation:
    """A Gio.DBusMethodInvocation's surface: the reply, or the error, recorded."""

    def __init__(self):
        self.value = None
        self.error = None

    def return_value(self, value):
        self.value = value

    def return_dbus_error(self, name, message):
        self.error = (name, message)


# -- the pure functions ---------------------------------------------------------------------

class RankTest(unittest.TestCase):
    def test_the_ranks_in_order(self):
        terms = ['tide']
        self.assertEqual(rank('tide', '', terms), 0)
        self.assertEqual(rank('tide pools', '', terms), 1)
        self.assertEqual(rank('high tide', '', terms), 2)
        self.assertEqual(rank('riptide nights', '', terms), 3)
        self.assertEqual(rank('harbour', 'tide pool', terms), 4)
        self.assertIsNone(rank('harbour', 'the band', terms))

    def test_every_term_has_to_match_somewhere(self):
        self.assertEqual(rank('high tide', 'the band', ['high', 'tide']), 0)
        self.assertEqual(rank('high tide', 'the band', ['tide', 'high']), 2)
        self.assertEqual(rank('high tide', 'the band', ['tide', 'band']), 4)
        self.assertIsNone(rank('high tide', 'the band', ['tide', 'quartet']))
        self.assertIsNone(rank('high tide', 'the band', []))

    def test_terms_are_folded_and_blank_ones_dropped(self):
        self.assertEqual(search_terms(['Étude', ' ', '', 'The Tide ']), ['etude', 'the tide'])

    def test_identifiers_round_trip(self):
        self.assertEqual(result_id('album', 'l.a1'), 'album:l.a1')
        self.assertEqual(parse_id('album:l.a1'), ('album', 'l.a1'))
        self.assertEqual(parse_id('song:i.1:2'), ('song', 'i.1:2'))
        self.assertIsNone(parse_id('station:r.1'))
        self.assertIsNone(parse_id('album:'))
        self.assertIsNone(parse_id('nonsense'))


# -- the provider over an invented library --------------------------------------------------

class ProviderTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.cache = cls.temp_dir.name
        cls.thumb = os.path.join(cls.cache, 'thumb', 'a1.jpg')
        os.makedirs(os.path.dirname(cls.thumb))
        Path(cls.thumb).write_bytes(b'not really a jpeg')
        write_invented_library(cls.cache, thumb=cls.thumb)
        cls.library = load(cls.cache)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        self.app = FakeApp(self.library)
        self.provider = SearchProvider(self.app)

    async def with_songs(self):
        """A fresh library of the same cache with its Songs store built, as the provider's
        from here on (the class's own stays without one)."""
        library = Library()
        with mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.cache}):
            await library.load()
        await library.build_songs()
        self.app = FakeApp(library)
        self.provider = SearchProvider(self.app)
        return library


class ResultSetTest(ProviderTestCase):
    async def test_results_are_ranked_then_by_kind_then_in_store_order(self):
        results = await self.provider.search(['tide'])
        self.assertEqual(results, [
            'album:l.a2',          # 0: the title is the search
            'artist:l.ar3',        # 1: starts with it (Tidewater Quartet): artists first
            'playlist:p.1',        # 1: Tide Pools
            'artist:l.ar1',        # 2: a word starts with it
            'album:l.a1',          # 2: High Tide
            'album:l.a3',          # 3: Riptide Nights
            'album:l.a4',          # 4: the subtitle only (Tide Pool)
            'album:l.a5',          # 4: Tidewater Quartet
        ])

    async def test_each_term_has_to_match_and_the_text_is_folded(self):
        self.assertEqual(await self.provider.search(['TIDE', 'high']), ['album:l.a1'])
        self.assertEqual(await self.provider.search(['etudes']), ['playlist:p.3'])
        self.assertEqual(await self.provider.search(['nowhere']), [])
        self.assertEqual(await self.provider.search([]), [])
        self.assertEqual(await self.provider.search(['', ' ']), [])

    async def test_a_handful_per_kind(self):
        with mock.patch.object(search_provider, 'RESULTS_PER_KIND', 2):
            results = await self.provider.search(['tide'])
        kinds = [parse_id(identifier)[0] for identifier in results]
        self.assertEqual(kinds.count('album'), 2)
        self.assertEqual(kinds.count('artist'), 2)
        self.assertEqual(kinds.count('playlist'), 1)
        self.assertEqual(RESULTS_PER_KIND, 5)

    async def test_songs_come_only_once_the_songs_store_is_built(self):
        self.assertFalse(self.library.songs_ready)
        self.assertNotIn('song:l.a1.0', await self.provider.search(['harbour']))
        library = await self.with_songs()
        self.assertTrue(library.songs_ready)
        results = await self.provider.search(['harbour'])
        # Harbour (the search), Harbour Lights (starts with it), Late Night Harbour.
        self.assertEqual(results, ['album:l.a4', 'song:l.a1.0', 'playlist:p.2'])
        # A song is found by its artist and album too (its search key: every song here is
        # on 'Test Album'), a handful of them in the store's order.
        results = await self.provider.search(['test', 'album'])
        self.assertEqual(results, ['song:l.a1.0', 'song:l.a1.1', 'song:l.a2.0', 'song:l.a3.0',
                                   'song:l.a4.0'])

    async def test_a_subsearch_is_searched_afresh(self):
        first = await self.provider.search(['ti'])
        self.assertIn('album:l.a5', first)  # Quiet Streets, by Tidewater Quartet
        reply = await self.provider.get_subsearch_result_set(first, ['tide', 'pool'])
        self.assertEqual(reply.unpack(), (['playlist:p.1', 'album:l.a4'],))

    async def test_the_scan_yields_to_the_frame_clock_between_steps(self):
        yields = []

        async def noted():
            yields.append(True)

        with mock.patch.object(search_provider, 'SCAN_STEP', 2), \
                mock.patch.object(search_provider, 'yield_to_frames', noted):
            await self.provider.search(['tide'])
        self.assertGreaterEqual(len(yields), 3)  # 6 albums alone are two yields


class MetasTest(ProviderTestCase):
    async def test_metas_name_describe_and_draw_each_result(self):
        results = await self.provider.search(['tide'])
        metas = await self.provider.metas(results)
        self.assertEqual([meta['id'].unpack() for meta in metas], results)
        by_id = {meta['id'].unpack(): {key: value.unpack() for key, value in meta.items()}
                 for meta in metas}
        self.assertEqual(by_id['album:l.a1'], {
            'id': 'album:l.a1', 'name': 'High Tide', 'description': 'Album · The Invented Band',
            'gicon': self.thumb})  # the thumbnail file is there: a file icon
        self.assertEqual(by_id['album:l.a2']['gicon'], 'media-optical-cd-audio-symbolic')
        self.assertEqual(by_id['artist:l.ar1']['gicon'], 'audio-input-microphone-symbolic')
        self.assertEqual(by_id['artist:l.ar1']['description'], 'Artist · 2 albums')
        self.assertEqual(by_id['playlist:p.1']['gicon'], self.thumb)
        self.assertEqual(by_id['playlist:p.1']['description'], 'Playlist · 2 songs, 6 min')
        self.assertEqual(by_id['album:l.a4']['description'], 'Album · Tide Pool')

    async def test_a_missing_thumbnail_file_falls_back_to_the_kinds_icon(self):
        gone = os.path.join(self.cache, 'thumb', 'gone.jpg')
        item = self.library.by_id('album', 'l.a3')
        with mock.patch.dict(item.raw, {'thumb': gone}):
            await self.provider.search(['riptide'])
            metas = await self.provider.metas(['album:l.a3'])
        self.assertEqual(metas[0]['gicon'].unpack(), 'media-optical-cd-audio-symbolic')

    async def test_a_song_meta_names_its_artist_and_album(self):
        await self.with_songs()
        await self.provider.search(['harbour', 'lights'])
        metas = await self.provider.metas(['song:l.a1.0'])
        unpacked = {key: value.unpack() for key, value in metas[0].items()}
        self.assertEqual(unpacked, {'id': 'song:l.a1.0', 'name': 'Harbour Lights',
                                    'description': 'Song · Test Artist · Test Album',
                                    'gicon': self.thumb})  # its album's thumbnail

    async def test_every_identifier_gets_a_meta_with_a_name(self):
        metas = await self.provider.metas(['album:l.a6', 'album:gone', 'nonsense'])
        self.assertEqual([meta['name'].unpack() for meta in metas],
                         ['Nothing Here', 'Not in the library any more',
                          'Not in the library any more'])
        self.assertEqual([meta['id'].unpack() for meta in metas],
                         ['album:l.a6', 'album:gone', 'nonsense'])

    def test_a_description_leaves_out_what_is_missing(self):
        self.assertEqual(description('album', self.library.by_id('album', 'l.a6')),
                         'Album · Test Artist')
        item = self.library.by_id('artist', 'l.ar3')
        self.assertEqual(description('artist', item), 'Artist')


class ActivationTest(ProviderTestCase):
    async def test_a_result_opens_its_item_in_the_presented_window(self):
        await self.provider.search(['tide'])
        self.provider.activate('album:l.a1')
        self.assertEqual(self.app.activated, 1)
        self.assertEqual([item.id for item in self.app.window.opened], ['l.a1'])
        self.assertEqual(self.app.window.played, [])

    async def test_a_result_not_searched_for_is_found_in_the_library(self):
        self.provider.activate('artist:l.ar1')
        self.assertEqual([item.id for item in self.app.window.opened], ['l.ar1'])
        self.provider.activate('playlist:p.2')
        self.assertEqual([item.id for item in self.app.window.opened], ['l.ar1', 'p.2'])

    async def test_a_song_plays_from_its_album(self):
        library = await self.with_songs()
        self.provider.activate('song:l.a1.1')  # never searched for: found in the store
        self.assertEqual(self.app.window.opened, [])
        self.assertEqual(self.app.window.played,
                         [({'kind': 'album', 'id': 'l.a1'}, 1, None, 'l.a1.1')])
        self.assertIsInstance(next(iter(library.songs)), Track)

    async def test_an_unknown_result_only_presents_the_window(self):
        with self.assertLogs('applemusic.search_provider', 'WARNING'):
            self.provider.activate('album:gone')
        self.assertEqual(self.app.activated, 1)
        self.assertEqual(self.app.window.opened, [])

    async def test_nothing_is_opened_while_the_app_quits(self):
        self.app.quitting = True
        with self.assertLogs('applemusic.search_provider', 'WARNING'):
            self.provider.activate('album:l.a1')
        self.assertEqual(self.app.window.opened, [])

    async def test_launch_search_carries_the_terms_to_the_search_page(self):
        self.provider.launch(['high', '', 'tide'])
        self.assertEqual(self.app.activated, 1)
        self.assertEqual(self.app.window.searched, ['high tide'])


class DispatchTest(ProviderTestCase):
    """The D-Bus method call path over a stand-in invocation: the reply's type, the app
    held until the reply is on the bus, the library's load waited for first."""

    def call(self, method, parameters, invocation=None):
        invocation = invocation or FakeInvocation()
        self.provider._on_method_call(None, ':1.1', OBJECT_PATH, INTERFACE, method,
                                      parameters, invocation)
        return invocation

    async def settle(self):
        await asyncio.gather(*self.app.tasks)

    async def test_the_result_set_goes_out_as_a_string_array(self):
        invocation = self.call('GetInitialResultSet', GLib.Variant('(as)', (['tide'],)))
        self.assertEqual((self.app.held, self.app.released), (1, 0))
        await self.settle()
        self.assertEqual((self.app.held, self.app.released), (1, 1))
        self.assertEqual(invocation.value.get_type_string(), '(as)')
        self.assertEqual(invocation.value.unpack()[0][0], 'album:l.a2')
        self.assertIsNone(invocation.error)

    async def test_metas_activation_and_launch_through_the_bus_path(self):
        metas = self.call('GetResultMetas', GLib.Variant('(as)', (['album:l.a1'],)))
        await self.settle()
        self.assertEqual(metas.value.get_type_string(), '(aa{sv})')
        self.assertEqual(metas.value.unpack()[0][0]['name'], 'High Tide')
        activation = self.call('ActivateResult',
                               GLib.Variant('(sasu)', ('album:l.a1', ['tide'], 0)))
        launch = self.call('LaunchSearch', GLib.Variant('(asu)', (['tide'], 0)))
        await self.settle()
        self.assertIsNone(activation.value)
        self.assertIsNone(launch.value)
        self.assertEqual([item.id for item in self.app.window.opened], ['l.a1'])
        self.assertEqual(self.app.window.searched, ['tide'])
        self.assertEqual((self.app.held, self.app.released), (3, 3))

    async def test_an_unknown_method_is_refused_without_a_hold(self):
        invocation = self.call('Nonsense', GLib.Variant('()', ()))
        self.assertEqual(invocation.error[0], 'org.freedesktop.DBus.Error.UnknownMethod')
        self.assertEqual((self.app.held, self.app.released), (0, 0))

    async def test_a_failure_answers_an_error_and_releases(self):
        with mock.patch.object(self.provider, 'search', side_effect=ValueError('no')), \
                self.assertLogs('applemusic.search_provider', 'ERROR'):
            invocation = self.call('GetInitialResultSet', GLib.Variant('(as)', (['tide'],)))
            await self.settle()
        self.assertEqual(invocation.error[0], 'org.freedesktop.DBus.Error.Failed')
        self.assertEqual((self.app.held, self.app.released), (1, 1))

    async def test_an_answer_waits_for_the_librarys_first_load(self):
        self.app.load = asyncio.get_running_loop().create_future()
        invocation = self.call('GetInitialResultSet', GLib.Variant('(as)', (['tide'],)))
        for _turn in range(5):
            await asyncio.sleep(0)
        self.assertIsNone(invocation.value)  # still waiting
        self.app.load.set_result(None)
        await self.settle()
        self.assertEqual(invocation.value.unpack()[0][0], 'album:l.a2')

    async def test_a_load_that_takes_too_long_is_answered_from_what_is_loaded(self):
        self.app.load = asyncio.get_running_loop().create_future()
        with mock.patch.object(search_provider, 'LOAD_WAIT', 0.01), \
                self.assertLogs('applemusic.search_provider', 'WARNING'):
            invocation = self.call('GetInitialResultSet', GLib.Variant('(as)', (['tide'],)))
            await self.settle()
        self.assertFalse(self.app.load.cancelled())  # the load goes on for the window
        self.assertEqual(invocation.value.unpack()[0][0], 'album:l.a2')
        self.app.load.set_result(None)

    async def test_a_failed_load_still_answers(self):
        self.app.load = asyncio.get_running_loop().create_future()
        self.app.load.set_exception(OSError('unreadable'))
        invocation = self.call('GetInitialResultSet', GLib.Variant('(as)', (['tide'],)))
        await self.settle()
        self.assertIsNone(invocation.error)
        self.assertEqual(invocation.value.unpack()[0][0], 'album:l.a2')
        # The app's spawn() logs a task's failure; this stand-in's is taken here instead.
        self.assertIsInstance(self.app.load.exception(), OSError)


# -- on a bus -------------------------------------------------------------------------------

@unittest.skipUnless(shutil.which('dbus-daemon'), 'no dbus-daemon for a private bus')
class PrivateBusTest(unittest.IsolatedAsyncioTestCase):
    """The object on a private bus (a dbus-daemon of the test's own), reached through GDBus
    as the Shell would, the provider's tasks on the GLib loop as in the app: introspection
    (GLib's, from the node info), a search's round trip and its metas."""

    loop_factory = GLibEventLoop

    async def test_over_gdbus(self):
        address = private_bus(self)
        flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                 | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        server = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        client = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        cache = tempfile.mkdtemp(prefix='search-provider-test-')
        self.addCleanup(shutil.rmtree, cache, ignore_errors=True)
        write_invented_library(cache)
        library = Library()
        with mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': cache}):
            await library.load()
        app = FakeApp(library)
        provider = SearchProvider(app)
        provider.register(server, OBJECT_PATH)
        self.addCleanup(provider.unregister)
        self.assertTrue(provider.registered)
        name = server.get_unique_name()
        results = {}

        def call(method, parameters, reply):
            return client.call_sync(name, OBJECT_PATH, INTERFACE, method, parameters,
                                    GLib.VariantType(reply), Gio.DBusCallFlags.NONE, 5000,
                                    None).unpack()

        def talk():
            try:
                results['introspection'] = client.call_sync(
                    name, OBJECT_PATH, 'org.freedesktop.DBus.Introspectable', 'Introspect',
                    None, GLib.VariantType('(s)'), Gio.DBusCallFlags.NONE, 5000,
                    None).unpack()[0]
                results['initial'] = call('GetInitialResultSet',
                                          GLib.Variant('(as)', (['tide'],)), '(as)')[0]
                results['sub'] = call('GetSubsearchResultSet',
                                      GLib.Variant('(asas)', (results['initial'], ['tide', 'p'])),
                                      '(as)')[0]
                results['metas'] = call('GetResultMetas',
                                        GLib.Variant('(as)', (results['sub'],)), '(aa{sv})')[0]
            except Exception as error:  # noqa: BLE001  reported by the assertion below
                results['failure'] = error
            finally:
                client.close_sync(None)

        await asyncio.to_thread(talk)
        self.assertNotIn('failure', results, results.get('failure'))
        self.assertIn('<interface name="org.gnome.Shell.SearchProvider2">',
                      results['introspection'])
        self.assertIn('<method name="LaunchSearch">', results['introspection'])
        self.assertEqual(results['initial'][0], 'album:l.a2')
        self.assertEqual(results['sub'], ['playlist:p.1', 'album:l.a3', 'album:l.a4'])
        self.assertEqual([meta['name'] for meta in results['metas']],
                         ['Tide Pools', 'Riptide Nights', 'Harbour'])
        self.assertEqual(app.held, app.released)
        self.assertEqual(app.held, 3)
        provider.unregister()
        self.assertFalse(provider.registered)
        server.close_sync(None)


@unittest.skipUnless(shutil.which('dbus-daemon'), 'no dbus-daemon for a private bus')
class ServiceStartTest(unittest.TestCase):
    """The Application as the bus starts it for a search (tests/service_app.py, a process
    of its own on a private bus, --gapplication-service): the provider answers from the
    library of the cache, no window is built, the engine is never started, and the app
    quits once nothing has called it for its linger. The child needs a display for
    Adw.Application's startup, as the widget tests do."""

    @requires_gtk
    def test_a_search_starts_the_app_without_a_window_or_the_engine(self):
        address = private_bus(self)
        root = tempfile.mkdtemp(prefix='service-start-test-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        cache = os.path.join(root, 'cache')
        os.makedirs(cache)
        write_library(cache, [album('l.a1', 'Harbour Lights', ['i.1'])])
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address, APPLE_MUSIC_CACHE=cache,
                   APPLE_MUSIC_PROFILE=os.path.join(root, 'profile'), SERVICE_LINGER_MS='400')
        env.pop('APPLE_MUSIC_DEBUG_PORT', None)
        child = subprocess.Popen([sys.executable, '-m', 'tests.service_app'], cwd=ROOT,
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                 | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        client = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.addCleanup(client.close_sync, None)
        name = 'io.github.jackicus.MusicSleeve.ServiceTest'
        path = '/io/github/jackicus/MusicSleeve/ServiceTest/SearchProvider'

        def owned():
            return client.call_sync(
                'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                'NameHasOwner', GLib.Variant('(s)', (name,)), GLib.VariantType('(b)'),
                Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]

        deadline = time.monotonic() + 20
        while not owned():
            self.assertIsNone(child.poll(), child.stderr.read() if child.poll() else None)
            self.assertLess(time.monotonic(), deadline, 'the service never took its name')
            time.sleep(0.05)

        def call(method, parameters, reply):
            return client.call_sync(name, path, INTERFACE, method, parameters,
                                    GLib.VariantType(reply), Gio.DBusCallFlags.NONE, 10000,
                                    None).unpack()

        results = call('GetInitialResultSet', GLib.Variant('(as)', (['harbour'],)), '(as)')[0]
        self.assertEqual(results, ['album:l.a1'])  # the songs store is not built for it
        metas = call('GetResultMetas', GLib.Variant('(as)', (results,)), '(aa{sv})')[0]
        self.assertEqual(metas[0]['name'], 'Harbour Lights')
        self.assertEqual(metas[0]['gicon'], 'media-optical-cd-audio-symbolic')
        try:
            out, err = child.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            out, err = child.communicate()
            self.fail(f'the service did not quit when idle: {err}')
        self.assertEqual(child.returncode, 0, err)
        report = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(report, {'engine': [], 'windows': 0, 'service': True,
                                  'provider': True, 'library': 'ready', 'status': 0}, err)
        self.assertFalse(owned())


if __name__ == '__main__':
    unittest.main()
