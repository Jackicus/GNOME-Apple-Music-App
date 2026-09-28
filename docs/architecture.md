# Architecture

How Apple Music for GNOME is put together, for someone about to change it. Each module's
docstring has the detail; this page is the map and the flows between the parts.

## Why there is a browser inside

Apple Music's streams are Widevine-protected, WebKitGTK cannot play them, and Apple publishes
no streaming API. Google Chrome can play them. So the app starts its own Chrome, with a
private profile, on music.apple.com, and drives Apple's MusicKit JS in that page over the
Chrome DevTools Protocol. The protocol runs over a pipe between the app and its Chrome
(`--remote-debugging-pipe`, Chrome's file descriptors 3 and 4), so no port is open for other
programs to reach the signed-in session through. `src/backend/bridge.js` is injected into the
page and is the only code that runs there; MusicKit's events come back through a CDP binding.
Chrome shows a window once, for signing in, and runs headless afterwards. The sound comes out
of Chrome, and Chrome ends when the app does.

## One thread, one loop

GTK's main loop is also the asyncio loop (`main.use_glib_event_loop()` sets PyGObject's
`GLibEventLoopPolicy`). UI code awaits the engine in place, with no locks and no hops between
threads. Work that would block (parsing library.json, decoding images, downloading artwork,
reading files) runs in threads through `asyncio.to_thread`, and only the main thread touches
widgets.

## The parts

- **Application** (`main.py`): owns the settings, the library, the engine, the player, the
  MPRIS service and the sync (`LibrarySync`, one run at a time); the `app.*` actions and
  accelerators; the dialogs; quitting; demo mode. Signing out and clearing the cache are
  `account.py`'s, background playback `background.py`'s, the startup marks `timing.py`'s.
- **Window** (`window.py`): an `AdwBottomSheet` whose content is the split view (the
  `AdwSidebar` and an `AdwNavigationView`) and whose bottom bar and sheet are the player bar and
  Now Playing. Choosing a sidebar item replaces the navigation stack with that destination's
  root page; tiles and rows push album, playlist, artist and See All pages through the window's
  `open_item()`, `open_shelf()` and `open_songs()`, and every "play this" goes through its
  `play_request()`. The item actions and context menus are `actions.py` and
  `widgets/context_menu.py`.
- **Pages** (`pages/`): one module per destination or pushed page, built when first shown. The
  library pages bind the library's stores; New, Made for You and Search ask the engine (the
  first two, and Search's categories, keep its answers in the cache for a day).
- **Library** (`library.py`): the model. GObjects in `Gio.ListStore`s, filled from the cache's
  library.json. It has no GTK, so it is tested without a display. A reload after a sync keeps
  every object still in the library and says what changed through property notifications and
  `groups-changed`, which open pages follow.
- **Sync** (`sync.py`): fetches the library through the engine, turns Apple's answers into the
  library.json shapes with the backend's pure functions, downloads missing thumbnails, writes
  the file atomically and has the library reload itself in place.
- **Engine** (`engine.py` over `backend/`): Chrome's lifecycle, the one CDP connection over
  Chrome's pipe, attached to the music.apple.com page, and every command as a coroutine; a
  command issued while Chrome starts waits for it. It says through `lost(reason)` when it goes
  down on its own. The backend package is the standard library and asyncio only.
- **Player** (`player.py`): what is playing, as GObject properties fed by MusicKit's events,
  and the playback commands. The player bar, the Now Playing sheet and MPRIS all follow it.
- **MPRIS** (`mpris.py`): the app on the session bus as `org.mpris.MediaPlayer2.<app id>`, for
  GNOME Shell's media controls and the media keys. Chrome's own MPRIS player is switched off.
- **Artwork** (`widgets/artwork.py`): one loader that decodes covers in threads, at the size
  they are drawn, into a small LRU of textures.

## Flows

- **Startup.** `do_startup` starts reading library.json in a thread, then makes the engine,
  the player, the sync and MPRIS; `do_activate` builds the window, awaits the library (then
  trims the caches in a thread) and, when the account is signed in and `engine-autostart` is
  on, starts the engine, whose coming up starts a sync when one is due.
- **Playing.** A tile, row or button calls `window.play_request()`, which calls
  `app.player.play()`. The player starts the engine if it is down and the account is signed in,
  then asks the engine, which asks MusicKit through the bridge. Nothing comes back from the
  command itself: MusicKit's events arrive through the binding, the engine re-emits them, the
  player updates its properties, and the bar, the sheet and MPRIS follow.
- **Syncing.** Refresh (Ctrl+R) and a finished sign-in start `sync_library()`; so does the
  scheduler (`LibrarySync.schedule()`) whenever a sync is due while the engine is up and signed
  in: the last one older than the chosen interval, or no current library on disk (missing,
  unreadable, or of an older version), looked at by a timer, when the engine comes up and when
  the library has been read. A timer never starts Chrome. Progress shows in a banner; the end
  is a toast with the counts, a failure a toast with Retry. Pages keep their scroll positions,
  because the reload keeps every object that is still in the library.
- **Losing the engine.** Chrome crashing or being killed, the page crashing or closing, and a
  page that no longer answers after a call timed out (the engine probes it) all end the
  connection: the engine emits `lost(reason)` and goes down, the app offers to restart it in a
  toast, and the next play starts a fresh Chrome. When the page loads a new document, the client injects the bridge again and the
  engine passes a `bridgeReset` event on; if MusicKit does not come back after four tries, the
  connection is given up the same way.
- **Signing in and out.** Sign-in restarts Chrome visible on music.apple.com, waits until
  MusicKit is authorized, records it, reads the account name if it can, restarts Chrome
  headless (if `engine-headless` is on) and syncs. Sign-out asks first, then revokes the Apple
  session through MusicKit (starting a stopped engine for it, 20 seconds at most), stops every
  job that writes the cache (the cache's generation is bumped, the sync cancelled and waited
  for, its thread included), stops Chrome, deletes the profile and what the cache holds,
  forgets the account's settings and pages, and empties the library. Clear Cache stops the
  cache's writers the same way before it deletes anything.
- **Quitting.** Every way out (Ctrl+Q, closing the window, MPRIS Quit, SIGINT or SIGTERM)
  activates `app.quit`: the windows save their state, the sync is cancelled, and Chrome is
  stopped (given 6 seconds, then killed) before the app exits. If the app dies any other way,
  the kernel sends Chrome SIGTERM (`setpriv --pdeathsig`), and the next start ends a Chrome that
  still holds the profile. With background playback on (it is off by default), closing the
  window while music plays hides it instead; the app then quits once playback has stayed stopped
  for 10 seconds.
- **Demo mode.** `--demo` reads an invented library from build/demo and has no engine at all,
  so every page and screenshot works without Chrome or an account.

## Data

| What | Where |
|---|---|
| library.json, artwork, lyrics, the engine's kept answers | `$XDG_CACHE_HOME/apple-music/` (`apple-music-devel/` for the development build) |
| Chrome's profile, which holds the sign-in | `$XDG_DATA_HOME/apple-music/chrome/` (`chrome-devel/` for the development build) |
| Settings | GSettings, `io.github.jackicus.AppleMusic` |

Nothing records which Chrome is the app's: Chrome's own lock in the profile (`SingletonLock`)
names the process that holds it.

The development build (`-Dprofile=development`) has its own app ID, Chrome profile and cache,
so it runs beside a release build. It shares the release build's settings, the signed-in state
included.

## Further reading

- `src/backend/README.md`: the bridge's calls, MusicKit's events, the library.json shapes.
- `docs/decisions.md`: why things are the way they are.
- `docs/notes.md`: measurements and platform findings.
