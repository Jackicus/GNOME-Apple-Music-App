"""EngineStatus: what a page that asks the engine (Search, New, Made for You, a category, an
album's or an artist's page) shows when the engine cannot answer, and what its button does.
No widget: the page draws the state, so the rules live here once and are tested without a
display (tests/test_engine_status.py).

    self._status = EngineStatus(app(), self._show_status, self._load, {
        'engine-down': _('Start the engine to load this page'),
        'not-signed-in': _('This page appears once you sign in to Apple Music'),
        'failed': _('Could Not Load This Page'),
    })
    self._status.fail(error)      # an EngineError from the page's request
    self._status.activate()       # the status page's button
    self._status.watch()          # from do_map; unwatch() from do_unmap

`show(status, title, description, button)` draws `status`: 'loading' (a spinner; the texts
are None), or one of the states below with its words (`button` None for none). `retry()` asks
again: the page's load or fetch.

The states, from the error's code and the app's:
- 'demo': the demo library has no engine. Nothing is wrong, the page just has no source: no
  button.
- 'not-signed-in': the account is not signed in (an engine-down while the sign-in setting is
  off is this too: starting the engine could not help). Sign In opens the sign-in; once the
  engine is authorized, retry().
- 'engine-down': the engine is not running. While it is starting (autostart, a sign-in, a
  play elsewhere) the spinner shows; once it is up, retry(); if it falls back to down, the
  state shows again. Start Engine starts it through app.start_engine(), which quitting
  cancels; a start that fails is reported by the app (its toast says why), and the page shows
  this state again rather than the same failure a second time.
- 'failed': any other failure: the page's own title, the app's sentence for the code
  (errors.error_message(); the detail goes to the log) and Try Again.
"""

import logging
from gettext import gettext as _

from ..backend import errors
from ..errors import error_message
from .util import connect_weak, weak_method

log = logging.getLogger(__name__)


def _weak(callback):
    """A bound method held weakly (the page owns this helper: a strong one would keep a
    dropped page alive through its own attribute); anything else as it is."""
    return weak_method(callback) if hasattr(callback, '__self__') else callback


class EngineStatus:
    """See the module. `texts` gives the page's words: 'engine-down' (the description under
    "Engine Not Running"), 'not-signed-in' (a description, or a (title, description) pair) and
    'failed' (the title over a failure)."""

    def __init__(self, app, show, retry, texts):
        self._app = app
        self._show = _weak(show)
        self._retry = _weak(retry)
        self._texts = texts
        self._handlers = []
        self.status = None  # the state shown: None, 'loading' or one of the module's

    # What the page asks.

    def loading(self):
        """The spinner, while the page's request is on its way."""
        self.status = 'loading'
        self._show('loading', None, None, None)

    def clear(self):
        """The page shows its content: nothing here, and nothing to retry."""
        self.status = None

    def fail(self, error):
        """Show what the EngineError `error` means for the page."""
        code = getattr(error, 'code', None)
        if code == errors.ENGINE_DOWN:
            if self._app.demo:
                self._set('demo')
            elif not self._signed_in():
                self._set('not-signed-in')
            else:
                self._set('engine-down')
        elif code == errors.NOT_SIGNED_IN:
            self._set('not-signed-in')
        else:
            log.info('%s', error)
            self._set('failed', code)

    def activate(self):
        """The status page's button: Sign In, Start Engine or Try Again."""
        if self.status == 'not-signed-in':
            self._app.activate_action('sign-in', None)
        elif self.status == 'engine-down':
            self._show('loading', None, None, None)
            task = self._app.start_engine()
            if task is not None:
                self._app.spawn(self._started(task))
        elif self.status == 'failed':
            self._do_retry()

    # The engine, watched while the page is shown.

    def watch(self):
        """Follow the engine (from the page's do_map): up or authorized retries a state it
        can clear; starting shows the spinner."""
        if self._handlers:
            return
        engine = self._app.engine
        self._handlers = [connect_weak(engine, 'notify::state', self._on_engine_changed),
                          connect_weak(engine, 'notify::authorized', self._on_engine_changed)]
        self._on_engine_changed(engine, None)  # what changed while the page was hidden

    def unwatch(self):
        """Stop following the engine (from the page's do_unmap)."""
        engine = self._app.engine
        for handler in self._handlers:
            engine.disconnect(handler)
        self._handlers = []

    def _on_engine_changed(self, engine, _pspec):
        if self.status == 'engine-down':
            if engine.state == 'up':
                self._do_retry()
            else:
                self._set('engine-down')  # the spinner while starting; the button once down
        elif self.status == 'not-signed-in' and engine.authorized:
            self._do_retry()

    async def _started(self, task):
        await task  # app.start_engine() reports its own failure
        if self.status != 'engine-down':
            return  # retried already (the engine came up while watched), or moved on
        if self._app.engine.state == 'up':
            self._do_retry()
        else:
            self._set('engine-down')

    # Showing.

    def _do_retry(self):
        self.status = None
        self._retry()

    def _signed_in(self):
        return self._app.settings.get_boolean(self._app.account_key('signed-in'))

    def _set(self, status, code=None):
        self.status = status
        if status == 'engine-down' and self._app.engine.state == 'starting':
            self._show('loading', None, None, None)  # still 'engine-down': up retries
            return
        if status == 'demo':
            title = _('Not Available in the Demo')
            description = _('This page needs Apple Music; the demo library is offline')
            button = None
        elif status == 'not-signed-in':
            text = self._texts.get('not-signed-in')
            if isinstance(text, tuple):
                title, description = text
            else:
                title, description = _('Sign In to Apple Music'), text or ''
            button = _('Sign In')
        elif status == 'engine-down':
            title = _('Engine Not Running')
            description = self._texts.get('engine-down') or ''
            button = _('Start Engine')
        else:
            title = self._texts.get('failed') or _('Something Went Wrong')
            description = error_message(code)[0]
            button = _('Try Again')
        self._show(status, title, description, button)
