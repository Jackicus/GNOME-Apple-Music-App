"""Startup timing: when the process started, and the moments of the start worth measuring.

    marks = StartupMarks(app.get_active_window)
    marks.mark('window-built')                 # now, the first time only
    marks.mark('albums-bound', painted='albums-painted')   # and the end of the frame drawn

`marks.marks` maps each name to its GLib.get_monotonic_time() (Application.marks is the same
dict: scripts/bench.py reads it), and with --debug the log says how long after the process
started each one was (PROCESS_START).
"""

import logging
import os
import time

from gi.repository import GLib

log = logging.getLogger(__name__)


def process_start():
    """When this process started, in GLib.get_monotonic_time()'s microseconds: from
    /proc/self/stat's start time on Linux (so the interpreter's own start counts), else
    now. What the startup marks are measured from."""
    now = GLib.get_monotonic_time()
    try:
        with open('/proc/self/stat', encoding='ascii') as file:
            fields = file.read().rpartition(')')[2].split()
        ticks = int(fields[19])  # starttime, the 22nd field, in clock ticks since boot
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - ticks / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError, AttributeError):
        return now
    return now - int(age * 1e6)


PROCESS_START = process_start()


class StartupMarks:
    """The startup marks of one app. `window()` answers the window whose frame clock tells
    when a frame has been painted (the active window), or None."""

    def __init__(self, window):
        self._window = window
        self.marks = {}

    def mark(self, name, painted=None):
        """Note that `name` has just happened for the first time ('window-mapped',
        'library-ready', 'albums-bound'…); anything marked again is ignored. `painted` names
        a mark for the end of the frame being drawn (a page's first tiles are bound during a
        frame's layout and are on screen at its end)."""
        if name in self.marks:
            return
        now = GLib.get_monotonic_time()
        self.marks[name] = now
        log.debug('startup: %s at +%.0f ms', name, (now - PROCESS_START) / 1000)
        if painted:
            self.mark_after_paint(painted)

    def mark_after_paint(self, name):
        """Mark `name` at the end of the frame the window draws next."""
        window = self._window()
        clock = window.get_frame_clock() if window is not None else None
        if clock is None:
            return
        handler = []

        def on_after_paint(clock):
            clock.disconnect(handler[0])
            self.mark(name)

        handler.append(clock.connect('after-paint', on_after_paint))

    def on_window_mapped(self, _window):
        """The window's `map`: 'window-mapped', and 'first-frame' once it is painted."""
        if 'first-frame' not in self.marks:
            self.mark('window-mapped')
            self.mark_after_paint('first-frame')
