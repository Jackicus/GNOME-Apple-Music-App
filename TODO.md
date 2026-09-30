# TODO

What is left, as of 2026-09-30. Music Sleeve (`io.github.jackicus.MusicSleeve`) is released:
[v0.9.0](https://github.com/Jackicus/GNOME-Music-Sleeve/releases/tag/v0.9.0), the first
release, and v0.10.0, which adds Discord rich presence. A release is a tag and a GitHub release
(`docs/release.md`); why the app is built the way it is, is in `docs/decisions.md`.

## Open

- [ ] Publish to the AUR when the maintainer wants it: `build-aux/aur/` is ready (set the
      `# Maintainer:` contact, `updpkgsums && makepkg --printsrcinfo > .SRCINFO`, `makepkg -si`
      to try it, then push to aur.archlinux.org with the maintainer's account; README's install
      section changes then).
- [ ] [#193](https://github.com/Jackicus/GNOME-Music-Sleeve/issues/193): a refresh re-fetches the
      whole library, however little changed. The quick pass (#192) halved the common case;
      this is the rest.
- [ ] [#153](https://github.com/Jackicus/GNOME-Music-Sleeve/issues/153): a lazy Songs model, about
      21 MB on a large library. Only if memory turns out to matter on a real one.
- [ ] Build and run the Flatpak in GNOME Builder (the manifest in `build-aux/flatpak/` has
      never been built: no GNOME 50 runtime on the development machine). It can reach a
      native or Flatpak Discord's socket; that is untested too.
- [ ] Translations: the strings are ready (`po/`), no languages yet.
- [ ] The internal names that still say Apple Music, none of them user-visible: the
      `applemusic` Python package, the `AppleMusic*` GType names, the `APPLE_MUSIC_*`
      environment variables, and the cache and Chrome profile directories. Left alone on
      purpose: renaming the directories would strand an existing sign-in and library.

## Working on it

Every change goes through a pull request with CI. `CLAUDE.md` and `.claude/` hold the rules for
Claude Code sessions (area rules load as files are touched; skills: `fix-bug`, `hig-polish`,
`performance-pass`, `review-pass`, `screenshots`, `live-engine-check`). Run GUI scripts through
`scripts/headless.sh` so no window opens on the desktop.

`ruff`, `python-cairo` and `oxipng` are installed here (2026-09-30), so `scripts/check.sh`
runs the lint and the whole suite. Without the first two it used to skip the lint and three
demo-library tests silently, which once let a line-length error reach CI; if a machine lacks
them, `sudo pacman -S ruff python-cairo oxipng`.
