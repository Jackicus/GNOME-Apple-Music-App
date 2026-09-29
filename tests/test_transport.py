"""The transport pieces' decisions without a display: the seek slider's guard, the volume
button's coalescer, and the heart and the mode toggles over stand-in buttons and the
test_player fakes. No GTK widgets are built (importing the module needs the typelib only)."""

import asyncio
import unittest

from tests import ROOT  # noqa: F401  registers src/ as applemusic
from tests.test_player import make_player

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


class ModeControlTest(unittest.TestCase):
    def test_the_repeat_cycle_is_put_back_on_a_failure(self):
        from applemusic.widgets.transport import ModeControl

        async def go():
            player, engine, app = make_player(state='up')
            app.report = lambda error: app.toasts.append(error)
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
