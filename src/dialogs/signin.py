# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicSignInDialog: the sign-in flow (account.sign_in()), shown while it runs.

Presenting the dialog starts the flow as a task: Chrome restarted in a window, Apple's
sign-in in that window, and a wait for MusicKit to report the account authorized. The
dialog shows (and announces) each step. Once signed in, the flow is committed and the dialog
closes; the rest (the account's name, Chrome going headless, the first sync) goes on without
it. Until then Cancel, Escape or the close button cancel the flow, which stops the engine; a
failure is toasted and closes the dialog too.
"""

import logging

from gi.repository import Adw, Gtk

from .. import account
from ..widgets.util import connect_weak

log = logging.getLogger(__name__)


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/signin.ui')
class SignInDialog(Adw.Dialog):
    __gtype_name__ = 'AppleMusicSignInDialog'

    status_page = Gtk.Template.Child()
    cancel_button = Gtk.Template.Child()

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._task = None
        self._closed = False
        # Weakly (widgets/util.py): a bound method would keep every closed dialog alive.
        connect_weak(self.cancel_button, 'clicked', self._on_cancel_clicked)
        self.connect('closed', self._on_closed)

    def present(self, parent=None):
        Adw.Dialog.present(self, parent)
        if self._task is None:
            self._task = self._app.spawn(self._run())

    async def _run(self):
        try:
            await account.sign_in(self._app, self._set_status)
        finally:
            self._finish()

    def _set_status(self, text):
        """A step of the flow: under the title, and read out (through the window, which
        GTK always lets announce)."""
        if self._closed:
            return
        self.status_page.set_description(text)
        root = self.get_root()
        if root is not None:
            root.announce(text, Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def _finish(self):
        self._task = None
        if not self._closed:
            self.close()

    def _on_cancel_clicked(self, _button):
        self._cancel()

    def _on_closed(self, _dialog):
        self._closed = True
        self._cancel()  # Escape or the close button: the same as Cancel

    def _cancel(self):
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
