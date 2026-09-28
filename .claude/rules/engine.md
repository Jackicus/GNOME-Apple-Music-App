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
---

# Engine and backend

- Layers: `src/backend/` is the standard library and asyncio only, never gi
  (tests/test_backend.py checks). `src/engine.py` is the app's `Engine` GObject on top: Chrome as
  a `Gio.Subprocess`, the one `CDPClient`, the commands as coroutines. engine.py's docstring lists
  the commands; `src/backend/README.md` has the bridge's calls, the events and the library.json
  shapes. Keep the three in step when you add a command.
- The backend is a fork this app owns. `cdp.py` and `sync.py` (and their tests, and
  `scripts/demo_library.py`) still use the extension's style, exempted in `pyproject.toml`; new
  backend code follows the app's style.
- Only `EngineError(code, message)` leaves the backend and the Engine. A new failure kind gets a
  code in `backend/errors.py` and a sentence in `Application.report()`.
- States: `down`, `starting`, `up`, `signing-in` (plus `authorized` and `headless`). Every
  command raises `engine-down` unless the state is `up` or `signing-in`, and that includes
  `starting`; `Player.ensure_engine()` starts a `down` engine but returns at once in any other
  state. So a command issued during a start fails rather than waits: don't build on waiting.
- `start(visible=None)`: without a mode it keeps an engine running in either mode and starts a
  stopped one headless unless `engine-headless` is off. Callers that only need it up pass no
  mode; sign-in is the one caller that asks for a visible window. It reclaims the Chrome that
  engine.json names when that pid is alive and its mode and port match (otherwise it stops it).
  `stop()` closes the connection, then SIGTERM, 5 s, SIGKILL; `kill()` is the synchronous last
  resort. A changed browser command or port applies at the next start.
- Under `--demo` the Engine has `demo=True`: start and stop do nothing and every command raises
  `engine-down`.
- Profiles and ports (`engine_paths()`, `backend/config.py`): the release build uses
  `$XDG_DATA_HOME/apple-music/chrome` and the `engine-port` setting (9228); the .Devel build
  `chrome-devel` and the port after it (9229). `APPLE_MUSIC_PROFILE`, `APPLE_MUSIC_PORT` and
  `APPLE_MUSIC_CACHE` override them and are read on every call. Both builds share the cache and
  every setting, `signed-in` included.
- engine.json: the default profile's is `$XDG_RUNTIME_DIR/apple-music/engine.json`, any other
  profile keeps its own inside the profile. `EngineState.alive` checks the pid and that its
  command line contains `--user-data-dir=<profile>`, a substring match: `…/chrome` also matches
  `…/chrome-devel`.
- Chrome's argv (`chrome.chrome_args()`): `--disable-features=HardwareMediaKeyHandling` keeps
  Chrome's own MPRIS player off the bus (the app owns MPRIS); `--headless=new`; the visible
  window is an `--app=` window. In a Flatpak sandbox `find_chrome()` asks the host (blocking: call
  it in a thread) and the argv is prefixed with `flatpak-spawn --host --watch-bus`.
- CDP: `Runtime.addBinding('__amEvent')` is per CDP session, so every connection registers it
  and every registered connection receives each call. `Runtime.enable` replays
  `executionContextCreated` for existing contexts; the client re-injects the bridge only after
  `ensure_bridge()` has been called, and only into the main frame's default context (Apple's
  sign-in iframe has its own). Bridge events arrive as `am:<name>`; the Engine re-emits them as
  `event(name, data)` without the prefix.
- bridge.js writes (love, add to library, add to a playlist) through
  `mk.api.client.createRequest(...).send()`: `music()` cannot read Apple's empty 202/204 answers
  and would pass a 4xx off as success.
- MusicKit's `PlaybackStates` names (`none loading playing paused stopped ended seeking waiting
  stalled completed`) are what `playbackStateDidChange` carries; a play walks playing, waiting,
  loading, playing. Signed out, MusicKit plays 30-second catalog previews: enough to exercise
  playback and events without an account.
- Processes: an asyncio subprocess transport kills its child when it is garbage-collected, so
  `scripts/am.py` starts Chrome with `subprocess.Popen(start_new_session=True)`. The app uses
  `Gio.Subprocess`: `wait_async()` is awaitable, and an `asyncio.wait_for` timeout on it is clean
  (then `force_exit()`). Chrome's helpers write to the profile for a moment after the browser
  exits, so deleting the profile retries.
- `account_name()` is best effort: known selectors under the page's account footer, else ''.
  Apple's page is Svelte with hashed class names; don't guess new selectors without checking.
- Tests never start Chrome: tests/test_engine.py runs a sleeping Python process as "Chrome" and
  test_client's fake page. Anything against the real engine follows the `live-engine-check`
  skill.
