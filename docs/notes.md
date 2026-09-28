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
its "Performance pass" phase.

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

## Chrome and the page

- A headless start takes about 10 s until the bridge answers: Chrome about 4 s, the page the
  rest.
- `Gio.InputStream.read_line_async` returns an empty line at the end of the stream, which cannot
  be told from a blank line; the stderr relay reads bytes instead.
- Apple's page is Svelte with hashed class names. Signed out, the sidebar footer holds
  `div.auth-content > button.signin`; the signed-in markup that `account_name()` reads is not
  verified.
- The web player's own addresses (read from its router, 2026-09-28): a library playlist is
  `https://music.apple.com/library/playlist/<p. id>`, a library album
  `https://music.apple.com/library/albums/<l. id>` (only the owner can open either; a library
  album's catalog page comes from `/v1/me/library/albums/<id>/catalog`); a catalog song
  `https://music.apple.com/<storefront>/song/<id>` redirects to its canonical address.
- Ratings (2026-09-28): `GET /v1/me/ratings/<type>s/<id>` answers 404 for an unrated item, the
  `?ids=` form `{data: []}`; a loved item is `{type: 'ratings', attributes: {value: 1}}`. The
  library's artist ids (`l.art_…`) and loose songs' stand-in albums (`l.alb_…`) are made up by
  the sync, so Apple answers nothing for them: no rating, link or queue entry.
- Chrome's own MPRIS player would be `org.mpris.MediaPlayer2.chromium.instance<pid>`; with
  `--disable-features=HardwareMediaKeyHandling`, `busctl --user list | grep -i mpris` shows the
  app's name and nothing for the engine's pid. `MediaSessionService` did not need disabling.

## MPRIS and GNOME Shell

- GNOME Shell's media section lists a player only while its `CanPlay` is true, and looks the app
  up as `<DesktopEntry>.desktop` in the Shell's own data directories: the development build in
  build/install shows its Identity but no icon; a system install shows the icon.
- `gdbus` spells an object path argument `"objectpath '/…'"`.
