#!/usr/bin/env python3
"""Scroll a page of the real window from top to bottom and report dropped frames.

    scripts/scroll_test.py [--page KEY] [--speed PX_PER_S] [--size WxH] [--light]

Runs the installed build (scripts/run.sh or meson install -C build first) on the
demo library, as screenshot.py --demo does: build/demo, or $APPLE_MUSIC_CACHE
when set, which is how a big library is measured:

    scripts/demo_library.py --cache build/demo-2000 --albums 2000
    APPLE_MUSIC_CACHE=build/demo-2000 scripts/scroll_test.py --page albums

Once the library has loaded, the page's scrolled window is moved at a steady
speed on every frame. Two things are reported. The app's own work per frame,
timed on the main thread from the frame clock's before-paint to the end of its
paint (tick callbacks and the binds they cause, layout, snapshot and render):
a frame whose work overran the monitor's refresh interval, or 16.7 ms, would
have been dropped at that rate. And the gaps between the frames the compositor
actually asked for, which also depend on the compositor and on what else the
machine is doing, so they vary from run to run. Also counts the artwork decoded
on the way. Settings go to a memory backend.
"""

import argparse
import gettext
import os
import statistics
import subprocess
import sys
import time

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
prefix = os.path.join(root, 'build', 'install')
pkgdatadir = os.path.join(prefix, 'share', 'apple-music')

parser = argparse.ArgumentParser()
parser.add_argument('--page', default='albums')
parser.add_argument('--speed', type=float, default=4000, help='pixels a second (default 4000)')
parser.add_argument('--size', default='1100x760')
parser.add_argument('--light', action='store_true')
args = parser.parse_args()
width, height = (int(n) for n in args.size.split('x'))

demo_dir = os.path.join(root, 'build', 'demo')
if not os.environ.get('APPLE_MUSIC_CACHE') and not os.path.exists(
        os.path.join(demo_dir, 'library.json')):
    subprocess.run([sys.executable, os.path.join(root, 'scripts', 'demo_library.py'),
                    '--cache', demo_dir], check=True)

os.environ['GSETTINGS_SCHEMA_DIR'] = os.path.join(prefix, 'share', 'glib-2.0', 'schemas')
os.environ['GSETTINGS_BACKEND'] = 'memory'
sys.path.insert(1, pkgdatadir)
gettext.install('apple-music')

import gi  # noqa: E402

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

Gio.Resource.load(os.path.join(pkgdatadir, 'applemusic.gresource'))._register()
from applemusic import main  # noqa: E402
from applemusic.widgets import artwork  # noqa: E402

decoded = 0
_load = artwork._load


def counting_load(path):
    global decoded
    decoded += 1  # in a worker thread; a lost increment would only undercount
    return _load(path)


artwork._load = counting_load

app = main.Application('0.0.0', 'io.github.jackicus.AppleMusic.ScrollTest',
                       'io.github.jackicus.AppleMusic', 'default', demo_dir)
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)


def on_startup(_app):
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_LIGHT if args.light else Adw.ColorScheme.FORCE_DARK)


def on_window_added(_app, window):
    window.set_resizable(False)  # a tiling window manager leaves a fixed size alone


def on_activate(_app):
    app.settings.set_string('last-page', args.page)
    app.settings.set_int('window-width', width)
    app.settings.set_int('window-height', height)
    GLib.timeout_add(1000, start)


def scrolled_window(widget):
    """The first mapped Gtk.ScrolledWindow under widget, depth first."""
    if isinstance(widget, Gtk.ScrolledWindow) and widget.get_mapped():
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = scrolled_window(child)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def start():
    if app.library.props.state == 'loading':
        GLib.timeout_add(100, start)
        return GLib.SOURCE_REMOVE
    window = app.get_active_window()
    page = window.navigation_view.get_visible_page()
    scrolled = scrolled_window(page)
    if scrolled is None:
        print(f'scroll-test: no scrolled window on the {args.page} page', flush=True)
        app.quit()
        return GLib.SOURCE_REMOVE
    monitor = window.get_display().get_monitor_at_surface(window.get_surface())
    interval = 1e6 / (monitor.get_refresh_rate() / 1000) if monitor else 1e6 / 60
    adjustment = scrolled.get_vadjustment()
    times = []
    work = []  # seconds of main-thread work per frame
    frame_clock = window.get_frame_clock()
    began = {}
    handlers = [
        frame_clock.connect('before-paint', lambda _clock: began.update(at=time.perf_counter())),
        frame_clock.connect('paint', lambda _clock: 'at' in began and work.append(
            time.perf_counter() - began.pop('at'))),
    ]

    def tick(_widget, clock):
        end = adjustment.get_upper() - adjustment.get_page_size()
        if end <= 0:  # not laid out yet
            return GLib.SOURCE_CONTINUE
        now = clock.get_frame_time()
        times.append(now)
        adjustment.set_value(min(end, args.speed * (now - times[0]) / 1e6))
        if adjustment.get_value() < end:
            return GLib.SOURCE_CONTINUE
        for handler in handlers:
            frame_clock.disconnect(handler)
        report(times, work, interval, end)
        return GLib.SOURCE_REMOVE

    scrolled.add_tick_callback(tick)
    return GLib.SOURCE_REMOVE


def window_size():
    window = app.get_active_window()
    return f'{window.get_width()}x{window.get_height()}'


def report(times, work, interval, distance):
    gaps = [b - a for a, b in zip(times, times[1:])] or [0]
    work = sorted(work[5:] or [0])  # the first frames include the start
    budget = interval / 1e6
    print(f'scroll-test: {args.page} at {window_size()}: {distance:.0f} px in '
          f'{(times[-1] - times[0]) / 1e6:.1f} s at {args.speed:.0f} px/s; '
          f'{decoded} covers decoded', flush=True)
    over = [(budget, f'{1 / budget:.0f} Hz')]
    if round(1 / budget) != 60:
        over.append((1 / 60, '60 Hz'))
    print(f'  work per frame: mean {1000 * statistics.mean(work):.1f} ms, '
          f'90th percentile {1000 * work[int(len(work) * 0.9)]:.1f} ms, '
          f'longest {1000 * work[-1]:.1f} ms; '
          + '; '.join(f'over {1000 * limit:.1f} ms ({rate}): '
                      f'{sum(w > limit for w in work)} of {len(work)}' for limit, rate in over),
          flush=True)
    rate = len(gaps) / ((times[-1] - times[0]) / 1e6)
    print(f'  frames drawn: {len(gaps)}, {rate:.0f} a second; '
          f'longest gap {max(gaps) / 1000:.1f} ms', flush=True)
    GLib.timeout_add(200, app.quit)


app.connect('startup', on_startup)
app.connect('activate', on_activate)
app.connect('window-added', on_window_added)
GLib.timeout_add_seconds(300, app.quit)  # whatever happens
main.use_glib_event_loop()
app.run(['scroll-test', '--demo'])
