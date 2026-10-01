---
paths:
  - "meson.build"
  - "meson.options"
  - "src/meson.build"
  - "src/applemusic.gresource.xml"
  - "src/music-sleeve.in"
  - "data/**"
  - "po/**"
  - "build-aux/**"
  - "subprojects/**"
  - ".github/**"
  - "pyproject.toml"
---

# Build, data, translations, packaging and CI

- The hand-kept lists, all checked by tests/test_build_lists.py: `src/meson.build`'s blueprint
  list (one `custom_target` per `.blp`, its `.ui` flat in build/src) and its `install_data`
  lists (the app's modules; `pages/`, `widgets/`, `dialogs/` and `backend/`, bridge.js included,
  each their own); `src/applemusic.gresource.xml` (every `.ui` by bare name, style.css, the
  icons, aliased into `icons/scalable/actions/`, and `metainfo.xml`, the generated metainfo
  copied in by src/meson.build, which the About dialog is built from); `po/POTFILES.in`
  (every `.py` and `.blp` with `_()`, `ngettext` or `C_`).
- `-Dprofile=development` gives the `.Devel` app ID (desktop file, icons, metainfo, MPRIS name),
  the name with " (Development)" after it (`NAME_SUFFIX` in the desktop file and the metainfo;
  the Flatpak manifest adds none), the version with the git revision, and a `DEMO_DIR`
  pointing at the source tree's build/demo.
  A release build hides `--demo` from `--help`, and there it needs `APPLE_MUSIC_CACHE`.
  Both profiles install the same GSettings schema ID and resource path.
- The launcher (`src/music-sleeve.in`) calls `i18n.setup(localedir)` before anything is
  translated (it binds the domain for both Python's gettext and GtkBuilder), loads the
  gresource from pkgdatadir and calls `main.main()`. The modules install as data under
  `share/music-sleeve/applemusic`, outside site-packages, so `build-aux/meson/compile-python.py`
  byte-compiles them at install time (its level follows `python.bytecompile`).
- `data/meson.build` validates the desktop file, the metainfo (`appstreamcli validate
  --no-net`) and the schema (`glib-compile-schemas --strict`) as Meson tests, which check.sh
  runs; each is skipped when its tool is missing. A new settings key has a summary.
- The desktop file is `DBusActivatable=true`: `data/<id>.service.in` (installed as
  `<app id>.service` under `share/dbus-1/services`, its `Exec` the installed launcher with
  `--gapplication-service`) lets the bus start the app, for a launch and for the Shell's
  search (`data/<id>.search-provider.ini.in`, installed under
  `share/gnome-shell/search-providers`, whose `ObjectPath` is the app ID with dots as
  slashes plus `/SearchProvider`: data/meson.build derives both from `application_id`, so
  the .Devel build has its own). A Flatpak exports both directories itself and rewrites
  the service's `Exec` (untested here, like the manifest).
- The minimum versions live in `meson.build` (with the reason next to the GLib one). Raising
  one also means README.md, CONTRIBUTING.md, the PKGBUILD's depends and CI's package list.
- The metainfo lists its screenshots by `raw.githubusercontent.com/…/data/screenshots/…`
  URLs, on `main` between releases and pinned to the tag at a release (docs/release.md):
  renaming or removing a screenshot breaks the metainfo of any build that names it. Screenshots
  come from the demo library only (the `screenshots` skill).
- `build-aux/aur/PKGBUILD` is the AUR package's source. After any change to it:
  `updpkgsums && makepkg --printsrcinfo > .SRCINFO`. Test it without installing the package.
  Its modules are byte-compiled for the Python that built it (outside site-packages, which
  Arch's Python rebuilds cover), so each Python minor release needs a pkgrel bump.
- `build-aux/flatpak/*.Devel.json` is for development only (why in its `x-comment`) and has
  not been built here: there is no GNOME 50 runtime or flatpak-builder on the development
  machine. Say so when you change it.
- If Meson falls back to the blueprint-compiler wrap, that subproject installs a file of its
  own: installs use `--skip-subprojects`.
- CI (`.github/workflows/ci.yml`) installs its dependencies with pacman in an Arch Linux
  container and runs `xvfb-run scripts/check.sh`. A new build or test dependency goes on its
  pacman line too. It has no Chrome, and the tests need none (tests.md).
- Nothing installed (the launcher, the desktop file, the PKGBUILD, the Flatpak manifest) sets
  `APPLE_MUSIC_DEBUG_PORT`: it opens the signed-in session to every local program (engine.md).
- Release steps (version, metainfo release notes, tag, tarball, AUR) are in docs/release.md.
- **Apple's marks.** The app is Music Sleeve, with an icon of its own (`docs/decisions.md`,
  #145). Apple's guidelines for third parties allow no use of their logos, icons or artwork at
  all, and no imitation of them, and no Apple trademark as or as part of a product name;
  Flathub requires a name and icon distinct enough that no official affiliation is implied. So:
  the display name, the app ID, the icons and the AUR package never carry an Apple mark, and
  the icon is never drawn from Apple's artwork or in imitation of it. "Apple Music" is used
  referentially only — the metainfo summary and description, the desktop file's `Keywords=`,
  and strings that name the service — always less prominent than the app's own name. The
  "Not affiliated with Apple. Apple Music is a trademark of Apple Inc." notice stays in the
  metainfo description and the About dialog (`src/dialogs/about.py`), and the README keeps its
  equivalent. Changing any of this is a decision for the owner, not a tidy-up.
