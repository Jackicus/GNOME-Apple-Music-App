# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The Keyboard Shortcuts dialog (app.shortcuts, Ctrl+?): every accelerator and key of
shortcuts.py, grouped as shortcuts.sections() groups them.

    shortcuts.present(window)

libadwaita 1.9 leaves the dialog's rows without accessible names (a screen reader finds only
their parts), so each is named "title: keys" once it is built.
"""

from gettext import gettext as _

from gi.repository import Adw, Gtk

from ..shortcuts import accelerator, sections


def present(parent):
    """Build the dialog and present it over `parent` (a window, or None); returns it."""
    dialog = Adw.ShortcutsDialog()
    names = {}
    for title, items in sections():
        section = Adw.ShortcutsSection(title=title)
        for item_title, key in items:
            section.add(Adw.ShortcutsItem.new(item_title, accelerator(key)))
            keys = [_key_label(accel) for accel in accelerator(key).split()]
            if len(keys) > 1:
                # Translators: a shortcut's alternative keys, as a screen reader hears them:
                # {keys} is the list but the last ("Menu"), {key} the last ("Shift+F10").
                keys = [_('{keys} or {key}').format(keys=', '.join(keys[:-1]), key=keys[-1])]
            # Translators: a shortcut's row as a screen reader hears it: {title} is what the
            # shortcut does ("Quit"), {keys} its key or keys ("Ctrl+Q").
            names[item_title] = _('{title}: {keys}').format(title=item_title, keys=keys[0])
        dialog.add(section)
    dialog.present(parent)
    _name_rows(dialog, names)
    return dialog


def _key_label(accel):
    """How a key reads: "Ctrl+Q" for <primary>q."""
    ok, key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_get_label(key, mods) if ok else accel


def _name_rows(widget, names):
    """Give the rows under widget whose title is a key of names that name (the dialog's
    AdwShortcutRows: see the module)."""
    title = getattr(widget, 'get_title', None)
    if (title is not None and isinstance(widget, Gtk.ListBoxRow)
            and title() in names):
        widget.update_property([Gtk.AccessibleProperty.LABEL], [names[title()]])
    child = widget.get_first_child()
    while child is not None:
        _name_rows(child, names)
        child = child.get_next_sibling()
