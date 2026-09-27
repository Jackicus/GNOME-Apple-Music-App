"""The one error the backend raises, and the codes the UI switches on."""

# The README's codes. The UI shows a toast for each, never a traceback.
ENGINE_DOWN = 'engine-down'      # no Chrome, no DevTools, a closed connection
NOT_SIGNED_IN = 'not-signed-in'  # MusicKit is not authorized
API = 'api'                      # Apple, MusicKit or the page said no
TIMEOUT = 'timeout'              # Chrome or the page took too long
USAGE = 'usage'                  # a bad argument (the debug CLI's)

CODES = (ENGINE_DOWN, NOT_SIGNED_IN, API, TIMEOUT, USAGE)


class EngineError(Exception):
    """A failure with one of the codes above; `message` says what, for the log or a toast."""

    def __init__(self, code, message=''):
        if code not in CODES:
            raise ValueError(f'not an engine error code: {code!r}')
        super().__init__(message or code)
        self.code = code
        self.message = message or code

    def __str__(self):
        return f'{self.code}: {self.message}' if self.message != self.code else self.code
