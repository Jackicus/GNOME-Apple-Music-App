---
name: live-engine-check
description: Check the app against the real Chrome engine and a signed-in Apple Music account, read-only by default - engine status, a sync, brief playback, MPRIS, the online pages - leaving nothing in the repository and nothing changed in the account. Only when the user asks for a live check.
disable-model-invocation: true
argument-hint: "[what to check]"
---

Live check: $ARGUMENTS

Ground rules:
- Read-only. No sign-in or sign-out, no Clear Cache, no writes to the account (love, add to
  library, add to a playlist) unless the user names exactly which, and then put it back.
- Everything with real data stays outside the repository: logs, screenshots and notes go to a
  scratch directory, and a report that will be shared names no titles, names or IDs.
- Keep audio short and quiet.

1. Ask which build holds the sign-in if the user has not said. The release build uses
   `$XDG_DATA_HOME/apple-music/chrome` and port 9228; the .Devel build `chrome-devel` and 9229.
   `scripts/am.py status` (release) or `APPLE_MUSIC_PORT=9229
   APPLE_MUSIC_PROFILE=$XDG_DATA_HOME/apple-music/chrome-devel scripts/am.py status` (.Devel)
   starts nothing: it says whether that profile's Chrome runs and, when it does, whether
   MusicKit is authorized.
2. Start the app with logs to the scratch directory: `scripts/run.sh --debug` (the .Devel
   build; it starts the engine itself when signed in and `engine-autostart` is on).
3. Drive it without synthesised input:
   - app actions from a shell: `gapplication action io.github.jackicus.AppleMusic.Devel sync`
     (also `play-pause`, `next`, `previous`, `now-playing`, `quit`);
   - the page's state: `scripts/am.py now-playing`, `scripts/am.py events` (bridge events until
     Ctrl+C), `scripts/am.py eval '<read-only JS>'`, with the same port and profile variables as
     in step 1;
   - screenshots of the real window: `scripts/screenshot.py <scratch>/shot.png` without
     `--demo`, into the scratch directory only.
4. MPRIS on the session bus (the .Devel name shown; drop `.Devel` for a release build):
   - `gdbus introspect --session --dest org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel --object-path /org/mpris/MediaPlayer2`
   - `gdbus monitor --session --dest org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel`
     shows PropertiesChanged and Seeked;
   - before playing, turn the engine down:
     `gdbus call --session --dest org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel --object-path /org/mpris/MediaPlayer2 --method org.freedesktop.DBus.Properties.Set org.mpris.MediaPlayer2.Player Volume "<0.2>"`;
   - `busctl --user list | grep -i mpris` lists the app's name and no Chrome instance for the
     engine's pid (the user's own Chrome may have one: compare the pid).
5. Finish: `gapplication action io.github.jackicus.AppleMusic.Devel quit` (it stops Chrome),
   then check that `pgrep -af 'user-data-dir=.*apple-music'` finds nothing. Report each check
   as passed or failed with where its evidence is, and confirm the account is as it was.
