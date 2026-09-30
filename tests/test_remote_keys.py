# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""scripts/remote_keys.py turns an accelerator into the evdev keycodes mutter's RemoteDesktop
API takes, which is the part that is easy to get wrong: a keysym (Gdk.KEY_Tab, 0xff09), a GDK
hardware keycode (23) and an evdev keycode (KEY_TAB, 15) are three different numbers for the
same key. The mapping goes through the display's keymap, so these tests need one; they pin the
keys whose evdev code no layout moves, and check what a press sends over the bus with a
stand-in for it."""

import importlib.util
import sys
import unittest

from tests import ROOT
from tests.gtk import requires_gtk

# linux/input-event-codes.h. A layout maps these keys rather than moving them, so their evdev
# codes are the same everywhere; the letters and punctuation are not, and are left out.
FIXED = {'Escape': 1, 'Tab': 15, 'Return': 28, 'space': 57, 'F10': 68, 'Up': 103, 'Left': 105,
         'Right': 106, 'Down': 108, 'Menu': 127}
MODIFIERS = {'Control_L': 29, 'Shift_L': 42, 'Alt_L': 56}


def load():
    """scripts/remote_keys.py, imported by path: scripts/ is not a package."""
    if 'remote_keys' not in sys.modules:
        path = ROOT / 'scripts' / 'remote_keys.py'
        spec = importlib.util.spec_from_file_location('remote_keys', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules['remote_keys'] = module
        spec.loader.exec_module(module)
    return sys.modules['remote_keys']


class FakeBus:
    """A Gio.DBusConnection's call_sync, recording the keycode and state of each notification."""

    def __init__(self):
        self.sent = []

    def call_sync(self, _name, _path, _interface, method, arguments, *_rest):
        self.sent.append((method, arguments.unpack() if arguments is not None else None))
        return None


@requires_gtk
class KeymapTest(unittest.TestCase):
    """The keyval a shortcut names, to the evdev keycode a keyboard would send for it."""

    @classmethod
    def setUpClass(cls):
        cls.keys = load()

    def code(self, name):
        from gi.repository import Gdk

        return self.keys.evdev_key(Gdk.keyval_from_name(name))

    def test_the_keys_every_layout_leaves_alone(self):
        for name, evdev in FIXED.items():
            with self.subTest(key=name):
                self.assertEqual(self.code(name), (evdev, False))

    def test_the_modifiers(self):
        for name, evdev in MODIFIERS.items():
            with self.subTest(key=name):
                self.assertEqual(self.code(name), (evdev, False))

    def test_a_keyval_on_a_shifted_level_brings_shift(self):
        # Where the letter sits depends on the layout; that its capital is Shift and the same
        # key does not.
        lower, shifted_lower = self.code('a')
        upper, shifted_upper = self.code('A')
        self.assertEqual(lower, upper)
        self.assertFalse(shifted_lower)
        self.assertTrue(shifted_upper)

    def test_nothing_for_a_keyval_no_key_makes(self):
        from gi.repository import Gdk

        self.assertIsNone(self.keys.evdev_key(Gdk.KEY_Kanji))


@requires_gtk
class SendTest(unittest.TestCase):
    """What a press puts on the bus: the modifiers down, the key down and up, the modifiers up."""

    @classmethod
    def setUpClass(cls):
        cls.keys = load()

    def sent(self, accel):
        bus = FakeBus()
        self.keys.Keyboard(bus, '/session').send(accel)
        methods = {method for method, _arguments in bus.sent}
        self.assertEqual(methods, {'NotifyKeyboardKeycode'})
        return [arguments for _method, arguments in bus.sent]

    def test_a_bare_key(self):
        self.assertEqual(self.sent('Down'), [(108, True), (108, False)])

    def test_the_modifiers_are_held_around_it(self):
        self.assertEqual(self.sent('<primary>comma'),
                         [(29, True), (self.keys.evdev_key(0x2c)[0], True),
                          (self.keys.evdev_key(0x2c)[0], False), (29, False)])

    def test_a_shifted_keyval_holds_shift_once(self):
        # ISO_Left_Tab is Shift and Tab, and the accelerator names Shift too.
        self.assertEqual(self.sent('<shift>ISO_Left_Tab'),
                         [(42, True), (15, True), (15, False), (42, False)])

    def test_an_accelerator_that_does_not_parse(self):
        with self.assertRaises(ValueError):
            self.sent('not a key')

    def test_stopping_the_session_once(self):
        bus = FakeBus()
        keyboard = self.keys.Keyboard(bus, '/session')
        keyboard.close()
        keyboard.close()
        self.assertEqual([method for method, _arguments in bus.sent], ['Stop'])


if __name__ == '__main__':
    unittest.main()
