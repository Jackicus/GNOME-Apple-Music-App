#!/usr/bin/env python3
"""Drive the engine without the GUI: the backend layer on plain asyncio.

    scripts/am.py status                  is Chrome up, and what the bridge says
    scripts/am.py start [--visible]       start Chrome (headless unless --visible) on the app's
                                          profile and port, inject the bridge; reuses one running
    scripts/am.py stop                    SIGTERM, 5 s, SIGKILL; forgets it
    scripts/am.py eval 'MusicKit.getInstance().isAuthorized'
                                          a JS expression in the page (promises awaited), as JSON
    scripts/am.py now-playing             the bridge's nowPlaying()
    scripts/am.py events                  bridge events, one JSON line each, until Ctrl+C

Same configuration as the app: port 9228 and $XDG_DATA_HOME/apple-music/chrome unless
APPLE_MUSIC_PORT and APPLE_MUSIC_PROFILE say otherwise (the .Devel build's are 9229 and
chrome-devel: APPLE_MUSIC_PORT=9229 APPLE_MUSIC_PROFILE=~/.local/share/apple-music/chrome-devel).
Every command prints one JSON value and exits 0, or {"error": code, "message": …} and exits 1.
Nothing here signs in: `start --visible` shows the page for that, the app's sign-in flow does it.
"""

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import pathlib
import signal
import subprocess
import sys

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
from applemusic.backend.client import connect_page, CDPClient  # noqa: E402
from applemusic.backend.errors import EngineError  # noqa: E402

log = logging.getLogger('am')

DEVTOOLS_TIMEOUT = 20.0   # Chrome opening its port
BRIDGE_WAIT = 15.0        # a fresh page loading MusicKit
QUICK_WAIT = 5.0          # a page that should be ready already
STOP_GRACE = 5.0


def settings():
    """(profile, port, state file) as configured."""
    profile = config.profile_dir()
    return profile, config.port(), config.state_file(profile)


async def stop_chrome(state, path, proc=None):
    """End the Chrome `state` describes: SIGTERM, STOP_GRACE seconds, SIGKILL. `proc` is the
    Popen when this process started it (so it is reaped)."""
    if state is not None and state.alive:
        log.info('stopping Chrome %d', state.pid)
        try:
            os.kill(state.pid, signal.SIGTERM)
        except OSError:
            pass
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_GRACE
        while state.alive and loop.time() < deadline:
            await asyncio.sleep(0.1)
        if state.alive:
            log.warning('Chrome %d ignored SIGTERM for %g s; killing it', state.pid, STOP_GRACE)
            try:
                os.kill(state.pid, signal.SIGKILL)
            except OSError:
                pass
            while state.alive:
                await asyncio.sleep(0.1)
    if proc is not None:
        await asyncio.to_thread(proc.wait)
    chrome.EngineState.remove(path)


async def close_browser(port):
    """Browser.close on a Chrome nothing here started (no state file), waiting for its port to
    go quiet. False when nothing answers on the port."""
    try:
        version = await chrome.get_json(port, '/json/version', timeout=2)
    except EngineError:
        return False
    ws_url = version.get('webSocketDebuggerUrl')
    if not ws_url:
        return False
    client = CDPClient(timeout=5)
    await client.connect(ws_url, page=False)
    try:
        await client.call('Browser.close')
    except EngineError as e:
        if e.code != 'engine-down':  # the answer may not make it out before Chrome is gone
            raise
    finally:
        await client.close()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STOP_GRACE
    while loop.time() < deadline:
        try:
            await chrome.get_json(port, '/json/version', timeout=1)
        except EngineError:
            return True
        await asyncio.sleep(0.2)
    return True


async def bridge_status(port, timeout=QUICK_WAIT):
    """The bridge's status() from the page on `port`, connecting for it."""
    client = await connect_page(port, wait=timeout)
    try:
        await client.ensure_bridge(timeout=timeout)
        return await client.bridge('status')
    finally:
        await client.close()


async def cmd_status(args):
    profile, port, path = settings()
    state = chrome.EngineState.load(path)
    running = state is not None and state.alive
    result = {
        'running': running,
        'pid': state.pid if running else None,
        'port': port,
        'headless': state.headless if running else None,
        'profile': str(profile),
        'state_file': str(path),
        'devtools': None,
        'bridge': None,
    }
    try:
        version = await chrome.get_json(port, '/json/version', timeout=2)
        result['devtools'] = version.get('Browser')
    except EngineError as e:
        log.debug('no DevTools: %s', e)
        return result
    try:
        result['bridge'] = await bridge_status(port)
    except EngineError as e:
        result['bridge'] = {'error': e.code, 'message': e.message}
    return result


async def cmd_start(args):
    profile, port, path = settings()
    headless = not args.visible
    state = chrome.EngineState.load(path)
    if state is not None and state.alive:
        if state.headless == headless:
            log.info('Chrome %d is already running; reusing it', state.pid)
            status = await bridge_status(port)
            return dict(state.to_dict(), running=True, reused=True, **status)
        log.info('Chrome %d is %s; restarting it %s', state.pid,
                 'headless' if state.headless else 'visible', 'headless' if headless else 'visible')
        await stop_chrome(state, path)

    binary = chrome.find_chrome(args.browser)
    if binary is None:
        raise EngineError('engine-down', 'Google Chrome was not found (google-chrome-stable, '
                          'google-chrome or /opt/google/chrome/chrome; --browser names another)')
    profile.mkdir(parents=True, exist_ok=True)
    argv = chrome.chrome_args(binary, profile, port, headless)
    log.debug('exec %s', ' '.join(argv))
    # Not asyncio.create_subprocess_exec: its transport kills a child still running when it
    # is garbage-collected, which is as this command exits, leaving Chrome to run on. A plain
    # Popen in its own session lets go of it (the app spawns with Gio.Subprocess instead).
    try:
        proc = await asyncio.to_thread(
            subprocess.Popen, argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        raise EngineError('engine-down', f'could not start {binary}: {e}') from e
    state = chrome.EngineState(proc.pid, port, headless, profile)
    state.save(path)
    log.info('Chrome %d started %s on port %d', proc.pid, 'headless' if headless else 'visible',
             port)
    try:
        version = await chrome.wait_for_devtools(port, timeout=DEVTOOLS_TIMEOUT)
        status = await bridge_status(port, timeout=BRIDGE_WAIT)
    except EngineError:
        await stop_chrome(state, path, proc)
        raise
    return dict(state.to_dict(), running=True, reused=False, browser=version.get('Browser'),
                **status)


async def cmd_stop(args):
    profile, port, path = settings()
    state = chrome.EngineState.load(path)
    if state is not None and state.alive:
        await stop_chrome(state, path)
        return {'running': False, 'stopped': state.pid, 'port': port}
    chrome.EngineState.remove(path)
    if await close_browser(port):
        return {'running': False, 'stopped': 'browser', 'port': port}
    return {'running': False, 'stopped': None, 'port': port}


async def with_page(args, coro):
    """Run `coro(client)` on the page, the bridge in it when the page is ready."""
    profile, port, path = settings()
    client = await connect_page(port, wait=3)
    try:
        try:
            await client.ensure_bridge(timeout=args.wait)
        except EngineError as e:
            if e.code == 'engine-down':
                raise
            log.warning('bridge not ready: %s', e)
        return await coro(client)
    finally:
        await client.close()


async def cmd_eval(args):
    js = ' '.join(args.js)

    async def run(client):
        return await client.evaluate(js, await_promise=not args.no_await, timeout=args.timeout)
    return await with_page(args, run)


async def cmd_now_playing(args):
    return await with_page(args, lambda client: client.bridge('nowPlaying'))


async def cmd_events(args):
    profile, port, path = settings()
    client = await connect_page(port, wait=3)
    # Ctrl+C and SIGTERM end it cleanly, however the process inherited the signals (a shell
    # starts a background job with SIGINT ignored, and Python then leaves it so).
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    count = 0
    try:
        def show(name, data):
            nonlocal count
            count += 1
            print(json.dumps({'name': name[len('am:'):], 'data': data}), flush=True)
        client.on('am:*', show)
        await client.subscribe()
        print(json.dumps({'subscribed': True, 'port': port}), file=sys.stderr, flush=True)
        lost = loop.create_task(client.wait_closed())
        stop = loop.create_task(stopped.wait())
        await asyncio.wait([lost, stop], return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if lost.done():
            raise EngineError('engine-down', 'the connection to Chrome was lost')
        return {'events': count}
    finally:
        await client.close()


COMMANDS = {
    'status': cmd_status,
    'start': cmd_start,
    'stop': cmd_stop,
    'eval': cmd_eval,
    'now-playing': cmd_now_playing,
    'events': cmd_events,
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog='am.py', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--debug', action='store_true', help='log at DEBUG on stderr')
    sub = parser.add_subparsers(dest='command', required=True, metavar='COMMAND')
    sub.add_parser('status', help='is the engine running')
    start = sub.add_parser('start', help='start Chrome and inject the bridge')
    start.add_argument('--visible', action='store_true', help='a window instead of headless')
    start.add_argument('--browser', metavar='CMD', help='the browser command to try first')
    sub.add_parser('stop', help='stop Chrome')
    ev = sub.add_parser('eval', help='evaluate JavaScript in the page')
    ev.add_argument('js', nargs='+', help='the expression')
    ev.add_argument('--no-await', action='store_true', help='do not await a promise')
    ev.add_argument('--timeout', type=float, default=30.0, metavar='S')
    ev.add_argument('--wait', type=float, default=5.0, metavar='S',
                    help='how long to wait for MusicKit before evaluating anyway')
    np = sub.add_parser('now-playing', help="the bridge's nowPlaying()")
    np.add_argument('--wait', type=float, default=15.0, metavar='S')
    sub.add_parser('events', help='print bridge events until Ctrl+C')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='am.py: %(levelname)s %(name)s: %(message)s', stream=sys.stderr)
    try:
        result = asyncio.run(COMMANDS[args.command](args))
    except EngineError as e:
        print(json.dumps({'error': e.code, 'message': e.message}))
        return 1
    except KeyboardInterrupt:  # before the signal handler was in place
        return 130
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
