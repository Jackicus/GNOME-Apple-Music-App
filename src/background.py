# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Background playback: the window closed while the music plays on.

    background = BackgroundPlayback(player, hold=app.hold, release=app.release,
                                    quit=lambda: app.activate_action('quit'))
    background.enter(window)   # the window has just been hidden: the app is held
    background.active          # whether the app is held for the music
    background.leave()         # the window is back, or the app quits: released, once

While active it watches the Player: once playback has looked stopped for GRACE seconds
(MusicKit passes through 'ended' and 'stopped' between items and queues, so a moment of it
is no reason to quit), it quits; anything playing or paused again before that disarms the
timer. The window shown again (its `visible`) ends it. No GTK: the timer functions are
GLib's by default, and a test passes its own.
"""

import logging

from gi.repository import GLib

log = logging.getLogger(__name__)

# How long playback may look stopped with the window closed before the app quits.
GRACE = 10


class BackgroundPlayback:
    """See the module. `player` has `stopped` and the `state` and `track` properties;
    `hold()`/`release()` keep the application running without a window; `quit()` ends it;
    `add_timeout(seconds, callback)` answers a source id that `remove_timeout(id)` removes
    (GLib's timeout_add_seconds and source_remove by default)."""

    def __init__(self, player, hold, release, quit, add_timeout=None, remove_timeout=None,
                 grace=GRACE):
        self._player = player
        self._hold = hold
        self._release = release
        self._quit = quit
        self._add_timeout = add_timeout or GLib.timeout_add_seconds
        self._remove_timeout = remove_timeout or GLib.source_remove
        self.grace = grace
        self._handlers = None  # [(source, handler id)] while active
        self._timer = None  # the grace before quitting, once playback stopped

    @property
    def active(self):
        """Whether the window is closed while the music plays on (the app held)."""
        return self._handlers is not None

    def enter(self, window):
        """The window has been hidden for the music: hold the app and watch the Player (and
        the window, whose showing again ends this)."""
        if self._handlers is None:
            self._hold()
            player = self._player
            self._handlers = [
                (player, player.connect('notify::state', self._check)),
                (player, player.connect('notify::track', self._check)),
                (window, window.connect('notify::visible', self._on_window_visible)),
            ]
            log.info('the window is closed; playing on in the background')
        self._check()

    def leave(self):
        """The window is back (or the app is quitting): the hold released, nothing watched.
        Nothing when not active."""
        if self._handlers is None:
            return
        for source, handler in self._handlers:
            source.disconnect(handler)
        self._handlers = None
        self._disarm()
        self._release()

    def _on_window_visible(self, window, _pspec):
        if window.get_visible():
            log.info('the window is shown again')
            self.leave()

    def _check(self, *_args):
        """Playback stopped: quit after the grace, unless something plays (or is paused)
        again by then."""
        if self._handlers is None:
            return
        if not self._player.stopped:
            self._disarm()
        elif self._timer is None:
            self._timer = self._add_timeout(self.grace, self._on_grace_over)

    def _disarm(self):
        if self._timer is not None:
            self._remove_timeout(self._timer)
            self._timer = None

    def _on_grace_over(self):
        self._timer = None
        if self._handlers is not None and self._player.stopped:
            log.info('playback stopped with the window closed: quitting')
            self._quit()
        return GLib.SOURCE_REMOVE
