# Security policy

## Reporting a vulnerability

Report vulnerabilities privately, through GitHub's private vulnerability reporting:
[open a report](https://github.com/Jackicus/GNOME-Music-Sleeve/security/advisories/new)
(Security › Report a vulnerability on the repository page). Do not open a public issue for
one, and do not put your own Apple Account details or anything from your Chrome profile in
the report.

One person maintains this app in their spare time, so allow some days for a reply before
assuming the report was missed. Once a fix is released, the advisory is published, crediting
you unless you prefer otherwise.

## Supported versions

Only the latest release and `main` get security fixes.

## The threat model

The app plays music through a local Google Chrome that it starts on a private profile,
showing music.apple.com. That Chrome is signed in to your Apple Account, and the app has full
control of it over Chrome's DevTools protocol, so the connection between them is what matters
most. It runs over a private pipe between the two processes, with no listening port; the
Chrome profile and the cache are the app's own directories under your home.

In scope, for example:

- Anything that lets another local user or process reach the DevTools connection, or the
  signed-in session through some other route. (`APPLE_MUSIC_DEBUG_PORT` deliberately opens a
  port on 127.0.0.1 for developers; that it does so is documented, not a vulnerability.)
- Files the app writes with modes that let other users read them, or outside its own
  directories.
- Sign Out or Clear Cache leaving account data behind.
- Data from Apple's API or a web page reaching the UI in a way that runs code or injects
  markup.
- The packaging (the Meson install, the AUR PKGBUILD, the Flatpak manifest) installing
  something unsafe.

Out of scope: vulnerabilities in Google Chrome, GTK, libadwaita, PyGObject or Apple's services
themselves (report those upstream), and anything that needs an attacker who already runs code
as your user.

## What the app does with your data

- It sends nothing to anyone but Apple: its traffic is the music.apple.com page in its own
  Chrome (and the Apple services that page uses) and artwork downloaded directly from Apple.
  Chrome, as the engine, also talks to Google as any Chrome does (component updates, the
  Widevine module among them, and Safe Browsing); the app does not turn that off.
- No telemetry, analytics, crash reporting or update checks.
- It never sees or stores your Apple Account password: you sign in on Apple's own page in
  Chrome, and the session lives in Chrome's profile. It does not read your other browser
  profiles.
- The library snapshot, artwork, lyrics and cached pages live in `$XDG_CACHE_HOME/apple-music/`,
  the Chrome profile with the sign-in in `$XDG_DATA_HOME/apple-music/chrome/`, and the
  settings in the GSettings schema `io.github.jackicus.MusicSleeve`. Sign Out deletes the
  profile and the cache; the [user guide](docs/user-guide.md) says how to remove everything.
