"""The engine's backend: Chrome DevTools, the page bridge, and turning Apple's answers into data.

Pure Python and the standard library: nothing here imports gi (GTK stays in the app), and
importing the package imports none of its modules.

    errors.py   EngineError(code, message): engine-down, not-signed-in, api, timeout, usage
    chrome.py   finding Chrome, its argv (the host's Chrome through flatpak-spawn from a
                Flatpak sandbox), the engine.json state (EngineState), the DevTools /json
                polling (wait_for_devtools, list_targets, find_target, wait_for_target)
    client.py   CDPClient: one asynchronous CDP connection (calls, evaluate, events, the
                bridge kept in the page across navigations); connect_page(port)
    cdp.py      the RFC 6455 handshake and frame codec as pure functions, shared with
                client.py, and the extension's blocking client on top of them
    bridge.js   injected into music.apple.com; drives the page's own MusicKit instance and
                forwards its events (subscribe). Installed as data beside the Python, found
                through config.BRIDGE_JS
    sync.py     Apple Music API answers -> the Item and Track shapes, the artwork cache,
                library.json
    config.py   paths, port, the state file and artwork sizes (new here; replaces the
                extension's GSettings)
    README.md   the command table, error codes, the events, and the library.json/Item/Track
                shapes

The extension's am.py (its one-process-per-command CLI) was vendored as a reference for
phases 9 to 11 and deleted in phase 11 once its engine lifecycle (chrome.py, scripts/am.py),
its commands (src/engine.py) and its sync (src/sync.py) had been ported.

Provenance. Vendored on 2026-09-27 from the GNOME Shell extension Apple Music Library,
~/Projects/GNOME-Extensions/GNOME-Apple-Music-Library at commit 906bfa9: src/backend/{cdp.py,
bridge.js,sync.py,am.py,README.md}; tests/{test_cdp.py,test_sync.py,test_demo_schema.py} and
tests/fixtures/ (the vendored fixtures unchanged; phase 11 added playlist_folders.json,
library_playlists_tags.json, library_music_videos.json and recently_added.json, invented, for
the app's sync tests); scripts/demo_library.py. Edits, to keep when refreshing
from upstream:

cdp.py
- The HTTP User-Agent is AppleMusicGNOME/1.0 (was the extension's name).
- (Phase 9) The handshake and frame codec are pure functions (parse_ws_url, handshake_request,
  check_handshake, encode_frame, decode_frame, close_frame) and the one-line JS exception
  message is exception_message(); CDPClient uses them (its _read_exact became _fill_buffer over
  decode_frame). Behaviour and test_cdp.py unchanged.
bridge.js
- The page globals keep their names (window.__appleMusicLibrary{,Wanted}).
- (Phase 9) subscribe() and unsubscribe(): MusicKit listeners (EVENT_DATA's eleven events),
  posted through the CDP binding window.__amEvent as JSON {name, data}; the listeners are
  remembered in window.__appleMusicListeners so a repeat is a no-op and a newer bridge replaces
  an older one's. queue() is queueSnapshot(), shared with the queueItemsDidChange event.
- (Phase 14) queueJump(index): mk.changeToMediaAtIndex, for the app's Up Next list.
- (Phase 16) addToPlaylist(playlistId, songId, type): the song's resource type is a third,
  optional argument ('songs', upstream's fixed value, when left out; the app sends
  'library-songs' for a library "i." id). rating(), addToLibrary() and playlists() unchanged.
sync.py
- Imports config; DEFAULT_ART_SIZES are config.COVER_SIZE/THUMB_SIZE, 640/320 (were 512/256).
  The art/.sizes marker and apply_art_sizes/load_art_sizes are unchanged.
- No gi: pixbuf(), which imported GdkPixbuf lazily, is gone. make_thumbnail calls the module's
  `scale_image(src, dest, size)` hook, which the app installs; without one it returns False and
  the thumbnail is fetched at its own size (upstream's behaviour without PyGObject).
- The artwork fetch's User-Agent, as in cdp.py.
- (Phase 11) download_art(progress=None, cancelled=None): progress(done, total) after each
  fetch, cancelled() asked before each result (True gives up the rest); save_library(indent=2)
  takes json.dump's indent (the app writes the compact form). Behaviour otherwise unchanged.
- (Phase 15) category_page's element walk is _grouping_shelves(grouping, cache_dir, prefix,
  featured=None), shared with the new editorial_shelves(raw, cache_dir) (the New page: the
  editorial groupings' elements as shelves, the untitled banner element as one "Featured"
  shelf) and made_for_you_shelves(raw_recs, cache_dir) (the recommendations made only of
  personal mixes and stations); items come through _shelf_item, which turns an Apple curator
  into a category tile (normalize_category) and keeps only SHELF_RESOURCE_TYPES.
  browse_cache_path() and made_for_you_cache_path() beside the landing's and categories'.
README.md
- A header note on what still applies; the paths, variables, port, sizes, the thumbnail scaler
  and the settings paragraph describe this app.
config.py
- New: cache_dir(), profile_dir(), port(), state_file() with the APPLE_MUSIC_CACHE,
  APPLE_MUSIC_PROFILE and APPLE_MUSIC_PORT overrides, THUMB_SIZE, COVER_SIZE, BRIDGE_JS.
- (Phase 9) state_file(profile=None) is keyed by profile: the default profile's engine.json is
  in $XDG_RUNTIME_DIR/apple-music, any other profile's (an override, the .Devel build's
  chrome-devel) inside that profile; default_profile_dir().
errors.py, chrome.py, client.py
- New in phase 9 (this app's own; nothing vendored).
tests/test_cdp.py
- Imports applemusic.backend.cdp through the tests/__init__.py shim. The test HTTP servers poll
  for shutdown every 50 ms instead of 500 ms (about three seconds off the suite).
tests/test_sync.py
- Imports applemusic.backend.{config,sync} through the shim. TestSync and TestArtworkDownload
  pin the extension's 512/256 sizes their expected URLs were written for; TestArtSizes expects
  config's defaults. The thumbnail-scaling test installs a GdkPixbuf scale_image itself, and a
  new test covers the fetch without one.
- Real artists, songs, albums and a curator id in test inputs are replaced by invented ones.
tests/test_demo_schema.py
- Imports through the shim; builds the demo once, through the command line with --cache, in
  setUpClass (it built it twice); a new test checks the artwork and .sizes are at config's sizes.
scripts/demo_library.py
- Loads src/ as the applemusic package, as tests/__init__.py does, for config and sync.
- `--cache DIR`, default build/demo, replaces --out-dir, the positional directory and the
  extension's cache default; the compatibility symlink named after the extension is gone.
- Covers at config.COVER_SIZE (the 512-unit layout, scaled) and thumbnails at THUMB_SIZE, as
  parameters of draw_cover and build_demo_library; art/.sizes written by sync.apply_art_sizes.
  Pixels go from the Cairo surface to GdkPixbuf directly instead of through a PNG.
- Names that were not invented are replaced: a band named after a real person, curators named
  after the extension and a real product, and Apple's own three radio stations. Apple's curator
  labels ("Apple Music Chill", "Apple Music Radio") stay, as the service's own wording.
- (Phase 4) `--albums N` adds generated albums and artists (generated_albums) after the
  hand-written ones, which stay as they were; covers are queued while building and drawn at the
  end (draw_covers), by a forked process pool from 200 up.
"""
