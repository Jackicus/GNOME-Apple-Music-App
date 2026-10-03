#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Render the app's window to a PNG, for checking UI changes without a human.

    scripts/headless.sh scripts/screenshot.py [out.png] --demo [--light] [--size WxH]
                          [--page KEY] [--open KIND:ID] [--sidebar] [--expand ID[,ID…]]
                          [--banner sign-in|expired] [--signed-in [NAME]]
                          [--now-playing [lyrics|queue]] [--playing] [--search TERM]
                          [--context-menu] [--preferences [general|engine]]
                          [--dialog about|shortcuts|new-playlist|rename|delete]
                          [--more-options] [--sidebar-menu KEY] [--scroll PX]

Builds nothing itself: run meson install -C build (or scripts/demo.sh) first. Run it
through scripts/headless.sh, so the window opens on a private display and never on
the desktop. The window is really mapped for a moment, so --size is only a request:
a tiling window manager may choose its own. Settings go to a memory backend,
and animations are off, so transitions finish at once. The shots use stock
GNOME's icons and font (the Adwaita icon theme, Adwaita Sans 11), not the
desktop's (scripts/harness.py). Before the shot, the script waits until no
artwork decode has been in flight for a few polls (5 s at most), so tiles show
their covers rather than placeholders.
In the narrow (collapsed) layout the shot shows the sidebar, or the page when
--page is given (--sidebar keeps the sidebar, scrolled to the page's row).
--demo shows the invented library in build/demo (generated first if missing)
or in $APPLE_MUSIC_CACHE when that is set, as scripts/demo.sh does. The shot
waits for the library to finish loading.
--open KIND:ID opens an item once the library has loaded, as activating its
tile does (window.open_item), over the --page (default home): KIND is album,
artist, playlist, folder, station or video, and ID an item id or "first", the
first of that section (for folder, the library's first playlist folder). An
artist opens as the library's page of them; artist-page:ID opens Apple Music's
page of the artist instead, as Go to Artist does (window.open_artist_page).
--page also takes a sidebar playlist or folder, as last-page names them:
playlist:ID or folder:ID (folder:first too). --expand opens these playlist
folders in the sidebar (the expanded-folders setting); "first" is the library's
first folder.
--banner sign-in|expired reveals the sign-in banner as a signed-out release build,
or one whose sign-in expired, shows it (the demo has no account, so none).
--signed-in shows the account button as signed in (the signed-in and account-name
settings, in the memory backend only), with NAME on it when given; the engine is
never started here.
--now-playing puts an invented item on the Player (the first track of the demo
library's first album, playing, its queue the album, the synced lyrics of
tests/fixtures/lyrics.json, the album's cover as its artwork) and opens the Now
Playing sheet on its Lyrics tab, or on Up Next with "queue". With --demo only.
--playing puts the same item on the Player and leaves the sheet closed: the player
bar in its playing state. With --demo only.
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
shoots it as --preferences does; new-playlist, rename and delete open the playlists' dialogs
(win.item-new-playlist, win.item-rename, win.item-delete) for the playlist or folder the page
shows, else the library's first playlist of the user's (a new playlist: in the folder shown,
else at the top level). The demo writes nothing: the dialogs only show.
--more-options pops up the More Options menu of the page shown (an album's, a playlist's, a
folder's) as a click on it would.
--sidebar-menu KEY pops up the sidebar's context menu for KEY (playlist:ID, folder:ID,
all-playlists; playlist:first and folder:first are the library's first of each), drawn from
the menu the sidebar fills for it, beside its row (--expand the folder holding it).
--scroll PX scrolls the page shown down by PX pixels before the shot (its first
scrolled window that scrolls vertically): the rest of a page taller than the
virtual monitor, which a --size cannot show.
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
                    help='open an item (ID an item id or "first") over the page; '
                         "artist-page:ID for Apple Music's page of an artist")
parser.add_argument('--sidebar', action='store_true',
                    help='in the narrow layout, show the sidebar rather than the --page')
parser.add_argument('--expand', metavar='ID[,ID…]', default='',
                    help='expand these playlist folders in the sidebar ("first": the first one)')
parser.add_argument('--banner', choices=['sign-in', 'expired'],
                    help='reveal the sign-in banner as a signed-out release build, or one '
                         'whose sign-in expired, shows it (the demo has no account)')
parser.add_argument('--signed-in', metavar='NAME', nargs='?', const='',
                    help='show the account as signed in, as NAME when given')
parser.add_argument('--now-playing', metavar='TAB', nargs='?', const='lyrics',
                    choices=['lyrics', 'queue'],
                    help='an invented item playing, the Now Playing sheet open on TAB')
parser.add_argument('--playing', action='store_true',
                    help='an invented item playing, the sheet closed (the player bar)')
parser.add_argument('--search', metavar='TERM',
                    help='the Search page in Your Library mode with TERM typed')
parser.add_argument('--context-menu', action='store_true',
                    help="pop up the context menu of the page's first tile or row")
parser.add_argument('--preferences', metavar='PAGE', nargs='?', const='general',
                    choices=['general', 'engine'],
                    help='open Preferences on PAGE and shoot the dialog')
parser.add_argument('--dialog', choices=['about', 'shortcuts', 'new-playlist', 'rename',
                                         'delete'],
                    help='open the About, Keyboard Shortcuts or a playlist dialog; shoot it')
parser.add_argument('--more-options', action='store_true',
                    help='pop up the More Options menu of the page shown')
parser.add_argument('--sidebar-menu', metavar='KEY',
                    help="pop up the sidebar's context menu for KEY")
parser.add_argument('--scroll', metavar='PX', type=int, default=0,
                    help='scroll the page shown down by PX pixels before the shot')
args = parser.parse_args()
if args.search:
    args.page = 'search'
if (args.now_playing or args.playing) and not args.demo:
    parser.error('--now-playing and --playing need --demo')
width, height = (int(n) for n in args.size.split('x'))

app = harness.make_app('Screenshot', demo=args.demo, light=args.light, size=(width, height))

from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402  (after make_app)


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
    page = args.page or 'home'
    if page == 'folder:first':
        page = f'folder:{first_folder()}'
    app.settings.set_string('last-page', page)
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
banner_shown = False
scrolled = False
failed = False  # a step raised: the app quits and the script exits 1
preferences = None  # the Preferences dialog, once --preferences has opened it
dialog = None  # the --dialog dialog, once opened
# Waiting for the artwork before the shot: a poll every ARTWORK_POLL ms, the shot once
# ARTWORK_QUIET polls in a row found no decode in flight (an ArtworkSlot asks in an idle
# after its frame, so one quiet poll is not enough), or after ARTWORK_POLLS polls whatever.
ARTWORK_POLL = 100
ARTWORK_QUIET = 3
ARTWORK_POLLS = 50
artwork_polls = 0
artwork_quiet = 0


def open_now_playing(window):
    """--now-playing: the invented item on the Player (the demo's first album playing, with
    the lyrics fixture) and the sheet open on the tab; --playing: the item only, for the
    player bar."""
    app.player.apply(harness.invented_playing_state(app, lyrics=True))
    if args.now_playing:
        window.now_playing.tab_stack.set_visible_child_name(args.now_playing)
        window.bottom_sheet.set_open(True)


def search_library(window):
    """--search: Your Library mode with the term typed into the entry."""
    page = window.navigation_view.get_visible_page()
    page.set_mode('library')
    page.search_entry.set_text(args.search)


def open_item(window):
    """--open: the item it names, opened as its tile would be."""
    kind, _sep, item_id = args.open.partition(':')
    catalog = kind == 'artist-page'  # Apple Music's page of the artist, as Go to Artist opens
    if catalog:
        kind = 'artist'
    if item_id == 'first' and kind in SECTIONS:
        item = getattr(app.library, SECTIONS[kind]).get_item(0)
    elif item_id == 'first' and kind == 'folder':
        item = app.library.by_id('folder', first_folder())
    else:
        item = app.library.by_id(kind, item_id)
    if item is None:
        sys.exit(f'screenshot: no {args.open} in the library')
    if catalog:
        window.open_artist_page(item)
    else:
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


def first_playlist():
    """The library's first playlist of the user's (one that can be renamed), or None."""
    return next((node.item for node in app.library.playlist_tree().flat
                 if node.kind == 'playlist' and node.item.editable), None)


def subject(window):
    """The playlist or folder the page shows (a pushed one's, or the sidebar key's), else
    the library's first playlist of the user's."""
    item = window.shown_item()
    if item is not None and item.kind in ('playlist', 'folder'):
        return item
    kind, _sep, item_id = (args.page or '').partition(':')
    if kind == 'folder' and item_id == 'first':
        item_id = first_folder()
    item = app.library.by_id(kind, item_id) if kind in ('playlist', 'folder') else None
    return item or first_playlist()


def open_playlist_dialog(window):
    """--dialog new-playlist, rename or delete: the item action, as its menu item runs it."""
    item = subject(window)
    if args.dialog == 'new-playlist':
        folder = item.id if item is not None and item.kind == 'folder' else 'root'
        target = ('folder', folder)
    elif item is None:
        sys.exit("screenshot: no playlist of the user's to show the dialog for")
    else:
        target = (item.kind, item.id)
    window.activate_action(f'win.item-{args.dialog}', GLib.Variant('(ss)', target))


def open_more_options(window):
    """--more-options: the page's More Options menu, popped up."""
    page = window.navigation_view.get_visible_page()
    button = next((found for found in harness_descendants(page, Gtk.MenuButton)
                   if found.get_tooltip_text() == 'More Options' and found.get_mapped()), None)
    if button is None:
        sys.exit('screenshot: the page shown has no More Options menu')
    button.popup()


def open_sidebar_menu(window):
    """--sidebar-menu: the menu the sidebar fills for the key's item (sidebar_view's
    setup-menu handler), popped up beside its row as the sidebar shows it."""
    key = args.sidebar_menu
    if key == 'folder:first':
        key = f'folder:{first_folder()}'
    elif key == 'playlist:first':
        key = f'playlist:{first_playlist().id}'
    sidebar = window._sidebar
    item = sidebar.item_for(key)
    row = sidebar.row(item.get_index()) if item is not None else None
    if row is None:
        sys.exit(f'screenshot: no sidebar row for {key}')
    sidebar._on_setup_menu(None, item)
    popover = Gtk.PopoverMenu.new_from_model(sidebar._sidebar_menu)
    popover.set_has_arrow(False)
    popover.set_parent(row)
    rectangle = Gdk.Rectangle()
    rectangle.x, rectangle.y = int(row.get_width() * 0.4), int(row.get_height() * 0.6)
    rectangle.width = rectangle.height = 1
    popover.set_pointing_to(rectangle)
    popover.set_halign(Gtk.Align.START)
    popover.popup()


def harness_descendants(widget, kind):
    if isinstance(widget, kind):
        yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from harness_descendants(child, kind)
        child = child.get_next_sibling()


def open_dialog(window):
    """--dialog: the About or Keyboard Shortcuts dialog, as its menu item opens it (inside
    the window, or in a window of its own), or a playlist dialog; the dialog, or None when
    none is shown."""
    if args.dialog in ('new-playlist', 'rename', 'delete'):
        open_playlist_dialog(window)
        kind = Adw.AlertDialog
    else:
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


def scroll_page(window):
    """Scroll the visible page's first vertically scrolling scrolled window by args.scroll."""
    def find(widget):
        if isinstance(widget, Gtk.ScrolledWindow) and \
                widget.props.vscrollbar_policy != Gtk.PolicyType.NEVER:
            return widget
        child = widget.get_first_child()
        while child is not None:
            found = find(child)
            if found is not None:
                return found
            child = child.get_next_sibling()
        return None

    scrolled_window = find(window.navigation_view.get_visible_page())
    if scrolled_window is None:
        sys.exit('screenshot: the page shown does not scroll')
    adjustment = scrolled_window.get_vadjustment()
    adjustment.set_value(min(args.scroll, adjustment.get_upper() - adjustment.get_page_size()))


def artwork_settled():
    """Whether the artwork has arrived: ARTWORK_QUIET polls in a row with no decode in
    flight, or ARTWORK_POLLS polls in all."""
    global artwork_polls, artwork_quiet
    from applemusic.widgets import artwork

    artwork_polls += 1
    artwork_quiet = 0 if artwork.get_default().pending() else artwork_quiet + 1
    return artwork_quiet >= ARTWORK_QUIET or artwork_polls >= ARTWORK_POLLS


def shoot():
    """One step of the shot (each stage waits for the next frame), or the shot itself. A
    step that raises ends the run: the app quits, the traceback shown, rather than waiting
    for a step that never comes."""
    global failed
    try:
        return _shoot()
    except Exception:
        import traceback

        traceback.print_exc()
        failed = True
        app.quit()
        return GLib.SOURCE_REMOVE


def _shoot():
    global opened, sheet_opened, searched, menu_opened, banner_shown, preferences, dialog
    global scrolled
    if app.library.props.state == 'loading':
        GLib.timeout_add(100, shoot)  # pages show what loaded, not "Loading…"
        return GLib.SOURCE_REMOVE
    window = app.get_active_window()
    split_view = window.split_view
    showing_sidebar = split_view.get_collapsed() and not split_view.get_show_content()
    if (args.page or args.open) and showing_sidebar and not args.sidebar:
        split_view.set_show_content(True)  # the page, not the sidebar
        GLib.timeout_add(600, shoot)  # after the transition
        return GLib.SOURCE_REMOVE
    if args.open and not opened:
        opened = True
        open_item(window)
        GLib.timeout_add(1500, shoot)  # after the push, with the artwork decoded
        return GLib.SOURCE_REMOVE
    if (args.now_playing or args.playing) and not sheet_opened:
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
    if args.more_options and not menu_opened:
        menu_opened = True
        open_more_options(window)
        GLib.timeout_add(800, shoot)  # the popover shown and placed
        return GLib.SOURCE_REMOVE
    if args.sidebar_menu and not menu_opened:
        menu_opened = True
        open_sidebar_menu(window)
        GLib.timeout_add(800, shoot)
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
    if args.banner and not banner_shown:
        banner_shown = True
        from applemusic.window import sign_in_title

        window.sign_in_banner.set_title(sign_in_title(args.banner == 'expired'))
        window.sign_in_banner.set_revealed(True)
        GLib.timeout_add(600, shoot)  # laid out under the header bar
        return GLib.SOURCE_REMOVE
    if args.scroll and not scrolled:
        scrolled = True
        scroll_page(window)
        GLib.timeout_add(1200, shoot)  # what scrolled into view bound, its artwork decoded
        return GLib.SOURCE_REMOVE
    if not artwork_settled():
        GLib.timeout_add(ARTWORK_POLL, shoot)
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
if failed:
    sys.exit(1)
