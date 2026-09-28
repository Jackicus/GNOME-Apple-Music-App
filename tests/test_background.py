"""Background playback (src/background.py): the hold, the grace timer and the release, with a
stand-in Player and window and a fake clock. No display, no bus."""

import unittest

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from gi.repository import GObject

from applemusic.background import BackgroundPlayback


class FakePlayer(GObject.Object):
    """What BackgroundPlayback reads of the Player: `state`, `track`, `stopped`."""

    state = GObject.Property(type=str, default='playing')
    track = GObject.Property(type=object)

    @property
    def stopped(self):
        return self.track is None or self.state in ('none', 'stopped', 'ended', 'completed')


class FakeWindow(GObject.Object):
    visible = GObject.Property(type=bool, default=False)

    def get_visible(self):
        return self.visible


class Clock:
    """add_timeout/remove_timeout that run nothing until fire() is called."""

    def __init__(self):
        self.timers = {}
        self.next_id = 1

    def add(self, seconds, callback):
        source = self.next_id
        self.next_id += 1
        self.timers[source] = (seconds, callback)
        return source

    def remove(self, source):
        del self.timers[source]

    def fire(self):
        for source, (_seconds, callback) in list(self.timers.items()):
            del self.timers[source]
            callback()


class BackgroundTest(unittest.TestCase):
    def setUp(self):
        self.player = FakePlayer(track='a track')
        self.window = FakeWindow()
        self.clock = Clock()
        self.calls = []
        self.background = BackgroundPlayback(
            self.player, hold=lambda: self.calls.append('hold'),
            release=lambda: self.calls.append('release'),
            quit=lambda: self.calls.append('quit'),
            add_timeout=self.clock.add, remove_timeout=self.clock.remove)

    def test_entering_while_playing_holds_once_and_arms_nothing(self):
        self.background.enter(self.window)
        self.background.enter(self.window)
        self.assertTrue(self.background.active)
        self.assertEqual(self.calls, ['hold'])
        self.assertEqual(self.clock.timers, {})

    def test_stopped_arms_the_grace_and_loading_disarms_it(self):
        self.background.enter(self.window)
        self.player.state = 'stopped'
        self.assertEqual([seconds for seconds, _ in self.clock.timers.values()],
                         [self.background.grace])
        self.player.state = 'ended'  # still stopped: the one timer stays
        self.assertEqual(len(self.clock.timers), 1)
        self.player.state = 'loading'  # the next item: not stopped
        self.assertEqual(self.clock.timers, {})
        self.clock.fire()
        self.assertNotIn('quit', self.calls)

    def test_stopped_for_the_grace_quits_once(self):
        self.background.enter(self.window)
        self.player.track = None
        self.clock.fire()
        self.clock.fire()
        self.assertEqual(self.calls, ['hold', 'quit'])

    def test_pause_arms_nothing(self):
        self.background.enter(self.window)
        self.player.state = 'paused'
        self.assertEqual(self.clock.timers, {})

    def test_the_window_shown_again_releases_once(self):
        self.background.enter(self.window)
        self.player.state = 'stopped'
        self.window.visible = True
        self.assertFalse(self.background.active)
        self.assertEqual(self.calls, ['hold', 'release'])
        self.assertEqual(self.clock.timers, {})  # the grace disarmed
        self.background.leave()  # a second leave is nothing
        self.window.visible = False
        self.window.visible = True  # no longer watched
        self.player.state = 'playing'
        self.assertEqual(self.calls, ['hold', 'release'])

    def test_leave_when_never_entered_is_nothing(self):
        self.background.leave()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
