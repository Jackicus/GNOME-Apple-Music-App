"""The developer scripts' shared start: the installed app, on its own, off the desktop's
settings, on the demo library.

screenshot.py, a11y_check.py, scroll_test.py and bench.py each run the installed build (meson
install -C build, or scripts/run.sh, first) in-process. make_app() does what they all need:

- the installed modules on sys.path (build/install/share/apple-music) and the translations
  bound (i18n.setup, as the launcher does);
- GSettings on the memory backend with the installed schema, so nothing the script sets (window
  size, last-page, signed-in) reaches the desktop's settings; engine-autostart off, so a script
  never starts Chrome, even one run without the demo library;
- the demo library: build/demo, generated first when it is missing, unless APPLE_MUSIC_CACHE
  names another (the app's --demo reads it), passed as --demo by run_app();
- gi's versions, the gresource registered, main.Application under an app ID of its own
  (io.github.jackicus.AppleMusic.<suffix>, NON_UNIQUE: beside a running app), asyncio on the
  GLib loop (main.use_glib_event_loop());
- at startup: the colour scheme forced dark (or light), animations off unless asked for, and
  with `stock_look` stock GNOME's icon theme and font (Adwaita, Adwaita Sans 11) instead of the
  desktop's, which GTK otherwise takes from the Settings portal; the installed icons findable
  (installed_icon() names the app's); each window made non-resizable, a fixed size a tiling
  window manager leaves alone. The version is the project's (build/meson-info).

Call make_app() before importing Gtk: importing it starts GTK, which may read the settings
schemas once and for all. invented_playing_state() is what the Player shows while the demo's
first album plays; popovers() finds the popovers shown under a widget.
"""

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = os.path.join(ROOT, 'build', 'install')
PKGDATADIR = os.path.join(PREFIX, 'share', 'apple-music')
GRESOURCE = os.path.join(PKGDATADIR, 'applemusic.gresource')
SCHEMA_DIR = os.path.join(PREFIX, 'share', 'glib-2.0', 'schemas')
LOCALEDIR = os.path.join(PREFIX, 'share', 'locale')
ICONS = os.path.join(PREFIX, 'share', 'icons')
DEMO_DIR = os.path.join(ROOT, 'build', 'demo')  # what the development launcher passes
BASE_ID = 'io.github.jackicus.AppleMusic'

# invented_playing_state()'s artwork URL: the album's cover is copied to where
# Artwork.fetch_remote would put this URL's 640 px image, so nothing is fetched.
DEMO_ART_URL = 'https://example.invalid/demo-art/{w}x{h}bb.jpg'
PLAYING_POSITION = 65.0  # seconds in: a line of the lyrics fixture is current, mid-song


def ensure_demo_library():
    """Generate build/demo when it is missing and APPLE_MUSIC_CACHE names no other library."""
    if os.environ.get('APPLE_MUSIC_CACHE') or os.path.exists(
            os.path.join(DEMO_DIR, 'library.json')):
        return
    subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'demo_library.py'),
                    '--cache', DEMO_DIR], check=True)


def project_version():
    """The version meson.build declares, as the build's introspection records it."""
    try:
        with open(os.path.join(ROOT, 'build', 'meson-info', 'intro-projectinfo.json'),
                  encoding='utf-8') as file:
            return json.load(file)['version']
    except (OSError, ValueError, KeyError):
        return '0.0.0'


def installed_icon():
    """The installed app icon's name (the .Devel one in the development profile), or None."""
    apps = os.path.join(ICONS, 'hicolor', 'scalable', 'apps')
    names = sorted(name for name in os.listdir(apps)) if os.path.isdir(apps) else []
    return next((name.removesuffix('.svg') for name in names
                 if name.startswith(BASE_ID) and name.endswith('.svg')), None)


def make_app(suffix, demo=True, light=False, animations=False, stock_look=True, size=None,
             demo_dir=DEMO_DIR, name=None):
    """The installed app's main.Application, set up as the module says, not yet run (run_app()
    runs it). `size` (width, height) is written to the window-size settings; `name` is the
    program name (argv[0] and GLib's prgname), the suffix lower-cased by default."""
    if not os.path.exists(GRESOURCE):
        sys.exit(f'{os.path.basename(sys.argv[0])}: no installed build in {PREFIX}: '
                 'run meson install -C build first')
    if demo:
        ensure_demo_library()
    os.environ['GSETTINGS_SCHEMA_DIR'] = SCHEMA_DIR
    os.environ['GSETTINGS_BACKEND'] = 'memory'  # never the desktop's settings
    sys.path.insert(1, PKGDATADIR)
    from applemusic import i18n

    i18n.setup(LOCALEDIR)

    import gi

    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk

    Gio.Resource.load(GRESOURCE)._register()
    from applemusic import main

    name = name or suffix.lower()
    GLib.set_prgname(name)
    app = main.Application(project_version(), f'{BASE_ID}.{suffix}', BASE_ID, 'default',
                           demo_dir)
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.harness_argv = [name] + (['--demo'] if demo else [])
    app.settings.set_boolean('engine-autostart', False)
    if size is not None:
        app.settings.set_int('window-width', size[0])
        app.settings.set_int('window-height', size[1])

    def on_startup(_app):
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_LIGHT if light else Adw.ColorScheme.FORCE_DARK)
        settings = Gtk.Settings.get_default()
        if not animations:
            # Pages arrive at once: a transition the compositor starves of frames (an
            # unfocused window) could still be sliding when a script looks.
            settings.set_property('gtk-enable-animations', False)
        if stock_look:
            settings.set_property('gtk-icon-theme-name', 'Adwaita')
            settings.set_property('gtk-font-name', 'Adwaita Sans 11')
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(ICONS)

    def on_window_added(_app, window):
        window.set_resizable(False)  # a tiling window manager leaves a fixed size alone

    app.connect('startup', on_startup)
    app.connect('window-added', on_window_added)
    main.use_glib_event_loop()  # as main.main() does, so app.spawn() works
    return app


def run_app(app, *options):
    """Run the app from make_app() (with --demo when it was made for the demo library, and
    `options`, e.g. '--debug'); its exit status."""
    return app.run(app.harness_argv + list(options))


def invented_playing_state(app, lyrics=False):
    """A Player.apply() dict: the demo library's first album as the queue, its first track
    playing 65 s in, the album's cover as the artwork, and with `lyrics` the synced lyrics of
    tests/fixtures/lyrics.json. The demo cannot play: this is what the Player would show."""
    from applemusic.backend import config
    from applemusic.widgets.artwork import remote_art_path

    if not app.demo:
        sys.exit('harness: an invented playing state needs the demo library')
    album = app.library.albums.get_item(0)
    if album is None:
        sys.exit('harness: the demo library has no album')
    entries = [dict(entry) for group in album.raw.get('groups') or []
               for entry in group.get('entries') or []]
    if not entries:
        sys.exit('harness: the demo album has no tracks')
    for position, entry in enumerate(entries):
        entry['artUrl'] = DEMO_ART_URL
        entry['index'] = position
    art_path = remote_art_path(DEMO_ART_URL, config.COVER_SIZE)
    if album.art and art_path and not os.path.exists(art_path):
        os.makedirs(os.path.dirname(art_path), exist_ok=True)
        shutil.copyfile(album.art, art_path)
    state = {
        'state': 'playing', 'track': entries[0], 'position': PLAYING_POSITION,
        'duration': entries[0].get('durationMs', 0) / 1000, 'shuffle': 'off',
        'repeat': 'none', 'volume': 0.7, 'queue': {'index': 0, 'items': entries},
    }
    if lyrics:
        with open(os.path.join(ROOT, 'tests', 'fixtures', 'lyrics.json'),
                  encoding='utf-8') as file:
            state['lyrics'] = json.load(file)
    return state


def popovers(widget):
    """The popovers shown under widget (each a child of the widget it points from), depth
    first."""
    from gi.repository import Gtk

    found = []
    if isinstance(widget, Gtk.Popover) and widget.get_visible():
        found.append(widget)
    child = widget.get_first_child()
    while child is not None:
        found.extend(popovers(child))
        child = child.get_next_sibling()
    return found
