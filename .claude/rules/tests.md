---
paths:
  - "tests/**"
---

# Tests

- Stdlib `unittest`, one module per source module (`tests/test_<module>.py`), each ending with
  the `if __name__ == '__main__': unittest.main()` guard. Run all with
  `python3 -m unittest discover -s tests` from the repo root, or one with
  `python3 -m unittest tests.test_player`; `meson test -C build --suite unit` runs the same.
- Every test module imports `tests` before `applemusic` (`from tests import ROOT`): discovery
  imports test modules as top-level modules, and importing `tests` is what registers `src/` as
  the `applemusic` package and puts GSettings on the memory backend with this tree's schema, so
  no test touches the desktop's settings.
- Put logic in non-widget classes and pure functions and test it with stand-ins (as
  test_mpris.py, test_player.py and test_actions.py do). A test that must build widgets uses
  `tests/gtk.py`: `@requires_gtk` on the class or method (it skips without a display or a
  compiled gresource from build/src or build/install), template modules imported inside the
  test, `pump()`/`wait_for()` for the main loop (`iterate()` in a test that pumps it itself:
  it silences PyGObject's warning about asyncio's deprecated policy, which the unittest runner
  would print), and under a second per test. They must pass on CI's Xvfb with the cairo renderer and no accessibility bus. A page test subclasses
  `tests/page_harness.py`'s `PageTestCase`: a presented stand-in window (the seams, recorded),
  a stand-in application (`pages.app()`) and an engine that answers each request from
  `engine.answers`.
- Tests never start Chrome, touch the real profile or cache, or reach the network (a server
  the test runs itself on 127.0.0.1, as test_normalize's artwork fetch does, is fine): point
  `APPLE_MUSIC_CACHE` (and `APPLE_MUSIC_PROFILE`) at a temporary directory. test_engine.py runs
  on gi.events' `GLibEventLoop` (`loop_factory`), as the app does, with `chrome.find_chrome`
  patched to a script that runs `tests/fake_chrome_relay.py` through the real `Engine._spawn`:
  it relays the DevTools pipe to test_client.py's `FakeBrowser` over a Unix socket and lives and
  dies as Chrome would (`FAKE_CHROME_MODE` `stubborn` or `exit:N`). test_client.py drives
  `PipeFakeChrome` (two pipes) and `FakeChrome` (a WebSocket, the attach); test_am_cli.py runs
  am.py's commands against the engine fixture. No test looks for a real Chrome, so CI has none.
  test_bridge.py runs src/backend/bridge.js under gjs against tests/bridge_harness.js's fake
  page, all its scenarios in one gjs process; it is skipped without gjs, which CI installs.
- Fixtures (`tests/fixtures/`) and anything a test writes are invented: no real titles, names
  or IDs.
- Some tests guard project rules rather than behaviour: test_build_lists (install, blueprint,
  gresource and POTFILES lists), test_shortcuts (the dialog lists every key), test_backend (no gi
  in the backend), test_i18n (gettext bound for Python and GtkBuilder), test_page_lifetime
  (droppable widgets are freed), test_spdx (every source file's licence header). When one
  fails, fix the code or the list, not the test.
- Keep the suite fast, since check.sh runs it on every change: patch clocks and timeouts
  rather than sleeping.
