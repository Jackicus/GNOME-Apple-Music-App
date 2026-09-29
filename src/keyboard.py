"""The window's own keys: which playback action a key press runs, given where the focus is.

The playback keys (shortcuts.PLAYBACK: Space, Ctrl+Right, Ctrl+Left) are not application
accelerators: GTK 4 runs those in the window's capture phase, before the focus widget, so a
bare Space would fire while typing in the Songs filter and Ctrl+Left would skip a track
instead of a word. The window's capture-phase key controller asks playback_action() instead,
which leaves the keys to the widgets whose keys they are, and the window activates the
action it names. The decision takes plain values (the focus widget's class, not the widget),
so tests/test_keyboard.py checks it without a display.

    name = playback_action(keyval, mods, type(focus), dialog_open, in_popover, enabled)
"""

from gi.repository import Gdk, Gtk

from . import shortcuts


def parse_keys(table):
    """{(keyval, modifiers): action} for a table of action -> accelerator strings."""
    keys = {}
    for name, accels in table.items():
        for accel in accels:
            ok, keyval, mods = Gtk.accelerator_parse(accel)
            if ok:
                keys[(keyval, int(mods))] = name
    return keys


# The playback keys, by (keyval, modifiers) -> app action.
PLAYBACK_KEYS = parse_keys(shortcuts.PLAYBACK)

# The primary menu's key (F10), which the window handles itself when the button is hidden.
MAIN_MENU_KEY = Gtk.accelerator_parse(shortcuts.MAIN_MENU)[1:]

# Where a key is the focused widget's own, whatever it is: an entry or a text view.
TYPING = (Gtk.Editable, Gtk.TextView)
# Where Space is the widget's own key: it toggles these, so it toggles them rather than
# playback. (Enter presses a button; Space on a plain button, a tile or a row plays or pauses.)
SPACE_OWNERS = (Gtk.ToggleButton, Gtk.Switch, Gtk.CheckButton)
SPACE_KEYS = (Gdk.KEY_space, Gdk.KEY_KP_Space)


def playback_action(keyval, mods, focus_type, dialog_open, in_popover, enabled):
    """The app action (without "app.") that a key press runs, or None when the key is not
    a playback key or belongs to something else: the focus widget (`focus_type`, its class,
    or type(None) with no focus) when it is typed into (TYPING), or for Space when it owns
    Space (SPACE_OWNERS); a dialog open over the window; a popover (a menu) the focus is
    in; the action itself when it is not enabled (`enabled(name)`: nothing plays)."""
    name = PLAYBACK_KEYS.get((keyval, int(mods)))
    if name is None:
        return None
    if dialog_open or in_popover:
        return None
    if issubclass(focus_type, TYPING):
        return None
    if keyval in SPACE_KEYS and issubclass(focus_type, SPACE_OWNERS):
        return None
    if not enabled(name):
        return None
    return name


def is_main_menu(keyval, mods):
    """Whether the key press is the primary menu's (F10, no modifiers)."""
    return (keyval, int(mods)) == MAIN_MENU_KEY
