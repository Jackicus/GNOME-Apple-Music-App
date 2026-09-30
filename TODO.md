# TODO

What is left before the first release, as of 2026-09-30. The app is feature-complete for 0.9.0:
all 20 build phases and the 13 work packages of the pre-release audit are merged, CI is green,
and the icon, the name and the live account checks are done. The audit's plan, results and
live-verification summary are on the tracking issue
[#3](https://github.com/Jackicus/GNOME-Music-Sleeve/issues/3).

The app is **Music Sleeve** (`io.github.jackicus.MusicSleeve`), with an icon of its own: a
record coming out of its sleeve. Why, and what it replaced, is in `docs/decisions.md`.

## 1. The last manual checks

The live account checks all passed on 2026-09-30 (the summary is a comment on
[#154](https://github.com/Jackicus/GNOME-Music-Sleeve/issues/154), which also lists what was done by
hand: the Shell media card, media keys, lyrics, drag onto a playlist, sign-out and sign-in).
These few need your hands and have not been done:

- [ ] **Clear Cache** (Preferences › Clear): the cache goes, the library reloads from a fresh
      sync, and nothing a running sync or download was writing comes back afterwards.
- [ ] **Add to Library** on a search result, and **Add to Playlist** from a row's context
      submenu: a toast each, and the item is there after a refresh.
- [ ] A keyboard walkthrough with real key presses, following `docs/accessibility.md`.
      [#169](https://github.com/Jackicus/GNOME-Music-Sleeve/issues/169) would automate it later.

## 2. The one decision left

- [ ] [#146](https://github.com/Jackicus/GNOME-Music-Sleeve/issues/146) **When to tag v0.9.0 and
      publish to the AUR.** Everything it was waiting on is done except section 1 above.

## 3. Release (when #146 says go)

From `docs/release.md`:

- [ ] Update the `<release version="0.9.0" date="…">` notes in
      `data/io.github.jackicus.MusicSleeve.metainfo.xml.in` and pin the screenshot URLs to the
      tag.
- [ ] `git tag -a v0.9.0 -m 'Music Sleeve 0.9.0' && git push origin v0.9.0`
- [ ] In `build-aux/aur/`: the package is now `music-sleeve`. Set the `# Maintainer:` line's
      contact to the one you want public, `updpkgsums && makepkg --printsrcinfo > .SRCINFO`
      (the checksum is `SKIP` until the tag exists), `makepkg -si` to try it, then publish.
- [ ] Optional: a GitHub release with `meson dist -C build`'s tarball.

## 4. Later, optional

- [ ] [#182](https://github.com/Jackicus/GNOME-Music-Sleeve/issues/182): the Shell's media card
      blinks briefly as the track changes. Cosmetic; the issue has the steps to measure it.
- [ ] [#153](https://github.com/Jackicus/GNOME-Music-Sleeve/issues/153): a lazy Songs model, about
      21 MB on a large library. Only if memory turns out to matter on a real one.
- [ ] [#180](https://github.com/Jackicus/GNOME-Music-Sleeve/issues/180): `tests.test_tiles` crashes
      GTK when run on its own (it passes in the full suite and in CI).
- [ ] Build and run the Flatpak in GNOME Builder (the manifest in `build-aux/flatpak/` has
      never been built: no GNOME 50 runtime on the development machine).
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

This machine needs `ruff` and `python-cairo` installed (`sudo pacman -S ruff python-cairo`):
without them `scripts/check.sh` quietly skips the lint and three demo-library tests.

Delete this file once it's all done.
