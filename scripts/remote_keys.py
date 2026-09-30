# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Real key presses on the headless display, through the compositor's RemoteDesktop API.

scripts/headless.sh runs mutter in a D-Bus session of its own, where mutter owns
``org.gnome.Mutter.RemoteDesktop``. A session on that interface injects keyboard events into
the compositor as a keyboard would, so the focused window is active and GTK sees what a real
press makes: the application's accelerators, a dialog's mnemonics, the toolkit's own focus
movement, and the handlers that read a widget's has-focus (a boxed list passing the focus to
the next one) all run for real. Only keyboard events are asked for, so no screen-cast stream
and no virtual monitor beyond the one headless.sh already starts are needed.

    keyboard = remote_keys.open_keyboard()   # raises Unavailable where there is no such bus
    keyboard.send('<primary>comma')          # Ctrl+, as the keyboard sends it
    keyboard.type_text('tide')               # each character in turn
    keyboard.close()

``NotifyKeyboardKeycode`` takes evdev keycodes, not keysyms and not GDK's: Tab is KEY_TAB, 15,
not ``Gdk.KEY_Tab`` (0xff09) and not the 23 GDK reports as its hardware keycode. send() goes
the other way round from an accelerator: Gtk.accelerator_parse gives the keyval and the
modifiers, Gdk.Display.map_keyval finds the keys that make that keyval on the layout the
compositor gave this client, and a Wayland (or XKB) keycode is the evdev one plus 8. A keyval
that lives on a shifted level of its key (``question`` and ``ISO_Left_Tab`` on a British
layout) brings Shift with it, which is what a person's hand does too.
"""

import gi

gi.require_version('Gdk', '4.0')
gi.require_version('Gtk', '4.0')
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402  (after require_version)

BUS_NAME = 'org.gnome.Mutter.RemoteDesktop'
OBJECT_PATH = '/org/gnome/Mutter/RemoteDesktop'
SESSION_INTERFACE = BUS_NAME + '.Session'
TIMEOUT = 5000  # ms: the compositor answers at once or something is wrong

EVDEV_OFFSET = 8  # a Wayland or XKB keycode is the evdev one plus 8

# The modifiers an accelerator can name, and the key that holds each down.
MODIFIER_KEYS = ((Gdk.ModifierType.CONTROL_MASK, Gdk.KEY_Control_L),
                 (Gdk.ModifierType.SHIFT_MASK, Gdk.KEY_Shift_L),
                 (Gdk.ModifierType.ALT_MASK, Gdk.KEY_Alt_L),
                 (Gdk.ModifierType.SUPER_MASK, Gdk.KEY_Super_L))


class Unavailable(Exception):
    """No RemoteDesktop keyboard here; the message says what answered instead."""


def evdev_key(keyval):
    """(evdev keycode, whether Shift makes it) for a keyval on the display's current layout,
    or None when no key makes it. The first group's lowest level is the key a hand would use."""
    display = Gdk.Display.get_default()
    ok, keys = display.map_keyval(keyval)
    if not ok:
        return None
    for entry in sorted(keys, key=lambda key: (key.group, key.level)):
        if entry.group == 0 and entry.level in (0, 1):
            return entry.keycode - EVDEV_OFFSET, entry.level == 1
    return None


class Keyboard:
    """A started RemoteDesktop session, sending keys until close()."""

    def __init__(self, bus, session):
        self._bus = bus
        self._session = session

    def _notify(self, keycode, state):
        self._bus.call_sync(BUS_NAME, self._session, SESSION_INTERFACE,
                            'NotifyKeyboardKeycode', GLib.Variant('(ub)', (keycode, state)),
                            None, Gio.DBusCallFlags.NONE, TIMEOUT, None)

    def send(self, accel):
        """Press and release the accelerator (Gtk.accelerator_parse's spelling:
        '<primary><shift>n', 'Down', 'space'), its modifiers held around it."""
        ok, keyval, mods = Gtk.accelerator_parse(accel)
        if not ok:
            raise ValueError(f'remote_keys: {accel!r} is not an accelerator')
        self.send_keyval(keyval, mods)

    def send_keyval(self, keyval, mods=0):
        """Press and release the key that makes keyval, with the modifiers in mods held."""
        found = evdev_key(keyval)
        if found is None:
            raise ValueError('remote_keys: no key on this layout makes '
                             f'{Gdk.keyval_name(keyval)}')
        keycode, shifted = found
        mods = Gdk.ModifierType(int(mods))
        if shifted:
            mods |= Gdk.ModifierType.SHIFT_MASK
        held = [evdev_key(modifier)[0] for mask, modifier in MODIFIER_KEYS if mods & mask]
        for code in held:
            self._notify(code, True)
        self._notify(keycode, True)
        self._notify(keycode, False)
        for code in reversed(held):
            self._notify(code, False)

    def nudge(self):
        """Press and release Shift on its own. The headless compositor points the keyboard at
        a window only once it has a keyboard event to deliver, so a window is not active — and
        the first key sent to it is lost — until something has been pressed; a modifier alone
        changes nothing else."""
        self.send_keyval(Gdk.KEY_Shift_L)

    def type_text(self, text):
        """Type text, character by character, as a person would."""
        for character in text:
            self.send_keyval(Gdk.unicode_to_keyval(ord(character)))

    def close(self):
        """Stop the session (mutter warns about a client that vanishes holding one)."""
        if self._session is None:
            return
        session, self._session = self._session, None
        try:
            self._bus.call_sync(BUS_NAME, session, SESSION_INTERFACE, 'Stop', None, None,
                                Gio.DBusCallFlags.NONE, TIMEOUT, None)
        except GLib.Error:
            pass  # the compositor is gone, which stops it anyway


def open_keyboard():
    """A started Keyboard on the session bus's RemoteDesktop interface. Raises Unavailable
    where there is none (no compositor on this bus, or one that refuses a session): on a real
    desktop the API is behind the remote-desktop portal, which asks the user first."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as error:
        raise Unavailable(f'no session bus: {error.message}') from error
    try:
        session = bus.call_sync(BUS_NAME, OBJECT_PATH, BUS_NAME, 'CreateSession', None,
                                GLib.VariantType('(o)'), Gio.DBusCallFlags.NONE, TIMEOUT,
                                None)[0]
    except GLib.Error as error:
        raise Unavailable(f'{BUS_NAME}.CreateSession: {error.message}') from error
    keyboard = Keyboard(bus, session)
    try:
        bus.call_sync(BUS_NAME, session, SESSION_INTERFACE, 'Start', None, None,
                      Gio.DBusCallFlags.NONE, TIMEOUT, None)
    except GLib.Error as error:
        keyboard.close()
        raise Unavailable(f'{SESSION_INTERFACE}.Start: {error.message}') from error
    return keyboard
