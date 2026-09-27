# Apple Music for GNOME

> **Early work in progress.** Right now this is the app's shell: the window,
> the sidebar and the build. Nothing plays yet.

A native GNOME app for Apple Music, built with GTK 4 and libadwaita. Its
layout follows the Apple Music web player: Home, New and Radio, your library,
and your playlists in a sidebar.

Not affiliated with Apple. Apple Music is a trademark of Apple Inc.

## Building

You need GTK 4.20+, libadwaita 1.9+, PyGObject and Meson. `blueprint-compiler`
is used if installed; otherwise Meson fetches it.

```bash
scripts/run.sh          # build the development profile into ./build and run it
scripts/run.sh --debug  # the same with debug logging (or set APPLE_MUSIC_DEBUG=1)
scripts/demo.sh         # the same on an invented demo library (build/demo), no account needed
scripts/check.sh        # byte-compile, lint (if ruff is installed), run the unit
                        # tests, build, validate the desktop/metainfo/schema files
python3 -m unittest discover -s tests -v    # just the unit tests
```

To install system-wide:

```bash
meson setup build --prefix=/usr
meson install -C build
```

GNOME Builder can also open the folder and run it through the Flatpak manifest
in `build-aux/flatpak/`.

## License

GPL-2.0-or-later.
