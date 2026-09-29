# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Finding and describing the Chrome the engine runs in.

Nothing here starts a process: the Engine spawns Chrome with Gio.Subprocess from the command
line chrome_args() builds, which has Chrome speak the DevTools protocol over a pipe on its file
descriptors 3 and 4 (--remote-debugging-pipe) rather than on a port any local program could
reach. chrome_environment() is what Chrome's environment gets over the app's: the desktop's
session bus (config.host_session_bus()) when the app runs on another, since the keyring that
encrypts the profile's cookies answers there. profile_used_keyring() reads whether a profile's
`Local State` records that key coming from the OS keyring: a Chrome started on such a profile
without reaching the keyring encrypts with a fallback key and deletes the cookies it cannot
decrypt, the sign-in among them, so the Engine refuses to start one. select_page() picks the
music.apple.com page from Chrome's targets. get_json() reads a DevTools port's /json, for the
developer attach only (APPLE_MUSIC_DEBUG_PORT, scripts/am.py --attach); its HTTP is urllib in
a thread, so nothing blocks the loop.

Inside a Flatpak sandbox (/.flatpak-info exists; the development manifest only) Chrome is the
host's: find_chrome() asks the host's shell for it and chrome_args() runs it through
`flatpak-spawn --host`, forwarding the pipe's descriptors 3 and 4 (the manifest's
--talk-name=org.freedesktop.Flatpak). flatpak-spawn relays SIGTERM to Chrome, and --watch-bus
ends Chrome when flatpak-spawn itself goes (a SIGKILL, the sandbox closing). The profile (under
~/.var/app) is the same path on both sides. Untested: this machine has no Flatpak runtime.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import urllib.parse
from pathlib import Path

from .errors import EngineError

log = logging.getLogger(__name__)

# The real Google Chrome only: it ships Widevine, which the streams need; Chromium does not.
CANDIDATES = ('google-chrome-stable', 'google-chrome', '/opt/google/chrome/chrome')
START_URL = 'https://music.apple.com/'
DEVTOOLS_HOST = '127.0.0.1'

# Chrome's lock on its profile: a symlink to "<hostname>-<pid>" of the browser holding it.
SINGLETON_LOCK = 'SingletonLock'
# Chrome's profile-wide preferences (JSON), among them os_crypt: how its encryption key was got.
LOCAL_STATE = 'Local State'
# The Secret Service (GNOME Keyring, KWallet): where Chrome keeps its encryption key on Linux.
SECRETS_NAME = 'org.freedesktop.secrets'

FLATPAK_INFO = '/.flatpak-info'
# How a sandboxed app runs a host command; --watch-bus ends it when flatpak-spawn ends.
HOST_SPAWN = ('flatpak-spawn', '--host', '--watch-bus')
# The DevTools pipe's descriptors, passed on to the host's Chrome.
HOST_PIPE = ('--forward-fd=3', '--forward-fd=4')
# The host's shell prints the first of its arguments that resolves to an executable.
HOST_LOOKUP = ('for name do p=$(command -v "$name") && [ -x "$p" ] '
               '&& { printf "%s\\n" "$p"; exit 0; }; done; exit 1')


def in_flatpak():
    """Whether this process runs in a Flatpak sandbox, where Chrome is the host's."""
    return os.path.exists(FLATPAK_INFO)


def find_chrome(command=None, path=None, host=None):
    """The executable for `command` (the configured browser), else the first of CANDIDATES
    on `path` (default $PATH); None when there is no Chrome. A configured command that is not
    found is logged before the candidates are tried. With `host` (by default when
    in_flatpak()) the names are resolved on the host instead (find_host_chrome). Blocking in
    the sandbox: call it in a thread."""
    if host is None:
        host = in_flatpak()
    if host:
        return find_host_chrome([name for name in (command, *CANDIDATES) if name])
    if command:
        found = shutil.which(command, path=path)
        if found:
            return found
        log.warning("the browser command %r was not found; trying Google Chrome's usual names",
                    command)
    for name in CANDIDATES:
        found = shutil.which(name, path=path)
        if found:
            return found
    return None


def find_host_chrome(names, run=subprocess.run):
    """The host's path for the first of `names` it can execute, asked of the host's shell
    through flatpak-spawn; None when it has none, or flatpak-spawn fails."""
    argv = [*HOST_SPAWN[:2], 'sh', '-c', HOST_LOOKUP, 'sh', *names]
    try:
        result = run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning('could not look for Chrome on the host: %s', e)
        return None
    lines = (result.stdout or '').splitlines()
    if result.returncode != 0 or not lines:
        return None
    return lines[0]


def chrome_args(binary, profile, headless=True, debug_port=None, host=None):
    """The argv that runs `binary` on `profile` showing music.apple.com, with DevTools on the
    pipe of its descriptors 3 (commands in) and 4 (answers and events out); headless (no
    window, audio still out) or a visible window for sign-in. `debug_port` also opens DevTools
    on 127.0.0.1:`debug_port`, for a developer to attach (APPLE_MUSIC_DEBUG_PORT): any local
    program can then drive the signed-in session. With `host` (by default when in_flatpak())
    it runs on the host through flatpak-spawn."""
    if host is None:
        host = in_flatpak()
    args = [
        str(binary),
        f'--user-data-dir={profile}',
        '--remote-debugging-pipe',
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
    if debug_port is not None:
        args[3:3] = [f'--remote-debugging-port={int(debug_port)}',
                     f'--remote-debugging-address={DEVTOOLS_HOST}']
    if headless:
        args.append('--headless=new')
        args.append(START_URL)
    else:
        # The sign-in flow shows an app window of the page alone, without tabs or an address
        # bar.
        args.append(f'--app={START_URL}')
    if host:
        args[:0] = [*HOST_SPAWN, *HOST_PIPE]
    return args


def chrome_environment(host_bus=None, host=None):
    """The variables set in Chrome's environment over this process's: DBUS_SESSION_BUS_ADDRESS
    when `host_bus` names the session bus Chrome is to use (config.host_session_bus(): the
    desktop's, while the app runs on a private one), so that it reaches the keyring its
    profile is encrypted with. Nothing with `host` (by default when in_flatpak()): the host's
    Chrome has the host's session already."""
    if host is None:
        host = in_flatpak()
    if host_bus and not host:
        return {'DBUS_SESSION_BUS_ADDRESS': host_bus}
    return {}


def profile_used_keyring(profile):
    """Whether `profile`'s Local State records that Chrome's encryption key came from the OS
    keyring: `os_crypt.<provider>.prev_init_success` is true (Chrome 154's provider is
    `portal`; any provider recorded the same way counts). Such a profile's cookies, the
    sign-in among them, are encrypted with that key. False for a profile without the file
    (never run), one that records nothing, or one that cannot be read. Blocking (a file
    read): call it in a thread."""
    try:
        state = json.loads((Path(profile) / LOCAL_STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    os_crypt = state.get('os_crypt') if isinstance(state, dict) else None
    if not isinstance(os_crypt, dict):
        return False
    return any(isinstance(provider, dict) and provider.get('prev_init_success') is True
               for provider in os_crypt.values())


def describe_argv(argv):
    """argv as one line for the log, the profile's path left out (a home directory in a bug
    report)."""
    return ' '.join('--user-data-dir=<profile>' if arg.startswith('--user-data-dir=') else arg
                    for arg in argv)


def is_music_url(url):
    """Whether `url` is a music.apple.com page (the host itself, over https)."""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return False
    return parts.scheme == 'https' and parts.hostname == 'music.apple.com'


def select_page(targets):
    """The first page target (Target.getTargets' TargetInfo dicts) showing music.apple.com,
    or None."""
    for target in targets:
        if (target.get('type') == 'page' and target.get('targetId')
                and is_music_url(target.get('url', ''))):
            return target
    return None


def cmdline_names_profile(cmdline, profile):
    """Whether a /proc/<pid>/cmdline (bytes) is a Chrome browser process on exactly `profile`:
    `--user-data-dir=<profile>` as a whole argument, and no `--type=` (a renderer, zygote or
    other helper, which carry the profile too). Chrome rewrites its command line into one
    space-joined string, so an argument ends at a space, a NUL or the end: splitting on NULs
    alone would never match the real Chrome, and a prefix match would take .../chrome-devel
    for .../chrome."""
    if re.search(rb'(?:^|[ \0])--type=', cmdline):
        return False
    flag = re.escape(os.fsencode(f'--user-data-dir={profile}'))
    return re.search(rb'(?:^|[ \0])' + flag + rb'(?:[ \0]|$)', cmdline) is not None


def pid_alive(pid, profile=None):
    """Whether `pid` is a live process, and (on a system with /proc, when `profile` is given)
    a Chrome browser process on that profile rather than whatever reused the number."""
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
    return cmdline_names_profile(cmdline, profile)


def profile_owner(profile):
    """The pid of the Chrome that holds `profile`, read from Chrome's own lock (SingletonLock,
    a symlink to "<hostname>-<pid>"), when that is this host and a live Chrome browser process
    on exactly this profile; None otherwise, and always in a Flatpak sandbox (the lock names a
    host pid). Blocking (a readlink and /proc reads): call it in a thread."""
    if in_flatpak():
        return None
    try:
        target = os.readlink(Path(profile) / SINGLETON_LOCK)
    except OSError:
        return None
    host, _, pid = target.rpartition('-')
    if host != socket.gethostname() or not pid.isdigit():
        return None
    pid = int(pid)
    try:
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return None
    return pid if cmdline and cmdline_names_profile(cmdline, profile) else None


def _get_json(url, timeout):
    """GET `url` and parse the body as JSON; in a thread, so it may block. No proxy: urllib
    would send even 127.0.0.1 through $http_proxy. urllib is imported here, when the engine
    first starts: it (and http.client) cost 10 ms of the app's startup otherwise."""
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={'User-Agent': 'AppleMusicGNOME/1.0'})
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def devtools_url(port, path):
    return f'http://{DEVTOOLS_HOST}:{port}{path}'


async def get_json(port, path, timeout=5.0):
    """Chrome's /json answer at `path` on `port`. EngineError('engine-down') when nothing, or
    something that is not DevTools, answers; ('timeout') when it is slow."""
    import http.client  # loaded by urllib.request in _get_json anyway

    try:
        return await asyncio.to_thread(_get_json, devtools_url(port, path), timeout)
    except TimeoutError as e:
        raise EngineError('timeout', f'DevTools on port {port} did not answer') from e
    except http.client.HTTPException as e:  # not HTTP (BadStatusLine), a body cut short
        raise EngineError('engine-down',
                          f'no DevTools on port {port}: {type(e).__name__}') from e
    except (OSError, ValueError) as e:  # urllib.error.URLError is an OSError
        reason = getattr(e, 'reason', e)
        if isinstance(reason, TimeoutError):
            raise EngineError('timeout', f'DevTools on port {port} did not answer') from e
        raise EngineError('engine-down', f'no DevTools on port {port}: {reason}') from e
