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
GTK main thread = GLib main loop = asyncio loop (gi.events.GLibEventLoopPolicy)
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Window: AdwToastOverlay > AdwNavigationSplitView                                 │
│   sidebar: AdwSidebar: sections.py's fixed items; the Playlists section bound to │
│            a store of SidebarEntry (sidebar.py): All Playlists, Favourite Songs, │
│            then library.playlist_tree() depth first, folders collapsible         │
│   content: AdwNavigationView; a sidebar item replaces its stack with that        │
│            destination's root page (pages/, built on first visit and kept, each  │
│            with its own header bar): Home and Radio (shelves), GridPages, the    │
│            SongsPage (ColumnView), Favourite Songs and each sidebar playlist (a  │
│            DetailPage), each folder (a GridPage of its folders and playlists),   │
│            placeholders for the rest; tiles → window.open_item() pushes a        │
│            DetailPage (album, playlist), an ArtistPage or a folder's GridPage,   │
│            or plays a station; a shelf's See All →                               │
│            window.open_shelf() pushes a GridPage; track rows, Play, Shuffle,     │
│            stations → window.play_request(play, start_with, shuffle) (a toast    │
│            until phase 12*)                                                      │
│   PlayerBar stub at the bottom of the content (→ AdwBottomSheet bottom bar*)     │
│ app.library: Item/Track GObjects in Gio.ListStores, loaded from library.json     │
│   (parsed in a thread, wrapped in batches; the Songs store on request);          │
│   --demo reads build/demo instead                                                │
│ Artwork (widgets/artwork.py): thumbnails decoded in threads into a 200-texture   │
│   LRU, asked for by tiles, rows and covers while they are on screen              │
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
src/main.py                    Application: app.* actions (quit, about, shortcuts), GSettings, dialogs,
                               logging and --debug, --demo (app.demo), app.library (made in
                               do_startup, loaded in do_activate), spawn(coro),
                               use_glib_event_loop()
src/library.py                 the model: Library (state empty/loading/ready, 'changed', stores
                               albums artists playlists radio videos, shelves, by_id, shelf,
                               favourite_songs(), track_at(play, index), playlist_tree(),
                               folder_items(id), async load; songs filled by async
                               build_songs(), songs-ready), Item (favourites; kind 'folder' for
                               a playlist folder), Group, Track (search_key), Shelf (GType
                               AppleMusicShelfModel: the widget is AppleMusicShelf),
                               PlaylistTree/TreeNode (the folders); SongOrder (the Songs table's
                               orders), fold(), collation_key(); GObject/Gio only
src/window.py + window.blp     Window: split view, sidebar (the Playlists section bound to the
                               library's tree; folder expansion in `expanded-folders`), the
                               content's AdwNavigationView and its root pages (pages.create,
                               pages.playlist/folder, or a placeholder), last-page restore,
                               open_item(item), open_shelf(shelf), play_request(play,
                               start_with=None, shuffle=False), win.back, toasts, window state
src/sections.py                the fixed sidebar destinations (key, title, icon), grouped as on the web
src/sidebar.py                 the Playlists section's model: SidebarEntry (kind fixed/folder/
                               playlist, key, title, icon, depth, item, ancestors),
                               playlist_entries(tree), is_shown(), parse_key(), SidebarItem (an
                               Adw.SidebarItem holding its entry; a folder's arrow suffix)
src/player_bar.py + .blp       $AppleMusicPlayerBar, a stub transport bar
src/pages/__init__.py          PAGES: destination key → factory; create(destination, library);
                               playlist(library, id, title) and folder(library, id, title, root)
                               for the sidebar's playlists and folders
src/pages/grid.py + .blp       $AppleMusicGridPage: title over a Gtk.GridView of tiles, sort drop-down,
                               loading/empty states (albums, artists, recently-added,
                               all-playlists (the root folder), music-videos, folders; pushed
                               with root=False for See All and folder tiles); `model` a
                               Gio.ListModel or a function returning one; the title follows
                               the page's `title`
src/pages/home.py + .blp       $AppleMusicHomePage: title over a Gtk.Box of AppleMusicShelf, one per
                               non-empty library.shelves, the first as hero cards, all with See All
src/pages/radio.py + .blp      $AppleMusicRadioPage: the first HERO_COUNT (4) of library.radio as a
                               hero shelf ("Recently Played"), the rest in a Gtk.FlowBox of tiles
src/pages/songs.py + .blp      $AppleMusicSongsPage: title and count over a Gtk.ColumnView (Title,
                               Artist, Album, Time), a filter entry in the header; sorted and
                               filtered in Python (see its docstring), rows replaced by _show()
src/pages/detail.py + .blp     $AppleMusicDetailPage: an album or playlist, one Gtk.ListView whose
                               first row is the hero (cover, titles, Play/Shuffle, summary) and
                               then a section per group ("Disc 2" headers); pushed with an item,
                               or a root page following find() (Favourite Songs)
src/pages/artist.py + .blp     $AppleMusicArtistPage: round portrait, name, bio, a Gtk.FlowBox of
                               album tiles (the artist's groups, resolved through by_id)
src/widgets/artwork.py         the process-wide Artwork loader (get_default(): get, request, cancel);
                               art_colour(item.art_color) → Gdk.RGBA, is_dark(rgba)
src/widgets/tile.py + .blp     $AppleMusicTile: cover (or round portrait, set_artist(); a folder's
                               big folder icon) and one Gtk.Inscription
src/widgets/hero_tile.py + .blp  $AppleMusicHeroTile: a 260 px AppleMusicCover over a two-line
                               caption band in the item's art colour (drawn in do_snapshot)
src/widgets/shelf.py + .blp    $AppleMusicShelf: title row (title-2, subtitle, See All) over a
                               horizontal Gtk.ListView of tiles in its own scrolled window;
                               bind_shelf(shelf), `hero`, `see-all`
src/widgets/song_title.py + .blp  $AppleMusicSongTitle: the Songs title cell, 32 px thumbnail,
                               title, explicit badge
src/widgets/cover.py + .blp    $AppleMusicCover: artwork `size` px square over a placeholder card,
                               set_paths(*paths) (the first that decodes), loaded while mapped;
                               CSS classes `small`/`large` for the corners
src/widgets/track_row.py + .blp  $AppleMusicTrackRow: number (albums) or 40 px thumbnail
                               (playlists), title + badge, artist, duration
src/style.css                  auto-loaded app CSS: accent colour and a few small classes
src/icons/*-symbolic.svg       bundled icons, aliased into icons/scalable/actions/ by the gresource
src/applemusic.gresource.xml   compiled .ui files (subdirectories' aliased to the root: grid.ui,
                               songs.ui, detail.ui, artist.ui, home.ui, radio.ui, tile.ui,
                               song_title.ui, cover.ui, track_row.ui, shelf.ui, hero_tile.ui),
                               style.css, icons
src/meson.build                blueprint list, gresource, install_data lists (app .py; pages/,
                               widgets/, backend/ each their own)
src/backend/                   the engine layer: vendored from the extension plus this app's async
                               layer; no gi, asyncio and stdlib only. __init__.py records provenance
                               and every edit, README.md the command table, error codes, the events
                               and the library.json/Item/Track shapes
  errors.py                    EngineError(code, message): engine-down, not-signed-in, api, timeout,
                               usage
  chrome.py                    find_chrome(), chrome_args(binary, profile, port, headless),
                               EngineState (engine.json load/save/remove, .alive), pid_alive(),
                               async wait_for_devtools/list_targets/find_target/wait_for_target
                               (urllib in a thread); select_target() picks the music.apple.com page
  client.py                    CDPClient: connect(ws_url), call(), evaluate(), bridge(method, *args),
                               on/off(event, cb(name, data)) for CDP events and 'am:<event>' bridge
                               events ('am:*', '*' wildcards), ensure_bridge() (kept across the
                               page's navigations), subscribe(), close(), wait_closed();
                               connect_page(port)
  config.py                    cache_dir() profile_dir() port() state_file(profile), APPLE_MUSIC_CACHE/
                               _PROFILE/_PORT overrides, THUMB_SIZE 320, COVER_SIZE 640, BRIDGE_JS
  cdp.py                       the WebSocket handshake and frame codec as pure functions (shared with
                               client.py) and the extension's blocking client on them
  bridge.js                    injected into music.apple.com (window.__appleMusicLibrary); installed
                               as data beside the Python; subscribe() forwards MusicKit events through
                               the window.__amEvent binding as {name, data}
  sync.py                      API answers → Item/Track, artwork cache (.sizes, scale_image hook),
                               library.json writing
  am.py                        REFERENCE ONLY (the extension's CLI): not installed, imported or
                               linted; phases 10-11 port its sync and command bodies, then delete it
data/                          desktop, metainfo, gschema, app icons; Meson tests validate them
po/                            gettext; POTFILES.in must list every file with translatable strings
scripts/run.sh check.sh screenshot.py demo.sh scroll_test.py
scripts/am.py                  the engine's debug CLI (no GUI): status, start [--visible], stop,
                               eval <js>, now-playing, events; the app's port and profile
scripts/demo_library.py        invented library.json + drawn artwork (config sizes) into --cache DIR;
                               --albums N adds generated albums and artists; the last playlist is
                               Favourite Songs (attributes.isFavourites); `folders`: three
                               playlist folders (l.fd002 inside l.fd001) and loose playlists
tests/                         stdlib unittest; __init__.py registers src/ as `applemusic`;
                               fixtures/ holds invented API answers
pyproject.toml                 ruff config only (line length 100, E/F/W; am.py excluded, vendored
                               files exempt from E501)
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
scripts/run.sh [args]     meson setup (dev profile, prefix build/install) + install + run;
                          `--debug` (or APPLE_MUSIC_DEBUG=1) logs at DEBUG
scripts/check.sh          compileall, ruff (skipped if not installed), unit tests, meson compile,
                          meson tests (desktop/metainfo/schema validation); prints `check: ok`
scripts/demo.sh [args]    run.sh --demo: the app on the invented library in build/demo (generated
                          first when missing); no Chrome, no account. Use it for all UI work
scripts/screenshot.py [out.png] [--light] [--size WxH] [--page KEY] [--demo] [--open KIND:ID]
                      [--expand ID[,ID…]]
                          renders the real window to a PNG; needs a display and a prior run.sh/install;
                          dark by default; GSettings go to a memory backend; animations off;
                          --demo as demo.sh (without it the real cache is read); waits for the
                          library to load; in the narrow layout shows the page when --page or
                          --open is given, else the sidebar. --page takes a last-page value
                          (a destination key, playlist:ID, folder:ID); --expand opens sidebar
                          folders ("first": the library's first). --open album:first (or
                          artist/playlist/station/video/folder, ID an id or "first") calls
                          window.open_item over the page and waits 1.5 s more for artwork.
                          The sidebar is not scrolled: --size 1100x1000 shows all the demo's
                          playlists
scripts/scroll_test.py [--page KEY] [--speed PX_PER_S] [--distance PX] [--size WxH] [--sidebar]
                          scrolls a page (or, with --sidebar, the sidebar) of the demo library top
                          to bottom (or PX pixels) and reports the app's work per frame (mean, 90th
                          percentile, frames over the refresh interval and over 16.7 ms); run it
                          on a big library:
                          APPLE_MUSIC_CACHE=build/demo-2000 scripts/scroll_test.py --page albums
                          (Songs of 30,000: --page songs --distance 40000, the whole is 1.5M px)
python3 -m unittest discover -s tests -v      unit tests alone, from the repo root
scripts/am.py [--debug] status | start [--visible] [--browser CMD] | stop | eval [--no-await] JS |
              now-playing | events
                          drives the engine without the GUI, on the app's port and profile
                          (APPLE_MUSIC_PORT/APPLE_MUSIC_PROFILE override; the .Devel build's
                          are 9229 and chrome-devel). One JSON value per command, or
                          {"error": code, "message"} and exit 1. `start` leaves Chrome running
                          (headless unless --visible) and reuses one already up; `events`
                          prints bridge events one per line until Ctrl+C. Never signs in.
scripts/demo_library.py [--cache DIR] [--albums N]
                          writes an invented library (library.json, art/, thumb/, art/.sizes) into
                          DIR, default build/demo; no Chrome. --albums 2000 (about 24,000 songs,
                          10 s: covers drawn in parallel) into build/demo-2000 for measuring;
                          --albums 2500 gives 30,116 songs (build/demo-2500, the Songs page's)
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
  `Gtk.Box` of hundreds of widgets. Past a few thousand items that the user re-sorts or filters
  interactively (Songs), order and filter in Python and splice the result into the view's
  `Gio.ListStore` (`pages/songs.py`): GTK's sorters and filters read each Python item's
  properties from C at about 3 µs a read, which made them 5 to 100 times slower there.
- The model (`src/library.py`): pages bind `app.library`'s stores, which keep their identity across
  loads and are refilled in place; watch `notify::state` and `changed`. Playlist folders:
  library.json's optional top-level `folders` is a list of {id, title, parent, children: [{kind:
  folder|playlist, id}]} in Apple's order, the entry with id `root` listing the top level
  (playlists in no folder included); the children lists decide, `parent` is informational.
  `library.playlist_tree()` (a new PlaylistTree per load) has `root` (nested TreeNodes: item,
  depth, parent, children, store) and `flat` (depth first, with depth); whatever no list reaches
  goes at the end of the top level, so a library without `folders` is all playlists at the top.
  Folders are Items of kind `folder` (`by_id('folder', id)`, no art, no groups);
  `folder_items(id)` is a folder's Gio.ListStore of folder and playlist Items (`root`: All
  Playlists), replaced by each load, so pages follow a folder by id. `item.groups` (Group:
  `name`, `play`, `entries` store of Track) is wrapped on first access. `library.songs` stays
  empty until `await library.build_songs()` (the Songs page asks when first shown; batched with
  `yield_to_frames()`, one splice at the end), and every load after that refills it;
  `songs-ready` says it is filled, `library.song_count()` counts without building. Model GObjects declare properties with
  `library.model_property` (kept in `_<name>` attributes, assigned directly when wrapping):
  passing properties to `GObject.Object.__init__` costs about 4 µs each, 5-10x slower wrapping.
- Sorting: `Gtk.StringSorter`/`Gtk.NumericSorter` (combined with `Gtk.MultiSorter`) over
  `Gtk.PropertyExpression`s of the model properties. `model_property` makes real GObject
  properties whose getter reads the plain attribute, so expressions work as they are; key-based
  sorters read each item once and sort in C. Measured on 2,000 albums: 10 ms (title), 25 ms
  (artist, year, title); a Python `Gtk.CustomSorter` 23 ms, and on 24,000 songs 115 ms against
  570 ms. A `Gtk.ClosureExpression` over the attribute halves the read cost if it ever matters.
  A `Gtk.ColumnViewSorter` has no sort keys: a `Gtk.SortListModel` over `column_view.get_sorter()`
  compares pairs and evaluates both items' expressions at every comparison (1.6 to 6 s a click
  on 30,000 songs). Give columns sorters so their headers sort, but read the primary column and
  order from the view's sorter (`changed`) and sort yourself, as the Songs page does with
  `library.SongOrder`.
- Recycled rows and tiles (`Gtk.GridView`, and phase 5's `Gtk.ColumnView`): GTK 4.22's grid
  rebinds each item many times as it scrolls past (2,000 items, 52,000 binds top to bottom), so
  bind must be cheap. Text goes in `Gtk.Inscription`, whose size comes from its line count and
  not its text, so a rebind is a redraw; a wrapping `Gtk.Label` re-measured on every rebind cost
  7 ms a frame at 4,000 px/s against 1 ms. No `_()` in bind (gettext searches the disk on every
  call: look strings up once), no label-backed widgets (`Adw.Avatar` initials) in tiles. Artwork
  is requested on map and released on unmap: the grid binds a few hundred tiles, ~20 on screen.
  A `Gtk.ListView`/`Gtk.ColumnView` keeps 200 rows alive (`GTK_LIST_VIEW_MAX_LIST_ITEMS`), and
  a model change that removes the items they show destroys their widgets and builds new ones
  (about 0.5 ms a Songs row, 100 ms a keystroke); only widgets whose items survive the change are
  recycled. To replace a list's contents, insert the new items first, `scroll_to(0)`, then
  remove the old ones (`SongsPage._show()`): the view then only rebinds (about 800 cell binds,
  20 ms). Give a table's columns fixed widths (`fixed-width` plus `expand`) so they never
  measure their cells.
- Artwork: widgets draw covers through `widgets.artwork.get_default()`: `get(path)` (cache hit,
  sync) or `request(path, callback)` (decoded in a thread, called back on the main loop; shared
  per path) and `cancel(token)` when recycled or unmapped. None means no artwork, including a
  path not on disk; tiles draw `thumb` (320 px), falling back to `art`; a detail page's hero
  draws `art` (640 px), falling back to `thumb` (shown at once when its tile left it cached).
  `widgets.cover.Cover` does this for any square artwork; the tiles and Songs cells keep their
  own copies of the same logic.
- Main-thread work in chunks yields with `library.yield_to_frames()`, not a bare
  `await asyncio.sleep(0)`: asyncio runs at `G_PRIORITY_DEFAULT`, above GTK's redraw, so sleep(0)
  alone paints nothing until the task ends (measured: 0 frames against 5 in the same load).
- The backend is reached only through the `Engine` object (coroutines: `await app.engine.play(...)`),
  spawned from signal handlers with `app.spawn(coro)`. Errors are `EngineError(code)` with the
  README's codes (`engine-down`, `not-signed-in`, `api`, `timeout`); the UI shows a toast, never a
  traceback. Library reads are synchronous in-memory models.
- Demo mode: `--demo` sets `app.demo = True`, runs as its own instance (`NON_UNIQUE`) and points
  `APPLE_MUSIC_CACHE` at the launcher's `DEMO_DIR` (the source tree's `build/demo`) unless the
  variable is already set, so `APPLE_MUSIC_CACHE=DIR scripts/demo.sh` shows another generated
  library. Treat `app.demo` as "no engine": never start Chrome or sign in under it.
- Logging: Python `logging`, `log = logging.getLogger(__name__)`; configured once in `main.main()`
  (`basicConfig`, INFO to stderr as `LEVEL logger: message`; DEBUG with `--debug`, handled in
  `do_handle_local_options`, or with `APPLE_MUSIC_DEBUG` set to anything but empty or `0`). No
  `print` in app code.
- Async: `app.spawn(coro)` runs a coroutine as a task on the GLib-backed asyncio loop, keeps a
  reference until it finishes, logs its exception (cancellation is silent) and returns the task for
  cancelling. Use it from signal handlers instead of threads or `GLib.idle_add`; blocking work
  inside the coroutine goes through `asyncio.to_thread`.
- Pages: a destination's root page is an `Adw.NavigationPage` with its own `Adw.ToolbarView` and
  `Adw.HeaderBar` (`show-title: false`; the header bar still shows the back button to the sidebar
  when collapsed), registered in `pages.PAGES`. Pages listen to `app.library` only while mapped
  (connect in `do_map`, disconnect in `do_unmap`): the library outlives the window. Pushed pages
  (detail, artist) show their title in the header bar and keep what they show in `page.item`.
  Breakpoints need an `Adw.BreakpointBin` inside the page (a navigation page takes none).
  A pushed `GridPage` (`root=False`, See All) shows its title in the header bar as well as in
  the content, as a detail page's hero repeats its header's.
- Shelves: a page of shelves (Home, Radio; phase 15's Search, New, Made for You) is a vertical
  `Gtk.Box` of `AppleMusicShelf` in a `Gtk.ScrolledWindow` with the `view` style class (the
  lists' background): a handful of shelves, each a horizontal `Gtk.ListView` that recycles its
  tiles in its own scrolled window. Keep the shelf widgets and `bind_shelf()` new Shelf objects
  into them on a reload (`HomePage._show()`). A grid under a shelf in the same scrolled window
  cannot be a `Gtk.GridView` (see below), so Radio's is a `Gtk.FlowBox` of tiles (recent
  stations are tens); anything unbounded belongs on its own page (See All). Tiles sit 18 px
  apart with the first cover on the titles' 24 px margin (`listview.shelf-list` in style.css).
  A shelf item that is also in a section is the section's Item object (`library._fill`).
- Per-item colours (the hero cards' band in `art_color`) are drawn in the widget's own
  `do_snapshot` (`snapshot.append_color`, then chain up), inside its rounded `overflow: hidden`
  clip; CSS cannot take a value per item, and a per-widget CSS provider is out (CSS lives in
  style.css). Keep such Python snapshots off the grid tiles: the hero card is its own class.
- The seams to the rest of the app, on the window (`self.get_root()` from a page):
  `open_item(item)` pushes the item's page (album/playlist → `DetailPage`, artist →
  `ArtistPage`, folder → its `GridPage` (`pages.folder(root=False)`); a station has no page and
  goes to `play_request(item.play)`; videos toast for now; the same item twice in a row is pushed
  once); `open_shelf(shelf)` pushes a `GridPage` of
  the shelf's items (See All; a library shelf is followed by key across loads, any other shown
  as it is);
  `play_request(play, start_with=None, shuffle=False)` is every "play this": a track row passes
  `track.play, start_with=track.index` (its group's target and its place in that queue), Play and
  Shuffle `item.play` (with `shuffle=True`). It toasts until phase 12 hands it to the engine;
  `library.track_at(play, index)` finds the track a request starts with.
- Tests: `tests/test_<module>.py`, stdlib `unittest`, each starting with `from tests import …`
  (e.g. `SRC`, `ROOT`) before any `from applemusic import …`: discovery with `-s tests` imports test
  modules as top-level modules and never runs `tests/__init__.py` on its own. No GTK widgets in
  tests; UI is checked with screenshots.
- Lint: `pyproject.toml` configures ruff; imports after `gi.require_version()` need `# noqa: E402`.
- Settings: one schema `io.github.jackicus.AppleMusic` for both profiles; new keys go in
  `data/…gschema.xml` with a summary, and are read through `app.settings`.
- Actions: `app.*` in `main.py`, `win.*` in `window.py`; accelerators via `set_accels_for_action`;
  every shortcut also appears in the shortcuts dialog.
- Style: 4-space Python, single quotes, no type-annotation ceremony, a docstring where a module or
  function is not obvious. New `.py` files go in `src/meson.build`'s `install_data` list.

## Verifying a change

1. `scripts/check.sh` passes (it grows: tests, lint).
2. Anything visual: `scripts/run.sh` (or `scripts/demo.sh`) builds, then
   `scripts/screenshot.py build/shot.png --demo --page KEY` and look at the PNG with the Read tool;
   also `--light`, and `--size 400x700` for anything adaptive.
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
  `mode: page` turns it into boxed lists for the collapsed layout (the window's breakpoint sets it).
  Measured in phase 8 (the Playlists section is bound, `window._update_playlists`):
  - It is a `Gtk.ListBox` of real rows (one per item, hidden ones `visible: false`) in its own
    `Gtk.ScrolledWindow`, not recycled. 169 playlist entries (162 playlists, 7 folders): about
    24 ms to splice in, then a 15-17 ms frame; scrolling 151 visible rows at 2,000-4,000 px/s,
    0.5 ms of work a frame, none over 4.2 ms (`scroll_test.py --sidebar`). Fine at ~150; a few
    thousand playlists would want a lighter path. The same shapes on a reload keep the items
    (only their entries' Items are swapped), so a reload costs 5 ms and loses nothing.
  - A click selects (`notify::selected-item`) and then emits `activated`; a click on the
    selected item only `activated`; `set_selected()` only the notify. So selection shows pages
    and `activated` toggles folders (arrow keys select without toggling).
  - Splicing out the selected bound item sets `selected` to INVALID (items before the splice keep
    theirs), and bind_model makes new items for everything spliced in: reselect by key after.
  - With nothing selected its list selects the row with the focus: the first (Search) when the
    window is shown and again when it becomes active. Never leave it empty on purpose (a
    playlist restored before the library loads keeps All Playlists selected meanwhile), and
    route the window's own selections through `_set_selected` (`_quiet`), which shows nothing.
  - It cannot indent: a folder's contents follow it, the arrow (`pan-end`/`pan-down-symbolic`
    suffix) says whether they show. In page mode the suffix sits before the row's own arrow.
    Selecting does not scroll the list to the item (a restored playlist far down stays off
    screen; the API has no scroll-to).
- Blueprint 0.22: `template $AppleMusicWindow: Adw.ApplicationWindow {}`; a custom widget used in a
  template (`$AppleMusicPlayerBar`) needs its Python class imported before the template is built
  (hence `from .player_bar import PlayerBar  # noqa: F401` in `window.py`); `[top]`/`[bottom]`/`[end]`
  slots; `styles ["flat"]`; `Adw.Breakpoint { condition ("max-width: 640sp") setters { … } }`;
  `menu primary_menu { … }` at top level; handlers `clicked => $on_clicked();`; bindings
  `label: bind item.title;`. Each new `.blp` goes in `src/meson.build`'s blueprint list and in
  `applemusic.gresource.xml` as `.ui`.
- The source tree is not importable as `applemusic`; the launcher imports it from
  `build/install/share/apple-music/applemusic`, where the gresource also lives. `tests/__init__.py`
  registers `src/` under that name (spec_from_file_location + sys.modules) for the tests;
  `scripts/demo_library.py` does the same to reach `applemusic.backend`.
- The vendored backend and its tests keep upstream's style (double quotes, annotations, long
  lines, which pyproject exempts from E501) so a refresh from the extension diffs cleanly (see the
  "Refresh the vendored backend" prompt); new backend modules follow this file's style. Nothing in
  `src/backend/` imports gi (a test checks); `sync.scale_image` is the hook through which the app
  lends GdkPixbuf for scaling covers into thumbnails (unset, thumbnails are fetched).
- Icons in `src/icons/` resolve by `icon-name` through the resource alias; symbolic SVGs use a `#222`
  fill and are recoloured. Adwaita no longer ships some legacy names (`emblem-favorite-symbolic` is
  gone); bundle anything not in `/usr/share/icons/Adwaita/symbolic/`.
- `screenshot.py` uses a `.Screenshot` app ID and renders after 1.2 s, and not before the library
  has loaded, so content that arrives later still needs a longer delay; it prints a harmless at-spi
  warning (and, on this desktop, an Adwaita one about gtk-application-prefer-dark-theme). It makes
  the window non-resizable so the desktop's tiling extension (Tiling Shell) honours `--size`; so
  shots have no maximize button.
  It calls `main.use_glib_event_loop()` itself, since it builds the Application without `main()`.
  In the collapsed (narrow) layout it shows the page when `--page` is given, the sidebar otherwise.
- A `Gtk.GridView` recycles only as the direct scrollable child of a `Gtk.ScrolledWindow`: in a
  box inside a viewport it creates a few hundred tiles and leaves the rest blank. So the grid
  page's `title-1` title is an overlay child, laid over the grid's CSS top padding (which scrolls
  with the content in GTK 4.22) and moved up by `get-child-position` as the grid scrolls. Row
  heights come from the tiles' minimum heights. After a sort change the grid would follow its old
  top item; `grid_view.scroll_to(0, …)` puts it back at the top.
- A `Gtk.ListView` gives each row its *minimum* height: a widget whose minimum is below its
  natural size is squashed in a row (an `Adw.StatusPage`, a scrolled window inside, got 58 px of
  292). Tab never reaches focusable widgets in a list *header* (`header-factory`), only in items
  (with `tab-behavior: item`, Tab goes through the focused item's widgets, then leaves; arrows
  move between items). Hence the detail page's hero is its list's first item, not a header, and
  a box that looks like a compact status page. A `Gtk.FlattenListModel` is a `Gtk.SectionModel`
  (one section per child model), which is what the "Disc 2" headers hang on.
- Nested scrolling (GTK 4.22, `gtkscrolledwindow.c`): a scrolled window handles a scroll event
  only along an axis it can scroll (its scrollbar is visible), and returns PROPAGATE for one
  wholly along the other, so a vertical wheel over a shelf (`vscrollbar-policy: never`) scrolls
  the page and a horizontal one the shelf, with no code (checked by emitting `scroll` on both
  bubble-phase `Gtk.EventControllerScroll`s: the shelf's declines (0, 1), the page's then
  scrolls 75 px). An event with any horizontal part (a diagonal touchpad swipe) is kept by the
  shelf; libinput locks two-finger scrolling to one axis, and once the page has started a
  touchpad scroll its capture-phase controller keeps the gesture even over a shelf. Emitting
  `scroll` on a controller from Python runs GTK's handler without an event (no modifiers, wheel
  units); pick the bubble-phase one, the capture-phase one only continues a scroll in progress.
- libadwaita's `.card` sets its own `color` (`--card-fg-color`, dark in the light theme), so a
  `card` placeholder on a coloured background needs `color: inherit`; `--card-bg-color` is
  white in the light theme, invisible on the `view` background (the hero cards tint with
  `color-mix(in srgb, currentColor 8%, transparent)` instead).
- `Adw.NavigationView` pops on Escape, Back, `<Alt>Left` (class shortcuts, *local* scope: only
  with the focus inside it) and the mouse back button (a click gesture on every button). A push
  moves the focus into the new page (the detail page's Play button), so the keys work there;
  `win.back` (`<alt>Left`, in the shortcuts dialog) does the same from the sidebar or the player
  bar, and is disabled when there is nowhere to go back to, so the keys pass through.
- `Gtk.ColumnView.sort_by_column()` does not tell the previous primary column to drop its sort
  arrow (GTK 4.22's `gtk_column_view_sorter_set_column`); clicking a header does. Use it only
  for the initial order, or call `sort_by_column(None, …)` first.
- Frame timing on this desktop varies with the compositor and the other load (the monitor is
  240 Hz; unfocused windows may get 60), so `scroll_test.py` reports the app's own work per frame,
  which is what the app controls. The desktop's tiling extension resizes windows that are
  resizable when mapped: test scripts make theirs non-resizable in `window-added`.
- The dev build shares the release schema and resource path; only the app ID, desktop file and icons
  differ. `run.sh` sets `GSETTINGS_SCHEMA_DIR` and `XDG_DATA_DIRS` to `build/install`.
- Chrome: `google-chrome-stable` 154 is installed. Its MPRIS player is
  `org.mpris.MediaPlayer2.chromium.instance<pid>`, disabled by
  `--disable-features=HardwareMediaKeyHandling`. The extension's engine uses port 9227 and profile
  `$XDG_DATA_HOME/apple-music-library/chrome`; this app uses 9228 and `$XDG_DATA_HOME/apple-music/chrome`
  (the `.Devel` build 9229 and `chrome-devel`) so they can run side by side, which also means a
  separate sign-in per profile.
- The engine layer (phase 9): `EngineState.alive` is the pid *and* `--user-data-dir=<profile>`
  on its `/proc` command line, so a reused pid is never mistaken for our Chrome; the state file
  is keyed by profile (`config.state_file(profile)`: the default profile's in
  `$XDG_RUNTIME_DIR/apple-music/engine.json`, any other's inside the profile). A
  `Runtime.addBinding` is per CDP session: each connection registers `__amEvent` itself and
  the last one to connect owns the page's `window.__amEvent`. `Runtime.enable` replays
  `executionContextCreated` for existing contexts; the client re-injects only once
  `ensure_bridge()` has been asked for, and only for the main frame's default context
  (iframes such as Apple's sign-in get their own). MusicKit's `PlaybackStates` names
  (`none loading playing paused stopped ended seeking waiting stalled completed`) are what
  `am:playbackStateDidChange` carries; a play walks playing → waiting → loading → playing.
  An asyncio subprocess transport kills its child when garbage-collected (as a CLI command
  exits), so `scripts/am.py` spawns Chrome with `subprocess.Popen(start_new_session=True)`;
  the app spawns with `Gio.Subprocess`. Visible Chrome is an `--app=` window (no tabs or
  address bar), as the extension had it. Unauthorised MusicKit plays 30-second catalog
  previews, which is enough to exercise playback and events without signing in.
- Python 3.14 deprecates `asyncio.set_event_loop_policy` (removal in 3.16), but it is still how
  PyGObject 3.56 puts asyncio on the GLib loop; `main.use_glib_event_loop()` filters the
  DeprecationWarning and sets the policy. PyGObject's `Gio.Application.run` marks the GLib loop as
  the running asyncio loop only when that policy is set.
- History starts at the "Scaffold: window, sidebar, build" commit; one commit (or a few) per phase.
