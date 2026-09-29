"""The transport pieces' decisions without a display: the seek slider's guard, the volume
button's coalescer, and the heart and the mode toggles over stand-in buttons and the
test_player fakes. No GTK widgets are built (importing the module needs the typelib only)."""

import asyncio
import unittest

from gi.repository import GObject

from tests import ROOT  # noqa: F401  registers src/ as applemusic
from tests.test_player import TRACK, make_player

from applemusic.backend.errors import EngineError
from applemusic.widgets.transport import Coalescer, SeekGuard


class SeekGuardTest(unittest.TestCase):
    def test_nothing_incoming_counts_while_a_seek_waits_to_be_sent(self):
        """A click 1.5 s ahead of the play head: the tick that arrives during the settle
        must not release the target (it used to, and the seek was never sent)."""
        guard = SeekGuard(hold=1.5, tolerance=2.0)
        self.assertTrue(guard.accept(60.0, now=0.0))  # no seek about
        guard.moved(61.5)
        self.assertFalse(guard.accept(60.25, now=0.1))
        self.assertEqual(guard.target, 61.5)
        guard.moved(62.0)  # the drag goes on
        self.assertFalse(guard.accept(60.5, now=0.2))
        self.assertEqual(guard.target, 62.0)

    def test_a_position_near_the_target_releases_the_guard_once_sent(self):
        guard = SeekGuard(hold=1.5, tolerance=2.0)
        guard.moved(120.0)
        guard.sent(now=1.0)
        self.assertFalse(guard.accept(60.5, now=1.1))  # stale, from before the seek
        self.assertIsNotNone(guard.target)
        self.assertTrue(guard.accept(121.0, now=1.3))  # the seek has taken
        self.assertIsNone(guard.target)
        self.assertTrue(guard.accept(60.0, now=1.4))  # anything again

    def test_the_hold_running_out_releases_the_guard(self):
        guard = SeekGuard(hold=1.5, tolerance=2.0)
        guard.moved(120.0)
        guard.sent(now=1.0)
        self.assertFalse(guard.accept(60.0, now=2.4))
        self.assertTrue(guard.accept(60.0, now=2.5))
        self.assertIsNone(guard.target)

    def test_a_failure_releases_the_guard(self):
        guard = SeekGuard()
        guard.moved(120.0)
        guard.sent(now=1.0)
        guard.release()
        self.assertIsNone(guard.target)
        self.assertTrue(guard.accept(60.0, now=1.0))


class CoalescerTest(unittest.TestCase):
    def test_rapid_moves_send_at_most_two_commands(self):
        coalescer = Coalescer()
        sent = []
        for value in (0.1, 0.2, 0.3, 0.4):
            to_send = coalescer.moved(value)
            if to_send is not None:
                sent.append(to_send)
        self.assertEqual(sent, [0.1])
        self.assertEqual(coalescer.target, 0.4)
        self.assertEqual(coalescer.done(), 0.4)  # the first returned: the last value goes
        self.assertEqual(coalescer.target, 0.4)
        self.assertIsNone(coalescer.done())  # and then nothing waits
        self.assertIsNone(coalescer.target)
        self.assertEqual(coalescer.moved(0.5), 0.5)


class Button:
    """A Gtk.ToggleButton's surface the heart and the mode toggles use."""

    def __init__(self):
        self.active = False
        self.icon = None
        self.tooltip = None
        self.sensitive = True
        self._handlers = []

    def connect(self, _signal, handler):
        self._handlers.append(handler)

    def set_active(self, active):
        if active != self.active:
            self.active = active
            for handler in self._handlers:
                handler(self)

    def get_active(self):
        return self.active

    def set_icon_name(self, name):
        self.icon = name

    def set_tooltip_text(self, text):
        self.tooltip = text

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive


class RatingEngine(GObject.Object):
    """An engine whose rating commands are recorded and gated: `rating` answers through
    `rated` what `ratings` holds (1 loved, 0 not), `love`/`unlove` wait for `gate` and then
    raise when `fail` says so."""

    __gsignals__ = {
        'event': (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        'rated': (GObject.SignalFlags.RUN_FIRST, None, (str, str, int)),
    }

    state = GObject.Property(type=str, default='up')
    authorized = GObject.Property(type=bool, default=True)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.ratings = {}
        self.gate = asyncio.Event()
        self.read_gate = None  # an Event a rating() read waits for, when set
        self.fail = None

    async def rating(self, kind, item_id):
        self.calls.append(('rating', kind, item_id))
        if self.read_gate is not None:
            await self.read_gate.wait()
        value = self.ratings.get(item_id, 0)
        self.emit('rated', kind, item_id, value)
        return value

    async def love(self, kind, item_id):
        return await self._write('love', kind, item_id, 1)

    async def unlove(self, kind, item_id):
        return await self._write('unlove', kind, item_id, 0)

    async def _write(self, name, kind, item_id, value):
        self.calls.append((name, kind, item_id))
        await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        self.ratings[item_id] = value
        self.emit('rated', kind, item_id, value)


def make_heart():
    """A HeartControl over a stand-in button, a Player over the test_player fake engine
    (the events) and a RatingEngine (the ratings), the app reporting nothing."""
    from applemusic.widgets.transport import HeartControl

    player, events, app = make_player(state='up')
    engine = RatingEngine()
    app.engine = engine
    button = Button()
    heart = HeartControl(button)
    heart.attach(player, app)
    return heart, button, player, events, engine, app


class HeartTest(unittest.TestCase):
    def test_a_failed_write_reverts_only_the_item_it_was_for(self):
        """Unlove A, skip to B, then A's unlove fails: B's heart is left alone."""
        async def go():
            heart, button, player, events, engine, app = make_heart()
            await app.settle()
            engine.ratings['1000000001'] = 1
            events.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            await app.settle()
            self.assertTrue(button.active)  # A is loved
            button.set_active(False)  # the user unloves A: the write waits at the gate
            await asyncio.sleep(0)
            self.assertEqual(engine.calls[-1], ('unlove', 'song', '1000000001'))
            other = dict(TRACK, id='i.demo0002', catalogId='1000000002', index=3)
            events.event('nowPlayingItemDidChange', {'track': other, 'index': 3})
            for _turn in range(5):  # B's rating read answers; A's write still waits
                await asyncio.sleep(0)
            self.assertFalse(button.active)  # B is not loved
            engine.fail = EngineError('timeout', 'slow')
            engine.gate.set()
            await app.settle()
            self.assertFalse(button.active)  # A's failure does not turn B's heart on
            self.assertEqual(len(app.toasts), 1)  # but is reported
        asyncio.run(go())

    def test_a_click_outranks_the_read_started_with_the_item(self):
        """The rating read when the item began answers after the click: the click's value
        stays."""
        async def go():
            heart, button, player, events, engine, app = make_heart()
            await app.settle()
            engine.read_gate = asyncio.Event()
            engine.gate.set()
            events.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            await asyncio.sleep(0)
            button.set_active(True)  # loved by the user before the read answered
            await app.settle()
            self.assertTrue(button.active)
            self.assertIn(('love', 'song', '1000000001'), engine.calls)
            engine.ratings['1000000001'] = 0  # what the read would have said
            engine.read_gate.set()
            await app.settle()
            self.assertTrue(button.active)  # the read was cancelled: nothing flipped
        asyncio.run(go())

    def test_the_same_song_again_is_not_read_again(self):
        async def go():
            heart, button, player, events, engine, app = make_heart()
            await app.settle()
            engine.ratings['1000000001'] = 1
            events.event('nowPlayingItemDidChange', {'track': TRACK, 'index': 2})
            await app.settle()
            reads = engine.calls.count(('rating', 'song', '1000000001'))
            self.assertEqual(reads, 1)
            # The same song re-created (its queue index changed): no reset, no read.
            events.event('nowPlayingItemDidChange', {'track': dict(TRACK, index=5), 'index': 5})
            await app.settle()
            self.assertTrue(button.active)
            self.assertEqual(engine.calls.count(('rating', 'song', '1000000001')), 1)
            # A fresh engine: read again.
            engine.state = 'down'
            engine.state = 'up'
            await app.settle()
            self.assertEqual(engine.calls.count(('rating', 'song', '1000000001')), 2)
        asyncio.run(go())

    def test_targets_by_kind(self):
        heart, button, player, events, engine, app = make_heart()
        self.assertIsNone(heart.target())
        events.event('nowPlayingItemDidChange',
                     {'track': dict(TRACK, type='library-songs'), 'index': 2})
        self.assertEqual(heart.target(), ('song', '1000000001'))
        events.event('nowPlayingItemDidChange',
                     {'track': dict(TRACK, id='i.v1', catalogId='555', type='music-videos',
                                    index=3), 'index': 3})
        self.assertEqual(heart.target(), ('video', '555'))
        self.assertTrue(button.sensitive)
        events.event('nowPlayingItemDidChange',
                     {'track': dict(TRACK, id='ra.1', catalogId=None, type='stations',
                                    index=0), 'index': 0})
        self.assertIsNone(heart.target())  # a station cannot be loved
        self.assertFalse(button.sensitive)
        events.event('nowPlayingItemDidChange',
                     {'track': dict(TRACK, id='i.x', catalogId=None, type='', index=1),
                      'index': 1})
        self.assertIsNone(heart.target())  # nor an item of no known kind


class QueueSliceTest(unittest.TestCase):
    def test_the_slice_starts_at_the_entry_playing(self):
        from applemusic.widgets.queue import slice_offset

        self.assertEqual(slice_offset(4), 4)
        self.assertEqual(slice_offset(0), 0)
        self.assertEqual(slice_offset(-1), 0)  # nothing playing: the whole queue
        # A row activated at slice position 2 while entry 4 plays is the queue's entry 6.
        self.assertEqual(2 + slice_offset(4), 6)


class ModeControlTest(unittest.TestCase):
    def test_the_repeat_cycle_is_put_back_on_a_failure(self):
        from applemusic.widgets.transport import ModeControl

        async def go():
            player, engine, app = make_player(state='up')
            await app.settle()
            shuffle, repeat = Button(), Button()
            modes = ModeControl(shuffle, repeat)
            modes.attach(player, app)
            self.assertEqual(repeat.icon, 'media-playlist-repeat-symbolic')
            repeat.set_active(True)  # a click: none → one, shown at once
            self.assertEqual(repeat.icon, 'media-playlist-repeat-song-symbolic')
            await app.settle()
            self.assertEqual(engine.calls[-1], ('repeat', 'one'))
            engine.event('repeatModeDidChange', {'repeat': 'one'})
            engine.fail = EngineError('engine-down', 'gone')
            repeat.set_active(False)  # one → all asked for, shown, then refused
            self.assertEqual(repeat.icon, 'media-playlist-repeat-symbolic')
            self.assertTrue(repeat.active)
            await app.settle()
            self.assertEqual(repeat.icon, 'media-playlist-repeat-song-symbolic')  # back to one
            self.assertTrue(repeat.active)
            self.assertEqual(len(app.toasts), 1)
        asyncio.run(go())


if __name__ == '__main__':
    unittest.main()
