# Decisions

The design decisions this app rests on, with the reasons and what was given up. When one
changes, change it here, with the date. The plan they came from is in
`docs/history/build-plan.md`.

## Product

- **Distribution: native first** (2026-09-27). Releases are `meson install` and an AUR package
  (`build-aux/aur/PKGBUILD`). The Flatpak manifest is for development only: from the sandbox,
  the app runs the host's Chrome through `flatpak-spawn --host`. Flathub is out of scope: it
  would refuse an app that depends on the host's Chrome. (It was also out of scope while the
  app carried Apple's name; that half no longer applies, but the Chrome dependency still does.)
- **A profile of its own** (2026-09-27). The app's Chrome uses its own profile
  (`$XDG_DATA_HOME/apple-music/chrome`, the development build `chrome-devel`), so it never
  fights another Chrome, such as the GNOME Shell extension's, over a profile lock. The cost is
  one sign-in per profile, and two Chromes when both run.
- **The development build's own paths** (2026-09-28). The .Devel build has its own cache
  (`$XDG_CACHE_HOME/apple-music-devel`) beside its own Chrome profile, chosen in
  `backend/config.py` from the build profile, so a development sync, Clear Cache or Sign Out
  never rewrites or deletes the release build's library. The settings are shared but for the
  account's: `signed-in`, `account-name`, `last-sync`, `last-page` and `expanded-folders` have
  `-devel` twins for the .Devel build (`Application.account_key()`), whose profile holds a
  sign-in of its own; a development build starts signed out, with an empty library.
- **The player at the bottom** (2026-09-27). A full-width bar at the bottom of the window, as
  in GNOME Music, with Now Playing as an `AdwBottomSheet` that slides up over the content and
  holds Lyrics and Up Next. The web player puts its player at the top; the bottom keeps the
  header bar for titles and back buttons, which is what GNOME users expect. Shown only while
  something plays (2026-10-01), as GNOME Music's is: with nothing playing the window ends at
  the content rather than carrying a dead "Not Playing" bar, and the bar slides in with the
  first item (`src/window.py` reveals it from the Player's track, after the Player's grace
  between queues, so a change of queue shows no blink). Ctrl+Shift+N still opens Now
  Playing; Ctrl+3 with no bar leaves the focus and says "Not Playing" to a screen reader.
- **Playlist folders in the sidebar** (2026-09-27). Folders are sidebar items with a folder icon
  and a disclosure arrow; activating one shows or hides its playlists and opens the folder's
  page (in the narrow layout's page mode it only opens the page). `AdwSidebar` cannot indent,
  so nesting shows through order, the arrow and a nested item's subtitle naming its folder.
- **Keys as GNOME has them** (2026-09-28). Space presses a focused button, switch, check box or
  list row, as everywhere in GTK; it plays or pauses only on a tile, a list item, the seek
  slider or nothing. Now Playing is Ctrl+Shift+N, leaving Ctrl+N to the HIG's "New". Bare keys
  and editing chords are never application accelerators (`src/keyboard.py` decides them).
- **Music videos play as audio** (2026-09-28). The engine is headless, so a video tile plays
  the video's sound, as the context menu's Play does, rather than refusing or opening a
  browser. Easy to change to a toast with Open in Browser.
- **MPRIS Stop pauses** (2026-09-28). Stop pauses and seeks to the start, keeping the item, as
  the spec allows; the Shell then keeps showing the player, where ending the session would
  drop it.
- **Favourite is a heart** (2026-09-28), everywhere: the sidebar's Favourite Songs and its
  page as well as the player bar and Now Playing, with the bundled heart icons.
- **A refresh on a schedule** (2026-09-28). The library is synced when the last sync is older
  than the chosen interval, or when library.json is missing, unreadable or of an older
  version, checked by a timer, when the engine comes up and when the library has been read;
  only with the engine up and signed in, so a timer never starts Chrome.
- **Sign-out revokes the session** (2026-09-28). Signing out calls MusicKit's `unauthorize()`
  before anything is deleted, so the token stops working at Apple's end too; when the engine
  is down, it starts Chrome headless for up to 20 seconds to do so, then wipes whatever
  happens. The alternative, skipping the revocation when the engine is down, is quicker but
  leaves the token valid until Apple expires it. Every job that writes the cache is stopped
  first (the cache's generation), so nothing is written after the wipe.
- **Discord presence, off by default** (2026-09-30). A switch in Preferences sends the playing
  track to the Discord desktop app as rich presence, over its local IPC socket, with no
  library: the protocol is a handshake and one command, and a dependency would be the app's
  only one outside GNOME's stack. Off until the user turns it on, since it publishes what they
  listen to. The application id is the project's own Discord application, public by nature
  (rich presence needs no token). The activity names itself "Apple Music", so the card reads
  "Listening to Apple Music" as it reads "Listening to Spotify": it names the service being
  listened to, not this app, which keeps to the rule on Apple's marks. The artist is the
  status line under the user's name, and the album is the cover's caption, which Discord
  prints as the card's third line. The cover is Apple's own public URL, which Discord fetches;
  nothing is uploaded anywhere.
- **A click plays a song** (2026-10-01). A track in an album's or a playlist's list, the Songs
  table and Search's songs plays on a single click (`single-click-activate`), as GNOME Music
  plays one: the first click of a double click selected nothing anyone used, and a row that
  wants a second click is a file manager's. Enter plays the focused row as before; the
  context menu, a drag to a sidebar playlist and a row's artist and album links
  (`src/widgets/track_links.py`) claim their presses before the row sees them, so they are
  unchanged. The Songs table keeps no selection any more (a `Gtk.NoSelection`, as the lists
  have): GTK selects the hovered row of a single-click list, which would have followed the
  pointer around. The track playing is marked in every row that shows it, as Up Next marks
  its entry (#241).
- **One look for loading, status pages, links and grids** (2026-10-01). A page that waits
  shows a bare 32 px `AdwSpinner`, the album and artist pages' sections under their hero too,
  in place of three looks (that spinner, a hand-made "Loading…", a status page with a spinner
  paintable). A state with words is an `AdwStatusPage`, or a box shaped as a compact one
  where a list row cannot hold one (the album page's), with one `pill suggested-action`
  button and plain `pill` ones beside it. A link in text (an album's artist, a row's artist
  and album) is the text's own colour, underlined under the pointer, never a `link` button's
  blue. A grid's first column sits on the page's 24 px margin, the tiles at the start of
  cells that grow with the page, as Radio's flow box has them, where the grids' tiles were
  centred in theirs and drifted (28 px wide, 13 at 360); under 400sp the margins shrink to
  the 14 px that still fit two tiles, Radio's the same, the grid never centred (a centred
  scrollable has GTK measure its width for its height, which warns) (#243).

## Engine

- **The overview searches the library, through the bus** (2026-10-01). The app exports
  `org.gnome.Shell.SearchProvider2` (`src/search_provider.py`), with a D-Bus service file
  whose `Exec` has `--gapplication-service`, as GNOME's own apps do.
  The Shell starts the app for a search when it is not running; that start runs
  `do_startup` alone, and the engine's autostart lives in `do_activate`, so no window shows
  and no Chrome runs until a result is chosen, and the app quits half a minute after its
  last call. The provider answers from the library in memory only: never the engine, never
  the Songs store built for it (a second on a big library; songs show once a page has
  built it). The desktop file stays `DBusActivatable=false`, unlike GNOME's own apps: the
  search does not need it, and a bus launch has no fallback to `Exec` (GLib drops the
  error), so an install whose service directory the session bus cannot see would leave the
  app's icon doing nothing.
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
- **No Chrome without its keyring** (2026-09-29). Chrome keeps the key that encrypts its
  profile's cookies in the desktop's keyring, and a Chrome that cannot reach it deletes those
  cookies, the Apple Music sign-in among them, for good. So the engine refuses to start Chrome
  on a profile whose `Local State` records the keyring's key when no `org.freedesktop.secrets`
  answers, or comes up when asked, on the bus Chrome will use (`no-keyring`, a toast with
  Retry), and `scripts/headless.sh` hands Chrome the desktop's session bus
  (`APPLE_MUSIC_HOST_SESSION_BUS`) while the app stays on its private one. A refused start is
  a toast and a retry; a lost sign-in is a trip to Apple's sign-in page and a full sync. A
  profile that never reached the keyring starts as before, since there is nothing to lose.
- **State from events, not polling** (2026-09-27). What is playing comes from MusicKit's events,
  forwarded through a CDP binding (`Runtime.addBinding`), plus one read when the engine comes
  up. The only polling is while waiting for sign-in, and briefly after it for the account's
  name.
- **The backend is a fork** (2026-09-28). `src/backend/` began as a copy of the extension's
  backend, and this app has since rewritten large parts of it. It is maintained here, for this
  app, in the app's own style (its provenance is at the end of `src/backend/README.md`).

## Code

- **asyncio on the GLib main loop** (2026-09-27), through PyGObject's `GLibEventLoopPolicy`.
  CDP is requests and events, which coroutines model directly: no locks, no hops between
  threads, and UI code awaits the engine in place. Blocking work goes to `asyncio.to_thread`;
  Chrome is spawned with `Gio.Subprocess`. Python 3.14 deprecates the policy call PyGObject
  still needs; if PyGObject changes its entry point, only `main.use_glib_event_loop()` changes.
- **One navigation view** (2026-09-27). The content pane is one `AdwNavigationView`. A sidebar
  item replaces its stack with the destination's root page, made on first visit and kept (a
  playlist's or folder's page only while it is among the last few shown); tiles push detail
  pages; root pages show a large in-content title and show the header bar's only while that
  one is out of view.
- **The cache as the library** (2026-09-27). `$XDG_CACHE_HOME/apple-music/library.json` is the
  one library snapshot, parsed off the main thread from the start of the process and wrapped
  into GObjects a section at a time. Thumbnails (320 px) are fetched for every item at sync;
  covers (640 px) on demand. Decoded textures live in an LRU at the size they are drawn: 8 MB at
  a scale factor of 1, the same tiles' 32 MB at 2 (2026-09-29).
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
- **Errors as sentences** (2026-09-28). Everything that fails in the engine is an
  `EngineError` with a code; the user sees one plain-text toast per failure, whose sentence
  and button (`src/errors.py`) follow the code, and the detail goes to the log. A play MusicKit
  refused is worded from MusicKit's own code; sign-in and a failed sync have sentences of their
  own. A page that shows a state for the failure (Sign In, Start Engine, Try Again) does not
  toast it too.
- **The memory target** (2026-09-29). Anonymous memory, not RSS, which adds what the libraries
  and the GL driver map: under 250 MB after every page is browsed twice on the big invented
  library, and pages opened and closed leave at most 5 MB behind (`docs/notes.md`).
- **Comments describe the code** (2026-09-28), as it is and why: no phase numbers, review IDs,
  plans or history, which belong in commit messages.
- **A name of the app's own** (2026-09-30). The app is **Music Sleeve**, app ID
  `io.github.jackicus.MusicSleeve`. "Apple Music" as the display name was Apple's trademark
  used as a product name, which their guidelines for third parties do not allow. The service
  is still named where it has to be — the metainfo summary and description, the desktop file's
  `Keywords=` (so the app is still found by searching for "apple music"), and strings like
  "Sign in to Apple Music" — always less prominent than the app's own name. The full rule is in
  `.claude/rules/packaging.md` (#145).
- **An icon of the app's own** (2026-09-30). A record coming out of its sleeve, in GNOME's
  palette: the old icon was a red rounded square with a white double note, near enough to Apple
  Music's own logo to confuse users and to invite a complaint. Apple's guidelines for third
  parties allow no use of their logos or icons at all, and Flathub requires an icon distinct
  enough to carry the project's own identity. The symbolic icon is the record alone: at 16 px
  the sleeve behind it only muddies the silhouette. The development build's icon is the same
  artwork under a band of stripes, and the metainfo's brand colours follow it.
- **The accent is the user's** (2026-09-30). The app sets no accent of its own: libadwaita
  hands it whichever of GNOME's accent colours the user chose in Settings, and everything
  tinted (the Play button, the seek slider, the filled heart, links) follows. It had been
  pinned first to Apple Music's red, then to the new icon's purple; both were wrong for the
  same reason, that a GNOME app wears the user's colour, not its own. The metainfo's
  `<branding>` stays purple and keeps following the icon: that marks the app in a software
  centre, where it sits among other apps, and is not the user's choice to make (#195).

- **An item reaches MPRIS once** (2026-09-30). A new item's Metadata and Can*s wait for its
  artwork file, at most 200 ms (`ART_GRACE_MS`), rather than go out at once and again with
  `mpris:artUrl`: two changes made GNOME Shell's card blink through its no-cover icon at every
  track change (#182). The wait ends the moment the file is there, which is at once for the
  next song of one album (the same URL, nothing fetched) and a moment for a file on disk; a
  slower download publishes the item first and its artwork after, since a card that is right
  late is worse than one that fills in twice.
- **The keyboard walkthrough presses real keys** (2026-09-30). `scripts/a11y_check.py` sends
  them through the headless mutter's `org.gnome.Mutter.RemoteDesktop` interface, which
  `scripts/headless.sh` already puts on a private session bus, rather than dispatching each
  press through GTK's controllers: the window is then active, so the walkthrough covers what
  only an active window does — Tab from row to row and Down from one boxed sidebar section to
  the next — and typing is typed. The emulation stays behind `--emulate-keys`, and as the
  fallback on a session that has no such interface (a desktop one keeps it behind the
  remote-desktop portal, which asks the user first). It stays a tool to run here rather than a
  CI job: the container installs no mutter, and nothing in `check.sh` may need one (#169).
- **Artist pages are Apple's views** (2026-10-01). An artist's page is one catalog read with
  every view music.apple.com's views-based page asks for (`api.ARTIST_VIEWS`), kept for a day,
  laid out in the order and under the titles Apple gives: the release beside Top Songs (a grid
  three rows high), Essential Albums as large cards, the shelves, About (biography, From,
  Born or Formed, Genre) before Similar Artists. It replaces fetching every album with its
  tracks, which took one read an album. A library artist is the sync's own invention (no
  catalog id), so its catalog artist is found through one of its songs (the library album's,
  since the artist's copy of the tracks is dropped at load). The library's albums of the
  artist's show first, as In Your Library, beside the catalog's shelves: a library album
  carries no catalog id to tell it from the catalog's, so an album may be on both, which is
  the price of keeping what is in the library on the page (#236); Go to Artist opens the
  catalog artist's page while the engine is up. Videos about the artist open on music.apple.com: the engine is
  headless and has no picture to show. The demo engine answers with the artist pages the
  demo library invents (marked `demo`), and with none of Apple's (#226).

## Open

- Nothing. The questions that were open here are settled above; what is left before the
  first release is in `TODO.md` and the open issues.

