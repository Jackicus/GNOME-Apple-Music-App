# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately**, through GitHub's private vulnerability reporting:
[open a report](https://github.com/Jackicus/GNOME-Apple-Music-App/security/advisories/new)
(Security › Report a vulnerability on the repository page). Do not open a public issue, and do
not include your own Apple Account details or anything from your Chrome profile in the report.

You can expect an acknowledgement within a week. Once a fix is ready it is released and the
advisory is published, crediting you unless you prefer otherwise.

## Supported versions

Only the latest release, and `main`, get security fixes. The app is at its first release
candidate (0.9.0).

## How the app works, and what is in scope

Apple Music streams are protected with Widevine DRM, so the app cannot play them itself. Its
playback engine is a local **Google Chrome** that the app starts with a private profile, showing
music.apple.com. The app drives Apple's own MusicKit player in that page over Chrome's DevTools
protocol, through a small script it injects (`src/backend/bridge.js`). Chrome's window opens once
for sign-in; afterwards it runs headless.

In scope, for example:

- Anything that lets another local user or process reach the engine: the DevTools connection
  gives full control of a browser that is signed in to your Apple Account. (Until the engine
  moves to a pipe, the DevTools port listens on 127.0.0.1; see the issues labelled
  [`security`](https://github.com/Jackicus/GNOME-Apple-Music-App/issues?q=label%3Asecurity).)
- Files the app writes with modes that let other users read them, or writes outside its own
  directories.
- Sign-out or Clear Cache leaving account data behind.
- Data from Apple's API or a web page reaching the UI in a way that can run code or inject
  markup.
- The packaging (Meson install, AUR PKGBUILD, Flatpak manifest) installing something unsafe.

Out of scope: vulnerabilities in Google Chrome, GTK, libadwaita, PyGObject or Apple's services
themselves (report those upstream), and anything that needs an attacker who already runs code as
your user.

## What the app stores

| What | Where |
|---|---|
| Library snapshot, artwork, lyrics and cached pages | `$XDG_CACHE_HOME/apple-music/` (usually `~/.cache/apple-music/`) |
| Chrome's profile, which holds your Apple sign-in | `$XDG_DATA_HOME/apple-music/chrome/` (`chrome-devel/` for the development build) |
| Which Chrome process belongs to the app | `$XDG_RUNTIME_DIR/apple-music/engine.json` |
| Settings | GSettings schema `io.github.jackicus.AppleMusic` |

Sign Out stops Chrome and deletes the Chrome profile and the cache; Preferences › Clear Cache
deletes the cache only.

## What the app never does

- It sends nothing to anyone but Apple: the only network traffic is the music.apple.com page in
  its own Chrome (and the Apple services that page uses) and artwork downloaded directly from
  Apple's servers.
- No telemetry, analytics, crash reporting or update checks.
- It never sees or stores your Apple Account password: you sign in on Apple's own page in
  Chrome, and the session lives in Chrome's profile.
- It does not read your other browser profiles.
