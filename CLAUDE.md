# Music Sleeve

A native GNOME client for Apple Music: Python and PyGObject, GTK 4 and libadwaita, Blueprint for
the UI, Meson, gettext. GPL-2.0-or-later. App ID `io.github.jackicus.MusicSleeve`
(`io.github.jackicus.MusicSleeve.Devel` with `-Dprofile=development`), resource base path
`/io/github/jackicus/MusicSleeve`. It should feel like a GNOME core app (Music, Nautilus,
Settings); its pages carry a streaming player's structure, its look is GNOME's, not Apple's.

Developed on GTK 4.22, libadwaita 1.9, Python 3.14 and PyGObject 3.56. The minimums are Python
3.12 (`pyproject.toml`) and, in `meson.build`, GTK 4.20, GLib 2.84, libadwaita 1.9, PyGObject
3.50, Meson 1.2: use no newer API without raising them there and in README.md and
CONTRIBUTING.md. This file holds rules and pointers: a module's API is in its docstring, area
rules in `.claude/rules/`, measurements and lab notes in `docs/notes.md`, history in git.

## The one hard constraint

Apple Music streams are Widevine-protected. WebKitGTK cannot play them and Apple offers no public
streaming API, so the app cannot embed music.apple.com in a WebView. The playback engine is Google
Chrome (the real build, which ships Widevine; Chromium does not), started by the app with a private
profile, showing music.apple.com. The app drives Apple's own MusicKit JS in that page over the
Chrome DevTools Protocol (CDP) on a pipe (no port), through an injected `src/backend/bridge.js`,
and hears MusicKit's events through a CDP binding. Chrome is visible once, for sign-in, and
headless after that (unless `engine-headless` is off); the sound comes out of Chrome. One
long-running process, one persistent, asynchronous CDP connection. `src/backend/` is a fork,
owned here, of the backend of the owner's GNOME Shell extension.

## Architecture

One thread runs GTK and asyncio: the GLib main loop is the asyncio loop
(`gi.events.GLibEventLoopPolicy`, set by `main.use_glib_event_loop()`). Blocking work goes to
`asyncio.to_thread`; the library file is parsed in a thread of its own.

```
Application (main.py)    app.settings, .library, .engine, .player, .mpris, .library_sync, .demo;
│                        app.spawn(coro), app.toast(), app.report(error), app.player_command(coro)
├─ Window (window.py)    AdwBottomSheet > AdwToastOverlay > AdwNavigationSplitView; its keys
│  │                     as keyboard.py decides; the banners under each page's header bar
│  ├─ sidebar            AdwSidebar run by sidebar_view.SidebarController: sections.py's
│  │                     destinations, then the Playlists (sidebar.py over playlist_tree())
│  ├─ content            AdwNavigationView: a sidebar item replaces the stack with its root page
│  │                     (pages/, made on first visit); tiles and rows push pages
│  ├─ bottom bar, sheet  PlayerBar (player_bar.py), NowPlayingSheet (widgets/now_playing.py)
│  └─ item_actions       win.item-* actions, context menus (actions.py, widgets/context_menu.py)
├─ Library (library.py)  Item/Track GObjects in Gio.ListStores from the cache's library.json;
│                        sync.py fetches and writes it; reload() keeps objects, notifies changes
├─ Engine (engine.py)    Chrome (Gio.Subprocess, CDP on its pipe) + CDPClient ─► bridge.js ─►
│                        MusicKit; commands are coroutines; MusicKit events re-emitted as `event`
├─ Player (player.py)    what is playing, as GObject properties fed by engine events; the bar,
│                        the sheet and MPRIS follow its notify:: signals; nothing polls MusicKit
└─ Mpris (mpris.py)      org.mpris.MediaPlayer2.<app id> on the session bus
```

Pages reach the window through its seams (`self.get_root()`), not its internals: `open_item()`,
`open_shelf()`, `open_songs()`, `play_request(play, start_with=None, shuffle=None)` (every "play
this"), `add_toast()`, `item_actions`; the Application through `pages.app()`.

On disk: `$XDG_CACHE_HOME/apple-music/` (library.json, art/, thumb/, remote-art/, lyrics/,
day-long page answers) and `$XDG_DATA_HOME/apple-music/chrome/` (the Chrome profile), with
`apple-music-devel/` and `chrome-devel/` for .Devel; GSettings, one schema for both builds, the
sign-in's keys each build's own (`account_key()`).

## Where things go

```
src/main.py, src/window.py      the Application and the Window
src/*.py                        services and logic (engine, player, mpris, sync, account, cache,
                                background, actions, errors, i18n, lyrics, remote, shortcuts,
                                keyboard, sidebar, sections, timing; new ones go here), and the
                                window's parts (player_bar, sidebar_view)
src/library.py                  the data model: GLib, GObject and Gio only, no GTK
src/pages/<name>.py + .blp      one per destination or pushed page (PAGES in pages/__init__.py)
src/widgets/                    reusable widgets: tiles, shelves, rows, covers, transport, artwork
src/dialogs/                    sign-in, preferences, keyboard shortcuts, about
src/backend/                    Chrome, the CDP client, bridge.js, API normalising: stdlib and
                                asyncio only, never gi (tests/test_backend.py checks)
src/style.css, src/icons/       the only stylesheet; bundled symbolic icons
data/                           desktop file, metainfo, gschema, app icons, metainfo screenshots
po/                             translations (POTFILES.in)
tests/                          stdlib unittest; tests/gtk.py for widget tests; fixtures/ (invented)
scripts/, build-aux/, docs/     developer tools; packaging; documentation for people
```

A new file has to be listed, or tests/test_build_lists.py fails: a `.py` in `src/meson.build`'s
install list for its directory; a `.blp` in `src/meson.build`'s blueprint list and its `.ui` (bare
name) in `src/applemusic.gresource.xml`; a file with user-visible strings in `po/POTFILES.in`.

## Commands

Anything that opens a window (the app, the widget tests, the scripts that show one) runs
through `scripts/headless.sh COMMAND…`, a private invisible display, never on the desktop. It
hands Chrome the desktop's session bus, so a live check there keeps the keyring and the sign-in.

```
scripts/demo.sh [args]       build the development profile into build/install and run it on the
                             invented library in build/demo: no Chrome, no account (UI work)
scripts/run.sh [args]        the same on the real cache; autostart starts Chrome on the signed-in
                             profile. --debug (or APPLE_MUSIC_DEBUG=1) logs at DEBUG
scripts/headless.sh scripts/check.sh    compileall, ruff, meson compile, unit tests,
                             desktop/metainfo/schema validation; prints `check: ok`
scripts/headless.sh python3 -m unittest discover -s tests    the unit tests alone
scripts/headless.sh scripts/screenshot.py build/shot.png --demo [--page KEY]
                             [--open album:first] [--light] [--size WxH]: a PNG (--help for more)
scripts/headless.sh scripts/a11y_check.py [--size 360x640] [--names]   the keyboard checklist
scripts/demo_library.py --cache build/demo-big --albums 3000 --tracks 40000   a big invented
                             library; then scripts/bench.py and scripts/scroll_test.py measure on it
scripts/am.py --help         the engine, no GUI: its own Chrome on the real profile, or --attach
meson setup _build --prefix=/usr …   a system install, never into build/ (docs/release.md)
```

## Conventions

- **Never block the main loop**: it is the UI thread and the asyncio loop. No `time.sleep`,
  synchronous sockets or HTTP, `json.load` of the library, image decoding or large file reads on
  it: `await asyncio.to_thread(...)`, and Gio's `*_async` methods, which are awaitable here. Touch
  widgets only on the main thread (an immutable `Gdk.Texture` may be made in a thread).
- **Async**: start coroutines from signal handlers with `app.spawn(coro)` (it keeps a reference,
  logs the exception, returns the task). `GLib.idle_add`/`timeout_add` defer UI work past a
  frame or a layout, not async work. Main-thread work in chunks yields with `await
  yield_to_frames()` (library.py): asyncio runs above GTK's redraw, so `sleep(0)` paints nothing.
- **Chrome and MusicKit only through `app.engine`**: engine.py is the one module that imports
  `backend.chrome` and `backend.client`; the rest of the app may import the backend's pure parts
  (`api`, `config`, `errors`, `normalize`, `store`). Playback goes through `app.player`.
- **Errors**: engine failures are `EngineError(code)`, codes in `src/backend/errors.py`. The
  user sees a plain-text toast (`app.report(error)`: `errors.error_message()`'s sentence and
  button for the code, the detail in the log; `app.toast()` makes every toast), no traceback.
- **UI**: widget templates are Blueprint, `Gtk.Template` classes with `__gtype_name__ =
  'AppleMusic<Name>'`. libadwaita widgets and style classes before custom CSS, CSS only in
  `src/style.css`; follow the GNOME HIG. A widget that can be dropped (a pushed page, a shelf, a
  dialog, a row) connects its children's signals with `widgets.util.connect_weak()`, never to
  its own bound method, and has no `=> $handler()` in its `.blp`, or it is never freed
  (tests/test_page_lifetime.py).
- **Collections are list models**: `Gtk.GridView`/`ListView`/`ColumnView` over a
  `Gio.ListStore` with a `Gtk.SignalListItemFactory`, not a box of widgets that grows with the
  library. Rows and tiles are recycled, so bind has to be cheap (`.claude/rules/performance.md`).
- **Strings**: every user-visible string goes through `_()` (or `ngettext`, `C_`), `_("…")` in
  Blueprint, and its file is in `po/POTFILES.in`. Source strings are en-GB ("Favourite").
- **Settings**: one schema for both builds; a new key goes in the gschema with a summary. Read
  it through `app.settings`, the account's (`main.ACCOUNT_KEYS`) by `app.account_key()`.
- **Actions and shortcuts**: `app.*` in main.py, `win.*` in window.py (the item actions in
  actions.py). Every shortcut goes in `src/shortcuts.py`, which feeds the accelerators and the
  Keyboard Shortcuts dialog (tests/test_shortcuts.py). A bare key or an editing chord (Space,
  Ctrl+Left) is never an application accelerator (GTK runs those before the focused widget):
  the window handles them as `src/keyboard.py` decides (Space belongs to a focused button).
- **Quitting**: activate `app.quit`, never `Gio.Application.quit()` directly: the quit path
  saves the window state and stops Chrome cleanly, where `do_shutdown` could only SIGKILL it.
- **Demo mode** (`--demo`, `app.demo`): no engine (commands raise `engine-down`), no MPRIS, its
  own settings (`settings.ini`); nothing in it may start Chrome or read the real cache.
- **Logging**: `log = logging.getLogger(__name__)`, set up once in main.py; no `print` in `src/`.
- **Style**: `ruff check .` must be clean (`pyproject.toml`, no per-file exemptions). Beyond
  ruff: 4-space indents, no type annotations, a docstring where a module or function is not
  obvious, and comments that describe the code as it is (no phase numbers, review IDs or plans).
  Every source file starts with the two SPDX lines (tests/test_spdx.py).
- **Tests**: stdlib `unittest` in `tests/test_<module>.py`. Keep logic in non-widget classes and
  pure functions, tested with stand-ins; widget tests go through `tests/gtk.py`. Backend, model
  and service changes come with a test.

## Privacy, the real account and Apple's marks

- The repository is public. No real account data in code, tests, fixtures, docs, logs or
  committed screenshots: no names, playlist or song titles, library IDs, tokens, artwork, or
  anything from the cache or the Chrome profile. Fixtures and the demo library are invented.
- Live data stays outside the repo: `$XDG_CACHE_HOME/apple-music` (and `apple-music-devel`),
  `$XDG_DATA_HOME/apple-music` and the app's GSettings (`account-name`, `last-page` and
  `expanded-folders`, and their `-devel` twins, hold names and IDs). `build/` is git-ignored.
- `scripts/run.sh`, `scripts/am.py` and `scripts/screenshot.py` without `--demo` use the real
  profile or cache: use `scripts/demo.sh` and `--demo` unless the task needs the real engine.
  `APPLE_MUSIC_DEBUG_PORT` opens the signed-in session to every local program: live checks only.
- Don't write to a real Apple account (love, add to library or a playlist, sign out, clear the
  cache) unless the task asks for it, and put back anything you change.
- **Apple's marks are Apple's**: the name and icon are the app's own; "Apple Music" names the
  service, never this app; Apple's logos, fonts and artwork are never used (packaging.md).

## Verifying and landing a change

1. `scripts/check.sh` passes. CI (`.github/workflows/ci.yml`) runs it on every push to main and
   every pull request, in an Arch Linux container under Xvfb with software rendering, no
   accessibility bus and no Chrome: widget tests must pass there.
2. Anything visible: screenshots of the demo library (the `screenshots` skill), each looked at
   with the Read tool; also `--light`, and `--size 360x640` (the narrowest width) if adaptive.
3. Keyboard or accessibility changes, or a new page: `scripts/a11y_check.py`, also with
   `--size 360x640` and `--names`; a new page or key gets a step there.
4. The startup path, a page's first build or a list's bind: `scripts/bench.py` and
   `scripts/scroll_test.py` on build/demo-big, before and after (headless runs only).
5. The real engine only when the change needs it, and it never leaves data in the repo.
6. `main` is protected and takes no direct push, from anyone. Work on a branch, open a pull
   request, let CI go green, then `gh pr merge --squash --delete-branch`: no approving review is
   required, so a session merges its own. Anything but a trivial fix gets an issue first.

## More

- `.claude/rules/<area>.md` (`engine`, `library`, `ui`, `gtk-notes`, `sidebar`, `performance`,
  `playback`, `tests`, `scripts`, `packaging`): each area's rules, loaded when the Read tool
  reads a file of that area; working through a shell, read the area's file first.
- Skills in `.claude/skills/`: `review-pass`, `fix-bug`, `hig-polish`, `performance-pass`,
  `screenshots`, `live-engine-check`.
- `docs/`: `architecture.md` (how the parts fit), `decisions.md` (settled decisions and why),
  `notes.md` (measurements), `accessibility.md` (the keyboard walkthrough), `release.md`,
  `user-guide.md` (for users), `history/build-plan.md` (history); `src/backend/README.md` (bridge).
- A change that makes a line in these files wrong fixes that line in the same commit
  (tests/test_docs.py checks the paths and keys they name, and this file's 200 lines).
