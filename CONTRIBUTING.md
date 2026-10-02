# Contributing

Thanks for helping with Music Sleeve. Bug reports, fixes, features and translations are all
welcome; for anything bigger than a small fix, open or pick an issue first so the approach can
be agreed before the work. Everyone taking part is expected to follow the
[GNOME Code of Conduct](https://conduct.gnome.org).

## Setting up

The app needs GTK 4.20, libadwaita 1.9, GLib 2.84, Python 3.12 and PyGObject 3.50, or newer:
the libraries of GNOME 50. Nothing in it is tied to a distribution. Fedora 44, Ubuntu 26.04,
Arch Linux and openSUSE Tumbleweed ship those versions; Debian 13 and Ubuntu 24.04 do not.
To build and test it you also need Meson, gettext, `blueprint-compiler` 0.22 (Meson downloads
its own when the installed one is missing or older), pycairo for the demo library generator,
`appstream` and `desktop-file-utils` for the data checks, `ruff` for the lint, and `gjs` for
the tests of `src/backend/bridge.js` (skipped without it).

```bash
# Arch Linux
sudo pacman -S --needed gtk4 libadwaita python-gobject python-cairo meson blueprint-compiler \
    gettext appstream desktop-file-utils ruff gjs
# Fedora 44 or later
sudo dnf install gtk4-devel libadwaita-devel glib2-devel python3-gobject python3-gobject-devel \
    python3-cairo meson gettext appstream desktop-file-utils ruff gjs
```

Google Chrome is only needed to play music: the demo library and the tests work without it,
and without an Apple Account.

## Build, run and test

```bash
scripts/demo.sh          # build the development profile into ./build and run it on an
                         # invented library: no Chrome, no Apple Account
scripts/run.sh           # the same with the real engine (Chrome on the app's own profile)
scripts/check.sh         # byte-compile, ruff, build, unit tests, desktop/metainfo/schema checks
python3 -m unittest discover -s tests -v                        # the unit tests alone
scripts/headless.sh scripts/screenshot.py --demo build/shot.png --page albums [--light]
scripts/headless.sh COMMAND   # any of these on an invisible display, off your desktop
```

The development build has its own app ID (`io.github.jackicus.MusicSleeve.Devel`), Chrome
profile, cache and sign-in, so it installs beside a release build without touching it.
`scripts/check.sh` is what CI runs (in an Arch Linux container, with the widget tests on
Xvfb) and must pass before a pull request is merged. GNOME Builder can also build the
development profile through the Flatpak manifest in `build-aux/flatpak/`, which runs the
host's Chrome from the sandbox; that manifest has never been built, so it is untested.

## Conventions

[docs/architecture.md](docs/architecture.md) explains how the app is put together and
[docs/decisions.md](docs/decisions.md) why. The code conventions are in [CLAUDE.md](CLAUDE.md),
with the rules for each area in `.claude/rules/`; please read them before a larger change.
In short:

- Follow the [GNOME HIG](https://developer.gnome.org/hig/) and use libadwaita widgets and style
  classes before custom CSS.
- Never block the main loop: it runs both GTK and asyncio.
- Collections are list models over `Gio.ListStore`, not boxes of widgets.
- Every user-visible string goes through `_()`; a new file with strings goes in
  `po/POTFILES.in`. Source strings use en-GB spelling.
- New Python modules go in `src/meson.build`'s install list, new `.blp` files in its blueprint
  list and their `.ui` (by bare name) in `src/applemusic.gresource.xml`; a test checks these
  lists.
- Backend and model changes come with a unit test (stdlib `unittest`) in `tests/`.
- 4-space Python, single quotes; `ruff check .` must be clean.

## Privacy

The repository is public. Never commit real account data: no names, emails, playlist or song
titles, library IDs, tokens, artwork, or anything from `~/.cache/apple-music` or the Chrome
profile. Tests, fixtures, docs and screenshots use invented data only (`scripts/demo_library.py`
generates the demo library). The same goes for logs pasted into issues.

## Pull requests

1. Branch from `main`. You do not need write access: fork the repository, push your branch
   to your fork and open the pull request from there. CI runs on it automatically.
2. Keep commits focused (one logical change each, mechanical changes such as renames in their
   own commit), with messages that say what changes and why.
3. Fill in the pull request template: link the issues with `Closes #123` so they close on
   merge, and attach screenshots for UI changes.
4. `main` is protected: nothing is pushed to it directly, and a pull request is merged only
   once its `check.sh (Arch Linux)` run has passed. No approving review is required; the
   maintainer merges, and only the maintainer can. Pull requests are squashed, so `main` keeps
   a linear history, and the branch is deleted after the merge.

Security problems are reported privately, not as pull requests or issues: see
[SECURITY.md](SECURITY.md).

## Translating

The strings are in gettext's usual form, with the sources in British English. To start a
translation, say German, after a `scripts/demo.sh` or `scripts/check.sh` has configured
`build/`:

```bash
meson compile -C build music-sleeve-pot            # the template, music-sleeve.pot, into po/
cd po && msginit -l de -i music-sleeve.pot -o de.po
```

then add `de` to `po/LINGUAS`, translate `de.po` and rebuild. `meson compile -C build
music-sleeve-update-po` merges new strings into every translation.
