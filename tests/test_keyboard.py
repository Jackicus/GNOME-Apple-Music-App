"""The window's key policy (applemusic.keyboard): which playback action a key runs, given
where the focus is, as a table; the context-menu and main-menu keys agree with the shortcuts
table. Widget classes only, no display."""

import unittest

import gi

from tests import ROOT  # noqa: F401  registers src/ as applemusic

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gdk, Gtk  # noqa: E402

from applemusic import keyboard, shortcuts  # noqa: E402

PRIMARY = Gdk.ModifierType.CONTROL_MASK
NO_FOCUS = type(None)


class ListItemWidget(Gtk.Widget):
    """A stand-in for a grid's tile or a list's row: a plain widget."""


def route(accel, focus_type=NO_FOCUS, dialog_open=False, in_popover=False, disabled=()):
    _ok, keyval, mods = Gtk.accelerator_parse(accel)
    return keyboard.playback_action(keyval, mods, focus_type, dialog_open, in_popover,
                                    lambda name: name not in disabled)


class RoutingTest(unittest.TestCase):
    def test_the_playback_keys_with_nothing_in_the_way(self):
        self.assertEqual(route('space'), 'play-pause')
        self.assertEqual(route('KP_Space'), 'play-pause')
        self.assertEqual(route('<primary>Right'), 'next')
        self.assertEqual(route('<primary>Left'), 'previous')
        self.assertEqual(route('space', ListItemWidget), 'play-pause')
        self.assertEqual(route('<primary>Right', Gtk.Scale), 'next')

    def test_other_keys_are_not_playback(self):
        for accel in ('Right', 'Left', 'Return', 'a', '<primary>space', '<shift>Right', 'F10'):
            with self.subTest(accel=accel):
                self.assertIsNone(route(accel))

    def test_typing_keeps_every_key(self):
        for focus_type in (Gtk.Text, Gtk.Entry, Gtk.SearchEntry, Gtk.TextView):
            for accel in ('space', '<primary>Right', '<primary>Left'):
                with self.subTest(focus=focus_type.__name__, accel=accel):
                    self.assertIsNone(route(accel, focus_type))

    def test_space_belongs_to_what_space_toggles(self):
        for focus_type in (Gtk.ToggleButton, Gtk.Switch, Gtk.CheckButton):
            with self.subTest(focus=focus_type.__name__):
                self.assertIsNone(route('space', focus_type))
                self.assertIsNone(route('KP_Space', focus_type))
                self.assertEqual(route('<primary>Right', focus_type), 'next')

    def test_a_dialog_or_a_menu_keeps_the_keys(self):
        self.assertIsNone(route('space', dialog_open=True))
        self.assertIsNone(route('<primary>Right', in_popover=True))

    def test_a_disabled_action_does_not_run(self):
        self.assertIsNone(route('space', disabled=('play-pause',)))
        self.assertEqual(route('<primary>Right', disabled=('play-pause',)), 'next')


class KeysTest(unittest.TestCase):
    def test_the_playback_keys_are_the_shortcuts_tables(self):
        names = set(keyboard.PLAYBACK_KEYS.values())
        self.assertEqual(names, set(shortcuts.PLAYBACK))
        for action, accels in shortcuts.PLAYBACK.items():
            for accel in accels:
                _ok, keyval, mods = Gtk.accelerator_parse(accel)
                self.assertEqual(keyboard.PLAYBACK_KEYS[(keyval, int(mods))], action)

    def test_the_main_menu_key(self):
        self.assertTrue(keyboard.is_main_menu(Gdk.KEY_F10, 0))
        self.assertFalse(keyboard.is_main_menu(Gdk.KEY_F10, Gdk.ModifierType.SHIFT_MASK))
        self.assertFalse(keyboard.is_main_menu(Gdk.KEY_F9, 0))


if __name__ == '__main__':
    unittest.main()
