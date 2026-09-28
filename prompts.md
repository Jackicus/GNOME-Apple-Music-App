# Plan: Apple Music for GNOME

One phase per Claude Code session. Paste the phase's prompt; `CLAUDE.md` is loaded automatically.
When a phase is done, tick it in the list below (`[x]`, with the date) and note anything the next
phase must know under that phase's heading. If a phase turns out to be more than one session, split
it here before continuing rather than leaving it half done. Every phase leaves the app building,
running and better than before.

## Phases at a glance

- [x] 1. Foundations: git, tests, lint, logging, asyncio on GLib (2026-09-27)
- [x] 2. Vendor the backend (cdp.py, bridge.js, sync.py, demo generator, tests) (2026-09-27)
- [x] 3. Library model and demo mode (2026-09-27)
- [x] 4. Grid pages (Albums, Artists, Recently Added, All Playlists, Music Videos) and artwork (2026-09-27)
- [x] 5. Songs page (GtkColumnView) (2026-09-27)
- [x] 6. Detail pages: album, playlist, artist (2026-09-27)
- [x] 7. Shelves: Home and Radio from the cache (2026-09-27)
- [x] 8. Sidebar: playlists and folders (2026-09-27)
- [x] 9. Backend layer: Chrome process, async CDP client, bridge events, debug CLI (2026-09-27)
- [x] 10. Engine in the app: lifecycle, sign-in, account (2026-09-27)
- [x] 11. Library sync and artwork cache (2026-09-28)
- [x] 12. Playback: Player state and the player bar (2026-09-28)
- [x] 13. MPRIS (2026-09-28)
- [x] 14. Now Playing sheet, queue, lyrics (2026-09-28)
- [x] 15. Search, New and Made for You (2026-09-28)
- [x] 16. Context menus and actions (play next, love, add, drag to playlist) (2026-09-28)
- [ ] 17. Preferences
- [ ] 18. Keyboard navigation and accessibility
- [ ] 19. Performance pass
- [ ] 20. Packaging and release

## Settled questions (Jack agreed to all four recommendations, 2026-09-27)

1. **Distribution.** Recommendation: native install is the release path (`meson install`, plus an
   AUR PKGBUILD since this is an Arch-family machine). The Flatpak manifest stays for development
   only, with `--talk-name=org.freedesktop.Flatpak` so the sandboxed app can `flatpak-spawn --host`
   the host's Chrome. Flathub is out of scope: it would refuse both the host-Chrome dependency and
   the trademark in the name/ID. The ID `io.github.jackicus.AppleMusic` stays.
2. **Chrome profile.** Recommendation: the app gets its own profile and port (`$XDG_DATA_HOME/
   apple-music/chrome`, 9228; the `.Devel` build `chrome-devel`, 9229), so it never fights the
   extension's Chrome over a profile lock. Cost: signing in again, once per profile, and two Chromes
   if both run. Alternative: `APPLE_MUSIC_PROFILE` pointed at the extension's profile, which reuses
   its sign-in but means only one of the two can run at a time.
3. **Player placement.** Recommendation: a full-width bar at the bottom of the window (GNOME Music,
   Decibels) with Now Playing as an `AdwBottomSheet` sheet that slides up over the content, with
   Lyrics and Up Next as tabs inside it. Apple's web player puts the player at the top; the bottom
   keeps the header bar for page titles and back buttons and is what GNOME users expect.
4. **Playlist folders.** Recommendation: folders are sidebar items with a `folder-symbolic` icon and
   a disclosure arrow as the suffix; activating one toggles its playlists (hidden via
   `AdwSidebarItem:visible`) and opens the folder's page in the content. `AdwSidebar` cannot indent,
   so nested items rely on order and the arrow. Alternative: one titled `AdwSidebarSection` per
   folder, always expanded, no nesting.

## Decisions already taken (change here if you disagree)

- **Concurrency: asyncio on the GLib main loop**, via `gi.events.GLibEventLoopPolicy` (PyGObject
  3.50+, which `meson.build` already requires). Verified on this machine: tasks created from GTK
  signal handlers run under `Gtk.Application.run()`, Gio async methods are awaitable
  (`await file.load_contents_async()`), `asyncio.to_thread` and `asyncio.open_connection` work.
  Why not threads: CDP is request/response plus events, which coroutines model directly with no
  locks, no `GLib.idle_add` hops and no shared-state bugs; UI code awaits the engine in place. Blocking
  work (JSON parse, image decode, artwork HTTP) goes through `asyncio.to_thread`. Chrome is spawned
  with `Gio.Subprocess` (GLib-native, awaitable `wait_async`). Python 3.14's DeprecationWarning for
  `set_event_loop_policy` is filtered; if PyGObject changes the entry point before 3.16, only
  `main.py` changes.
- **MPRIS: the app owns the service** (`org.mpris.MediaPlayer2.<app-id>`) reflecting the Player
  state, and Chrome is started with `--disable-features=HardwareMediaKeyHandling` so its own
  `org.mpris.MediaPlayer2.chromium.instance<pid>` never appears. Tradeoff: ~200 lines of D-Bus code
  and we relay every command, but the shell shows this app's name and icon, Raise focuses this
  window, media keys reach us, and the service survives engine restarts. Following Chrome's player
  would be free but shows "Chromium" in the shell and disappears whenever the engine restarts.
- **Navigation: one `AdwNavigationView` in the content pane.** Selecting a sidebar item replaces the
  stack with that destination's root page (created on first visit and kept); tiles push detail pages;
  the header bar's back button and Alt+Left pop. Root pages show a big in-content title (`title-1`,
  as on the web) and hide the header-bar title; detail pages show their title in the header bar.
- **Engine lifecycle.** Chrome starts headless on launch when signed in and `engine-autostart` is
  on, is reclaimed from `$XDG_RUNTIME_DIR/apple-music/engine.json` if it survived an app crash, and is
  stopped on quit (SIGTERM, 5 s, SIGKILL). Background playback with the window closed is a preference
  (phase 17), off by default.
- **Now-playing state comes from MusicKit events** forwarded through a CDP binding
  (`Runtime.addBinding` → `Runtime.bindingCalled`), never polled. Polling is allowed only while
  waiting for sign-in.
- **Cache and data flow.** `$XDG_CACHE_HOME/apple-music/library.json` is the single library
  snapshot (the extension's format, plus `folders`, `sections.songs`, `sections.videos`). Thumbnails
  are 320 px (tiles are ≤ 160 logical px at 2× scale) and fetched for every library item at sync;
  covers are 640 px and fetched on demand for detail pages. Decoded textures live in an LRU of ~200
  entries (~80 MB at 320 px). The library is parsed off the main thread; GObjects are wrapped lazily
  per section and spliced into `Gio.ListStore`s in batches. Pages show a placeholder state
  (`AdwSpinner` or empty `AdwStatusPage`) and fill when models arrive; tiles show a placeholder icon
  until their texture is decoded. Targets: content visible within 1 s of launch for 2,000 albums,
  page switch under 100 ms, no dropped frames while scrolling grids.
- **Songs** come from the albums' track groups immediately and from `/v1/me/library/songs` after a
  sync (loose songs), merged by id.
- **Demo mode** (`--demo`, phase 3) runs the app from a generated, fictional library so UI work and
  screenshots never need Chrome or a real account.
- **Tests** are stdlib `unittest` (nothing to install), run by `scripts/check.sh`; UI is verified by
  screenshots, not widget tests.

## Reference layout (music.apple.com, from Jack's screenshot; not in the repo)

Sidebar: Apple Music wordmark; Search, Home, New, Radio; "Library" heading: Recently Added,
Artists, Albums, Songs, Music Videos, Made for You; "Playlists" heading: All Playlists, Favourite
Songs, then the user's folders (folder icon, disclosure arrow, collapsed) and playlists (playlist
icon) in the user's order; footer: "Open in Music ↗" and the account (round avatar and name).
Content: a large bold page title ("Home"); shelves with bold titles: "Top Picks for You" (large
landscape cards with a coloured caption band: "Made for You", names), "Recently Played" (square
tiles, title and dim subtitle), decade and genre shelves; a scrollbar on the sidebar. The player
sits at the top full width in the web player; here it goes at the bottom (settled question 3).

---

## Phase 1: Foundations

**Goal.** Turn the scaffold into a repo future sessions can trust: first commits, a test runner in
`check.sh`, optional lint, logging, the asyncio-on-GLib bootstrap, and one HIG fix.

**Not in this phase.** No backend, no data, no new pages.

**Files.** `.gitignore`, `scripts/check.sh`, `pyproject.toml` (new, ruff config only),
`tests/__init__.py`, `tests/test_sections.py` (new), `src/main.py`, `src/window.blp`, `README.md`.

**Done when.** `git log` shows a "Scaffold" commit followed by this phase's commit;
`scripts/check.sh` runs unittest and prints `check: ok`; `scripts/run.sh --debug` logs the asyncio
loop class at startup; `scripts/screenshot.py --size 400x700` shows the sidebar as boxed lists.

```text
Phase 1 of prompts.md: Foundations. Read CLAUDE.md first.

1. Commit the untouched scaffold as the first commit ("Scaffold: window, sidebar, build") so the
   history starts from what exists. Before that, add `subprojects/.wraplock`,
   `subprojects/packagecache/` and `.ruff_cache/` to .gitignore (build/ and subprojects/*/ are
   already ignored; check with `git status --short --untracked-files=all`).
2. Tests. Create tests/__init__.py that registers src/ as the `applemusic` package without an
   install, using importlib.util.spec_from_file_location('applemusic', src/'__init__.py',
   submodule_search_locations=[str(src)]) and sys.modules. Add tests/test_sections.py: destination
   keys are unique and non-empty, 'home' exists, every icon name is either bundled in src/icons/ or
   present under /usr/share/icons/Adwaita/symbolic/. Run with
   `python3 -m unittest discover -s tests -v`.
3. scripts/check.sh: `python3 -m compileall -q src scripts tests`; `ruff check .` if `command -v
   ruff` succeeds (print one line saying it was skipped otherwise; ruff is not installed here);
   the unittest discovery; then the existing meson compile and meson tests. Add pyproject.toml with
   only `[tool.ruff]` (line-length = 100, target-version = "py312") and `[tool.ruff.lint]`
   select = ["E", "F", "W"]. Keep check.sh under ten seconds.
4. Logging in src/main.py: `logging.basicConfig(level=INFO, format='%(levelname)s %(name)s:
   %(message)s')`, DEBUG when `--debug` is passed (Gio.Application.add_main_option + 
   do_handle_local_options, returning -1 to continue) or APPLE_MUSIC_DEBUG is set. Replace nothing
   else; there are no prints to remove.
5. asyncio on GLib in main(): filter the DeprecationWarning for asyncio policies, then
   `asyncio.set_event_loop_policy(gi.events.GLibEventLoopPolicy())` before app.run(). Add
   `Application.spawn(coro)` that creates a task on `asyncio.get_event_loop()` and logs any exception
   from a done-callback. In do_activate, spawn a trivial coroutine that awaits asyncio.sleep(0) and
   logs `asyncio: <loop class name>` at DEBUG. This is the pattern every later phase uses; do not
   introduce threads for GTK work.
6. window.blp: in the breakpoint setters add `sidebar.mode: page;` next to `split_view.collapsed:
   true;` (the libadwaita-documented pairing).
7. README.md: keep it accurate (mention check.sh now runs tests).

Verify: scripts/check.sh passes; scripts/run.sh --debug shows the loop line; scripts/screenshot.py
build/narrow.png --size 400x700 shows the boxed-list sidebar, and build/wide.png the normal one.
Look at both PNGs. Update CLAUDE.md where this changed a convention (logging, spawn, tests).
Tick phase 1 in prompts.md and commit.
```

**Done 2026-09-27. Notes for later phases.**

- `python3 -m unittest discover -s tests` imports test modules as top-level modules and never runs
  `tests/__init__.py`, so every test module starts with `from tests import …` (`ROOT`, `SRC`)
  before `from applemusic import …`. That works from the repo root, where check.sh runs.
- `main.use_glib_event_loop()` holds the warning filter and the policy; `scripts/screenshot.py` calls
  it too (it builds the Application without `main()`), so `app.spawn()` also works in screenshots.
  Without the policy, `Gio.Application.run` does not mark a running asyncio loop and
  `asyncio.get_event_loop()` raises.
- The desktop runs the Tiling Shell extension, which ignored `--size`; screenshot.py now makes the
  window non-resizable, which it respects (so screenshots lack the maximize button). In the narrow
  layout the sidebar page is shown whatever `--page` is.
- ruff is not installed; `uvx ruff check --no-cache .` works for a one-off lint (it passed here).
  Imports after `gi.require_version()` carry `# noqa: E402`.
- check.sh takes about a second on an existing build.

## Phase 2: Vendor the backend

**Goal.** Bring the proven backend into this repo unchanged in behaviour, with its tests passing
here, and the demo-library generator. No process is started.

**Not in this phase.** No async client, no engine lifecycle, no UI changes.

**Files.** New `src/backend/{__init__.py,cdp.py,bridge.js,sync.py,config.py,am.py}`,
`tests/{test_cdp.py,test_sync.py,test_demo_schema.py}`, `tests/fixtures/`,
`scripts/demo_library.py`, `src/meson.build`, `pyproject.toml` (ruff excludes), `po/POTFILES.in`
unchanged (backend has no UI strings).

**Done when.** `scripts/check.sh` passes with the vendored tests;
`scripts/demo_library.py --cache build/demo` writes `build/demo/library.json` and artwork;
`src/backend/__init__.py` records provenance (source path, date, list of edits); nothing in
`src/backend/` imports GTK or reads the extension's schema.

```text
Phase 2 of prompts.md: Vendor the backend. Read CLAUDE.md first.

Source: /home/jackt/Projects/GNOME-Extensions/GNOME-Apple-Music-Library. Read its
src/backend/README.md fully (the command table, error codes, library.json/Item/Track shapes, the
environment overrides). Copy: src/backend/{cdp.py,bridge.js,sync.py,am.py} to src/backend/;
tests/{test_cdp.py,test_sync.py,test_demo_schema.py} and tests/fixtures/ to tests/;
scripts/demo_library.py to scripts/. Do not copy or read the extension's shell UI (src/*.js,
src/lib/) or its other scripts.

Adapt, minimally and listing every edit in the src/backend/__init__.py docstring:
- Package imports (`from . import sync`); tests import `applemusic.backend.<mod>` through the
  tests/__init__.py shim from phase 1.
- Settings: the backend read the extension's compiled GSettings schema. Replace with
  src/backend/config.py: cache_dir() = $APPLE_MUSIC_CACHE or $XDG_CACHE_HOME/apple-music;
  profile_dir() = $APPLE_MUSIC_PROFILE or $XDG_DATA_HOME/apple-music/chrome; port() =
  $APPLE_MUSIC_PORT or 9228; THUMB_SIZE = 320, COVER_SIZE = 640; the engine state file at
  $XDG_RUNTIME_DIR/apple-music/engine.json. The old names (APPLE_MUSIC_LIBRARY_*, port 9227, the
  apple-music-library directories) must not survive anywhere. If tests hard-code 256/512, make the
  sizes parameters of the functions rather than editing fixtures. The `.sizes` mechanism stays.
- am.py is kept only as a reference for later phases (its sync orchestration and command bodies
  get ported in phases 9 to 11): add a header comment saying so, exclude it from install and from
  ruff (pyproject `[tool.ruff] exclude`), and make sure importing the package does not import it.
- bridge.js is installed as data beside the Python (install_data into moduledir / 'backend') and
  located with Path(__file__).with_name('bridge.js'). Update src/meson.build to install the
  backend package. Nothing in src/backend/ may import gi.
- scripts/demo_library.py: it must write a complete library.json plus generated artwork into a
  directory given by --cache (default build/demo), using the config sizes. Read it and confirm every
  name, title and image in it is invented; if anything looks like real account data, replace it.
  test_demo_schema.py must pass against its output.

Verify: python3 -m unittest discover -s tests -v passes; scripts/check.sh passes;
scripts/demo_library.py --cache build/demo produces library.json with albums, artists, playlists,
radio and shelves; grep -r "apple-music-library\|9227" src tests scripts returns nothing. No Chrome
is started by anything in this phase. Update CLAUDE.md's layout section for src/backend/ and
scripts/demo_library.py. Tick phase 2 in prompts.md and commit.
```

**Done 2026-09-27. Notes for later phases.**

- Upstream was the extension at commit 906bfa9. `src/backend/__init__.py` lists every edit;
  keep it current. `src/backend/README.md` was vendored too (later phases cite "the vendored
  README"). It still describes am.py's one-process-per-command model, but its command table,
  error codes and library.json/Item/Track shapes apply as they are.
- `config` functions return `Path`s and read the environment on every call, so demo mode (phase 3)
  only has to set `APPLE_MUSIC_CACHE` before the Library loads. They know nothing of the `.Devel`
  profile: phase 10 derives `chrome-devel`/9229 from `app.profile` itself (env overrides still
  win). `state_file()` keeps upstream's rule that an `APPLE_MUSIC_PROFILE` override moves
  engine.json into that profile. Without one, a release and a `.Devel` build would share
  `$XDG_RUNTIME_DIR/apple-music/engine.json`, so phase 9/10 should key the file by profile.
- `sync.ART_SIZES` is module-global, set by `apply_art_sizes()` (sync, demo) or `load_art_sizes()`;
  the defaults are config's 640/320. Tests that depend on sizes pin them in `setUp`. Phase 11
  calls `sync.apply_art_sizes(cache, config.COVER_SIZE, config.THUMB_SIZE)` and can install
  `sync.scale_image = f(src, dest, size)` (GdkPixbuf, run from a thread) to scale cached covers
  into thumbnails. Without it, thumbnails are fetched at their own size, which is what happens
  anyway when covers are fetched lazily.
- `am.py` no longer runs (package-relative imports); read it, do not execute it. `cdp.py` is
  blocking (sockets and threading locks). `test_cdp.py`'s `MockWebSocketServer` is thread-based,
  so phase 9's asyncio tests can drive it from a thread or adapt it. `bridge.js` is unchanged: its
  globals are still `window.__appleMusicLibrary`/`__appleMusicLibraryWanted`, and `formatTrack`'s
  `artUrl` hard-codes 256 px.
- The demo library: 40 albums, 15 artists, 12 playlists, 8 stations; shelves heavy-rotation,
  recently-added, recently-played, made-for-you; `generated` fixed; ids like `l.alb001`,
  `l.art001`, `l.pl001`, `ra.st001`. It has no `songs`, `videos` or `folders` sections and no
  favourites playlist yet. `test_demo_schema.py` asserts exactly the four section keys, so the
  generator and that test grow together (phases 6, 8, 11). Art paths are absolute into `--cache`,
  so regenerate rather than move a demo directory. A build costs about 1.5 s of CPU;
  `test_demo_schema.py` builds once, through the CLI.
- Names in the demo generator that were not invented were replaced (see the provenance list), and
  so were real artists and songs in `test_sync.py`'s inputs. The fixtures were already invented
  apart from Apple's own labels ("Apple Music", "Today's Hits") and stay as they were.
- ruff: `am.py` is left out through `extend-exclude` (plain `exclude` would drop ruff's default
  excludes), and the vendored files have per-file E501 ignores. `uvx ruff check --no-cache .` passes.
  check.sh runs 81 unit tests and takes about 3 s of CPU (5 s wall on a loaded machine).

## Phase 3: Library model and demo mode

**Goal.** An in-memory model the UI can bind to, loaded off the main thread from `library.json`,
and a `--demo` mode so every later UI phase runs without Chrome.

**Not in this phase.** No grids yet; pages only show counts. No sync.

**Files.** New `src/library.py`, `tests/test_library.py`, `scripts/demo.sh`; edit `src/main.py`,
`src/window.py`, `scripts/screenshot.py`, `src/meson.build`.

**Done when.** `scripts/demo.sh` runs the app on the demo library and the Albums placeholder page
reads "1,234 albums" (whatever the count is) once loaded; `scripts/screenshot.py --demo --page albums`
shows it; `tests/test_library.py` loads a generated library and checks counts and `by_id`.

```text
Phase 3 of prompts.md: Library model and demo mode. Read CLAUDE.md first, then
src/backend/sync.py's docstrings and the Item/Track shapes in the vendored README.

1. src/library.py:
   - Item(GObject.Object) with GObject properties mirroring the Item shape (id, kind, title,
     subtitle, year, genre, summary, art, thumb, art_color, count_label, explicit, catalog_id, url)
     plus `play` (dict) and `raw` (the source dict); `groups` wraps Track objects lazily on first
     access. Track(GObject.Object) mirrors the Track shape (id, catalog_id, title, artist, album,
     track_number, disc_number, duration_ms, duration_label, explicit, index, thumb, raw).
     Shelf(GObject.Object): key, title, items (Gio.ListStore of Item).
   - Library(GObject.Object): properties `state` ('empty', 'loading', 'ready'), `generated`,
     `storefront`; Gio.ListStores albums, artists, playlists, radio, videos (empty if the section is
     absent); `shelves` (list of Shelf); `songs` built lazily from every album's groups (a
     Gio.ListStore of Track, deduplicated by id); by_id(kind, id); signal 'changed'.
   - async load(): read and json-parse the file in asyncio.to_thread; wrap and splice into the
     stores on the main thread in batches of 500 with an `await asyncio.sleep(0)` between batches so
     frames keep painting; set state; emit changed. A missing file is state 'empty', not an error.
     Paths come from applemusic.backend.config.
2. Demo mode: a `--demo` main option that points config at build/demo (set APPLE_MUSIC_CACHE
   before the backend reads it) and marks app.demo = True; scripts/demo.sh generates build/demo
   with scripts/demo_library.py when missing and execs scripts/run.sh --demo "$@";
   scripts/screenshot.py gains --demo doing the same. Later phases treat app.demo as "no engine".
3. Wire it: Application creates app.library in do_startup and spawns load() in do_activate. The
   placeholder pages' descriptions become live: "Loading…" while loading, then a count from the
   matching store ("%d albums", with ngettext) for albums, artists, all-playlists, songs,
   music-videos, radio; others keep "Nothing here yet".
4. tests/test_library.py: generate a demo library into a temp dir with scripts/demo_library.py
   (import it or subprocess), point config at it, run Library.load() under asyncio.run (no GTK
   needed: GObject creation works without a display), assert counts match library.json, by_id works,
   songs are deduplicated and `groups` wraps lazily.

Verify: scripts/check.sh passes; scripts/demo.sh runs and the sidebar pages show counts;
scripts/screenshot.py build/albums.png --demo --page albums and look at it. Update CLAUDE.md
(architecture: Library exists; commands: demo.sh and --demo). Tick phase 3 in prompts.md and commit.
```

**Done 2026-09-27. Notes for later phases.**

- The model: `Library` has stores `albums artists playlists radio videos` (created once, refilled
  in place by every load), `shelves` (a list, replaced per load), `shelf(key)`, `by_id(kind, id)`
  over section and shelf Items (kinds as in the README: album, artist, playlist, station, video,
  song; a shelf item with a section item's kind and id is that same object), `songs` and
  `song_count()`. `Item.groups` is a list of `Group` (GObject: `name`; `play`, `raw`, `entries` a
  Gio.ListStore of Track). A `Track` also carries `play` (its group's, which `index` counts in:
  phase 6 rows call `play_request(track.play, start_with=track.index)`), and its `thumb` falls back
  to its album's when the Track shape has none, so phase 5's Songs rows have art. Integer
  properties hold 0 for JSON null; string ones keep None.
- Properties are declared with `library.model_property(name, type, default)` and assigned to
  `_<name>` in `__init__`. Handing them to `GObject.Object.__init__` instead cost ~50 µs per Track
  against under 10. Measured on a 2,000-album, 23,000-track, 8.3 MB library.json: load() about
  100 ms under `asyncio.run` (JSON parse included), Item ~6 µs, `songs` built in ~220 ms on first
  access, synchronously. Phase 5 (30,000 tracks) should build it in batches or ahead of the page.
- `await asyncio.sleep(0)` does not let GTK paint under the GLib loop: asyncio runs at
  `G_PRIORITY_DEFAULT`, above the redraw. `library.yield_to_frames()` runs the task's next step at
  `G_PRIORITY_DEFAULT_IDLE` instead; in the app, wrapping 2,000 slowed-down albums in 8 batches
  painted 5 frames that way and 0 with sleep(0). Use it for any chunked main-thread work.
- `json.load` in `asyncio.to_thread` keeps the main loop's C work (drawing) going, but the parser
  holds the GIL for the whole parse (~70 ms at 8 MB), so Python callbacks on the main thread wait
  it out. Phase 19 should measure this with its big library.
- load() makes new Item objects each time (phase 11's reload diffs by id to keep identity). Each
  load bumps a generation; one overtaken by a newer load returns at its next pause without
  touching state or emitting `changed`. A load that raises sets 'empty', emits `changed` and
  re-raises (spawn logs it). The state is 'empty' until the first load; that load sets 'loading'
  before the first frame is painted (the task's first step outranks the redraw).
- `sections.songs` (phase 11's loose songs) is not read yet; `Library._build_songs` and
  `_read_library`'s count are the two places that merge it by id.
- Demo mode: `--demo` sets `app.demo`, adds `NON_UNIQUE` (a demo never raises a running app and
  owns no bus name) and sets `APPLE_MUSIC_CACHE` to the launcher's `DEMO_DIR` unless it is already
  set. `DEMO_DIR` is configured by `src/meson.build` as the source tree's `build/demo` for both
  profiles; phase 20 may want it blank for packaged builds. `scripts/demo.sh` and
  `screenshot.py --demo` generate `build/demo` when it is missing and `APPLE_MUSIC_CACHE` is unset.
  Demo runs share the real GSettings (last page, window size).
- `run.sh` and `check.sh` now run `meson setup` when `build/build.ninja` is missing rather than
  when `build/` is: `build/demo` alone made `build/` exist and `meson install` fail.
- `screenshot.py` waits for the library to leave 'loading' before rendering; without `--demo` it
  reads the real cache (`~/.cache/apple-music`, which does not exist on this machine yet).
- The placeholder counts live in `window.py`'s `COUNTED` (ngettext `'%s albums'` with the count
  formatted `f'{n:n}'`, which groups digits under GTK's locale: "23,000 songs"). A count of 0 reads
  "Nothing here yet" (Music Videos in the demo). Drop entries as real pages replace placeholders.
  The window disconnects its library handlers in `do_close_request`.
- check.sh runs 95 unit tests; `test_library.py` generates a demo library through the CLI once
  (about 1.7 s) and builds small invented ones for dedup, batches, reload and overtaken loads.

## Phase 4: Grid pages and artwork

**Goal.** The first real pages: recycled `Gtk.GridView` grids over the model, with asynchronous
artwork, and the content pane becomes an `AdwNavigationView` ready for drill-down.

**Not in this phase.** No detail pages (activation shows a toast), no folders, no Songs table.

**Files.** New `src/widgets/{artwork.py,tile.py,tile.blp}`, `src/pages/{grid.py,grid.blp}`,
`src/pages/__init__.py` (registry); edit `src/window.py`, `src/window.blp`, `src/style.css`,
`src/meson.build`, `src/applemusic.gresource.xml`, `po/POTFILES.in`.

**Done when.** Albums, Artists, Recently Added, All Playlists and Music Videos are grids of tiles
with artwork; scrolling 2,000 demo albums drops no frames; the header bar still shows the sidebar
toggle when collapsed; screenshots in both themes and at 400 px wide look right.

```text
Phase 4 of prompts.md: Grid pages and artwork. Read CLAUDE.md first, then src/library.py and
src/window.py.

1. src/widgets/artwork.py: a process-wide Artwork loader. get(path) returns a Gdk.Texture from an
   OrderedDict LRU (200 entries) or None; request(path, callback) decodes with
   asyncio.to_thread(Gdk.Texture.new_from_filename, path) (texture creation is thread-safe),
   deduplicates in-flight requests, and calls back on the main loop. Callers pass a token and
   cancel on unbind so recycled tiles never show a stale cover. A path that is missing on disk
   counts as no artwork (the README's rule).
2. src/widgets/tile.py + tile.blp: AppleMusicTile, a vertical Gtk.Box: a 160×160 Gtk.Picture
   (content-fit: cover, overflow: hidden, CSS class tile-art with border-radius 8px in style.css)
   showing a placeholder icon (music-note-symbolic on a card background) until the texture
   arrives; title label (ellipsize end, xalign 0, two lines max) and subtitle (dim-label, caption).
   An `artist` variant uses Adw.Avatar (size 160, custom-image) with a centred title. bind(item) /
   unbind() handle the artwork token. Set the Gtk.ListItem's accessible-label to "title, subtitle"
   in bind.
3. src/pages/grid.py + grid.blp: AppleMusicGridPage(Adw.NavigationPage) = Adw.ToolbarView with an
   Adw.HeaderBar (show-title: false; the sidebar toggle and back button still appear) and a
   Gtk.ScrolledWindow containing: a title label (title-1, xalign 0, margins 24) and a Gtk.GridView
   (min-columns 2, max-columns 12, single-click-activate true, tab-behavior item) over a
   Gtk.SingleSelection or Gtk.NoSelection of the page's model, plus a Gtk.Stack for
   loading (Adw.Spinner) and empty (Adw.StatusPage) states driven by library.state and the
   model's item count. Sorting: a Gtk.DropDown in the header (Title / Artist / Year) driving a
   Gtk.SortListModel with Gtk.StringSorter/Gtk.NumericSorter over Gtk.PropertyExpression (they run
   in C; do not use Python CustomSorter for big models). The factory is a Gtk.SignalListItemFactory
   creating AppleMusicTile in setup and binding in bind/unbind.
4. Pages: albums (library.albums), artists (library.artists, avatar tiles), recently-added (the
   shelf whose key is "recently-added", unsorted), all-playlists (library.playlists),
   music-videos (library.videos; the demo may lack it, so the empty state must look right).
   src/pages/__init__.py maps destination keys to page factories; unknown keys keep the
   placeholder.
5. Window: replace the GtkStack with an Adw.NavigationView; the content Adw.NavigationPage loses
   its own header bar (each page carries one). Root pages are created on first visit and kept;
   selecting a sidebar item calls navigation_view.replace([root]). Keep last-page behaviour.
   Activating a tile shows a toast with the item's title for now, through a single
   window.open_item(item) that phase 6 will implement.
6. Add the new .blp files to src/meson.build and the gresource, the .py files to install_data, and
   every file with strings to po/POTFILES.in.

Verify: scripts/check.sh; scripts/demo.sh; screenshots with --demo for --page albums, artists,
all-playlists in dark, one in --light, and albums at --size 400x700; look at each. Scroll the
Albums page with the demo library and confirm no jank (if the demo has fewer than 1,000 albums,
generate a bigger one: check demo_library.py's options). Update CLAUDE.md (architecture:
NavigationView, widgets/, pages/; the Artwork rule). Tick phase 4 in prompts.md and commit.
```

**Done 2026-09-27. Notes for later phases.**

- Navigation: `window._root(destination)` builds a root page on first visit (`pages.create`, or a
  placeholder `Adw.NavigationPage` with its own header bar and the old counted status page),
  tags it with the destination key, `navigation_view.add()`s it (so it survives being popped or
  replaced) and the sidebar calls `navigation_view.replace([root])`. `COUNTED` now covers only
  songs and radio. `window.open_item(item)` toasts the title: phase 6 pushes detail pages there.
- `GridPage(library, title, model, sorts=(), artist=False, icon_name, empty_title,
  empty_description)`: `model` is a function (called again on the library's `changed`, so a
  shelf's store, which each load replaces, is followed; None shows nothing), `sorts` are keys of
  `grid.SORTS` (`title`, `artist` = subtitle→year→title, `year` = newest first→title); with more
  than one, a header `Gtk.DropDown` offers them, hidden outside the grid state. Albums and Music
  Videos offer Title/Artist/Year; Artists and All Playlists are sorted by title with no
  drop-down; Recently Added keeps Apple's order. The choice is not remembered (no settings key).
  The chain is store → `Gtk.SortListModel` → `Gtk.NoSelection` → `Gtk.GridView`
  (single-click-activate, tab-behavior item). A sort change calls `grid_view.scroll_to(0, …)`: the
  grid otherwise follows its old top item to its new place. Pages connect to the library in
  `do_map` and disconnect in `do_unmap`. Loading shows an `Adw.Spinner`, an empty model after
  loading an `Adw.StatusPage` (both screenshotted).
- The in-content title cannot share a box with the grid in the scrolled window (a
  `Gtk.GridView` in a viewport stops recycling and leaves most of itself blank, checked), so it is
  an overlay child over `gridview.tile-grid`'s top padding (`style.css`, `calc(36px + 2.4em)`,
  sized for the label's 24+12 px margins and a `title-1` line; change both together) and
  `get-child-position` moves it up as the grid scrolls. Phase 5's page can use a
  `Gtk.ColumnView` header or the same overlay.
- Sorting uses `Gtk.PropertyExpression` over the existing `model_property` properties, unchanged:
  they are real GObject properties with a Python getter, and key-based sorters read each item
  once. Measured with 2,000 albums: title 10 ms, artist chain 25 ms, a Python `CustomSorter` 23
  ms; 24,000 songs: title 115 ms, `CustomSorter` 570 ms. A `Gtk.ClosureExpression` reading the
  attribute was twice as fast as the property if phase 5 needs it. A sort change in the app,
  rebinding included, takes about 40 ms.
- GTK 4.22's `Gtk.GridView` rebinds each item ~25 times while it scrolls past (plain GridView of
  2,000 strings: 52,000 binds top to bottom), so per-bind cost is what makes or breaks scrolling.
  Tiles show title and subtitle as markup in one `Gtk.Inscription` (3 lines: the title wraps to
  two, the dim smaller subtitle follows) instead of two `Gtk.Label`s: a wrapping label is
  re-measured on every rebind (layout 7 ms a frame, p90 15 ms, at 4,000 px/s), an inscription
  only redraws (under 1 ms). The price: a title longer than two lines takes the third and the
  subtitle is not shown (the list item's accessible label still has both). Artist tiles use
  `Adw.Avatar` without initials, which are a label too (p90 12.5 ms → 4 ms). `_()` in bind cost
  65 µs a call; strings are looked up once per page.
- Measured with `scripts/scroll_test.py` on `--albums 2000` (1100×760, four columns, whole grid
  top to bottom at 4,000 px/s): main-thread work per frame mean 2.1 to 3.5 ms, 90th percentile
  3.1 to 8.5 ms depending on the run and on which monitor the window lands, longest 13 ms, none
  over 16.7 ms in four runs; at 2,000 px/s mean 1.8 ms, one frame of 12,587 at 39 ms.
  Artists (585): mean 1.5 ms, p90 4.1 ms, none over 16.7 ms. The monitor is 240 Hz: about 7% of
  frames exceed its 4.2 ms, which phase 19 can look at. The machine was loaded (Chrome) and
  frame-to-frame gaps vary with the compositor, hence the work-per-frame measure.
- Artwork: `widgets.artwork.get_default()`; tiles `get()` on map, else `request()`, and
  `cancel()`/drop the texture on unmap, so live textures are the ~20 on screen plus the 200 in the
  LRU. Tiles draw `thumb`, else `art`. A missing file calls back None and is not remembered, so it
  is tried again at the next map (cheap: a failed open in a thread). A sync that rewrites covers
  under the same names (a new thumbnail size) should call `artwork.get_default().clear()` (phase
  11). Phase 5's rows and phase 6's heroes use the same loader (heroes with `art`).
- `scripts/demo_library.py --albums N` adds generated albums by generated artists (1 to 6 albums
  each, 8 to 16 songs an album) after the 40 hand-written ones, which stay byte-identical; the
  covers are drawn at the end, by a forked process pool from 200 up. `--albums 2000` → 585
  artists, 24,000 songs, a 27 MB library.json (indented; artist groups repeat their albums'
  tracks), about 10 s; it loads in ~200 ms. Phase 5 wants `--albums 2500` for 30,000 songs.
  `test_demo_schema.py` tests `generated_albums()` without drawing.
- `scripts/scroll_test.py` (new) and `screenshot.py` make their window non-resizable in
  `window-added`: without it the tiling extension resized the window and the grid had two
  columns. `screenshot.py --page KEY` now shows that page in the narrow layout too (no `--page`:
  the sidebar, as phase 1 had it).
- `test_library.test_a_newer_load_wins` failed about one run in ten (the first load's thread
  could finish before the second load started); its read now waits for the second to start.

## Phase 5: Songs page

**Goal.** A sortable, filterable table of every song, as on music.apple.com's Songs page, built on
`Gtk.ColumnView`.

**Not in this phase.** Playback (activation toasts), Favourite Songs (phase 6).

**Files.** New `src/pages/{songs.py,songs.blp}`; edit `src/pages/__init__.py`, `src/library.py`
(if `songs` needs fields), meson/gresource/POTFILES.

**Done when.** 30,000 demo tracks sort by any column in under 300 ms, filtering as you type
works, the page shows a count, and screenshots look right in both themes.

```text
Phase 5 of prompts.md: Songs page. Read CLAUDE.md first, then src/pages/grid.py to match its
structure.

Build src/pages/songs.py + songs.blp: AppleMusicSongsPage(Adw.NavigationPage) with the same
header-bar arrangement as the grid pages, the in-content title "Songs" and a dim count label, a
Gtk.SearchEntry in the header (placeholder "Filter"), and a Gtk.ColumnView (show-row-separators,
tab-behavior item, single-click-activate false) with columns: Title (32 px thumb through the
Artwork loader + title, an "E" caption badge for explicit), Artist, Album, Time (right-aligned,
CSS class numeric). Each column gets a sorter (Gtk.StringSorter over a Gtk.PropertyExpression;
Gtk.NumericSorter for duration_ms); the model chain is library.songs → Gtk.FilterListModel
(Gtk.AnyFilter of Gtk.StringFilters on title, artist, album, ignore-case, substring) →
Gtk.SortListModel(sorter=column_view.get_sorter()) → Gtk.SingleSelection. Filtering is
incremental (set-filter with the entry text; the FilterListModel handles the rest). Row
activation (Enter or double-click) calls window.play_request(track) which for now toasts the
title; phase 12 makes it play.

Performance: generate a demo library with at least 30,000 tracks (check demo_library.py's
options; add one if needed) and confirm sorting by Artist and filtering feel instant; no Python
per-row work beyond bind. Bind must not decode images on the main thread.

Verify: scripts/check.sh; scripts/screenshot.py build/songs.png --demo --page songs (dark and
--light); look at them. Update CLAUDE.md if a convention changed. Tick phase 5 in prompts.md and
commit.
```

**Done 2026-09-27. Notes for later phases.**

- The model chain is not the one above: GTK's is too slow over Python objects, measured in the
  app on `--albums 2500` (30,116 songs, `build/demo-2500`; no new generator option was needed).
  A `Gtk.ColumnViewSorter` has no sort keys, so a `Gtk.SortListModel` over it compares pairs and
  reads both tracks' Python properties (about 3 µs a read from C) at every comparison: 1.6 to
  6 s a click. The `Gtk.AnyFilter` of three `Gtk.StringFilter`s refilters once per sub-filter it
  changes: up to 1.1 s a keystroke (one `Gtk.StringFilter` over the three fields: 60 ms a pass).
  What the page does instead: `library.songs` → `library.SongOrder` (Python sorts positions with
  cached keys: collation via `locale.strxfrm` of the casefolded text, once per distinct string;
  ties broken by the other columns, always ascending, so by artist the albums and tracks keep
  their order either way) → a list comprehension over `Track.search_key` (title, artist and
  album folded by `library.fold()`: case and accents ignored, so "beyonce" finds "Beyoncé") →
  one `Gio.ListStore` of rows → `Gtk.SingleSelection` (no autoselect) → `Gtk.ColumnView`. The
  columns still carry `Gtk.StringSorter`/`Gtk.NumericSorter`s so their headers sort and show the
  arrow; the page follows the view sorter's `changed` and reads its primary column and order.
- The other half of the cost was GTK's: a `Gtk.ListView`/`Gtk.ColumnView` keeps 200 rows alive,
  and a change that removes the items they show destroys their widgets and builds new ones
  (about 0.5 ms a Songs row: an `Inscription` costs 100 µs to build in the app against 30 alone,
  the title cell's template 130 µs), whatever model filters. `SongsPage._show()` inserts the new
  rows first, `scroll_to(0)`s, then removes the old ones, so the view recycles every widget and
  only rebinds (about 800 cell binds). A filter with no matches leaves the rows as they are behind
  the "No Results Found" page, so clearing it rebinds rather than rebuilds. Result, rebinding
  included: a sort click 30 to 50 ms, a keystroke 10 to 50 ms (the first also folds every
  track's key, ~30 ms), after `Gtk.SearchEntry`'s own 150 ms delay. Phase 6's track lists and
  phase 15's results can reuse `_show()`'s trick if they are big.
- `library.songs` is no longer built on first access: `await library.build_songs()` fills it
  (the page calls it through `app.spawn` when first mapped), a few albums at a time with
  `yield_to_frames()` (`FRAME_BUDGET` 8 ms) and one splice at the end; asked during a load, that
  load fills it before reporting ready; once asked, every load refills it (in place, so the page's
  handler re-sorts). `songs-ready` notifies. 30,116 songs: about 320 ms of work, 500 ms wall.
  Phase 11's loose songs merge in `_fill_songs` (and `_read_library`'s count, now used only by
  `song_count()`).
- `window.play_request(track)` toasts the title. Phase 6 describes rows calling
  `play_request(group.play, start_with=track.index)`: a Track carries both (`track.play`,
  `track.index`), so phase 6 can either keep this signature for tracks or generalise it
  (`play_request(play, start_with=None, shuffle=False)`) and change the one call in
  `pages/songs.py` (`on_activate`).
- Layout: the title and count ("30,116 songs", or "3,482 of 30,116 songs" while filtering) sit
  above the table and do not scroll (a `Gtk.ColumnView` recycles only as the scrolled window's
  child, and has its own header row); that box is on the `view` background like the grid pages.
  CSS pads the first and last columns' cells and headers to the title's 24 px. Columns have fixed
  widths (170, 100, 100, 80) shared out by `expand`, so they never measure cells; below 560sp an
  `Adw.BreakpointBin` (`width-request` 360, `height-request` 200) hides Album, and Title, Artist
  and Time fit 360 px. Text cells are `Gtk.Inscription`s with `valign: center` (given the row's
  height they wrap onto a second line); the title is a one-line `Gtk.Label` so the "E" badge
  (`.explicit-badge`, accessible label "Explicit") follows its text. Rows are 49 px (the 32 px
  thumbnail and cell padding). The sort starts at Title ascending and, like the grids', is not
  remembered. The filter entry is hidden while loading or empty; Escape clears it; typing
  elsewhere does not start filtering (no key-capture widget: phase 12's Space and phase 18's
  shortcuts would compete).
- Scrolling (`scripts/scroll_test.py`, which gained `--distance`): the 460-song demo top to
  bottom at 4,000 px/s: mean 2.3 ms of work a frame, 90th percentile 3.5, longest 9.6, none over
  16.7 ms. 30,116 songs, first 40,000 px at 4,000 px/s: mean 3.6 to 3.9 ms, p90 4.6 to 5.3,
  longest 7 to 11, none over 16.7 ms. The whole 1.5M px at 20,000 px/s (a fling): mean 11.4 ms,
  211 of 4,575 frames over 16.7 ms. Sorted by title nearly every row shows another album, so a
  row decodes its album's 320 px thumbnail to draw 32 px (785 decodes in those 10 s); phase 19
  could decode small copies for rows (the Artwork cache is keyed by path only).
- Not done here: accessible labels for rows (phase 18; the cells' text is readable), row context
  menus (16), Favourite Songs (6).
- `Gtk.ColumnView.sort_by_column()` leaves the previous primary column's arrow drawn (GTK 4.22
  does not notify it; header clicks do). The page uses it once, before anything is sorted.

## Phase 6: Detail pages

**Goal.** Album, playlist and artist pages pushed onto the navigation view from any tile, with the
hero, metadata, Play/Shuffle buttons and track lists, plus Favourite Songs.

**Not in this phase.** Actual playback (the seam `window.play_request` toasts), context menus,
shelf items without `groups` (they show a "Sign in to load" state until phase 10).

**Files.** New `src/pages/{detail.py,detail.blp,artist.py,artist.blp}`,
`src/widgets/{track_row.py,track_row.blp}`; edit `src/window.py`, `scripts/screenshot.py`
(`--open`), `scripts/demo_library.py` (a flagged favourites playlist), meson/gresource/POTFILES.

**Done when.** Clicking an album, playlist or artist tile opens its page; back works with the
header button and Alt+Left; discs show as groups; Favourite Songs opens the flagged playlist;
`scripts/screenshot.py --demo --open album:first` renders an album page.

```text
Phase 6 of prompts.md: Detail pages. Read CLAUDE.md first, then src/library.py (Item.groups,
Track), src/pages/grid.py and src/window.py.

1. src/widgets/track_row.py + .blp: AppleMusicTrackRow for Gtk.ListView rows: a leading slot
   (track number for albums, 40 px thumb for playlists), title with an explicit badge, artist
   (playlists only), duration_label right-aligned (numeric). Rows are activatable (Enter or
   double-click; single-click-activate false) and call window.play_request(group.play,
   start_with=track.index).
2. src/pages/detail.py + detail.blp: AppleMusicDetailPage(Adw.NavigationPage) for album and
   playlist Items. Adw.ToolbarView with an Adw.HeaderBar showing the item title; content in a
   Gtk.ScrolledWindow: a hero row (Gtk.Picture 260 px from item.art, falling back to thumb, then
   the placeholder; title title-1; subtitle title-3; a caption line "Genre · Year · count_label";
   Play and Shuffle Gtk.Buttons with icons, Play styled suggested-action, both calling
   window.play_request(item.play, shuffle=…)), the summary as a wrapping dim label when present,
   then one Gtk.ListView per group with a heading label when there is more than one group ("Disc
   1"). Below 600sp (Adw.Breakpoint on the page) the hero stacks vertically and centres. Items
   without groups (shelf hits) show an Adw.StatusPage "Sign in to load this" in place of the list
   (phase 10 replaces it with a fetch).
3. src/pages/artist.py + artist.blp: AppleMusicArtistPage: circular hero (Adw.Avatar 200 px with
   custom-image) and name, then an "Albums" heading and a Gtk.FlowBox bound to the artist's albums
   (each group of an artist Item is one album per the README; resolve through library.by_id and
   reuse AppleMusicTile). A FlowBox is fine here because an artist has tens of albums, not
   thousands; grids of unbounded size stay GridView.
4. window.open_item(item): push the right page (kind album/playlist → detail, artist → artist,
   station → toast for now). AdwNavigationView provides the back button; confirm Alt+Left and the
   mouse back button pop (test it; if not, add a win.back action with <alt>Left).
5. Favourite Songs: demo_library.py marks one playlist in its raw attributes as the favourites
   playlist (pick a key like attributes.isFavourites and keep it in the fixture only; phase 11
   maps Apple's real attribute onto the same key). The sidebar destination favourite-songs opens
   that playlist's detail page as a root page, or an empty state if none.
6. scripts/screenshot.py: add --open KIND:ID (ID may be "first") that calls window.open_item after
   the library loads, and raise the render delay when --open is used so artwork has arrived.

Verify: scripts/check.sh; screenshots with --demo: --open album:first, --open playlist:first,
--open artist:first, plus album at --size 400x700 and one --light; look at each. Update CLAUDE.md
(pages list, open_item and play_request seams). Tick phase 6 in prompts.md and commit.
```

**Done 2026-09-27. Notes for later phases.**

- The detail page is not "a hero, then a Gtk.ListView per group" in a box: a `Gtk.ListView` in
  a viewport builds a row for every item up to about 200 and leaves the rest blank (phase 4's
  finding), and a playlist can hold thousands of songs. Nor is the hero a list header: Tab never
  reached the Play button in a `header-factory` header (checked). What it is: one
  `Gtk.ListView` as the scrolled window's child, over a `Gtk.FlattenListModel` of sections: a
  store holding one marker item, whose row the factory fills with the page's hero (a top-level
  `Box hero` in `detail.blp`, reparented into whichever row is bound to the marker), then each
  non-empty group's `entries`. A `Gtk.FlattenListModel` is a `Gtk.SectionModel`, so with more
  than one group a `header-factory` heads each with "Disc N" (albums; the disc number comes
  from the tracks and is translated, whatever the group is called) or the group's name. The
  hero row is neither activatable nor focusable; Tab goes Play → Shuffle → out, Down from
  Shuffle into the tracks, and a push puts the focus on Play. 3,000 songs run on about 205 row
  widgets; `scripts/scroll_test.py --page favourite-songs` on an invented 3,000-song Favourite
  Songs: mean 4.2 ms of work a frame, p90 5.2 ms, 2 of 3,836 frames over 16.7 ms.
- A `Gtk.ListView` row gets its *minimum* height. An `Adw.StatusPage` in the hero (min 58 px,
  natural 292: a scrolled window inside) was cut to a sliver, so "Sign In to Load This" (no
  groups: phase 10 replaces it with a fetch) and "No Songs" (empty groups) are a box laid out
  like a compact status page, with a Sign In pill (`win.sign-in`). Nothing in the demo lacks
  groups; both states were checked with an invented library outside the repo. Phase 7's shelf
  hits will be the first real ones.
- `DetailPage(library, item)` is a pushed page; `DetailPage(library, find=callable, root=True,
  title=…, icon_name=…, empty_title=…, empty_description=…)` is a root page that shows `find()`,
  asked again on every `changed`/`notify::state` while mapped (spinner while loading, the empty
  state when it returns None). Favourite Songs uses `find=library.favourite_songs`; phase 8's
  playlist root pages can use `find=lambda: library.by_id('playlist', id)`, which follows the
  new Item objects each load makes. A pushed page keeps the Item it was given (stale after a
  reload until phase 11 keeps identity). Pages keep what they show in `page.item`; `open_item`
  compares it to skip pushing the same item twice.
- `window.play_request(play, start_with=None, shuffle=False)` replaced `play_request(track)`;
  the Songs page passes `track.play, start_with=track.index`, the detail rows the same, Play and
  Shuffle `item.play` (shuffle for the latter). For now it toasts "Playing “title” is not
  available yet" (or "Shuffling…"), the title from `library.track_at(play, index)` (the album or
  playlist by kind and id, then the entry with that index among its groups sharing that play)
  or the item's. Phase 12 replaces the body with the engine call; the signature matches
  `am.py play <kind> <id> [--start-with N] [--shuffle]`.
- `window.open_item`: album/playlist → `DetailPage`, artist → `ArtistPage`, anything else
  (stations, videos) toasts its title; phase 7 decides what a station tile does.
- Back: `Adw.NavigationView` already pops on `<Alt>Left`, Back, Escape (class shortcuts, local
  scope) and the mouse back button (its click gesture listens to every button: button 0),
  listed from its controllers. The focus is inside it after a push, so the keys work there;
  `win.back` (`<alt>Left`, set in `main.py`, "Go Back" in the shortcuts dialog) pops, or in the
  collapsed layout goes back to the sidebar, from anywhere else in the window, and is disabled
  when there is nowhere to go so the keys fall through. Real key and button presses could not be
  synthesised on this Wayland session (no ydotool or wtype, and uinput would type into the live
  desktop), so the tests activated the actions, the header back button and the list's
  `activate`, and read the controllers. Alt+Left with a dialog open pops the page behind it
  (phase 18).
- Favourite Songs: `library.FAVOURITES = 'isFavourites'`, read from a playlist's raw
  `attributes` dict (`Item.favourites`, `Library.favourite_songs()`); not in the README's Item
  shape. Phase 11's sync must write `attributes: {isFavourites: true}` onto Apple's favourites
  playlist. The demo's is the 13th playlist, `l.pl013` "Favourite Songs" (36 songs, no genre,
  year, catalogId or url), after the other twelve so their ids and tracks are unchanged; it also
  shows in All Playlists, and phase 8's sidebar should not list it twice (it is the fixed
  "Favourite Songs" entry). `test_demo_schema.py` now expects 13 playlists and one flag.
  `build/demo` was regenerated; `build/demo-2000`/`-2500` predate the flag.
- New widgets: `$AppleMusicCover` (`widgets/cover.py`: `size` property, `set_paths(*paths)` shows
  the first that decodes, a cached later one at once while a better one decodes; loads on map,
  releases on unmap; `hexpand: false` in its template, since the placeholder icon's expand
  otherwise stretched a cover with no artwork; corners by `cover` + `small`/`large` in
  style.css) and `$AppleMusicTrackRow` (Inscriptions for number, artist and duration; a
  one-line Label for the title so the badge follows it). The tiles and the Songs title cell
  still carry their own copy of the load-on-map logic; phase 19 could fold them into Cover if a
  measurement says the extra overlay costs nothing. Album rows are 40 px, playlist rows 56
  (`listview.track-list > row`, inset 12 px with rounded corners, contents on the page's 24 px).
- The artist page is a `Gtk.FlowBox` of `AppleMusicTile`s (children made in `bind_model`'s
  create function, each `Gtk.FlowBoxChild` given an accessible label), albums newest first; a
  group whose album the library lacks becomes a stand-in Item made from the group, so its page
  still lists the tracks. The tiles centre in their cells as the grid pages' do. The album
  subtitle on a detail page is not a link to the artist yet.
- `scripts/screenshot.py` gained `--open KIND:ID` and now turns GTK animations off: one shot
  caught the push transition 13 px short of done (the compositor starves an unfocused window of
  frames).

## Phase 7: Shelves, Home and Radio

**Goal.** The shelf widget (a horizontal recycled row of tiles under a title) and the Home and
Radio pages built from the cached shelves and stations.

**Not in this phase.** New and Made for You (engine data, phase 15), fetching `groups` for shelf
items (phase 10).

**Files.** New `src/widgets/{shelf.py,shelf.blp}`, `src/pages/{home.py,home.blp,radio.py,radio.blp}`;
edit registry, meson/gresource/POTFILES, `src/style.css`.

**Done when.** Home shows the demo shelves as horizontal rows with a larger hero row first; Radio
shows stations; vertical mouse-wheel over a shelf scrolls the page; screenshots look right.

```text
Phase 7 of prompts.md: Shelves, Home and Radio. Read CLAUDE.md first, then src/widgets/tile.py
and src/library.py (Shelf).

1. src/widgets/shelf.py + shelf.blp: AppleMusicShelf: a title row (title-2 label, optional
   subtitle, optional "See All" flat button) above a Gtk.ScrolledWindow (vscrollbar-policy never,
   hscrollbar-policy automatic with overlay scrolling) containing a horizontal Gtk.ListView
   (orientation horizontal, single-click-activate true, tab-behavior item) whose factory makes
   AppleMusicTile; bind_shelf(shelf). A `hero` property makes tiles 260 px wide with a two-line
   caption for the first Home shelf ("Top Picks"-style cards). Activation calls
   window.open_item(item). Confirm a vertical wheel over the shelf scrolls the page and a
   horizontal gesture scrolls the shelf; fix event propagation if not.
2. src/pages/home.py + home.blp: title "Home", a vertical Gtk.Box of AppleMusicShelf inside a
   Gtk.ScrolledWindow (a Box is fine: shelves are few; the tiles inside are recycled), one per
   library.shelves in order, the first as hero. Loading/empty states like the grid pages.
3. src/pages/radio.py + radio.blp: title "Radio", a hero shelf of the first stations and a grid
   (reuse AppleMusicGridPage's grid or a shelf per subtitle group, whichever the data supports;
   read what sync.py puts in sections.radio). Station activation toasts until playback exists.
4. "See All" on a shelf pushes an AppleMusicGridPage built from that shelf's store (make the grid
   page accept an arbitrary model and title).

Verify: scripts/check.sh; screenshots with --demo for --page home (dark, --light, and --size
400x700) and --page radio; look at them. Update CLAUDE.md if conventions changed. Tick phase 7 in
prompts.md and commit.
```

**Done 2026-09-27. Notes for later phases.**

- `AppleMusicShelf` (`widgets/shelf.py`): `Shelf(hero=…, see_all=…)`, `bind_shelf(shelf,
  subtitle=None)` with a `library.Shelf`, whose GType is now `AppleMusicShelfModel` (the widget
  took `AppleMusicShelf`; nothing referred to the old name). Phase 15's search results can wrap
  their items in `library.Shelf(key, title, items)` and bind them the same way. The factory is
  declared in `shelf.blp` (template callbacks) and the state starts as class attributes, so the
  widget also works as `$AppleMusicShelf` in a template (Radio's). Tab leaves a shelf after the
  focused tile (`tab-behavior: item`), so Tab goes shelf to shelf and arrows move along one
  (phase 18). Accessible labels: the list is labelled with the shelf's title, each tile "title,
  subtitle".
- Hero cards are their own widget, `AppleMusicHeroTile` (`widgets/hero_tile.py`), not a mode
  of `AppleMusicTile`: a 260 px `AppleMusicCover` (the 640 px `art`, the thumbnail meanwhile)
  over two one-line `Gtk.Inscription`s (title in `heading`, subtitle dimmed), the whole card
  in `item.art_color`, drawn in its `do_snapshot` inside the rounded `overflow` clip, so the
  grid tiles pay nothing for it. White or black text by `artwork.is_dark()`, BT.601 luma under
  60%: WCAG's relative luminance put black on the demo's mid blue and green, where Apple uses
  white. No art colour: a tint of the text colour; no artwork: the note on the card's colour.
  Cards are square-covered, not landscape: the covers (the demo's, and most album art) carry
  text that a crop would cut. An artist on a hero shelf gets a square card; on a normal shelf
  `Tile.set_artist()` switches the tile to the round portrait as it binds.
- `window.open_shelf(shelf)` (See All) pushes `GridPage(library, shelf.title, model,
  root=False)`: a library shelf is followed by its key (a load replaces Shelf objects), any
  other is shown as it is; `page.shelf` guards a double click. `GridPage`'s `model` may now be a
  `Gio.ListModel` or a function; `root=False` shows the header-bar title too. Home offers See All
  on every shelf, whether or not the row overflows (the button is always there; the header
  keeps the button's 34 px height either way).
- Stations: `open_item(station)` calls `play_request(item.play)`, which toasts "Playing “…” is
  not available yet" (the station is found by `by_id('station', id)`); phase 12 makes it play,
  with no change here. Videos still toast their title.
- Home shows every *non-empty* shelf in library.json's order, the first as hero cards; the
  shelf widgets are kept and rebound on a reload (checked: same widgets, new Shelf objects).
  Radio: `sections.radio` is the sync's `/v1/me/recent/radio-stations` (most recent first; the
  subtitle is the provider, curator or artist, else "Apple Music Radio", all of them in the
  demo), so no grouping by subtitle: the first `HERO_COUNT` (4) are a hero shelf "Recently
  Played" and the rest a `Gtk.FlowBox` "More Stations" (hidden with 4 or fewer), its covers
  left-aligned in their cells so the first column lines up with the shelf. The window's
  counted placeholder (`COUNTED`, last used by Radio) is gone, and with it the window's own
  library handlers.
- Scrolling needed no fix: GTK 4.22's scrolled window declines a scroll along an axis it cannot
  scroll (CLAUDE.md, "Nested scrolling"); checked by emitting `scroll` on the real controllers.
  Real wheel, touchpad and touchscreen input could not be sent (no input synthesis on this
  Wayland session), so touch dragging over a shelf is unverified. `scroll_test.py --page home`
  (686 px at 1,000 px/s): mean 1.0 ms of work a frame, p90 1.5 ms, none over 16.7 ms.
- Checked with an invented library outside the repo: a hero item without an art colour, one
  with a light colour, one without artwork and with a long title, an artist on a normal shelf,
  an empty shelf (skipped), no stations (the Radio empty state), no library.json (Home's).
  Shelf items without `groups` (Apple's recommendations) open the detail page's "Sign In to
  Load This" state until phase 10 fetches them.
- The pointer on the real desktop hovers (and once scrolled) the screenshot windows that open
  under it; retake a shot that shows a stray scroll.

## Phase 8: Sidebar playlists and folders

**Goal.** The user's playlists and folders in the Playlists section of the sidebar, in Apple's
order, with collapsible folders, folder pages, and All Playlists showing the tree.

**Not in this phase.** Fetching folders from Apple (phase 11 adds it to sync; here the demo
generator and the model define the shape), context menus, drag and drop.

**Files.** Edit `scripts/demo_library.py`, `src/library.py`, `src/window.py`, `src/window.blp`,
`src/pages/grid.py`, `data/…gschema.xml` (`expanded-folders`), `tests/test_library.py`.

**Done when.** The sidebar lists All Playlists, Favourite Songs, then folders and playlists from
the demo; a folder collapses and expands with its arrow and opens its page; expansion survives a
restart; selecting a playlist shows its detail page as a root page; `last-page` remembers
`playlist:<id>`.

```text
Phase 8 of prompts.md: Sidebar playlists and folders. Read CLAUDE.md first (the AdwSidebar notes),
then src/window.py and src/library.py.

1. Shape: library.json gains "folders": a list of {id, title, parent (folder id or null),
   children: [{kind: "folder"|"playlist", id}]} in Apple's order, and playlists not in any folder
   are listed in a root entry with id "root". Extend scripts/demo_library.py to emit three folders
   (one nested) and loose playlists, keep test_demo_schema.py green, and add
   Library.playlist_tree() returning nested entries plus a flat depth-first list with depth.
2. Sidebar: the Playlists section becomes model-driven. Build a Gio.ListStore of SidebarEntry
   GObjects (kind: fixed/folder/playlist, key, title, icon, depth, item) starting with All
   Playlists and Favourite Songs, then the flat tree; call section.bind_model(store, create_func)
   where create_func returns a subclass of Adw.SidebarItem holding the entry. Folders use
   icon-name folder-symbolic and a Gtk.Image suffix (pan-end-symbolic collapsed, pan-down-symbolic
   expanded); their descendants get visible=False when any ancestor is collapsed. Playlists use
   the bundled playlist-symbolic. Expansion state is a GSettings key expanded-folders (type as,
   folder ids); add it to the gschema with a summary.
3. Selection: on notify::selected-item, a playlist entry replaces the navigation stack with its
   AppleMusicDetailPage (a root page, cached per playlist); a folder entry toggles its expansion
   and shows a folder page: AppleMusicGridPage over the folder's children where folder children
   are tiles with a big folder-symbolic icon that push the sub-folder's page. All Playlists shows
   the root entry the same way. Store last-page as "playlist:<id>" or "folder:<id>" and restore
   it, falling back to home if the id is gone. Re-select the right item after the library reloads
   (bind_model rebuilds items and loses selection).
4. Keep the fixed sections untouched. Check that ~150 playlists in the sidebar still scroll and
   render fine (AdwSidebar rows are real widgets, not recycled; note the number in CLAUDE.md).

Verify: scripts/check.sh (add a tests/test_library.py case for playlist_tree); screenshots with
--demo of the sidebar with a folder expanded and collapsed (add --expand FOLDER_ID or expand the
first folder when --demo is set) and of a folder page; restart and confirm the expansion persists
(GSettings memory backend in screenshots won't; test with scripts/demo.sh). Update CLAUDE.md.
Tick phase 8 in prompts.md and commit.
```

**Done 2026-09-27. Notes for later phases.**

- The shape (phase 11 writes it): library.json's top-level `folders` (optional, beside
  `sections`), a list of `{id, title, parent, children: [{kind: "folder"|"playlist", id}]}` in
  Apple's order; the entry with id `root` lists the top level, loose playlists included, and
  its `parent` is null; top-level folders have `parent: "root"`. The children lists decide the
  tree and its order; `parent` is informational. Map Apple's root folder (expected
  `p.playlistsroot`) onto `root`. `PlaylistTree` puts whatever no list reaches at the end of the
  top level (folders, then playlists in section order), skips children naming nothing and
  anything listed twice, and survives cycles, so a library without `folders` (the extension's
  format, `build/demo-2000`/`-2500`) is every playlist at the top level. The demo's: "Chill &
  Focus" (`l.fd001`, holding "Jazz Nights" `l.fd002` and three playlists), "On the Road"
  (`l.fd003`), then five loose playlists, Favourite Songs last (`build/demo` was regenerated).
- The model: `library.playlist_tree()` (a new tree per load; an empty one before the first):
  `root` is nested `TreeNode`s (`item`, `depth` (root -1, top level 0), `parent`, `children`,
  `store`, `ancestors()`), `flat` depth first; `tree.folder(id)`, `tree.folders()`. Folders are
  `Item`s of kind `folder` (title only, no art or groups, `raw` the folders entry), found by
  `by_id('folder', id)`, the top level as `by_id('folder', 'root')`. `library.folder_items(id)`
  is a folder's `Gio.ListStore` of folder and playlist Items (the playlists are the section's
  objects); each load makes new ones, so pages follow a folder by id. Phase 11's in-place reload
  can keep them or not: the sidebar compares what it shows, not identity.
- The sidebar (`src/sidebar.py`, new; `window.py`): the Playlists section is bound to a
  `Gio.ListStore` of `SidebarEntry` (`kind` fixed/folder/playlist, `key`, `title`, `icon-name`,
  `depth`, and Python attributes `item` and `ancestors`), the section's two destinations first,
  then `playlist_entries(tree)`, which leaves Favourite Songs out wherever it is (All Playlists
  still shows it, as phase 6 wanted). Items are `SidebarItem`s (`AppleMusicSidebarItem`,
  `item.entry`), folders with a `pan-end`/`pan-down-symbolic` suffix (presentation role). Keys,
  which `last-page` stores: a destination key, `playlist:<id>`, `folder:<id>`
  (`sidebar.parse_key`). A reload whose entries have the same shapes (kind, key, title, depth,
  ancestors) only swaps their Items, keeping the items, the selection and pushed pages; any
  change re-splices the tail, then the shown key is selected again (its folders expanded if it
  is hidden), roots of vanished keys are dropped, renamed ones retitled, and a shown playlist or
  folder that is gone falls back to Home.
- Selection and activation: selecting an item (click or arrow keys) shows its root page;
  `activated` (click, Enter, also on the selected item) toggles a folder and shows the content
  when collapsed. A sidebar playlist's root page is `pages.playlist()` (a `DetailPage` with
  `find=by_id('playlist', id)`, `root=True`); a folder's is `pages.folder()` (a `GridPage` over
  `folder_items(id)`, unsorted, empty state "Empty Folder"); folder tiles (a 72 px
  `folder-symbolic` in place of the note, switched only when a tile's kind changes) push
  `pages.folder(root=False)` through `open_item`. All Playlists is now the root folder in
  Apple's order: no longer sorted by title.
- Restoring: `last-page` = `playlist:`/`folder:` is shown at once (its page loads with the
  library) with All Playlists selected, and selected when the library arrives, or Home. Not
  with nothing selected: AdwSidebar's list selects the row with the focus when nothing is
  selected, as the window is shown and again when it becomes active (it took Search in one
  shot out of two). Every selection the window makes itself goes through `_set_selected`
  (`_quiet`: the notify shows nothing), splices included.
- `expanded-folders` (as, new key) holds folder ids, written on each toggle; ids of folders the
  library lacks are kept, since a demo run shares the settings with the real library. Checked
  across restarts on the real backend: a click (the row's `Gtk.ListBoxRow.activate()`) wrote
  `['l.fd001']` and `folder:l.fd001`, `scripts/demo.sh --debug` then logged `Sidebar: 3
  folders, 12 playlists in 3.8 ms; expanded: l.fd001` and showed it expanded; the real
  settings were put back afterwards.
- ~150 playlists: an invented library of 163 playlists and 7 folders (the demo's with 150
  cloned playlists, made by a throwaway script into `build/demo-150`, not kept): 169 entries
  spliced in 24 ms, then a 15-17 ms frame; a folder toggle 5 ms including building its page;
  `scroll_test.py --sidebar` (new) over 151 visible rows at 2,000 and 4,000 px/s: 0.5 ms of
  work a frame, none over 4.2 ms. Numbers in CLAUDE.md's AdwSidebar note.
- For phase 16: the section's `menu-model` and `setup-menu` get the `SidebarItem` (its
  `entry.kind` and `entry.item`); drops belong on `playlist` entries only. For phase 18: a
  folder's expanded state is not exposed to assistive technology (the arrow is presentation;
  the row reads its title), arrow keys select (and show pages) without toggling, and selecting
  does not scroll the sidebar to the item (a restored playlist far down stays off screen).
  AdwSidebar cannot indent, so nesting shows only by order and arrows, as settled question 4
  accepted; in the narrow layout the arrow sits before the row's own navigation arrow.
- `scripts/screenshot.py` gained `--expand ID[,ID…]` ("first": the library's first folder) and
  `--page` takes any last-page value; `--open folder:ID` pushes a folder. It does not scroll
  the sidebar: `--size 1100x1000` fits the demo's.

## Phase 9: Backend layer, engine process and async CDP client

**Goal.** The pure-Python (no gi) layer that finds and describes Chrome, keeps one asynchronous CDP
connection, injects the bridge, forwards MusicKit events, and a debug CLI to drive it without the
GUI. Nothing in the GTK app changes yet.

**Not in this phase.** The GObject `Engine` façade, sign-in UI, sync (phases 10 and 11).

**Files.** New `src/backend/{chrome.py,client.py,errors.py}`, `scripts/am.py`,
`tests/{test_chrome.py,test_client.py}`; edit `src/backend/bridge.js`, `src/backend/am.py`
(reference only), `src/meson.build`.

**Done when.** `tests/test_client.py` drives `CDPClient` against the fake WebSocket server from the
vendored `test_cdp.py` (calls, evaluate with promise, binding events, timeout); `scripts/am.py start
--visible` opens Chrome on music.apple.com with the app's own profile and port, `scripts/am.py eval
'MusicKit.getInstance().isAuthorized'` prints a JSON false, `scripts/am.py events` prints bridge
events, `scripts/am.py stop` ends it; `busctl --user list | grep mpris` shows no chromium player
while it runs.

```text
Phase 9 of prompts.md: Backend layer, engine process and async CDP client. Read CLAUDE.md first,
then src/backend/cdp.py, bridge.js, the vendored README, tests/test_cdp.py, and the engine and
command parts of src/backend/am.py (engine_start/stop/status, how commands call the bridge).
Nothing in src/backend may import gi; keep it asyncio and stdlib.

1. src/backend/errors.py: EngineError(code, message) with the README's codes engine-down,
   not-signed-in, api, timeout, plus usage.
2. src/backend/chrome.py: find_chrome(command) tries the configured command, then
   google-chrome-stable, google-chrome, /opt/google/chrome/chrome on PATH (return None if absent);
   chrome_args(binary, profile, port, headless) builds the argv: --user-data-dir, 
   --remote-debugging-port, --remote-debugging-address=127.0.0.1,
   --autoplay-policy=no-user-gesture-required, --disable-features=HardwareMediaKeyHandling,
   --no-first-run, --no-default-browser-check, --headless=new when headless, and
   https://music.apple.com/ as the URL (copy any other flag am.py used and say why in a comment);
   EngineState read/write of $XDG_RUNTIME_DIR/apple-music/engine.json {pid, port, headless,
   profile, started} with pid liveness checks; async wait_for_devtools(port, timeout) polling
   http://127.0.0.1:port/json/version every 200 ms; async list_targets(port) returning the page
   whose url starts with https://music.apple.com. HTTP through urllib in asyncio.to_thread.
3. src/backend/client.py: CDPClient over asyncio.open_connection, reusing cdp.py's handshake and
   frame codec where they are separable (refactor cdp.py into pure functions if needed, keeping
   test_cdp.py green). API: await connect(ws_url); await call(method, params=None, timeout=30)
   correlated by id; await evaluate(js, await_promise=True, timeout=30) using Runtime.evaluate
   with returnByValue, raising EngineError('api', …) on exceptionDetails; on(event, callback) for
   CDP events and for bridge events under names like 'am:playbackStateDidChange'; await
   ensure_bridge() reads bridge.js, hashes it, sets window.__appleMusicLibraryWanted and injects
   when the page's __version differs (the existing idempotent scheme), and re-runs after
   Runtime.executionContextCreated for the page's default context; await close(). Enable Runtime
   and Page domains and register Runtime.addBinding('__amEvent') before injecting. Timeouts and a
   closed socket raise EngineError('timeout'/'engine-down').
4. bridge.js: add subscribe() that attaches MusicKit listeners once for
   authorizationStatusDidChange, playbackStateDidChange, nowPlayingItemDidChange,
   playbackTimeDidChange, playbackDurationDidChange, queueItemsDidChange, queuePositionDidChange,
   shuffleModeDidChange, repeatModeDidChange, playbackVolumeDidChange, mediaPlaybackError, and
   posts window.__amEvent(JSON.stringify({name, data})) with plain-data payloads (the same shapes
   the bridge's now-playing/queue answers use; state as the PlaybackStates name). Verify the event
   names against Object.keys(MusicKit.Events) in the live page and fix any that differ.
5. scripts/am.py: a debug CLI on plain asyncio (asyncio.run; spawn Chrome with
   asyncio.create_subprocess_exec here): status, start [--visible], stop, eval <js>, now-playing,
   events (prints bridge events until Ctrl+C). It uses the same config as the app (port 9228 and
   $XDG_DATA_HOME/apple-music/chrome by default, env overrides respected). It must never touch the
   extension's profile.
6. tests: test_chrome.py (argv builder, state file round-trip, find_chrome with a fake PATH
   directory, target selection from a /json/list sample); test_client.py with
   unittest.IsolatedAsyncioTestCase against the fake server from test_cdp.py (adapt it to asyncio
   if it is thread-based): call/response, evaluate returning a promise value, an exception
   becoming EngineError('api'), a binding event dispatched to on(), a timeout.

Verify: scripts/check.sh; then the manual run: scripts/am.py start --visible (Chrome opens on
music.apple.com, not signed in; do not sign in in this phase), scripts/am.py eval
'MusicKit.getInstance().isAuthorized', scripts/am.py events in another terminal while clicking a
preview in Chrome shows playbackStateDidChange events, busctl --user list | grep -i mpris shows no
chromium entry, scripts/am.py stop. Report in prompts.md under this phase if any event name or
flag needed changing. Update CLAUDE.md (backend layout, the debug CLI). Tick phase 9 and commit.
```

**Done 2026-09-27. Notes for later phases.**

- The API phase 10 builds on (all in `src/backend/`, no gi; `README.md`'s last section has
  the same in full):
  - `errors.EngineError(code, message)`; `.code`, `.message`; a bad code is a `ValueError`.
  - `chrome.find_chrome(command, path=None)`, `chrome.chrome_args(binary, profile, port,
    headless)`, `chrome.EngineState(pid, port, headless, profile, started=None)` with
    `.load(path)` (None when missing or unreadable), `.save(path)`, `.remove(path)`,
    `.to_dict()`, `.alive` (the pid runs *and* its `/proc` command line names the profile, so
    a reused pid after a reboot is not "our Chrome"); `chrome.pid_alive(pid, profile=None)`;
    `await chrome.wait_for_devtools(port, timeout=15)` (the `/json/version` dict),
    `await chrome.list_targets(port)`, `chrome.select_target(targets)`,
    `await chrome.find_target(port)` (None when no music.apple.com page),
    `await chrome.wait_for_target(port, timeout=15)`, `await chrome.get_json(port, path)`.
  - `client.CDPClient(timeout=30)`: `await connect(ws_url, page=True)` (page=False for the
    browser target: no domains enabled), `await call(method, params=None, timeout=None)`,
    `await evaluate(js, await_promise=True, timeout=None)`, `await bridge(method, *args)`
    (`window.__appleMusicLibrary.method(json args)`), `on(event, callback)` / `off` where
    callback is `(name, data)` and event is a CDP method, `'am:<MusicKit event>'`, `'am:*'` or
    `'*'` (a coroutine returned runs as a task), `await ensure_bridge(timeout=15)`,
    `await subscribe()` (ensures the bridge, turns events on, and both are re-done after every
    navigation of the main frame), `await unsubscribe()`, `await close()`, `await
    wait_closed()` (returns when Chrome hangs up: phase 10's "engine died" signal), `connected`.
    `await client.connect_page(port, timeout=30, wait=15)` finds the page and connects.
  - Every failure is `EngineError`: `engine-down` (no DevTools, refused or lost connection,
    pending calls included), `timeout`, `api` (a CDP error or a JS exception, one line).
  - `config.state_file(profile=None)` is keyed by profile (phase 2's note): the default profile's
    is `$XDG_RUNTIME_DIR/apple-music/engine.json`, any other profile's (`chrome-devel`, an
    `APPLE_MUSIC_PROFILE` override) is `<profile>/engine.json`. Phase 10 passes the profile it
    derived from `app.profile`.
- Events: all eleven names in the prompt exist in `MusicKit.Events` of the live page (checked
  against `Object.keys(MusicKit.Events)`; none needed changing). Payloads are in the README's
  table. `playbackStateDidChange.state` is the `MusicKit.PlaybackStates` name (`none loading
  playing paused stopped ended seeking waiting stalled completed`; a play walks playing →
  waiting → loading → playing, a stop goes stopped → seeking → stopped);
  `nowPlayingItemDidChange` carries the Track (null after a stop); `queueItemsDidChange` the
  queue shape (index -1 before playback starts); `playbackTimeDidChange` about 4/s with
  integer seconds. `nowPlaying()`'s own `state` stays the coarse playing/paused/stopped.
- Flags: as the prompt, plus `--hide-crash-restore-bubble` (a Chrome that was SIGKILLed would
  otherwise greet the next visible window with "Restore pages?") and, from am.py, `--app=URL`
  for the visible window (no tabs or address bar; headless gets the bare URL).
  `--disable-features=HardwareMediaKeyHandling` verified: no `chromium.instance<pid>` on the
  bus for our Chrome, visible or headless. `EngineState.alive` looks for
  `--user-data-dir=<profile>` on the command line, so keep that flag's spelling.
- `scripts/am.py` spawns Chrome with `subprocess.Popen(..., start_new_session=True)` in a
  thread, not `asyncio.create_subprocess_exec` as the prompt said: an asyncio subprocess
  transport kills its child when it is garbage-collected, which is as the command exits, and
  Chrome died with it (seen as "Close running child process: kill" under
  PYTHONASYNCIODEBUG=1). Phase 10's `Gio.Subprocess` has no such behaviour; stop is
  SIGTERM, 5 s, SIGKILL there as here. `stop` without a state file falls back to
  `Browser.close` on the browser target. `events` handles SIGINT/SIGTERM itself
  (`loop.add_signal_handler`): a shell's background job inherits SIGINT ignored.
- `Runtime.addBinding` is per CDP session, so one connection at a time owns
  `window.__amEvent`; the app's single connection is fine, and two debug CLIs at once are not.
  `Runtime.enable` replays `executionContextCreated`; the client re-injects only after
  `ensure_bridge()` has been called, and only for the main frame's default context.
- Verified live (visible and headless, the app's profile, port 9228, not signed in): `start`,
  `eval 'MusicKit.getInstance().isAuthorized'` → `false`, `events` while a catalog preview was
  played from CDP (`mk.setQueue({song})` + `mk.play()`: unauthorised MusicKit plays 30-second
  previews; audio confirmed as a PipeWire sink input from our Chrome's audio process), the
  bridge coming back after a real `location.assign` navigation, `stop`. A sign-in was not
  attempted, so `authorizationStatusDidChange` is untested live.
- Tests: `tests/test_client.py` has an asyncio fake Chrome (`FakeChrome`, responders by method,
  `FakePage` for the bridge's probe/inject/status/subscribe round) worth reusing for phase 10's
  Engine tests; `tests/test_chrome.py` has a `/json` HTTP server for the polling. The suite is
  184 tests, about 8 s.

## Phase 10: Engine in the app, sign-in and account

**Goal.** The GObject `Engine` façade that owns Chrome's lifecycle inside the app, the sign-in flow
with a visible Chrome, the account button, and clean shutdown.

**Not in this phase.** Sync (phase 11), playback (phase 12), preferences UI (phase 17; the keys are
added now).

**Files.** New `src/engine.py`, `src/dialogs/signin.py` + `.blp` (or in `src/widgets/`); edit
`src/main.py`, `src/window.py`, `src/window.blp`, `data/…gschema.xml`, `src/pages/detail.py`
(fetch `groups`), meson/gresource/POTFILES.

**Done when.** Sign In opens a dialog and a visible Chrome; after signing in, Chrome restarts headless,
the account button shows the name (best effort) and the app remembers `signed-in`; next launch starts
the engine headless automatically; Quit stops Chrome (no stray process); Sign Out wipes the profile
and cache; a shelf item without `groups` now loads on demand when the engine is up.

```text
Phase 10 of prompts.md: Engine in the app, sign-in and account. Read CLAUDE.md first, then
src/backend/{chrome.py,client.py,errors.py}, scripts/am.py, and the signin/status/item command
bodies in src/backend/am.py (reference).

1. GSettings keys (add to data/io.github.jackicus.AppleMusic.gschema.xml with summaries):
   browser-command (s, 'google-chrome-stable'), engine-port (i, 9228), engine-headless (b, true),
   engine-autostart (b, true), signed-in (b, false), account-name (s, ''). The .Devel profile uses
   profile directory chrome-devel and port 9229 by default so it can run beside a release build
   (derive from app.profile; the env overrides still win).
2. src/engine.py: Engine(GObject.Object) with properties state ('down', 'starting', 'up',
   'signing-in'), authorized (bool), headless (bool); signal event(name, data) re-emitting bridge
   events. Coroutines: start(visible=False) reclaims a live Chrome from engine.json when its
   headless mode matches, otherwise spawns Gio.Subprocess (stdout/stderr silenced, or piped to the
   log at DEBUG) with chrome_args, waits for DevTools, connects CDPClient, ensure_bridge,
   subscribe, reads isAuthorized; stop() closes the client, SIGTERM, waits up to 5 s
   (subprocess.wait_async is awaitable), then force_exit; restart(visible); and command
   coroutines ported from am.py for this phase: status(), item(kind, id) (fills groups and caches
   under <cache>/items/), signin(). Every command raises EngineError; nothing here blocks. In demo
   mode every command raises EngineError('engine-down') immediately and start() is a no-op.
3. Lifecycle: app.engine is created in do_startup; do_activate spawns engine.start() when
   signed-in and engine-autostart are set and not in demo mode. app.quit becomes: spawn a
   coroutine that awaits engine.stop() (bounded by 6 s) then calls Gio.Application.quit. Closing
   the last window quits (background playback is a later preference).
4. Sign-in: app.sign-in shows an Adw.Dialog (Adw.StatusPage with an Adw.Spinner, "Sign in with
   your Apple ID in the Chrome window", a Cancel button) and runs: engine.restart(visible=True),
   the bridge's authorize call, then waits for the am:authorizationStatusDidChange event or polls
   isAuthorized every 2 s for up to 10 minutes (the one place polling is allowed); on success set
   signed-in, try to read a display name (inspect the visible page for a stable element holding
   the account name in the sidebar footer; if none is reliable leave account-name empty and do not
   guess), restart headless when engine-headless is set, close the dialog, toast "Signed in".
   Cancel stops the engine. app.sign-out asks with Adw.AlertDialog, then stops the engine, deletes
   the profile directory and the cache (asyncio.to_thread(shutil.rmtree)), clears the keys and the
   Library.
5. Account button (window.blp): signed out → "Sign In" runs app.sign-in; signed in → the label is
   account-name or "Signed In", Adw.Avatar shows initials from the name (show-initials), and the
   button opens a popover menu with Sign Out. An Adw.Banner above the content, revealed when
   signed out and not in demo mode: "Sign in to see your library" with a Sign In button.
6. Detail pages: an Item without groups now calls engine.item(kind, id) (spinner while waiting),
   merges the answer into the Item (raw and groups) and shows the list; engine-down shows the
   status page with a "Start Engine" button; not-signed-in shows "Sign In".

Verify: scripts/check.sh; scripts/run.sh (not demo): sign in for real, watch `ps -ef | grep
remote-debugging-port=9229` show one Chrome that goes headless after sign-in, quit the app and
confirm the Chrome is gone; relaunch and confirm autostart. Never commit anything containing the
account name or profile contents; screenshots of the account button use --demo (which shows the
signed-out state) only. Update CLAUDE.md (Engine façade, lifecycle, keys). Tick phase 10 and
commit.
```

**Done 2026-09-27 (without a real sign-in: the session could not sign in, so the flow was
verified up to the visible Chrome on Apple's page, then cancelled). Notes for later phases.**

- **Jack, verify by hand** (the release build uses `chrome` and 9228, the dev build
  `chrome-devel` and 9229; `ps -ef | grep remote-debugging-port=9229` shows the dev Chrome):
  1. `scripts/run.sh --debug`, click Sign In (the sidebar's account button, the banner or a
     shelf item's page): the dialog appears, a Chrome window opens on music.apple.com; sign in
     there with the Apple ID. The dialog should say "Signed in", the Chrome window should
     close and a headless one appear (`ps` shows `--headless=new`), the toast "Signed in"
     shows and the account button reads the name or "Signed In". Note whether the name was
     found (it is best effort; see below).
  2. Open an item from a Home shelf whose tracks the library lacks (any album or playlist the
     sync did not fetch groups for; after phase 11 these are Apple's recommendations): the
     page shows a spinner, then the tracks; `~/.cache/apple-music/items/<kind>-<id>.json`
     appears.
  3. Quit (Ctrl+Q or close the window): the Chrome is gone within a second or two, and
     `~/.local/share/apple-music/chrome-devel/engine.json` too.
  4. Relaunch: the engine starts headless by itself ("engine up: authorized" in the log with
     `--debug`) and the account button is still signed in.
  5. Kill the app (`kill -9`), relaunch: the log says "reclaiming Chrome <pid>".
  6. Sign Out from the account button's menu: confirm, the Chrome stops, the profile and the
     cache directories are gone (`~/.local/share/apple-music/chrome-devel`,
     `~/.cache/apple-music`), the library is empty and the button says Sign In.
  7. If the account name was not found in step 1: with the engine up and signed in, run
     `APPLE_MUSIC_PORT=9229 APPLE_MUSIC_PROFILE=~/.local/share/apple-music/chrome-devel
     scripts/am.py eval 'document.querySelector(".auth-content").outerHTML'` and add the
     element that holds the name to `ACCOUNT_NAME_JS` in `src/engine.py` (candidates first,
     `.auth-content` fallbacks last). The `bad` regex there lists what is not a name.
- **Verified by Jack and the coordinator on 2026-09-28** (dev build): sign-in in the visible
  Chrome completed, the engine restarted headless and came up authorized; quit removed the
  Chrome and the state file; relaunch autostarted headless; after `kill -9` the relaunch
  reclaimed the surviving Chrome. The account name was missed at sign-in because Apple's page
  renders its account menu a moment after authorization: `account_name(wait=…)` now polls
  (15 s in the dialog), the element is `.account-menu .user__name` (first candidate now), and
  autostart fills an empty `account-name`. Item fetch (step 2) waits for phase 11's shelves;
  sign-out was not run on the real profile.
- What was verified live on this machine (dev build, `chrome-devel`, 9229, never signed in):
  `app.sign-in` opened the dialog and a visible `--app` Chrome on `music.apple.com/us/new`,
  the engine reached `signing-in` (the bridge's `signin()` was called, its promise pending),
  Cancel stopped the Chrome and left `signed-in` false; a headless start reached the bridge in
  about 10 s and `status()` answered; a groupless album and artist showed the not-signed-in
  state (and the engine-down one under `--demo`); `scripts/run.sh --debug` with `signed-in`
  set autostarted one headless Chrome (stderr relayed at DEBUG), `gapplication action
  io.github.jackicus.AppleMusic.Devel quit` stopped it; after `kill -9` of the app the
  relaunch reclaimed the surviving Chrome; sign-out (on throwaway copies of the profile and
  cache) stopped the engine, wiped both, cleared the keys and emptied the library. The real
  settings were put back (`signed-in` false). Unit tests: `tests/test_engine.py` (33: the
  lifecycle against a sleeping process standing in for Chrome and test_client's fake page,
  reclaiming, SIGKILL after the grace, a lost connection, events, status, item for an album
  and an artist, signin by event, by poll, timeout, cancel and engine stop, account_name,
  demo, `engine_paths`, `item_endpoint`); `Item.merge` in test_library.py. 218 tests.
- The Engine's API (`src/engine.py`, CLAUDE.md has the summary): `Engine(profile_dir, port,
  browser_command, demo=False)`; `await start(visible=False)` / `stop()` / `restart(visible)`,
  `kill()`; `await status()`, `item(kind, id)` (raises `not-signed-in` when MusicKit is not
  authorized, `engine-down` when down; the answer is the Item shape with groups, `cached`
  stamped, kept at `<cache>/items/<kind>-<id>.json`, artwork fetched in the thread),
  `signin(timeout=600)`, `account_name()`; properties `state`, `authorized`, `headless`;
  `pid`, `profile_dir`, `port`, `state_file`. Phase 12 adds `play`, `control`… as thin
  `client.bridge(...)` wrappers behind `_require_up()`; phase 11's sync gets the client the
  same way (`self._client`, or add a `bridge(method, *args)` passthrough) and should mark the
  engine busy rather than start a second connection (`Runtime.addBinding` is per session).
  The `event` signal is where phase 12's Player subscribes (`engine.connect('event', …)`,
  names without `am:`). `authorized` follows `authorizationStatusDidChange` and every
  `status()`.
- Autostart when `signed-in` is set but MusicKit says not authorized (a session Apple
  expired): the engine stays up, a toast with a Sign In button says so, `signed-in` is not
  cleared. Sign-in while the engine is up headless restarts it visible (the profile keeps
  Apple's cookies, so a second sign-in on the same profile may complete at once).
- The account name: `ACCOUNT_NAME_JS` tries selectors and returns '' otherwise; the
  signed-out footer is `div.auth-content > button.commerce-button.signin` (Svelte, hashed
  classes), the signed-in markup unknown until Jack's run (step 7 above). `account-name`
  empty shows "Signed In" and the default avatar; with a name, `Adw.Avatar` initials.
- Quit: `app.quit` is an action that stops the engine first; `Window.do_close_request`
  activates it and returns True (the window is hidden by `prepare_quit()` and destroyed when
  the app ends). SIGINT/SIGTERM do the same (`GLib.unix_signal_add`). `screenshot.py` and
  `scroll_test.py` still call `app.quit()` directly (the Gio method), which is fine with no
  engine. Phase 17's background-playback preference changes `do_close_request` only.
- The banner is the content pane's `[top]` bar above the pages' header bars (each page has
  its own header bar, so "under the header bar" would mean one banner per page); it is hidden
  in demo mode and once signed in. `Adw.Dialog` on this desktop is a separate toplevel unless
  the window is maximized (CLAUDE.md), which phase 18 should remember for focus and Escape.
- Not done: the detail page does not read `<cache>/items/` back when the engine is down (the
  cache is written for a later offline path); `sync.py` still writes `library.json` only from
  phase 11's sync. `src/backend/am.py` keeps its sync body for phase 11 and is deleted then.

## Phase 11: Library sync and artwork cache

**Goal.** The app fetches the whole library through the engine, normalises it with `sync.py`,
fetches thumbnails, writes `library.json` atomically and updates the live models in place.

**Not in this phase.** Playback (12), search and the New and Made for You pages (15).

**Files.** New `src/sync.py` (app-level orchestration) or extend `src/library.py`; edit
`src/engine.py` (fetch commands), `src/backend/bridge.js` (folders, songs, videos, recently added if
missing), `src/main.py` (app.sync, `<primary>r`), `src/window.blp` (progress banner),
`data/…gschema.xml` (`last-sync`, `sync-interval`), `tests/test_sync_app.py`, fixtures.

**Done when.** After sign-in the library syncs with visible progress and the grids fill with real
data; a second sync updates in place without resetting scroll; folders appear in the sidebar;
Songs includes loose songs; Favourite Songs resolves; artwork sizes are 320/640 and `.sizes` says
so; `<primary>r` re-syncs; sync runs on launch when `last-sync` is older than `sync-interval`.

```text
Phase 11 of prompts.md: Library sync and artwork cache. Read CLAUDE.md first, then the sync
command in src/backend/am.py (reference), src/backend/sync.py, the vendored README's sync and
library.json sections, src/library.py and src/engine.py.

1. Port am.py's sync orchestration into src/sync.py as async def sync_library(engine, library,
   progress): per section (albums, artists, playlists, radio, shelves) await the engine's paged
   fetches (bridge calls over mk.api.music, as am.py did), normalise with sync.py's pure
   functions, then await asyncio.to_thread(<sync.py's artwork fetcher>) for missing thumbnails at
   THUMB_SIZE (covers at COVER_SIZE are fetched lazily by detail pages through a new
   engine-independent Artwork.fetch_cover(item) that downloads into <cache>/art/ in a thread),
   write library.json atomically (temp file + os.replace, under the existing flock), prune as
   before, then library.reload() which diffs by id: updates existing Items' properties, splices
   additions and removals, and keeps object identity so open pages and scroll positions survive.
   Progress is reported as (section, done, total).
2. New data, added to the bridge and to the shape: folders from the playlist-folders endpoint
   (confirm the exact request the web player makes for its sidebar in the visible engine's
   DevTools Network panel; the root folder id is expected to be p.playlistsroot), sections.songs
   from /v1/me/library/songs paginated (limit 100, offset), sections.videos from
   /v1/me/library/music-videos, the recently-added shelf from /v1/me/library/recently-added, and
   the favourites playlist identified by whatever attribute Apple sets (inspect a
   /v1/me/library/playlists answer; map it onto the same raw key phase 6 used in the demo). Keep
   library.json version 1 with these as optional keys, and keep test_demo_schema.py and the demo
   generator consistent.
3. Triggers: app.sync ("Refresh Library" in the primary menu, <primary>r, in the shortcuts
   dialog); automatically after sign-in; on activate when last-sync (s, ISO 8601) is older than
   sync-interval (i, hours, default 6). Add both keys. Only one sync runs at a time.
4. Progress: reveal an Adw.Banner over the content ("Syncing your library: albums 3 of 12") and
   finish with an Adw.Toast giving the counts; errors become a toast with the EngineError message
   and a Retry button.
5. Tests: tests/test_sync_app.py with a fake engine object whose fetch coroutines return
   fictional fixture pages; assert the written library.json, that reload keeps Item identity for
   unchanged ids, and that removed ids leave the stores. No network.

Verify: scripts/check.sh; scripts/run.sh signed in: sync completes, `ls ~/.cache/apple-music`
shows library.json, art/, thumb/ and .sizes with 320/640; pages fill; sync again and confirm the
Albums page keeps its scroll position. Nothing from the real library goes into the repo. Update
CLAUDE.md (sync flow, keys). Tick phase 11 and commit.
```

**Done 2026-09-28. Notes for later phases.**

- The endpoints, confirmed by watching the signed-in web player's own requests over CDP
  (`Network.requestWillBeSent` while `location.assign`ing its library pages) and by calling
  them through the bridge (`src/sync.py`'s docstring has the same):
  - Sidebar playlists and folders: `GET /v1/me/library/playlist-folders/p.playlistsroot/children`
    (the player adds `extend[library-playlists]=tags`, `fields[playlists]=curatorName`,
    `include=catalog`, `omit[resource]=autos`, `platform=web`, `offset=0`), then each folder's
    `/children`. The children come in the sidebar's order, playlists and folders mixed, typed
    `library-playlists` / `library-playlist-folders`; a folder's `attributes` are `name`,
    `canEdit`, `canDelete`, `dateAdded`. `/v1/me/library/playlist-folders` lists the folders
    flat (the root not among them). The sync maps `p.playlistsroot` onto `root`.
  - The favourites playlist: a library playlist's attributes carry no flag by default; asked
    with `extend=tags` (the player's `extend[library-playlists]=tags`), `attributes.tags` is
    `["favorited"]` on Favourite Songs (also the one playlist with `canEdit` and `canDelete`
    both false; nothing else has tags). The sync writes `attributes: {isFavourites: true}`
    (`library.FAVOURITES`, as the demo) onto it: `library.favourite_songs()` resolves.
  - Songs: `/v1/me/library/songs?limit=100&offset=N` answers `meta.total` and `next`; the
    sync asks `include=albums` (the albums and artists are grouped from the songs, as the
    extension did). The player asks `include[library-songs]=catalog`. Music videos:
    `/v1/me/library/music-videos?limit=100&offset=N` (this library has none; the section is
    written empty and the page shows its empty state). Recently Added:
    `/v1/me/library/recently-added?limit=25`, paged by `next` only (no `meta`), 25 at most a
    page; the sync takes up to 100. `/v1/me/library/albums` and `/artists` exist (628 and
    379 here, against 793 songs) but are not used: the songs' albums are what the extension's
    shape wants (an album holds only its library songs).
- What the sync writes beyond the README's shape (all optional, version 1; the demo omits
  them): `sections.songs` (Track dicts: the loose songs, whose `albums` relationship is
  empty; each is also under a stand-in `l.alb_…` album, and the Songs store merges by id
  albums first, so a loose song plays as its stand-in album's track), `sections.videos`
  (Items of kind `video`, play `musicVideo`), `folders` (phase 8's shape), `artUrl` on every
  Item with artwork (the 640 px cover URL) and `attributes.isFavourites`. library.json is
  written compact (1.8 MB for 628 albums; `save_library(indent=None)`).
- How the sync reuses the engine: `Engine.api(path, params)`, `api_pages(path, params,
  page=100, limit=None, progress=None)` (follows `next` by offset; with `meta.total` known
  the remaining pages are fetched three at a time, in order) and `api_all(paths)` are thin
  wrappers over the one CDPClient (`bridge('api', …)`, retried as `item()` is). No second
  connection, no busy flag: MusicKit answers concurrent reads, and `_api`'s retries cover a
  navigation. Playlist tracks are fetched four playlists at a time. Measured on this
  library (628 albums, 380 artists, 34 playlists, 793 songs, 21 shelves): the first sync 72 s
  (about 50 s of it the 1,034 thumbnail downloads, 8 threads), a second 18 s (nothing to
  fetch; 36 s from a freshly started Chrome), `library.reload()` 24 ms in place, the sidebar
  10 ms. Checked in-process (a throwaway script driving the app on the real cache and the dev
  engine): the Albums grid scrolled to 1,400 px stayed at 1,400 px with the same first item
  after the sync; the banner and the counts toast showed; quitting stopped the Chrome.
- `library.reload()` keeps identity by (kind, id): `Item.merge(raw, replace=True)` (groups and
  their Tracks kept when the group dicts are equal), `apply_diff(store, items)` (difflib
  over object ids: 2 ms at 2,000, 23 ms at 23,000), changed Items spliced over themselves so
  bound tiles rebind, Shelf objects kept by key (Home rebinds nothing), folder Items kept by
  id, the Songs store untouched when nothing moved. `load()` still makes new objects (the
  first load, sign-out). Tests: `tests/test_sync_app.py` (a fake engine over invented
  fixtures: the written file, a failed section keeping last time's entry, identity across a
  second sync, the reload diff), `api_pages` in `test_engine.py`. 230 tests.
- Covers are not fetched by the sync: `Artwork.fetch_cover(item)` downloads `artUrl` to
  `item.art` in a thread when a detail or artist page shows the item (`cover.refresh()`
  after), shared per path. Hero cards on Home still draw the thumbnail (260 px from 320: fine
  at 1×; phase 19 may fetch covers for the hero shelf). `prune_art` keeps a cover once
  fetched (its path is named by the Item). `sync.install_scaler()` gives the backend a
  GdkPixbuf scaler, so a thumbnail whose cover is on disk is scaled, not fetched.
- Triggers: `app.sync` (`<primary>r`, "Refresh Library" first in the primary menu, in the
  shortcuts dialog), after sign-in (`signin.py`), and `sync_due()` (last-sync older than
  sync-interval hours; 0 = manual) checked when the engine comes up authorized in
  `_autostart()` and on a second activation of the running app. One sync at a time
  (`Application._sync_task`; a second request is logged and ignored: checked live with two
  `gapplication action … sync` a second apart). Quit cancels the task; `download_art`'s
  `cancelled()` hook makes the thread give up at its next fetch, so the exit does not wait
  for hundreds of downloads. Progress is `(section, done, total)` with sections `songs
  playlists folders videos radio shelves artwork`; the banner reads "Syncing your library:
  artwork 300 of 1,034". Errors: `not-signed-in` through `app.report` (Sign In button), the
  rest "Could not sync your library: …" with Retry (`app.sync`). A section that fails
  (folders, videos, radio, a shelf, a playlist's tracks) keeps last time's entry and logs a
  warning; a failed songs or playlists listing fails the sync.
- Two things found on the way, in CLAUDE.md's "worth knowing": the blueprints custom_target
  does not recompile an edited `.blp` once anything else has been written into `build/src`
  (touch the `.blp`); and `screenshot.py --signed-in` autostarted a real Chrome on the
  *release* profile and port (its `profile` is `default`), so it now turns `engine-autostart`
  off in its memory settings.
- Not done: the Songs page is not told about a reload that changes nothing (fine) but a
  reload that changes an album's tracks re-splices the whole Songs store (the page re-sorts,
  30-50 ms, and loses its scroll); the hero shelf draws thumbnails; no "last synced" time is
  shown anywhere (phase 17's preferences could); `sections.videos` is untested on a library
  that has music videos (the fixture has two).

## Phase 12: Playback, Player state and the player bar

**Goal.** Real playback from every place that has a play affordance, a Player object fed by
MusicKit events, and a proper player bar at the bottom of the window inside an `AdwBottomSheet`.

**Not in this phase.** MPRIS (13), the Now Playing sheet contents (14), love/queue actions (16).

**Files.** New `src/player.py`; edit `src/engine.py` (play, control, seek, volume, shuffle, repeat,
now_playing, queue), `src/player_bar.py` + `.blp`, `src/window.blp`, `src/window.py`, `src/main.py`
(actions), `src/style.css`, `src/widgets/artwork.py` (remote art), `tests/test_player.py`.

**Done when.** Play on an album, a track row, a Songs row, a shelf tile and a station all play through
Chrome; the bar shows artwork, title, artist, a working seek slider updated from events, transport,
shuffle, repeat and volume; Space toggles play when no entry has focus; the bar never polls.

```text
Phase 12 of prompts.md: Playback, Player state and the player bar. Read CLAUDE.md first, then
src/engine.py, src/backend/bridge.js (the events added in phase 9), the play/control/seek/volume/
shuffle/repeat/now-playing/queue bodies in src/backend/am.py (reference), src/player_bar.blp and
src/window.blp.

1. Engine: port play(kind, id, start_with=None, shuffle=False), play_next, play_later, control
   (play/pause/toggle/next/previous/stop), seek(seconds), volume(level), shuffle(on/off/toggle),
   repeat(none/one/all/cycle), now_playing(), queue() from am.py as coroutines.
2. src/player.py: Player(GObject.Object) with properties state (MusicKit PlaybackStates names as
   strings), track (a NowPlaying GObject: id, catalog_id, title, artist, album, duration_ms,
   artwork_url, or None), position (float s), duration (float s), shuffle (bool), repeat
   ('none'/'one'/'all'), volume (float), and a position_updated_at monotonic stamp. State changes
   only from engine events (am:playbackStateDidChange, nowPlayingItemDidChange,
   playbackTimeDidChange, playbackDurationDidChange, shuffleModeDidChange, repeatModeDidChange,
   playbackVolumeDidChange) plus one now_playing() when the engine comes up. Commands are thin
   coroutines over the engine; a play request while the engine is down and signed in starts the
   engine first (toast "Starting playback engine…"); signed out runs app.sign-in. window.play_request
   from phases 5 and 6 now calls player.play. tests/test_player.py feeds synthetic events and
   checks the properties (no GTK needed).
3. Window: wrap the split view in an Adw.BottomSheet (content: split view; bottom-bar:
   $AppleMusicPlayerBar; sheet: a placeholder Adw.StatusPage "Now Playing", filled in phase 14).
   The bar is always revealed. Try full-width false first and keep whichever looks right at 1100
   px and at 400 px.
4. Player bar: previous, play/pause (icon follows state, suggested-action circular), next;
   shuffle and repeat Gtk.ToggleButtons (media-playlist-shuffle-symbolic,
   media-playlist-repeat-symbolic, -repeat-song-symbolic for "one"); artwork Gtk.Picture 44 px
   fetched from track.artwork_url through a new Artwork.fetch_remote(url, size) into
   <cache>/remote-art/ (thread; sized 640 so phase 14 reuses it); title and artist labels
   (ellipsize); a Gtk.Scale seek bar with elapsed and remaining labels, driven by position and
   duration, seeking on change-value and ignoring incoming updates while the user drags; a
   Gtk.ScaleButton for volume with the audio-volume-*-symbolic icons. Below 600sp hide the volume
   and the labels around the slider (Adw.Breakpoint on the window). Everything disabled with
   "Not Playing" when track is None.
5. Actions with accelerators, listed in the shortcuts dialog: app.play-pause (space; GTK gives
   focused entries the key first, confirm), app.next (<primary>Right), app.previous
   (<primary>Left), app.shuffle, app.repeat.

Verify: scripts/check.sh; scripts/run.sh signed in: play an album from its page, a track from
row 3 (starts at track 3), a Songs row, a station; pause, seek, next; watch the bar follow with
--debug logs showing events, not polling; scripts/screenshot.py --demo of the bar at 1100 and
400 px wide (Not Playing state). Update CLAUDE.md (Player, BottomSheet structure, actions). Tick
phase 12 and commit.
```

**Done 2026-09-28. Notes for later phases.**

- Verified live (dev build, in-process script on the real cache and the dev engine, as
  phase 11 did; audio at volume 0.2, put back after): Play on an album page (the album's
  first track, index 0), a track row at index 2 (starts there), a Songs row (that song), a
  station (a track arrives, state playing); pause and resume (`app.play-pause`), seek by the
  bar's slider (`change-value` → 30 s; the slider stayed there), next and previous
  (`app.next`/`app.previous`), shuffle and repeat by the bar's toggles and `app.shuffle`,
  the volume by the bar's button; the bar followed every one of them from events
  (`--debug` shows `event playbackTimeDidChange` four times a second and no now-playing
  reads after the one at engine start); artwork arrived in `<cache>/remote-art/`; stop
  cleared the track and disabled the bar. 39 of 39 checks. MusicKit calls that worked:
  `setQueue({album|playlist|station: id, startWith, startPlaying: true})` + `play()`,
  `pause()`, `skipToNextItem()`, `skipToPreviousItem()`, `seekToTime()`, `stop()`, the
  `volume`, `shuffleMode` and `repeatMode` setters (each answered by its event).
- The Player API (`src/player.py`, CLAUDE.md has the summary): `Player(app)`; properties
  `state` (the PlaybackStates name; `ACTIVE_STATES` = playing, loading, waiting, stalled →
  `player.active`, the bar's Pause icon), `track` (`NowPlaying`, from the bridge's Track
  shape: `artwork_url` is the 256 px URL; `Artwork.fetch_remote(url, 640)` re-sizes it),
  `position`, `duration`, `shuffle`, `repeat`, `volume`, plus `position_updated_at`
  (monotonic) and `estimated_position()` for MPRIS. `apply(now_playing_dict)` sets everything
  (phase 14's `--now-playing` screenshot can feed it a fictional answer); `apply(None)`
  resets the state, track and times but keeps shuffle, repeat and the volume (Apple's page
  keeps them across engine restarts; the next `refresh()` reads them). `error(message)` is
  emitted for `mediaPlaybackError` (toasted "Playback failed: …").
- What the events do, seen live: a `play()` from a new queue goes paused → seeking → paused
  → `nowPlayingItemDidChange` null → stopped → `nowPlayingItemDidChange` (the new item) →
  duration → playing → waiting → loading → playing; the bar shows "Not Playing" for about
  half a second in the middle (the actions disable and enable with it). `skipToNextItem` at
  the end of the queue (repeat none) goes paused → seeking → item null → stopped →
  completed. `playbackTimeDidChange` carries integer seconds. MusicKit's `isPlaying` is false
  while loading, so the bridge's `control('toggle')` would ask a loading item to play again:
  `Player.toggle()` decides from its own `active` (pause when active, else play).
- The bottom sheet: `window.blp` is `Adw.BottomSheet bottom_sheet { content: Adw.ToastOverlay
  toast_overlay { margin-bottom: bind bottom_sheet.bottom-bar-height; child: split view };
  bottom-bar: $AppleMusicPlayerBar player_bar; sheet: Adw.StatusPage "Now Playing" }`, full
  width (not full-width, the bar floats as a pill the width of its controls over the tiles;
  seen at 1100 px). The bar overlays the content in libadwaita 1.9, hence the margin binding.
  Toasts sit above the bar; with the sheet open (modal) they are under it, so phase 14 may
  want a toast overlay inside the sheet. Phase 14 replaces the `sheet:` child and flips
  nothing else; `bottom_sheet.open` opens it (`app.now-playing`); the bar's click and swipe
  open it already.
- The bar: transport left (`app.previous`, `app.play-pause`, `app.next`, actionable buttons,
  so their sensitivity is the actions'), `$AppleMusicCover` 44 px + title + "Artist — Album"
  + seek row in the middle, shuffle/repeat/volume right. A 600sp breakpoint on the window
  sets `player_bar.compact` (volume and the times hidden), repeating the 640sp collapse
  setters (only the last matching breakpoint applies). Shuffle and repeat are
  `Gtk.ToggleButton`s not bound to actions: a click calls the Player with the mode asked for
  (repeat: the next in none → one → all, shown at once with `-repeat-song-symbolic` for one)
  and the event confirms; a failure puts the button back. `app.shuffle` and `app.repeat`
  have no accelerators (none in the plan; Ctrl+R is Refresh). The three
  `media-playlist-*-symbolic` icons are bundled (Adwaita has none).
- Space: GTK 4 puts application accelerators in the window's capture phase (the controller
  named `gtk-application-shortcuts` reports `capture`), so a `space` accel would have fired
  while typing in the Songs filter and `<primary>Left` would have skipped a track instead of a
  word. The keys are `window.PLAYBACK_KEYS` in a capture-phase `Gtk.EventControllerKey` on
  the window: not handled when the focus is a `Gtk.Editable` or `Gtk.TextView` or the action
  is disabled (Space then presses a focused button as usual). Checked in demo mode by calling
  the handler with the filter entry focused and not (no key injection on this desktop).
- The seek slider ignores incoming positions from `change-value` until the seek has been
  sent (250 ms after the last movement) and then until MusicKit reports a position within
  2 s of the target or 1.5 s pass. `playbackTimeDidChange` is 4/s, so the bar needs no timer.
- The blueprint build was fixed on the way: one `custom_target` per `.blp` (see CLAUDE.md);
  the gresource lists bare `.ui` names. `scripts/screenshot.py` needed no change.
- Not done: no grace for the half-second "Not Playing" between queues; the bar's artwork is
  the 640 px file decoded whole (fine for one); `NowPlaying` has no `thumb` from the library
  (the bar could show the library thumbnail at once for library tracks); the volume button is
  insensitive with nothing playing (the plan's "everything disabled"), though MusicKit would
  take a level then too.

## Phase 13: MPRIS

**Goal.** The app appears in GNOME Shell's media controls and answers media keys, as itself.

**Not in this phase.** Anything visual.

**Files.** New `src/mpris.py`; edit `src/main.py`, `src/player.py` (if it needs a monotonic position
helper), `build-aux/flatpak/*.json` (`--own-name`), `tests/test_mpris.py` (metadata/variant
construction only).

**Done when.** `gdbus introspect --session --dest org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel
--object-path /org/mpris/MediaPlayer2` lists both interfaces; the shell shows the app with artwork
and the play/pause/next keys work; `busctl --user list | grep mpris` shows exactly one entry for this
app and none for chromium while the engine runs.

```text
Phase 13 of prompts.md: MPRIS. Read CLAUDE.md first, then src/player.py and src/engine.py.

Implement src/mpris.py: own the bus name org.mpris.MediaPlayer2.<application id> with
Gio.bus_own_name on the session bus, register /org/mpris/MediaPlayer2 with
Gio.DBusConnection.register_object using Gio.DBusNodeInfo.new_for_xml for the interfaces
org.mpris.MediaPlayer2 (Identity "Apple Music", DesktopEntry = the app id, CanRaise true → present
the window, CanQuit true → app.quit, Fullscreen/HasTrackList false, SupportedUriSchemes and
SupportedMimeTypes empty) and org.mpris.MediaPlayer2.Player (PlaybackStatus Playing/Paused/
Stopped mapped from Player.state, LoopStatus None/Track/Playlist, Shuffle, Volume, Position as
int64 microseconds computed from position plus the time since the last event while playing,
Rate 1.0, MinimumRate/MaximumRate 1.0, Metadata with mpris:trackid as an object path derived from
the track id, mpris:length, mpris:artUrl as a file:// URL of the cached remote art, xesam:title,
xesam:artist as a string array, xesam:album; CanGoNext/CanGoPrevious/CanPlay/CanPause/CanSeek/
CanControl true when a track exists; methods Next, Previous, Pause, PlayPause, Stop, Play, Seek,
SetPosition, OpenUri (no-op); signal Seeked). Emit org.freedesktop.DBus.Properties.PropertiesChanged
on the Player's notify signals with only the changed keys, and Seeked after a seek. Start it in
do_startup after the Player exists, release it in do_shutdown. Handle the name being lost
(log, keep running).

Confirm Chrome's own player is absent: with the engine playing, busctl --user list | grep -i
mpris must show only this app. If a chromium.instance entry appears despite
--disable-features=HardwareMediaKeyHandling, try adding MediaSessionService to the disabled
features in src/backend/chrome.py and confirm playback still works; record the outcome under
this phase in prompts.md.

Add --own-name=org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel to the Flatpak
manifest's finish-args. tests/test_mpris.py checks the metadata dict and variant types for a
sample track and for None.

Verify: scripts/check.sh; scripts/run.sh signed in and playing: gdbus introspect as in the phase's
"done when"; gdbus call --session --dest <name> --object-path /org/mpris/MediaPlayer2 --method
org.mpris.MediaPlayer2.Player.PlayPause toggles; GNOME Shell's calendar media section shows the
app's icon, title and artwork; keyboard media keys work; playerctl if installed. Update CLAUDE.md.
Tick phase 13 and commit.
```

**Done 2026-09-28. Notes for later phases.**

- Chrome's own player is absent: with the dev engine playing, `busctl --user list | grep -i
  mpris` listed `org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel` (the app's
  process) and no `chromium.instance<pid>` for the engine's Chrome pid, so
  `--disable-features=HardwareMediaKeyHandling` is enough and `MediaSessionService` was not
  added to `chrome.py`. The user's everyday Chrome (a PWA window) does publish
  `chromium.instance<its pid>`; tell them apart by the pid column.
- Verified live (the real Application with the Devel id in-process, as phases 11 and 12 did,
  volume 0.2, the album's queue played for under two minutes, the engine stopped after):
  `gdbus introspect` lists both interfaces with every method, property and `Seeked`; `GetAll`
  answers the right types (Position `x`, Metadata `a{sv}` with `mpris:trackid` an
  `objectpath`, `mpris:length`, `mpris:artUrl` a `file://` URL into `<cache>/remote-art/`,
  `xesam:artist` a string array); Position advanced ~1 s per second while playing and froze
  when paused; `PlayPause` toggled (PropertiesChanged `{PlaybackStatus}` only); `Seek` +10 s
  moved the position and emitted `Seeked`; `Set Volume 0.15` then `0.2`, `Set Shuffle`,
  `Set LoopStatus Track` each did what they say and answered with one PropertiesChanged of
  that key (from the MusicKit event, not the set); `Set Rate` is taken and ignored;
  `Set Position` is refused by GLib (read-only in the introspection); `Raise` returned;
  `Next` past the end of a one-track queue went to Stopped with `Metadata {}` and the Can*s
  false in one PropertiesChanged; `Quit` via `gapplication` stopped Chrome and released the
  name (the object is unregistered in `do_shutdown`). 18 unit tests over a stand-in
  connection.
- Not verified, for Jack to confirm: GNOME Shell's calendar media section and the keyboard
  media keys (no input synthesis here). The Shell shows a player while `CanPlay` is true and
  looks up `<DesktopEntry>.desktop` in its own data directories: the dev build's desktop
  file in `build/install` is invisible to it, so the entry may show the Identity without the
  app icon until a system install (`meson install` to /usr). `playerctl` is not installed.
- Design choices: `CanControl` is constant true (the spec says it is an intrinsic capability
  that never changes; the prompt had it follow the track like the other Can*s, which do).
  PlaybackStatus maps the ACTIVE_STATES (playing, loading, waiting, stalled) to Playing,
  paused to Paused, and `seeking` keeps the status before it (MusicKit passes through it on
  every seek and every new queue), the rest Stopped; no track is Stopped whatever the state.
  Metadata's length is the track's own `duration_ms`; the Player's `duration` stands in only
  for a track without one, and only once a duration has arrived for that track (the Player
  resets `duration` *after* the track's notify, and not at all for a track without
  `duration_ms`, so until then it is the previous item's; the first cut used it and sent
  each new track once with the old length, then twice more). So a track change is one
  Metadata change, seen live. `mpris:trackid` is `/io/github/jackicus/AppleMusic/track/<id with every non-
  alphanumeric byte as _XX>`; a queue with the same song twice gives both entries one path.
  The artwork is asked from `Artwork.fetch_remote` at the cover size (the bar's call and this
  one share the download) and Metadata goes out again with `mpris:artUrl` when the file is
  there; a track whose art is cached already carries it at once.
- Seeked: emitted after a Seek/SetPosition asked for over the bus (with the target; the
  hold and the note are set *before* the seek is awaited, since MusicKit's position at the
  target can arrive during the await and counted as a jump of its own at first), and when a
  position lands more than SEEK_JUMP (2 s) from where the last one led (a seek from the bar
  or Apple's page), except for TRACK_HOLD (2 s) after a track change: seen live, MusicKit
  reports the previous item's position once more with the state transitions of a skip, then
  the new item's 0, which the first cut signalled as `Seeked <old>` and `Seeked 0` on every
  Next and Previous. Still seen: MusicKit reports position 0 for a moment when a queue ends
  or Stop is called, before the item goes null, which shows as one `Seeked 0`; harmless and
  left alone.
- The methods and the writable properties spawn Player coroutines through
  `app.player_command`, so an EngineError becomes a toast, and the D-Bus reply goes out at
  once (the MPRIS methods have no return values). Transport methods do nothing without a
  track (CanPlay false), as the spec asks; the writable properties need only the engine
  (Volume can be set before the first play, which is how the tests set it low).
- `screenshot.py` and `--demo` own the name too (a `.Screenshot` or the Devel one), with
  CanPlay false; two demo instances at once mean the second logs the name as owned
  elsewhere and runs on.

## Phase 14: Now Playing sheet, queue and lyrics

**Goal.** The bottom sheet: big artwork, transport, and Lyrics / Up Next tabs, driven by events.

**Not in this phase.** Queue editing beyond jumping (MusicKit offers little), love (16).

**Files.** New `src/widgets/{now_playing.py,now_playing.blp,lyrics.py,queue.py}`; edit
`src/engine.py` (lyrics, queue_jump), `src/backend/bridge.js` (queueJump), `src/window.blp`,
`src/main.py` (`app.now-playing`), `src/player.py` (queue store), `scripts/screenshot.py`
(`--now-playing` demo state), `tests/test_lyrics.py`.

**Done when.** Clicking the bar opens the sheet; synced lyrics highlight and scroll with playback and
click-to-seek works; Up Next lists the queue with the current item marked and jumps on activation;
Escape and the close button close the sheet; `scripts/screenshot.py --demo --now-playing` renders it.

```text
Phase 14 of prompts.md: Now Playing sheet, queue and lyrics. Read CLAUDE.md first, then
src/player.py, src/player_bar.blp, src/window.blp, and the lyrics and queue bodies in
src/backend/am.py (reference).

1. Engine: lyrics(catalog_song_id) (cached under <cache>/lyrics/ as before) and queue_jump(index)
   (add queueJump to bridge.js using mk.changeToMediaAtIndex). Player gains a queue Gio.ListStore
   of NowPlaying-like entries and queue_index, updated from queue() on track change and from
   am:queueItemsDidChange / am:queuePositionDidChange.
2. src/widgets/now_playing.py + .blp: the AdwBottomSheet's sheet: an Adw.ToolbarView whose
   header has no title buttons and a close button (go-down-symbolic) that sets bottom_sheet.open
   false; content in an Adw.Clamp (maximum-size 900): artwork Gtk.Picture 320 px (from the 640 px
   remote art), title (title-2) and artist (dim), a transport row reusing the player's actions
   with larger buttons, the seek Gtk.Scale, then an Adw.ToggleGroup (Lyrics / Up Next) switching a
   Gtk.Stack. On wide layouts (>= 900sp) show artwork and controls on the left and the tabs on the
   right with an Adw.Breakpoint; single column below.
3. Lyrics: on track change request lyrics for track.catalog_id; synced lines become a Gio.ListStore
   of LyricLine(start_ms, end_ms, text) shown in a Gtk.ListView; the current line (by
   Player.position) gets a `current` CSS class (full opacity, bold; others dim-label) and the view
   calls scroll_to(index, Gtk.ListScrollFlags.NONE, None) only when the index changes; clicking a
   line seeks to its start. Unsynced lyrics are a wrapping Gtk.Label in a ScrolledWindow. No
   lyrics or engine-down: a compact Adw.StatusPage ("No Lyrics"). Never fetch lyrics on a timer.
4. Up Next: a Gtk.ListView of the queue with AppleMusicTrackRow-like rows, the current entry
   marked with an icon and bold, activation calling player.queue_jump(i).
5. app.now-playing (<primary>n) toggles the sheet; Escape closes it (bottom sheet handles it;
   confirm). scripts/screenshot.py --demo --now-playing sets a fictional NowPlaying with a lyrics
   fixture on the Player and opens the sheet.

Verify: scripts/check.sh (tests/test_lyrics.py: current-line lookup by position); real run: play
a song with synced lyrics, open the sheet, watch the line move, click a line, jump in Up Next;
screenshots: --demo --now-playing at 1100x760 and 400x700, dark and --light. Update CLAUDE.md.
Tick phase 14 and commit.
```

**Done 2026-09-28. Notes for later phases.**

- Verified live (the real Application with the Devel id in-process on the dev engine and the
  real cache, settings in a memory backend, the engine's volume 0.2 before and after): an
  album of three tracks whose first song has synced lyrics (36 lines): `app.now-playing`
  opened the sheet at the window's full height; the current line moved 2 → 3 from the
  position events (sampled once a second, nothing polled); a click on a line seeked to its
  start (44 s → 62 s; the line followed); Up Next listed the three entries with the first
  marked; activating entry 1 played it (queuePositionDidChange and nowPlayingItemDidChange:
  index 1, playing, the new song's lyrics read; the marker moved); the close button and the
  action closed the sheet; a toast added while the sheet is open shows inside it
  (`window.add_toast`); stop cleared the item; the engine was stopped after. The first two
  albums tried were one-track releases: MusicKit's queue was one entry and Next ended
  playback (`completed`), which is right, not a bug.
- Escape: no controller on the `AdwBottomSheet` widget itself; walking `observe_controllers()`
  down its tree finds a local-scope `Gtk.ShortcutController` with `Escape → callback` on
  the sheet's inner `AdwGizmo`, so Escape closes the sheet while the focus is inside it,
  which the modal sheet arranges. Not exercised with a real key (no input synthesis).
- Where things live: the lyrics are the Player's (`player.lyrics`, a `lyrics.Lyrics`;
  `player.lyrics_loading`), asked of `engine.lyrics()` once per catalog song as it starts
  (a repeat, or the same song again, keeps them; an item without a catalog id asks nothing)
  and kept under `<cache>/lyrics/<id>.json` only when Apple had lines: the page answers the
  same empty shape when Apple refuses (no subscription, a failed request), so an empty
  answer is asked again next time. The queue is `player.queue` (NowPlaying entries) and
  `queue_index`, from `queueItemsDidChange` / `queuePositionDidChange`; `queue()` is read
  only when the item playing is not among the entries held; a snapshot with the same ids
  is not spliced again (rows keep their widgets). `Player.apply()` takes `queue` and
  `lyrics` keys too (the `--now-playing` screenshot feeds it those). The views only follow
  the Player; `set_active()` (the sheet's `open`, and the tab shown) gates the scrolling.
- Files beyond the plan's list: `src/lyrics.py` (the model, GObject only, so
  `tests/test_lyrics.py` imports it without the gresource), `src/widgets/transport.py` (the
  bar's play-button, seek, shuffle/repeat and remote-artwork logic, now shared with the
  sheet; `player_bar.py` shrank to the titles and the volume), `tests/fixtures/lyrics.json`
  (invented). The sheet's LyricsView and QueueView are built in Python inside the template
  class (GtkBuilder skips a Python widget's `__init__`).
- Sizing and scrolling (the details are in CLAUDE.md): the sheet asks for a tall natural
  height (an `Adw.Bin`'s Python `do_measure` is never called: its layout manager is
  dropped and measure/allocate implemented); the lyrics list carries no CSS padding (GTK
  scrolls a list's padding with its rows, outside the page); the current line is brought
  on screen with `scroll_to(index, NONE)` when its index changes, then glides to the middle
  (Adw.TimedAnimation on the vadjustment) once its geometry has held still for a frame
  (a tick callback), and again whenever the page size changes (the open, a resize; the
  re-issued scroll_to goes through an idle: one asked from inside the adjustment's
  `changed` is lost); no following for 4 s after the user scrolls the list. The first and
  last few lines of a song sit where the clamp leaves them (no padding to centre them in).
- Layout: `wide` (window breakpoint `min-width: 900sp`) puts the item and the tabs side by
  side in a 900 px clamp; `compact` (600sp) shrinks the artwork to 240 px so a 400x700
  window keeps about 150 px for the tabs. The drag handle stays over the header bar.
- Not done: no thumbnails in Up Next rows (number or play marker, title, artist, duration);
  queue editing and love (16); the sheet has no volume control (the bar's is hidden while
  the sheet is open); the queue stays listed after playback ends (Not Playing above it);
  the `--now-playing` shot copies the demo cover into `build/demo/remote-art/`.

## Phase 15: Search, New and Made for You

**Goal.** Search with suggestions, results shelves and the browse landing page, plus the two remaining
engine-driven pages.

**Not in this phase.** Context menus on results (16).

**Files.** New `src/pages/{search.py,search.blp,new.py,made_for_you.py}`; edit `src/engine.py`
(search, suggest, landing, category, browse, made_for_you), `src/backend/bridge.js`, `src/main.py`
(`win.search`, `<primary>f`), registry, meson/gresource/POTFILES.

**Done when.** Typing shows suggestions after 250 ms, Enter shows shelves in Apple's order, the
Library toggle searches the cached models offline, the empty state shows browse categories that open
category pages, `<primary>f` jumps to search, and New and Made for You show shelves.

```text
Phase 15 of prompts.md: Search, New and Made for You. Read CLAUDE.md first, then the search,
suggest, landing and category bodies in src/backend/am.py (reference), src/widgets/shelf.py,
src/pages/grid.py.

1. Engine: search(term, library=False, limit=…, suggest=…), suggest(term), landing(),
   category(id) ported from am.py (the caches under <cache>/landing.json and <cache>/categories/
   stay). New: browse() for the New page: the web player's "New" page comes from an editorial
   groupings request (expected shape: /v1/editorial/{storefront}/groupings with name=music and
   platform=web); confirm the exact request in the visible engine's DevTools Network panel and
   shape the answer into shelves of Items as search does; and made_for_you() from
   /v1/me/recommendations, keeping the recommendation groups whose items are the personal mixes
   and stations. Both are cached for a day like landing.
2. src/pages/search.py + .blp: title "Search"; a Gtk.SearchEntry in an Adw.Clamp (600) with an
   Adw.ToggleGroup (Apple Music / Your Library) beside it; a Gtk.Stack: landing (a grid of
   category tiles: a Gtk.Picture from the category art through Artwork.fetch_remote with the title
   overlaid; activation pushes a category page of shelves), suggestions (a Gtk.ListBox of terms
   and top hits, shown while typing with a 250 ms GLib.timeout_add debounce, results discarded if a
   newer request is in flight), results (one AppleMusicShelf per returned shelf, in Apple's order;
   "See All" pushes a grid page for that kind). Your Library mode searches the cached models
   offline through Gtk.FilterListModel + Gtk.StringFilter over albums, artists, playlists and
   songs, shown as shelves and a song list, and works with the engine down. Engine-down or signed
   out in Apple Music mode shows an Adw.StatusPage with the fitting button. Result tiles call
   window.open_item (which fetches groups on demand since phase 10).
3. win.search (<primary>f, in the shortcuts dialog) selects the Search sidebar item and focuses the
   entry.
4. src/pages/new.py and made_for_you.py: shelf pages like Home over browse() and made_for_you(),
   with spinner, engine-down and signed-out states, and a pull-to-refresh equivalent: a refresh
   button in the header bar.

Verify: scripts/check.sh; real run: type, see suggestions, Enter, open a result, open a category,
toggle Your Library with the engine stopped (Preferences do not exist yet; use scripts/am.py stop)
and confirm it still searches; New and Made for You render. Screenshots with --demo: search
landing (empty state, engine-down) and Your Library results. Update CLAUDE.md. Tick phase 15 and
commit.
```

**Done 2026-09-28. Notes for later phases.**

- Endpoints, confirmed by watching the signed-in web player's own requests over CDP
  (`Network.requestWillBeSent` while `location.assign`ing `/new` and
  `/{storefront}/library/made-for-you`, then back to `/`; the bridge re-injected itself):
  - The New page is `GET /v1/editorial/{storefront}/groupings` with `name=music`,
    `platform=web`, `tabs=nonsubscriber`, `extend=artistUrl,editorialArtwork,plainEditorialNotes`,
    `format[resources]=map`, `include[albums]=artists`, `include[songs]=artists`,
    `include[stations]=events,radio-show`, `relate[songs]=albums`, `fields[albums]=…`,
    `fields[artists]=…`, `omit[resource:artists]=autos`, `art[url]=c,f`, `l=en-US`. The app
    asks for `name=music&platform=web&extend=editorialArtwork` (no `format[resources]=map`,
    which flattens the answer): `data[0]` is a `groupings` whose `tabs[0]` (an
    `editorial-elements` of kind 382) has 21 children here: kind 316 (untitled; its children
    of kinds 317/320 each hold one item: an album, a music video, a curator) → the
    "Featured" shelf; 327 (20 songs) and 326 (20 albums, playlists, stations or music
    videos; `attributes.name` the title, `displayStyle` compact/expanded) → shelves; 385 (a
    titled group of kind-394 links with `link` and no contents), 391 and 322 (`links`) →
    left out; `uploaded-videos` among music videos → left out (`SHELF_RESOURCE_TYPES`).
    19 shelves for this storefront (gb). `sync.editorial_shelves()` parses it; a category
    page's grouping (kind 326/327 children of `relationships.grouping.data[*].tabs`) goes
    through the same `_grouping_shelves`.
  - The web player's Made for You page asks `GET /v1/me/library/playlists` with
    `filter[featured]=made-for-you`, `sort=type`, `include[library-playlists]=catalog`,
    `fields[library-playlists]=artwork,dateAdded,name,playParams`,
    `fields[playlists]=lastModifiedDate`: the mixes *added to the library*, which answered
    `{data: []}` for this account (its page would be empty). So `made_for_you()` reads
    `/v1/me/recommendations?limit=25` as the plan said and keeps the recommendations made
    only of personal mixes (`playlists` with `playlistType: "personal-mix"`, `pl.pm-…` ids;
    Apple's `display.kind` is `MusicNotesHeroShelf`) or stations (`ra.u-…` the user's own,
    `ra.q-…`, catalog `ra.9…`): two shelves here, 5 mixes and 12 stations. A later phase
    could add the library filter above as a third shelf ("In Your Library") when it answers.
  - The search landing (the bridge's `searchLanding`, `/v1/recommendations/{sf}?name=
    search-landing&types=activities,apple-curators,editorial-items&extend=editorialArtwork
    &platform=web`) gave 50 categories: `apple-curators` with a square 1080 px `artwork`
    (a photo on `bgColor`) and `editorialArtwork` keys `brandLogo` (1080²),
    `subscriptionCover` and `subscriptionHero` (4320×1080), sometimes `superHeroTall`/`Wide`.
    The tile draws the square artwork at 84 px at the right over `bgColor` with the name at
    the left, which reads like Apple's tiles; the wide editorial images were not used.
- Caches: `<cache>/landing.json`, `categories/<id>.json`, `browse.json` and
  `made-for-you.json`, each stamped `cached` and good for `engine.ANSWER_MAX_AGE` (a day),
  answered without the engine (the demo engine refuses even those, so `--demo` shows the
  engine-down state); `refresh=True` (the pages' header-bar refresh button) asks Apple again
  and rewrites the file. Search and suggest answers are not kept. Remote artwork goes under
  `<cache>/remote-art/` (trimmed to 32 MB at the end of each sync): a search with 83 hits
  fetched its 83 thumbnails in about five seconds, six at a time, tiles rebinding as they
  landed; a category page and New each about a hundred more.
- The search page's structure (for phase 16's menus and 18's keyboard work): one
  `Gtk.Stack` with pages `loading`, `status` (an `Adw.StatusPage` with a pill button, worded
  per state: Engine Not Running / Start Engine, Sign In to Search Apple Music / Sign In,
  Search Failed / Try Again, No Results Found, No Suggestions, Search Your Library),
  `landing` ("Browse Categories" over a `Gtk.FlowBox` of `AppleMusicCategoryTile`s bound to
  a store of `category` Items), `suggestions` (a boxed `Gtk.ListBox` of `Adw.ActionRow`s in a
  600 px clamp: terms with a search icon, top hits with a 40 px `AppleMusicCover`, the title
  and "Kind · subtitle"), `results` (a box of `AppleMusicShelf`s kept and rebound, `top` as
  hero cards, See All → `open_shelf`), `library` (three shelves bound once to
  `Gtk.FilterListModel`s over the albums, artists and playlists — `LibraryShelf` objects with
  `key`, `title`, `items` — and "Songs" with a count over a non-scrolling `Gtk.ListView` of
  `TrackRow`s on a `Gtk.SliceListModel` of the first 25 matches; See All →
  `window.open_songs(text)`, the Songs page with its filter set). The entry's `changed` starts
  a 250 ms `GLib.timeout_add`; Enter searches (Apple Music) or re-filters (Your Library);
  Escape (`stop-search`) clears; a suggested term is put in the entry under a flag and
  searched. Every engine request carries a serial and an older answer is dropped. The page
  watches `engine.notify::state/authorized` while mapped and redoes the pending action once
  the engine is up or signed in; while the engine is `starting` the spinner shows instead of
  Engine Not Running. On a remap it refreshes only what is stale (a failure, nothing shown,
  a library filter), so coming back from an item keeps the results.
- Verified live, 32 of 32 checks in an in-process driver (the real Application with the
  Devel id, memory-backend settings, the dev engine reclaimed from `scripts/am.py start`'s
  Chrome, the real cache): `win.search` selected Search and focused the entry (the inner
  `Gtk.Text`); the landing showed 50 categories; typing three letters showed nothing before
  the debounce and then 12 suggestion rows (3 terms, 9 hits); the whole term likewise; Enter
  showed the spinner then six shelves (top, playlists, albums, songs, stations, artists) with
  every thumbnail fetched; the first album of the albums shelf opened as a DetailPage whose
  50 tracks were fetched on demand; clearing the entry brought the landing back; a category
  opened as a ShelvesPage of 10 shelves; New showed 19 shelves (Featured first) and wrote
  browse.json; Made for You showed 2 shelves and its refresh button re-fetched (the file's
  mtime moved); with `engine.stop()` Your Library found 514 albums, 380 artists, 34
  playlists and 712 songs for "a" and showed the empty prompt for nothing, Apple Music mode
  still showed the landing from the cache and then Engine Not Running with Start Engine,
  which restarted the engine and answered the pending suggestions; activating a suggested
  term searched; See All on the artists shelf pushed a grid of round portraits. No audio was
  played. Screenshots with `--demo`: the landing's engine-down state (dark and light), Your
  Library results for "the" at 1100×760 and 1100×1000 (the songs list), "mid" at 400×700
  (the toggle under the entry), New's engine-down state. Real-data shots stayed in `build/`.
- Deviations and things not done: the DevTools Network panel was replaced by CDP
  `Network.requestWillBeSent` logging from a scratch script (not in the repo). The songs'
  filter is one `Gtk.StringFilter` over a `Gtk.ClosureExpression` of `Track.search_key`
  (folded: accents ignored, one pass over 30,000 songs), not three property filters; the
  albums/artists/playlists filters are GTK's (title or subtitle, case only). Search, suggest,
  the landing, categories, browse and made-for-you all require a signed-in engine, as the
  plan's states imply, though the catalog would answer signed out. A music video in a
  shelf still toasts (phase 12); a song tile plays through `play_request`. The library-side
  search of Apple's `/v1/me/library/search` (`engine.search(term, library=True)`) exists but
  the page's Your Library mode is the offline filter, as planned. The Featured shelf's
  curator card (a category) opens its page rather than playing. Tests: `tests/test_browse.py`
  (the two shapers over an invented `editorial_groupings.json` and inline recommendations),
  search/suggest/landing/category/browse/made-for-you in `test_engine.py`, `remote_item` and
  `fetch_thumb` in `test_artwork.py`; 395 tests.

## Phase 16: Context menus and actions

**Goal.** Right-click, long-press and keyboard context menus on tiles, rows and sidebar playlists with
Play, Play Next, Play Later, Love, Add to Library, Add to Playlist, Open in Browser, Copy Link; a heart
in the player bar; drag tracks onto sidebar playlists.

**Not in this phase.** Playlist editing beyond adding, deleting playlists.

**Files.** New `src/actions.py`, `src/icons/heart-outline-symbolic.svg`, `heart-filled-symbolic.svg`
(bundled; Adwaita has no favourite icon now); edit `src/widgets/{tile.py,track_row.py}`,
`src/pages/songs.py`, `src/window.py`, `src/window.blp` (sidebar `menu-model`), `src/player_bar.*`,
`src/engine.py` (love, unlove, add_to_library, playlists, add_to_playlist), gresource, POTFILES.

**Done when.** Every tile and row has a context menu whose actions work against the real account (on
items Jack names during the session), the sidebar playlists have a menu, a track can be dragged onto
a sidebar playlist, and every action confirms with a toast or reports the error.

```text
Phase 16 of prompts.md: Context menus and actions. Read CLAUDE.md first, then the love/
add-to-library/playlists/add-to-playlist bodies in src/backend/am.py (reference),
src/widgets/tile.py, src/widgets/track_row.py, src/window.py and the AdwSidebar notes.

1. Engine: love(kind, id), unlove, add_to_library(kind, id), playlists(), add_to_playlist(
   playlist_id, song_id) ported from am.py.
2. src/actions.py: win.* Gio.SimpleActions with a GLib.Variant target "(ss)" (kind, id):
   item-play, item-play-next, item-play-later, item-love, item-unlove, item-add-to-library,
   item-open-in-browser (Gtk.UriLauncher on item.url), item-copy-link (window.get_clipboard().set);
   item-add-to-playlist with target "(sss)" (playlist id, kind, id). Each awaits the engine and
   toasts ("Playing next", "Added to Favourite Songs", …) or toasts the EngineError message.
   Build the Gio.Menu per item kind in code with Gio.MenuItem.set_action_and_target_value; the Add
   to Playlist submenu lists library.playlists (folders flattened).
3. Triggers: a Gtk.PopoverMenu created from the model, parented to the tile or row, has-arrow
   false, positioned with set_pointing_to at the pointer, on Gtk.GestureClick (button 3),
   Gtk.GestureLongPress (touch), and a Gtk.ShortcutController for the Menu key and Shift+F10 on
   the focused item. Sidebar playlist items: set the section's menu-model to a menu with Play,
   Play Next, Open in Browser and use the sidebar's setup-menu signal to point the actions at the
   right playlist.
4. Player bar: a heart Gtk.ToggleButton (bundle heart-outline-symbolic and heart-filled-symbolic
   in src/icons/, 16 px, #222 fill, drawn to match Adwaita's stroke weight) calling love/unlove on
   the current track's catalog id; if now_playing() reports a loved state use it, otherwise reset
   to unloved on each track change.
5. Drag and drop: track rows are Gtk.DragSources providing a TrackRef GObject (song id, title) via
   Gdk.ContentProvider.new_for_value; sidebar.setup_drop_target(Gdk.DragAction.COPY, [TrackRef])
   and the sidebar's drop signal call add_to_playlist for the target playlist item (ignore drops on
   folders and fixed items).

Verify: scripts/check.sh; real run: before any write to the real account (add to library,
add to playlist, love), ask Jack in the session which playlist or track to use; if none is
offered, test only love then unlove on one track. Confirm Play Next inserts after the current
track, Open in Browser launches the page, drag onto a playlist adds. Screenshot --demo with a
context menu open (add --context-menu to screenshot.py that pops it on the first tile).
Update CLAUDE.md (actions.py, the DnD types, bundled icons). Tick phase 16 and commit.
```

**Done 2026-09-28. Notes for later phases.**

- What exists: `src/actions.py` (`ItemActions`, made by the window as `window.item_actions`
  before the sidebar; the nine `win.item-*` actions on a `(ss)` (kind, id) target, the
  playlist one `(sss)`; `build_menu()` per kind and the other pure helpers, unit-tested in
  `tests/test_actions.py` with a stand-in window, app and engine), `src/widgets/context_menu.py`
  (`attach(view, drag=False)` on the grid, detail, Songs, search songs and top hits, artist
  and radio flow boxes, and every shelf; `popup()`), the engine's `love`, `unlove`, `rating`,
  `add_to_library`, `playlists`, `add_to_playlist`, `catalog_url` and its `rated(kind, id,
  value)` signal, the bar's heart (`transport.HeartControl`), the sidebar's playlist menu and
  drop target, `screenshot.py --context-menu`, and the two bundled hearts. Tiles, hero tiles,
  track rows and the Songs title cell expose `context_item`; a widget elsewhere gets a menu by
  doing the same and attaching its view.
- What each menu offers (only where it applies): Play (albums, playlists, songs, stations,
  catalog artists), Play Next and Play Later (albums, playlists, songs; a song by its catalog
  id), Favourite or Remove from Favourites (songs, albums, playlists, stations, videos; not
  Favourite Songs itself), Add to Library (catalog ids only), Add to Playlist (songs; the
  library's editable playlists in the sidebar's order, folders flattened), Open in Browser and
  Copy Link (whatever has a page). Folders, categories and the library's own artists (the
  sync's made-up `l.art_…` ids) have no menu; a loose song's stand-in album (`l.alb_…`) only
  Play. The sidebar's menu is Play, Play Next, Open in Browser on playlist entries and
  Favourite Songs; folders and All Playlists get none (AdwSidebar shows an empty popover for
  an empty model, so the window closes it from a high-priority idle before it is drawn).
- Decisions: the labels say "Favourite" / "Remove from Favourites" (Apple renamed Love; the
  sidebar has Favourite Songs), the actions keep the plan's item-love/item-unlove names;
  toasts "Added to Favourite Songs" for a song, "Added to Favourites" for the rest. Whether an
  item is loved is not in the library: `now_playing()` has no loved state, so the heart reads
  `engine.rating()` (GET `/v1/me/ratings/<type>s?ids=<id>`; the single-item path is a 404 for
  an unrated item) once per item, and a menu reads it as it opens and swaps its item when the
  answer differs; both follow `rated` after. Songs are rated by catalog id, albums, playlists,
  stations and videos by their own id (a library id through the `library-` type). The heart
  has no toast (its fill is the answer); it is hidden in the compact bar (400 px), where it
  squeezed the title to "Not Playi…". Drops go only to playlist entries whose Item is
  `editable` (a library playlist, not Favourite Songs, not Apple's `canEdit: false`); the sync
  now keeps `canEdit: false` in the Item's attributes (checked after a sync). Every sidebar
  item has `drag-motion-activate` off. A touchscreen drag scrolls; the
  long press opens the menu. The bridge's `addToPlaylist` takes the song's type (a library
  song is `library-songs`); "i." ids count as library ids now, which also sends
  `item('song', 'i.…')` to the library path. Open in Browser for a library album asks the
  engine for its catalog page and falls back to the web player's `/library/albums/<id>`.
- Verified live (the real Application with the Devel id in-process on the dev engine and the
  real cache, memory-backend settings; a sync first, reads only; volume 0.2): one library song
  that was not loved (rating 0) played from its album row; the heart showed unloved, a click on
  it loved the song (rating read back 1, heart filled); its context menu then offered Remove
  from Favourites, whose action unloved it (rating 0, the heart followed through `rated`, toast
  "Removed from Favourite Songs"); Favourite Songs had the same songs before and after. Play
  Next put another song right after the one playing (queue 3 → 4, at index + 1), Play Later put
  a third at the end (4 → 5), both toasted, Up Next followed. Open in Browser gave the album's
  catalog page, a playlist's library page and the song's `/<sf>/song/<id>` (recorded instead
  of launched; `Gtk.UriLauncher` took the URL); a drop emitted on a sidebar playlist called
  `add_to_playlist(playlist, song)` and one on Favourite Songs was refused; Add to Library went
  to the call. Those two were stand-ins for the run, so nothing was added. The Add to Playlist
  submenu listed 32 of the 34 playlists. In demo mode, by a script: the sidebar's menu (and
  none on a folder), the drop, the Menu key through the view's shortcut (the focused tile's
  menu), a right click through the capture gesture (the tile under the point; on the Songs
  table's album column, the row's menu pointing there), the submenu, the heart following
  `rated`. No real input was synthesised.
- For Jack to check by hand: Add to Library on a search result, Add to Playlist from a row's
  submenu, and dragging a track from an album, a playlist or Songs onto a sidebar playlist (the
  toast, then the playlist after a refresh); a real right click, long press, Menu key and
  Shift+F10; Open in Browser opening the default browser on the right page (a library album
  shows its catalog page, a playlist of his the web player's library page).
- Not done: no menu on Up Next rows or on the detail page's hero (no "…" button); Favourite
  Songs and the library are not refreshed after a love or an add until the next sync; a catalog
  item already in the library still offers Add to Library (Apple ignores the repeat); the
  playlist submenu is one flat list (no folder headings); `engine.playlists()` is ported and
  tested but the menus use the library's playlists.

## Phase 17: Preferences

**Goal.** An `AdwPreferencesDialog` for playback, library and engine settings, and background
playback.

**Not in this phase.** Appearance settings (none planned).

**Files.** New `src/dialogs/preferences.py` + `.blp`; edit `data/…gschema.xml`
(`background-playback`, and whatever phase 11 named for the interval), `src/main.py`,
`src/window.py` (close behaviour), `src/engine.py` (cache size, restart), POTFILES.

**Done when.** `<primary>comma` opens the dialog; every row is bound to its key; "Refresh Now",
"Clear Cache", Start/Stop Engine and Sign Out work; with background playback on, closing the window
keeps playing and MPRIS Raise brings it back; with it off, closing stops the engine.

```text
Phase 17 of prompts.md: Preferences. Read CLAUDE.md first, then data/…gschema.xml, src/main.py,
src/engine.py and src/sync.py.

Build src/dialogs/preferences.py + .blp as an Adw.PreferencesDialog opened by app.preferences
(<primary>comma, primary menu, shortcuts dialog):
- Page "General". Group "Playback": Adw.SwitchRow "Keep playing when the window is closed"
  (new key background-playback, b, false). Group "Library": Adw.ComboRow "Refresh library"
  (Every hour / Every 6 hours / Every day / Manually → sync-interval, with 0 meaning manually),
  Adw.ButtonRow "Refresh Now" (app.sync), Adw.ActionRow "Cache" whose subtitle is the cache size
  computed in a thread with a "Clear" button (Adw.AlertDialog confirm; clears art, thumb,
  remote-art, items, lyrics and library.json, then re-syncs if signed in).
- Page "Engine". Adw.EntryRow "Browser command" (browser-command), Adw.SpinRow "DevTools port"
  (engine-port, 1024–65535), Adw.SwitchRow "Run the browser hidden" (engine-headless; the
  subtitle says a visible browser helps debugging), Adw.SwitchRow "Start the engine with the app"
  (engine-autostart), Adw.ActionRow "Engine" with the state as subtitle and a Start/Stop button
  bound to engine.state, and a destructive Adw.ButtonRow "Sign Out" (app.sign-out). Browser and
  port changes apply at the next engine start; say so in the subtitles.
Bind rows with Gio.Settings.bind. Background playback: when on and something is playing,
closing the window hides it and calls app.hold() (release when playback stops or on quit);
app.quit and MPRIS Quit still stop the engine; MPRIS Raise re-presents the window. When off,
closing the window quits as today.

Verify: scripts/check.sh; scripts/screenshot.py --demo --preferences (add the flag: opens the
dialog) in dark and --light; real run: toggle background playback, close the window while playing,
raise it from the shell's media controls, quit. Update CLAUDE.md (keys, dialogs/). Tick phase 17
and commit.
```

## Phase 18: Keyboard navigation and accessibility

**Goal.** The whole app usable from the keyboard and readable by a screen reader, correct at 360 px
wide and in both colour schemes.

**Not in this phase.** New features.

**Files.** Touches most `.blp` files and `src/main.py` (shortcuts dialog).

**Done when.** A written walkthrough (sidebar → grid → detail → play → sheet → search → preferences)
works with keyboard only; the GTK inspector's accessibility panel shows names on every interactive
widget; `--size 360x640` screenshots of Home, Albums, a detail page and the sheet look right; the
shortcuts dialog lists every accelerator that exists.

```text
Phase 18 of prompts.md: Keyboard navigation and accessibility. Read CLAUDE.md first, then every
.blp file and src/main.py's shortcuts dialog.

1. Keyboard: Tab order sidebar → content → player bar; grids and lists use
   Gtk.ListTabBehavior.ITEM so Tab leaves them and arrows move inside; Enter activates tiles and
   rows; Alt+Left pops pages; Escape closes the sheet and dialogs; F10 opens the primary menu;
   Space play/pause outside entries; <primary>f search; <primary>r refresh; <primary>n Now
   Playing; <primary>comma preferences; add <primary>1..3 to focus the sidebar, content and
   player bar (win.focus-*). Every accelerator appears in the Adw.ShortcutsDialog, grouped.
   Dialog buttons get mnemonics.
2. Accessibility: every icon-only button has tooltip-text (it becomes the accessible name); tiles
   and rows set accessible-label in bind; decorative images use accessible-role presentation;
   the seek Gtk.Scale and volume button get accessible labels via update_property; the player bar
   announces the track change (Gtk.Widget.announce if available in this GTK, otherwise skip);
   check everything in the GTK inspector (GTK_DEBUG=interactive scripts/run.sh --demo,
   Accessibility tab). If orca is installed, run it briefly against the demo and note gaps.
3. Adaptive: at 360 px the sidebar is a page, grids show two columns, the player bar drops the
   volume and time labels, detail heroes stack, the sheet is single-column; fix anything that
   overflows. Both colour schemes: no hard-coded colours outside the accent; contrast of the
   dim captions on the card tiles.
4. Write the keyboard walkthrough as a short checklist in prompts.md under this phase for future
   regressions (not a separate file).

Verify: scripts/check.sh; screenshots at --size 360x640 for --page home, --page albums,
--open album:first and --now-playing, in both schemes; walk the checklist for real. Update
CLAUDE.md (shortcut list, a11y rules). Tick phase 18 and commit.
```

## Phase 19: Performance pass

**Goal.** Measure against the targets with a large fictional library and fix what misses.

**Not in this phase.** New features; SQLite unless JSON parsing alone breaks the target.

**Files.** `scripts/demo_library.py` (sizes), `scripts/bench.py` (new), `src/library.py`,
`src/widgets/artwork.py`, pages as needed, `src/main.py` (timing logs).

**Done when.** With 3,000 albums, 300 playlists and 40,000 tracks: content visible under 1 s from
launch, page switches under 100 ms, no frame over 16 ms while scrolling Albums and Songs, RSS under
250 MB after browsing every page; the numbers are recorded under this phase in prompts.md.

```text
Phase 19 of prompts.md: Performance pass. Read CLAUDE.md first, then src/library.py,
src/widgets/artwork.py, src/pages/grid.py and songs.py.

1. Generate a big fictional library: scripts/demo_library.py --cache build/demo-big with options
   for 3,000 albums, 300 playlists, 40,000 tracks (add options if missing). Add
   scripts/bench.py that launches the app with APPLE_MUSIC_CACHE=build/demo-big and --debug and
   reports: process start → window mapped, → library ready, → Albums page bound (log timestamps
   with GLib.get_monotonic_time at those points), then page-switch times across all root pages,
   and RSS from /proc/self/status. Scroll tests are manual with GDK_DEBUG=frames or the
   inspector's statistics.
2. Fix in this order, measuring after each: python3 -X importtime scripts/... to defer imports of
   pages and backend until needed; GObject wrapping per section on first access, splice batches;
   sorters as Gtk.PropertyExpression (C) everywhere; artwork requests cancelled on unbind and
   the LRU sized so a full screen of tiles plus one page ahead fits; Gtk.Picture fixed sizes to
   avoid relayout; GSettings writes debounced (last-page written on close, not each click);
   Gio.ListStore.find replaced by dict lookups. If json parsing of library.json alone exceeds 1 s
   on this machine, write the plan for a SQLite cache under this phase in prompts.md instead of
   doing it now.
3. Record the before and after numbers under this phase in prompts.md.

Verify: scripts/check.sh; scripts/bench.py output meets the targets; the demo and real libraries
still work. Update CLAUDE.md with any new rule. Tick phase 19 and commit.
```

## Phase 20: Packaging and release

**Goal.** A version people can install: native Meson install and an AUR PKGBUILD, accurate metainfo
with fictional screenshots, README, a tag; the Flatpak manifest kept working for development.

**Not in this phase.** Flathub.

**Files.** `meson.build` (version), `data/…metainfo.xml.in`, `data/screenshots/` (new),
`build-aux/aur/PKGBUILD` (new), `build-aux/flatpak/*.json`, `src/backend/chrome.py`
(`flatpak-spawn --host`), `README.md`.

**Done when.** `meson dist -C build` produces a tarball that builds from scratch; `appstreamcli
validate` passes with screenshots; `makepkg` in `build-aux/aur/` (if available) builds and installs;
the Flatpak build runs against the host Chrome; the tag exists.

```text
Phase 20 of prompts.md: Packaging and release. Read CLAUDE.md first, then meson.build,
data/*.metainfo.xml.in, build-aux/flatpak/*.json, src/backend/chrome.py and README.md.

1. Version: bump meson.build and add a <release> to the metainfo with a short description of what
   works. Screenshots: scripts/screenshot.py --demo for Home, Albums, an album page and Now Playing
   at 1100x760 in both schemes into data/screenshots/ (fictional data only; check the PNGs
   contain nothing real), referenced from <screenshots> with captions by their raw GitHub URL on
   the main branch. appstreamcli validate --no-net --explain must pass.
2. Native: build-aux/aur/PKGBUILD (pkgname gnome-apple-music or as Jack prefers; depends gtk4,
   libadwaita, python-gobject, glib2; makedepends meson, blueprint-compiler; optdepends
   google-chrome: playback engine). Verify it with makepkg -si if that is acceptable on this
   machine, otherwise with makepkg --nobuild plus a manual build of the dist tarball. Run
   meson dist -C build and build the tarball in a temp dir with its own --prefix.
3. Flatpak, development only: add --talk-name=org.freedesktop.Flatpak and the MPRIS --own-name
   for the release id; in chrome.py, when /.flatpak-info exists, run Chrome through
   flatpak-spawn --host and resolve the binary on the host; document in the manifest's comments
   (JSON allows none; use a "x-comment" key) that Flathub is out of scope because of the host
   Chrome dependency and the trademark. Build with flatpak-builder if the GNOME 50 runtime is
   available; otherwise note it was not tested.
4. README: what it is, requirements (Google Chrome for playback, why), install (meson, AUR),
   first run and sign-in, where data lives and how to remove it, development commands, license.
5. Tag: git tag -a v<version>.

Verify: scripts/check.sh; the dist tarball builds and runs; the .desktop, metainfo and schema
validate; screenshots contain only fictional data. Update CLAUDE.md (distribution facts). Tick
phase 20 and commit.
```

---

## Reusable prompts

### Review pass

```text
Review pass. Read CLAUDE.md first. Review the code changed since the last tag (or the last N
commits I name) for: blocking calls on the main loop (time.sleep, sync sockets, json.load of the
library, image decoding, Gio sync calls), GTK touched off the main thread, Box-of-widgets where a
model view belongs, strings missing _() or files missing from po/POTFILES.in, new .py/.blp files
missing from src/meson.build or the gresource, exceptions that would reach the user as a traceback
instead of a toast, real account data in fixtures or screenshots, and libadwaita widgets replaced
by custom CSS. Fix what is clear-cut, list what needs a decision, run scripts/check.sh and the
demo screenshots for any page you touched, and commit.
```

### Performance pass

```text
Performance pass on <page or feature>. Read CLAUDE.md first. Measure before touching anything:
scripts/bench.py (if present) with the big demo library, GDK_DEBUG=frames for scrolling,
python3 -X importtime for startup. State the numbers, then fix the biggest cost first: work moved
off the main loop, sorters and filters as Gtk expressions, artwork cancelled on unbind, models
spliced in batches, imports deferred. Re-measure, record before/after in prompts.md under the
phase or a "Performance log" heading, run scripts/check.sh, and commit.
```

### HIG and polish pass

```text
HIG and polish pass on <page or dialog>. Read CLAUDE.md first. Take screenshots with
scripts/screenshot.py --demo in dark, --light and at --size 360x640, and compare against the
GNOME HIG (spacing in multiples of 6, title styles, boxed lists, header bar contents, empty and
loading states, tooltips on icon buttons, ellipsizing) and the layout of music.apple.com described
in prompts.md. Prefer libadwaita widgets and style classes to CSS; remove CSS that a style class
covers. Fix, re-screenshot, look at every PNG, run scripts/check.sh, and commit.
```

### Fix this bug

```text
Bug: <what happens>. Expected: <what should happen>. Steps: <how to reproduce; demo or real
engine>. Logs: <paste from scripts/run.sh --debug, or "none">. Read CLAUDE.md first. Reproduce it
(with --demo if possible, otherwise the real engine), find the cause rather than the symptom,
add a unit test in tests/ when the bug is in the model, backend or sync layers, fix it, run
scripts/check.sh and a screenshot if the fix is visual, and commit with a message that names
the cause.
```

### Refresh the vendored backend

```text
Refresh the vendored backend from
/home/jackt/Projects/GNOME-Extensions/GNOME-Apple-Music-Library/src/backend. Read CLAUDE.md and
the provenance list in src/backend/__init__.py first. Diff each vendored file against upstream,
port upstream fixes that apply (cdp.py codec, sync.py normalisation, bridge.js calls) while
keeping this app's edits (config.py, the event subscription, queueJump, the async client),
update the provenance list, run scripts/check.sh and scripts/am.py status, and commit.
```
