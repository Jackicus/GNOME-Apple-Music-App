#!/usr/bin/env python3
"""Scroll a page of the real window from top to bottom and report dropped frames.

    scripts/scroll_test.py [--page KEY] [--speed PX_PER_S] [--distance PX] [--size WxH]
                           [--light] [--sidebar]

Runs the installed build (meson install -C build, or scripts/run.sh, first) on the
demo library, as screenshot.py --demo does (scripts/harness.py), with the desktop's
icon theme and animations: build/demo, or $APPLE_MUSIC_CACHE when set, which is how
a big library is measured:

    scripts/demo_library.py --cache build/demo-2000 --albums 2000
    APPLE_MUSIC_CACHE=build/demo-2000 scripts/scroll_test.py --page albums

Once the library has loaded and the page shows its list (the scrolled window of
its grid_view, column_view or list_view, once mapped: not a status page's), that
scrolled window is moved at a steady speed on every frame, to the bottom or for
--distance pixels (the Songs page of 30,000 songs is 1.5 million pixels long:
6 minutes at 4,000 px/s). A page without such a view scrolls its first mapped
scrolled window:

    APPLE_MUSIC_CACHE=build/demo-2500 scripts/scroll_test.py --page songs --distance 40000

--sidebar scrolls the sidebar instead of the page (a library with many
playlists makes it long: its rows are not recycled).

Two things are reported. The app's own work per frame, timed on the main thread
from the frame clock's before-paint to the end of its paint (tick callbacks and
the binds they cause, layout, snapshot and render): a frame whose work overran
the monitor's refresh interval, or 16.7 ms, would have been dropped at that
rate. And the gaps between the frames the compositor actually asked for, which
also depend on the compositor and on what else the machine is doing, so they
vary from run to run. Both count from the first frame that scrolled, not the
loading before it. A monitor that reports no refresh rate is taken as 60 Hz.
Also counts the artwork decoded on the way. Settings go to a memory backend.
"""

import argparse
import statistics
import time

import harness

parser = argparse.ArgumentParser()
parser.add_argument('--page', default='albums')
parser.add_argument('--speed', type=float, default=4000, help='pixels a second (default 4000)')
parser.add_argument('--distance', type=float, help='pixels to scroll (default: to the bottom)')
parser.add_argument('--size', default='1100x760')
parser.add_argument('--light', action='store_true')
parser.add_argument('--sidebar', action='store_true', help='scroll the sidebar, not the page')
args = parser.parse_args()
width, height = (int(n) for n in args.size.split('x'))

# Animations on and the desktop's look, as the numbers were always measured.
app = harness.make_app('ScrollTest', light=args.light, animations=True, stock_look=False,
                       size=(width, height), name='scroll-test')

from gi.repository import GLib, Gtk  # noqa: E402  (after make_app)

from applemusic.widgets import artwork  # noqa: E402

decoded = 0
_load = artwork._load


def counting_load(path, size=None):
    global decoded
    decoded += 1  # in a worker thread; a lost increment would only undercount
    return _load(path, size)


artwork._load = counting_load


def on_activate(_app):
    app.settings.set_string('last-page', args.page)
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


def list_scroller(page):
    """The Gtk.ScrolledWindow holding the page's list (its grid_view, column_view or
    list_view), mapped or not; else the first mapped scrolled window under it, or None."""
    for name in ('grid_view', 'column_view', 'list_view'):
        widget = getattr(page, name, None)
        while widget is not None and not isinstance(widget, Gtk.ScrolledWindow):
            widget = widget.get_parent()
        if widget is not None:
            return widget
    return scrolled_window(page)


waited = [0]  # polls since the library loaded, while the list is not shown


def start():
    if app.library.props.state == 'loading':
        GLib.timeout_add(100, start)
        return GLib.SOURCE_REMOVE
    window = app.get_active_window()
    page = window.sidebar if args.sidebar else window.navigation_view.get_visible_page()
    scrolled = list_scroller(page)
    if scrolled is not None and not scrolled.get_mapped():  # a status page shows meanwhile
        waited[0] += 1
        if waited[0] < 600:  # a minute
            GLib.timeout_add(100, start)
            return GLib.SOURCE_REMOVE
        scrolled = None
    if scrolled is None:
        print(f'scroll-test: no scrolled window shown on the {where()}', flush=True)
        app.quit()
        return GLib.SOURCE_REMOVE
    monitor = window.get_display().get_monitor_at_surface(window.get_surface())
    rate = monitor.get_refresh_rate() if monitor else 0  # millihertz; 0 when unknown
    interval = 1e9 / rate if rate else 1e6 / 60  # microseconds
    adjustment = scrolled.get_vadjustment()
    times = []
    work = []  # seconds of main-thread work per frame
    frame_clock = window.get_frame_clock()
    began = {}
    handlers = []

    def collect():
        """Time each frame's work from now on: the scrolling has begun."""
        handlers.extend([
            frame_clock.connect('before-paint',
                                lambda _clock: began.update(at=time.perf_counter())),
            frame_clock.connect('paint', lambda _clock: 'at' in began and work.append(
                time.perf_counter() - began.pop('at'))),
        ])

    def tick(_widget, clock):
        end = adjustment.get_upper() - adjustment.get_page_size()
        if end <= 0:  # not laid out yet
            return GLib.SOURCE_CONTINUE
        if args.distance:
            end = min(end, args.distance)
        now = clock.get_frame_time()
        if not times:
            collect()
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


def where():
    return 'sidebar' if args.sidebar else f'{args.page} page'


def window_size():
    window = app.get_active_window()
    return f'{window.get_width()}x{window.get_height()}'


def report(times, work, interval, distance):
    gaps = [b - a for a, b in zip(times, times[1:])] or [0]
    work = sorted(work or [0])
    budget = interval / 1e6
    print(f'scroll-test: {where()} at {window_size()}: {distance:.0f} px in '
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
          f'longest gap {max(gaps) / 1000:.1f} ms; gaps over 16.7 ms: '
          f'{sum(gap > 16700 for gap in gaps)}, over 33.3 ms: {sum(gap > 33300 for gap in gaps)}',
          flush=True)
    GLib.timeout_add(200, app.quit)


app.connect('activate', on_activate)
GLib.timeout_add_seconds(300, app.quit)  # whatever happens
harness.run_app(app)
