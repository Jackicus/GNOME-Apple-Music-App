# Releasing and packaging

Releases are native: `meson install` with the release profile, and the AUR package
`music-sleeve`, whose PKGBUILD lives in `build-aux/aur/`. The Flatpak manifest is for
development only, and Flathub is out of scope (`docs/decisions.md`). Tagging a release,
publishing it on GitHub and uploading the package to the AUR are the owner's steps: an agent
prepares everything up to them and stops there.

## Checklist

1. `main` is green in CI and `scripts/check.sh` passes locally.
2. Walk `docs/accessibility.md`'s keyboard checklist (`scripts/a11y_check.py`, also
   `--size 360x640` and `--names`).
3. Retake the metainfo screenshots if the UI changed (the `screenshots` skill, step 5), and
   look at every one.
4. Set the version in `meson.build` (`version`). The development profile appends the git
   revision by itself.
5. Add a `<release version="…" date="…">` at the top of the metainfo's `<releases>`, with a
   short description: a `<p>` and a `<ul>`.
6. Pin the metainfo's screenshot URLs to the tag: `/main/` becomes `/v<version>/` in each
   `<image>`, so an installed copy keeps showing the shots it was released with whatever
   later happens to the files on `main`.
7. Commit, then tag `v<version>` (annotated) and push the tag. The PKGBUILD downloads
   GitHub's archive of that tag.
8. Update the PKGBUILD: `pkgver`, `pkgrel=1`, then `updpkgsums` (it fills in the checksum once
   the tag exists) and `makepkg --printsrcinfo > .SRCINFO`. Set the `# Maintainer:` line's
   contact to the one the owner wants public on the AUR (the repository's copy points at the
   GitHub profile). Test it as below, then publish to the AUR.

## Installing from source

```bash
meson setup _build --prefix=/usr
meson compile -C _build
sudo meson install -C _build --skip-subprojects
```

Use a build directory of its own, never `build/`: that is the development build that
`scripts/run.sh` and `scripts/check.sh` keep on the development profile with the prefix
`build/install`. Running `meson setup build --prefix=/usr` on it only changes the prefix and
keeps the development profile, so the install would put the `.Devel` build into /usr.

`--skip-subprojects` matters when Meson fell back to the blueprint-compiler wrap: that
subproject would otherwise install its own `reference_docs.json` into the prefix. To
uninstall: `sudo ninja -C _build uninstall`, then remove `/usr/share/music-sleeve`, since the
byte-compiled `__pycache__` directories are not in Meson's install log.

## Byte-compiling

The app's modules install as data under `share/music-sleeve/applemusic`, outside
site-packages, so Meson's own `python.bytecompile` never reaches them. `src/meson.build` runs
`build-aux/meson/compile-python.py` at install time instead: `compileall` over the installed
package, level 0, plus `-O` or `-OO` when `python.bytecompile` is 1 or 2 (arch-meson sets 1),
skipped at -1. Only stale files are compiled again, so development installs stay quick.

## Testing the packages

- `meson dist -C build` archives HEAD (commit first) without the subprojects, into
  `build/meson-dist/`, and builds and tests the tarball with the build directory's options, so
  it needs `blueprint-compiler` installed or the network for the wrap.
- Test a tarball in a temporary directory with its own `--prefix`.
- Test the PKGBUILD in a temporary copy: make a `git archive --prefix=GNOME-Apple-Music-App-<version>/`
  tarball named as its source (makepkg then skips the download) and run `makepkg` there without
  `-i`: build and check it, don't install it. Without `blueprint-compiler` installed, use
  `makepkg --nodeps` with a `blueprint-compiler` shim on PATH that runs
  `subprojects/blueprint-compiler/blueprint-compiler.py`.
- The PKGBUILD's `check()` runs `meson test`: the unit tests (Meson's `unit` suite, whose
  widget tests skip without a display) and the desktop file, metainfo and schema validation.

## Screenshots

`data/screenshots/{home,albums,album,now-playing}-{light,dark}.png`, 1100×760 at scale 1, taken
with `scripts/screenshot.py --demo` from the invented library only (the `screenshots` skill has
the exact options). The metainfo lists the light ones first (`environment="gnome"`, Home as
`type="default"`), then the dark ones (`environment="gnome:dark"`), by
`raw.githubusercontent.com/…/data/screenshots/…` URLs: on `main` between releases, pinned to
the tag at a release (step 6). Renaming or removing a file still breaks the metainfo of any
build from `main` that names it. Shrink them losslessly (oxipng through `uv run --no-project
--with pyoxipng` saved about 15 %), and check that none carries a text chunk before
committing.

## The Flatpak manifest

`build-aux/flatpak/io.github.jackicus.MusicSleeve.Devel.json` builds the development profile
for GNOME Builder or flatpak-builder. It talks to `org.freedesktop.Flatpak` so the app can run
the host's Chrome, and has no audio socket, because the host's Chrome makes the sound; the MPRIS
name, `org.mpris.MediaPlayer2.<app id>`, is one Flatpak lets an app own without asking. Its
desktop entry's name ends in " (Development)". In the sandbox, `chrome.find_chrome()` asks the host for the
binary through `flatpak-spawn --host`, and `chrome_args()` prefixes
`flatpak-spawn --host --watch-bus --forward-fd=3 --forward-fd=4`, which passes the DevTools pipe
on to the host's Chrome. The pid the engine holds is flatpak-spawn's, which relays SIGTERM to
Chrome and whose end ends Chrome (`--watch-bus`), so `setpriv` is not used there; the profile's
`SingletonLock` names a host pid, which the sandbox cannot check, so a Chrome left on the
profile is not ended at the next start. It has not been built: there is no GNOME 50 runtime or
flatpak-builder on the development machine.

Nothing installed may set `APPLE_MUSIC_DEBUG_PORT`: the launcher, the desktop file, the
PKGBUILD and the manifest leave the engine on its pipe alone.
