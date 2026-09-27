# Apple Music for GNOME

A native GNOME client for Apple Music. GTK 4.22, libadwaita 1.9, Python 3.14 with PyGObject 3.56,
Blueprint for UI, Meson, gettext. GPL-2.0-or-later. App ID `io.github.jackicus.AppleMusic`
(`.Devel` under `-Dprofile=development`); resource base path `/io/github/jackicus/AppleMusic`.
It should feel like a GNOME core app (Nautilus, Music, Settings) while laying out its pages like
the Apple Music web player. The phased plan, one ready-to-paste prompt per session, is in
`prompts.md`. This file is what every session needs regardless of phase: what exists and the rules.

## The one hard constraint

Apple Music streams are Widevine-protected. WebKitGTK cannot play them and there is no public
streaming API, so the app cannot embed music.apple.com in a WebView. The playback engine is Google
Chrome (the real build: it ships Widevine, Chromium does not), started by the app with a private
profile and `--remote-debugging-port` on 127.0.0.1, showing music.apple.com. The app drives Apple's
own MusicKit JS object in that page over the DevTools protocol (CDP) through an injected `bridge.js`.
Chrome is visible once for sign-in and headless (`--headless=new`) afterwards; audio comes out of
Chrome. Jack proved this in a GNOME Shell extension whose backend
(`~/Projects/GNOME-Extensions/GNOME-Apple-Music-Library/src/backend/`, README there) is vendored
into `src/backend/`. This app is one long-running process, so it keeps a single persistent,
asynchronous CDP connection instead of the extension's process-per-command.

## Architecture

Parts marked `*` exist only once their phase in `prompts.md` is done.

```
GTK main thread = GLib main loop = asyncio loop (gi.events.GLibEventLoopPolicy)*
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Window: AdwToastOverlay > AdwNavigationSplitView                                 │
│   sidebar: AdwSidebar built from sections.py (+ playlists and folders*)          │
│   content: GtkStack of placeholder AdwStatusPages (→ AdwNavigationView*)         │
│   PlayerBar stub at the bottom of the content (→ AdwBottomSheet bottom bar*)     │
│ Library*: Item/Track GObjects in Gio.ListStores, loaded from library.json        │
│ Engine*: Chrome (Gio.Subprocess) + async CDP client ─► bridge.js ─► MusicKit     │
│   MusicKit events ─► Player* state ─► PlayerBar / Now Playing* / MPRIS* service  │
│ Blocking work (JSON parse, image decode, artwork HTTP) ─► asyncio.to_thread      │
└──────────────────────────────────────────────────────────────────────────────────┘
Disk*: $XDG_CACHE_HOME/apple-music/{library.json, art/, thumb/, remote-art/, lyrics/, items/}
       $XDG_DATA_HOME/apple-music/chrome (the Chrome profile)      GSettings: one schema
```

## Layout, and where new things go

```
meson.build, meson.options     project; -Dprofile=development → .Devel ID, version gets the git rev
src/apple-music.in             launcher configured by Meson: gettext, loads the gresource, main.main()
src/main.py                    Application: app.* actions (quit, about, shortcuts), GSettings, dialogs
src/window.py + window.blp     Window: split view, sidebar, content stack, toasts, window-state memory
src/sections.py                the fixed sidebar destinations (key, title, icon), grouped as on the web
src/player_bar.py + .blp       $AppleMusicPlayerBar, a stub transport bar
src/style.css                  auto-loaded app CSS: accent colour and a few small classes
src/icons/*-symbolic.svg       bundled icons, aliased into icons/scalable/actions/ by the gresource
src/applemusic.gresource.xml   compiled .ui files, style.css, icons
src/meson.build                blueprint list, gresource, install_data list of .py files
data/                          desktop, metainfo, gschema, app icons; Meson tests validate them
po/                            gettext; POTFILES.in must list every file with translatable strings
scripts/run.sh check.sh screenshot.py
build-aux/flatpak/*.Devel.json Flatpak manifest, GNOME 50 runtime (not installed locally)
subprojects/blueprint-compiler.wrap   fallback when blueprint-compiler is not on PATH
```

New code goes in: `src/backend/` (engine, CDP, sync; imports no GTK), `src/library.py` (data
model), `src/pages/<name>.py` + `.blp` (one module per sidebar destination or detail page),
`src/widgets/` (reusable: tiles, shelves, track rows, artwork loader), `src/dialogs/` (sign-in,
preferences), `tests/` (stdlib `unittest`), `scripts/` (developer tools). Anything installed
outside the Python module goes in `data/`.

## Commands

```
scripts/run.sh [args]     meson setup (dev profile, prefix build/install) + install + run
scripts/check.sh          py_compile, meson compile, meson tests (desktop/metainfo/schema validation)
scripts/screenshot.py [out.png] [--light] [--size WxH] [--page KEY]
                          renders the real window to a PNG; needs a display and a prior run.sh/install;
                          dark by default; GSettings go to a memory backend
python3 -m unittest discover -s tests -v      unit tests (tests/ arrives in phase 1, in check.sh too)
meson setup build --prefix=/usr && meson install -C build      system install, release profile
```

## Conventions

- UI is Blueprint (`.blp`, compiled to `.ui` by Meson). Widgets are `Gtk.Template` classes with
  `__gtype_name__ = 'AppleMusic<Name>'`, `Gtk.Template.Child()` for named children,
  `@Gtk.Template.Callback()` for handlers named in the `.blp`.
- Every user-visible string goes through `_()`: `from gettext import gettext as _` in Python,
  `_("…")` in Blueprint. New files with strings go in `po/POTFILES.in`. Source strings keep the
  scaffold's en-GB spelling ("Favourite").
- GNOME HIG and libadwaita first: `AdwStatusPage`, `AdwSpinner`, `AdwToast`, `AdwDialog`,
  `AdwPreferencesDialog`, style classes (`card`, `flat`, `circular`, `dim-label`, `heading`,
  `title-1`…`title-4`, `caption`, `boxed-list`) before any CSS. CSS lives only in `style.css`.
- Never block the main loop: no `time.sleep`, synchronous sockets or HTTP, `json.load` of the
  library, or image decoding on it. Use `await asyncio.to_thread(...)`; Gio async methods are
  awaitable under the GLib loop (`await file.load_contents_async()`). Touch GTK/GObject only from
  the main thread (creating a `Gdk.Texture` in a thread is fine; it is immutable).
- Collections are models: `Gtk.GridView`/`Gtk.ListView`/`Gtk.ColumnView` over `Gio.ListStore`
  (with `Gtk.SortListModel`/`Gtk.FilterListModel`) and `Gtk.SignalListItemFactory`. Never a
  `Gtk.Box` of hundreds of widgets.
- The backend is reached only through the `Engine` object (coroutines: `await app.engine.play(...)`),
  spawned from signal handlers with `app.spawn(coro)`. Errors are `EngineError(code)` with the
  README's codes (`engine-down`, `not-signed-in`, `api`, `timeout`); the UI shows a toast, never a
  traceback. Library reads are synchronous in-memory models.
- Logging: Python `logging`, `log = logging.getLogger(__name__)`; configured once in `main.py`
  (INFO to stderr; DEBUG with `--debug` or `APPLE_MUSIC_DEBUG=1`). No `print` in app code.
- Settings: one schema `io.github.jackicus.AppleMusic` for both profiles; new keys go in
  `data/…gschema.xml` with a summary, and are read through `app.settings`.
- Actions: `app.*` in `main.py`, `win.*` in `window.py`; accelerators via `set_accels_for_action`;
  every shortcut also appears in the shortcuts dialog.
- Style: 4-space Python, single quotes, no type-annotation ceremony, a docstring where a module or
  function is not obvious. New `.py` files go in `src/meson.build`'s `install_data` list.

## Verifying a change

1. `scripts/check.sh` passes (it grows: tests, lint).
2. Anything visual: `scripts/run.sh` builds, then `scripts/screenshot.py build/shot.png --page KEY`
   and look at the PNG with the Read tool; also `--light`, and `--size 400x700` for anything adaptive.
3. Backend or model changes get a unit test in `tests/`.
4. The real engine is only exercised when the phase needs it, and never leaves data in the repo.

## Privacy

No real account data in the repo, tests, docs or committed screenshots: no names, playlist titles,
IDs, tokens, artwork, or contents of the Chrome profile. Fixtures and the demo library are invented.
Live data lives only under `$XDG_CACHE_HOME/apple-music` and `$XDG_DATA_HOME/apple-music`, both
outside the repo; `build/` is git-ignored. Screenshots for the metainfo come from the demo library.

## Things worth knowing

- `AdwSidebar` (libadwaita 1.9): `AdwSidebarSection`s (optional title) hold `AdwSidebarItem`s,
  which are GObjects, not widgets: no children or expanders, but `suffix` (a widget), `subtitle`,
  `visible`, `enabled`, `icon-name`/`icon-paintable`, and the class is derivable. `selected` is the
  index across all sections (`item.get_index()`); `selected-item` is the object; `activated` fires
  on click (the scaffold uses it to show the content pane when collapsed).
  `AdwSidebarSection.bind_model(model, create_func)` mirrors a `Gio.ListModel`; `menu-model` plus
  the `setup-menu` signal give per-item context menus; `setup_drop_target()` accepts drops;
  `mode: page` turns it into boxed lists for the collapsed layout (not yet set in the scaffold).
- Blueprint 0.22: `template $AppleMusicWindow: Adw.ApplicationWindow {}`; a custom widget used in a
  template (`$AppleMusicPlayerBar`) needs its Python class imported before the template is built
  (hence `from .player_bar import PlayerBar  # noqa: F401` in `window.py`); `[top]`/`[bottom]`/`[end]`
  slots; `styles ["flat"]`; `Adw.Breakpoint { condition ("max-width: 640sp") setters { … } }`;
  `menu primary_menu { … }` at top level; handlers `clicked => $on_clicked();`; bindings
  `label: bind item.title;`. Each new `.blp` goes in `src/meson.build`'s blueprint list and in
  `applemusic.gresource.xml` as `.ui`.
- The source tree is not importable as `applemusic`; the launcher imports it from
  `build/install/share/apple-music/applemusic`, where the gresource also lives. Tests register
  `src/` under that name (phase 1).
- Icons in `src/icons/` resolve by `icon-name` through the resource alias; symbolic SVGs use a `#222`
  fill and are recoloured. Adwaita no longer ships some legacy names (`emblem-favorite-symbolic` is
  gone); bundle anything not in `/usr/share/icons/Adwaita/symbolic/`.
- `screenshot.py` uses a `.Screenshot` app ID and renders after 1.2 s, so content that arrives later
  needs a longer delay; it prints a harmless at-spi warning.
- The dev build shares the release schema and resource path; only the app ID, desktop file and icons
  differ. `run.sh` sets `GSETTINGS_SCHEMA_DIR` and `XDG_DATA_DIRS` to `build/install`.
- Chrome: `google-chrome-stable` 154 is installed. Its MPRIS player is
  `org.mpris.MediaPlayer2.chromium.instance<pid>`, disabled by
  `--disable-features=HardwareMediaKeyHandling`. The extension's engine uses port 9227 and profile
  `$XDG_DATA_HOME/apple-music-library/chrome`; this app uses 9228 and `$XDG_DATA_HOME/apple-music/chrome`
  (the `.Devel` build 9229 and `chrome-devel`) so they can run side by side, which also means a
  separate sign-in per profile.
- Python 3.14 deprecates `asyncio.set_event_loop_policy` (removal in 3.16), but it is still how
  PyGObject 3.56 puts asyncio on the GLib loop; the DeprecationWarning is expected and filtered.
- No commits yet: the repo is initialised and empty until phase 1.
