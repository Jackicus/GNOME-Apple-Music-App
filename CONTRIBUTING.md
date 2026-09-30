# Contributing

Thanks for helping with Music Sleeve. Everyone taking part is expected to follow the
[GNOME Code of Conduct](https://conduct.gnome.org).

## Build and test

You need GTK 4.20+, libadwaita 1.9+, GLib 2.84+, Python 3.12+ with PyGObject 3.50+, Meson 1.2+,
gettext and `blueprint-compiler` 0.22 (Meson downloads it when it is missing). The demo library
generator (`scripts/demo_library.py`, used by the tests) also needs pycairo (`python-cairo` on
Arch). Google Chrome is only needed to play music: the demo library and the tests work without
it.

```bash
scripts/demo.sh          # build the development profile into ./build and run it on an
                         # invented library: no Chrome, no Apple Account
scripts/run.sh           # the same with the real engine (Chrome on the app's own profile)
scripts/check.sh         # byte-compile, ruff, build, unit tests, desktop/metainfo/schema checks
python3 -m unittest discover -s tests -v                        # the unit tests alone
scripts/headless.sh scripts/screenshot.py --demo build/shot.png --page albums [--light]
scripts/headless.sh COMMAND   # any of these on an invisible display, off your desktop
```

`scripts/check.sh` is what CI runs (in an Arch Linux container, with widget tests on Xvfb). It
must pass before a pull request is merged.

## Conventions

[docs/architecture.md](docs/architecture.md) explains how the app is put together and
[docs/decisions.md](docs/decisions.md) why. The code conventions (Blueprint templates, `_()` for
every user-visible string, never blocking the main loop, list models instead of boxes of widgets,
logging, settings, actions) are in [CLAUDE.md](CLAUDE.md), with the rules for each area in
`.claude/rules/`. Please read them before a larger change. In short:

- Follow the [GNOME HIG](https://developer.gnome.org/hig/) and use libadwaita widgets and style
  classes before custom CSS.
- Every user-visible string goes through `_()`; a new file with strings goes in
  `po/POTFILES.in`. Source strings use en-GB spelling.
- New Python modules go in `src/meson.build`'s install list, new `.blp` files in its blueprint
  list and their `.ui` (by bare name) in `src/applemusic.gresource.xml` (a test checks these
  lists).
- Backend and model changes come with a unit test (stdlib `unittest`) in `tests/`.
- 4-space Python, single quotes; `ruff check .` must be clean.

## Privacy

The repository is public. Never commit real account data: no names, emails, playlist or song
titles, library IDs, tokens, artwork, or anything from `~/.cache/apple-music` or the Chrome
profile. Tests, fixtures, docs and screenshots use invented data only (`scripts/demo_library.py`
generates the demo library). The same goes for logs pasted into issues.

## Pull requests

1. Open or pick an issue first for anything bigger than a small fix, so the approach can be
   agreed.
2. Branch from `main`, keep commits focused (one logical change each, mechanical changes such as
   renames in their own commit), and write commit messages that say what changes and why.
3. Fill in the pull request template: link the issues with `Closes #123` so they close on merge,
   and attach screenshots for UI changes.
4. CI must be green. Pull requests are merged by rebase or squash; the branch is deleted after
   the merge.

Security problems are reported privately: see [SECURITY.md](SECURITY.md).
