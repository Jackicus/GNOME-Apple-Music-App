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
  - "tests/test_lyrics.py"
---

# Playback: the Player, MPRIS, the bar and Now Playing

- `Player` (player.py) has no GTK. Its properties change from the engine's `event` signal, from
  one `now_playing()`/`queue()` read when the engine comes up, and from the lyrics fetched once
  per catalog song. Nothing polls MusicKit (sign-in's wait is the one poll). The bar, the sheet
  and MPRIS follow `notify::*`.
- Its commands are thin coroutines over the engine; the UI runs them through
  `app.player_command(coro)`, which toasts an EngineError. `play()` starts a `down` engine when
  signed in (`ensure_engine()`) and raises `not-signed-in` otherwise; see engine.md for the
  `starting` state.
- `player.stopped` means no item, or `none`, `stopped`, `ended` or `completed` (paused is not).
  MusicKit passes through `ended` and `stopped` between items, so anything acting on "stopped"
  waits a moment (background playback waits 10 s before quitting).
- The bar and the sheet share their transport pieces (`widgets/transport.py`: `PlayButton`,
  `SeekControl`, `ModeControl`, `HeartControl`, `RemoteCover`, `run_command`): change them there.
  The bar announces each new item through the window, once per item.
- MPRIS: the app owns `org.mpris.MediaPlayer2.<app id>`; Chrome's own player is disabled by its
  launch flags. The object is registered with
  `Gio.DBusConnection.register_object_with_closures2` (the older call is deprecated since GLib
  2.84, the declared minimum); GLib answers Get, GetAll, Set and introspection from the node
  info. `bus_acquired` comes before `name_acquired`; `name_lost` with no connection means no bus
  at all, and the app runs on.
- PropertiesChanged is emitted by hand, `(sa{sv}as)`, with only the keys that differ from what
  was last sent. Position is never in it (the spec's annotation): clients read it, and
  `Player.estimated_position()` runs it on between events. Seeked follows the app's own seeks
  and any position jump.
- GNOME Shell lists a player while `CanPlay` is true (so "Not Playing" shows nothing) and finds
  its icon through `<DesktopEntry>.desktop` in the Shell's data directories: a system install
  shows the icon, the dev build in build/install shows only the Identity.
- Tests drive the Player and Mpris with stand-in apps and engines (tests/test_player.py,
  tests/test_mpris.py), with no bus and no GTK. Checks on the session bus are in the
  `live-engine-check` skill.
