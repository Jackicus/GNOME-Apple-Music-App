"""MPRIS: the metadata and property variants for a track and for none, PropertiesChanged with
only the changed keys, Seeked, the methods and the writable properties, over a stand-in bus
connection and a Player fed by apply(). No bus, no GTK; the commands run under asyncio.run."""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

from gi.repository import GLib, GObject

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic import mpris
from applemusic.backend.errors import EngineError
from applemusic.mpris import (OBJECT_PATH, PLAYER_INTERFACE, PROPERTIES_INTERFACE,
                              ROOT_INTERFACE, Mpris, loop_status, metadata, playback_status,
                              repeat_mode, track_path)
from applemusic.player import NowPlaying, Player

APP_ID = 'io.github.jackicus.AppleMusic.Test'

# An invented track, as the bridge's formatTrack shapes it.
TRACK = {
    'id': 'i.demo0001', 'catalogId': '1000000001', 'title': 'Harbour Lights',
    'artist': 'The Invented Band', 'album': 'Fictional Album', 'trackNumber': 3,
    'discNumber': 1, 'durationMs': 214000, 'durationLabel': '3:34', 'explicit': False,
    'artUrl': 'https://example.invalid/art/256x256bb.jpg', 'index': 2,
}
TRACK_PATH = '/io/github/jackicus/AppleMusic/track/i_2edemo0001'


class FakeEngine(GObject.Object):
    """The Engine's surface the Player uses: state, the event signal, and the commands,
    recorded."""

    __gsignals__ = {
        'event': (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }

    state = GObject.Property(type=str, default='down')
    authorized = GObject.Property(type=bool, default=True)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail = None

    async def _command(self, name, *args):
        self.calls.append((name, *args))
        if self.fail is not None:
            raise self.fail
        return None

    async def now_playing(self):
        return await self._command('now_playing')

    async def control(self, action):
        return await self._command('control', action)

    async def seek(self, seconds):
        return await self._command('seek', seconds)

    async def volume(self, level):
        return await self._command('volume', level)

    async def shuffle(self, mode):
        return await self._command('shuffle', mode)

    async def repeat(self, mode):
        return await self._command('repeat', mode)


class FakeSettings:
    def get_boolean(self, _key):
        return True


class FakeWindow:
    def __init__(self):
        self.presented = 0

    def present(self):
        self.presented += 1


class FakeApp:
    """What the Player and the service ask of the Application."""

    def account_key(self, name):
        return name  # the release build's keys

    def __init__(self, engine):
        self.engine = engine
        self.settings = FakeSettings()
        self.demo = False
        self.window = FakeWindow()
        self.tasks = []
        self.actions = []
        self.errors = []
        self.activated = 0

    def get_application_id(self):
        return APP_ID

    def get_active_window(self):
        return self.window

    def activate(self):
        self.activated += 1

    def activate_action(self, name):
        self.actions.append(name)

    def toast(self, *_args):
        pass

    def spawn(self, coro):
        task = asyncio.get_event_loop().create_task(coro)
        self.tasks.append(task)
        return task

    def player_command(self, coro):
        async def command():
            try:
                await coro
            except EngineError as error:
                self.errors.append(error)
        return self.spawn(command())

    async def settle(self):
        """Let every task spawned so far finish (one a track change cancelled is fine)."""
        tasks, self.tasks = self.tasks, []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class FakeConnection:
    """A Gio.DBusConnection's surface the service uses: the registrations and the signals."""

    def __init__(self):
        self.closures = {}  # interface name → (method_call, get_property, set_property)
        self.signals = []   # (interface, signal, parameters unpacked)
        self.unregistered = []

    def register_object_with_closures2(self, path, interface, method_call, get_property,
                                       set_property):
        assert path == OBJECT_PATH
        self.closures[interface.name] = (method_call, get_property, set_property)
        return len(self.closures)

    def unregister_object(self, registration):
        self.unregistered.append(registration)

    def emit_signal(self, destination, path, interface, signal, parameters):
        assert destination is None and path == OBJECT_PATH
        self.signals.append((interface, signal, parameters.unpack()))

    # -- what a client would do --

    def get(self, interface, name):
        return self.closures[interface][1](self, ':1.1', OBJECT_PATH, interface, name)

    def get_all(self, interface):
        return {name: self.get(interface, name)
                for name in ('PlaybackStatus', 'LoopStatus', 'Rate', 'Shuffle', 'Metadata',
                             'Volume', 'Position', 'MinimumRate', 'MaximumRate', 'CanGoNext',
                             'CanGoPrevious', 'CanPlay', 'CanPause', 'CanSeek', 'CanControl')}

    def set(self, interface, name, value):
        return self.closures[interface][2](self, ':1.1', OBJECT_PATH, interface, name, value)

    def call(self, interface, method, parameters=None):
        invocation = FakeInvocation()
        self.closures[interface][0](self, ':1.1', OBJECT_PATH, interface, method,
                                    parameters or GLib.Variant('()', ()), invocation)
        return invocation

    def changed(self):
        """The keys of every PropertiesChanged so far, in order, and the signals forgotten."""
        keys = [sorted(parameters[1]) for interface, signal, parameters in self.signals
                if signal == 'PropertiesChanged']
        self.signals = []
        return keys

    def seeked(self):
        positions = [parameters[0] for interface, signal, parameters in self.signals
                     if signal == 'Seeked']
        self.signals = [entry for entry in self.signals if entry[1] != 'Seeked']
        return positions


class FakeInvocation:
    def __init__(self):
        self.returned = None
        self.error = None

    def return_value(self, value):
        self.returned = value

    def return_dbus_error(self, name, message):
        self.error = (name, message)


class FakeArtwork:
    """remote.fetch_remote: answers `path` at once, the URLs asked for recorded."""

    def __init__(self, path):
        self.path = path
        self.urls = []

    async def fetch_remote(self, url, size=640):
        self.urls.append(url)
        return self.path


class MetadataTest(unittest.TestCase):
    def test_a_track(self):
        data = metadata(NowPlaying(TRACK))
        self.assertEqual(sorted(data), ['mpris:length', 'mpris:trackid', 'xesam:album',
                                        'xesam:artist', 'xesam:title'])
        self.assertEqual(data['mpris:trackid'].get_type_string(), 'o')
        self.assertEqual(data['mpris:trackid'].get_string(), TRACK_PATH)
        self.assertEqual(data['mpris:length'].get_type_string(), 'x')
        self.assertEqual(data['mpris:length'].get_int64(), 214_000_000)
        self.assertEqual(data['xesam:title'].get_type_string(), 's')
        self.assertEqual(data['xesam:title'].get_string(), 'Harbour Lights')
        self.assertEqual(data['xesam:artist'].get_type_string(), 'as')
        self.assertEqual(data['xesam:artist'].unpack(), ['The Invented Band'])
        self.assertEqual(data['xesam:album'].unpack(), 'Fictional Album')
        # What goes on the bus: an a{sv} of it builds.
        variant = GLib.Variant('a{sv}', data)
        self.assertEqual(variant.get_type_string(), 'a{sv}')
        self.assertEqual(variant.unpack()['xesam:title'], 'Harbour Lights')

    def test_no_track(self):
        self.assertEqual(metadata(None), {})
        self.assertEqual(GLib.Variant('a{sv}', metadata(None)).unpack(), {})

    def test_art_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'art file.jpg')
            data = metadata(NowPlaying(TRACK), art_path=path, duration=200.5)
            self.assertEqual(data['mpris:artUrl'].get_type_string(), 's')
            self.assertEqual(data['mpris:artUrl'].get_string(),
                             GLib.filename_to_uri(path, None))
            self.assertTrue(data['mpris:artUrl'].get_string().startswith('file:///'))
            self.assertIn('art%20file.jpg', data['mpris:artUrl'].get_string())
            # The track's own length wins; MusicKit's stands in for a track without one.
            self.assertEqual(data['mpris:length'].get_int64(), 214_000_000)
            unknown = NowPlaying(dict(TRACK, durationMs=None))
            self.assertNotIn('mpris:length', metadata(unknown))
            self.assertEqual(metadata(unknown, duration=200.5)['mpris:length'].get_int64(),
                             200_500_000)

    def test_a_bare_track(self):
        bare = NowPlaying({'id': 'x', 'title': None, 'artist': '', 'durationMs': None})
        data = metadata(bare)
        self.assertEqual(list(data), ['mpris:trackid'])
        self.assertEqual(data['mpris:trackid'].get_string(),
                         '/io/github/jackicus/AppleMusic/track/x')

    def test_track_paths(self):
        self.assertEqual(track_path('i.demo0001'), TRACK_PATH)
        self.assertEqual(track_path('a_b'), '/io/github/jackicus/AppleMusic/track/a_5fb')
        self.assertEqual(track_path('1000000001'),
                         '/io/github/jackicus/AppleMusic/track/1000000001')
        self.assertEqual(track_path(''), '/io/github/jackicus/AppleMusic/track/_')
        self.assertEqual(track_path(None), '/io/github/jackicus/AppleMusic/track/_')
        for track_id in ('i.demo0001', 'a b/c-d.é', '', 'ra.978194965'):
            self.assertTrue(GLib.Variant.is_object_path(track_path(track_id)), track_id)
        self.assertNotEqual(track_path('a_b'), track_path('a.b'))

    def test_playback_status(self):
        for state in ('playing', 'loading', 'waiting', 'stalled'):
            self.assertEqual(playback_status(state), 'Playing')
        self.assertEqual(playback_status('paused'), 'Paused')
        for state in ('none', 'stopped', 'ended', 'completed', 'bogus'):
            self.assertEqual(playback_status(state), 'Stopped')
        self.assertEqual(playback_status('seeking', 'Playing'), 'Playing')
        self.assertEqual(playback_status('seeking', 'Paused'), 'Paused')
        self.assertEqual(playback_status('seeking'), 'Stopped')

    def test_loop_status(self):
        self.assertEqual([loop_status(mode) for mode in ('none', 'one', 'all', 'x')],
                         ['None', 'Track', 'Playlist', 'None'])
        self.assertEqual([repeat_mode(status) for status in ('None', 'Track', 'Playlist')],
                         ['none', 'one', 'all'])
        self.assertIsNone(repeat_mode('Bogus'))


class ServiceTest(unittest.TestCase):
    """The service on a stand-in connection, its Player fed by apply()."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.art_path = os.path.join(self.temp.name, 'remote.jpg')
        self.artwork = FakeArtwork(self.art_path)
        for name, value in (('fetch_remote', self.artwork.fetch_remote),
                            ('remote_art_path', lambda url, size=640: self.art_path)):
            patcher = mock.patch.object(mpris.remote, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.owned = []
        for name, value in (('bus_own_name', lambda *args: self.owned.append(args) or 7),
                            ('bus_unown_name', lambda owner: self.owned.append(owner))):
            patcher = mock.patch.object(mpris.Gio, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make(self):
        """A started service on a fresh connection, nothing playing."""
        self.engine = FakeEngine()
        self.app = FakeApp(self.engine)
        self.player = Player(self.app)
        self.app.player = self.player
        self.service = Mpris(self.app)
        self.service.start()
        self.assertEqual(self.owned[0][1], 'org.mpris.MediaPlayer2.' + APP_ID)
        self.connection = FakeConnection()
        self.service._on_bus_acquired(self.connection, self.owned[0][1])
        self.assertEqual(sorted(self.connection.closures), [ROOT_INTERFACE, PLAYER_INTERFACE])
        return self.service

    def play(self, position=10, state='playing', track=TRACK):
        self.player.apply({'track': track, 'state': state, 'position': position,
                           'duration': 214})

    def test_root_properties(self):
        self.make()
        get = self.connection.get
        self.assertEqual(get(ROOT_INTERFACE, 'Identity').unpack(), 'Apple Music')
        self.assertEqual(get(ROOT_INTERFACE, 'DesktopEntry').unpack(), APP_ID)
        for name, expected in (('CanQuit', True), ('CanRaise', True), ('Fullscreen', False),
                               ('CanSetFullscreen', False), ('HasTrackList', False)):
            self.assertEqual(get(ROOT_INTERFACE, name).get_type_string(), 'b', name)
            self.assertEqual(get(ROOT_INTERFACE, name).unpack(), expected, name)
        for name in ('SupportedUriSchemes', 'SupportedMimeTypes'):
            self.assertEqual(get(ROOT_INTERFACE, name).get_type_string(), 'as')
            self.assertEqual(get(ROOT_INTERFACE, name).unpack(), [])

    def test_player_properties_with_nothing_playing(self):
        self.make()
        values = self.connection.get_all(PLAYER_INTERFACE)
        types = {name: value.get_type_string() for name, value in values.items()}
        self.assertEqual(types, {
            'PlaybackStatus': 's', 'LoopStatus': 's', 'Rate': 'd', 'Shuffle': 'b',
            'Metadata': 'a{sv}', 'Volume': 'd', 'Position': 'x', 'MinimumRate': 'd',
            'MaximumRate': 'd', 'CanGoNext': 'b', 'CanGoPrevious': 'b', 'CanPlay': 'b',
            'CanPause': 'b', 'CanSeek': 'b', 'CanControl': 'b'})
        unpacked = {name: value.unpack() for name, value in values.items()}
        self.assertEqual(unpacked, {
            'PlaybackStatus': 'Stopped', 'LoopStatus': 'None', 'Rate': 1.0, 'Shuffle': False,
            'Metadata': {}, 'Volume': 1.0, 'Position': 0, 'MinimumRate': 1.0,
            'MaximumRate': 1.0, 'CanGoNext': False, 'CanGoPrevious': False, 'CanPlay': False,
            'CanPause': False, 'CanSeek': False, 'CanControl': True})

    def test_a_track_changes_the_properties_once(self):
        async def go():
            self.make()
            self.play()
            changed = self.connection.changed()
            # The track's notify comes first (Metadata and the Can*s), then the state's.
            self.assertEqual(changed[0], ['CanGoNext', 'CanGoPrevious', 'CanPause', 'CanPlay',
                                          'CanSeek', 'Metadata'])
            self.assertIn(['PlaybackStatus'], changed)
            values = {name: value.unpack()
                      for name, value in self.connection.get_all(PLAYER_INTERFACE).items()}
            self.assertEqual(values['PlaybackStatus'], 'Playing')
            self.assertTrue(values['CanPlay'] and values['CanSeek'] and values['CanGoNext'])
            self.assertEqual(values['Metadata']['xesam:title'], 'Harbour Lights')
            self.assertEqual(values['Metadata']['mpris:trackid'], TRACK_PATH)
            self.assertEqual(values['Metadata']['mpris:length'], 214_000_000)
            self.assertGreaterEqual(values['Position'], 10_000_000)
            self.assertLess(values['Position'], 11_000_000)
            # The same again: nothing differs, nothing is emitted.
            self.play()
            self.assertEqual(self.connection.changed(), [])
            # The artwork arrives: Metadata again, with its file URL, and only that.
            await self.app.settle()
            self.assertEqual(self.artwork.urls, [TRACK['artUrl']])
            self.assertEqual(self.connection.changed(), [['Metadata']])
            art_url = self.connection.get(PLAYER_INTERFACE, 'Metadata').unpack()['mpris:artUrl']
            self.assertEqual(art_url, GLib.filename_to_uri(self.art_path, None))
        asyncio.run(go())

    def test_refresh_art_after_the_cache_is_cleared(self):
        async def go():
            self.make()
            self.play()
            await self.app.settle()
            self.connection.changed()
            # The cache cleared: the file the Metadata named is not there (it never was
            # here), so it goes from the Metadata, and is fetched again.
            self.service.refresh_art()
            self.assertEqual(self.connection.changed(), [['Metadata']])
            self.assertNotIn('mpris:artUrl',
                             self.connection.get(PLAYER_INTERFACE, 'Metadata').unpack())
            await self.app.settle()
            self.assertEqual(self.artwork.urls, [TRACK['artUrl']] * 2)
            self.assertEqual(self.connection.changed(), [['Metadata']])
        asyncio.run(go())

    def test_state_and_modes(self):
        async def go():
            self.make()
            self.play()
            self.connection.changed()
            self.player.apply({'state': 'paused'})
            self.assertEqual(self.connection.changed(), [['PlaybackStatus']])
            self.assertEqual(self.connection.get(PLAYER_INTERFACE, 'PlaybackStatus').unpack(),
                             'Paused')
            self.player.apply({'state': 'seeking'})  # a transient: the status stays
            self.assertEqual(self.connection.changed(), [])
            self.player.apply({'state': 'loading'})
            self.assertEqual(self.connection.get(PLAYER_INTERFACE, 'PlaybackStatus').unpack(),
                             'Playing')
            self.player.apply({'shuffle': 'on', 'repeat': 'all', 'volume': 0.25})
            self.assertEqual(self.connection.changed(),
                             [['PlaybackStatus'], ['Shuffle'], ['LoopStatus'], ['Volume']])
            values = {name: value.unpack()
                      for name, value in self.connection.get_all(PLAYER_INTERFACE).items()}
            self.assertEqual((values['Shuffle'], values['LoopStatus'], values['Volume']),
                             (True, 'Playlist', 0.25))
            # MusicKit's duration changes nothing for a track with its own length.
            self.player.apply({'duration': 230})
            self.assertEqual(self.connection.changed(), [])
            self.assertEqual(self.connection.get(PLAYER_INTERFACE, 'Metadata').unpack()[
                'mpris:length'], 214_000_000)
            # A track without one takes MusicKit's when it comes, and the Metadata after a
            # track change never carries the previous item's length.
            self.player.apply({'track': dict(TRACK, id='i.demo0003', index=3, durationMs=None),
                               'position': 0})
            self.assertEqual(self.connection.changed(), [['Metadata']])
            self.assertNotIn('mpris:length',
                             self.connection.get(PLAYER_INTERFACE, 'Metadata').unpack())
            self.player.apply({'duration': 231})
            self.assertEqual(self.connection.changed(), [['Metadata']])
            self.assertEqual(self.connection.get(PLAYER_INTERFACE, 'Metadata').unpack()[
                'mpris:length'], 231_000_000)
            await self.app.settle()
            self.assertEqual(self.connection.changed(), [['Metadata']])  # its artwork
            # The engine goes: nothing playing, Stopped, the Can*s off, Metadata empty.
            self.player.apply(None)
            self.assertEqual(self.connection.changed()[0], [
                'CanGoNext', 'CanGoPrevious', 'CanPause', 'CanPlay', 'CanSeek', 'Metadata',
                'PlaybackStatus'])
            self.assertEqual(self.connection.get(PLAYER_INTERFACE, 'PlaybackStatus').unpack(),
                             'Stopped')
            self.assertEqual(self.connection.get(PLAYER_INTERFACE, 'Metadata').unpack(), {})
        asyncio.run(go())

    def test_seeked_on_a_jump_only(self):
        async def go():
            self.make()
            self.play(position=10)
            await self.app.settle()
            self.connection.signals = []
            self.service._hold_until = 0.0  # TRACK_HOLD after the first track has passed
            self.player.apply({'position': 10.25})  # the next time event
            self.player.apply({'position': 10.5})
            self.assertEqual(self.connection.seeked(), [])
            self.player.apply({'position': 60})  # the bar, or Apple's page, seeks
            self.assertEqual(self.connection.seeked(), [60_000_000])
            self.player.apply({'position': 60.25})
            self.assertEqual(self.connection.seeked(), [])
            # A new item: the reset to 0 and the previous item's position reported once more
            # are not seeks (TRACK_HOLD), and its Metadata goes out once, with its own
            # length (the Player resets the duration after the track's notify).
            self.connection.signals = []
            self.play(position=0, track=dict(TRACK, id='i.demo0002', index=3,
                                             durationMs=180000))
            self.player.apply({'position': 60.25})  # stale, from the item before
            self.player.apply({'position': 0.25})
            self.assertEqual(self.connection.seeked(), [])
            self.service._hold_until = 0.0  # later: a real jump is a seek again
            self.player.apply({'position': 90})
            self.assertEqual(self.connection.seeked(), [90_000_000])
            metadata_changes = [parameters[1]['Metadata'] for _i, _s, parameters
                                in self.connection.signals if 'Metadata' in parameters[1]]
            self.assertEqual(len(metadata_changes), 1)
            self.assertEqual(metadata_changes[0]['mpris:length'], 180_000_000)
            self.assertEqual(self.connection.get(PLAYER_INTERFACE, 'Metadata').unpack()[
                'mpris:trackid'], '/io/github/jackicus/AppleMusic/track/i_2edemo0002')
            await self.app.settle()
        asyncio.run(go())

    def test_methods_run_the_player(self):
        async def go():
            self.make()
            call = self.connection.call
            # Nothing playing: the transport does nothing (CanPlay is false).
            self.assertIsNone(call(PLAYER_INTERFACE, 'PlayPause').error)
            await self.app.settle()
            self.assertEqual(self.engine.calls, [])
            self.play()
            await self.app.settle()
            self.engine.calls.clear()
            for method in ('PlayPause', 'Next', 'Previous', 'Pause', 'Stop', 'Play'):
                invocation = call(PLAYER_INTERFACE, method)
                self.assertIsNone(invocation.error, method)
                self.assertIsNone(invocation.returned, method)
            await self.app.settle()
            self.assertEqual(self.engine.calls, [
                ('control', 'pause'), ('control', 'next'), ('control', 'previous'),
                ('control', 'pause'), ('control', 'stop'), ('control', 'play')])
            self.engine.calls.clear()
            self.player.apply({'state': 'paused', 'position': 10})
            call(PLAYER_INTERFACE, 'PlayPause')  # paused: play
            call(PLAYER_INTERFACE, 'Seek', GLib.Variant('(x)', (5_000_000,)))
            call(PLAYER_INTERFACE, 'Seek', GLib.Variant('(x)', (-30_000_000,)))  # to 0
            call(PLAYER_INTERFACE, 'SetPosition',
                 GLib.Variant('(ox)', (TRACK_PATH, 30_000_000)))
            call(PLAYER_INTERFACE, 'SetPosition',  # not the track playing: dropped
                 GLib.Variant('(ox)', ('/io/github/jackicus/AppleMusic/track/other', 1)))
            call(PLAYER_INTERFACE, 'Seek', GLib.Variant('(x)', (500_000_000,)))  # past the end
            call(PLAYER_INTERFACE, 'OpenUri', GLib.Variant('(s)', ('https://example.invalid',)))
            await self.app.settle()
            self.assertEqual(self.engine.calls, [
                ('control', 'play'), ('seek', 15.0), ('seek', 0.0), ('seek', 30.0),
                ('control', 'next')])
            self.assertEqual(self.connection.seeked(), [15_000_000, 0, 30_000_000])
            self.assertEqual(self.app.errors, [])
            # The root interface.
            call(ROOT_INTERFACE, 'Raise')
            self.assertEqual(self.app.window.presented, 1)
            call(ROOT_INTERFACE, 'Quit')
            self.assertEqual(self.app.actions, ['quit'])
            unknown = call(PLAYER_INTERFACE, 'Bogus')
            self.assertEqual(unknown.error[0], 'org.freedesktop.DBus.Error.UnknownMethod')
        asyncio.run(go())

    def test_a_failed_command_is_reported_not_raised(self):
        async def go():
            self.make()
            self.play()
            await self.app.settle()
            self.engine.fail = EngineError('engine-down', 'gone')
            self.assertIsNone(self.connection.call(PLAYER_INTERFACE, 'Next').error)
            await self.app.settle()
            self.assertEqual([error.code for error in self.app.errors], ['engine-down'])
        asyncio.run(go())

    def test_writable_properties(self):
        async def go():
            self.make()
            self.play()
            await self.app.settle()
            self.engine.calls.clear()
            set_property = self.connection.set
            self.assertTrue(set_property(PLAYER_INTERFACE, 'Volume', GLib.Variant('d', 0.3)))
            self.assertTrue(set_property(PLAYER_INTERFACE, 'Volume', GLib.Variant('d', 4.0)))
            self.assertTrue(set_property(PLAYER_INTERFACE, 'Shuffle', GLib.Variant('b', True)))
            self.assertTrue(set_property(PLAYER_INTERFACE, 'LoopStatus',
                                         GLib.Variant('s', 'Track')))
            self.assertTrue(set_property(PLAYER_INTERFACE, 'LoopStatus',
                                         GLib.Variant('s', 'Bogus')))
            self.assertTrue(set_property(PLAYER_INTERFACE, 'Rate', GLib.Variant('d', 2.0)))
            self.assertTrue(set_property(ROOT_INTERFACE, 'Fullscreen', GLib.Variant('b', True)))
            self.assertFalse(set_property(PLAYER_INTERFACE, 'Position', GLib.Variant('x', 1)))
            await self.app.settle()
            self.assertEqual(self.engine.calls, [
                ('volume', 0.3), ('volume', 1.0), ('shuffle', 'on'), ('repeat', 'one')])
            # Nothing changed on the bus: only the events do that.
            self.assertEqual(self.connection.get(PLAYER_INTERFACE, 'Volume').unpack(), 1.0)
            self.assertEqual(self.connection.get(PLAYER_INTERFACE, 'Rate').unpack(), 1.0)
        asyncio.run(go())

    def test_name_lost_and_stop(self):
        async def go():
            service = self.make()
            with self.assertLogs('applemusic.mpris', level='WARNING') as logged:
                service._on_name_lost(self.connection, 'org.mpris.MediaPlayer2.' + APP_ID)
                service._on_name_lost(None, 'org.mpris.MediaPlayer2.' + APP_ID)
            self.assertEqual(len(logged.output), 2)
            self.assertTrue(service.connected)  # the object stays; the name may come back
            self.play()
            self.assertTrue(self.connection.changed())
            await self.app.settle()
            service.stop()
            self.assertEqual(self.connection.unregistered, [1, 2])
            self.assertEqual(self.owned[-1], 7)  # bus_unown_name with the owner id
            self.assertFalse(service.connected)
            self.connection.signals = []
            self.player.apply({'state': 'paused'})  # no longer followed
            self.assertEqual(self.connection.signals, [])
            service.stop()  # again: nothing to do
            await self.app.settle()
        asyncio.run(go())

    def test_signals_without_a_connection_are_dropped(self):
        async def go():
            self.engine = FakeEngine()
            self.app = FakeApp(self.engine)
            self.player = Player(self.app)
            self.app.player = self.player
            service = Mpris(self.app)
            service.start()  # the bus never answers: no connection
            self.play()
            await self.app.settle()
            self.assertFalse(service.connected)
            service.stop()
        asyncio.run(go())

    def test_properties_changed_shape(self):
        async def go():
            self.make()
            self.play()
            interface, signal, parameters = self.connection.signals[0]
            self.assertEqual((interface, signal), (PROPERTIES_INTERFACE, 'PropertiesChanged'))
            self.assertEqual(parameters[0], PLAYER_INTERFACE)
            self.assertEqual(parameters[2], [])
            await self.app.settle()
        asyncio.run(go())


if __name__ == '__main__':
    unittest.main()
