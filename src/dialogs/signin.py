"""AppleMusicSignInDialog: the sign-in flow, shown while it runs.

Presenting the dialog starts the flow as a task: the engine restarted with a visible Chrome
window, the page asked to authorize (Apple's sign-in appears in that window), a wait for
MusicKit to report the account authorized (the event, or a poll every two seconds, for up to
ten minutes), then `signed-in` set, the account name read from the page if it can be, and the
engine restarted headless when `engine-headless` is on. Cancel (or closing the dialog) cancels
the task, which stops the engine. An error is toasted and stops the engine too.
"""

import asyncio
import logging
from gettext import gettext as _

from gi.repository import Adw, Gtk

from ..backend.errors import EngineError

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
        self.connect('closed', self._on_closed)

    def present(self, parent=None):
        Adw.Dialog.present(self, parent)
        if self._task is None:
            self._task = self._app.spawn(self._run())

    async def _run(self):
        app = self._app
        engine = app.engine
        settings = app.settings
        try:
            self.status_page.set_description(_('Starting Chrome…'))
            await engine.restart(visible=True)
            self.status_page.set_description(
                _('Sign in with your Apple ID in the Chrome window'))
            await engine.signin()
            settings.set_boolean('signed-in', True)
            self.status_page.set_description(_('Signed in'))
            self.cancel_button.set_sensitive(False)
            name = await engine.account_name()
            settings.set_string('account-name', name)
            if name:
                log.info('account name read from the page')
            else:
                log.info('no account name found on the page; the button says Signed In')
            if settings.get_boolean('engine-headless'):
                self.status_page.set_description(_('Restarting the engine in the background…'))
                await engine.restart(visible=False)
            self._finish()
            app.toast(_('Signed in'))
        except asyncio.CancelledError:
            log.info('sign-in cancelled')
            app.spawn(engine.stop())
            self._finish()
            raise
        except EngineError as error:
            app.report(error)
            app.spawn(engine.stop())
            self._finish()

    def _finish(self):
        self._task = None
        if not self._closed:
            self.close()

    @Gtk.Template.Callback()
    def on_cancel_clicked(self, _button):
        self._cancel()

    def _on_closed(self, _dialog):
        self._closed = True
        self._cancel()  # Escape or the close button: the same as Cancel

    def _cancel(self):
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
