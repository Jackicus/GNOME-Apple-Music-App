#!/usr/bin/env python3
"""Render the app's window to a PNG, for checking UI changes without a human.

    scripts/screenshot.py [out.png] [--light] [--size WxH] [--page KEY]

Builds nothing itself: run scripts/run.sh (or meson install -C build) first.
The window is really mapped for about a second, so --size is only a request:
a tiling window manager may choose its own. Settings go to a memory backend.
"""

import argparse
import gettext
import os
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
prefix = os.path.join(root, 'build', 'install')
pkgdatadir = os.path.join(prefix, 'share', 'apple-music')

parser = argparse.ArgumentParser()
parser.add_argument('out', nargs='?', default=os.path.join(root, 'build', 'screenshot.png'))
parser.add_argument('--light', action='store_true')
parser.add_argument('--size', default='1100x760')
parser.add_argument('--page', default='home')
args = parser.parse_args()
width, height = (int(n) for n in args.size.split('x'))

os.environ['GSETTINGS_SCHEMA_DIR'] = os.path.join(prefix, 'share', 'glib-2.0', 'schemas')
os.environ['GSETTINGS_BACKEND'] = 'memory'  # don't touch the real settings
sys.path.insert(1, pkgdatadir)
gettext.install('apple-music')

import gi  # noqa: E402

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

Gio.Resource.load(os.path.join(pkgdatadir, 'applemusic.gresource'))._register()
from applemusic import main  # noqa: E402

app = main.Application('0.0.0', 'io.github.jackicus.AppleMusic.Screenshot',
                       'io.github.jackicus.AppleMusic', 'default')
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)


def on_startup(_app):
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_LIGHT if args.light else Adw.ColorScheme.FORCE_DARK)


def on_activate(_app):
    app.settings.set_string('last-page', args.page)
    app.settings.set_int('window-width', width)
    app.settings.set_int('window-height', height)
    GLib.timeout_add(1200, shoot)


def shoot():
    window = app.get_active_window()
    paintable = Gtk.WidgetPaintable(widget=window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
    texture.save_to_png(args.out)
    print(args.out)
    app.quit()


app.connect('startup', on_startup)
app.connect('activate', on_activate)
app.run([])
