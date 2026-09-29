---
paths:
  - "scripts/**"
---

# Developer scripts

- `run.sh`, `check.sh` and `demo.sh` share one build directory, `build/`, set up with the
  development profile and the prefix `build/install`; run.sh and check.sh reconfigure it back
  if something set it up otherwise. Keep a release build in its own directory (`_build/`).
- The in-process scripts (screenshot.py, a11y_check.py, scroll_test.py, bench.py) run the
  installed build (run `scripts/demo.sh` or `meson install -C build` first) through
  `harness.make_app()`: the installed modules and translations, GSettings on the memory backend
  with engine-autostart off, the demo library (build/demo, or `APPLE_MUSIC_CACHE`; screenshot.py
  only with `--demo`), an app ID of their own, asyncio on the GLib loop, a fixed dark or light
  scheme, and windows made non-resizable so a tiling window manager keeps `--size`. Call
  `make_app()` before importing Gtk. A new in-process script starts through the harness too: the
  scripts' app runs as the release profile, and the harness is what keeps it from starting a real
  Chrome.
- `headless.sh COMMAND…` runs a command on a private, invisible display: a headless mutter with a
  virtual monitor (`HEADLESS_SIZE`, default 1920x1080) in its own D-Bus session, with `DISPLAY`
  unset. Run every GUI script through it (screenshot.py, a11y_check.py, scroll_test.py,
  bench.py, the widget tests) so no window opens on, or takes focus from, the user's desktop, and
  the app's MPRIS player never reaches the real session. Compare timings only with other
  headless runs.
- screenshot.py and a11y_check.py also use the stock GNOME look (Adwaita icons, Adwaita Sans 11)
  and no animations, so shots do not depend on the desktop's theme.
- `screenshot.py` without `--demo` reads the release build's real cache (`APPLE_MUSIC_CACHE`
  names the .Devel build's): never for a shot that is committed or shared. `--now-playing
  [lyrics|queue]` opens the sheet on an invented item; `--playing` puts the item on the Player
  with the sheet closed (the bar's playing state); both need `--demo`. `--signed-in [NAME]`,
  `--banner sign-in|expired` and `--sidebar` (the narrow layout's sidebar) show those states;
  `--help` lists the rest. It shoots once no artwork decode has been in flight for a few polls
  (5 s at most), so a new step that shows artwork needs no delay of its own for it.
- `a11y_check.py` cannot send real key presses here: `press()` runs the controllers a real event
  would reach. `--names` runs a private AT-SPI bus (a dbus-daemon and at-spi2-registryd) and
  stops it however the script exits.
- `am.py` drives the real engine: `status`, `eval`, `now-playing`, `events`. By default each
  command starts a Chrome of its own on the release build's profile (`--devel` for the .Devel
  build's, `APPLE_MUSIC_PROFILE` over both) through the app's Engine, over the pipe, headless
  unless `--visible`, and stops it when done: there is no start or stop. While the app's Chrome
  holds the profile, `status` says so and the rest refuse. `--attach [PORT]` drives the running
  app instead, which must have been started with `APPLE_MUSIC_DEBUG_PORT` (a bare `--attach`
  reads that variable). One JSON value per command, or `{"error": code, "message": …}` and
  exit 1. It never signs in.
- `demo_library.py` writes invented data only and, without options, the same library every
  time: tests and the metainfo screenshots depend on both.
- Scripts may print; app code logs.
