# The backend

> **Vendored** from the GNOME Shell extension; `__init__.py` records where
> from, when, and every edit. Most of this file still describes the
> extension's process-per-command `am.py` (and the shell code around it),
> which this app keeps only as a reference for phases 9 to 11 of
> `prompts.md`: it is not installed, imported or linted. What carries over
> as is: `cdp.py`, `bridge.js`, `sync.py`, the command table (as the list of
> what the bridge can do), the error codes, and the `library.json`, `Item`
> and `Track` shapes. Here, paths, the port and the artwork sizes come from
> `config.py`, and nothing in this directory imports gi.

Everything that talks to Apple Music lives here. The shell process never
touches the network: `src/lib/amctl.js` spawns `am.py <command>`, reads the
one JSON object it prints, and otherwise only reads files this code wrote.

```
Extension (src/lib, in the shell) ──spawn──► am.py <command> ──CDP──► Chrome: music.apple.com
        ▲  reads ~/.cache/apple-music/library.json + art/ + thumb/           (one-shot, JSON on stdout)  │
        └──────────────── MPRIS (org.mpris.MediaPlayer2.chrome.*) ◄──────────────────────┘
```

- **`am.py`** — the CLI. Every command prints one JSON object and exits 0,
  or `{"error": "<code>", "message": "…"}` and exits 1. Error codes:
  `engine-down`, `not-signed-in`, `api`, `timeout`, `usage`. Failures worth
  a note go to stderr, prefixed `am.py:`.
- **`cdp.py`** — a Chrome DevTools Protocol client on the standard library
  alone (its own WebSocket framing). Nothing here needs pip.
- **`bridge.js`** — the only code that runs inside the page. Injected
  idempotently, it defines `window.__appleMusicLibrary` over the page's own
  `MusicKit` instance; `am.py`'s commands are thin calls into it.
- **`sync.py`** — pure functions turning API responses into the `Item` and
  `Track` shapes below, plus the artwork cache. Unit tested in `tests/`.

## The engine

The engine is Google Chrome (the real build: it ships Widevine, Chromium
doesn't) running music.apple.com in a profile of its own, with the
debugging port on 127.0.0.1. The user signs in once in a visible window;
after that it runs headless. Its state is
`$XDG_RUNTIME_DIR/apple-music/engine.json`: `{pid, port, headless, profile,
started}`, which `player.js` reads to pick Chrome's MPRIS player out of the
bus by PID.

The page exposes MusicKit v3 as a global, and that is all the bridge uses:
`mk.api.music(path, params)` for reads, `mk.setQueue({album|playlist|
station|song|songs, startWith, startPlaying})` and `mk.play()` for
playback, `mk.playNext`/`mk.playLater`, `mk.shuffleMode`/`mk.repeatMode`,
and `mk.isAuthorized`/`mk.storefrontId`. API failures come back as a 200
with `{"errors": [...]}`, which `am.py` treats as a failure and retries.
Writes (love, add to library, add to playlist) use
`mk.api.client.createRequest(path, {params, method, body}).send()` instead
of `music()`: Apple answers them with 202 or 204 and an empty body, which
`music()` cannot parse, and a refusal is a 4xx that `music()` would hand
back as if it had worked. The bridge checks the status itself.

Every command starts the engine if it is not running — headless, or
visible when the `engine-headless` setting is off — unless `--no-start` is
given (`status` never starts it). The shell passes `--no-start` for
everything that only wants an answer if the engine happens to be up (search,
the now-playing poll, lyrics, the queue), so typing in the overview never
launches Chrome.

The settings (`browser-command`, `engine-port`, `engine-headless`,
`engine-autostart`) were read from the extension's own compiled schema. The
backend imports no gi, so here `am.py` takes their defaults, with the paths
and the port from `config.py`; the app's own GSettings keys arrive with the
Engine (phase 10) and the sync (phase 11), and are read in the app.

| Command | Result |
|---|---|
| `engine start [--visible\|--headless]`, `engine stop`, `engine status` | `{running, pid, port, headless}` |
| `signin` | Restarts the engine visible at music.apple.com and calls `mk.authorize()`. `{authorized}` |
| `status` | `{engine, authorized, storefront, bitrate}` |
| `sync [--only albums\|artists\|playlists\|radio\|shelves]` | Fetches the missing artwork in threads at the `cover-size`/`thumb-size` settings, then writes library.json once (merged per section, under `flock`), then prunes: the artwork nothing names, and `remote-art/` to a budget. `{counts, generated}` |
| `item <kind> <id>` | One full item with its `groups`, for a shelf or search result picked on demand; fetches its artwork too, and keeps the answer at `<cache>/items/<kind>-<id>.json` for the shell to read back without a process |
| `play <kind> <id> [--start-with N] [--shuffle]` | `{ok: true}`. kind ∈ `album playlist station song musicVideo artist` |
| `play-next <kind> <id>`, `play-later <kind> <id>` | `{ok: true}` |
| `control play\|pause\|toggle\|next\|previous\|stop`, `seek <sec>` | `{ok: true}` |
| `volume <0..1>` | `{volume}` — the level as MusicKit has it after the set. It is the engine's own, not the system's; Apple's page keeps it across restarts, and a nought is a mute |
| `shuffle on\|off\|toggle`, `repeat none\|one\|all\|cycle` | `{shuffle, repeat}` |
| `now-playing` | `{state, track, position, duration, shuffle, repeat, volume}` |
| `queue` | `{index, items: [Track…]}` |
| `love\|unlove <kind> <id>`, `add-to-library <kind> <id>` | `{ok: true}` |
| `playlists`, `add-to-playlist <playlistId> <songId>` | `{items: [{id, title}]}`, `{ok: true}` |
| `lyrics <catalogSongId>` | `{synced, lines: [{startMs, endMs, text}]}`, cached under `<cache>/lyrics/` |
| `search <term> [--library] [--limit N] [--suggest N]` | `{shelves: [{key, title, items: [Item without groups]}], items: [the same, flat, no repeats]}` — the shelves in Apple's own order (`meta.results.order`: Top Results, then Artists, Songs, Albums, Playlists, Stations; `--limit` is per kind), for the search in the library; the flat list for the overview's provider. A music video is an Item of kind `video`, played as MusicKit's `musicVideo`. A hit's `art` is its cached cover or, unfetched, a thumbnail-sized catalog URL; its `thumb` is null unless on disk. `--suggest N` adds `terms`, the first N of what `suggest` would answer, asked in the same round trip — one process for the shell's search rather than two |
| `suggest <term> [--limit N]` | `{terms: [{term, display}], items: [Item without groups]}` — Apple's own autocomplete for a term half typed (`search/suggestions`): the few searches it would complete it to, and its best few hits for it as it stands, art as `search` has it |
| `landing` | `{categories: [{id, kind: "category", title, subtitle, art, artColor, url}]}` — the "Browse Categories" of Apple Music's own search page before anything is typed (the `search-landing` recommendation set): Apple's curators, Rock to Wellbeing, in Apple's order; `art` a 320px catalog URL the shell fetches itself, `artColor` the tile's colour. Kept at `<cache>/landing.json` and answered from there for a day without touching the engine |
| `category <id>` | `{id, title, shelves: [{key, title, items: [Item without groups]}]}` — a category's page: the curator's grouping as shelves (Best New Songs, New Releases, Playlists, Stations…), items as `search` has them. Kept at `<cache>/categories/<id>.json` for a day, as `landing` is |

## `library.json`

`<cache>/library.json` (`~/.cache/apple-music/library.json`) is written only by `am.py sync` (or by
`scripts/demo_library.py` for demos and tests) and read only by
`src/lib/library.js`.

```jsonc
{
  "version": 1, "generated": "2026-09-25T12:00:00Z", "storefront": "us",
  "sections": {"albums": [Item], "artists": [Item], "playlists": [Item], "radio": [Item]},
  "shelves": [{"key": "rec-<id>", "title": "New Releases for You", "items": [Item]}, …,  // Apple's home
              {"key": "heavy-rotation", "title": "Heavy Rotation", …}, {"key": "recently-added", …}]
}

Item = {
  "id": "l.abc123",             // library id, or catalog id when not in the library
  "kind": "album" | "playlist" | "artist" | "station",
  "title": "…", "subtitle": "…",                 // artist, curator, or "Apple Music"
  "year": 2007, "genre": "Rock", "summary": "plain text" /* or null */,
  "art": "<cache>/art/<sha1>.jpg" /* or null */,  // 640x640, the hero
  "thumb": "<cache>/thumb/<sha1>.jpg" /* or null */,  // 320x320, the tiles; same name as its art
  "artColor": "#1a1a1a" /* or null */,
  "countLabel": "12 songs, 43 min", "explicit": false,
  "catalogId": "…" /* or null */, "url": "https://music.apple.com/…" /* or null */,
  "play": {"kind": "album", "id": "l.abc123"},   // what `am.py play` gets
  "groups": [{"name": "Disc 1", "play": {"kind": "album", "id": "l.abc123"}, "entries": [Track]}]
                                                  // artists: one group per album; stations and
                                                  // shelf items: [] until `am.py item` fills them
}
Track = {"id": "i.xyz", "catalogId": "…" /* or null */, "title": "…", "artist": "…", "album": "…",
         "trackNumber": 1, "discNumber": 1, "durationMs": 216000, "durationLabel": "3:36",
         "explicit": true, "index": 0,            // index = position in group.play's queue
         "thumb": "<cache>/thumb/<sha1>.jpg" /* or null */}  // a playlist's rows only
```

Artwork is fetched at `config.COVER_SIZE` (640×640) into
`<cache>/art/`, and each cover has a `config.THUMB_SIZE` (320×320) thumbnail under
the same name in `<cache>/thumb/` — scaled from the cover when it is
already on disk (by `sync.scale_image`, which the app installs), fetched otherwise. The tiles and track rows
draw the thumbnail; the detail pane's hero draws the cover. The shell
decodes a cover whole, on the compositor thread, the first time a tile is
painted, so the thumbnail size is what a page of tiles costs to show.
`<cache>/art/.sizes` records what the cache was built at: a sync applies the
sizes and writes it (a changed thumbnail size wipes `thumb/`, the files
keeping their names whatever the size; a changed cover size is a new URL
and so new names, the old pruned), and every other command reads it so the
URLs it names agree with the files. A path that is not on disk counts as
no artwork. A sync fetches the artwork *before* writing library.json, so a
listing never lands ahead of its covers. A track row plays `group.play`
with `--start-with entry.index`.

Beside these, `<cache>/remote-art/` holds what the shell fetches for itself
(a search hit's cover, the player's, a category's picture; `lib/playerUtil.js`),
trimmed to 32 MB by mtime at the end of each sync; `<cache>/items/`,
`<cache>/landing.json` and `<cache>/categories/` hold the answers above,
each stamped `cached`; `<cache>/lyrics/` the lyrics fetched.

Every `am.py` command is a process of its own, so what is imported at load
is paid on every one: PyGObject, urllib and the thread pool are imported
only when artwork is fetched, and the process modules only when Chrome is
started or stopped — `am.py engine status` is about sixty milliseconds
end to end.

## Keeping tests and dev runs off the real profile

| Variable | Overrides |
|---|---|
Read by `config.py` on every call:

| Variable | Overrides |
|---|---|
| `APPLE_MUSIC_PROFILE` | Chrome's profile directory (default `$XDG_DATA_HOME/apple-music/chrome`); the engine state then lives in it too |
| `APPLE_MUSIC_PORT` | the debugging port (default 9228) |
| `APPLE_MUSIC_CACHE` | the cache directory (default `$XDG_CACHE_HOME/apple-music`) |

```bash
python3 -m unittest discover -s tests -v   # the tests, the backend's among them
scripts/check.sh                            # those plus byte-compiling, lint, the build and its tests
scripts/demo_library.py --cache build/demo  # an invented library.json and artwork, no Chrome
```
