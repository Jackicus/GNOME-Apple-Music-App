# User guide

Using Music Sleeve once it is installed: signing in, everyday use, what it cannot do, what to
do when something goes wrong, what it sends where, and where it keeps your data. The
[README](../README.md) has the requirements and the install steps.

## Signing in

1. Start Music Sleeve. On a first run, the sidebar ends with a **Sign In** button.
2. Click **Sign In**. A Chrome window opens on music.apple.com. Sign in there with your Apple
   Account, as you would in a browser: the app never sees the password.
3. Once Apple reports you as signed in, the window closes and Chrome carries on hidden. The
   app then syncs your library, and a banner shows its progress.

## Everyday use

- The app starts Chrome, its playback engine, whenever it starts. Preferences › Engine › Start
  the engine with the app turns that off; the first thing you play then starts it.
- While the engine runs, the library is refreshed when the last refresh is older than the
  interval chosen in Preferences (every hour, every 6 hours, the default, every day, or
  manually). <kbd>Ctrl</kbd>+<kbd>R</kbd> refreshes it at any time.
- Closing the window quits the app and stops Chrome. To keep the music playing with the window
  closed, turn on background playback in Preferences (<kbd>Ctrl</kbd>+<kbd>,</kbd>); the
  system's media controls bring the window back.
- Preferences › General › Playback › Show what you are listening to on Discord puts the song
  playing on your Discord profile, as "Listening to Apple Music" with the title, the artist,
  the album and its cover, and the artist in the status under your name. It is off until you
  turn it on, and it needs the Discord desktop app running on the same computer (the web
  client cannot be reached). Discord shows it only while something plays: pausing stops the
  clock and, five seconds later, takes it off until the music plays again, as playback
  ending or the app quitting does.
- In a wide window a playlist's songs are a table, as the Songs page is: click an artist
  or an album there to open its page.
- Right-click a song, an album or a playlist (or press and hold it, or press
  <kbd>Menu</kbd>) for its menu: play it next or later, add it to a playlist or your
  favourites, open it on music.apple.com, and, for a song, **Go to Album** and **Go to
  Artist**. The song playing has the same menu: right-click the player bar, or use **More
  Options** in Now Playing; Up Next's songs have theirs too.
- <kbd>Ctrl</kbd>+<kbd>?</kbd> lists every keyboard shortcut.
- Because the sound comes from Chrome, the system's per-app volume controls list it as Chrome.

## Known limitations

- Google Chrome is required, and must be the real Chrome: Chromium lacks Widevine.
- Music videos play as audio only: the engine has no window to show them in.
- No offline listening or downloads: the app streams, as the web player does.
- Editing playlists is limited to adding songs; creating, renaming and reordering playlists
  happen in Apple's own apps.
- One Apple Account per build: a release install and a development build each have their own
  Chrome profile, cache and sign-in, so each is signed in and synced on its own.
- The account's name next to the avatar is read from Apple's web page and may stay
  "Signed In" when the page does not show it.

## Troubleshooting

- **"Google Chrome is needed to play Apple Music."** No Chrome was found. Install Google
  Chrome, or name its command in Preferences › Engine › Browser Program.
- **"The playback engine stopped."** Chrome exited or its page stopped answering. Click
  **Restart** in the message, or just play something.
- **"The keyring is not available, so starting would lose the Apple Music sign-in."** Chrome
  keeps the key that encrypts its sign-in in your keyring (GNOME Keyring or KWallet's Secret
  Service), and none answered on the session bus, so the app did not start Chrome: without
  that key, Chrome would delete the sign-in. Make sure the keyring is running and unlocked,
  then click **Retry**.
- **"Your Apple Music sign-in has expired."** Apple ended the session: click **Sign In** on
  that banner, or **Sign In Again** in the account menu at the bottom of the sidebar.
- **Discord shows nothing.** Check that the setting is on and that the Discord desktop app is
  running (Discord installed as a Flatpak or a Snap is found too), and that Discord's own
  Settings › Activity Privacy › Share your detected activities with others is on. Discord
  learns of a track when it starts or changes, so after starting Discord, give it until the
  next track.
- **Something else.** Run `music-sleeve --debug` from a terminal and look at what it logs (no
  password or tokens are logged). Preferences › Engine shows whether the engine is running;
  turning off **Run the browser hidden** shows Chrome's window at the next start.
- **Starting over.** Sign Out deletes the sign-in and the cache; the commands under "Where your
  data lives" remove everything.

## Privacy

- The app itself talks only to Apple: the music.apple.com page in its Chrome (and the Apple
  services that page uses), and artwork it downloads directly from Apple's servers. No
  telemetry, analytics or crash reports.
- With Discord presence on, and only then, the app also tells the Discord app running on your
  computer what is playing: the title, the artist, the album and the address of the album's
  cover on Apple's servers. Discord shows these on your profile, to the people who can see your
  activity there, and fetches the cover itself. Nothing of your account is sent: no Apple ID,
  no library, no playlists.
- Chrome, as the engine, also talks to Google the way any Chrome does, for example to update
  its components (the Widevine module among them) and for Safe Browsing. The app does not turn
  these off.
- The app never sees your Apple Account password: you sign in on Apple's own page, and the
  session lives in the app's own Chrome profile, apart from your everyday browser's.
- The app controls Chrome through a private pipe, with no network port that other programs
  could use. A developer can open a port with the `APPLE_MUSIC_DEBUG_PORT` environment
  variable, but while it is set any program on the computer can control the signed-in
  session: never set it for everyday use.

## Where your data lives, and how to remove it

| What | Where |
|---|---|
| The library snapshot, artwork, lyrics and cached pages | `~/.cache/apple-music/` |
| Chrome's profile, which holds your Apple sign-in | `~/.local/share/apple-music/chrome/` |
| Settings | GSettings schema `io.github.jackicus.MusicSleeve` |

(`~/.cache` and `~/.local/share` stand for `$XDG_CACHE_HOME` and `$XDG_DATA_HOME`.) The
development build uses `~/.local/share/apple-music/chrome-devel/` and
`~/.cache/apple-music-devel/` instead, and settings of its own for the account.

- **Sign Out** (in the account menu at the bottom of the sidebar) signs out of Apple Music,
  asking Apple to end the session too, stops Chrome and deletes both the Chrome profile and
  the cache.
- **Preferences › General › Cache › Clear** deletes only the cache.
- To remove everything by hand, quit the app first, then run:

  ```bash
  rm -r ~/.cache/apple-music ~/.local/share/apple-music
  gsettings reset-recursively io.github.jackicus.MusicSleeve
  ```
