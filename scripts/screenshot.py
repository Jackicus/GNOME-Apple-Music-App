#!/usr/bin/env python3
"""Render the app's window to a PNG, for checking UI changes without a human.

    scripts/screenshot.py [out.png] [--light] [--size WxH] [--page KEY] [--demo]
                          [--open KIND:ID] [--expand ID[,ID…]] [--signed-in [NAME]]
                          [--now-playing [lyrics|queue]] [--search TERM] [--context-menu]
                          [--preferences [general|engine]]

Builds nothing itself: run scripts/run.sh (or meson install -C build) first.
The window is really mapped for about a second, so --size is only a request:
a tiling window manager may choose its own. Settings go to a memory backend,
and animations are off, so transitions finish at once.
In the narrow (collapsed) layout the shot shows the sidebar, or the page when
--page is given.
--demo shows the invented library in build/demo (generated first if missing)
or in $APPLE_MUSIC_CACHE when that is set, as scripts/demo.sh does. The shot
waits for the library to finish loading.
--open KIND:ID opens an item once the library has loaded, as activating its
tile does (window.open_item), over the --page (default home): KIND is album,
artist, playlist, station or video, and ID an item id or "first", the first
of that section. The shot then waits longer, for the artwork.
--page also takes a sidebar playlist or folder, as last-page names them:
playlist:ID or folder:ID. --expand opens these playlist folders in the
sidebar (the expanded-folders setting); "first" is the library's first folder.
--signed-in shows the account button as signed in (the signed-in and account-name
settings, in the memory backend only), with NAME on it when given; the engine is
never started here.
--now-playing puts an invented item on the Player (the first track of the demo
library's first album, playing, its queue the album, the synced lyrics of
tests/fixtures/lyrics.json, the album's cover as its artwork) and opens the Now
Playing sheet on its Lyrics tab, or on Up Next with "queue". With --demo only.
--search TERM shows the Search page in Your Library mode with TERM typed (the
results of the offline filter; the engine is never started here).
--context-menu pops up the context menu of the page's first tile or row (the
first shown widget with a context_item), as a right click on it would, and
draws the popover into the shot where the compositor put it.
--preferences opens the Preferences dialog (app.preferences) on its General page, or
on Engine, and shoots it: inside the window when libadwaita put it there, else its
own window (a fixed-size window that is neither maximized nor tiled gets one).
"""

import argparse
import gettext
import json
import os
import shutil
import subprocess
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
prefix = os.path.join(root, 'build', 'install')
pkgdatadir = os.path.join(prefix, 'share', 'apple-music')

parser = argparse.ArgumentParser()
parser.add_argument('out', nargs='?', default=os.path.join(root, 'build', 'screenshot.png'))
parser.add_argument('--light', action='store_true')
parser.add_argument('--size', default='1100x760')
parser.add_argument('--page')
parser.add_argument('--demo', action='store_true', help='show the demo library in build/demo')
parser.add_argument('--open', metavar='KIND:ID',
                    help='open an item (ID an item id or "first") over the page')
parser.add_argument('--expand', metavar='ID[,ID…]', default='',
                    help='expand these playlist folders in the sidebar ("first": the first one)')
parser.add_argument('--signed-in', metavar='NAME', nargs='?', const='',
                    help='show the account as signed in, as NAME when given')
parser.add_argument('--now-playing', metavar='TAB', nargs='?', const='lyrics',
                    choices=['lyrics', 'queue'],
                    help='an invented item playing, the Now Playing sheet open on TAB')
parser.add_argument('--search', metavar='TERM',
                    help='the Search page in Your Library mode with TERM typed')
parser.add_argument('--context-menu', action='store_true',
                    help="pop up the context menu of the page's first tile or row")
parser.add_argument('--preferences', metavar='PAGE', nargs='?', const='general',
                    choices=['general', 'engine'],
                    help='open Preferences on PAGE and shoot the dialog')
args = parser.parse_args()
if args.search:
    args.page = 'search'
if args.now_playing and not args.demo:
    parser.error('--now-playing needs --demo')
width, height = (int(n) for n in args.size.split('x'))

demo_dir = os.path.join(root, 'build', 'demo')  # what the launcher passes as DEMO_DIR
if (args.demo and not os.environ.get('APPLE_MUSIC_CACHE')
        and not os.path.exists(os.path.join(demo_dir, 'library.json'))):
    subprocess.run([sys.executable, os.path.join(root, 'scripts', 'demo_library.py'),
                    '--cache', demo_dir], check=True)

os.environ['GSETTINGS_SCHEMA_DIR'] = os.path.join(prefix, 'share', 'glib-2.0', 'schemas')
os.environ['GSETTINGS_BACKEND'] = 'memory'  # don't touch the real settings
sys.path.insert(1, pkgdatadir)
gettext.install('apple-music')

import gi  # noqa: E402

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Graphene, Gtk  # noqa: E402

Gio.Resource.load(os.path.join(pkgdatadir, 'applemusic.gresource'))._register()
from applemusic import main  # noqa: E402

app = main.Application('0.0.0', 'io.github.jackicus.AppleMusic.Screenshot',
                       'io.github.jackicus.AppleMusic', 'default', demo_dir)
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)


def on_startup(_app):
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_LIGHT if args.light else Adw.ColorScheme.FORCE_DARK)
    # Pages arrive at once: a transition the compositor starves of frames (an unfocused window)
    # can still be sliding when the shot is taken.
    Gtk.Settings.get_default().set_property('gtk-enable-animations', False)


def on_window_added(_app, window):
    # A fixed-size window is one a tiling window manager leaves alone.
    window.set_resizable(False)


def first_folder():
    """The id of the first playlist folder of the library the app will load, or None."""
    from applemusic.backend import config
    try:
        with open(config.cache_dir() / 'library.json', encoding='utf-8') as file:
            folders = json.load(file).get('folders') or []
    except (OSError, ValueError):
        return None
    return next((folder['id'] for folder in folders if folder.get('id') != 'root'), None)


def on_activate(_app):
    app.settings.set_string('last-page', args.page or 'home')
    app.settings.set_int('window-width', width)
    app.settings.set_int('window-height', height)
    expand = [folder_id for folder_id in args.expand.split(',') if folder_id]
    expand = [first_folder() if folder_id == 'first' else folder_id for folder_id in expand]
    app.settings.set_strv('expanded-folders', [folder_id for folder_id in expand if folder_id])
    if args.signed_in is not None:
        app.settings.set_boolean('signed-in', True)
        app.settings.set_string('account-name', args.signed_in)
        app.settings.set_boolean('engine-autostart', False)  # signed in, but no engine here
    GLib.timeout_add(1200, shoot)


# The library's store for each kind --open takes.
SECTIONS = {'album': 'albums', 'artist': 'artists', 'playlist': 'playlists', 'station': 'radio',
            'video': 'videos'}
opened = False
sheet_opened = False
searched = False
menu_opened = False
preferences = None  # the Preferences dialog, once --preferences has opened it

# --now-playing: the invented item's artwork URL. Its cover is copied to where
# Artwork.fetch_remote would put this URL's 640 px image, so nothing is fetched.
DEMO_ART_URL = 'https://example.invalid/demo-art/{w}x{h}bb.jpg'
NOW_PLAYING_POSITION = 65.0  # seconds in: a line of the fixture is current, mid-song


def now_playing_state():
    """The Player.apply() dict for --now-playing: the demo library's first album as the
    queue, its first track playing, with the lyrics fixture."""
    from applemusic.backend import config
    from applemusic.widgets.artwork import remote_art_path

    album = app.library.albums.get_item(0)
    if album is None:
        sys.exit('screenshot: the demo library has no album')
    groups = album.raw.get('groups') or []
    entries = [dict(entry) for group in groups for entry in group.get('entries') or []]
    if not entries:
        sys.exit('screenshot: the demo album has no tracks')
    for position, entry in enumerate(entries):
        entry['artUrl'] = DEMO_ART_URL
        entry['index'] = position
    art_path = remote_art_path(DEMO_ART_URL, config.COVER_SIZE)
    if album.art and art_path and not os.path.exists(art_path):
        os.makedirs(os.path.dirname(art_path), exist_ok=True)
        shutil.copyfile(album.art, art_path)
    with open(os.path.join(root, 'tests', 'fixtures', 'lyrics.json'), encoding='utf-8') as file:
        lyrics = json.load(file)
    return {
        'state': 'playing', 'track': entries[0], 'position': NOW_PLAYING_POSITION,
        'duration': entries[0].get('durationMs', 0) / 1000, 'shuffle': 'off',
        'repeat': 'none', 'volume': 0.7, 'queue': {'index': 0, 'items': entries},
        'lyrics': lyrics,
    }


def open_now_playing(window):
    """--now-playing: the invented item on the Player and the sheet open on the tab."""
    app.player.apply(now_playing_state())
    window.now_playing.tabs.set_active_name(args.now_playing)
    window.bottom_sheet.set_open(True)


def search_library(window):
    """--search: Your Library mode with the term typed into the entry."""
    page = window.navigation_view.get_visible_page()
    page.set_mode('library')
    page.search_entry.set_text(args.search)


def open_item(window):
    """--open: the item it names, opened as its tile would be."""
    kind, _sep, item_id = args.open.partition(':')
    if item_id == 'first' and kind in SECTIONS:
        item = getattr(app.library, SECTIONS[kind]).get_item(0)
    else:
        item = app.library.by_id(kind, item_id)
    if item is None:
        sys.exit(f'screenshot: no {args.open} in the library')
    window.open_item(item)


def first_context_widget(widget):
    """The first mapped widget under `widget` with a context_item, depth first."""
    if getattr(widget, 'context_item', None) is not None and widget.get_mapped():
        return widget
    child = widget.get_first_child()
    while child is not None:
        if child.get_mapped():
            found = first_context_widget(child)
            if found is not None:
                return found
        child = child.get_next_sibling()
    return None


def open_context_menu(window):
    """--context-menu: the first tile's or row's menu, pointing into it as a click would."""
    from applemusic.widgets import context_menu

    widget = first_context_widget(window.navigation_view.get_visible_page())
    if widget is None:
        sys.exit('screenshot: nothing on the page has a context menu')
    x, y = widget.get_width() * 0.6, widget.get_height() * 0.35
    if context_menu.popup(widget, widget.context_item, x, y) is None:
        sys.exit('screenshot: the first item has no menu')


def popovers(widget, found):
    """The popovers shown under widget (children of the widgets they point from)."""
    if isinstance(widget, Gtk.Popover) and widget.get_mapped():
        found.append(widget)
    child = widget.get_first_child()
    while child is not None:
        popovers(child, found)
        child = child.get_next_sibling()
    return found


def draw_popovers(window, snapshot):
    """Draw the window's popovers over it, each where its surface is: a popup's position is
    relative to the window's surface, both offset by their shadows' margins."""
    window_x, window_y = window.get_surface_transform()
    for popover in popovers(window, []):
        surface = popover.get_surface()
        popover_x, popover_y = popover.get_surface_transform()
        point = Graphene.Point()
        point.x = surface.get_position_x() + popover_x - window_x
        point.y = surface.get_position_y() + popover_y - window_y
        snapshot.save()
        snapshot.translate(point)
        Gtk.WidgetPaintable(widget=popover).snapshot(
            snapshot, popover.get_width(), popover.get_height())
        snapshot.restore()


def shoot():
    global opened, sheet_opened, searched, menu_opened, preferences
    if app.library.props.state == 'loading':
        GLib.timeout_add(100, shoot)  # pages show what loaded, not "Loading…"
        return GLib.SOURCE_REMOVE
    window = app.get_active_window()
    split_view = window.split_view
    showing_sidebar = split_view.get_collapsed() and not split_view.get_show_content()
    if (args.page or args.open) and showing_sidebar:
        split_view.set_show_content(True)  # the page, not the sidebar
        GLib.timeout_add(600, shoot)  # after the transition
        return GLib.SOURCE_REMOVE
    if args.open and not opened:
        opened = True
        open_item(window)
        GLib.timeout_add(1500, shoot)  # after the push, with the artwork decoded
        return GLib.SOURCE_REMOVE
    if args.now_playing and not sheet_opened:
        sheet_opened = True
        open_now_playing(window)
        GLib.timeout_add(1500, shoot)  # the sheet open, the artwork decoded
        return GLib.SOURCE_REMOVE
    if args.search and not searched:
        searched = True
        search_library(window)
        GLib.timeout_add(1500, shoot)  # after the debounce, the songs built, artwork decoded
        return GLib.SOURCE_REMOVE
    if args.context_menu and not menu_opened:
        menu_opened = True
        open_context_menu(window)
        GLib.timeout_add(800, shoot)  # the popover shown and placed
        return GLib.SOURCE_REMOVE
    if args.preferences and preferences is None:
        preferences = app.show_preferences(args.preferences)
        GLib.timeout_add(1200, shoot)  # shown, the cache measured
        return GLib.SOURCE_REMOVE
    if preferences is not None and preferences.get_root() is not window:
        window = preferences.get_root()  # a window of its own
    paintable = Gtk.WidgetPaintable(widget=window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    draw_popovers(window, snapshot)
    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
    texture.save_to_png(args.out)
    print(args.out)
    app.quit()
    return GLib.SOURCE_REMOVE


app.connect('startup', on_startup)
app.connect('activate', on_activate)
app.connect('window-added', on_window_added)
main.use_glib_event_loop()  # as main.main() does, so app.spawn() works
app.run(['screenshot'] + (['--demo'] if args.demo else []))
