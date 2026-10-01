# Architecture

How Music Sleeve is put together, for someone about to change it. Each module's
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
threads. Work that would block runs in threads (`asyncio.to_thread`, the library's own reader
thread for library.json, `remote.py`'s pool for artwork downloads), and only the main thread
touches widgets.

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
  `play_request()`. The sidebar itself (its items following the library, the selection, the
  folders, the drops) is `sidebar_view.py`'s controller, with its decisions as `sidebar.py`'s
  functions; which playback action a key runs is `keyboard.py`'s decision. The item actions
  and context menus are `actions.py` and `widgets/context_menu.py`; where their Go to Album
  and Go to Artist lead is `related.py`'s: an album the library's, else the catalog's, which
  the engine looks up; an artist Apple Music's while the engine is up, the library's
  otherwise.
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
  down on its own. Before spawning Chrome on a profile whose cookies are encrypted with a key
  in the desktop's keyring, it checks that the keyring answers on the session bus Chrome will
  use (the app's, or `APPLE_MUSIC_HOST_SESSION_BUS`) and refuses otherwise, since a Chrome
  without its key deletes those cookies, the sign-in among them. The backend package is the
  standard library and asyncio only.
- **Player** (`player.py`): what is playing, as GObject properties fed by MusicKit's events,
  and the playback commands. The player bar, the Now Playing sheet and MPRIS all follow it.
- **MPRIS** (`mpris.py`): the app on the session bus as `org.mpris.MediaPlayer2.<app id>`, for
  GNOME Shell's media controls and the media keys. Chrome's own MPRIS player is switched off.
- **Search provider** (`search_provider.py`): `org.gnome.Shell.SearchProvider2` under the
  app's own D-Bus object, exported before its name is owned, so the Activities overview
  searches the library: albums, artists, playlists and (once the Songs store is built)
  songs, matched as the Search page's Your Library mode matches them and ranked, a handful
  of each kind, with their thumbnails. It answers from the library in memory only, never
  the engine. Choosing a result opens its page (a song plays); the overview's own search
  button opens the Search page with the terms. A D-Bus service file starts the app for a
  search in service mode: no window and no Chrome until a result is chosen, and the app
  quits half a minute after its last call.
- **Discord presence** (`discord.py`): while `discord-presence` is on, the track the player
  holds, sent as a rich presence activity over the Discord desktop app's local socket. It
  follows the player's notifications like MPRIS, connects only when there is something to
  show and Discord is there, waits for Discord's READY before sending, and sends one frame per
  change once a track change has settled. Discord being absent is not an error. At shutdown it
  clears the activity; Discord also drops it by itself when the app's socket closes.
- **Artwork** (`widgets/artwork.py`): one loader that decodes covers in threads, at the size
  they are drawn, into a small LRU of textures. The files it decodes that the sync does not
  bring (covers, the item playing, the engine's search and browse answers) are downloaded by
  `remote.py`, in threads of their own.

## Flows

- **Startup.** `do_dbus_register` exports the search provider; `do_startup` starts reading
  library.json in a thread, then makes the engine, the player, the sync, MPRIS and the
  Discord presence; `do_activate` builds the window, awaits the library (then trims the
  caches in a thread) and, when the account is signed in and `engine-autostart` is on,
  starts the engine, whose coming up starts a sync when one is due. A search from the
  overview starts the app over the bus instead (the service file's `Exec` has
  `--gapplication-service`), which runs `do_startup` alone; the provider's first call
  finishes the library's load (`Application.load_library()`, the same once-only task
  `do_activate` uses), and nothing shows or starts Chrome until a result is chosen.
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
  is a toast with the counts, a failure a toast with Retry (or, for a missing or stopped engine
  or a lost sign-in, that error's own toast and button). Pages keep their scroll positions,
  because the reload keeps every object that is still in the library.
- **Losing the engine.** Chrome crashing or being killed, the page crashing or closing, and a
  page that no longer answers after a call timed out (the engine probes it) all end the
  connection: the engine emits `lost(reason)` and goes down, the app offers to restart it in a
  toast, and the next play starts a fresh Chrome. When the page loads a new document, the client injects the bridge again and the
  engine passes a `bridgeReset` event on; if MusicKit does not come back after four tries, the
  connection is given up the same way.
- **Signing in and out.** Sign-in (`account.sign_in()`, shown by its dialog) stops a running
  sync, restarts Chrome visible on music.apple.com and waits until MusicKit is authorized;
  from then on it is committed (the dialog closes, closing Chrome's window changes nothing),
  and it reads the account name if it can, restarts Chrome headless (if `engine-headless` is
  on) and syncs. Sign-out asks first, then stops the sync (waited for, its thread included),
  revokes the Apple session through MusicKit (starting a stopped engine for it, 20 seconds at
  most), stops every other job that writes the cache (the cache's generation is bumped),
  stops Chrome and keeps it from starting, deletes the profile and what the cache holds,
  forgets the account's settings and pages, and empties the library; quitting meanwhile cuts
  only the revocation short. Clear Cache stops the cache's writers the same way before it
  deletes anything.
- **Quitting.** Every way out (Ctrl+Q, closing the window, MPRIS Quit, SIGINT or SIGTERM)
  activates `app.quit`: the windows save their state, close their dialogs and hide, a start of
  the engine the app asked for is cancelled, and the sync (its thread included) and Chrome are
  stopped, 6 seconds at most for both (a sign-out under way gets up to 10 more to finish its
  wipe), Chrome then killed if it still runs, before the app exits. If the app dies any other
  way, the kernel sends Chrome SIGTERM (`setpriv --pdeathsig`), and the next start ends a Chrome
  that still holds the profile. With background playback on (it is off by default), closing the
  window while music plays hides it instead (`background.py`): what the app has to say
  meanwhile comes as a notification, and the app quits once playback has stayed stopped for 10
  seconds.
- **Demo mode.** `--demo` reads an invented library from build/demo (a release build lists no
  `--demo` and needs `APPLE_MUSIC_CACHE` for it), and its engine is inert (starting it does
  nothing, every command answers `engine-down`), so every page and screenshot works without
  Chrome or an account. Its settings are its own (settings.ini beside
  the library), it takes no MPRIS name and exports no search provider, so it runs beside
  the real app.

## Data

| What | Where |
|---|---|
| library.json, artwork, lyrics, the engine's kept answers | `$XDG_CACHE_HOME/apple-music/` (`apple-music-devel/` for the development build) |
| Chrome's profile, which holds the sign-in | `$XDG_DATA_HOME/apple-music/chrome/` (`chrome-devel/` for the development build) |
| Settings | GSettings, `io.github.jackicus.MusicSleeve` |

Nothing records which Chrome is the app's: Chrome's own lock in the profile (`SingletonLock`)
names the process that holds it.

The development build (`-Dprofile=development`) has its own app ID, Chrome profile and cache,
so it runs beside a release build. It shares the release build's settings, but for the
account's own: whether it is signed in, the account's name, the last sync, the last page and
the open folders (`-devel` keys).

## Further reading

- `src/backend/README.md`: the bridge's calls, MusicKit's events, the library.json shapes.
- `docs/decisions.md`: why things are the way they are.
- `docs/notes.md`: measurements and platform findings.
