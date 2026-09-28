# Decisions

The design decisions this app rests on, with the reasons and what was given up. When one
changes, change it here, with the date. The plan they came from is in
`docs/history/build-plan.md`.

## Product

- **Distribution: native first** (2026-09-27). Releases are `meson install` and an AUR package
  (`build-aux/aur/PKGBUILD`). The Flatpak manifest is for development only: from the sandbox,
  the app runs the host's Chrome through `flatpak-spawn --host`. Flathub is out of scope: it
  would refuse an app that depends on the host's Chrome, and the name and ID carry Apple's
  trademark.
- **A profile of its own** (2026-09-27). The app's Chrome uses its own profile
  (`$XDG_DATA_HOME/apple-music/chrome`, the development build `chrome-devel`), so it never
  fights another Chrome, such as the GNOME Shell extension's, over a profile lock. The cost is
  one sign-in per profile, and two Chromes when both run.
- **The development build's own paths** (2026-09-28). The .Devel build has its own cache
  (`$XDG_CACHE_HOME/apple-music-devel`) beside its own Chrome profile, chosen in
  `backend/config.py` from the build profile, so a development sync, Clear Cache or Sign Out
  never rewrites or deletes the release build's library. The settings stay shared (see Open).
- **The player at the bottom** (2026-09-27). A full-width bar at the bottom of the window, as
  in GNOME Music, with Now Playing as an `AdwBottomSheet` that slides up over the content and
  holds Lyrics and Up Next. The web player puts its player at the top; the bottom keeps the
  header bar for titles and back buttons, which is what GNOME users expect.
- **Playlist folders in the sidebar** (2026-09-27). Folders are sidebar items with a folder icon
  and a disclosure arrow; activating one shows or hides its playlists and opens the folder's
  page. `AdwSidebar` cannot indent, so nesting shows through order and the arrow.

## Engine

- **Chrome as the engine** (2026-09-27). See "Why there is a browser inside" in
  `docs/architecture.md`. The real Chrome, because Chromium ships without Widevine.
- **The app owns MPRIS** (2026-09-27). The app publishes `org.mpris.MediaPlayer2.<app id>`
  from its Player, and Chrome runs with `--disable-features=HardwareMediaKeyHandling` so its
  own player never appears. A module of D-Bus code and every command relayed, in return
  for the Shell showing this app's name and icon, Raise focusing this window, the media keys
  reaching it, and a service that survives engine restarts.
- **DevTools over a pipe, no port** (2026-09-28). Chrome speaks CDP on its file descriptors 3
  and 4 (`--remote-debugging-pipe`), not on `--remote-debugging-port`. A DevTools port on
  127.0.0.1 answers every local user and every process that can open a socket, sandboxed apps
  with network access included, and whoever reaches it has full control of a browser signed in
  to the user's Apple Account: its session, its library, playback. A pipe reaches only the
  process that spawned Chrome. What it cost: a Chrome from an earlier run can no longer be
  reclaimed (so Chrome is tied to the app's life, below), the port preference went, and
  `scripts/am.py` runs a Chrome of its own unless the app opted in to a port. That opt-in is
  `APPLE_MUSIC_DEBUG_PORT`, for developers only: it opens a port on 127.0.0.1 beside the pipe,
  the log warns at every start, and nothing sets it by default, since while it is set the
  signed-in session is open to local users again.
- **Chrome lives and dies with the app** (2026-09-28). Chrome is spawned through
  `setpriv --pdeathsig TERM`, so the kernel ends it however the app ends (a crash, a SIGKILL, a
  lost display), not only on a clean quit; `flatpak-spawn --watch-bus` does the same in the
  sandbox.
  Which Chrome holds the profile is read from Chrome's own `SingletonLock`, checked against
  `/proc`, and a start ends such a Chrome before spawning its own. No state file records the
  engine's pid any more: the old one could sit at a predictable path in /tmp, and its check
  matched the profile as a substring. Without util-linux's `setpriv`, a crash can leave Chrome
  running until the next start.
- **State from events, not polling** (2026-09-27). What is playing comes from MusicKit's events,
  forwarded through a CDP binding (`Runtime.addBinding`), plus one read when the engine comes
  up. The only polling is while waiting for sign-in.
- **The backend is a fork** (2026-09-28). `src/backend/` began as a copy of the extension's
  backend, and this app has since rewritten large parts of it. It is maintained here, for this
  app; the old files keep the extension's code style until they are reformatted.

## Code

- **asyncio on the GLib main loop** (2026-09-27), through PyGObject's `GLibEventLoopPolicy`.
  CDP is requests and events, which coroutines model directly: no locks, no hops between
  threads, and UI code awaits the engine in place. Blocking work goes to `asyncio.to_thread`;
  Chrome is spawned with `Gio.Subprocess`. Python 3.14 deprecates the policy call PyGObject
  still needs; if PyGObject changes its entry point, only `main.use_glib_event_loop()` changes.
- **One navigation view** (2026-09-27). The content pane is one `AdwNavigationView`. A sidebar
  item replaces its stack with the destination's root page, made on first visit and kept (a
  playlist's or folder's page only while it is among the last few shown); tiles push detail
  pages; root pages show a large in-content title and hide the header bar's.
- **The cache as the library** (2026-09-27). `$XDG_CACHE_HOME/apple-music/library.json` is the
  one library snapshot, parsed off the main thread from the start of the process and wrapped
  into GObjects a section at a time. Thumbnails (320 px) are fetched for every item at sync;
  covers (640 px) on demand. Decoded textures live in a 32 MB LRU at the size they are drawn.
- **Songs sorted in Python** (2026-09-27). Past a few thousand rows that the user re-sorts and
  filters, GTK's sorters read each Python item's properties from C too slowly, and a column
  view's own sorter compares pairs. The Songs page sorts and filters in Python and splices the
  result; every other list sorts with GTK's expression sorters.
- **The model's own words** (2026-09-28). What the app calls things (an untitled album,
  Home's own shelves, a count caption such as "12 songs, 43 min") is made and translated in
  `library.py` from the data, not read from English text the backend wrote into
  library.json, which gettext cannot reach. The captions are made once per Item, never in a
  list's bind.
- **Demo mode** (2026-09-27). `--demo` runs the app on a generated, invented library, so UI
  work, tests and screenshots never need Chrome or a real account.
- **Tests** (2026-09-28). Stdlib `unittest`, nothing to install. Logic lives in classes and
  functions without widgets, tested with stand-ins. Widget tests are allowed through
  `tests/gtk.py`, skipped without a display; CI runs them under Xvfb. The rest of the UI is
  checked with screenshots of the demo library. (First decided as screenshots only, no widget
  tests; changed because the logic that broke lived in widget classes.)
- **Weak connections in droppable widgets** (2026-09-28). A page, shelf, dialog or row that can
  be dropped connects its children's signals through `widgets.util.connect_weak()` and has no
  Blueprint handlers, because a connection to its own bound method forms a cycle through C that
  kept every popped page alive. `tests/test_page_lifetime.py` holds the line.

## Open

- The app icon and the display name "Apple Music" resemble Apple's own; whether to change them
  is the owner's call.
- The development build shares the release build's settings, `signed-in`, `account-name` and
  `last-sync` included, though each has its own Chrome profile and cache: signing in one build
  marks the other signed in when its profile is not.
