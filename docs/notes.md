# Lab notes

Findings and measurements that explain the code but are not rules. Each was true when it was
written down (September 2026: GTK 4.22, libadwaita 1.9, PyGObject 3.56, Python 3.14, Chrome
154, on the development machine); re-check one before building on it, and date what you add.

The measurements behind the list and model code live in the module docstrings, next to the
code they justify: `pages/songs.py` (GTK's sorters and filters over Python objects, row
rebuilding), `pages/grid.py` (expression sorters against a custom sorter), `library.py`
(`raw_property`'s and `TrackRecord`'s memory, `SongOrder`'s keys, `apply_diff`, `paused_gc`,
`yield_to_frames`), and `widgets/artwork.py` (texture sizes, the cache budget). The
performance pass that produced most of them is written up in `docs/history/build-plan.md`, in
its "Performance pass" section.

## The development machine

- A bare `Adw.ApplicationWindow` paints its first frame about 760 ms after its process starts:
  loading the desktop's icon theme takes about 200 ms of GTK's start, and `present()` waits about
  330 ms for the compositor's first configure. Its RSS is about 158 MB, 105 MB of it
  file-backed (the NVIDIA driver maps about 50 MB). No change to the app removes this floor;
  the startup target of content within a second is measured against it.
- Timings vary by 100 ms or more between runs with the usual desktop load: `bench.py --runs 5`
  and medians.
- The monitor runs at 240 Hz, and an unfocused window may get 60 frames a second, so
  `scroll_test.py` reports the app's own work per frame. The compositor sends no frames to a
  window it does not show, so a hidden bench or scroll-test window stalls.
- A tiling extension resizes windows that are resizable when mapped; the scripts make theirs
  non-resizable (`scripts/harness.py`).
- No input can be synthesised on this Wayland desktop: live runs are driven with
  `gapplication action`, `scripts/am.py` and D-Bus, and `a11y_check.py` dispatches keys through
  GTK's own controllers.

## GTK and libadwaita, measured

- AdwSidebar at about 150 playlist rows (162 playlists and 7 folders in one test library):
  splicing them in took about 24 ms, then one 15-17 ms frame; scrolling the list at 2,000-4,000
  px/s cost about 0.5 ms of work a frame. A reload with the same shapes keeps the items and costs
  about 5 ms. Its rows are real widgets, not recycled: a few thousand playlists would need a
  lighter path.
- A `Gtk.GridView` of 2,000 items bound about 52,000 times scrolling top to bottom; a wrapping
  `Gtk.Label` in each tile cost about 7 ms a frame at 4,000 px/s against about 1 ms for a
  `Gtk.Inscription`. An `Adw.Avatar` is about 18 KB and a third of a tile's construction time.
- A full garbage collection over a loaded 3,000-album, 40,000-song library took about 50 ms,
  hence `paused_gc`. A chunked load that yielded with `asyncio.sleep(0)` painted no frames; with
  `yield_to_frames()` it painted as it went.
- Nested scrolling needs no code: a scrolled window handles a scroll event only along an axis it
  can scroll, so a vertical wheel over a shelf (`vscrollbar-policy: never`) scrolls the page and
  a horizontal one the shelf. A diagonal touchpad swipe stays with the shelf; libinput locks a
  two-finger scroll to one axis, and once the page has started a touchpad scroll it keeps the
  gesture even over a shelf. In a test, emit `scroll` on the bubble-phase
  `Gtk.EventControllerScroll` (the capture-phase one only continues a scroll in progress).
- `Adw.BottomSheet` clamps its sheet to the window less a top margin of about 30 px, and
  Escape is a local `Gtk.ShortcutController` on the sheet's inner widget, which the open sheet's
  focus satisfies.
- `Gio.Settings.bind_with_mapping` exists in PyGObject 3.56, but its get-mapping closure got
  the GValue as a plain int it could not set, and GLib aborted on the rejected default; the
  refresh-interval row in Preferences is bound by hand instead.
- Opening a sidebar row's context menu from a script worked by emitting the row's long-press
  gesture's `pressed(x, y)`; `sidebar.activate_action('menu.popup')` did nothing. A
  `Gtk.PopoverMenu` submenu is named by its label: `visible-submenu` 'Add to Playlist' opens it.
- A stand-in texture of another size (`ArtworkSlot` showing `get_any()` while the size wanted
  decodes) changes the picture's size twice, and each time lays the list out again. The worst
  case, Songs scrolled at 2,000 px/s with every row's thumbnail cached only at the tiles'
  160 px (headless, 40,000 songs, 2026-09-29): 3.1-3.5 ms of work a frame against 2.9 ms
  without stand-ins, the longest frame the same (5.2-5.6 ms). Kept: the rows show the cover at
  once instead of the placeholder.
- Hidden root pages keep their list and grid widgets (the window keeps each fixed
  destination's root page once visited, and the last eight playlist and folder pages).
  Letting the six hidden views' item widgets go while they are hidden, and rebuilding them on a
  revisit, would free about 6.3 MB on the 3,000-album, 40,000-song library (headless,
  2026-09-29): under the 10 MB it had to save to be worth a rebuild on every revisit, so it is
  not done.
- A focused entry destroyed with its page and GTK's Wayland text input (2026-10-01, GTK
  4.22.5, mutter 50.4, headless). The first `Gtk.Text` to take the focus in a process binds
  the `zwp_text_input_v3` object, and the compositor answers at once with `enter`. If that
  entry is destroyed before the main loop has dispatched the answer, GTK still holds the dead
  context and the `enter` crashes the process in `gtk_widget_get_display`
  (`text_input_enter` → `enable`), which is what a page test's synchronous teardown did
  (tests/page_harness.py): focus the entry, tear the page down, pump. Moving the focus off
  first (`set_focus(None)`) alone does not help; one main-loop turn before the teardown does,
  and a later focus (the text input bound already) torn down the same way is harmless. With
  the turn, every teardown was clean: the Search page's entry and the Songs filter focused
  and typed into, the page dropped and freed, then the keyboard moved to another window and
  back (the compositor's `leave` and `enter`), with the protocol log showing `enable` at the
  focus, `disable` at the teardown and nothing dangling. The app never destroys a page in
  the same loop iteration as a first focus: sign-out's `forget_account_pages()` (account.py)
  comes after the engine's stop, the wipe and the library's reload; eviction past
  `ROOT_LIMIT` and a sync's stale playlists drop only pages not shown; and a drop moves the
  focus into the new root page by itself (libadwaita's replace). Driven on the demo library
  over scripts/harness.py with real keys (scripts/remote_keys.py), with and without
  animations, the window active and not: no crash in any of 20-odd rounds, so nothing was
  changed in the window (issue 254).

## Memory

The target (`.claude/rules/performance.md`, checked by `scripts/bench.py`): anonymous memory
under 250 MB after every page has been browsed twice on the 3,000-album, 40,000-song library,
and pages opened and closed leaving at most 5 MB behind. RSS is not the target: it adds what
the libraries and the GL driver map, which the app cannot shrink (a bare `Adw.ApplicationWindow`
is 158 MB on the desktop, 105 of it file-backed; 192 MB headless, 64 of it anonymous).

- Measured headless, 2026-09-29: anonymous memory 212-215 MB after every page browsed twice
  (RSS about 367 MB); the pages step (20 albums, 5 artists, 5 See All opened and popped)
  leaves -1 to +1 MB after a warm-up round, and frees every page. Its first round grows RSS by
  about 6 MB once: the artwork's texture cache filling to its budget.
- What brought it there (2026-09-28/29): each track of library.json kept as a tuple
  (`TrackRecord`), the parsed library 47.5 MB to 32.4; the Songs orders keeping ranks in
  arrays instead of collation keys, 22.3 MB to 3.1; a reload keeping one copy of each
  unchanged dict, not two, 30 MB less after a sync; the texture cache sized by the scale factor
  (8 MB at 1x, not 32), anonymous memory after scrolling all of Albums 156 MB to 137; pages,
  shelves and dialogs freed once dropped (weak connections).
- Not done, about 21 MB more: a lazy Songs model, a `Gio.ListModel` over the track records
  that wraps a `Track` only for the rows GTK asks for, instead of 40,000 Tracks and three
  stores. It changes the type of `library.songs` and moves Search's library song filter into
  Python; with it, the per-Track `search_key` could go too.

## Chrome and the page

- A headless start takes about 10 s until the bridge answers: Chrome about 4 s, the page the
  rest.
- `Gio.InputStream.read_line_async` returns an empty line at the end of the stream, which cannot
  be told from a blank line; the stderr relay reads bytes instead.
- Apple's page is Svelte with hashed class names. Signed out, the sidebar footer holds
  `div.auth-content > button.signin`. Signed in, the account menu names the user in
  `.account-menu .user__name` (seen live on 2026-09-28), which `account_name()` reads first,
  once the page has drawn the menu, some seconds after authorization.
- The web player's own addresses (read from its router, 2026-09-28): a library playlist is
  `https://music.apple.com/library/playlist/<p. id>`, a library album
  `https://music.apple.com/library/albums/<l. id>` (only the owner can open either; a library
  album's catalog page comes from `/v1/me/library/albums/<id>/catalog`); a catalog song
  `https://music.apple.com/<storefront>/song/<id>` redirects to its canonical address.
- Ratings (2026-09-28): `GET /v1/me/ratings/<type>s/<id>` answers 404 for an unrated item, the
  `?ids=` form `{data: []}`; a loved item is `{type: 'ratings', attributes: {value: 1}}`. The
  library's artist ids (`l.art_…`) and loose songs' stand-in albums (`l.alb_…`) are made up by
  the sync, so Apple answers nothing for them: no rating, link or queue entry.
- The DevTools pipe (2026-09-28): asyncio's pipe transports (`connect_read_pipe`,
  `connect_write_pipe`) work on gi.events' loop. Chrome gets its ends through
  `Gio.SubprocessLauncher.take_fd()`, and `launcher.close()` after the spawn is what closes this
  process's copies: without it, Chrome's exit never read as the end of the pipe. That closing
  the pipe also makes Chrome quit, as Puppeteer relies on, is not confirmed on Chrome 154; the
  engine sends `Browser.close` and signals anyway.
- A cancelled `Gio.Subprocess.wait_async()` raised `GLib.Error` (cancelled), not
  `CancelledError`, hence `engine._process_exit()`.
- Chrome rewrites its `/proc/<pid>/cmdline` into one space-joined string, its helpers too (with
  `--type=`), so splitting it on NULs never finds `--user-data-dir=<profile>` as an argument,
  and a prefix match takes `…/chrome-devel` for `…/chrome`. `<profile>/SingletonLock` is a
  symlink to `<hostname>-<pid>` of the browser process holding the profile.
- `setpriv --pdeathsig TERM --` execs Chrome, so the pid and command line the engine sees are
  Chrome's. The parent-death signal follows the thread that spawned it, which is the main
  thread; a grandchild spawned this way exited within a second of its parent's SIGKILL.
- Chrome's own MPRIS player would be `org.mpris.MediaPlayer2.chromium.instance<pid>`; with
  `--disable-features=HardwareMediaKeyHandling`, `busctl --user list | grep -i mpris` shows the
  app's name and nothing for the engine's pid. `MediaSessionService` did not need disabling.
- Chrome without its keyring drops the encrypted cookies (2026-09-29). Chrome 154 encrypts its
  cookies with a key kept in the Secret Service (`org.freedesktop.secrets` on its session bus,
  through the desktop portal's secret provider, which `Local State` records as
  `os_crypt.portal.prev_init_success`). Started inside `scripts/headless.sh`'s private D-Bus
  session, it found the name activatable (the system's gnome-keyring service file) but the
  activation timed out after dbus-daemon's 25 s, so Chrome sat on `about:blank` that long, ran
  on a fallback key, and deleted the cookies it could not decrypt: the .Devel profile came up
  signed out and stayed so with the keyring back (one `geo` cookie left, `prev_init_success`
  flipped to false, `Failed to log in to GCM … wrong_secret` in its log). Hence
  `APPLE_MUSIC_HOST_SESSION_BUS` and the engine's refusal (`no-keyring`) before a start on
  such a profile. On this machine a private `dbus-daemon --session` lists 65 activatable names,
  `org.freedesktop.secrets` among them, and `StartServiceByName` for it hangs: an activatable
  name is no sign of a working keyring, so the check asks for the activation and waits 5 s.
  dbus-daemon does not end a child it activated when the activation fails or the daemon exits.

## The sync

- What Apple's library API offers a sync that wants only the changes (2026-09-30, from the
  documented endpoints and the answers the fixtures mirror): nothing general. The library
  listings (`/v1/me/library/songs`, `albums`, `playlists`, `music-videos`) take `limit`,
  `offset`, `include`, `extend` and `fields`; no `filter` by date, no `sort`, no changes
  feed or cursor. `meta.total` counts the listing, which an addition plus a removal, or an
  edit, leaves as it was, so it cannot stand for "unchanged". A conditional read (`ETag`,
  `If-None-Match`) is out of reach whatever Apple sends: the bridge's `api()` is MusicKit's
  `music()`, which hands back the parsed body alone, and the app never sees a header.
  `/v1/me/library/recently-added` covers additions only. What a listing does carry, per
  library playlist, is `lastModifiedDate` (and, as seen on the real answers, `trackCount`):
  enough to keep a playlist's tracks while both hold, which is what the sync does.
- The requests of a full pass against an invented library shaped like the one in #193 (793
  songs, 34 playlists holding 1,053 tracks, 5 folders), counted through the tests' fake
  engine with 0.3 s a request (scratch script, not in the repo): 59 requests, 36 of them the
  playlists' tracks, whatever changed, before; after, a refresh with nothing changed makes
  23 (no tracks read) and one with one playlist changed 24, the first sync still 59 and a
  listing without dates still 59. With the fake's latency: 8.5 s to 5.8 s and 6.1 s (the
  fake reads the songs' pages and the tracks a few at a time, as the engine does).
  Measured live on the maintainer's library (#192, #194) a full pass was 33 s and a quick
  pass (no tracks, folders, videos or stations) 18.5 s, so the tracks are at most 14.5 s of
  the 33: the songs listing, which nothing above can narrow, is the rest. The log's
  `sync done in N s (engine …, songs …, playlists …)` line now gives each phase's seconds
  (`phase_text()`), for the next live measurement.
- Live, on the maintainer's library (2026-10-02, the .Devel build headless): the first full
  pass with the stamps took 21 s (songs 6.7, playlists 6.6, shelves 4.4); the next, with
  nothing changed, 8 s, all 34 playlists kept (playlists 0.5 s). On a throwaway playlist of
  three songs, each edit moved its `lastModifiedDate` (to the second) within 5 s: a reorder
  (a `PUT` of its tracks in a new order), a removal (`DELETE` of one track) and an addition
  (`POST`); after the reorder the next full pass read that playlist alone and kept the new
  order. The edits went through Apple's library API, as music.apple.com's own do; an edit
  on a phone reaches the same cloud library, but was not tried. A single playlist's answer
  carried no `trackCount`, so the date alone did the work.

## MPRIS and GNOME Shell

- GNOME Shell's media section lists a player only while its `CanPlay` is true, and looks the app
  up as `<DesktopEntry>.desktop` in the Shell's own data directories: the development build in
  build/install shows its Identity but no icon; a system install shows the icon.
- `gdbus` spells an object path argument `"objectpath '/…'"`.
