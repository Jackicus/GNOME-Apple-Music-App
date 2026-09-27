"""Finding and describing the Chrome the engine runs in, and the file that remembers it.

Nothing here starts a process: the app spawns Chrome with Gio.Subprocess and the debug CLI with
asyncio; both build the command line with chrome_args(), record it in an EngineState and wait
for its DevTools port with wait_for_devtools(). The HTTP here is urllib in a thread, so nothing
blocks the loop.
"""

import asyncio
import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from .errors import EngineError

log = logging.getLogger(__name__)

# The real Google Chrome only: it ships Widevine, which the streams need; Chromium does not.
CANDIDATES = ('google-chrome-stable', 'google-chrome', '/opt/google/chrome/chrome')
START_URL = 'https://music.apple.com/'
DEVTOOLS_HOST = '127.0.0.1'


def find_chrome(command=None, path=None):
    """The executable for `command` (the configured browser), else the first of CANDIDATES
    on `path` (default $PATH); None when there is no Chrome."""
    for name in (command, *CANDIDATES):
        if not name:
            continue
        found = shutil.which(name, path=path)
        if found:
            return found
    return None


def chrome_args(binary, profile, port, headless=True):
    """The argv that runs `binary` on `profile` with DevTools on 127.0.0.1:`port`, showing
    music.apple.com; headless (no window, audio still out) or a visible window for sign-in."""
    args = [
        str(binary),
        f'--user-data-dir={profile}',
        f'--remote-debugging-port={port}',
        f'--remote-debugging-address={DEVTOOLS_HOST}',
        # MusicKit's play() is called from CDP, not from a click in the page.
        '--autoplay-policy=no-user-gesture-required',
        # Chrome would otherwise publish org.mpris.MediaPlayer2.chromium.instance<pid> and take
        # the media keys; the app owns the MPRIS service and relays them itself.
        '--disable-features=HardwareMediaKeyHandling',
        '--no-first-run',
        '--no-default-browser-check',
        # A Chrome that had to be killed (the 5 s SIGTERM grace ran out, the app crashed) would
        # greet the next visible window with the "Restore pages?" bubble.
        '--hide-crash-restore-bubble',
    ]
    if headless:
        args.append('--headless=new')
        args.append(START_URL)
    else:
        # As am.py did: an app window of the page alone, without tabs or an address bar, is what
        # the sign-in flow shows.
        args.append(f'--app={START_URL}')
    return args


def pid_alive(pid, profile=None):
    """Whether `pid` is a live process, and (on a system with /proc, when `profile` is given)
    a Chrome on that profile rather than whatever reused the number."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if profile is None:
        return True
    try:
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return True  # no /proc: liveness is all there is to go on
    if not cmdline:
        # Empty for an instant while the process execs, and for good once it is a zombie.
        try:
            return ') Z ' not in Path(f'/proc/{pid}/stat').read_text()
        except OSError:
            return True
    return f'--user-data-dir={profile}'.encode() in cmdline


class EngineState:
    """What engine.json records about the Chrome the app started: {pid, port, headless,
    profile, started}. `load` returns None for a missing or unreadable file; `alive` is whether
    the pid still runs a Chrome on the profile."""

    def __init__(self, pid, port, headless, profile, started=None):
        self.pid = int(pid)
        self.port = int(port)
        self.headless = bool(headless)
        self.profile = str(profile)
        self.started = int(started if started is not None else time.time())

    def to_dict(self):
        return {'pid': self.pid, 'port': self.port, 'headless': self.headless,
                'profile': self.profile, 'started': self.started}

    @classmethod
    def from_dict(cls, data):
        return cls(data['pid'], data['port'], data.get('headless', True), data.get('profile', ''),
                   data.get('started'))

    @classmethod
    def load(cls, path):
        """The state in `path`, or None when there is none or it is not readable."""
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            return cls.from_dict(data)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, path):
        """Write atomically, creating the directory."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp, path)

    @staticmethod
    def remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    @property
    def alive(self):
        return pid_alive(self.pid, self.profile)

    def __repr__(self):
        return f'EngineState(pid={self.pid}, port={self.port}, headless={self.headless})'


def _get_json(url, timeout):
    """GET `url` and parse the body as JSON; in a thread, so it may block. No proxy: urllib
    would send even 127.0.0.1 through $http_proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={'User-Agent': 'AppleMusicGNOME/1.0'})
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def devtools_url(port, path):
    return f'http://{DEVTOOLS_HOST}:{port}{path}'


async def get_json(port, path, timeout=5.0):
    """Chrome's /json answer at `path` on `port`. EngineError('engine-down') when nothing
    answers, ('timeout') when it is slow."""
    try:
        return await asyncio.to_thread(_get_json, devtools_url(port, path), timeout)
    except TimeoutError as e:
        raise EngineError('timeout', f'DevTools on port {port} did not answer') from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, 'reason', e)
        if isinstance(reason, TimeoutError):
            raise EngineError('timeout', f'DevTools on port {port} did not answer') from e
        raise EngineError('engine-down', f'no DevTools on port {port}: {reason}') from e


async def wait_for_devtools(port, timeout=15.0):
    """Poll /json/version every 200 ms until Chrome answers; its answer (Browser, webSocket
    DebuggerUrl…), or EngineError('timeout')."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            return await get_json(port, '/json/version', timeout=2.0)
        except EngineError as e:
            if loop.time() >= deadline:
                raise EngineError(
                    'timeout', f'Chrome did not open its DevTools port {port} in {timeout:g} s'
                ) from e
        await asyncio.sleep(0.2)


async def list_targets(port):
    """Every target Chrome lists at /json/list."""
    targets = await get_json(port, '/json/list')
    if not isinstance(targets, list):
        raise EngineError('engine-down', f'DevTools on port {port} listed no targets')
    return [t for t in targets if isinstance(t, dict)]


def select_target(targets):
    """The page target showing music.apple.com (the first with a debugger URL), or None."""
    for target in targets:
        if (target.get('type') == 'page' and target.get('webSocketDebuggerUrl')
                and str(target.get('url', '')).startswith('https://music.apple.com')):
            return target
    return None


async def find_target(port):
    """The music.apple.com page target on `port`, or None when Chrome shows no such page."""
    return select_target(await list_targets(port))


async def wait_for_target(port, timeout=15.0):
    """The music.apple.com page, polled for: Chrome lists its first page a moment after the
    DevTools port opens. EngineError('timeout') when none appears."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        target = await find_target(port)
        if target is not None:
            return target
        if loop.time() >= deadline:
            raise EngineError('timeout', f'Chrome on port {port} shows no music.apple.com page')
        await asyncio.sleep(0.2)
