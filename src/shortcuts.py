# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The keyboard shortcuts in one table: the accelerators the app sets, the playback keys the
window handles itself, and the Keyboard Shortcuts dialog, which lists them all.

main.py sets ACCELS with Gtk.Application.set_accels_for_action and builds the dialog from
sections(); keyboard.py parses PLAYBACK into its PLAYBACK_KEYS and MAIN_MENU into the window's
F10, and widgets/context_menu.py binds CONTEXT_MENU. tests/test_shortcuts.py checks that the
dialog lists every accelerator and playback key. Keys that are none of these are listed as they
are: Escape (the Now Playing sheet's, the dialogs').

The playback keys are not accelerators: GTK 4 runs application accelerators in the window's
capture phase, before the focus widget, so a bare Space would fire while typing in an entry
and Ctrl+Left would skip a track instead of a word (see keyboard.playback_action). The
accelerators keep off the HIG's standard ones the app has no use for (Ctrl+N is "New").
"""

from gettext import gettext as _

# Action -> its accelerators (Gtk.Application.set_accels_for_action).
ACCELS = {
    'app.quit': ('<primary>q',),
    'app.shortcuts': ('<primary>question',),
    'app.preferences': ('<primary>comma',),
    'app.sync': ('<primary>r',),
    'app.now-playing': ('<primary><shift>n',),
    'window.close': ('<primary>w',),
    'win.back': ('<alt>Left',),
    'win.search': ('<primary>f',),
    'win.focus-sidebar': ('<primary>1',),
    'win.focus-content': ('<primary>2',),
    'win.focus-player': ('<primary>3',),
}

# App action (without "app.") -> the keys the window's own key controller maps to it.
PLAYBACK = {
    'play-pause': ('space', 'KP_Space'),
    'next': ('<primary>Right',),
    'previous': ('<primary>Left',),
}

MAIN_MENU = 'F10'
CONTEXT_MENU = 'Menu <shift>F10'
CLOSE = 'Escape'


def accelerator(key):
    """What the dialog shows for `key`: an action's accelerators (ACCELS), a playback action's
    first key (PLAYBACK), or `key` itself, an accelerator string (alternatives separated by
    spaces, as Adw.ShortcutLabel takes them)."""
    if key in ACCELS:
        return ' '.join(ACCELS[key])
    if key in PLAYBACK:
        return PLAYBACK[key][0]
    return key


def sections():
    """The dialog's sections, translated on call: [(title, [(title, key)])], each key as
    accelerator() takes it."""
    return [
        (_('General'), [
            (_('Show Main Menu'), MAIN_MENU),
            (_('Preferences'), 'app.preferences'),
            (_('Refresh Library'), 'app.sync'),
            (_('Keyboard Shortcuts'), 'app.shortcuts'),
            (_('Close Window'), 'window.close'),
            (_('Quit'), 'app.quit'),
        ]),
        (_('Navigation'), [
            (_('Search or Filter'), 'win.search'),
            (_('Go Back'), 'win.back'),
            (_('Focus the Sidebar'), 'win.focus-sidebar'),
            (_('Focus the Page'), 'win.focus-content'),
            (_('Focus the Player Bar'), 'win.focus-player'),
            (_('Show Context Menu'), CONTEXT_MENU),
        ]),
        (_('Playback'), [
            (_('Play or Pause'), 'play-pause'),
            (_('Next'), 'next'),
            (_('Previous'), 'previous'),
            (_('Show or Hide Now Playing'), 'app.now-playing'),
            (_('Close Now Playing or a Dialog'), CLOSE),
        ]),
    ]
