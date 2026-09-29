---
paths:
  - "src/player.py"
  - "src/mpris.py"
  - "src/lyrics.py"
  - "src/player_bar.py"
  - "src/player_bar.blp"
  - "src/widgets/transport.py"
  - "src/widgets/now_playing.*"
  - "src/widgets/lyrics.py"
  - "src/widgets/queue.py"
  - "tests/test_player.py"
  - "tests/test_mpris.py"
  - "tests/test_transport.py"
  - "tests/test_lyrics.py"
---

# Playback: the Player, MPRIS, the bar and Now Playing

- `Player` (player.py) has no GTK. Its properties change from the engine's `event` signal, from
  one `now_playing()`/`queue()` read when the engine comes up, and from the lyrics fetched once
  per catalog song. Nothing polls MusicKit (sign-in's wait is the one poll). The bar, the sheet
  and MPRIS follow `notify::*`. Every view reads the same cleaned state, because the Player
  does the cleaning once:
  - `resting` is the state with MusicKit's `seeking` transient seen through; `active` and
    `stopped` derive from it, so a seek never flips the play button or the background timer.
  - A null `nowPlayingItemDidChange` clears the track only after `TRACK_GRACE_MS` (800) with
    no new item (`track_grace_ms`, 0 in synchronous tests): MusicKit sends one between
    queues, right before the next item. Positions reported meanwhile are ignored.
  - After a new item, MusicKit reports the previous item's position (and duration) once more:
    for `TRACK_HOLD` seconds a position further than `SEEK_JUMP` past where the item can be is
    dropped (`_plausible`); the state is still taken. The item's times change with the track
    under one `freeze_notify`, so a `notify::track` handler reads 0 and Apple's length.
  - The position is stamped only when it changes (MusicKit reports whole seconds), and
    `estimated_position()` runs on a second at most. The lyrics view follows the estimate.
  - A state or track event that arrives while `refresh()` awaits its answer outranks the
    answer, which then sets only the modes and the volume; the engine's `bridgeReset` event
    resets the Player as the engine going down does, then reads `now_playing()` again.
- Its commands are thin coroutines over the engine; the UI runs them through
  `app.player_command(coro, on_error=None)`, which toasts an EngineError. `play()` starts a
  `down` engine when signed in (`ensure_engine()`) and raises `not-signed-in` otherwise; see
  engine.md for the `starting` state. Play requests go out one at a time, the newest winning
  (a request superseded while it waits is dropped), and `pending` is True while one is with
  the engine (the play buttons show a spinner). `play(shuffle=None)` keeps the mode (a track
  row); a Play button passes False, a Shuffle button True. `previous()` restarts the item
  after `PREVIOUS_RESTART` seconds. A `mediaPlaybackError` is emitted as `error(sentence)`,
  the sentence `playback_error_text(code)` gives MusicKit's code; the app toasts it as it is.
- `player.stopped` means no item, or `none`, `stopped`, `ended` or `completed` (paused is not).
  MusicKit passes through `ended` and `stopped` between items, so anything acting on "stopped"
  waits a moment (background playback waits 10 s before quitting).
- The bar and the sheet share their transport pieces (`widgets/transport.py`: `PlayButton`,
  `TrackTitles`, `SeekControl` over a GTK-free `SeekGuard`, `ModeControl`, `VolumeControl`
  over a `Coalescer`, `HeartControl`, `RemoteCover`): change them there, and test their
  decisions in tests/test_transport.py with stand-in buttons. The bar announces each new
  item through the window, once per item, and with nothing playing hides its seek slider
  and heart. The sheet's layout follows three of its own properties, set by window.blp's
  breakpoints: `wide` (900sp and up: the item beside the tabs), `compact` (600sp and under:
  a smaller cover) and `short` (a window too low for the cover over the titles: a 96 px
  cover beside them). Only the last matching breakpoint applies, so each width band has a
  short variant repeating its setters; check every size with
  `scripts/screenshot.py --demo --now-playing lyrics|queue --size WxH` and the log for
  libadwaita's "exceeds … height" warning. Up Next is a `Gtk.SliceListModel` of the queue
  from the entry playing on; the lyrics list is the user's while they scroll it (wheel,
  touch, scrollbar) or have the focus on a line.
- MPRIS: the app owns `org.mpris.MediaPlayer2.<app id>`; Chrome's own player is disabled by its
  launch flags. The object is registered with
  `Gio.DBusConnection.register_object_with_closures2` (the older call is deprecated since GLib
  2.84, the declared minimum); GLib answers Get, GetAll, Set and introspection from the node
  info, and each property has a getter of its own (`PLAYER_GETTERS`), so Get builds only what
  is asked. `bus_acquired` comes before `name_acquired`; `name_lost` with no connection means
  no bus at all, and the app runs on.
- PropertiesChanged is emitted by hand, `(sa{sv}as)`, with only the keys that differ from what
  was last sent. Position is never in it (the spec's annotation): clients read it, and
  `Player.estimated_position()` runs it on between events. Seeked follows the app's own seeks
  and any position jump from where a client would extrapolate it (while the published status
  is Playing, so a stall behind that status ends with a Seeked). `mpris:trackid` carries the
  queue index (`/track/<index>/<id>`): the same song twice in a row is two tracks. Stop
  pauses and seeks to 0, keeping the item (Stopped until the music plays again; CanPlay stays
  true, so the Shell keeps the player). SetPosition drops a stale trackid or a position outside
  the track; Seek clamps to 0 and goes to the next item past the end; PlayPause with nothing to
  play answers NotSupported. The artwork file is found through `remote.fetch_remote`, never a
  stat on the main loop.
- GNOME Shell lists a player while `CanPlay` is true (so "Not Playing" shows nothing) and finds
  its icon through `<DesktopEntry>.desktop` in the Shell's data directories: a system install
  shows the icon, the dev build in build/install shows only the Identity.
- Tests drive the Player and Mpris with stand-in apps and engines (tests/test_player.py,
  tests/test_mpris.py), with no GTK. `patched_clocks()` (test_player.py) puts `time.monotonic`
  under the test's control for both; no `asyncio.sleep(> 0)` inside it. The `FakeEngine` has
  per-command gates (`engine.gate(name)`) to hold an answer back. test_mpris.py's
  `SEQUENCES` table replays the event sequences seen live through a Player and the service
  together: add a sequence there when a new one is recorded. One test runs the service on a
  dbus-daemon of its own and talks to it through GDBus (never `Gio.TestDBus`, which is for a
  process of its own). Checks on the session bus are in the `live-engine-check` skill.
