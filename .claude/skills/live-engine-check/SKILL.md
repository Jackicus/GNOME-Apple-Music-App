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

1. Ask which build holds the sign-in if the user has not said. The release build uses the
   Chrome profile `$XDG_DATA_HOME/apple-music/chrome` and the cache
   `$XDG_CACHE_HOME/apple-music`; the .Devel build (what `scripts/run.sh` runs) `chrome-devel`
   and `apple-music-devel`. Both read one `signed-in` setting, so it can say yes for a profile
   that is not signed in. Before the app runs, `scripts/am.py status` (release) or
   `scripts/am.py --devel status` (.Devel) says whether a Chrome holds that profile and, when
   none does, starts a headless Chrome of its own on it for the command, reports whether
   MusicKit is authorized, and stops it.
2. Start the app with a DevTools port for this check only and its log in the scratch directory:
   `APPLE_MUSIC_DEBUG_PORT=<free port> scripts/run.sh --debug 2> <scratch>/app.log` (the .Devel
   build; a release install is `apple-music --debug`). It starts the engine itself when signed
   in and `engine-autostart` is on. The engine has no port otherwise; while this one is open
   any local program can drive the signed-in session, so set it on that command line alone,
   never in a shell profile. The log warns about it at every engine start.
3. Drive it without synthesised input:
   - app actions from a shell: `gapplication action io.github.jackicus.AppleMusic.Devel sync`
     (also `play-pause`, `next`, `previous`, `now-playing`, `quit`);
   - the page's state through the port: `scripts/am.py --attach <port> now-playing`,
     `scripts/am.py --attach <port> events` (bridge events until Ctrl+C),
     `scripts/am.py --attach <port> eval '<read-only JS>'`. Without `--attach`, am.py refuses
     while the app's Chrome holds the profile;
   - screenshots of the real window: `scripts/screenshot.py <scratch>/shot.png` without
     `--demo`, into the scratch directory only. It reads the release build's cache; for the
     .Devel build's, prefix `APPLE_MUSIC_CACHE=$XDG_CACHE_HOME/apple-music-devel`.
4. MPRIS on the session bus (the .Devel name shown; drop `.Devel` for a release build):
   - `gdbus introspect --session --dest org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel --object-path /org/mpris/MediaPlayer2`
   - `gdbus monitor --session --dest org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel`
     shows PropertiesChanged and Seeked;
   - before playing, turn the engine down:
     `gdbus call --session --dest org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel --object-path /org/mpris/MediaPlayer2 --method org.freedesktop.DBus.Properties.Set org.mpris.MediaPlayer2.Player Volume "<0.2>"`;
   - `busctl --user list | grep -i mpris` lists the app's name and no Chrome instance for the
     engine's pid (the user's own Chrome may have one: compare the pid).
5. Finish: `gapplication action io.github.jackicus.AppleMusic.Devel quit` (it stops Chrome),
   then check that `pgrep -af 'user-data-dir=.*apple-music'` finds nothing and that
   `ss -ltn | grep <port>` shows the port closed. Report each check as passed or failed with
   where its evidence is, and confirm the account is as it was.
