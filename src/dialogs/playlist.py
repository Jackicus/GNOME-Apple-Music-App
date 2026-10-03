# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The playlists' dialogs, which the item actions (actions.py) present over the window.

    NameDialog(heading, confirm, name, description, done)   # a name, and a description
    confirm(parent, heading, body, confirm, done)           # before something is deleted

NameDialog asks for a new playlist's name and description, a playlist's new ones, or a
folder's new name (no description row: `description` None). Its confirming response is the
default, so Enter in either row confirms, and it is off while the name is blank; done(name,
description) gets the name stripped and the description as typed, or None when it is the one
the dialog opened with (nothing to change). confirm() is libadwaita's alert, the deleting
response destructive and Cancel the default, as Clear Cache's and Sign Out's are.
"""

from gettext import gettext as _

from gi.repository import Adw, Gtk

from ..widgets.util import connect_weak

CANCEL = 'cancel'
CONFIRM = 'confirm'


class NameDialog(Adw.AlertDialog):
    """A name (and a description) for a playlist or a folder. See the module."""

    __gtype_name__ = 'AppleMusicNameDialog'

    def __init__(self, heading, confirm, name='', description=None, done=None):
        super().__init__(heading=heading)
        self._done = done
        self._description = description
        self.name_row = Adw.EntryRow(title=_('Name'), text=name or '', activates_default=True)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
        rows.append(self.name_row)
        self.description_row = None
        if description is not None:
            self.description_row = Adw.EntryRow(title=_('Description'), text=description,
                                                activates_default=True)
            rows.append(self.description_row)
        self.set_extra_child(rows)
        self.add_response(CANCEL, _('_Cancel'))
        self.add_response(CONFIRM, confirm)
        self.set_response_appearance(CONFIRM, Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response(CONFIRM)
        self.set_close_response(CANCEL)
        self.set_focus(self.name_row)
        connect_weak(self.name_row, 'changed', self._on_name_changed)
        self.connect('response', NameDialog._on_response)
        self._on_name_changed()

    def name(self):
        return self.name_row.get_text().strip()

    def description(self):
        """The description typed, or None when it is unchanged (or there is none to edit)."""
        if self.description_row is None:
            return None
        text = self.description_row.get_text()
        return None if text == self._description else text

    def _on_name_changed(self, *_args):
        self.set_response_enabled(CONFIRM, bool(self.name()))

    def _on_response(self, response):
        if response == CONFIRM and self.name() and self._done is not None:
            self._done(self.name(), self.description())
        self._done = None  # the actions it holds go with the dialog's answer


def confirm(parent, heading, body, label, done):
    """An alert over `parent` asking to delete something: Cancel (the default, and what
    Escape does) and `label`, destructive, which calls done(). The dialog."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response(CANCEL, _('_Cancel'))
    dialog.add_response(CONFIRM, label)
    dialog.set_response_appearance(CONFIRM, Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response(CANCEL)
    dialog.set_close_response(CANCEL)
    dialog.connect('response', lambda _dialog, response: done() if response == CONFIRM else None)
    dialog.present(parent)
    return dialog
