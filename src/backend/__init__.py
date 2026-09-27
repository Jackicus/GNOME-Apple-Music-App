"""The engine's backend: Chrome DevTools, the page bridge, and turning Apple's answers into data.

Pure Python and the standard library: nothing here imports gi (GTK stays in the app), and
importing the package imports none of its modules.

    cdp.py      a CDP client over its own RFC 6455 WebSocket framing (blocking; phase 9 adds
                the asynchronous one)
    bridge.js   injected into music.apple.com; drives the page's own MusicKit instance.
                Installed as data beside the Python, found through config.BRIDGE_JS
    sync.py     Apple Music API answers -> the Item and Track shapes, the artwork cache,
                library.json
    config.py   paths, port and artwork sizes (new here; replaces the extension's GSettings)
    am.py       REFERENCE ONLY: the extension's one-process-per-command CLI. Not installed,
                not imported, not linted; phases 9 to 11 port its engine lifecycle, sync and
                command bodies, then delete it
    README.md   the command table, error codes and library.json/Item/Track shapes

Provenance. Vendored on 2026-09-27 from the GNOME Shell extension Apple Music Library,
~/Projects/GNOME-Extensions/GNOME-Apple-Music-Library at commit 906bfa9: src/backend/{cdp.py,
bridge.js,sync.py,am.py,README.md}; tests/{test_cdp.py,test_sync.py,test_demo_schema.py} and
tests/fixtures/ (fixtures unchanged); scripts/demo_library.py. Edits, to keep when refreshing
from upstream:

cdp.py
- The HTTP User-Agent is AppleMusicGNOME/1.0 (was the extension's name).
bridge.js
- None. The page globals keep their names (window.__appleMusicLibrary{,Wanted}).
sync.py
- Imports config; DEFAULT_ART_SIZES are config.COVER_SIZE/THUMB_SIZE, 640/320 (were 512/256).
  The art/.sizes marker and apply_art_sizes/load_art_sizes are unchanged.
- No gi: pixbuf(), which imported GdkPixbuf lazily, is gone. make_thumbnail calls the module's
  `scale_image(src, dest, size)` hook, which the app installs; without one it returns False and
  the thumbnail is fetched at its own size (upstream's behaviour without PyGObject).
- The artwork fetch's User-Agent, as in cdp.py.
am.py
- A header comment marks it reference only. Package imports (from .cdp, from . import config,
  sync) instead of a sys.path insert, so it no longer runs as a script.
- Paths, port and state file come from config.py (APPLE_MUSIC_* variables, port 9228, the
  apple-music directories) instead of the extension's; the settings the extension read from its
  GSettings schema through gi are plain defaults (get_setting), set_setting does nothing.
- ensure_bridge reads config.BRIDGE_JS.
README.md
- A header note on what still applies; the paths, variables, port, sizes, the thumbnail scaler
  and the settings paragraph describe this app.
config.py
- New: cache_dir(), profile_dir(), port(), state_file() with the APPLE_MUSIC_CACHE,
  APPLE_MUSIC_PROFILE and APPLE_MUSIC_PORT overrides, THUMB_SIZE, COVER_SIZE, BRIDGE_JS.
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
