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
- screenshot.py and a11y_check.py also use the stock GNOME look (Adwaita icons, Adwaita Sans 11)
  and no animations, so shots do not depend on the desktop's theme.
- `screenshot.py` without `--demo` reads the real cache: never for a shot that is committed or
  shared.
- `a11y_check.py` cannot send real key presses here: `press()` runs the controllers a real event
  would reach. `--names` runs a private AT-SPI bus (a dbus-daemon and at-spi2-registryd) and
  stops it however the script exits.
- `am.py` drives the real engine on the app's profile and port (`APPLE_MUSIC_PROFILE` and
  `APPLE_MUSIC_PORT` override): one JSON value per command, or `{"error": code, "message": …}`
  and exit 1. It never signs in.
- `demo_library.py` writes invented data only and, without options, the same library every
  time: tests and the metainfo screenshots depend on both.
- Scripts may print; app code logs.
