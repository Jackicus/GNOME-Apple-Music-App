# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The Application run as the bus runs it for a search: `--gapplication-service`, on the
session bus the environment names (a private one, in tests/test_search_provider.py), the
library of APPLE_MUSIC_CACHE, the settings on the memory backend (tests/__init__.py), and a
stand-in engine that records its starts and never runs Chrome. The run ends by itself,
SERVICE_LINGER_MS (an environment variable here, short) after the last D-Bus call; at the
end one JSON line goes to stdout: the engine's calls, the windows made, the run's status and
whether the run was a service one.

    python3 -m tests.service_app      (from the repository root, as the test runs it)
"""

import json
import os
import sys

from gi.repository import GObject

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from applemusic import main

BASE_ID = 'io.github.jackicus.MusicSleeve'
APP_ID = BASE_ID + '.ServiceTest'


class FakeEngine(GObject.Object):
    """The Engine as the app's parts see it at startup and shutdown: its state, its
    signals, the settings the app sets on it, and start, stop and kill, recorded."""

    __gsignals__ = {
        'lost': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'event': (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }

    state = GObject.Property(type=str, default='down')
    authorized = GObject.Property(type=bool, default=False)
    headless = GObject.Property(type=bool, default=True)

    def __init__(self, browser_command=None, demo=False):
        super().__init__()
        self.browser_command = browser_command
        self.prefer_headless = True
        self.demo = demo
        self.pid = None
        self.refuse_starts = None
        self.calls = []

    async def start(self, visible=None):
        self.calls.append('start')
        self.state = 'up'

    async def stop(self, grace=None):
        self.calls.append('stop')
        self.state = 'down'

    def kill(self):
        self.calls.append('kill')


def run():
    main.Engine = FakeEngine
    main.SERVICE_LINGER_MS = int(os.environ.get('SERVICE_LINGER_MS', '500'))
    main.use_glib_event_loop()
    app = main.Application('0.0.0', APP_ID, BASE_ID, 'default', None)
    # The provider is exported before startup and gone by the end of the run.
    seen = []
    app.connect('startup', lambda app: seen.append(app.search_provider is not None))
    status = app.run(['music-sleeve', '--gapplication-service'])
    from gi.repository import Gio

    print(json.dumps({
        'engine': app.engine.calls if app.engine is not None else None,
        'windows': len(app.get_windows()),
        'service': bool(app.get_flags() & Gio.ApplicationFlags.IS_SERVICE),
        'provider': seen == [True],
        'library': app.library.state if app.library is not None else None,
        'status': status,
    }), flush=True)
    return status


if __name__ == '__main__':
    sys.exit(run())
