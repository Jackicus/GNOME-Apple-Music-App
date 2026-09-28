#!/usr/bin/env python3
"""Measure the app's startup, page switches and memory on a big demo library.

    scripts/bench.py [--cache DIR] [--size WxH] [--settle MS] [--runs N] [--profile KEY]

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
  until the songs are built and its rows painted.
- memory: RSS (and the anonymous part of it, the app's own) from /proc/self/status
  after the window is mapped, the library is loaded, and every page has been browsed
  twice, and the peak (VmHWM).

A frame that does not come (the compositor sends none to a window it does not show:
keep the bench's window visible) is reported as missing after 3 s rather than waited
for. Scrolling is measured by scripts/scroll_test.py.
"""

import argparse
import gc
import json
import logging
import os
import statistics
import subprocess
import sys
import time

import harness

root = harness.ROOT

CONTENT_TARGET = 1000  # ms from launch
SWITCH_TARGET = 100  # ms
RSS_TARGET = 250  # MB
FRAME_TIMEOUT = 3000  # ms to wait for a frame before calling it missing

parser = argparse.ArgumentParser()
parser.add_argument('--cache', default=os.environ.get('APPLE_MUSIC_CACHE')
                    or os.path.join(root, 'build', 'demo-big'),
                    help='the library to run on (default build/demo-big)')
parser.add_argument('--size', default='1100x760')
parser.add_argument('--settle', type=int, default=300,
                    help='milliseconds between page switches (default 300)')
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
               '--size', args.size, '--settle', str(args.settle), *extra]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
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
    print('  page switches (selection to the end of the next paint), first visit / again:')
    for key in first['order']:
        firsts = median(run['switches'].get(key, [None, None])[0] for run in runs)
        agains = median(run['switches'].get(key, [None, None])[1] for run in runs)
        note = ''
        if key == 'songs':
            note = f'; songs built and painted {fmt(median(run["songs_ready"] for run in runs))}'
        tiles = first['tiles'].get(key)
        if tiles:
            note += f'; {tiles[0]} tiles made, {tiles[2]} in view'
        print(f'    {key:<22} {fmt(firsts):>8} / {fmt(agains):>8}{note}')
    longest = median(run['longest_switch'] for run in runs)
    print(f'  longest switch {fmt(longest)} ({verdict(longest, SWITCH_TARGET)} the '
          f'{SWITCH_TARGET} ms target)')
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
                    GLib.timeout_add(args.settle, visit, position + 1)

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
            'songs_ready': songs.get('ready'),
            'rss': rss,
            'page_rss': page_rss,
            'tiles': page_tiles,
            'gc': f'{len(collections)}, {sum(g == 2 for g, _ms in collections)} full, longest '
                  f'{max((ms for _g, ms in collections), default=0):.0f} ms',
        }
        print('bench-result: ' + json.dumps(result), flush=True)
        GLib.timeout_add(200, app.quit)

    app.connect('activate', on_activate)
    GLib.timeout_add_seconds(180, app.quit)  # whatever happens
    harness.run_app(app, '--debug')


if args.bare:
    bare()
elif args.child:
    child()
else:
    parent()
