---
paths:
  - "src/**/*.blp"
  - "src/window.py"
  - "src/pages/**"
  - "src/widgets/**"
  - "src/dialogs/**"
  - "src/player_bar.py"
  - "src/main.py"
---

# GTK 4.22, libadwaita 1.9 and PyGObject 3.56: behaviour this code depends on

Each of these was checked against the toolkit (or found the hard way). The sidebar's are in
`sidebar.md`; measurements are in docs/notes.md.

## Lists and grids

- A `Gtk.GridView` recycles only as the direct scrollable child of a `Gtk.ScrolledWindow`
  (hence the grid page's title is an overlay over the grid's top padding). After a sort change
  it follows its old top item: `scroll_to(0, …)`.
- A `Gtk.ListView` gives each row its minimum height. Tab never reaches focusable widgets in a
  `header-factory` header, only in items, so a page's hero is its list's first item. A
  `Gtk.FlattenListModel` is a `Gtk.SectionModel` (one section per child model).
- CSS padding on a `Gtk.ListView` scrolls with its rows and lies outside the adjustment's page:
  when geometry is computed from the list, pad with margins around it instead. A `scroll_to`
  asked from inside the adjustment's `changed` handler is lost: defer it to an idle.
- `Gtk.ColumnView.sort_by_column()` leaves the previous column's sort arrow; use it for the
  initial order only, or call `sort_by_column(None, …)` first.
- A `Gtk.StringFilter` over `Gtk.ClosureExpression.new(str, lambda item: item.search_key, None)`
  filters tens of thousands of items in one pass (fold the text first, `ignore-case: false`).

## Layout and widgets

- A Python `do_measure` on a widget with a layout manager (an `Adw.Bin`) is never called: set
  the layout manager to None and implement both `do_measure` and `do_size_allocate`.
- A `Gtk.Overlay` gives an overlay child its natural size, and a `Gtk.Picture`'s natural size is
  its image's: give the picture an overlay whose main child is a sized placeholder.
- A `Gtk.Stack` will not switch to a child that is not visible. `Gtk.Inscription`'s CSS name is
  `label`.
- Several `Adw.Breakpoint`s: only the last matching one applies, so the window's narrower one
  (600sp) repeats the wider one's (640sp) setters; the `min-width: 900sp` one is separate. A
  navigation page takes no breakpoints: put an `Adw.BreakpointBin` inside it.
- libadwaita's `.card` sets its own `color`, so a card placeholder on a coloured background
  needs `color: inherit`; `--card-bg-color` is white in the light theme.
- `Adw.BottomSheet`: the bottom bar is laid over the content (window.blp binds the content's
  `margin-bottom` to `bottom-bar-height`, which is 0 while the sheet is open). With `can-open`
  the bar is wrapped in a focusable `Gtk.Button`, which the player bar names. The sheet gets its
  child's natural height (clamped to the window), so a full-height sheet asks for a tall one;
  the child is laid out while closed and keeps that size briefly after opening, so wait for the
  adjustment's `changed`. Opening focuses its first focusable widget; closing restores focus.
- `Adw.NavigationView` pops on Escape, Back, Alt+Left (local shortcuts: only with the focus
  inside it) and the mouse back button; a push moves the focus into the new page. `win.back`
  covers the rest. A push maps, unmaps and maps the pushed page again: stop a page's requests
  in `do_hidden` (after it has really gone), not in `do_unmap`.
- An `Adw.Dialog` over a window that is neither maximized nor tiled is a separate
  `Gtk.Window` transient for it: its widgets find no `app.*` actions (Preferences inserts the
  `app` group itself), `get_active_window()` stays the main window, `get_visible_dialog()` does
  not see it (use `Gtk.Window.list_toplevels()`), and a screenshot of the window misses it.
- An actionable widget's sensitivity is its action's: `set_sensitive(False)` on a row with an
  `action-name` is undone.
- A callback GtkBuilder connected (`clicked => $on_x()`) is not found by
  `handler_block_by_func`: use a flag. `Gtk.SearchEntry.grab_focus()` focuses its inner
  `Gtk.Text`. An `Adw.ToggleGroup`'s `active-name` is the toggle's `name`.

## Menus, drags, clipboard

- Context menu controllers go on the view, not the recycled tiles, in the capture phase and
  claimed, so the item's own click gesture never sees them. A popover can be parented to any
  widget with a layout manager; unparent it from an idle after `closed`, because the chosen
  item's action runs after the popover closes and finds `win.*` through its parent.
- The drag type is `actions.TrackRef`, offered with `Gdk.ContentProvider.new_for_value(ref)`.
  `Gdk.Clipboard.set(text)` works from Python (it is `set_value`).

## PyGObject and asyncio

- Python 3.14 deprecates `asyncio.set_event_loop_policy` (removal in 3.16), but it is still how
  PyGObject 3.56 puts asyncio on the GLib loop; `main.use_glib_event_loop()` filters the warning.
  `Gio.Application.run` marks the GLib loop as asyncio's running loop only with that policy set.
- Gio's `*_async` methods (`Gio.Subprocess.wait_async`, `Gio.InputStream.read_bytes_async`,
  `Gio.File.load_contents_async`) are awaitable on this loop.
