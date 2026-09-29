# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""What the user is told when the engine fails: one sentence per EngineError code, and the
button that helps, if one does. Application.report() shows it; the error's own message (what
Chrome, the page or Apple said) goes to the log only.

    title, button_label, action = error_message(error.code)

No GTK: the words are translated on call.
"""

from gettext import gettext as _

from .backend import errors


def error_message(code):
    """(the toast's title, its button's label or None, the action the button runs or None)
    for an EngineError code; a code this does not know gets the generic sentence."""
    if code == errors.NO_BROWSER:
        return (_('Google Chrome is needed to play Apple Music'), _('Preferences'),
                'app.show-engine-preferences')
    if code == errors.ENGINE_DOWN:
        return _('The playback engine is not running'), _('Start'), 'app.start-engine'
    if code == errors.NOT_SIGNED_IN:
        return _('Sign in to Apple Music again'), _('Sign In'), 'app.sign-in'
    if code == errors.TIMEOUT:
        return _('Apple Music did not answer in time'), None, None
    if code == errors.API:
        return _('Apple Music could not do that'), None, None
    return _('Something went wrong'), None, None
