#!/usr/bin/env python3
"""Measure the app's startup, page switches and memory on a big demo library.

    scripts/bench.py [--cache DIR] [--size WxH] [--settle MS] [--watch MS] [--runs N]
                     [--profile KEY]

Runs the installed build (meson install -C build, or scripts/run.sh, first) as
scroll_test.py does (scripts/harness.py), on the library in DIR (default
build/demo-big, or $APPLE_MUSIC_CACHE when set), with --demo and --debug, the
settings in a memory backend, animations off and the desktop's icon theme and
font. Make the library first:

    scripts/demo_library.py --cache build/demo-big --albums 3000 --playlists 300 --tracks 40000

Each run is a process of its own (this script again, with --child), --runs of them
(default 3), and a bare Adw.ApplicationWindow is timed the same way for comparison:
the platform's own cost, which no change to the app can remove (GTK's start, the
icon theme, the compositor's first configure, the GL driver's memory). The report
gives each run and the median, against phase 19's targets (content under 1 s,
switches under 100 ms, RSS under 250 MB):

- startup: from the process start (/proc/self/stat, so the interpreter's own start
  counts) to the window mapped, its first frame painted, the library ready, and the
  Albums page's first tile bound and painted (content): Application.mark(), which
  --debug logs too. The Albums page is the one restored (last-page).
- page switches: every root page of the sidebar, plus the first folder and the first
  playlist, shown once (the page built) and again (kept), each timed from the
  selection to the end of the next frame's paint, with --settle ms between them for
  the artwork to arrive, as it would for someone browsing. For Songs, also the time
  until the songs are built and its rows painted. Beside each, the longest frame
  (before-paint to after-paint, on the main thread) in the --watch ms after it
  (default 1000; the next switch waits for it): work a page puts off until after its
  first paint (Home's later shelves, the grid's columns, the Songs prepare) shows
  there, not in the switch time.
- memory: RSS (and the anonymous part of it, the app's own) from /proc/self/status
  after the window is mapped, the library is loaded, and every page has been browsed
  twice, and the peak (VmHWM).
- pages: 20 albums, 5 artists and 5 See All grids opened (window.open_item and
  open_shelf, as a tile or See All does) and popped one after the other, --settle ms
  each: the RSS and anonymous RSS they left behind, after gc.collect() and
  malloc_trim(0), and how many of the pages pushed are still alive (weak references),
  by class. A page that is freed once popped leaves 0 alive.

A frame that does not come (the compositor sends none to a window it does not show:
keep the bench's window visible) is reported as missing after 3 s rather than waited
for. Scrolling is measured by scripts/scroll_test.py.
"""

import argparse
import asyncio
import ctypes
import gc
import json
import logging
import os
import statistics
import subprocess
import sys
import time
import weakref

import harness

root = harness.ROOT

CONTENT_TARGET = 1000  # ms from launch
SWITCH_TARGET = 100  # ms
RSS_TARGET = 250  # MB
FRAME_TIMEOUT = 3000  # ms to wait for a frame before calling it missing
PAGES = (('album', 20), ('artist', 5), ('shelf', 5))  # what the pages step opens and pops

parser = argparse.ArgumentParser()
parser.add_argument('--cache', default=os.environ.get('APPLE_MUSIC_CACHE')
                    or os.path.join(root, 'build', 'demo-big'),
                    help='the library to run on (default build/demo-big)')
parser.add_argument('--size', default='1100x760')
parser.add_argument('--settle', type=int, default=300,
                    help='milliseconds between page switches (default 300)')
parser.add_argument('--watch', type=int, default=1000,
                    help='milliseconds after a switch whose longest frame is reported '
                         '(default 1000)')
parser.add_argument('--runs', type=int, default=3, help='how many runs (default 3)')
parser.add_argument('--profile', metavar='KEY',
                    help="print a cProfile of the Python run during this page's first switch")
parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
parser.add_argument('--bare', action='store_true', help=argparse.SUPPRESS)
args = parser.parse_args()
width, height = (int(n) for n in args.size.split('x'))


def rss_mb(field='VmRSS'):
    with open('/proc/self/status', encoding='ascii') as file:
        for line in file:
            if line.startswith(field + ':'):
                return int(line.split()[1]) / 1024
    return 0.0


def malloc_trim():
    """Hand freed heap back to the system (glibc), so RSS counts what is still in use."""
    try:
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except (OSError, AttributeError):
        pass


def process_age_ms():
    """Milliseconds since this process started, from /proc/self/stat."""
    with open('/proc/self/stat', encoding='ascii') as file:
        fields = file.read().rpartition(')')[2].split()
    started = int(fields[19]) / os.sysconf('SC_CLK_TCK')
    return (time.clock_gettime(time.CLOCK_BOOTTIME) - started) * 1000


# -- the parent: runs, medians -----------------------------------------------------------

def run_child(*extra):
    """This script as a process of its own; its result line, parsed."""
    command = [sys.executable, os.path.abspath(__file__), '--cache', args.cache,
               '--size', args.size, '--settle', str(args.settle), '--watch', str(args.watch),
               *extra]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as error:  # its output is bytes, whatever text= says
        output = (error.stdout or b'').decode(errors='replace')
        sys.stderr.write(output[-3000:] + (error.stderr or b'').decode(errors='replace')[-3000:]
                         + f'bench: no result in {error.timeout:.0f} s\n')
        return None, output
    for line in completed.stdout.splitlines():
        if line.startswith('bench-result: '):
            return json.loads(line[len('bench-result: '):]), completed.stdout
    sys.stderr.write(completed.stdout[-3000:] + completed.stderr[-3000:])
    return None, completed.stdout


def median(values):
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def fmt(value, unit='ms'):
    return '-' if value is None else f'{value:.0f} {unit}'


def verdict(value, target):
    return '' if value is None else ('ok' if value < target else 'OVER')


def parent():
    if not os.path.exists(os.path.join(args.cache, 'library.json')):
        sys.exit(f'bench: no library.json in {args.cache}; see scripts/bench.py --help')
    runs, bares = [], []
    for number in range(args.runs):
        bare, _output = run_child('--bare')
        if bare:
            bares.append(bare)
        extra = ['--child'] + (['--profile', args.profile] if args.profile else [])
        result, output = run_child(*extra)
        if args.profile:
            print(''.join(line + '\n' for line in output.splitlines()
                          if not line.startswith('bench-result: ')), end='')
        if result is None:
            print(f'bench: run {number + 1} failed', flush=True)
            continue
        runs.append(result)
        startup = result['startup']
        print(f'run {number + 1}: first frame {fmt(startup.get("first-frame"))}, '
              f'library ready {fmt(startup.get("library-ready"))}, '
              f'content {fmt(startup.get("albums-painted"))}; '
              f'longest switch {fmt(result["longest_switch"])}; '
              f'RSS after browsing {fmt(result["rss"]["browsed"], "MB")} '
              f'(anon {fmt(result["rss"]["browsed_anon"], "MB")}); '
              f'bare window: first frame {fmt(bare and bare["first_frame"])}, '
              f'RSS {fmt(bare and bare["rss"], "MB")}', flush=True)
    if not runs:
        sys.exit('bench: no run finished')
    first = runs[0]
    print(f'\nbench: {args.cache}: {first["library"]}; window {first["window"]}; '
          f'median of {len(runs)} run(s)')
    stages = ['startup', 'gtk-started', 'activate', 'window-built', 'window-mapped',
              'first-frame', 'library-ready', 'albums-bound', 'albums-painted']
    print('  startup: ' + ', '.join(
        f'{stage} {fmt(median(run["startup"].get(stage) for run in runs))}'
        for stage in stages))
    content = median(run['startup'].get('albums-painted') for run in runs)
    floor = median(bare['first_frame'] for bare in bares)
    print(f'  content painted {fmt(content)} ({verdict(content, CONTENT_TARGET)} the '
          f'{CONTENT_TARGET} ms target); a bare Adw window\'s first frame {fmt(floor)}')
    print('  page switches (selection to the end of the next paint), first visit / again; '
          f'the longest frame in the {args.watch} ms after each:')
    for key in first['order']:
        firsts = median(run['switches'].get(key, [None, None])[0] for run in runs)
        agains = median(run['switches'].get(key, [None, None])[1] for run in runs)
        frame_firsts = median(run['frames'].get(key, [None, None])[0] for run in runs)
        frame_agains = median(run['frames'].get(key, [None, None])[1] for run in runs)
        note = ''
        if key == 'songs':
            note = f'; songs built and painted {fmt(median(run["songs_ready"] for run in runs))}'
        tiles = first['tiles'].get(key)
        if tiles:
            note += f'; {tiles[0]} tiles made, {tiles[2]} in view'
        print(f'    {key:<22} {fmt(firsts):>8} / {fmt(agains):>8};'
              f' frames after {fmt(frame_firsts):>6} / {fmt(frame_agains):>6}{note}')
    longest = median(run['longest_switch'] for run in runs)
    print(f'  longest switch {fmt(longest)} ({verdict(longest, SWITCH_TARGET)} the '
          f'{SWITCH_TARGET} ms target); longest frame after a switch '
          f'{fmt(median(run["longest_frame"] for run in runs))}')
    for name, label in (('mapped', 'window mapped'), ('ready', 'library ready'),
                        ('browsed', 'every page browsed twice'), ('peak', 'peak (VmHWM)')):
        rss = median(run['rss'][name] for run in runs)
        anon = median(run['rss'].get(name + '_anon') for run in runs)
        print(f'  RSS {label}: {fmt(rss, "MB")}' + (f' (anon {fmt(anon, "MB")})' if anon else ''))
    browsed = median(run['rss']['browsed'] for run in runs)
    bare_rss = median(bare['rss'] for bare in bares)
    bare_anon = median(bare['anon'] for bare in bares)
    print(f'  RSS after browsing {verdict(browsed, RSS_TARGET)} the {RSS_TARGET} MB target; '
          f'a bare Adw window: {fmt(bare_rss, "MB")} (anon {fmt(bare_anon, "MB")})')
    print(f'  RSS added by each first visit (run 1): {first["page_rss"]}')
    pages = [run['pages'] for run in runs if run.get('pages')]
    if pages:
        opened = ', '.join(f'{count} {kind}' for kind, count in pages[0]['opened'].items())
        alive = ', '.join(f'{name} {alive} of {pushed}'
                          for name, (alive, pushed) in pages[0]['alive'].items())
        print(f'  pages ({opened} opened and popped): RSS '
              f'{median(page["rss"] for page in pages):+.1f} MB (anon '
              f'{median(page["anon"] for page in pages):+.1f} MB) after gc.collect() and '
              f'malloc_trim(0); still alive (run 1): {alive}')
    else:
        print('  pages: no run finished the step')
    print(f'  garbage collections (run 1): {first["gc"]}')


# -- a bare window: the platform's floor ---------------------------------------------------

def bare():
    import gi

    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, Gio, GLib

    app = Adw.Application(application_id='io.github.jackicus.AppleMusic.BenchBare',
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    result = {}

    def on_activate(app):
        window = Adw.ApplicationWindow(application=app, default_width=width,
                                       default_height=height, resizable=False)
        window.set_content(Adw.StatusPage(title='Bench', icon_name='audio-x-generic-symbolic'))

        def on_map(window):
            clock = window.get_frame_clock()
            handler = []

            def after_paint(clock):
                clock.disconnect(handler[0])
                result['first_frame'] = process_age_ms()
                GLib.timeout_add(500, done)

            handler.append(clock.connect('after-paint', after_paint))

        window.connect('map', on_map)
        window.present()

    def done():
        result['rss'] = rss_mb()
        result['anon'] = rss_mb('RssAnon')
        app.quit()

    app.connect('activate', on_activate)
    GLib.timeout_add_seconds(20, app.quit)
    app.run([])
    print('bench-result: ' + json.dumps(result), flush=True)


# -- a run of the app ----------------------------------------------------------------------

def child():
    os.environ['APPLE_MUSIC_CACHE'] = os.path.abspath(args.cache)

    class Stamp(logging.Filter):
        """Every log line stamped with the RSS then."""

        def filter(self, record):
            record.rss = rss_mb()
            return True

    logging.basicConfig(level=logging.INFO,
                        format='%(relativeCreated)6.0f ms %(rss)4.0f MB %(levelname)s '
                               '%(name)s: %(message)s')
    logging.getLogger().handlers[0].addFilter(Stamp())

    # The desktop's look, as the bare window below has it.
    app = harness.make_app('Bench', stock_look=False, size=(width, height),
                           demo_dir=args.cache)
    from gi.repository import GLib

    from applemusic import main, sections
    from applemusic.sidebar import folder_key, playlist_key

    rss = {}
    results = {}  # (key, 'first' | 'again') -> ms, None for a frame that never came
    page_rss = {}  # key -> RSS added by its first visit, in MB
    page_tiles = {}  # key -> (tiles created, mapped, in view) after its first visit
    collections = []  # (generation, ms) of every garbage collection
    frames = {}  # (key, 'first' | 'again') -> the longest frame after that switch, ms or None
    watch = {}  # the frames watched since the last switch: its key, handlers, longest
    gc_started = {}
    songs = {}

    def on_gc(phase, info):
        if phase == 'start':
            gc_started['at'] = time.perf_counter()
        elif 'at' in gc_started:
            collections.append((info['generation'],
                                (time.perf_counter() - gc_started.pop('at')) * 1000))

    gc.callbacks.append(on_gc)

    def ms_since_start(mark):
        at = app.marks.get(mark)
        return None if at is None else (at - main.PROCESS_START) / 1000

    def on_activate(_app):
        app.settings.set_string('last-page', 'albums')
        GLib.timeout_add(20, wait_for_content)

    def wait_for_content():
        """Until the library is loaded and the Albums page has painted its first tiles."""
        if 'window-mapped' in app.marks and 'mapped' not in rss:
            rss['mapped'], rss['mapped_anon'] = rss_mb(), rss_mb('RssAnon')
        if 'library-ready' in app.marks and 'ready' not in rss:
            rss['ready'], rss['ready_anon'] = rss_mb(), rss_mb('RssAnon')
        if 'albums-painted' not in app.marks or 'library-ready' not in app.marks:
            if process_age_ms() < 60000:
                return GLib.SOURCE_CONTINUE
            print('bench: gave up waiting for the Albums page', flush=True)
            app.quit()
            return GLib.SOURCE_REMOVE
        counts = tile_counts(app.get_active_window().navigation_view.get_visible_page())
        if counts:
            print(f'bench: albums grid after loading: {counts[0]} tiles created, {counts[1]} '
                  f'mapped, {counts[2]} within the viewport', flush=True)
        GLib.timeout_add(args.settle, start_tour)
        return GLib.SOURCE_REMOVE

    def tile_counts(page):
        """(tiles created, mapped, within the viewport) of a grid page's Gtk.GridView, or
        None."""
        grid = getattr(page, 'grid_view', None)
        if grid is None:
            return None
        adjustment = page.scrolled_window.get_vadjustment()
        top = adjustment.get_value()
        bottom = top + adjustment.get_page_size()
        created = mapped = in_view = 0
        child = grid.get_first_child()
        while child is not None:
            created += 1
            if child.get_mapped():
                mapped += 1
                ok, bounds = child.compute_bounds(grid)
                if ok and bounds.get_y() + bounds.get_height() > top and bounds.get_y() < bottom:
                    in_view += 1
            child = child.get_next_sibling()
        return created, mapped, in_view

    def start_watch(key, stage):
        """Time every frame from now until stop_watch(), keeping the longest."""
        clock = app.get_active_window().get_frame_clock()
        began = {}

        def before_paint(_clock):
            began['at'] = time.perf_counter()

        def after_paint(_clock):
            if 'at' in began:
                ms = (time.perf_counter() - began.pop('at')) * 1000
                watch['longest'] = max(watch['longest'] or 0.0, ms)

        # None while no frame came: a page shown again usually draws nothing more.
        watch.update(key=(key, stage), clock=clock, longest=None, handlers=[
            clock.connect('before-paint', before_paint),
            clock.connect('after-paint', after_paint)])

    def stop_watch():
        if watch:
            for handler in watch['handlers']:
                watch['clock'].disconnect(handler)
            frames[watch['key']] = watch['longest']
            watch.clear()

    def page_keys():
        keys = [destination.key for _title, destinations in sections.sidebar_sections()
                for destination in destinations]
        tree = app.library.playlist_tree()
        folder = next((node for node in tree.flat if node.kind == 'folder'), None)
        playlist = next((node for node in tree.flat if node.kind == 'playlist'), None)
        if folder is not None:
            keys.append(folder_key(folder.id))
        if playlist is not None:
            keys.append(playlist_key(playlist.id))
        return keys

    def start_tour():
        keys = page_keys()
        plan = [(key, 'first') for key in keys] + [(key, 'again') for key in keys]
        last_rss = [rss_mb()]

        def visit(position):
            stop_watch()
            if position == len(plan):
                finish(keys)
                return GLib.SOURCE_REMOVE
            key, stage = plan[position]
            window = app.get_active_window()
            profile = None
            if args.profile == key and stage == 'first':
                import cProfile

                profile = cProfile.Profile()
                profile.enable()
            started = time.perf_counter()
            window._select(key)
            clock = window.get_frame_clock()
            state = {}

            def done(ms):
                clock.disconnect(state.pop('handler'))
                timeout = state.pop('timeout')
                if timeout:
                    GLib.source_remove(timeout)
                results[(key, stage)] = ms
                start_watch(key, stage)
                if profile is not None:
                    import pstats

                    profile.disable()
                    print(f'bench: profile of the {key} switch (selection to the end of the '
                          'paint):', flush=True)
                    pstats.Stats(profile, stream=sys.stdout).sort_stats('cumulative') \
                        .print_stats(22)
                def account():
                    # What the first visit added: a page's widgets, and for Songs the
                    # songs built on the way (their rows painted).
                    if stage == 'first':
                        now = rss_mb()
                        page_rss[key] = round(now - last_rss[0], 1)
                        last_rss[0] = now
                        counts = tile_counts(window.navigation_view.get_visible_page())
                        if counts:
                            page_tiles[key] = counts
                    GLib.timeout_add(max(args.settle, args.watch), visit, position + 1)

                if key == 'songs' and stage == 'first' and not app.library.songs_ready:
                    wait_for_songs(started, account)
                else:
                    account()

            def on_timeout():
                state['timeout'] = 0
                print(f'bench: no frame came for {key} ({stage}) in {FRAME_TIMEOUT} ms: '
                      'is the window hidden?', flush=True)
                done(None)
                return GLib.SOURCE_REMOVE

            state['handler'] = clock.connect(
                'after-paint', lambda _clock: done((time.perf_counter() - started) * 1000))
            state['timeout'] = GLib.timeout_add(FRAME_TIMEOUT, on_timeout)
            window.queue_draw()  # a page shown already may have queued nothing
            return GLib.SOURCE_REMOVE

        visit(0)
        return GLib.SOURCE_REMOVE

    def wait_for_songs(started, then):
        """The Songs page's rows built and painted: 'songs-painted' is marked by its first
        row's bind (Application.mark), which counts from the switch."""
        switched = GLib.get_monotonic_time() - int((time.perf_counter() - started) * 1e6)

        def check():
            if 'songs-painted' in app.marks:
                songs['ready'] = (app.marks['songs-painted'] - switched) / 1000
            elif time.perf_counter() - started < 20:
                return GLib.SOURCE_CONTINUE
            then()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(20, check)

    def finish(keys):
        rss['browsed'], rss['browsed_anon'] = rss_mb(), rss_mb('RssAnon')
        rss['peak'] = rss_mb('VmHWM')
        switches = {key: [results.get((key, 'first')), results.get((key, 'again'))]
                    for key in keys}
        after = {key: [frames.get((key, 'first')), frames.get((key, 'again'))]
                 for key in keys}
        library = app.library
        window = app.get_active_window()
        result = {
            'library': f'{library.albums.get_n_items():,} albums, '
                       f'{library.artists.get_n_items():,} artists, '
                       f'{library.playlists.get_n_items():,} playlists, '
                       f'{library.song_count():,} songs',
            'window': f'{window.get_width()}x{window.get_height()}, '
                      f'scale {window.get_scale_factor()}',
            'startup': {name: ms_since_start(name) for name in app.marks},
            'order': keys,
            'switches': switches,
            'longest_switch': max((ms for pair in switches.values() for ms in pair
                                   if ms is not None), default=None),
            'frames': after,
            'longest_frame': max((ms for pair in after.values() for ms in pair
                                  if ms is not None), default=None),
            'songs_ready': songs.get('ready'),
            'rss': rss,
            'page_rss': page_rss,
            'tiles': page_tiles,
            'gc': f'{len(collections)}, {sum(g == 2 for g, _ms in collections)} full, longest '
                  f'{max((ms for _g, ms in collections), default=0):.0f} ms',
        }

        async def pages_then_report():
            try:
                result['pages'] = await pages_step()
            finally:
                print('bench-result: ' + json.dumps(result), flush=True)
                GLib.timeout_add(200, app.quit)

        app.spawn(pages_then_report())

    async def next_paint(window):
        """Until the end of the window's next frame; False when none came in time."""
        future = asyncio.get_running_loop().create_future()
        clock = window.get_frame_clock()
        handler = clock.connect(
            'after-paint', lambda _clock: future.done() or future.set_result(True))
        window.queue_draw()
        try:
            return await asyncio.wait_for(future, FRAME_TIMEOUT / 1000)
        except TimeoutError:
            return False
        finally:
            clock.disconnect(handler)

    def memory():
        gc.collect()
        malloc_trim()
        return rss_mb(), rss_mb('RssAnon')

    async def pages_step():
        """Open and pop albums, artists and See All grids (PAGES) over the Albums page, as a
        tile or See All would; what they left behind (see the module)."""
        window = app.get_active_window()
        library = app.library
        window._select('albums')
        await next_paint(window)
        await asyncio.sleep(args.settle / 1000)
        shelves = [shelf for shelf in library.shelves if shelf.items.get_n_items()]
        stores = {'album': library.albums, 'artist': library.artists}
        plan = []
        for kind, count in PAGES:
            if kind == 'shelf':
                plan += [(kind, shelves[i % len(shelves)]) for i in range(count if shelves else 0)]
            else:
                store = stores[kind]
                plan += [(kind, store.get_item(i))
                         for i in range(min(count, store.get_n_items()))]
        rss_before, anon_before = memory()
        pushed = {}  # page class -> weak references to the pages pushed
        for kind, target in plan:
            if kind == 'shelf':
                window.open_shelf(target)
            else:
                window.open_item(target)
            page = window.navigation_view.get_visible_page()
            pushed.setdefault(type(page).__name__, []).append(weakref.ref(page))
            del page
            await next_paint(window)
            await asyncio.sleep(args.settle / 1000)  # rows bound, artwork decoded
            window.navigation_view.pop()
            await next_paint(window)
        await asyncio.sleep(0.2)
        rss_after, anon_after = memory()
        opened = {}
        for kind, _target in plan:
            name = {'album': 'albums', 'artist': 'artists', 'shelf': 'See All'}[kind]
            opened[name] = opened.get(name, 0) + 1
        return {
            'opened': opened,
            'rss': round(rss_after - rss_before, 1),
            'anon': round(anon_after - anon_before, 1),
            'alive': {name: [sum(ref() is not None for ref in refs), len(refs)]
                      for name, refs in pushed.items()},
        }

    app.connect('activate', on_activate)
    GLib.timeout_add_seconds(180, app.quit)  # whatever happens
    harness.run_app(app, '--debug')


if args.bare:
    bare()
elif args.child:
    child()
else:
    parent()
