# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The desktop file and the metainfo in data/, beyond what desktop-file-validate and
appstreamcli check of the built ones (data/meson.build's tests): the keys GNOME reads, the
development build's name, and the help link."""

import json
import unittest

from tests import ROOT

DATA = ROOT / 'data'
DESKTOP_FILE = DATA / 'io.github.jackicus.MusicSleeve.desktop.in'
METAINFO = DATA / 'io.github.jackicus.MusicSleeve.metainfo.xml.in'
MANIFEST = ROOT / 'build-aux' / 'flatpak' / 'io.github.jackicus.MusicSleeve.Devel.json'


def desktop_entries():
    """The desktop file's keys and values (its comments left out)."""
    lines = DESKTOP_FILE.read_text(encoding='utf-8').splitlines()
    return dict(line.split('=', 1) for line in lines if '=' in line and not line.startswith('#'))


class DesktopFileTest(unittest.TestCase):
    def test_gnome_lists_it_under_notifications(self):
        # The app sends a notification while its window is closed for background playback.
        self.assertEqual(desktop_entries()['X-GNOME-UsesNotifications'], 'true')

    def test_fits_a_phone(self):
        self.assertEqual(desktop_entries()['X-Purism-FormFactor'], 'Workstation;Mobile;')
        self.assertIn('<display_length compare="ge">360</display_length>',
                      METAINFO.read_text(encoding='utf-8'))


class DevelopmentNameTest(unittest.TestCase):
    """The development build is "Music Sleeve (Development)": the suffix is data/meson.build's
    NAME_SUFFIX, in the desktop file and the metainfo, and nowhere else, or it would double."""

    def test_the_name_takes_the_suffix(self):
        self.assertEqual(desktop_entries()['Name'], 'Music Sleeve@NAME_SUFFIX@')
        self.assertIn('<name>Music Sleeve@NAME_SUFFIX@</name>',
                      METAINFO.read_text(encoding='utf-8'))

    def test_meson_sets_it_for_the_development_profile(self):
        meson = (DATA / 'meson.build').read_text(encoding='utf-8')
        self.assertIn("get_option('profile') == 'development' ? ' (Development)' : ''", meson)
        self.assertIn("'NAME_SUFFIX': name_suffix", meson)

    def test_the_flatpak_manifest_adds_none(self):
        manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
        self.assertNotIn('desktop-file-name-suffix', manifest)
        self.assertIn('-Dprofile=development',
                      [opt for module in manifest['modules'] if module['name'] == 'music-sleeve'
                       for opt in module['config-opts']])


class MetainfoTest(unittest.TestCase):
    def test_the_help_link_is_the_user_guide(self):
        text = METAINFO.read_text(encoding='utf-8')
        self.assertIn('<url type="help">https://github.com/Jackicus/GNOME-Music-Sleeve/blob/main/'
                      'docs/user-guide.md</url>', text)
        self.assertTrue((ROOT / 'docs' / 'user-guide.md').is_file())


if __name__ == '__main__':
    unittest.main()
