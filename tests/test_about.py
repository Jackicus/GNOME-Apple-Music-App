# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The About dialog (src/dialogs/about.py): the release its notes are for, the debug
information, and the dialog built from the metainfo in the compiled gresource."""

import unittest

import gi

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)
from tests.gtk import requires_gtk

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import GObject  # noqa: E402

from applemusic.dialogs import about  # noqa: E402


class Engine(GObject.Object):
    state = GObject.Property(type=str, default='up')
    authorized = GObject.Property(type=bool, default=True)
    headless = GObject.Property(type=bool, default=True)


class App:
    version = '0.9.0-1a2b3c4'
    demo = False

    def __init__(self):
        self.engine = Engine()

    def get_application_id(self):
        return 'io.github.jackicus.MusicSleeve.Devel'


class AboutTest(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(about.release_version('0.9.0'), '0.9.0')
        self.assertEqual(about.release_version('0.9.0-1a2b3c4'), '0.9.0')
        self.assertEqual(about.release_version('0.9.0-devel'), '0.9.0')

    def test_debug_info(self):
        app = App()
        text = about.debug_info(app, 'HeadlessChrome/154.0.0.0')
        self.assertIn('Music Sleeve 0.9.0-1a2b3c4 (io.github.jackicus.MusicSleeve.Devel)', text)
        self.assertIn('PyGObject', text)
        self.assertIn('libadwaita', text)
        self.assertIn('Engine: up, headless, signed in', text)
        self.assertIn('Browser: HeadlessChrome/154.0.0.0', text)
        app.engine.state = 'down'
        self.assertIn('Engine: down', about.debug_info(app))
        self.assertNotIn('Browser', about.debug_info(app))
        app.demo = True
        self.assertIn('Engine: none (demo library)', about.debug_info(app))

    @requires_gtk
    def test_the_dialog_comes_from_the_metainfo(self):
        from gi.repository import Adw

        dialog = Adw.AboutDialog.new_from_appdata(about.METAINFO, '0.9.0')
        self.assertEqual(dialog.get_application_name(), 'Music Sleeve')
        self.assertEqual(dialog.get_release_notes_version(), '0.9.0')
        self.assertTrue(dialog.get_release_notes())
        self.assertTrue(dialog.get_issue_url().startswith('https://github.com/'))


if __name__ == '__main__':
    unittest.main()
