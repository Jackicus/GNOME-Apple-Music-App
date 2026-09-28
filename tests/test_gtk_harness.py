"""Unit tests for tests/gtk.py: a template widget builds under requires_gtk, settings are the
test's own, the main-loop helpers, and a clean skip where GTK cannot start."""

import os
import subprocess
import sys
import unittest

from tests import ROOT
from tests.gtk import SCHEMA_ID, pump, requires_gtk, wait_for


@requires_gtk
class HarnessTest(unittest.TestCase):
    def test_a_template_widget_builds(self):
        from applemusic.widgets.cover import Cover

        cover = Cover()
        cover.props.size = 64
        self.assertEqual(cover.props.size, 64)
        self.assertEqual(cover.placeholder_icon.get_pixel_size(), 19)
        self.assertTrue(cover.set_paths('/nonexistent/a.jpg', None))
        self.assertFalse(cover.set_paths('/nonexistent/a.jpg'))  # the same paths

    def test_settings_are_in_memory_with_this_trees_schema(self):
        from gi.repository import Gio

        settings = Gio.Settings.new(SCHEMA_ID)
        self.assertEqual(settings.props.backend.__gtype__.name, 'GMemorySettingsBackend')
        settings.set_string('last-page', 'albums')
        self.assertEqual(settings.get_string('last-page'), 'albums')

    def test_pump_and_wait_for(self):
        from gi.repository import GLib

        ran = []
        GLib.idle_add(lambda: ran.append('idle'))
        pump()
        self.assertEqual(ran, ['idle'])
        GLib.timeout_add(30, lambda: ran.append('timeout'))
        self.assertTrue(wait_for(lambda: 'timeout' in ran, timeout=1))
        self.assertFalse(wait_for(lambda: False, timeout=0.05))


class SkipTest(unittest.TestCase):
    def test_without_a_display_widget_tests_skip(self):
        env = dict(os.environ, GDK_BACKEND='none')
        done = subprocess.run(
            [sys.executable, '-m', 'unittest', '-v', 'tests.test_gtk_harness.HarnessTest'],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('skipped', done.stderr)
        self.assertIn('GTK cannot open a display', done.stderr + done.stdout)


if __name__ == '__main__':
    unittest.main()
