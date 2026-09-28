import os
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests import ROOT, SRC

from applemusic.backend import config, sync

BACKEND = SRC / 'backend'
TEXT_SUFFIXES = {'.py', '.js', '.json', '.md', '.sh', '.build', '.in', '.xml', '.blp', '.css'}

# The vendored extension's names, which must not survive here (spelled in pieces so this file
# does not match itself).
OLD_NAMES = re.compile('|'.join([
    'apple-music' + '-library',
    'APPLE_MUSIC' + '_LIBRARY',
    r'\b92' + r'27\b',
]))


class ConfigTest(unittest.TestCase):
    def env(self, **values):
        """Run with exactly these of the variables config reads set."""
        names = ('APPLE_MUSIC_CACHE', 'APPLE_MUSIC_PROFILE', 'APPLE_MUSIC_DEBUG_PORT',
                 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR')
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(values)

    def test_xdg_defaults(self):
        self.env(XDG_CACHE_HOME='/x/cache', XDG_DATA_HOME='/x/data', XDG_RUNTIME_DIR='/x/run')
        self.assertEqual(config.cache_dir(), pathlib.Path('/x/cache/apple-music'))
        self.assertEqual(config.profile_dir(), pathlib.Path('/x/data/apple-music/chrome'))
        self.assertIsNone(config.debug_port())

    def test_home_fallbacks(self):
        self.env(XDG_CACHE_HOME='relative/is/ignored')
        home = pathlib.Path.home()
        self.assertEqual(config.cache_dir(), home / '.cache' / 'apple-music')
        self.assertEqual(config.profile_dir(), home / '.local' / 'share' / 'apple-music' / 'chrome')

    def test_overrides(self):
        self.env(APPLE_MUSIC_CACHE='/o/cache', APPLE_MUSIC_PROFILE='/o/profile',
                 APPLE_MUSIC_DEBUG_PORT='9300', XDG_RUNTIME_DIR='/x/run')
        self.assertEqual(config.cache_dir(), pathlib.Path('/o/cache'))
        self.assertEqual(config.profile_dir(), pathlib.Path('/o/profile'))
        self.assertEqual(config.debug_port(), 9300)

    def build(self, profile):
        config.set_build_profile(profile)
        self.addCleanup(config.set_build_profile, 'default')

    def test_the_development_build_has_paths_of_its_own(self):
        self.env(XDG_CACHE_HOME='/x/cache', XDG_DATA_HOME='/x/data')
        self.build('development')
        self.assertEqual(config.build_profile(), 'development')
        self.assertEqual(config.cache_dir(), pathlib.Path('/x/cache/apple-music-devel'))
        self.assertEqual(config.profile_dir(), pathlib.Path('/x/data/apple-music/chrome-devel'))
        self.build('default')
        self.assertEqual(config.cache_dir(), pathlib.Path('/x/cache/apple-music'))
        self.assertEqual(config.profile_dir(), pathlib.Path('/x/data/apple-music/chrome'))

    def test_the_environment_wins_in_either_build(self):
        self.env(APPLE_MUSIC_CACHE='/o/cache', APPLE_MUSIC_PROFILE='/o/profile')
        for profile in ('default', 'development'):
            with self.subTest(profile=profile):
                self.build(profile)
                self.assertEqual(config.cache_dir(), pathlib.Path('/o/cache'))
                self.assertEqual(config.profile_dir(), pathlib.Path('/o/profile'))

    def test_relative_override_is_made_absolute(self):
        self.env(APPLE_MUSIC_CACHE='some/cache')
        self.assertEqual(config.cache_dir(), pathlib.Path.cwd() / 'some' / 'cache')

    def test_a_bad_debug_port_is_none(self):
        for value in ('', 'abc', '0', '70000', '-1'):
            with self.subTest(value=value):
                self.env(APPLE_MUSIC_DEBUG_PORT=value)
                self.assertIsNone(config.debug_port())

    def test_sizes(self):
        self.assertEqual((config.THUMB_SIZE, config.COVER_SIZE), (320, 640))
        self.assertEqual(sync.DEFAULT_ART_SIZES, {'cover': 640, 'thumb': 320})

    def test_bridge_is_beside_the_module(self):
        self.assertEqual(config.BRIDGE_JS, BACKEND / 'bridge.js')
        self.assertTrue(config.BRIDGE_JS.is_file())


class PackageTest(unittest.TestCase):
    def test_importing_loads_neither_am_nor_gi(self):
        code = ('import sys, tests\n'
                'import applemusic.backend, applemusic.backend.cdp, applemusic.backend.config\n'
                'import applemusic.backend.sync\n'
                "print(sorted(m for m in sys.modules\n"
                "             if m.endswith('.am') or m.split('.')[0] == 'gi'))\n")
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True,
                                text=True, check=True)
        self.assertEqual(result.stdout.strip(), '[]')

    def test_no_gi_in_the_backend(self):
        for path in BACKEND.glob('*.py'):
            with self.subTest(file=path.name):
                source = path.read_text()
                self.assertNotRegex(source, r'(?m)^\s*(import gi\b|from gi\b)')

    def test_old_names_are_gone(self):
        for folder in ('src', 'tests', 'scripts'):
            for path in (ROOT / folder).rglob('*'):
                if path.suffix not in TEXT_SUFFIXES:
                    continue
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.assertIsNone(OLD_NAMES.search(path.read_text()))


if __name__ == '__main__':
    unittest.main()
