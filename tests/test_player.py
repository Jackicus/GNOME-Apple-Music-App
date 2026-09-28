"""The Player: its properties fed by synthetic engine events, and its commands over a fake
engine. GObject only, no GTK or display; the commands run under asyncio.run."""

import asyncio
import time
import unittest

from gi.repository import GObject

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic.backend.errors import EngineError
from applemusic.player import ACTIVE_STATES, NowPlaying, Player, format_time

# An invented track, as the bridge's formatTrack shapes it.
TRACK = {
    'id': 'i.demo0001', 'catalogId': '1000000001', 'title': 'Harbour Lights',
    'artist': 'The Invented Band', 'album': 'Fictional Album', 'trackNumber': 3,
    'discNumber': 1, 'durationMs': 214000, 'durationLabel': '3:34', 'explicit': False,
    'artUrl': 'https://example.invalid/art/256x256bb.jpg', 'index': 2,
}


class FakeEngine(GObject.Object):
    """The Engine's surface the Player uses: the event signal, state, and the commands,
    which are recorded and answer what the test put in `answers`."""

    __gsignals__ = {
        'event': (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }

    state = GObject.Property(type=str, default='down')
    authorized = GObject.Property(type=bool, default=True)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.answers = {}
        self.fail = None  # an EngineError every command raises

    def event(self, name, data):
        self.emit('event', name, data)

    async def _command(self, name, *args):
        self.calls.append((name, *args))
        if self.fail is not None:
            raise self.fail
        return self.answers.get(name)

    async def start(self, visible=False):
        self.calls.append(('start', visible))
        self.state = 'up'

    async def now_playing(self):
        return await self._command('now_playing')

    async def play(self, kind, item_id, start_with=None, shuffle=False):
        return await self._command('play', kind, item_id, start_with, shuffle)

    async def play_next(self, kind, item_id):
        return await self._command('play_next', kind, item_id)

    async def play_later(self, kind, item_id):
        return await self._command('play_later', kind, item_id)

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
    def __init__(self, signed_in=True):
        self.signed_in = signed_in

    def get_boolean(self, key):
        assert key == 'signed-in'
        return self.signed_in


class FakeApp:
    """What the Player asks of the Application."""

    def __init__(self, engine, signed_in=True, demo=False):
        self.engine = engine
        self.settings = FakeSettings(signed_in)
        self.demo = demo
        self.toasts = []
        self.tasks = []

    def toast(self, title, *_args):
        self.toasts.append(title)

    def spawn(self, coro):
        task = asyncio.get_event_loop().create_task(coro)
        self.tasks.append(task)
        return task


def make_player(signed_in=True, demo=False, state='down'):
    engine = FakeEngine()
    engine.state = state
    app = FakeApp(engine, signed_in, demo)
    return Player(app), engine, app


class EventTest(unittest.TestCase):
    def setUp(self):
        self.player, self.engine, self.app = make_player()
        self.notified = []
        for prop in ('state', 'track', 'position', 'duration', 'shuffle', 'repeat', 'volume'):
            self.player.connect(f'notify::{prop}',
                                lambda _p, pspec: self.notified.append(pspec.name))

    def test_starts_with_nothing_playing(self):
        player = self.player
        self.assertEqual(player.state, 'none')
        self.assertIsNone(player.track)
        self.assertEqual((player.position, player.duration), (0.0, 0.0))
        self.assertFalse(player.shuffle)
        self.assertEqual(player.repeat, 'none')
        self.assertEqual(player.volume, 1.0)
        self.assertFalse(player.active)

    def test_now_playing_item_becomes_a_track(self):
        self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        track = self.player.track
        self.assertIsInstance(track, NowPlaying)
        self.assertEqual((track.id, track.catalog_id), ('i.demo0001', '1000000001'))
        self.assertEqual((track.title, track.artist, track.album),
                         ('Harbour Lights', 'The Invented Band', 'Fictional Album'))
        self.assertEqual(track.duration_ms, 214000)
        self.assertEqual(track.artwork_url, 'https://example.invalid/art/256x256bb.jpg')
        self.assertEqual(track.index, 2)
        self.assertFalse(track.explicit)
        # A new item starts at the top, its duration Apple's until MusicKit says.
        self.assertEqual((self.player.position, self.player.duration), (0.0, 214.0))
        self.assertIn('track', self.notified)

    def test_the_same_item_again_is_not_a_change(self):
        self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        self.engine.event('playbackTimeDidChange', {'position': 40, 'duration': 214})
        self.notified.clear()
        self.engine.event('nowPlayingItemDidChange', {'track': dict(TRACK), 'index': 2})
        self.assertEqual(self.notified, [])
        self.assertEqual(self.player.position, 40.0)

    def test_null_item_clears_the_track(self):
        self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        self.engine.event('playbackTimeDidChange', {'position': 40, 'duration': 214})
        self.engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})
        self.assertIsNone(self.player.track)
        self.assertEqual((self.player.position, self.player.duration), (0.0, 0.0))

    def test_a_track_without_artwork(self):
        self.engine.event('nowPlayingItemDidChange',
                          {'track': dict(TRACK, artUrl=None, durationMs=None), 'index': 0})
        self.assertIsNone(self.player.track.artwork_url)
        self.assertEqual(self.player.track.duration_ms, 0)
        self.assertEqual(self.player.duration, 0.0)

    def test_playback_state_and_times(self):
        self.engine.event('playbackStateDidChange',
                          {'state': 'playing', 'position': 12.5, 'duration': 214})
        self.assertEqual(self.player.state, 'playing')
        self.assertTrue(self.player.active)
        self.assertEqual((self.player.position, self.player.duration), (12.5, 214.0))
        self.engine.event('playbackStateDidChange', {'state': 'paused', 'position': 13})
        self.assertEqual(self.player.state, 'paused')
        self.assertFalse(self.player.active)
        self.assertEqual(self.player.position, 13.0)
        for state in ACTIVE_STATES:
            self.engine.event('playbackStateDidChange', {'state': state})
            self.assertTrue(self.player.active, state)
        self.engine.event('playbackStateDidChange', {'state': 'stopped'})
        self.assertFalse(self.player.active)

    def test_an_unknown_state_is_kept_as_it_is(self):
        self.engine.event('playbackStateDidChange', {'state': 'buffering'})
        self.assertEqual(self.player.state, 'buffering')
        self.engine.event('playbackStateDidChange', {'state': None})
        self.assertEqual(self.player.state, 'none')

    def test_time_updates_stamp_the_position(self):
        before = time.monotonic()
        self.engine.event('playbackTimeDidChange', {'position': 61, 'duration': 214})
        self.assertEqual((self.player.position, self.player.duration), (61.0, 214.0))
        self.assertGreaterEqual(self.player.position_updated_at, before)
        self.assertLessEqual(self.player.position_updated_at, time.monotonic())
        self.notified.clear()
        self.engine.event('playbackTimeDidChange', {'position': 61, 'duration': 214})
        self.assertEqual(self.notified, [])  # the same position notifies nothing

    def test_estimated_position_runs_on_while_playing(self):
        self.engine.event('playbackStateDidChange', {'state': 'playing'})
        self.engine.event('playbackTimeDidChange', {'position': 10, 'duration': 12})
        self.player.position_updated_at -= 1.0
        estimate = self.player.estimated_position()
        self.assertGreaterEqual(estimate, 11.0)
        self.assertLessEqual(estimate, 12.0)
        self.player.position_updated_at -= 10.0
        self.assertEqual(self.player.estimated_position(), 12.0)  # never past the end
        self.engine.event('playbackStateDidChange', {'state': 'paused'})
        self.player.position_updated_at -= 5.0
        self.assertEqual(self.player.estimated_position(), 10.0)  # paused: as reported

    def test_duration_shuffle_repeat_volume(self):
        self.engine.event('playbackDurationDidChange', {'duration': 300.5})
        self.assertEqual(self.player.duration, 300.5)
        self.engine.event('shuffleModeDidChange', {'shuffle': 'on'})
        self.assertTrue(self.player.shuffle)
        self.engine.event('shuffleModeDidChange', {'shuffle': 'off'})
        self.assertFalse(self.player.shuffle)
        self.engine.event('repeatModeDidChange', {'repeat': 'one'})
        self.assertEqual(self.player.repeat, 'one')
        self.engine.event('repeatModeDidChange', {'repeat': 'all'})
        self.assertEqual(self.player.repeat, 'all')
        self.engine.event('repeatModeDidChange', {'repeat': 'bogus'})
        self.assertEqual(self.player.repeat, 'none')
        self.engine.event('playbackVolumeDidChange', {'volume': 0.25})
        self.assertEqual(self.player.volume, 0.25)
        self.engine.event('playbackVolumeDidChange', {'volume': 7})
        self.assertEqual(self.player.volume, 1.0)  # clamped
        self.assertEqual(self.notified.count('volume'), 2)

    def test_next_repeat_cycles(self):
        self.assertEqual(self.player.next_repeat(), 'one')
        self.engine.event('repeatModeDidChange', {'repeat': 'one'})
        self.assertEqual(self.player.next_repeat(), 'all')
        self.engine.event('repeatModeDidChange', {'repeat': 'all'})
        self.assertEqual(self.player.next_repeat(), 'none')

    def test_other_events_and_bad_payloads_are_ignored(self):
        self.engine.event('queueItemsDidChange', {'index': 0, 'items': []})
        self.engine.event('authorizationStatusDidChange', {'authorized': True})
        self.engine.event('playbackTimeDidChange', None)
        self.engine.event('playbackTimeDidChange', {'error': 'MusicKit not initialized'})
        self.engine.event('playbackStateDidChange', 'playing')
        self.assertEqual(self.notified, [])
        self.assertEqual(self.player.state, 'none')

    def test_playback_error_is_a_signal(self):
        errors = []
        self.player.connect('error', lambda _p, message: errors.append(message))
        self.engine.event('mediaPlaybackError', {'message': 'CONTENT_UNAVAILABLE'})
        self.engine.event('mediaPlaybackError', {})
        self.assertEqual(errors[0], 'CONTENT_UNAVAILABLE')
        self.assertEqual(len(errors), 2)
        self.assertTrue(errors[1])  # a wording of its own


class EngineLifecycleTest(unittest.TestCase):
    def test_engine_coming_up_reads_now_playing_once(self):
        async def go():
            player, engine, app = make_player(state='down')
            engine.answers['now_playing'] = {
                'state': 'paused', 'track': TRACK, 'position': 90, 'duration': 214,
                'shuffle': 'on', 'repeat': 'all', 'volume': 0.4}
            engine.state = 'up'
            await asyncio.gather(*app.tasks)
            self.assertEqual(engine.calls, [('now_playing',)])
            self.assertEqual(player.state, 'paused')
            self.assertEqual(player.track.title, 'Harbour Lights')
            self.assertEqual((player.position, player.duration), (90.0, 214.0))
            self.assertTrue(player.shuffle)
            self.assertEqual(player.repeat, 'all')
            self.assertEqual(player.volume, 0.4)
            # The engine going down: nothing playing, the modes and the volume kept.
            engine.state = 'down'
            self.assertEqual(player.state, 'none')
            self.assertIsNone(player.track)
            self.assertEqual((player.position, player.duration), (0.0, 0.0))
            self.assertTrue(player.shuffle)
            self.assertEqual(player.volume, 0.4)
        asyncio.run(go())

    def test_an_engine_already_up_is_read_at_once(self):
        async def go():
            player, engine, app = make_player(state='up')
            engine.answers['now_playing'] = {'state': 'playing', 'track': TRACK,
                                             'position': 1, 'duration': 214}
            await asyncio.gather(*app.tasks)
            self.assertEqual(player.state, 'playing')
            self.assertEqual(player.track.id, 'i.demo0001')
        asyncio.run(go())

    def test_a_failed_read_leaves_nothing_playing(self):
        async def go():
            player, engine, app = make_player(state='down')
            engine.fail = EngineError('api', 'no')
            engine.state = 'up'
            await asyncio.gather(*app.tasks)
            self.assertEqual(player.state, 'none')
            self.assertIsNone(player.track)
        asyncio.run(go())


class CommandTest(unittest.TestCase):
    def test_play_starts_a_down_engine_when_signed_in(self):
        async def go():
            player, engine, app = make_player(state='down')
            await player.play({'kind': 'album', 'id': 'l.alb1'}, start_with=2)
            self.assertEqual(engine.calls[0], ('start', False))
            self.assertEqual(engine.calls[-1], ('play', 'album', 'l.alb1', 2, False))
            self.assertEqual(app.toasts, ['Starting playback engine…'])
        asyncio.run(go())

    def test_play_with_the_engine_up_plays_at_once(self):
        async def go():
            player, engine, app = make_player(state='up')
            await player.play({'kind': 'playlist', 'id': 'p.pl1'}, shuffle=True)
            self.assertEqual([call for call in engine.calls if call[0] != 'now_playing'],
                             [('play', 'playlist', 'p.pl1', None, True)])
            self.assertEqual(app.toasts, [])
        asyncio.run(go())

    def test_play_signed_out_is_not_signed_in(self):
        async def go():
            player, engine, app = make_player(signed_in=False, state='down')
            with self.assertRaises(EngineError) as raised:
                await player.play({'kind': 'station', 'id': 'ra.1'})
            self.assertEqual(raised.exception.code, 'not-signed-in')
            self.assertEqual(engine.calls, [])
        asyncio.run(go())

    def test_play_in_demo_mode_is_engine_down(self):
        async def go():
            player, engine, app = make_player(demo=True, state='down')
            with self.assertRaises(EngineError) as raised:
                await player.play({'kind': 'album', 'id': 'l.alb1'})
            self.assertEqual(raised.exception.code, 'engine-down')
            self.assertEqual(engine.calls, [])
        asyncio.run(go())

    def test_play_needs_a_target(self):
        async def go():
            player, engine, app = make_player(state='up')
            for play in (None, {}, {'kind': 'album'}, {'kind': '', 'id': 'x'}):
                with self.assertRaises(EngineError) as raised:
                    await player.play(play)
                self.assertEqual(raised.exception.code, 'usage')
        asyncio.run(go())

    def test_commands_are_thin(self):
        async def go():
            player, engine, app = make_player(state='up')
            engine.answers['volume'] = 0.3
            engine.answers['shuffle'] = {'shuffle': 'on', 'repeat': 'none'}
            await player.toggle()  # nothing under way: play
            engine.event('playbackStateDidChange', {'state': 'loading'})
            await player.toggle()  # loading counts as under way: pause
            engine.event('playbackStateDidChange', {'state': 'paused'})
            await player.pause()
            await player.resume()
            await player.next()
            await player.previous()
            await player.stop()
            await player.seek(42.5)
            self.assertEqual(await player.set_volume(0.3), 0.3)
            self.assertEqual(await player.set_shuffle(True), {'shuffle': 'on', 'repeat': 'none'})
            await player.set_shuffle(False)
            await player.toggle_shuffle()
            await player.set_repeat('all')
            await player.cycle_repeat()
            await player.play_next('song', 'i.1')
            await player.play_later('album', 'l.2')
            self.assertEqual([call for call in engine.calls if call[0] != 'now_playing'], [
                ('control', 'play'), ('control', 'pause'), ('control', 'pause'),
                ('control', 'play'),
                ('control', 'next'), ('control', 'previous'), ('control', 'stop'),
                ('seek', 42.5), ('volume', 0.3), ('shuffle', 'on'), ('shuffle', 'off'),
                ('shuffle', 'toggle'), ('repeat', 'all'), ('repeat', 'cycle'),
                ('play_next', 'song', 'i.1'), ('play_later', 'album', 'l.2')])
            # Nothing above touched the properties: only events do.
            self.assertEqual(player.volume, 1.0)
            self.assertFalse(player.shuffle)
        asyncio.run(go())

    def test_errors_pass_through(self):
        async def go():
            player, engine, app = make_player(state='up')
            engine.fail = EngineError('engine-down', 'gone')
            with self.assertRaises(EngineError) as raised:
                await player.toggle()
            self.assertEqual(raised.exception.code, 'engine-down')
        asyncio.run(go())


class FormatTimeTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(format_time(0), '0:00')
        self.assertEqual(format_time(7.4), '0:07')
        self.assertEqual(format_time(61), '1:01')
        self.assertEqual(format_time(214.6), '3:35')
        self.assertEqual(format_time(3600), '1:00:00')
        self.assertEqual(format_time(3725), '1:02:05')
        self.assertEqual(format_time(-3), '0:00')
        self.assertEqual(format_time(None), '0:00')
        self.assertEqual(format_time(30, remaining=True), '-0:30')


if __name__ == '__main__':
    unittest.main()
