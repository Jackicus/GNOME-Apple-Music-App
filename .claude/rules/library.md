---
paths:
  - "src/library.py"
  - "src/sync.py"
  - "scripts/demo_library.py"
  - "tests/test_library.py"
  - "tests/test_reload.py"
  - "tests/test_normalize.py"
  - "tests/test_sync_app.py"
  - "tests/test_demo_schema.py"
  - "tests/fixtures/**"
---

# The library model and the sync

library.py's and sync.py's docstrings (the module's, `Library`'s, `Item.merge()`'s, `reload()`'s,
`paused_gc`'s) describe the model, the file format additions and the sync step by step; read
them before changing either. What matters most:

- `library.py` uses GLib, GObject and Gio only, no GTK, so it works in tests without a display.
- The stores (`albums`, `artists`, `playlists`, `radio`, `videos`, `songs`) keep their
  identity for the life of the app: pages bind them once and watch `notify::state` and
  `changed`. `state` is `empty`, `loading` or `ready`; a load that fails or is cancelled ends
  `empty` and still emits `changed`. `file-state` (`ok`, `missing`, `unreadable`) and `version`
  say what the last load found on disk. `syncing` is true while a sync runs (`LibrarySync`
  sets it), so a page can show an empty library as on its way.
- `load()` (the first load, after sign-out) makes new objects. `reload()` (after a sync) matches
  everything by kind and id and keeps every object still in the library: Items (with their
  Groups and Tracks when the groups are unchanged), ShelfModels by key, folder Items and the
  folders' stores by id, the Songs store. Open pages, scroll positions and the sidebar's
  selection survive it.
- A reload says what changed through signals. `Item.merge(data, replace=True)` does nothing
  for an equal dict (the one in hand is kept); otherwise it notifies each property that
  changed, returns their names, and emits `groups-changed` last when the groups changed (the
  next `groups` access makes new Groups and Tracks). Stores change through `apply_diff()`, the
  fewest splices. A kept Item is spliced over itself only when one of `SORT_KEYS` (title,
  subtitle, year) changed, so that sort and filter models place it again.
- GTK 4.22 does not rebind a row when a store splices an item over itself, so bound rows keep
  showing old values. A widget or page showing an Item follows that Item's `notify::*` signals
  and `groups-changed`, not the store's `items-changed`.
- One frame budget covers a whole load and Songs build: their loops await
  `self._pause(generation)` once `FRAME_BUDGET` has passed since the last pause (`_paused`,
  shared by all of them); it lets GTK paint and gives up when a newer load has started. A new
  loop in a load does the same, not a budget of its own.
- Properties: most of Track's and Item's are `raw_property`s, read from the object's `raw` at
  each access (an Item's are writable into it); Group and ShelfModel use `model_property`. Both are
  real GObject properties, so `Gtk.PropertyExpression` works on them. When wrapping in bulk,
  don't pass properties to `GObject.Object.__init__` (several µs each): assign the attributes
  as the wrappers do.
- A Track's `raw` is a `TrackRecord`, not a dict: the parse keeps each track of library.json
  as a tuple that reads like the dict (`get()`, `[]`, `in`, `keys()`, `items()`,
  `dict(record)`), to save memory. Code reading a Track's `raw` or a group's `entries` must not
  test for a dict or write into it. Items' `raw` stay dicts.
- The model's words are made and translated in library.py, not taken from the English the
  backend writes: an album or artist without a title is "Unknown Album" or "Unknown Artist"
  (`unknown_title()`); Home's fixed shelves are titled by key (`shelf_title()`); `count-label`
  ("12 songs, 43 min", "3 albums") is made by `count_text()` from an Item's `trackCount`,
  `durationMs` or `albumCount`, which the sync writes (a library.json from before them has an
  English `countLabel`, shown as it is, then the groups' counts), once, and kept so bind never
  calls gettext. The backend writes no words: a missing name is '', a shelf the app names is
  a key with an empty title (the Search page's kinds, New's `featured` shelf).
- Lazy parts: an Item's `groups` are wrapped on first access; `songs` stays empty until
  `await library.build_songs()` (the Songs page asks), then `songs-ready`, and from then on
  every load and reload brings it up to date with the fewest splices; `song_count()` counts
  without building.
- `SongOrder` sorts the Songs table in Python: each column's keys are computed once per track
  (a text column's as ranks of its collated texts) and kept in integer arrays.
  `await order.prepare(column)` computes them, and every track's `search_key`, in frame-sized
  steps; `tracks(column, descending)` then only sorts. The Songs page makes a new SongOrder
  whenever the Songs store changes.
- Playlist folders: library.json's optional `folders`; `playlist_tree()` (a new tree per load
  and reload) and `folder_items(id)` are made from it. A reload keeps a folder's store, a load
  makes a new one: follow a folder by its id, not by holding its store. The file keeps Apple's
  order; the tree lists each folder's contents as Apple Music's sidebar does (its web player's
  code): Favourite Songs, then folders, then playlists, each by `title_key()` (lower-cased,
  `localeCompare`-like: spaces and punctuation before digits, digits a character at a time,
  "10" before "2"), ties in the file's order. The sidebar, the folder grids, All Playlists and
  Add to Playlist follow the tree. A title changed in place (a rename merged into its Item)
  is placed by `place_playlists()`, which moves it in the stores and emits
  `playlists-changed`, which the window follows as it follows `changed`.
- Garbage collection (`paused_gc`): the collector is paused from the call of `load()` or
  `reload()` (so the parse in the library's thread runs without it) to the load's end, and
  during a Songs build. `load()` and `build_songs()` then freeze the heap (`gc.freeze()`), so
  full collections never scan the library again; a freezing pause collects first, and again
  before the freeze when something is frozen already, so no garbage is frozen. `reload()`
  never freezes: anything alive at a freeze that later dies in a reference cycle is never
  collected, and a reload follows every sync. Don't freeze anywhere else.
- Startup: `load()` starts reading the file in the library's thread the moment it is called
  (from `do_startup`); `hold_reading()`/`resume_reading()` pause the parse while the main thread
  runs Python before the window is presented, since the two share the GIL. The load's
  main-thread part is `Application.load_library()`'s task, started once: by `do_activate`,
  or by the search provider when the bus started the app for a search with no window to
  come (it resumes the parse itself).
- The sync (`sync_library()`, run one at a time by `LibrarySync`, `app.library_sync`) fetches
  through the engine only, normalises in a thread with `backend.normalize`'s pure functions,
  fetches missing thumbnails (covers are fetched on demand by the pages that show them), writes
  library.json atomically, prunes, then `reload()`s. A failed optional section keeps last
  time's entry (a playlist whose tracks failed keeps last time's tracks under its fresh
  listing, or gets `groups: []` for its page to fetch), read from library.json only then; a
  404 for a playlist's tracks or a folder's children means none; a failed songs or playlists
  listing, or a lost engine, fails the sync. It writes no words (shelf titles included).
- When to sync: `LibrarySync.schedule()` (not in demo mode) starts one when `due()` (the
  last sync older than `sync-interval`, or `library_ok()` false: library.json missing,
  unreadable or older than `LIBRARY_VERSION`, whatever the interval) and it can run without
  starting anything (signed in, the engine up and authorized): at its timer, when the engine
  comes up or is authorized, when the library has been read. After a failure it waits
  `RETRY_DELAY`; failed thumbnails are retried once. A change to library.json's shape that
  old files lack bumps `LIBRARY_VERSION` (and demo_library.py's), so they are synced again.
- After a library write (Add to Library) the app runs a **quick pass**,
  `app.start_sync(quick=True)`: the songs, the playlist listing and the shelves, keeping last
  time's playlist tracks, folders, videos and stations. It is what makes an added song appear
  in Songs and Recently Added without a full pass (#192); it stamps no last-sync.
- A full pass reads a playlist's tracks again only when its listing's `lastModifiedDate` or
  `trackCount` differs from what the Item recorded when they were last read (`modified`, and
  its `trackCount`: `playlist_unchanged()`); kept tracks keep their stamp, so a pass that
  did not read them cannot make them look current. A playlist listed without a date, or from
  a file without the key, is read every time. Nothing narrows the songs listing: Apple's API
  has no changes feed, since filter or conditional read the bridge could use (docs/notes.md).
- A cancelled sync's task ends only after its build thread has (`sync_library()` waits it
  out), so once `await app.library_sync.cancel()` returns nothing more of it is written.
  Clear Cache (account.py) bumps the cache's generation, cancels, then wipes; sign-out cancels
  the sync first (revoking the session would fail it), then revokes, bumps, stops the engine
  and wipes, and bumps and clears once more at the end for what a page fetched meanwhile.
  Both hold the sync (`LibrarySync.hold()`) so none starts meanwhile.
- The library.json shape lives in `src/backend/README.md` (with this app's additions) and
  library.py's docstring. A new key or kind touches the sync's writer, `scripts/demo_library.py`,
  tests/test_demo_schema.py and the README together.
- The demo library is invented and deterministic: `scripts/demo_library.py` with no options
  always writes the same build/demo. Fixtures in tests/fixtures/ are invented API answers; keep
  new ones invented (no real titles, names or IDs).
