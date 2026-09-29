# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The one error the backend raises, and the codes the UI switches on."""

# The README's codes. The UI shows a toast for each, never a traceback.
ENGINE_DOWN = 'engine-down'      # Chrome not running, exited, or the connection closed
NO_BROWSER = 'no-browser'        # no Google Chrome to run, or it could not be started
NOT_SIGNED_IN = 'not-signed-in'  # MusicKit is not authorized
API = 'api'                      # Apple, MusicKit or the page said no
TIMEOUT = 'timeout'              # Chrome or the page took too long
USAGE = 'usage'                  # a bad argument (the debug CLI's)

CODES = (ENGINE_DOWN, NO_BROWSER, NOT_SIGNED_IN, API, TIMEOUT, USAGE)


class EngineError(Exception):
    """A failure with one of the codes above; `message` says what, for the log or a toast.
    `status` is the HTTP status Apple answered an API request with, when it said (an 'api'
    error from api.api_error), else None. `musickit_code` is MusicKit's own code for a
    playback it refused ('CONTENT_UNAVAILABLE', 'SUBSCRIPTION_ERROR'…), else None."""

    def __init__(self, code, message='', status=None, musickit_code=None):
        if code not in CODES:
            raise ValueError(f'not an engine error code: {code!r}')
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = status
        self.musickit_code = musickit_code

    def __str__(self):
        return f'{self.code}: {self.message}' if self.message != self.code else self.code
