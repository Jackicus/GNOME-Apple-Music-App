# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""scripts/headless.sh records the desktop's session bus for the engine (the keyring that
encrypts Chrome's profile answers there) before dbus-run-session replaces the address. Checked
with stand-ins for mutter and dbus-run-session on PATH, the latter printing what the command
would inherit, so no display or session starts."""

import os
import pathlib
import socket
import subprocess
import tempfile
import unittest

from tests import ROOT

HEADLESS = ROOT / 'scripts' / 'headless.sh'


class HostSessionBusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = pathlib.Path(self.tmp.name) / 'bin'
        self.bin.mkdir()
        self.stub('mutter', '#!/bin/sh\nexit 0\n')
        self.stub('dbus-run-session',
                  '#!/bin/sh\nprintf "%s" "${APPLE_MUSIC_HOST_SESSION_BUS:-}"\n')

    def stub(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def recorded(self, **environment):
        """What dbus-run-session's command would see as APPLE_MUSIC_HOST_SESSION_BUS."""
        env = {'PATH': f'{self.bin}:{os.environ.get("PATH", "/usr/bin:/bin")}',
               'HOME': self.tmp.name, **environment}
        result = subprocess.run([str(HEADLESS), 'true'], env=env, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_address_in_the_environment(self):
        self.assertEqual(self.recorded(DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/7/bus'),
                         'unix:path=/run/user/7/bus')

    def test_the_runtime_directory_s_bus_without_one(self):
        runtime = pathlib.Path(self.tmp.name) / 'runtime'
        runtime.mkdir()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(runtime / 'bus'))
            self.assertEqual(self.recorded(XDG_RUNTIME_DIR=str(runtime)),
                             f'unix:path={runtime}/bus')

    def test_nothing_without_a_bus(self):
        runtime = pathlib.Path(self.tmp.name) / 'no-bus'
        runtime.mkdir()
        self.assertEqual(self.recorded(XDG_RUNTIME_DIR=str(runtime)), '')

    def test_a_nested_run_keeps_the_outer_s(self):
        self.assertEqual(self.recorded(APPLE_MUSIC_HOST_SESSION_BUS='unix:path=/outer/bus',
                                       DBUS_SESSION_BUS_ADDRESS='unix:path=/inner/bus'),
                         'unix:path=/outer/bus')


if __name__ == '__main__':
    unittest.main()
