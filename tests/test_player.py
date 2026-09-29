"""The Player: its properties fed by synthetic engine events, and its commands over a fake
engine. GObject only, no GTK or display; the commands run under asyncio.run."""

import asyncio
import contextlib
import time
import unittest
from unittest import mock

from gi.repository import GObject

from tests import ROOT  # noqa: F401  registers src/ as applemusic

from applemusic.backend.errors import EngineError
from applemusic.lyrics import Lyrics
from applemusic.player import (ACTIVE_STATES, STOPPED_STATES, NowPlaying, Player, format_time,
                               playback_error_text)


class Clock:
    """A stand-in time.monotonic(): `now`, moved by advance()."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@contextlib.contextmanager
def patched_clocks(start=1000.0):
    """time.monotonic() under the test's control, as the Player and the MPRIS service read
    it (both read the time module's). asyncio's own timers stand still meanwhile: no
    `asyncio.sleep()` of more than 0 inside."""
    clock = Clock(start)
    with mock.patch.object(time, 'monotonic', clock):
        yield clock

# An invented track, as the bridge's formatTrack shapes it.
TRACK = {
    'id': 'i.demo0001', 'catalogId': '1000000001', 'title': 'Harbour Lights',
    'artist': 'The Invented Band', 'album': 'Fictional Album', 'trackNumber': 3,
    'discNumber': 1, 'durationMs': 214000, 'durationLabel': '3:34', 'explicit': False,
    'artUrl': 'https://example.invalid/art/256x256bb.jpg', 'index': 2,
}

# An invented queue of three, TRACK at index 2.
QUEUE = {'index': 2, 'items': [
    dict(TRACK, id='i.demo0003', catalogId='1000000003', title='Shoreline', index=0),
    dict(TRACK, id='i.demo0002', catalogId='1000000002', title='Pilot Light', index=1),
    TRACK,
]}

LYRICS = {'synced': True, 'lines': [
    {'startMs': 1000, 'endMs': 3000, 'text': 'One'},
    {'startMs': 4000, 'endMs': 6000, 'text': 'Two'},
]}


class FakeEngine(GObject.Object):
    """The Engine's surface the Player uses: the event signal, state, and the commands,
    which are recorded and answer what the test put in `answers`. A command named in
    `gates` (an asyncio.Event each) waits for its gate before answering, so a test can
    release an answer late; `now_playing` raises EngineError('api') without an answer, as
    the real engine does when the page gives none."""

    __gsignals__ = {
        'event': (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }

    state = GObject.Property(type=str, default='down')
    authorized = GObject.Property(type=bool, default=True)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.answers = {}
        self.gates = {}
        self.fail = None  # an EngineError every command raises

    def event(self, name, data):
        self.emit('event', name, data)

    def gate(self, name):
        """Hold the next `name` command until the returned event is set."""
        self.gates[name] = asyncio.Event()
        return self.gates[name]

    async def _command(self, name, *args):
        self.calls.append((name, *args))
        gate = self.gates.get(name)
        if gate is not None:
            await gate.wait()
        if self.fail is not None:
            raise self.fail
        return self.answers.get(name)

    async def start(self, visible=None):
        self.calls.append(('start', visible))
        self.state = 'up'

    async def now_playing(self):
        answer = await self._command('now_playing')
        if not isinstance(answer, dict):
            raise EngineError('api', 'the page gave no now-playing answer')
        return answer

    async def play(self, kind, item_id, start_with=None, shuffle=None):
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

    async def queue(self):
        return await self._command('queue')

    async def queue_jump(self, index):
        return await self._command('queue_jump', index)

    async def lyrics(self, catalog_id):
        return await self._command('lyrics', catalog_id)


class FakeSettings:
    def __init__(self, signed_in=True):
        self.signed_in = signed_in

    def get_boolean(self, key):
        assert key == 'signed-in'
        return self.signed_in


class FakeApp:
    """What the Player asks of the Application."""

    def account_key(self, name):
        return name  # the release build's keys

    def __init__(self, engine, signed_in=True, demo=False):
        self.engine = engine
        self.settings = FakeSettings(signed_in)
        self.demo = demo
        self.toasts = []
        self.tasks = []
        self.spawned = []  # the names of coroutines spawned with no loop running

    def toast(self, title, *_args):
        self.toasts.append(title)

    def report(self, error):
        self.toasts.append(error)

    def player_command(self, coro, on_error=None):
        """As the app's: a task whose EngineError is reported, then on_error() called."""
        async def command():
            try:
                await coro
            except EngineError as error:
                self.report(error)
                if on_error is not None:
                    on_error()
        return self.spawn(command())

    def spawn(self, coro):
        """A task on the running loop; without one (the event tests) the coroutine is only
        noted by name and closed."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.spawned.append(coro.__qualname__)
            coro.close()
            return None
        task = loop.create_task(coro)
        self.tasks.append(task)
        return task

    async def settle(self):
        """Wait for every task spawned so far, and for what they spawned."""
        while any(not task.done() for task in self.tasks):
            await asyncio.gather(*self.tasks, return_exceptions=True)


def make_player(signed_in=True, demo=False, state='down', grace_ms=0):
    """A Player over a FakeEngine; a null item clears the track at once unless `grace_ms`
    says otherwise (the synchronous tests want no timer)."""
    engine = FakeEngine()
    engine.state = state
    app = FakeApp(engine, signed_in, demo)
    player = Player(app)
    player.track_grace_ms = grace_ms
    return player, engine, app


def pump_until(predicate, timeout=1.0):
    """Run the default main context until predicate() holds (True) or `timeout` seconds
    pass (False): what a GLib timeout of the Player's needs."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    wake = GLib.timeout_add(5, lambda: GLib.SOURCE_CONTINUE)
    try:
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            context.iteration(True)
        return True
    finally:
        GLib.source_remove(wake)


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
        with patched_clocks() as clock:
            self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            clock.advance(41)
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

    def test_a_null_item_waits_for_the_next_one(self):
        """MusicKit sends a null item between queues, right before the next item: the
        track is cleared only after the grace passes with no new item."""
        player, engine, _app = make_player(grace_ms=40)
        tracks = []
        player.connect('notify::track', lambda p, _pspec: tracks.append(
            p.track.id if p.track is not None else None))
        engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        engine.event('playbackTimeDidChange', {'position': 1, 'duration': 214})
        engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})
        self.assertIsNotNone(player.track)  # not yet
        # The 0 MusicKit reports as it clears the queue: the item is on its way out.
        engine.event('playbackStateDidChange', {'state': 'stopped', 'position': 0})
        self.assertEqual((player.state, player.position), ('stopped', 1.0))
        other = dict(TRACK, id='i.demo0002', catalogId='1000000002', index=0)
        engine.event('nowPlayingItemDidChange', {'track': other, 'index': 0})
        self.assertFalse(pump_until(lambda: player.track is None, timeout=0.12))
        self.assertEqual(tracks, ['i.demo0001', 'i.demo0002'])  # never None in between
        # A null alone: cleared once the grace is over.
        engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})
        engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})  # one timer
        self.assertTrue(pump_until(lambda: player.track is None))
        self.assertEqual(tracks[-1], None)
        self.assertEqual(player.queue_index, -1)
        # The engine going down clears at once, and cancels a grace under way.
        engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})
        player.apply(None)
        self.assertIsNone(player.track)
        self.assertEqual(player._clear_source, 0)
        engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        self.assertFalse(pump_until(lambda: player.track is None, timeout=0.1))

    def test_a_track_without_artwork(self):
        self.engine.event('nowPlayingItemDidChange',
                          {'track': dict(TRACK, artUrl=None, durationMs=None), 'index': 0})
        self.assertIsNone(self.player.track.artwork_url)
        self.assertEqual(self.player.track.duration_ms, 0)
        self.assertEqual(self.player.duration, 0.0)

    def test_a_track_change_notifies_with_its_own_times(self):
        self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        self.engine.event('playbackTimeDidChange', {'position': 200, 'duration': 214})
        seen = []
        self.player.connect('notify::track', lambda p, _pspec: seen.append(
            (p.track.id, p.position, p.duration)))
        other = dict(TRACK, id='i.demo0002', catalogId='1000000002', durationMs=180000,
                     index=3)
        self.engine.event('nowPlayingItemDidChange', {'track': other, 'index': 3})
        # The handler read the new item's times, not the previous one's.
        self.assertEqual(seen, [('i.demo0002', 0.0, 180.0)])
        # An item without a length after one with it: 0, not the previous item's.
        unknown = dict(TRACK, id='i.demo0003', catalogId='1000000003', durationMs=None, index=4)
        self.engine.event('nowPlayingItemDidChange', {'track': unknown, 'index': 4})
        self.assertEqual((self.player.position, self.player.duration), (0.0, 0.0))
        self.engine.event('playbackDurationDidChange', {'duration': 190})
        self.assertEqual(self.player.duration, 190.0)

    def test_a_zero_duration_keeps_the_items_length(self):
        self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        self.engine.event('playbackStateDidChange',
                          {'state': 'loading', 'position': 0, 'duration': 0})
        self.assertEqual(self.player.duration, 214.0)
        self.engine.event('playbackTimeDidChange', {'position': 1, 'duration': 213.5})
        self.assertEqual(self.player.duration, 213.5)

    def test_the_same_song_at_another_queue_position_keeps_its_times(self):
        with patched_clocks() as clock:
            self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            clock.advance(41)
            self.engine.event('playbackTimeDidChange', {'position': 40, 'duration': 214})
            self.notified.clear()
            self.engine.event('nowPlayingItemDidChange',
                              {'track': dict(TRACK, index=5), 'index': 5})
            self.assertEqual(self.notified, ['track'])  # the queue was reordered under it
            self.assertEqual(self.player.track.index, 5)
            self.assertEqual((self.player.position, self.player.duration), (40.0, 214.0))

    def test_a_position_an_answer_gave_goes_on(self):
        """The engine coming up mid-song: the answer's position, then MusicKit's ticks from
        there, all taken (a seek of the app's own ends the hold too)."""
        with patched_clocks() as clock:
            self.player.apply({'track': TRACK, 'state': 'playing', 'position': 90,
                               'duration': 214})
            clock.advance(0.25)
            self.engine.event('playbackTimeDidChange', {'position': 90, 'duration': 214})
            clock.advance(0.75)
            self.engine.event('playbackTimeDidChange', {'position': 91, 'duration': 214})
            self.assertEqual(self.player.position, 91.0)
            self.engine.event('nowPlayingItemDidChange',
                              {'track': dict(TRACK, id='i.demo0002', index=3), 'index': 3})

            async def seek():
                await self.player.seek(60)
            asyncio.run(seek())
            self.engine.event('playbackTimeDidChange', {'position': 60, 'duration': 214})
            self.assertEqual(self.player.position, 60.0)

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
        self.assertEqual(self.player.duration, 214.0)  # no duration in the event: kept
        for state in ACTIVE_STATES:
            self.engine.event('playbackStateDidChange', {'state': state})
            self.assertTrue(self.player.active, state)
        self.engine.event('playbackStateDidChange', {'state': 'stopped'})
        self.assertFalse(self.player.active)

    def test_seeking_keeps_the_state_before_it(self):
        self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        self.engine.event('playbackStateDidChange', {'state': 'playing'})
        self.engine.event('playbackStateDidChange', {'state': 'seeking', 'position': 30})
        self.assertEqual((self.player.state, self.player.resting), ('seeking', 'playing'))
        self.assertTrue(self.player.active)  # the bar keeps its Pause icon
        self.assertFalse(self.player.stopped)

        async def toggle():
            await self.player.toggle()
        asyncio.run(toggle())
        self.assertEqual(self.engine.calls[-1], ('control', 'pause'))
        self.engine.event('playbackStateDidChange', {'state': 'paused'})
        self.engine.event('playbackStateDidChange', {'state': 'seeking'})
        self.assertEqual(self.player.resting, 'paused')
        self.assertFalse(self.player.active)
        self.engine.event('playbackStateDidChange', {'state': 'stopped'})
        self.engine.event('playbackStateDidChange', {'state': 'seeking'})
        self.assertTrue(self.player.stopped)

    def test_stopped_is_no_item_or_a_stopped_state(self):
        self.assertTrue(self.player.stopped)  # nothing yet
        self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        for state in ACTIVE_STATES + ('paused', 'seeking'):
            self.engine.event('playbackStateDidChange', {'state': state})
            self.assertFalse(self.player.stopped, state)
        for state in STOPPED_STATES:
            self.engine.event('playbackStateDidChange', {'state': state})
            self.assertTrue(self.player.stopped, state)
        self.engine.event('playbackStateDidChange', {'state': 'playing'})
        self.engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})
        self.assertTrue(self.player.stopped)  # playing, but no item

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
        with patched_clocks() as clock:
            self.engine.event('playbackStateDidChange', {'state': 'playing'})
            self.engine.event('playbackTimeDidChange', {'position': 10, 'duration': 12})
            clock.advance(0.5)
            self.assertEqual(self.player.estimated_position(), 10.5)
            clock.advance(10)
            self.assertEqual(self.player.estimated_position(), 11.0)  # a second at most
            self.engine.event('playbackTimeDidChange', {'position': 11.5, 'duration': 12})
            clock.advance(0.8)
            self.assertEqual(self.player.estimated_position(), 12.0)  # never past the end
            self.engine.event('playbackStateDidChange', {'state': 'paused'})
            clock.advance(5)
            self.assertEqual(self.player.estimated_position(), 11.5)  # paused: as reported
            # Resumed: it runs on from the moment of the resume, not from the last report.
            self.engine.event('playbackStateDidChange', {'state': 'playing'})
            clock.advance(0.25)
            self.assertEqual(self.player.estimated_position(), 11.75)

    def test_the_estimate_never_runs_backwards(self):
        """MusicKit reports whole seconds four times a second: the estimate follows the
        clock within a quarter second and never decreases."""
        with patched_clocks() as clock:
            self.engine.event('playbackStateDidChange', {'state': 'playing'})
            estimates = []
            for step in range(20):
                second = 10 + step // 4
                self.engine.event('playbackTimeDidChange', {'position': second, 'duration': 214})
                estimates.append(self.player.estimated_position())
                self.assertAlmostEqual(estimates[-1], 10 + step / 4, delta=0.25)
                clock.advance(0.25)
            self.assertEqual(estimates, sorted(estimates))
            # After the tick from 12 to 13 plus 0.3 s, the estimate reaches a line at 13.2 s.
            clock.advance(0.3)
            lyrics = Lyrics({'synced': True, 'lines': [
                {'startMs': 1000, 'text': 'One'}, {'startMs': 13200, 'text': 'Two'}]})
            self.assertEqual(lyrics.index_at(self.player.estimated_position()), 1)

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

    def test_playback_error_is_a_signal_with_a_sentence(self):
        errors = []
        self.player.connect('error', lambda _p, message: errors.append(message))
        with self.assertLogs('applemusic.player', level='WARNING') as logged:
            self.engine.event('mediaPlaybackError',
                              {'code': 'CONTENT_UNAVAILABLE', 'message': 'x'})
            self.engine.event('mediaPlaybackError', {'code': 'BOGUS', 'message': 'y'})
            self.engine.event('mediaPlaybackError', {})
        self.assertEqual(errors[0], "This isn't available in your country or region")
        self.assertEqual(errors[1], 'This could not be played')  # an unknown code
        self.assertEqual(errors[2], 'This could not be played')
        self.assertIn('CONTENT_UNAVAILABLE: x', logged.output[0])  # the raw text, logged
        for code, sentence in (('GEO_BLOCK', "This isn't available in your country or region"),
                               ('CONTENT_RESTRICTED', 'This content is restricted'),
                               ('SUBSCRIPTION_ERROR',
                                'An Apple Music subscription is needed to play this'),
                               ('MEDIA_LICENSE', 'The engine could not play protected content'),
                               ('', 'This could not be played')):
            self.assertEqual(playback_error_text(code), sentence, code)


class StalePositionTest(unittest.TestCase):
    """After a track change MusicKit reports the previous item's position once more: the
    Player drops it, so no view sees it."""

    def test_the_previous_items_position_is_dropped_after_a_change(self):
        with patched_clocks() as clock:
            player, engine, _app = make_player()
            engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            engine.event('playbackStateDidChange',
                         {'state': 'playing', 'position': 195, 'duration': 214})
            clock.advance(5)
            engine.event('playbackTimeDidChange', {'position': 200, 'duration': 214})
            self.assertEqual(player.position, 200.0)
            other = dict(TRACK, id='i.demo0009', catalogId='1000000009', title='Later',
                         durationMs=180000, index=3)
            engine.event('nowPlayingItemDidChange', {'track': other, 'index': 3})
            # The skip's state transitions carry A's position and length once more.
            engine.event('playbackStateDidChange',
                         {'state': 'playing', 'position': 200, 'duration': 214})
            self.assertEqual(player.state, 'playing')  # the state is still taken
            self.assertEqual((player.position, player.duration), (0.0, 180.0))
            engine.event('playbackTimeDidChange', {'position': 200, 'duration': 214})
            self.assertEqual((player.position, player.duration), (0.0, 180.0))
            # The new item's own positions are taken, a little ahead of the clock too.
            engine.event('playbackTimeDidChange', {'position': 1, 'duration': 180})
            self.assertEqual(player.position, 1.0)
            clock.advance(1)
            engine.event('playbackTimeDidChange', {'position': 2.5, 'duration': 180})
            self.assertEqual(player.position, 2.5)
            engine.event('playbackTimeDidChange', {'position': 5, 'duration': 180})
            self.assertEqual(player.position, 2.5)  # 5 s in, 1 s after it started: stale
            # Once the hold is over, a jump is a seek and is taken.
            clock.advance(2)
            engine.event('playbackTimeDidChange', {'position': 60, 'duration': 180})
            self.assertEqual(player.position, 60.0)

    def test_the_same_song_at_another_position_is_not_a_new_start(self):
        with patched_clocks() as clock:
            player, engine, _app = make_player()
            engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            clock.advance(5)
            engine.event('playbackTimeDidChange', {'position': 200, 'duration': 214})
            engine.event('nowPlayingItemDidChange', {'track': dict(TRACK, index=4), 'index': 4})
            engine.event('playbackTimeDidChange', {'position': 201, 'duration': 214})
            self.assertEqual(player.position, 201.0)  # a reorder: its positions go on


class EngineLifecycleTest(unittest.TestCase):
    def test_engine_coming_up_reads_now_playing_once(self):
        async def go():
            player, engine, app = make_player(state='down')
            engine.answers['now_playing'] = {
                'state': 'paused', 'track': TRACK, 'position': 90, 'duration': 214,
                'shuffle': 'on', 'repeat': 'all', 'volume': 0.4}
            engine.answers['queue'] = QUEUE
            engine.state = 'up'
            await app.settle()
            self.assertEqual(engine.calls, [('now_playing',), ('queue',), ('lyrics', '1000000001')])
            self.assertEqual(player.state, 'paused')
            self.assertEqual(player.queue.get_n_items(), 3)
            self.assertEqual(player.queue_index, 2)
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
            self.assertEqual(player.queue.get_n_items(), 0)
            self.assertEqual(player.queue_index, -1)
            self.assertIsNone(player.lyrics)
        asyncio.run(go())

    def test_an_engine_already_up_is_read_at_once(self):
        async def go():
            player, engine, app = make_player(state='up')
            engine.answers['now_playing'] = {'state': 'playing', 'track': TRACK,
                                             'position': 1, 'duration': 214}
            await app.settle()
            self.assertEqual(player.state, 'playing')
            self.assertEqual(player.track.id, 'i.demo0001')
        asyncio.run(go())

    def test_a_failed_read_leaves_nothing_playing(self):
        async def go():
            player, engine, app = make_player(state='down')
            engine.fail = EngineError('api', 'no')
            engine.state = 'up'
            await app.settle()
            self.assertEqual(player.state, 'none')
            self.assertIsNone(player.track)
        asyncio.run(go())

    def test_events_during_the_read_are_newer_than_its_answer(self):
        """A state and a track arrive while now_playing() is on its way: the answer, older,
        sets only the modes and the volume."""
        async def go():
            player, engine, app = make_player(state='down')
            gate = engine.gate('now_playing')
            engine.answers['now_playing'] = {
                'state': 'paused', 'track': dict(TRACK, id='i.old', index=0), 'position': 90,
                'duration': 214, 'shuffle': 'on', 'repeat': 'all', 'volume': 0.4}
            engine.state = 'up'
            await asyncio.sleep(0)
            engine.event('playbackStateDidChange', {'state': 'loading'})
            engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            gate.set()
            await app.settle()
            self.assertEqual(player.state, 'loading')
            self.assertEqual(player.track.id, 'i.demo0001')
            self.assertEqual(player.position, 0.0)
            self.assertTrue(player.shuffle)
            self.assertEqual(player.repeat, 'all')
            self.assertEqual(player.volume, 0.4)
        asyncio.run(go())

    def test_a_reloaded_page_resets_and_is_read_again(self):
        """The engine's bridgeReset: the page loaded a new document, and nothing plays in
        it until MusicKit there says so."""
        async def go():
            player, engine, app = make_player(state='up')
            await app.settle()
            engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            engine.event('playbackStateDidChange', {'state': 'playing', 'position': 30})
            await app.settle()
            reads = engine.calls.count(('now_playing',))
            engine.event('bridgeReset', None)
            self.assertIsNone(player.track)
            self.assertEqual(player.state, 'none')
            self.assertEqual((player.position, player.duration), (0.0, 0.0))
            await app.settle()
            self.assertEqual(engine.calls.count(('now_playing',)), reads + 1)
        asyncio.run(go())


class CommandTest(unittest.TestCase):
    def test_play_starts_a_down_engine_when_signed_in(self):
        async def go():
            player, engine, app = make_player(state='down')
            await player.play({'kind': 'album', 'id': 'l.alb1'}, start_with=2)
            self.assertEqual(engine.calls[0], ('start', None))  # the preferred mode
            self.assertEqual(engine.calls[-1], ('play', 'album', 'l.alb1', 2, None))
            self.assertEqual(app.toasts, ['Starting playback engine…'])
            await player.play({'kind': 'album', 'id': 'l.alb1'}, shuffle=False)
            self.assertEqual(engine.calls[-1], ('play', 'album', 'l.alb1', None, False))
        asyncio.run(go())

    def test_play_requests_run_in_order_and_the_newest_wins(self):
        async def go():
            player, engine, app = make_player(state='up')
            pending = []
            player.connect('notify::pending', lambda p, _pspec: pending.append(p.pending))
            gate = engine.gate('play')
            first = asyncio.ensure_future(player.play({'kind': 'album', 'id': 'l.a'}))
            await asyncio.sleep(0)
            self.assertTrue(player.pending)
            second = asyncio.ensure_future(player.play({'kind': 'album', 'id': 'l.b'}))
            third = asyncio.ensure_future(player.play({'kind': 'album', 'id': 'l.c'}))
            await asyncio.sleep(0)
            plays = [call for call in engine.calls if call[0] == 'play']
            self.assertEqual(len(plays), 1)  # the first is with the engine; the rest wait
            gate.set()
            await asyncio.gather(first, second, third)
            plays = [call[2] for call in engine.calls if call[0] == 'play']
            self.assertEqual(plays, ['l.a', 'l.c'])  # the second was superseded by the third
            self.assertFalse(player.pending)
            self.assertEqual(pending, [True, False])
        asyncio.run(go())

    def test_a_failed_request_is_no_longer_pending(self):
        async def go():
            player, engine, app = make_player(signed_in=False, state='down')
            with self.assertRaises(EngineError):
                await player.play({'kind': 'album', 'id': 'l.a'})
            self.assertFalse(player.pending)
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
            await player.set_repeat('all')
            await player.play_next('song', 'i.1')
            await player.play_later('album', 'l.2')
            await player.queue_jump(1)
            self.assertEqual([call for call in engine.calls if call[0] != 'now_playing'], [
                ('control', 'play'), ('control', 'pause'), ('control', 'pause'),
                ('control', 'play'),
                ('control', 'next'), ('control', 'previous'), ('control', 'stop'),
                ('seek', 42.5), ('volume', 0.3), ('shuffle', 'on'), ('shuffle', 'off'),
                ('repeat', 'all'),
                ('play_next', 'song', 'i.1'), ('play_later', 'album', 'l.2'),
                ('queue_jump', 1)])
            # Nothing above touched the properties: only events do.
            self.assertEqual(player.volume, 1.0)
            self.assertFalse(player.shuffle)
        asyncio.run(go())

    def test_previous_restarts_the_item_a_few_seconds_in(self):
        async def go():
            player, engine, app = make_player(state='up')
            await app.settle()
            engine.calls.clear()
            player.apply({'track': TRACK, 'state': 'paused', 'position': 30})
            await player.previous()
            self.assertEqual(engine.calls, [('seek', 0)])
            player.apply({'position': 1})
            await player.previous()
            self.assertEqual(engine.calls[-1], ('control', 'previous'))
        asyncio.run(go())

    def test_errors_pass_through(self):
        async def go():
            player, engine, app = make_player(state='up')
            engine.fail = EngineError('engine-down', 'gone')
            with self.assertRaises(EngineError) as raised:
                await player.toggle()
            self.assertEqual(raised.exception.code, 'engine-down')
        asyncio.run(go())


class QueueTest(unittest.TestCase):
    """The queue store and index, from the queue events and the item playing."""

    def setUp(self):
        self.player, self.engine, self.app = make_player()
        self.changes = []
        self.player.queue.connect(
            'items-changed', lambda _s, position, removed, added: self.changes.append(
                (position, removed, added)))

    def ids(self):
        return [entry.id for entry in self.player.queue]

    def test_queue_items_event_fills_the_store(self):
        self.engine.event('queueItemsDidChange', QUEUE)
        self.assertEqual(self.ids(), ['i.demo0003', 'i.demo0002', 'i.demo0001'])
        self.assertEqual([entry.index for entry in self.player.queue], [0, 1, 2])
        self.assertEqual(self.player.queue_index, 2)
        self.assertEqual(self.changes, [(0, 0, 3)])
        # The same snapshot again rebinds nothing; its index is taken.
        self.engine.event('queueItemsDidChange', dict(QUEUE, index=0))
        self.assertEqual(self.changes, [(0, 0, 3)])
        self.assertEqual(self.player.queue_index, 0)
        # The same entries with a title filled in: replaced (the rows show the new title).
        renamed = [dict(item) for item in QUEUE['items']]
        renamed[1]['title'] = 'Pilot Light (Live)'
        self.engine.event('queueItemsDidChange', {'index': 0, 'items': renamed})
        self.assertEqual(self.changes, [(0, 0, 3), (0, 3, 3)])
        self.assertEqual(self.player.queue.get_item(1).title, 'Pilot Light (Live)')
        # A different queue replaces it in one splice.
        self.engine.event('queueItemsDidChange', {'index': 0, 'items': QUEUE['items'][:1]})
        self.assertEqual(self.ids(), ['i.demo0003'])
        self.assertEqual(self.changes, [(0, 0, 3), (0, 3, 3), (0, 3, 1)])

    def test_an_identical_snapshot_makes_no_objects(self):
        self.engine.event('queueItemsDidChange', QUEUE)
        entries = list(self.player.queue)
        with mock.patch('applemusic.player.NowPlaying', side_effect=AssertionError('made')):
            self.engine.event('queueItemsDidChange', dict(QUEUE, index=1))
        self.assertEqual(list(self.player.queue), entries)  # the same objects
        self.assertEqual(self.player.queue_index, 1)

    def test_index_before_playback_is_the_tracks_place(self):
        self.engine.event('nowPlayingItemDidChange', {'track': QUEUE['items'][1], 'index': 1})
        self.engine.event('queueItemsDidChange', dict(QUEUE, index=-1))
        self.assertEqual(self.player.queue_index, 1)

    def test_queue_position_event_moves_the_index(self):
        self.engine.event('queueItemsDidChange', QUEUE)
        self.engine.event('queuePositionDidChange', {'index': 1, 'oldIndex': 2})
        self.assertEqual(self.player.queue_index, 1)
        self.engine.event('queuePositionDidChange', {'index': 7, 'oldIndex': 1})
        self.assertEqual(self.player.queue_index, -1)  # nowhere in the queue held
        self.engine.event('queuePositionDidChange', {'index': None})
        self.assertEqual(self.player.queue_index, -1)

    def test_a_queued_item_playing_points_the_index_without_a_read(self):
        self.engine.event('queueItemsDidChange', dict(QUEUE, index=-1))
        self.app.spawned.clear()
        self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        self.assertEqual(self.player.queue_index, 2)
        self.assertNotIn('Player.refresh_queue', self.app.spawned)
        # At another index than its own: found by id.
        moved = dict(TRACK, index=5)
        self.engine.event('nowPlayingItemDidChange', {'track': moved, 'index': 5})
        self.assertEqual(self.player.queue_index, 2)
        self.assertNotIn('Player.refresh_queue', self.app.spawned)

    def test_an_item_not_in_the_queue_reads_it_again(self):
        self.engine.event('queueItemsDidChange', QUEUE)
        self.app.spawned.clear()
        stranger = dict(TRACK, id='i.demo0009', catalogId='1000000009', index=0)
        self.engine.event('nowPlayingItemDidChange', {'track': stranger, 'index': 0})
        self.assertEqual(self.player.queue_index, -1)
        self.assertEqual(self.app.spawned.count('Player.refresh_queue'), 1)

    def test_no_item_clears_the_index_and_keeps_the_entries(self):
        self.engine.event('queueItemsDidChange', QUEUE)
        self.engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
        self.engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})
        self.assertEqual(self.player.queue_index, -1)
        self.assertEqual(len(self.ids()), 3)

    def test_refresh_queue_reads_the_engine(self):
        async def go():
            player, engine, app = make_player(state='up')
            await app.settle()
            engine.answers['queue'] = QUEUE
            await player.refresh_queue()
            self.assertEqual([entry.title for entry in player.queue],
                             ['Shoreline', 'Pilot Light', 'Harbour Lights'])
            self.assertEqual(player.queue_index, 2)
            engine.fail = EngineError('engine-down', 'gone')
            await player.refresh_queue()  # logged, the queue kept
            self.assertEqual(player.queue.get_n_items(), 3)
        asyncio.run(go())

    def test_a_queue_event_during_the_read_wins(self):
        async def go():
            player, engine, app = make_player(state='up')
            await app.settle()
            gate = engine.gate('queue')
            engine.answers['queue'] = {'index': 0, 'items': QUEUE['items'][:1]}
            task = app.spawn(player.refresh_queue())
            await asyncio.sleep(0)
            engine.event('queueItemsDidChange', QUEUE)  # newer than the answer
            gate.set()
            await task
            self.assertEqual(self.ids_of(player), ['i.demo0003', 'i.demo0002', 'i.demo0001'])
        asyncio.run(go())

    @staticmethod
    def ids_of(player):
        return [entry.id for entry in player.queue]

    def test_apply_takes_a_queue_and_bad_snapshots(self):
        self.player.apply({'track': TRACK, 'queue': QUEUE})
        self.assertEqual(self.player.queue_index, 2)
        self.player.apply_queue({'index': 0, 'items': 'nonsense'})
        self.assertEqual(self.player.queue.get_n_items(), 0)
        self.player.apply_queue(None)
        self.assertEqual(self.player.queue_index, -1)


class LyricsTest(unittest.TestCase):
    """Lyrics read once per catalog song as it starts, from the engine."""

    def test_a_new_item_asks_the_engine_once(self):
        async def go():
            player, engine, app = make_player(state='up')
            await app.settle()
            engine.answers['lyrics'] = LYRICS
            loading = []
            player.connect('notify::lyrics-loading',
                           lambda p, _pspec: loading.append(p.lyrics_loading))
            engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            self.assertTrue(player.lyrics_loading)
            self.assertIsNone(player.lyrics)
            await app.settle()
            # The item is not in the (empty) queue, so the queue is read as well.
            self.assertEqual(engine.calls,
                             [('now_playing',), ('queue',), ('lyrics', '1000000001')])
            self.assertIsInstance(player.lyrics, Lyrics)
            self.assertEqual(player.lyrics.catalog_id, '1000000001')
            self.assertEqual(len(player.lyrics), 2)
            self.assertEqual(loading, [True, False])
            # The same song again (repeat one): kept, not asked again.
            engine.event('nowPlayingItemDidChange', {'track': dict(TRACK, index=3), 'index': 3})
            await app.settle()
            self.assertEqual(engine.calls.count(('lyrics', '1000000001')), 1)
            self.assertIsNotNone(player.lyrics)
            # Another song: asked; none for it: None.
            engine.answers['lyrics'] = {'synced': False, 'lines': []}
            other = dict(TRACK, id='i.demo0002', catalogId='1000000002', index=0)
            engine.event('nowPlayingItemDidChange', {'track': other, 'index': 0})
            self.assertIsNone(player.lyrics)
            await app.settle()
            self.assertEqual(engine.calls[-1], ('lyrics', '1000000002'))
            self.assertIsNone(player.lyrics)
            self.assertFalse(player.lyrics_loading)
            # Nothing playing: nothing asked, nothing loading.
            engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})
            await app.settle()
            self.assertIsNone(player.lyrics)
            self.assertFalse(player.lyrics_loading)
        asyncio.run(go())

    def test_an_answer_for_a_song_no_longer_playing_is_dropped(self):
        async def go():
            player, engine, app = make_player(state='up')
            await app.settle()
            engine.answers['lyrics'] = LYRICS
            engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            other = dict(TRACK, id='i.demo0002', catalogId='1000000002', index=0)
            engine.event('nowPlayingItemDidChange', {'track': other, 'index': 0})
            await app.settle()
            self.assertEqual(player.lyrics.catalog_id, '1000000002')
            self.assertFalse(player.lyrics_loading)
        asyncio.run(go())

    def test_a_late_answer_for_the_song_before_is_dropped(self):
        """A → B while A's read is still out: A's task is cancelled and B's answer is what
        shows; A → B → A: A's lyrics end up shown once, with one read out at a time."""
        async def go():
            player, engine, app = make_player(state='up')
            await app.settle()
            gate = engine.gate('lyrics')
            engine.answers['lyrics'] = LYRICS
            engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            await asyncio.sleep(0)
            first = player._lyrics_task
            other = dict(TRACK, id='i.demo0002', catalogId='1000000002', index=0)
            engine.event('nowPlayingItemDidChange', {'track': other, 'index': 0})
            await asyncio.sleep(0)
            self.assertTrue(first.cancelled() or first.done())
            gate.set()
            await app.settle()
            self.assertEqual(player.lyrics.catalog_id, '1000000002')
            self.assertFalse(player.lyrics_loading)
            # Back to A while B's read is out, then A's answer: shown once.
            gate.clear()
            shown = []
            player.connect('notify::lyrics', lambda p, _pspec: shown.append(
                p.lyrics.catalog_id if p.lyrics is not None else None))
            engine.event('nowPlayingItemDidChange', {'track': other, 'index': 0})
            engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            await asyncio.sleep(0)
            gate.set()
            await app.settle()
            self.assertEqual(player.lyrics.catalog_id, '1000000001')
            self.assertEqual(shown, [None, '1000000001'])  # cleared once, then A's once
            self.assertFalse(player.lyrics_loading)
        asyncio.run(go())

    def test_a_null_item_or_the_engine_going_clears_the_lyrics(self):
        """Lyrics shown, or being read: a null item (and the engine going down) clears the
        lyrics, the loading flag and the read under way."""
        async def go():
            for how in ('null', 'down'):
                player, engine, app = make_player(state='up')
                await app.settle()
                gate = engine.gate('lyrics')
                engine.answers['lyrics'] = LYRICS
                engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
                await asyncio.sleep(0)
                task = player._lyrics_task
                self.assertTrue(player.lyrics_loading)
                if how == 'null':
                    engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})
                else:
                    engine.state = 'down'
                self.assertIsNone(player.lyrics, how)
                self.assertFalse(player.lyrics_loading, how)
                gate.set()
                await app.settle()
                self.assertTrue(task.cancelled(), how)
                self.assertIsNone(player.lyrics, how)
                # Shown, then gone the same way.
                engine.state = 'up'
                await app.settle()
                engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
                await app.settle()
                self.assertIsNotNone(player.lyrics, how)
                if how == 'null':
                    engine.event('nowPlayingItemDidChange', {'track': None, 'index': -1})
                else:
                    engine.state = 'down'
                self.assertIsNone(player.lyrics, how)
                self.assertFalse(player.lyrics_loading, how)
        asyncio.run(go())

    def test_engine_failure_means_no_lyrics(self):
        async def go():
            player, engine, app = make_player(state='up')
            await app.settle()
            engine.fail = EngineError('engine-down', 'gone')
            engine.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            await app.settle()
            self.assertIsNone(player.lyrics)
            self.assertFalse(player.lyrics_loading)
        asyncio.run(go())

    def test_an_item_without_a_catalog_id_asks_nothing(self):
        player, engine, app = make_player()
        engine.event('nowPlayingItemDidChange',
                     {'track': dict(TRACK, catalogId=None), 'index': 0})
        self.assertNotIn('Player._load_lyrics', app.spawned)
        self.assertFalse(player.lyrics_loading)

    def test_apply_with_lyrics_takes_them_as_they_are(self):
        player, engine, app = make_player()
        player.apply({'track': TRACK, 'lyrics': LYRICS})
        self.assertNotIn('Player._load_lyrics', app.spawned)
        self.assertEqual(player.lyrics.catalog_id, '1000000001')
        self.assertTrue(player.lyrics.synced)
        self.assertFalse(player.lyrics_loading)


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
