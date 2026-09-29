#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Drive the engine without the GUI: the app's own Engine, on the GLib-backed asyncio loop.

    scripts/am.py status                  who holds the Chrome profile, and what the bridge says
    scripts/am.py eval 'MusicKit.getInstance().isAuthorized'
                                          a JS expression in the page (promises awaited), as JSON
    scripts/am.py now-playing             the bridge's nowPlaying()
    scripts/am.py events                  bridge events, one JSON line each, until Ctrl+C

Two modes. By default each command starts a Chrome of its own on the app's profile, over the
DevTools pipe as the app does (headless unless --visible), and stops it when it is done: a
pipe Chrome cannot outlive the command, so there is no start or stop. That needs the profile
to itself: while the app's Chrome holds it (its SingletonLock), `status` says so and the other
commands refuse. To look inside the running app instead, start the app with
APPLE_MUSIC_DEBUG_PORT=N, which also opens its DevTools on 127.0.0.1:N (and lets any local
program drive the signed-in session while it is set), and use --attach N (or --attach alone,
which reads APPLE_MUSIC_DEBUG_PORT).

The profile is the release build's ($XDG_DATA_HOME/apple-music/chrome), the .Devel build's
with --devel, or APPLE_MUSIC_PROFILE. Every command prints one JSON value and exits 0, or
{"error": code, "message": …} and exits 1. Nothing here signs in.
"""

import argparse
import asyncio
import importlib.util
import json
import logging
import pathlib
import signal
import sys
import warnings

ROOT = pathlib.Path(__file__).resolve().parent.parent

# src/ is laid out for installation and becomes the applemusic package only there; load it under
# that name here, as tests/__init__.py does.
if 'applemusic' not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        'applemusic', ROOT / 'src' / '__init__.py', submodule_search_locations=[str(ROOT / 'src')])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules['applemusic'] = _module
    _spec.loader.exec_module(_module)

from applemusic.backend import chrome, config  # noqa: E402
from applemusic.backend.client import EVENT_PREFIX, attach_devtools  # noqa: E402
from applemusic.backend.errors import EngineError  # noqa: E402

log = logging.getLogger('am')

ATTACH_FROM_ENV = 'env'   # --attach with no port: APPLE_MUSIC_DEBUG_PORT's
STOP_GRACE = 3.0          # the command's own Chrome: SIGTERM to SIGKILL


def attach_port(text):
    """--attach's value: a port, or ATTACH_FROM_ENV (parse() writes a bare --attach so)."""
    if text == ATTACH_FROM_ENV:
        return text
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError(f'not a port: {text}')
    return port


class Session:
    """The page to drive: the running app's through its debug port (attach), or a Chrome of
    the command's own on the profile, stopped at the end."""

    def __init__(self, args):
        self.args = args
        self.engine = None
        self.client = None

    async def __aenter__(self):
        args = self.args
        if args.attach:
            self.client = await attach_devtools(args.attach, timeout=args.timeout, wait=3)
            try:
                await self.client.ensure_bridge(timeout=args.wait)
            except EngineError as e:
                if e.code == 'engine-down':
                    raise
                log.warning('bridge not ready: %s', e)
            return self.client
        owner = await asyncio.to_thread(chrome.profile_owner, config.profile_dir())
        if owner is not None:
            raise EngineError(
                'usage', f'the app is running (Chrome {owner} holds the profile): start it '
                'with APPLE_MUSIC_DEBUG_PORT=N and use --attach N')
        from applemusic.engine import Engine  # Gio and GObject: only for a Chrome of our own

        self.engine = Engine(config.profile_dir(), args.browser)
        self.engine.prefer_headless = not args.visible
        await self.engine.start()
        self.client = self.engine.client
        return self.client

    async def __aexit__(self, *exc):
        if self.engine is not None:
            await self.engine.stop(grace=STOP_GRACE)
        elif self.client is not None:
            await self.client.close()


async def cmd_status(args):
    profile = config.profile_dir()
    if args.attach:
        version = await chrome.get_json(args.attach, '/json/version', timeout=2)
        result = {'mode': 'attach', 'port': args.attach, 'browser': version.get('Browser')}
    else:
        owner = await asyncio.to_thread(chrome.profile_owner, profile)
        result = {'mode': 'own', 'profile': str(profile), 'held_by': owner}
        if owner is not None:
            result['bridge'] = None
            result['hint'] = ('the app is running: start it with APPLE_MUSIC_DEBUG_PORT=N and '
                              'use --attach N to look inside')
            return result
    async with Session(args) as client:
        try:
            result['bridge'] = await client.bridge('status', timeout=args.timeout)
        except EngineError as e:
            result['bridge'] = {'error': e.code, 'message': e.message}
    return result


async def cmd_eval(args):
    async with Session(args) as client:
        return await client.evaluate(' '.join(args.js), await_promise=not args.no_await,
                                     timeout=args.timeout)


async def cmd_now_playing(args):
    async with Session(args) as client:
        return await client.bridge('nowPlaying', timeout=args.timeout)


async def cmd_events(args):
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    # Ctrl+C and SIGTERM end it cleanly, however the process inherited the signals (a shell
    # starts a background job with SIGINT ignored, and Python then leaves it so).
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    count = 0

    def show(name, data):
        nonlocal count
        count += 1
        print(json.dumps({'name': name[len(EVENT_PREFIX):], 'data': data}), flush=True)

    async with Session(args) as client:
        client.on(EVENT_PREFIX + '*', show)
        await client.subscribe()
        print(json.dumps({'subscribed': True, 'mode': 'attach' if args.attach else 'own'}),
              file=sys.stderr, flush=True)
        lost = loop.create_task(client.wait_closed())
        stop = loop.create_task(stopped.wait())
        await asyncio.wait([lost, stop], return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if lost.done():
            raise EngineError('engine-down', 'the connection to Chrome was lost')
    return {'events': count}


COMMANDS = {
    'status': cmd_status,
    'eval': cmd_eval,
    'now-playing': cmd_now_playing,
    'events': cmd_events,
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog='am.py', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--debug', action='store_true', help='log at DEBUG on stderr')
    parser.add_argument('--attach', type=attach_port, metavar='[PORT]',
                        help='drive the running app through its DevTools port (the app '
                             "started with APPLE_MUSIC_DEBUG_PORT; alone, that variable's "
                             'port) instead of a Chrome of our own')
    parser.add_argument('--devel', action='store_true',
                        help="the .Devel build's Chrome profile (chrome-devel)")
    parser.add_argument('--visible', action='store_true',
                        help='our own Chrome as a window instead of headless')
    parser.add_argument('--browser', metavar='CMD', help='the browser command to try first')
    parser.add_argument('--timeout', type=float, default=30.0, metavar='S',
                        help='how long a call may take')
    sub = parser.add_subparsers(dest='command', required=True, metavar='COMMAND')
    sub.add_parser('status', help="who holds the profile, and the bridge's status")
    ev = sub.add_parser('eval', help='evaluate JavaScript in the page')
    ev.add_argument('js', nargs='+', help='the expression')
    ev.add_argument('--no-await', action='store_true', help='do not await a promise')
    ev.add_argument('--wait', type=float, default=5.0, metavar='S',
                    help='(with --attach) how long to wait for MusicKit before evaluating anyway')
    np = sub.add_parser('now-playing', help="the bridge's nowPlaying()")
    np.add_argument('--wait', type=float, default=15.0, metavar='S')
    sub.add_parser('events', help='print bridge events until Ctrl+C')
    return parser


def parse(argv=None):
    """The arguments, --attach resolved to a port (exit 2 when it names none). A bare --attach
    (followed by no number) takes APPLE_MUSIC_DEBUG_PORT's: argparse's optional value would
    swallow the command after it."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(argv):
        if arg == '--':
            break
        if arg == '--attach' and not (index + 1 < len(argv) and argv[index + 1].isdigit()):
            argv[index] = f'--attach={ATTACH_FROM_ENV}'
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.attach == ATTACH_FROM_ENV:
        args.attach = config.debug_port()
        if args.attach is None:
            parser.error('--attach needs a port, or APPLE_MUSIC_DEBUG_PORT set to the one the '
                         'app was started with')
    args.wait = getattr(args, 'wait', 15.0)
    return args


def main(argv=None):
    args = parse(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='am.py: %(levelname)s %(name)s: %(message)s', stream=sys.stderr)
    if args.devel:
        config.set_build_profile(config.DEVELOPMENT)
    # The Engine spawns and waits on Chrome through Gio, which needs asyncio on the GLib loop;
    # Python 3.14 deprecates the policy PyGObject reaches it through (see main.py).
    warnings.filterwarnings('ignore', r"'asyncio\.\w*policy\w*' is deprecated", DeprecationWarning)
    from gi.events import GLibEventLoop

    try:
        result = asyncio.run(COMMANDS[args.command](args), loop_factory=GLibEventLoop)
    except EngineError as e:
        print(json.dumps({'error': e.code, 'message': e.message}))
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
