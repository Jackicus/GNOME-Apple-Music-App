# Apple Music for GNOME

A native GNOME app for Apple Music, built with GTK 4 and libadwaita. The pages
are laid out like Apple's web player: Home, New, Radio and Search, then your
library (Recently Added, Artists, Albums, Songs, Music Videos, Made for You) and
your playlists and playlist folders in the sidebar. Playing music opens a player
bar with a Now Playing view that shows synced lyrics and the queue. It also works
with GNOME's media controls and the keyboard's media keys.

![Home, in the light style](data/screenshots/home-light.png)

The screenshots show an invented demo library.

**Status:** 0.9.0, the first release candidate.

Not affiliated with Apple. Apple Music is a trademark of Apple Inc.

## How it plays music, and why it needs Google Chrome

Apple Music streams are protected with Widevine DRM, and Apple offers no public
streaming API. WebKitGTK cannot play them, and neither can Chromium, which ships
without Widevine. Google's own Chrome can. So the app starts Google Chrome with
a private profile, shows music.apple.com in it and drives Apple's own player in
that page over Chrome's DevTools protocol, through a private pipe (no network
port). Chrome opens a window once, for you to sign in. After that it runs headless, with no window.

Because the sound comes from Chrome, your system's per-app volume controls list
it as Chrome. The app talks only to Apple: through that Chrome page, plus
artwork that it downloads directly from Apple's servers.

## Requirements

- An Apple Music subscription.
- **Google Chrome** (`google-chrome-stable`, `google-chrome` or
  `/opt/google/chrome/chrome`; Preferences › Engine can name another command).
  Chromium will not work.
- GTK 4.20 or later, libadwaita 1.9 or later, GLib 2.84 or later, and Python 3
  with PyGObject 3.50 or later. It is developed on Python 3.14.
- To build it: Meson 1.1 or later, gettext and `blueprint-compiler` 0.22. If
  `blueprint-compiler` is not installed, Meson downloads it.

## Install

### Arch Linux

Install `gnome-apple-music` from the AUR, and `google-chrome` from the AUR as
well:

```bash
yay -S gnome-apple-music google-chrome     # or your AUR helper of choice
```

The PKGBUILD is kept in this repository as `build-aux/aur/PKGBUILD`.

### From source, with Meson

```bash
git clone https://github.com/Jackicus/GNOME-Apple-Music-App.git
cd GNOME-Apple-Music-App
meson setup build --prefix=/usr
meson compile -C build
sudo meson install -C build --skip-subprojects
```

The install byte-compiles the app's Python modules. `--skip-subprojects` only
matters when Meson had to download `blueprint-compiler`: without it, that
subproject would install a file of its own. To uninstall, run
`sudo ninja -C build uninstall`, then `sudo rm -r /usr/share/apple-music` to
remove the compiled bytecode as well.

## First run and signing in

1. Start **Apple Music** from the app grid. On a first run, the sidebar ends
   with a **Sign In** button.
2. Click **Sign In**. A Chrome window opens on music.apple.com. Sign in there
   with your Apple Account, as you would in a browser.
3. Once Apple reports you as signed in, the window closes and Chrome carries on
   headless. The app then syncs your library. A banner shows its progress.

After that, the app starts the engine by itself whenever it starts, and it
refreshes the library every six hours. You can change both in Preferences
(<kbd>Ctrl</kbd>+<kbd>,</kbd>), or refresh at any time with <kbd>Ctrl</kbd>+<kbd>R</kbd>.
Closing the window quits the app and stops Chrome. To keep the music playing
with the window closed, turn on background playback in Preferences.
<kbd>Ctrl</kbd>+<kbd>?</kbd> lists every keyboard shortcut.

## Where your data lives, and how to remove it

| What | Where |
|---|---|
| The library snapshot, artwork, lyrics and cached pages | `~/.cache/apple-music/` |
| Chrome's profile, which holds your Apple sign-in | `~/.local/share/apple-music/chrome/` |
| Settings | GSettings schema `io.github.jackicus.AppleMusic` |

The development build uses `~/.local/share/apple-music/chrome-devel/` and
`~/.cache/apple-music-devel/` instead.

- **Sign Out** (in the account menu at the bottom of the sidebar) stops Chrome
  and deletes both the Chrome profile and the cache.
- **Preferences › General › Cache › Clear** deletes only the cache.
- To remove everything by hand, quit the app first, then run:

  ```bash
  rm -r ~/.cache/apple-music ~/.local/share/apple-music
  gsettings reset-recursively io.github.jackicus.AppleMusic
  ```

## Development

```bash
scripts/run.sh          # build the development profile (.Devel ID) into ./build and run it
scripts/run.sh --debug  # the same, with debug logging (or set APPLE_MUSIC_DEBUG=1)
scripts/demo.sh         # the same on an invented library (build/demo): no Chrome, no account
scripts/check.sh        # byte-compile, lint (ruff, if installed), unit tests, build,
                        # and validate the desktop, metainfo and schema files
python3 -m unittest discover -s tests -v    # the unit tests alone
scripts/screenshot.py build/shot.png --demo --page albums [--light] [--size WxH]
scripts/am.py status    # drive the engine without the GUI (start, stop, eval, events…)
```

The development build installs beside a release build, with its own app ID,
Chrome profile and port, and shares the release build's settings. `--demo`
never starts Chrome. `docs/architecture.md` describes the architecture, `CLAUDE.md`
the conventions, and `docs/history/build-plan.md` holds the plan the app was built from.

GNOME Builder can build and run the development profile through the Flatpak
manifest in `build-aux/flatpak/`. That manifest is for development only: from
the sandbox, the app runs the host's Google Chrome through `flatpak-spawn
--host`. Flathub is out of scope, both because the app depends on the host's
Chrome and because the name carries Apple's trademark.

To make a release tarball, run `meson dist -C build`.

## License

GPL-2.0-or-later. See `LICENSE`.
