#!/usr/bin/env python3
"""Render the app's window to a PNG, for checking UI changes without a human.

    scripts/screenshot.py [out.png] [--light] [--size WxH] [--page KEY] [--demo]
                          [--open KIND:ID] [--expand ID[,ID…]] [--signed-in [NAME]]
                          [--now-playing [lyrics|queue]] [--search TERM] [--context-menu]
                          [--preferences [general|engine]] [--dialog about|shortcuts]

Builds nothing itself: run meson install -C build (or scripts/run.sh) first.
The window is really mapped for about a second, so --size is only a request:
a tiling window manager may choose its own. Settings go to a memory backend,
and animations are off, so transitions finish at once. The shots use stock
GNOME's icons and font (the Adwaita icon theme, Adwaita Sans 11), not the
desktop's (scripts/harness.py).
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
own window (a fixed-size window that is neither maximized nor tiled gets one), at
--size when that is narrower than 640 px.
--dialog opens the About (app.about) or Keyboard Shortcuts (app.shortcuts) dialog and
shoots it as --preferences does.
"""

import argparse
import json
import os
import sys

import harness

parser = argparse.ArgumentParser()
parser.add_argument('out', nargs='?', default=os.path.join(harness.ROOT, 'build',
                                                            'screenshot.png'))
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
parser.add_argument('--dialog', choices=['about', 'shortcuts'],
                    help='open the About or Keyboard Shortcuts dialog and shoot it')
args = parser.parse_args()
if args.search:
    args.page = 'search'
if args.now_playing and not args.demo:
    parser.error('--now-playing needs --demo')
width, height = (int(n) for n in args.size.split('x'))

app = harness.make_app('Screenshot', demo=args.demo, light=args.light, size=(width, height))

from gi.repository import Adw, GLib, Graphene, Gtk  # noqa: E402  (after make_app)


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
    expand = [folder_id for folder_id in args.expand.split(',') if folder_id]
    expand = [first_folder() if folder_id == 'first' else folder_id for folder_id in expand]
    app.settings.set_strv('expanded-folders', [folder_id for folder_id in expand if folder_id])
    if args.signed_in is not None:
        app.settings.set_boolean('signed-in', True)
        app.settings.set_string('account-name', args.signed_in)  # no engine: autostart is off
    GLib.timeout_add(1200, shoot)


# The library's store for each kind --open takes.
SECTIONS = {'album': 'albums', 'artist': 'artists', 'playlist': 'playlists', 'station': 'radio',
            'video': 'videos'}
opened = False
sheet_opened = False
searched = False
menu_opened = False
preferences = None  # the Preferences dialog, once --preferences has opened it
dialog = None  # the --dialog dialog, once opened


def open_now_playing(window):
    """--now-playing: the invented item on the Player (the demo's first album playing, with
    the lyrics fixture) and the sheet open on the tab."""
    app.player.apply(harness.invented_playing_state(app, lyrics=True))
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


def find_widget(widget, kind):
    """The first widget of type kind under widget (itself included), depth first, or None."""
    if isinstance(widget, kind):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = find_widget(child, kind)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def open_dialog(window):
    """--dialog: the About or Keyboard Shortcuts dialog, as its menu item opens it (inside
    the window, or in a window of its own); the dialog, or None when none is shown."""
    kind = {'about': Adw.AboutDialog, 'shortcuts': Adw.ShortcutsDialog}[args.dialog]
    app.activate_action(args.dialog)
    shown = next((found for found in map(lambda toplevel: find_widget(toplevel, kind),
                                         Gtk.Window.list_toplevels()) if found is not None),
                 None)
    if isinstance(shown, Adw.AboutDialog) and harness.installed_icon():
        shown.set_application_icon(harness.installed_icon())  # not the script's own app ID
    if shown is not None and width < 640:
        shown.set_content_width(width)  # as narrow as the window, as for --preferences
        shown.set_content_height(height)
    return shown


def draw_popovers(window, snapshot):
    """Draw the window's popovers over it, each where its surface is: a popup's position is
    relative to the window's surface, both offset by their shadows' margins."""
    window_x, window_y = window.get_surface_transform()
    for popover in harness.popovers(window):
        if not popover.get_mapped():
            continue
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
    global opened, sheet_opened, searched, menu_opened, preferences, dialog
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
        if width < 640:
            # A narrow --size: the dialog as narrow (a window of its own takes its content
            # size, not the main window's).
            preferences.set_content_width(width)
            preferences.set_content_height(height)
        GLib.timeout_add(1200, shoot)  # shown, the cache measured
        return GLib.SOURCE_REMOVE
    if args.dialog and dialog is None:
        dialog = open_dialog(window)
        if dialog is None:
            sys.exit(f'screenshot: no {args.dialog} dialog shown')
        GLib.timeout_add(1200, shoot)  # shown
        return GLib.SOURCE_REMOVE
    for shown in (preferences, dialog):
        if shown is not None and shown.get_root() is not window:
            window = shown.get_root()  # a window of its own
    paintable = Gtk.WidgetPaintable(widget=window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    draw_popovers(window, snapshot)
    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
    texture.save_to_png(args.out)
    print(args.out)
    app.quit()
    return GLib.SOURCE_REMOVE


app.connect('activate', on_activate)
harness.run_app(app)
