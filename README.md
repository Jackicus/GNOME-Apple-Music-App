# Music Sleeve

A native GNOME app for Apple Music: your library, playlists, radio and
recommendations, in GTK 4 and libadwaita.

Not affiliated with Apple. Apple Music is a trademark of Apple Inc.

![Home: heavy rotation and recently added music, a song playing below](data/screenshots/home-light.png)

<p align="center">
  <img src="data/screenshots/album-dark.png" width="49%" alt="An album's page, in the dark style">
  <img src="data/screenshots/now-playing-light.png" width="49%" alt="Now Playing, with the lyrics following the song">
</p>

The screenshots show an invented demo library.

> [!IMPORTANT]
> The app plays music through **Google Chrome**, and it must be Google's own build:
> Chromium will not work. Apple Music's streams are protected with Widevine DRM, which
> Chrome ships and Chromium does not, and Apple offers no public streaming API. The app
> starts Chrome on a private profile, shows music.apple.com in it and drives Apple's own
> player there. Chrome opens a window once, for you to sign in, and runs hidden after
> that; the sound comes out of Chrome, so your system's per-app volume lists it as Chrome.

## What it does

- Your library: Recently Added, Artists, Albums, Songs, Music Videos and Made for You,
  with your playlists and playlist folders in the sidebar.
- Home, New, Radio and Search, laid out like Apple's web player.
- A player bar, and a Now Playing view with synced lyrics and the queue.
- GNOME's media controls (MPRIS) and the keyboard's media keys.
- Light and dark styles; the window adapts down to 360 px wide.

What it does not do: offline listening or downloads (it streams, as the web player does);
music videos with a picture (they play as audio); creating, renaming or reordering playlists
(adding songs to one works). The whole list is in the [user guide](docs/user-guide.md).

## Requirements

- An Apple Music subscription.
- Google Chrome, found as `google-chrome-stable`, `google-chrome` or
  `/opt/google/chrome/chrome`; Preferences › Engine › Browser Program can name another
  command.
- GTK 4.20, libadwaita 1.9, GLib 2.84, Python 3.12 and PyGObject 3.50, or newer: the
  libraries of GNOME 50. Any distribution that ships them will do. Fedora 44, Ubuntu 26.04,
  Arch Linux and openSUSE Tumbleweed do; Debian 13 and Ubuntu 24.04 are too old.
- To build it: Meson 1.2, gettext and `blueprint-compiler` 0.22 or newer. When the
  installed `blueprint-compiler` is missing or older (Fedora 44 and Ubuntu 26.04 ship an
  older one), Meson downloads its own during setup.

## Install

There is no distribution package yet. Build from the
[latest release](https://github.com/Jackicus/GNOME-Music-Sleeve/releases/latest)'s
tarball, or from a clone of the repository.

```bash
# The build dependencies, by distribution:
sudo pacman -S --needed gtk4 libadwaita python-gobject meson blueprint-compiler gettext     # Arch
sudo dnf install gtk4-devel libadwaita-devel glib2-devel python3-gobject python3-gobject-devel \
    meson gettext                                                                             # Fedora 44+
sudo apt install libgtk-4-dev libadwaita-1-dev python3-gi python-gi-dev meson gettext        # Ubuntu 26.04

tar xf music-sleeve-0.9.0.tar.xz && cd music-sleeve-0.9.0
meson setup _build --prefix=/usr
meson compile -C _build
sudo meson install -C _build --skip-subprojects
```

To uninstall: `sudo ninja -C _build uninstall`, then `sudo rm -r /usr/share/music-sleeve`
for the byte-compiled modules. On Arch Linux, `build-aux/aur/PKGBUILD` is the AUR package's
source, `music-sleeve`; until it is published there, `makepkg -si` in that directory builds
and installs it. Google Chrome is in the AUR as `google-chrome`.

## First run

1. Start Music Sleeve. The sidebar ends with a **Sign In** button.
2. Click it. A Chrome window opens on music.apple.com: sign in there with your Apple Account,
   as you would in a browser. The app never sees the password.
3. Once you are signed in, the window closes, Chrome carries on hidden and the app syncs your
   library. A banner shows the progress.

From then on the app starts Chrome by itself. <kbd>Ctrl</kbd>+<kbd>?</kbd> lists the keyboard
shortcuts. The [user guide](docs/user-guide.md) covers the rest: refreshing the library,
playing in the background, troubleshooting, privacy, and where your data lives and how to
remove it.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) explains how to build and run the app on an invented
library without Chrome or an account, the conventions, and how to translate it.
[docs/architecture.md](docs/architecture.md) describes how it is put together.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
