---
paths:
  - "src/engine.py"
  - "src/backend/**"
  - "src/dialogs/signin.py"
  - "scripts/am.py"
  - "tests/test_engine.py"
  - "tests/test_client.py"
  - "tests/test_chrome.py"
  - "tests/test_cdp.py"
  - "tests/test_backend.py"
  - "tests/test_am_cli.py"
  - "tests/fake_chrome_relay.py"
  - "tests/test_bridge.py"
  - "tests/bridge_harness.js"
---

# Engine and backend

- Layers: `src/backend/` is the standard library and asyncio only, never gi
  (tests/test_backend.py checks). `src/engine.py` is the app's `Engine` GObject on top: Chrome as
  a `Gio.Subprocess`, the one `CDPClient`, the commands as coroutines. engine.py's docstring lists
  the commands; `src/backend/README.md` has the bridge's calls, the events and the library.json
  shapes. Keep the three in step when you add a command.
- The backend is a fork this app owns, in the app's style like the rest of the code.
  `normalize.py` turns Apple's answers into the library's shapes; `api.py` is what the app
  knows of the API apart from any connection (library ids, endpoints, resource types, Apple's
  `{errors}` answers as an EngineError, through `api_error()` only); `cdp.py` is only the
  WebSocket codec, for the developer attach.
- API reads (`Engine._api`) are tried again, after a growing pause, only when they may pass
  another time (a 5xx, a 429, an `{errors}` answer without a status, a rejected promise); a
  4xx or a timeout raises at once (`api.is_final`), with Apple's HTTP status as
  `EngineError.status`, so a caller can take a 404 to mean "none". A read a person waits on
  (an item, a rating, a link) has `READ_TIMEOUT`, 15 s. The account's commands start with
  `_require_signed_in(what)`; the storefront they use is the one the start's (or sign-in's,
  or the last `status()`'s) status read gave, read again only after the authorization
  changes. `item()` follows Apple's `next` links, so an artist has all its albums and a
  playlist all its tracks. `api_pages(unique=True)` keeps each id once (offsets shift when
  the library changes during a read), for listings of resources, never a playlist's tracks;
  tests/test_sync_app.py's FakeEngine borrows `Engine.api_pages`, so it may use nothing of
  the Engine but `api()`.
- Only `EngineError(code, message)` leaves the backend and the Engine. A new failure kind gets a
  code in `backend/errors.py` and a sentence in `Application.report()` (`no-browser`, no Chrome
  to find or spawn, still has the generic one). `start()` raises nothing else: a Chrome that
  exits at once is `engine-down` with its exit status; anything unexpected is logged and raised
  as `engine-down`.
- Transport: Chrome runs with `--remote-debugging-pipe`, CDP as NUL-terminated JSON on its
  descriptors 3 (it reads) and 4 (it writes), handed over with
  `Gio.SubprocessLauncher.take_fd()`; `launcher.close()` after the spawn drops this process's
  copies, or Chrome's exit never reads as the pipe's end. No port listens. The client talks to
  the browser endpoint and attaches the music.apple.com page as a session
  (`Target.attachToTarget`, `flatten`), so the pipe and the attach's WebSocket behave alike.
- `APPLE_MUSIC_DEBUG_PORT` (developers only) also opens `--remote-debugging-port` on 127.0.0.1,
  with a warning at every start: while it is set, any local program can drive the signed-in
  session. Nothing sets it by default, in a script, a launcher or a package; only a person, for
  one live check (`scripts/am.py --attach`).
- States: `down`, `starting`, `up`, `signing-in` (plus `authorized` and `headless`). Every
  command begins `await self._ready()`: a start in progress is waited for (its failure is the
  command's), `down` is `engine-down`. Commands never start Chrome; `Player.ensure_engine()`
  starts a `down` engine and returns at once in any other state, so a play during a start
  waits in `_ready()`. A `start()` while one is under way joins it (no mode, or the same mode):
  one Chrome, one outcome; cancelling the caller that began it cancels the start.
- `start(visible=None)`: without a mode it keeps an engine running in either mode and starts a
  stopped one headless unless `engine-headless` is off; sign-in is the one caller that asks for
  a window. A changed browser command applies at the next start. Nothing is reclaimed and no
  state file is kept: a start first ends a Chrome this process did not start that holds the
  profile (am.py's, or one a crash left).
- `stop(grace=None)`: `Browser.close` over the pipe (up to 2 s), SIGTERM, `grace` (5 s),
  SIGKILL, the process kept until it is gone, so `kill()` (the synchronous last resort, after
  quit's 6 s bound) still reaches it; `kill()` lets go of the client first, as a stop does.
  However the app ends, Chrome ends too: `with_pdeathsig()` runs it through
  `setpriv --pdeathsig TERM --` (util-linux; it execs, so the pid and command line stay
  Chrome's; without it, one warning), and `do_shutdown` kills one a quit left running.
- The profile's owner is Chrome's own record: `<profile>/SingletonLock`, a symlink to
  `<hostname>-<pid>`. `chrome.profile_owner()` believes it only when `/proc/<pid>/cmdline`
  names exactly that profile: `cmdline_names_profile()` takes `--user-data-dir=<profile>` as a
  whole argument and rejects `--type=` helpers (docs/notes.md has why).
- Losses: the connection goes when the pipe closes, the page crashes (`Inspector.targetCrashed`,
  `Target.targetCrashed`), detaches or closes. A call that times out while up has the engine
  probe the page (`0`, 5 s, one at a time) and close a silent one. After a navigation the
  client puts the bridge back (four tries, the last after reloading the page) and emits
  `am:bridgeReset`, or gives the connection up. `lost(reason)` (`client.lost_reason`) is
  emitted before the engine goes down, only when nobody asked: never for stop, restart,
  sign-in's restarts, quitting or `kill()`. Nothing in the app answers it yet. `bridgeReset`
  is re-emitted on `event`, and the engine reads `status()` again.
- Under `--demo` the Engine has `demo=True`: start and stop do nothing and every command raises
  `engine-down`.
- Paths (`backend/config.py`): `Application.__init__` calls `config.set_build_profile()`. The
  release build uses `$XDG_DATA_HOME/apple-music/chrome` and `$XDG_CACHE_HOME/apple-music`; the
  .Devel build `chrome-devel` and `apple-music-devel`. `APPLE_MUSIC_PROFILE` and
  `APPLE_MUSIC_CACHE` override them, read on every call. The builds share every setting,
  `signed-in`, `account-name` and `last-sync` included.
- The cache: src/cache.py knows what it holds (`CACHE_ENTRIES`), measures and clears it (the
  entries and `*.tmp` leftovers, never an unrelated file) and reads the kept answers.
  `normalize.prune_caches()` trims what only grows (remote-art/ to 32 MB, the 2,000 lyrics
  played last, kept answers past their day, crash leftovers); nothing in the app calls it
  yet. The day-long answers (the landing, a category, New, Made for You) all go through
  `Engine._kept_answer()`: the file while it is under a day old, else Apple, else (the engine
  down or signed out, Apple failing, but not on a refresh) the older file marked
  `stale: True` (`cache.read_kept(allow_stale=True)`); every answer carries its `cached`
  stamp. Lyrics are kept answers too, stamped, fetched again after 30 days. `item()`'s artwork
  goes to remote-art/ (`place_in_remote_art`): art/ and thumb/ are the library's, pruned
  against library.json after every sync.
- Cache writes: every file in the cache goes through `backend/store.py` (`atomic_write`, or
  `atomic_create` for a writer that wants a path, such as the thumbnail scaler): a dot-named
  temporary file beside the target, fsync'd (not artwork, a copy that counts as missing when
  empty), renamed over it; a failure removes the temporary file and raises. Each write names
  its `root`, the cache directory: a path outside it raises ValueError (cache_artwork answers
  None), so a path from an answer cannot land elsewhere. Directories are made 0700, files
  0600. Never `open(path, 'w')` a cache file. The pruners leave a dot temp alone until it is an
  hour old.
- The cache's generation: a job that writes the cache (a sync, `item()`'s artwork, a kept
  answer, lyrics, a cover the pages fetch) takes `store.cache_generation()` on the main thread
  when it starts and passes `generation=` to every write. Clearing the cache or signing out
  calls `store.bump_cache_generation()` first; from then on that job's writes land nowhere and
  make no directory, and a job that must stop (a sync's artwork, save_library) raises
  `store.CacheGone`. That is a `store.Cancelled` (`normalize.Cancelled`), which
  `download_art` also raises when its `cancelled()` says so: a cancelled download never
  returns counts that pass for a finished one, and the sync writes nothing after it.
- Chrome's argv (`chrome.chrome_args()`): `--disable-features=HardwareMediaKeyHandling` keeps
  Chrome's own MPRIS player off the bus (the app owns MPRIS); `--headless=new`; the visible
  window is an `--app=` window. In a Flatpak sandbox `find_chrome()` asks the host (blocking:
  call it in a thread), and the argv starts
  `flatpak-spawn --host --watch-bus --forward-fd=3 --forward-fd=4` (untested), without setpriv
  (`--watch-bus` ends Chrome); `profile_owner()` answers None there.
- Logs: Chrome's argv is logged through `chrome.describe_argv()`, which leaves the profile's
  path out; never log tokens, API URLs with their queries, or the account name.
- CDP: `Runtime.addBinding('__amEvent')` is per CDP session, so every connection registers it
  and every registered connection receives each call. `Runtime.enable` replays
  `executionContextCreated` for existing contexts; the client re-injects the bridge only after
  `ensure_bridge()` has been called, and only into the main frame's default context (Apple's
  sign-in iframe has its own). Bridge events arrive as `am:<name>`; the Engine re-emits them as
  `event(name, data)` without the prefix. A bad payload or a raising handler is logged, never
  the end of the session.
- bridge.js is tested by tests/test_bridge.py: gjs runs it unchanged against
  tests/bridge_harness.js, a fake page (MusicKit and an instance that records its calls, a
  `document` and a small `DOMParser`). A change to the bridge gets a scenario there; what only
  Apple's MusicKit can show (what it accepts, what it answers) is a live check.
- bridge.js writes (love, add to library, add to a playlist) through
  `mk.api.client.createRequest(...).send()`: `music()` cannot read Apple's empty 202/204 answers
  and would pass a 4xx off as success.
- MusicKit's `PlaybackStates` names (`none loading playing paused stopped ended seeking waiting
  stalled completed`) are what `playbackStateDidChange` carries; a play walks playing, waiting,
  loading, playing. Signed out, MusicKit plays 30-second catalog previews: enough to exercise
  playback and events without an account.
- Processes: `wait_async()` is awaitable, and an `asyncio.wait_for` timeout on it is clean
  (then `force_exit()`), but a cancelled one raises `GLib.Error`, not `CancelledError`: race it
  through `engine._process_exit()`. Chrome's helpers write to the profile for a moment after the
  browser exits, so deleting the profile retries. `scripts/am.py` runs this same Engine on
  gi.events' loop (`loop_factory=GLibEventLoop`); its modes are in scripts.md.
- `unauthorize()` (the bridge's `signout()`, MusicKit's `unauthorize()`) revokes the Apple
  session at Apple's end; sign-out calls it before wiping anything. Best effort: it answers
  False and logs a warning when it cannot (engine down, page slow, MusicKit refusing), and
  never raises. MusicKit's real answer is unverified here: never try it on the real account
  (it signs the profile out).
- `account_name()` is best effort: the bridge's `accountName()` reads only selectors that
  name the user (`.account-menu .user__name` first) and answers null while a sign-in control
  is on the page, whatever its language; else ''. Apple's page is Svelte with hashed class
  names; don't guess new selectors without checking.
- Tests never start a real Chrome, or look one up: see tests.md. Anything against the real
  engine follows the `live-engine-check` skill.
