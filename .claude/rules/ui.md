---
paths:
  - "src/**/*.blp"
  - "src/window.py"
  - "src/pages/**"
  - "src/widgets/**"
  - "src/dialogs/**"
  - "src/player_bar.py"
  - "src/actions.py"
  - "src/sidebar.py"
  - "src/sidebar_view.py"
  - "src/sections.py"
  - "src/shortcuts.py"
  - "src/keyboard.py"
  - "src/style.css"
  - "src/icons/**"
---

# Window, pages, widgets and dialogs

Platform behaviour worth knowing is in `gtk-notes.md`; list and artwork performance in
`performance.md`.

## Templates and widget lifetimes

- The app's name in a user-visible string is "Music Sleeve". "Apple Music" names the service
  ("Sign in to Apple Music", "Search Apple Music"), never this app (`.claude/rules/packaging.md`).
- Blueprint 0.22: `template $AppleMusicName: Parent { … }`, `styles ["flat"]`, handlers
  `clicked => $on_clicked();`, bindings `label: bind item.title;`,
  `Adw.Breakpoint { condition ("max-width: 640sp") setters { … } }`. A custom widget used in a
  template needs its Python class imported first (window.py imports PlayerBar and
  NowPlayingSheet with `# noqa: F401` for this). Each `.blp` compiles to a `.ui` of the same bare
  name, flat in build/src. Small widgets built entirely in code are fine (LyricsView, QueueView).
- A widget that can be dropped (a pushed page, an evicted root page, a Shelf, a dialog, a row)
  never connects a child's or an owned object's signal (a factory, an adjustment, a controller)
  to its own bound method, and declares no `=> $handler()` in its `.blp`: the reference cycle
  runs through C and the widget is never freed. Connect in `__init__` with
  `widgets.util.connect_weak(obj, signal, self._method)`, or `weak_method()` for other callbacks
  a child holds (`FlowBox.bind_model`'s). Handlers on the widget itself (`self.connect(...)`)
  and vfuncs (`do_map`) are fine. `Gtk.Template.Callback` is for widgets that live as long as
  the window.
- Follow a widget's lifetime with a GObject weak reference (`obj.weak_ref()`), not `weakref`:
  PyGObject 3.56 drops a widget's Python wrapper while the widget lives and makes a new one.
  tests/test_page_lifetime.py checks each droppable class; add new ones to it.

## Pages

- A destination's root page is an `Adw.NavigationPage` with its own `Adw.ToolbarView` and
  `Adw.HeaderBar` (`show-title: false`; the `title-1` in the content is the title), registered in
  `pages.PAGES`. Its module is imported by its factory, never at the top of window.py or main.py
  (startup time). Playlist and folder root pages are kept only for the last few shown
  (`window.ROOT_LIMIT`). Sign-out forgets the pages that show the account's things
  (`Window.forget_account_pages()`: the pushed pages, New, Made for You, Search and every
  playlist and folder root), to be built again on the next visit.
- Pages listen to `app.library` (and anything else that outlives them) only while mapped:
  they declare the handlers once, in `__init__`, on a `widgets.util.MappedHandlers(self)`,
  which connects them (weakly) on map and disconnects them on unmap; `do_map` catches up with
  what changed while hidden. A page that must follow while hidden says why in a comment
  (SongsPage does). Pages reach the Application through `pages.app()`.
- The album, playlist and artist pages follow the Item they show (`notify` for the hero,
  `groups-changed` for the tracks), pushed pages too. An Item that came without tracks is
  fetched once per page (`detail.should_fetch()`: an answer without tracks shows "No Songs",
  never a second request), and the fetch is cancelled when the page is hidden (`do_hidden`).
- Root or pushed, a page's header bar shows its title exactly when the page's own `title-1`
  is out of view (a spinner or status page instead, or scrolled away):
  `widgets.util.HeaderTitle`, which looks again after each paint that follows a move.
- The album and artist pages' own menu is a More Options `Gtk.MenuButton` whose model
  `window.item_actions.menu_for(item)` makes as it opens (`set_create_popup_func`); long
  notes show three lines and More (`pages.show_notes()`); an album's artist is a link when
  the library has them (`detail.resolve_artist()`).
- New, Made for You and a search category are `ShelvesPage`s over the engine's answers. They,
  Search and the album and artist pages show what the engine's failure means through one
  `widgets.engine_status.EngineStatus` (no widget; tests/test_engine_status.py): the spinner
  while it starts, Sign In when signed out, Start Engine (through `app.start_engine()`, whose
  failure the app reports), Try Again, and "Not Available in the Demo" with no button (the
  demo engine answers `engine-down`, but for the artist pages scripts/demo_library.py invents).
  A state the page shows is not toasted too.
- A page of shelves is a vertical `Gtk.Box` of a handful of `AppleMusicShelf`, each a horizontal
  `Gtk.ListView` in its own scrolled window, placed by a `widgets.shelf.ShelfColumn`: a shelf
  shown again keeps its widget (moved into place), new ones are bound a frame apart after
  `FIRST_SHELVES`, and leftover widgets are hidden for later. A row shows at most `ROW_LIMIT`
  tiles; See All (the whole shelf, as a grid) and the paging arrows show only when the row does
  not show everything, the arrows only on a shelf wider than `ARROWS_MIN_WIDTH` (400sp, which
  the shelf measures itself: shelves are made at run time, out of a page breakpoint's reach),
  so the title has their room at 360 px. Anything unbounded gets a page of its own (See All), since a
  `Gtk.GridView` cannot sit under a shelf in the same scrolled window.
- The artist page (pages/artist.py) is Apple's artist page over `Engine.artist_page`: the
  release card beside Top Songs (`widgets.song_shelf.SongShelf`, a horizontal `Gtk.GridView`
  three rows high, which plays the songs as one queue from the one activated), then two
  `ShelfColumn`s, before About and after it (Similar Artists), Essential Albums as hero cards
  (`show(heroes=…)`). Shelf titles are Apple's (`view_title()`, not `remote.shelf_title()`), and
  a shelf Apple has more of fetches the rest on See All (its `complete`, which
  `window.open_shelf` runs). The library's albums show only while the catalog cannot answer.
- A tile or row offers a context menu by exposing `context_item` (its Item or Track, None when
  unbound) and having `context_menu.attach(view)` called on its view (`drag=True` for tracks).
  The item actions take their object as a `(ss)` target (kind, id), not as state. Go to Album
  and Go to Artist go where `related.py` says, through `window.item_actions.go_to(obj, kind)`;
  a row's artist and album links are `widgets.track_links.TrackLink`s, clicked through
  `track_links.attach(view)` (the Songs table, a playlist's table).
- A playlist's page is a table from 720sp (`DetailPage.table`, set by detail.blp's
  breakpoint): TrackRow's table rows under a `TrackTableHeader` (the tracks' section header);
  an album's tracks stay a numbered list.
- The window's keyed actions (`win.back`, `win.search`, `win.focus-*`) are disabled while a
  dialog is open over the window (`Window._update_actions`); add a new keyed window action
  there.
- Per-item colours (a hero card's band) are drawn in the widget's own `do_snapshot`
  (`append_color`, then chain up) inside its rounded clip: CSS cannot take a value per item.
  Keep Python snapshots off the grid tiles (the hero card is a class of its own).

## HIG, style and icons

- libadwaita widgets and style classes first (`AdwStatusPage`, `AdwSpinner`, `AdwToast`,
  `AdwDialog`, `AdwPreferencesDialog`; `card`, `flat`, `circular`, `heading`, `title-1` to
  `title-4`, `caption`, `boxed-list`, `dimmed`; not the older alias `dim-label`). CSS only in
  `src/style.css`, with no per-widget CSS providers.
- No hard-coded colours except text drawn on an item's own colour. The accent is the user's,
  from Settings: libadwaita supplies it, and the app never overrides `--accent-bg-color`
  (the metainfo's `<branding>` is separate — that is the app's identity, not the user's choice).
- Every page fits a 360 px wide window (a `max-width: 400sp` breakpoint narrows margins where
  two tiles would not fit).
- Bundled icons are `src/icons/*-symbolic.svg`, found by icon name through the gresource alias.
  Use a `#222` fill (GTK recolours fills and strokes), Adwaita's 2 px weight at 16 px, and an
  outline as a ring with `fill-rule="evenodd"` (compare `non-starred-symbolic`). Bundle any icon
  the installed Adwaita theme lacks (`emblem-favorite-symbolic` is gone).

## Accessibility

- An icon-only button has `tooltip-text` (its accessible name).
- A recycled tile or row sets `list_item.set_accessible_label()` in bind (a description where
  it helps) with `widgets/labels.py`'s words: `bind_label()` (and `unbind_label()`) for a tile,
  which follows a renamed Item, `track_label()` for a track; a `Gtk.ColumnView` row through a
  `row-factory`; a `Gtk.FlowBoxChild` through `flow_child()`.
- Decorative images (`Gtk.Image`, `Gtk.Picture`, `$AppleMusicCover`, an `Adw.Avatar` beside a
  name) are `accessible-role: presentation`. A slider has a name and a `VALUE_TEXT`
  ("1:05 of 3:40"). State a widget does not expose itself goes through `update_state`
  (`EXPANDED` takes an int).
- Announcements go through the window (`get_root().announce(...)`): GTK drops one from a widget
  that no assistive technology has asked about yet.
- Grids and lists have `tab-behavior: item`. Dialog buttons have mnemonics (`_Label`,
  `use-underline: true`).
- Text on an item's colour reaches WCAG AA (`artwork.band_colour()`); dim text in markup follows
  the high-contrast setting; style.css may use `@media (prefers-contrast: more)`.

## Actions and shortcuts

- `src/shortcuts.py` holds `ACCELS` (the accelerators main.py sets), `PLAYBACK` (keys the
  window's capture-phase key controller handles), `MAIN_MENU`, `CONTEXT_MENU`, the sidebar's
  `FOLDER_OPEN` and `FOLDER_CLOSE` (sidebar_view.py binds them) and `sections()` (the
  Keyboard Shortcuts dialog); tests/test_shortcuts.py checks that the dialog lists every
  key, and that no accelerator is a HIG standard one the app does not implement
  (Ctrl+N is "New"). `src/keyboard.py` decides which playback action a key runs
  (`playback_action()`, tests/test_keyboard.py): Space belongs to a focused button, switch,
  check box or boxed-list row (GTK's own binding presses it), and every key to an entry, a
  popover or a dialog; `context_menu.MENU_KEYS` is built from `shortcuts.CONTEXT_MENU`.
