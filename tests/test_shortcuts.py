"""The shortcuts table: the Keyboard Shortcuts dialog lists every accelerator and key."""

import unittest

import gi

from tests import SRC  # noqa: F401  registers src/ as applemusic

from applemusic import shortcuts

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk  # noqa: E402


def listed():
    return [key for _title, items in shortcuts.sections() for _item, key in items]


class ShortcutsTest(unittest.TestCase):
    def test_every_accelerator_is_listed(self):
        keys = listed()
        for action in shortcuts.ACCELS:
            self.assertIn(action, keys, action)
        for action in shortcuts.PLAYBACK:
            self.assertIn(action, keys, action)

    def test_titles_and_keys(self):
        for title, items in shortcuts.sections():
            self.assertTrue(title)
            self.assertTrue(items, title)
            for item_title, key in items:
                self.assertTrue(item_title, key)
                self.assertTrue(shortcuts.accelerator(key), item_title)

    def test_accelerators_parse(self):
        for key in listed():
            for accel in shortcuts.accelerator(key).split():
                self.assertTrue(Gtk.accelerator_parse(accel)[0], accel)
        for accels in list(shortcuts.ACCELS.values()) + list(shortcuts.PLAYBACK.values()):
            for accel in accels:
                self.assertTrue(Gtk.accelerator_parse(accel)[0], accel)

    def test_no_key_does_two_things(self):
        seen = {}
        table = list(shortcuts.ACCELS.items()) + list(shortcuts.PLAYBACK.items())
        for action, accels in table:
            for accel in accels:
                parsed = Gtk.accelerator_parse(accel)[1:]
                self.assertNotIn(parsed, seen, f'{accel}: {action} and {seen.get(parsed)}')
                seen[parsed] = action
        for accel in (shortcuts.MAIN_MENU, *shortcuts.CONTEXT_MENU.split(), shortcuts.CLOSE):
            self.assertNotIn(Gtk.accelerator_parse(accel)[1:], seen, accel)

    def test_the_higs_standard_keys_stay_free(self):
        """The HIG's standard shortcuts the app has no such action for are not taken by
        another: Ctrl+N (New), Ctrl+O (Open), Ctrl+S (Save), Ctrl+P (Print), Ctrl+Z (Undo)."""
        taken = {Gtk.accelerator_parse(accel)[1:]
                 for accels in shortcuts.ACCELS.values() for accel in accels}
        for accel in ('<primary>n', '<primary>o', '<primary>s', '<primary>p', '<primary>z'):
            self.assertNotIn(Gtk.accelerator_parse(accel)[1:], taken, accel)
        self.assertEqual(shortcuts.ACCELS['app.now-playing'], ('<primary><shift>n',))


if __name__ == '__main__':
    unittest.main()
