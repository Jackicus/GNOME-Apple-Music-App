#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Walk the keyboard checklist and list unnamed widgets, without a keyboard or a screen reader.

    scripts/a11y_check.py [--size WxH] [--light] [--names]

Runs the installed build (meson install -C build, or scripts/run.sh, first) on the demo
library, as screenshot.py --demo does (scripts/harness.py: memory settings, animations off,
no engine), and walks the
keyboard walkthrough in docs/accessibility.md key by key, printing PASS or FAIL for each step;
the exit status is 1 when a step fails. Use --size 360x640 for the narrow layout.

Keys cannot be sent to a window on this Wayland desktop, so each press is dispatched through
GTK's own handlers as a key event would be (press()): the capture-phase key and shortcut
controllers from the window down to the focus widget (the window's playback keys, the
application's accelerators, the mnemonics a dialog's shortcut manager runs on its labels),
then the shortcut controllers from the focus widget up to the window (the widgets' own keys:
Tab, the arrows, Enter, Escape, the context-menu keys) and a popover's own key handler.
Typing into an entry is not emulated (the text is set). Step 8 of the walkthrough (a dialog
over the window) runs with the window maximized, where a dialog is inside it, and the
window is made its --size again afterwards.

--names also starts a private accessibility bus (a dbus-daemon with at-spi's configuration on
an abstract socket, and at-spi2-registryd, stopped when the script exits, however it exits;
nothing on the desktop's own buses) and, on each page, lists the focusable controls that
have no accessible name, as libatspi reads them: what Orca would find, and what the GTK
inspector's Accessibility tab shows. Anything listed there is a gap (libadwaita's own
widgets included).
"""

import argparse
import asyncio
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import harness

parser = argparse.ArgumentParser()
parser.add_argument('--size', default='1100x760')
parser.add_argument('--light', action='store_true')
parser.add_argument('--names', action='store_true',
                    help='list focusable controls without an accessible name (libatspi)')
parser.add_argument('--dump', action='store_true', help=argparse.SUPPRESS)  # the lister
args = parser.parse_args()

# Where distributions put at-spi's registry daemon and its bus configuration.
REGISTRYD = ('/usr/libexec/at-spi2-registryd', '/usr/lib/at-spi2-registryd',
             '/usr/lib/at-spi2-core/at-spi2-registryd')
BUS_CONFIGS = ('/usr/share/defaults/at-spi2/accessibility.conf',
               '/etc/at-spi2/accessibility.conf')
REGISTRY_NAME = 'org.a11y.atspi.Registry'

# Roles whose focusable objects need a name of their own.
NAMED_ROLES = {'push button', 'toggle button', 'check box', 'slider', 'spin button',
               'combo box', 'menu item', 'list item', 'table row', 'text', 'entry', 'switch',
               'radio button', 'link', 'page tab', 'table cell', 'button', 'tree item'}


def list_unnamed():
    """--dump: print the app's showing, focusable, sensitive objects with a role in
    NAMED_ROLES and no name, one per line (role and path), from the bus in the environment."""
    import gi
    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi

    desktop = Atspi.get_desktop(0)
    apps = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
    app = next((a for a in apps if a is not None and a.get_name() == 'a11y_check'), None)
    if app is None:
        sys.exit('a11y_check: the app is not on the accessibility bus')

    def walk(obj, path):
        states = obj.get_state_set()
        if path and not states.contains(Atspi.StateType.SHOWING):
            return
        role = obj.get_role_name()
        if (role in NAMED_ROLES and not obj.get_name()
                and states.contains(Atspi.StateType.FOCUSABLE)
                and states.contains(Atspi.StateType.SENSITIVE)):
            print(f'{role} in {" > ".join(path[-3:])}')
        label = f'{role} {obj.get_name()!r}' if obj.get_name() else role
        for i in range(obj.get_child_count()):
            child = obj.get_child_at_index(i)
            if child is not None:
                walk(child, path + [label])

    walk(app, [])


if args.dump:
    list_unnamed()
    sys.exit(0)



def stop(process):
    """End a daemon this script started: SIGTERM, then SIGKILL after 5 s."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def start_daemon(argv, **kwargs):
    """Start a daemon, stopped when this script exits (atexit: also after an exception or
    sys.exit())."""
    process = subprocess.Popen(argv, **kwargs)
    atexit.register(stop, process)
    return process


def wait_for_name(address, name, timeout=5.0):
    """Whether `name` gets an owner on the bus at address within timeout seconds."""
    from gi.repository import Gio, GLib

    connection = Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = connection.call_sync(
                'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                'NameHasOwner', GLib.Variant('(s)', (name,)), GLib.VariantType('(b)'),
                Gio.DBusCallFlags.NONE, 1000, None)
            if reply.unpack()[0]:
                return True
            time.sleep(0.05)
        return False
    finally:
        connection.close_sync(None)


def start_accessibility_bus():
    """A private accessibility bus (dbus-daemon with at-spi's configuration listening on an
    abstract socket, and at-spi2-registryd on it), set as AT_SPI_BUS_ADDRESS, which GTK and
    libatspi take over the desktop's: nothing here touches the session."""
    registryd = next((path for path in REGISTRYD if os.access(path, os.X_OK)), None)
    if registryd is None:
        sys.exit('a11y_check: --names needs at-spi2-registryd (looked in '
                 + ', '.join(REGISTRYD) + ')')
    source = next((path for path in BUS_CONFIGS if os.path.exists(path)), None)
    if source is None:
        sys.exit("a11y_check: --names needs at-spi's accessibility.conf (looked in "
                 + ', '.join(BUS_CONFIGS) + ')')
    if shutil.which('dbus-daemon') is None:
        sys.exit('a11y_check: --names needs dbus-daemon')
    with open(source, encoding='utf-8') as file:
        text = file.read()
    listen = f'<listen>unix:abstract=apple-music-a11y-check-{os.getpid()}</listen>'
    config_text = re.sub(r'<listen>.*?</listen>', listen, text, count=1, flags=re.S)
    if config_text == text:
        sys.exit(f'a11y_check: no <listen> element to replace in {source}')
    with tempfile.TemporaryDirectory(prefix='a11y-check-') as config_dir:
        config = os.path.join(config_dir, 'accessibility.conf')
        with open(config, 'w', encoding='utf-8') as file:
            file.write(config_text)
        daemon = start_daemon(['dbus-daemon', '--config-file', config, '--nofork',
                               '--print-address'], stdout=subprocess.PIPE, text=True)
        address = daemon.stdout.readline().strip()  # printed once it listens
    if not address:
        sys.exit('a11y_check: the private dbus-daemon did not start')
    os.environ['AT_SPI_BUS_ADDRESS'] = address
    start_daemon([registryd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_for_name(address, REGISTRY_NAME):
        sys.exit('a11y_check: at-spi2-registryd did not come up on the private bus')


width, height = (int(n) for n in args.size.split('x'))
harness.require_install()  # before any daemon starts
if args.names:
    start_accessibility_bus()

# The program name is how the lister finds the app on the accessibility bus.
app = harness.make_app('A11yCheck', light=args.light, size=(width, height), name='a11y_check')

from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402  (after make_app)

from applemusic import shortcuts  # noqa: E402  (the installed build's, after make_app)

failures = []


# -- key presses --------------------------------------------------------------------------

def _parse(accel):
    ok, key, mods = Gtk.accelerator_parse(accel)
    return (key, int(mods)) if ok else None


def _controllers(widget):
    model = widget.observe_controllers()
    return [model.get_item(i) for i in range(model.get_n_items())]


def descendants(widget):
    """The widgets under widget, depth first."""
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def _mnemonic_owner(manager, shortcut):
    """The widget under manager whose own (managed) shortcut controller holds shortcut: the
    label a mnemonic belongs to, which GTK activates. None when there is none."""
    for widget in (manager, *descendants(manager)):
        for controller in _controllers(widget):
            if (isinstance(controller, Gtk.ShortcutController)
                    and controller.get_scope() == Gtk.ShortcutScope.MANAGED
                    and shortcut in [controller.get_item(i)
                                     for i in range(controller.get_n_items())]):
                return widget
    return None


def _shortcut(controller, key, mods, phase):
    """Activate controller's shortcut for (key, mods) in phase, as GTK does: what ran, or
    None. A mnemonic (a label's underlined letter, which the shortcut manager above the
    label runs) matches its letter with the controller's mnemonic modifier (Alt) and runs on
    its label while that is mapped and sensitive. As in GTK, a key that matches one shortcut
    only runs it exclusively: a mnemonic then activates its widget (several with the same
    letter only take the focus in turn)."""
    if controller.get_propagation_phase() != phase:
        return None
    matches = []
    for i in range(controller.get_n_items()):
        shortcut = controller.get_item(i)
        trigger = shortcut.get_trigger()
        widget = controller.get_widget()
        if isinstance(trigger, Gtk.MnemonicTrigger):
            if (mods != int(controller.get_mnemonics_modifiers())
                    or Gdk.keyval_to_lower(key) != Gdk.keyval_to_lower(trigger.get_keyval())):
                continue
            widget = _mnemonic_owner(widget, shortcut)
            if widget is None or not (widget.get_mapped() and widget.is_sensitive()):
                continue
        elif trigger is None or (key, mods) not in [
                _parse(alt) for alt in trigger.to_string().split('|')]:
            continue
        matches.append((shortcut, widget))
    flags = Gtk.ShortcutActionFlags.EXCLUSIVE if len(matches) == 1 else Gtk.ShortcutActionFlags(0)
    for shortcut, widget in matches:
        if shortcut.get_action().activate(flags, widget, shortcut.get_arguments()):
            return shortcut.get_action().to_string()
    return None


def press(window, accel):
    """Dispatch a key press through GTK's handlers, as the module says. What handled it, or
    None."""
    key, mods = _parse(accel)
    chain = []
    widget = window.get_focus() or window
    while widget is not None:
        chain.append(widget)
        widget = widget.get_parent()
    state = Gdk.ModifierType(mods)
    for widget in reversed(chain):  # capture: from the window down
        for controller in _controllers(widget):
            phase = controller.get_propagation_phase()
            if (isinstance(controller, Gtk.EventControllerKey)
                    and phase == Gtk.PropagationPhase.CAPTURE
                    and controller.emit('key-pressed', key, 0, state)):
                return f'{widget.__gtype__.name} key controller'
            if isinstance(controller, Gtk.ShortcutController):
                done = _shortcut(controller, key, mods, Gtk.PropagationPhase.CAPTURE)
                if done:
                    return done
    for widget in chain:  # bubble: from the focus up
        for controller in _controllers(widget):
            phase = controller.get_propagation_phase()
            if (isinstance(controller, Gtk.EventControllerKey) and isinstance(widget, Gtk.Popover)
                    and phase == Gtk.PropagationPhase.BUBBLE
                    and controller.emit('key-pressed', key, 0, state)):
                return f'{widget.__gtype__.name} key controller'
            if isinstance(controller, Gtk.ShortcutController):
                done = _shortcut(controller, key, mods, Gtk.PropagationPhase.BUBBLE)
                if done:
                    return done
    return None


# -- the checklist -------------------------------------------------------------------------

def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (f' ({detail})' if detail and not ok else ''))
    if not ok:
        failures.append(name)


def inside(widget, ancestor):
    return widget is not None and (widget is ancestor or widget.is_ancestor(ancestor))


def describe(widget):
    return widget.__gtype__.name if widget is not None else 'nothing'


def row_title(row):
    """The title of a page-mode sidebar row (an action row), or '' for anything else."""
    return getattr(row, 'get_title', lambda: '')() if row is not None else ''


async def key(window, accel, wait=0.25):
    """Press accel and wait; what handled it (press())."""
    handled = press(window, accel)
    await asyncio.sleep(wait)
    return handled


async def until(condition, timeout=2.0):
    """Wait, up to timeout seconds, for condition() to hold: True when it did. For what
    follows a press after a delay of the toolkit's (a button pressed by its key shows its
    pressed state for a moment before it clicks)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


def dialog_shown(window, kind):
    """The dialog of kind shown over window, inside it or as a window of its own (an
    Adw.Dialog over a window that is neither maximized nor tiled), or None."""
    shown = window.get_visible_dialog()
    if isinstance(shown, kind):
        return shown
    for toplevel in Gtk.Window.list_toplevels():
        if toplevel is not window and toplevel.get_visible():
            found = next((widget for widget in descendants(toplevel)
                          if isinstance(widget, kind)), None)
            if found is not None:
                return found
    return None


def watch(name, calls):
    """Replace app.player's command `name` with one that records its argument in calls and
    does nothing (the demo has no engine). Returns the function that puts it back."""
    def command(argument):
        calls.append(argument)
        return asyncio.sleep(0)
    setattr(app.player, name, command)
    return lambda: delattr(app.player, name)


async def walkthrough(window):
    narrow = window.split_view.get_collapsed()
    print('-- sidebar')
    await key(window, '<primary>1', 0.6)
    check('Ctrl+1 puts the focus on the selected sidebar row',
          isinstance(window.get_focus(), Gtk.ListBoxRow) and inside(window.get_focus(),
                                                                     window.sidebar))
    if narrow:
        # The page mode's sidebar is a boxed list per section. Down from a section's last
        # row moves to the next section's first (libadwaita passes the focus on) only in an
        # active window: GtkListBoxRow's focus handler reads the row's has-focus, which
        # GTK sets while the toplevel has the keyboard, and a headless window never has
        # it. The script steps over the section's end itself.
        for _ in range(2):  # Home → New, Radio
            await key(window, 'Down', 0.15)
        check('Down moves to Radio', row_title(window.get_focus()) == 'Radio',
              row_title(window.get_focus()))
        await key(window, 'Up', 0.15)
        check('Up moves back to New', row_title(window.get_focus()) == 'New',
              row_title(window.get_focus()))
        rows = window._sidebar.rows()
        next((row for row in rows if row_title(row) == 'Recently Added'), rows[0]).grab_focus()
        for _ in range(2):  # Recently Added → Artists, Albums
            await key(window, 'Down', 0.15)
        check('Down moves to Albums', row_title(window.get_focus()) == 'Albums',
              row_title(window.get_focus()))
    else:
        for _ in range(5):  # Home → New, Radio, Recently Added, Artists, Albums
            await key(window, 'Down', 0.15)
        check('Down selects Albums and shows it', window.shown == 'albums', window.shown)
        await key(window, 'Up', 0.15)
        check('Up selects Artists and shows it', window.shown == 'artists', window.shown)
        await key(window, 'Down', 0.15)
        check('Down selects Albums again', window.shown == 'albums', window.shown)
    await key(window, 'Return', 0.6)
    check('Enter shows Albums', window.shown == 'albums' and (
        not narrow or window.split_view.get_show_content()))
    print('-- grid')
    await key(window, '<primary>2', 0.6)
    grid = window.navigation_view.get_visible_page()
    check('Ctrl+2 puts the focus on a tile', inside(window.get_focus(), grid.grid_view),
          describe(window.get_focus()))
    await key(window, 'Right')
    await key(window, 'Down')
    await key(window, 'Return', 1.0)
    detail = window.navigation_view.get_visible_page()
    check('Right, Down, Enter opens an album', getattr(detail, 'item', None) is not None
          and detail.item.kind == 'album')
    check('the focus is on Play', inside(window.get_focus(), detail.play_button))
    print('-- album')
    await key(window, 'Tab')
    check('Tab: Shuffle', inside(window.get_focus(), detail.shuffle_button))
    await key(window, 'Tab')
    check('Tab: More Options', inside(window.get_focus(), detail.more_button))
    await key(window, 'Tab')
    check('Tab: the first track', inside(window.get_focus(), detail.list_view)
          and window.get_focus().__gtype__.name == 'GtkListItemWidget')
    await key(window, '<shift>ISO_Left_Tab')
    check('Shift+Tab: back to More Options', inside(window.get_focus(), detail.more_button),
          describe(window.get_focus()))
    await key(window, 'Tab')
    check('Tab: the first track again', inside(window.get_focus(), detail.list_view)
          and window.get_focus().__gtype__.name == 'GtkListItemWidget')
    requests = []
    play_request = window.play_request
    window.play_request = lambda play, start_with=None, shuffle=None: requests.append(
        start_with)
    await key(window, 'Down')
    await key(window, 'Return')
    window.play_request = play_request
    check('Down, Enter plays the second track', requests == [1], requests)
    print('-- the idle player bar')
    await key(window, '<primary>3')
    bar = window.get_focus()
    check('Ctrl+3 with nothing playing puts the focus on the bar',
          isinstance(bar, Gtk.Button) and window.player_bar.is_ancestor(bar), describe(bar))
    await key(window, 'Return')
    await until(window.bottom_sheet.get_open)
    check('Enter on it opens Now Playing', window.bottom_sheet.get_open())
    await key(window, 'Escape')
    await until(lambda: not window.bottom_sheet.get_open() and window.get_focus() is bar)
    check('Escape closes it, the focus back on the bar',
          not window.bottom_sheet.get_open() and window.get_focus() is bar,
          describe(window.get_focus()))
    app.player.apply(harness.invented_playing_state(app, lyrics=True))
    await asyncio.sleep(0.4)
    print('-- player bar')
    await key(window, '<primary>3')
    check('Ctrl+3 puts the focus on the play button',
          window.get_focus() is window.player_bar.play_button)
    # Watched at the actions: Space presses the focused button, whose own activation runs
    # its action (never the window's key handler); the window handles Ctrl+Right and Ctrl+Left.
    actions = []
    handlers = [(app.lookup_action(name), app.lookup_action(name).connect(
        'activate', lambda action, _parameter: actions.append(action.get_name())))
        for name in ('play-pause', 'next', 'previous')]
    handled = await key(window, 'space')
    await until(lambda: actions)  # the button clicks once its pressed state has shown
    check('Space presses the focused play button (play-pause)',
          actions == ['play-pause'] and handled == 'signal(activate)', (actions, handled))
    await key(window, '<primary>Right')
    await key(window, '<primary>Left')
    for action, handler in handlers:
        action.disconnect(handler)
    check('Space, Ctrl+Right, Ctrl+Left: play-pause, next, previous',
          actions == ['play-pause', 'next', 'previous'], actions)
    print('-- Now Playing')
    sheet = window.now_playing
    tabs = sheet.tab_stack.get_prev_sibling()  # the Lyrics / Up Next switcher
    await key(window, '<primary><shift>n', 0.8)
    check('Ctrl+Shift+N opens the sheet with the focus on its play button',
          window.bottom_sheet.get_open() and window.get_focus() is sheet.play_button)
    for _ in range(3):  # Next, Repeat, the tabs
        await key(window, 'Tab', 0.1)
    check('Tab reaches the Lyrics and Up Next tabs', inside(window.get_focus(), tabs),
          describe(window.get_focus()))
    await key(window, 'Right')
    await key(window, 'space')  # the toggle clicks once its pressed state has shown
    await until(lambda: sheet.tab_stack.get_visible_child_name() == 'queue')
    check('Right, Space: Up Next', sheet.tab_stack.get_visible_child_name() == 'queue')
    await key(window, 'Left')
    await key(window, 'Return')
    await until(lambda: sheet.tab_stack.get_visible_child_name() == 'lyrics')
    check('Left, Enter: Lyrics', sheet.tab_stack.get_visible_child_name() == 'lyrics')
    await key(window, 'Tab', 0.1)
    check('Tab reaches the lyrics', inside(window.get_focus(), sheet.lyrics_view.list_view),
          describe(window.get_focus()))
    seeks = []
    undo = watch('seek', seeks)
    await key(window, 'Down')
    await key(window, 'Return')
    undo()
    check('Down, Enter seeks to a line', len(seeks) == 1, seeks)
    await key(window, 'Tab', 0.1)
    check('Tab: the close button', window.get_focus() is sheet.close_button,
          describe(window.get_focus()))
    await key(window, 'Return')
    await until(lambda: not window.bottom_sheet.get_open())
    check('Enter on it closes the sheet', not window.bottom_sheet.get_open())
    await key(window, '<primary><shift>n', 0.8)
    for _ in range(3):  # Next, Repeat, the tabs
        await key(window, 'Tab', 0.1)
    await key(window, 'Right')
    await key(window, 'space')
    await until(lambda: sheet.tab_stack.get_visible_child_name() == 'queue')
    await key(window, 'Tab', 0.1)
    check('Tab reaches Up Next', inside(window.get_focus(), sheet.queue_view.list_view),
          describe(window.get_focus()))
    jumps = []
    undo = watch('queue_jump', jumps)
    await key(window, 'Down')
    await key(window, 'Return')
    undo()
    check('Down, Enter plays the next entry', jumps == [1], jumps)
    await key(window, 'Escape', 0.8)
    check('Escape closes the sheet', not window.bottom_sheet.get_open())
    print('-- back')
    await key(window, '<alt>Left', 0.8)
    check('Alt+Left goes back to Albums', window.navigation_view.get_visible_page() is grid)
    print('-- search')
    await key(window, '<primary>f', 0.8)
    search = window.navigation_view.get_visible_page()
    check('Ctrl+F shows Search with the focus in its entry',
          window.shown == 'search' and inside(window.get_focus(), search.search_entry))
    search.search_entry.set_text('tide')  # typed
    await asyncio.sleep(0.4)
    await key(window, 'Tab')
    await key(window, 'Right')
    await key(window, 'space', 0.8)
    check('Tab, Right, Space: Your Library', search.mode == 'library', search.mode)
    await key(window, '<shift>Tab')
    await key(window, 'Escape')
    check('Escape in the entry clears it', search.search_entry.get_text() == '')
    print('-- search over a pushed page, the Songs filter')
    window.open_item(app.library.albums.get_item(0))  # a result opened from Search
    await asyncio.sleep(0.6)
    await key(window, '<primary>f', 0.8)
    check('Ctrl+F over a page pushed on Search pops it and focuses the visible entry',
          window.navigation_view.get_visible_page() is search
          and inside(window.get_focus(), search.search_entry), describe(window.get_focus()))
    window.select_page('songs')
    await asyncio.sleep(0.8)
    songs = window.navigation_view.get_visible_page()
    await key(window, '<primary>f', 0.6)
    check("Ctrl+F on Songs puts the cursor in the page's filter",
          inside(window.get_focus(), songs.filter_entry), describe(window.get_focus()))
    print('-- menus')
    await key(window, 'F10', 0.6)
    menu = window.primary_menu_button.get_popover()
    check('F10 opens the main menu', menu is not None and menu.get_visible())
    await key(window, 'Escape', 0.4)
    check('Escape closes it', not menu.get_visible())
    window.select_page('albums')
    await asyncio.sleep(0.4)
    grid = window.navigation_view.get_visible_page()
    grid.sort_button.grab_focus()  # the header bar's Sort By
    await key(window, '<primary>2', 0.6)
    check("Ctrl+2 from the page's header bar moves into the grid",
          inside(window.get_focus(), grid.grid_view), describe(window.get_focus()))
    await key(window, 'Menu', 0.6)
    popover = harness.popovers(grid)
    check("Menu opens the focused tile's context menu", bool(popover))
    await key(window, 'Escape', 0.4)
    check('Escape closes it, the focus back on the tile',
          not any(each.get_visible() for each in popover)
          and inside(window.get_focus(), grid.grid_view), describe(window.get_focus()))
    actions = []
    handler = app.lookup_action('play-pause').connect(
        'activate', lambda action, _parameter: actions.append(action.get_name()))
    handled = await key(window, 'space')
    await until(lambda: actions)
    app.lookup_action('play-pause').disconnect(handler)
    check('Space on a tile plays or pauses',
          actions == ['play-pause'] and (handled or '').endswith('key controller'),
          (actions, handled, describe(window.get_focus())))
    print('-- folders')
    tree = app.library.playlist_tree()
    folder = next(node for node in tree.flat if node.kind == 'folder' and node.children)
    child = folder.children[0]
    sidebar = window._sidebar
    window.select_page(f'folder:{folder.id}')
    await asyncio.sleep(0.6)
    await key(window, '<primary>1', 0.6)
    folder_row = sidebar.row(sidebar.item_for(f'folder:{folder.id}').get_index())
    check("Ctrl+1 puts the focus on the folder's row", window.get_focus() == folder_row,
          (describe(window.get_focus()), row_title(window.get_focus()),
           window.sidebar.get_selected(), len(sidebar.rows())))
    child_item = sidebar.item_for(f'{child.kind}:{child.id}')
    await key(window, 'Right', 0.4)
    check('Right opens the folder without changing the page',
          child_item.get_visible() and window.shown == f'folder:{folder.id}',
          (child_item.get_visible(), window.shown))
    await key(window, 'Down', 0.4)
    check("Down moves to the folder's first entry",
          window.get_focus() == sidebar.row(child_item.get_index()), describe(window.get_focus()))
    await key(window, 'Left', 0.4)
    check('Left moves back to the folder', window.get_focus() == folder_row,
          describe(window.get_focus()))
    await key(window, 'Left', 0.4)
    check('Left closes the folder', not child_item.get_visible())
    print('-- Preferences')
    await key(window, '<primary>comma', 1.0)
    dialog = app._preferences
    check('Ctrl+, opens Preferences', dialog is not None)
    if dialog is not None:
        toplevel = dialog.get_root()
        # Tab from a row stays on it in a window without the keyboard (GtkListBoxRow's
        # focus handler reads has-focus), which a headless one never has: not walked.
        buttons = [widget for widget in descendants(dialog)
                   if isinstance(widget, Adw.ButtonRow)
                   or (isinstance(widget, Gtk.Button) and widget.get_label())]
        check('every labelled button in it has a mnemonic',
              buttons and all(button.get_use_underline() for button in buttons),
              [describe(button) for button in buttons if not button.get_use_underline()])
        # Clear refuses in the demo, here without the toast, which would take the next
        # Escape (a toast overlay's Escape dismisses its toast).
        clicks = []
        handler = dialog.clear_button.connect('clicked', lambda _button: clicks.append(True))
        app.refuse_in_demo = lambda: True
        handled = press(toplevel, '<alt>l')
        await until(lambda: clicks)  # the button clicks once its pressed state has shown
        del app.refuse_in_demo
        dialog.clear_button.disconnect(handler)
        check('Alt+L presses Clear', clicks == [True], (handled, clicks))
        press(toplevel, 'Escape')
        await asyncio.sleep(0.8)
        check('Escape closes it', app._preferences is None)
    print('-- Keyboard Shortcuts')
    await key(window, '<primary>question', 1.0)
    shown = dialog_shown(window, Adw.ShortcutsDialog)
    check('Ctrl+? opens Keyboard Shortcuts', shown is not None)
    if shown is not None:
        titles = {row.get_title() for row in descendants(shown)
                  if isinstance(row, Gtk.ListBoxRow) and hasattr(row, 'get_title')}
        wanted = {title for _section, items in shortcuts.sections() for title, _key in items}
        check('it lists every shortcut', titles == wanted, sorted(titles ^ wanted))
        press(shown.get_root(), 'Escape')
        await until(lambda: dialog_shown(window, Adw.ShortcutsDialog) is None)
        check('Escape closes it', dialog_shown(window, Adw.ShortcutsDialog) is None)
    print('-- a dialog over the window')
    # A dialog is inside the window only when the window is maximized or tiled (else it is
    # a window of its own, which the window's keys never reach).
    window.set_resizable(True)
    window.maximize()
    await until(window.is_maximized)
    await key(window, '<primary>comma', 1.0)
    dialog = app._preferences
    check('Preferences opens inside the maximized window',
          dialog is not None and window.get_visible_dialog() is dialog)
    if dialog is not None:
        on = [name for name in ('back', 'search', 'focus-sidebar', 'focus-content',
                                'focus-player') if window.lookup_action(name).get_enabled()]
        check("the window's keyed actions are off", not on, on)
        before = (window.shown, window.navigation_view.get_visible_page())
        actions = []
        handler = app.lookup_action('play-pause').connect(
            'activate', lambda action, _parameter: actions.append(action.get_name()))
        for accel in ('<alt>Left', '<primary>f', '<primary>1', '<primary>2', '<primary>3',
                      '<primary><shift>n', 'space'):
            await key(window, accel, 0.3)
        app.lookup_action('play-pause').disconnect(handler)
        after = (window.shown, window.navigation_view.get_visible_page())
        check('Alt+Left, Ctrl+F, Ctrl+1 to 3, Ctrl+Shift+N and Space leave the page alone',
              after == before and not window.bottom_sheet.get_open() and not actions
              and inside(window.get_focus(), dialog),
              (after[0], window.bottom_sheet.get_open(), actions, describe(window.get_focus())))
        press(window, 'Escape')
        await until(lambda: app._preferences is None)
        check('Escape closes it', app._preferences is None)
    window.unmaximize()
    await until(lambda: not window.is_maximized()
                and (window.get_width(), window.get_height()) == (width, height))
    window.set_resizable(False)


# -- names ---------------------------------------------------------------------------------

async def unnamed(label):
    process = Gio.Subprocess.new([sys.executable, os.path.abspath(__file__), '--dump'],
                                 Gio.SubprocessFlags.STDOUT_PIPE
                                 | Gio.SubprocessFlags.STDERR_SILENCE)
    _ok, out, _err = await process.communicate_utf8_async(None, None)
    lines = [line for line in (out or '').splitlines() if line]
    print(f'{label}: {len(lines)} unnamed' + ''.join(f'\n    {line}' for line in lines))
    return len(lines)


async def names(window):
    print('-- accessible names (focusable controls without one)')
    total = 0
    for page in ('home', 'albums', 'artists', 'songs', 'radio', 'all-playlists',
                 'favourite-songs'):
        window.select_page(page)
        window.split_view.set_show_content(True)
        await asyncio.sleep(1.0)
        total += await unnamed(page)
    for kind, store in (('album', app.library.albums), ('playlist', app.library.playlists),
                        ('artist', app.library.artists)):
        window.open_item(store.get_item(0))
        await asyncio.sleep(1.0)
        total += await unnamed(kind)
    window.select_page('search')
    await asyncio.sleep(0.5)
    search = window.navigation_view.get_visible_page()
    search.set_mode('library')
    search.search_entry.set_text('the')
    await asyncio.sleep(1.5)
    total += await unnamed('search')
    app.player.apply(harness.invented_playing_state(app))
    window.bottom_sheet.set_open(True)
    await asyncio.sleep(1.0)
    total += await unnamed('Now Playing')
    window.bottom_sheet.set_open(False)
    dialog = app.show_preferences()
    await asyncio.sleep(1.0)
    total += await unnamed('Preferences')
    dialog.set_visible_page_name('engine')
    await asyncio.sleep(0.5)
    total += await unnamed('Preferences, Engine')
    dialog.close()
    await asyncio.sleep(0.5)
    app.activate_action('shortcuts')
    await asyncio.sleep(1.0)
    total += await unnamed('Keyboard Shortcuts')
    print(f'{total} unnamed in all')


# -- running -------------------------------------------------------------------------------

async def run():
    try:
        while app.library.props.state != 'ready':
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)
        window = app.get_active_window()
        await walkthrough(window)
        if args.names:
            await names(window)
    except Exception as error:  # a step that raised is a failure too
        import traceback
        traceback.print_exc()
        failures.append(repr(error))
    finally:
        app.quit()


def on_activate(_app):
    app.settings.set_string('last-page', 'home')
    app.settings.set_strv('expanded-folders', [])
    app.spawn(run())


app.connect('activate', on_activate)
harness.run_app(app)
print(f'{len(failures)} failed' if failures else 'all passed')
sys.exit(1 if failures else 0)
