# The backend

> **A fork** of the GNOME Shell extension's backend, owned by this app (its history is
> `git log -- src/backend`). Most of this file still describes the
> extension's process-per-command `am.py` (and the shell code around it),
> which this app kept as a reference until its sync and commands were
> ported (`src/engine.py`, `src/sync.py`; the file is gone). What carries
> over: `cdp.py` (its WebSocket codec only), `bridge.js`, `normalize.py` (was `sync.py`), the command table (as the
> list of what the bridge can do), the error codes, and the `library.json`,
> `Item` and `Track` shapes (with this app's additions, noted there). Here,
> paths and the artwork sizes come from `config.py`, and nothing
> in this directory imports gi. This app's own layer on top, one long-lived
> asynchronous connection instead of a process per command, is described at
> the end ("The asynchronous layer").

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
- **`normalize.py`** — pure functions turning API responses into the `Item` and
  `Track` shapes below, plus the artwork cache. Unit tested in `tests/`.

## The engine

The engine is Google Chrome (the real build: it ships Widevine, Chromium
doesn't) running music.apple.com in a profile of its own. The user signs in
once in a visible window; after that it runs headless. The extension drove
it through a DevTools port on 127.0.0.1 and recorded it in a state file,
which its `player.js` read to pick Chrome's MPRIS player out of the bus by
PID; this app talks to its Chrome over a pipe (below, "The asynchronous
layer").

The page exposes MusicKit v3 as a global, and that is all the bridge uses:
`mk.api.music(path, params)` for reads, `mk.setQueue({album|playlist|
station|song|songs, startWith, startPlaying})` and `mk.play()` for
playback, `mk.playNext`/`mk.playLater`, `mk.shuffleMode`/`mk.repeatMode`,
and `mk.isAuthorized`/`mk.storefrontId`. API failures come back as a 200
with `{"errors": [...]}`, which the Engine treats as a failure
(`api.api_error`, its HTTP status as `EngineError.status`); it tries a read
again only when it may pass (a 5xx, a 429, no status), never after a 4xx or
a timeout.
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

The settings (`browser-command`, a port, `engine-headless`,
`engine-autostart`) were read from the extension's own compiled schema. The
backend imports no gi, so here `am.py` took their defaults, with the paths
from `config.py`; the app's own GSettings keys arrived with the Engine
(phase 10) and the sync (phase 11), and are read in the app.

| Command | Result |
|---|---|
| `engine start [--visible\|--headless]`, `engine stop`, `engine status` | `{running, pid, port, headless}` |
| `signin` | Restarts the engine visible at music.apple.com and calls `mk.authorize()`. `{authorized}` |
| (bridge only) `signout()` | `{ok: true}` or `{error}`, never a throw — `mk.unauthorize()`, which revokes the session at Apple's end; the app's sign-out calls it (`Engine.unauthorize()`) before forgetting the account |
| (bridge only) `accountName()` | the account's name as the signed-in page shows it, or null (none while a sign-in control is on the page) |
| `status` | `{engine, authorized, storefront, bitrate}` |
| `sync [--only albums\|artists\|playlists\|radio\|shelves]` | Fetches the missing artwork in threads at the `cover-size`/`thumb-size` settings, then writes library.json once (atomically: a temporary file renamed over it), then prunes: the artwork nothing names, and `remote-art/` to a budget. `{counts, generated}` |
| `item <kind> <id>` | One full item with its `groups`, for a shelf or search result picked on demand; fetches its artwork too |
| `play <kind> <id> [--start-with N] [--shuffle]` | `{ok: true}`. kind ∈ `album playlist station song musicVideo artist songs` (`songs`: song ids joined by commas, a stand-in album's). The bridge's `play(kind, id, {startWith, shuffle})`: shuffle `true` turns MusicKit's shuffle on, `false` off (a Play button plays in order), `null` leaves it (a track row); MusicKit refusing answers `{error, code}` (the Engine raises `EngineError('api')` with `musickit_code`) |
| `play-next <kind> <id>`, `play-later <kind> <id>` | `{ok: true}` |
| `control play\|pause\|toggle\|next\|previous\|stop`, `seek <sec>` | `{ok: true}` |
| `volume <0..1>` | `{volume}` — the level as MusicKit has it after the set. It is the engine's own, not the system's; Apple's page keeps it across restarts, and a nought is a mute |
| `shuffle on\|off\|toggle`, `repeat none\|one\|all\|cycle` | `{shuffle, repeat}` |
| `now-playing` | `{state, track, position, duration, shuffle, repeat, volume}` — state is the `MusicKit.PlaybackStates` name, as `playbackStateDidChange` carries it |
| `queue` | `{index, items: [Track…]}` |
| (bridge only) `queueJump(index)` | `{ok: true}` — plays the queue's entry at `index` (`mk.changeToMediaAtIndex`); the app's Up Next list, phase 14 |
| `love\|unlove <kind> <id>`, `add-to-library <kind> <id>` | `{ok: true}` |
| `add-to-playlist <playlistId> <songId>` | `{ok: true}` |
| `lyrics <catalogSongId>` | `{synced, lines: [{startMs, endMs, text, stanza?}]}` (the TTML read by the page's XML parser, so entities are decoded; `stanza: true` on the first line of each verse or chorus), cached under `<cache>/lyrics/` |
| `search <term> [--limit N]` | `{shelves: [{key, title: "", items: [Item without groups]}]}` (key `top`, `artists`, `albums`, `songs`, `playlists`, `music-videos` or `stations`: the Search page names them) — the shelves in Apple's own order (`meta.results.order`: Top Results, then Artists, Songs, Albums, Playlists, Stations; `--limit` is per kind). A music video is an Item of kind `video`, played as MusicKit's `musicVideo`. A hit's `art` is its cached cover or, unfetched, a thumbnail-sized catalog URL; its `thumb` is null unless on disk |
| `suggest <term> [--limit N]` | `{terms: [{term, display}], items: [Item without groups]}` — Apple's own autocomplete for a term half typed (`search/suggestions`): the few searches it would complete it to, and its best few hits for it as it stands, art as `search` has it |
| `landing` | `{categories: [{id, kind: "category", title, subtitle, art, artColor, url}]}` — the "Browse Categories" of Apple Music's own search page before anything is typed (the `search-landing` recommendation set): Apple's curators, Rock to Wellbeing, in Apple's order; `art` a 320px catalog URL the shell fetches itself, `artColor` the tile's colour. Kept at `<cache>/landing.json` and answered from there for a day without touching the engine |
| `category <id>` | `{id, title, shelves: [{key, title, items: [Item without groups]}]}` — a category's page: the curator's grouping as shelves (Best New Songs, New Releases, Playlists, Stations…), items as `search` has them. Kept at `<cache>/categories/<id>.json` for a day, as `landing` is |

## `library.json`

`<cache>/library.json` (`~/.cache/apple-music/library.json`) is written only by `am.py sync` (or by
`scripts/demo_library.py` for demos and tests) and read only by
`src/lib/library.js`.

```jsonc
{
  "version": 2, "generated": "2026-09-25T12:00:00Z", "storefront": "us",
  "sections": {"albums": [Item], "artists": [Item], "playlists": [Item], "radio": [Item]},
  "shelves": [{"key": "rec-<id>", "title": "New Releases for You", "items": [Item]}, …,  // Apple's home
              {"key": "heavy-rotation", "title": "", …}, {"key": "recently-added", …}]
                                                // the last two titled by the app, by key
}

Item = {
  "id": "l.abc123",             // library id, or catalog id when not in the library
  "kind": "album" | "playlist" | "artist" | "station",
  "title": "…", "subtitle": "…",                 // artist or curator; "" when none
  "year": 2007, "genre": "Rock", "summary": "plain text" /* or null */,
  "art": "<cache>/art/<sha1>.jpg" /* or null */,  // 640x640, the hero
  "thumb": "<cache>/thumb/<sha1>.jpg" /* or null */,  // 320x320, the tiles; same name as its art
  "artColor": "#1a1a1a" /* or null */,
  "trackCount": 12, "durationMs": 2580000 /* or null */,  // albums and playlists (the total
                                                  // time only with the tracks in hand)
  "albumCount": 3,                                // artists
  "explicit": false,
  "catalogId": "…" /* or null */, "url": "https://music.apple.com/…" /* or null */,
  "play": {"kind": "album", "id": "l.abc123"},   // what `am.py play` gets
  "groups": [{"name": "Disc 1", "play": {"kind": "album", "id": "l.abc123"}, "entries": [Track]}]
                                                  // artists: one group per album; stations and
                                                  // shelf items: [] until `am.py item` fills them
}
// The data has no words: the app writes the captions ("12 songs, 43 min") from the counts,
// and names what has no name ("Unknown Album") and the shelves it titles by key. A library.json
// from before the counts has an English "countLabel" instead, which the app shows as it is.
Track = {"id": "i.xyz", "catalogId": "…" /* or null */, "title": "…", "artist": "…", "album": "…",
         "trackNumber": 1, "discNumber": 1, "durationMs": 216000, "durationLabel": "3:36",
         "explicit": true, "index": 0,            // index = position in group.play's queue
         "thumb": "<cache>/thumb/<sha1>.jpg" /* or null */,  // a playlist's rows only
         "type": "library-songs"}                 // the API's: songs, music-videos (library- or
                                                  // not), '' when unknown; the bridge's Tracks
                                                  // (now playing, the queue) carry it too
```

**This app's additions** (`src/sync.py` writes them; all optional; the demo library
has the first two). The version is 2 since an album's tracks are indexed across its discs
(`index` counts on from disc 1 into disc 2): the app syncs a file of version 1 again at
once.

```jsonc
{
  "sections": {…,                              // (older syncs wrote "songs": [Track], the
                                               // loose songs, which are only under their
                                               // stand-in album now, `l.alb_…`, playing
                                               // {"kind": "songs", "id": "<id>,<id>…"}; the
                                               // model still reads it)
               "videos": [Item]},              // kind "video", play {"kind": "musicVideo"},
                                               // "durationMs" its length
  "folders": [{"id": "root", "title": "", "parent": null,      // Apple's p.playlistsroot
               "children": [{"kind": "playlist", "id": "p.pl123"}, {"kind": "folder", "id": "p.fld1"}]},
              {"id": "p.fld1", "title": "Workouts", "parent": "root", "children": […]}]
}
Item += {"artUrl": "https://…/640x640bb.jpg",  // the cover's URL, for fetching it on demand
         "attributes": {"isFavourites": true}} // on the Favourite Songs playlist only (Apple's
                                               // attributes.tags holds "favorited" for it)
```

Artwork is fetched at `config.COVER_SIZE` (640×640) into
`<cache>/art/`, and each cover has a `config.THUMB_SIZE` (320×320) thumbnail under
the same name in `<cache>/thumb/` — scaled from the cover when it is
already on disk (by `sync.scale_image`, which the app installs), fetched otherwise. The tiles and track rows
draw the thumbnail; the detail pane's hero draws the cover. (This app's
sync fetches the thumbnails only; a page that shows a cover fetches it
from `artUrl` when it first needs it.) The shell
decodes a cover whole, on the compositor thread, the first time a tile is
painted, so the thumbnail size is what a page of tiles costs to show.
`<cache>/art/.sizes` records what the cache was built at: a sync applies the
sizes and writes it (a changed thumbnail size wipes `thumb/`, the files
keeping their names whatever the size; a changed cover size is a new URL
and so new names, the old pruned), and the app reads it once at startup so
the URLs it names agree with the files. A path that is not on disk counts as
no artwork. A sync fetches the artwork *before* writing library.json, so a
listing never lands ahead of its covers. Every file here is written through
`store.py`: a dot-named temporary file beside it, renamed over it (library.json
and the kept answers fsync'd first), so a reader never sees half a file. A track row plays `group.play`
with `--start-with entry.index`.

Beside these, `<cache>/remote-art/` holds the artwork the pages fetch for
themselves (a search hit's cover, the player's, a category's picture) and that
of an item fetched on demand (`Engine.item()`, through
`normalize.place_in_remote_art`: art/ and thumb/ are pruned against
library.json), each named after its URL's hash; `<cache>/landing.json`
and `<cache>/categories/` hold the answers above,
each stamped `cached` (the app adds `browse.json`, the New page's editorial
groupings as shelves, and `made-for-you.json`, the recommendations made of
personal mixes and stations, kept the same way); `<cache>/lyrics/` the
lyrics fetched, stamped too and fetched again after 30 days.
`normalize.prune_caches()` trims them: remote-art/ to 32 MB by mtime, lyrics/
to the 2,000 played last (a cache hit touches the file), the kept answers once
past their day, an `items/` folder older versions kept, and the temporary files
of writes a crash cut short.

Every `am.py` command is a process of its own, so what is imported at load
is paid on every one: PyGObject, urllib and the thread pool are imported
only when artwork is fetched, and the process modules only when Chrome is
started or stopped — `am.py engine status` is about sixty milliseconds
end to end.

## Keeping tests and dev runs off the real profile

Read by `config.py` on every call:

| Variable | Overrides |
|---|---|
| `APPLE_MUSIC_PROFILE` | Chrome's profile directory (default `$XDG_DATA_HOME/apple-music/chrome`, `chrome-devel` for the development build) |
| `APPLE_MUSIC_DEBUG_PORT` | a DevTools port on 127.0.0.1 beside the engine's pipe, for `scripts/am.py --attach`; unset by default, and while it is set any local program can drive the signed-in session |
| `APPLE_MUSIC_CACHE` | the cache directory (default `$XDG_CACHE_HOME/apple-music`, `apple-music-devel` for the development build) |

```bash
python3 -m unittest discover -s tests -v   # the tests, the backend's among them
scripts/check.sh                            # those plus byte-compiling, lint, the build and its tests
scripts/demo_library.py --cache build/demo  # an invented library.json and artwork, no Chrome
```

## The asynchronous layer (this app, phase 9)

The app is one long-running process, so it keeps one CDP connection open
and hears MusicKit's events instead of polling. Pure Python and asyncio,
no gi; the app's `Engine` (`src/engine.py`, phase 10: Chrome as a
`Gio.Subprocess`, the commands as coroutines, sign-in) and `scripts/am.py`
sit on it.

- **`errors.py`** — `EngineError(code, message)` with the codes above
  (`engine-down`, `not-signed-in`, `api`, `timeout`, `usage`, and this app's
  `no-browser`: no Google Chrome to start, or it could not be started). Everything
  in this layer raises it and nothing else.
- **`chrome.py`** — `find_chrome(command)` (the configured command, then
  `google-chrome-stable`, `google-chrome`, `/opt/google/chrome/chrome`;
  None if absent; a configured command that is missing is logged),
  `chrome_args(binary, profile, headless, debug_port)` (the
  argv: `--remote-debugging-pipe`, so CDP runs over Chrome's descriptors 3
  and 4 and no port listens, and a port on 127.0.0.1 as well only with
  `debug_port`; `--disable-features=HardwareMediaKeyHandling` so Chrome
  publishes no MPRIS player; visible mode is an `--app=` window as before),
  `select_page(targets)` (the page target on `https://music.apple.com`, by
  parsed URL), `get_json(port, path)` (a DevTools port's `/json`, in a
  thread: the developer attach only), `profile_owner(profile)` (the pid
  Chrome's own `SingletonLock` names, when `/proc` shows a Chrome browser
  process on exactly that profile: `cmdline_names_profile` matches the flag
  as a whole argument and rejects `--type=` helpers), `pid_alive`.
- **`client.py`** — two transports carrying whole messages:
  `PipeTransport(read_fd, write_fd)` (Chrome's pipe, NUL-terminated JSON,
  asyncio pipe transports) and `WebSocketTransport(ws_url)` (a DevTools
  port's browser endpoint, for the attach). `CDPClient`: `await
  connect(transport)` (the browser endpoint), `await attach_page()` (finds
  the music.apple.com page through `Target.setDiscoverTargets` and
  `Target.getTargets`, sends another page there or opens one, attaches with
  `Target.attachToTarget({flatten: true})`, then enables `Runtime`, `Page`
  and `Inspector` and adds the `__amEvent` binding on that session; the page
  crashing, closing or detaching loses the connection, and a failed attach
  closes it), `await call(method, params, timeout, browser=False)`, `await
  evaluate(js, await_promise, timeout)` (by value, NaN/Infinity/-0/BigInt
  mapped; a JS exception is `EngineError('api')`),
  `await bridge(method, *args)` (`window.__appleMusicLibrary.method(...)`;
  it waits while the bridge is being put back, and is made again once it is
  when it found the bridge gone),
  `on(event, callback)` / `off` (callback gets `(name, data)`; a CDP method
  name, a bridge event as `'am:<name>'`, or the wildcards `'am:*'` and
  `'*'`), `await ensure_bridge()` (injects `bridge.js` when the page's
  `__version` differs from the file's hash, waits for MusicKit, and from then
  on puts the bridge back after every navigation of the page's main frame,
  seen as `Runtime.executionContextCreated`: up to four tries, the last after
  reloading the page, then `am:bridgeReset`, or the connection given up so the
  engine goes down rather than on without events), `await subscribe()` (bridge
  events on, re-done after navigations), `await close(reason)`, `await
  wait_closed()`, `connected`, `lost_reason` (why the connection went),
  `on_timeout(method)` (called when a call runs out of time). `await open_page(transport)` connects and
  attaches; `await attach_devtools(port)` does it through a DevTools port.
  Timeouts are `EngineError('timeout')`, a lost connection `'engine-down'`
  (pending calls included); a bad message or payload, or a handler that
  raises, is logged and skipped. Answers of 512 KB and more are parsed in a
  thread, in order.
- **`scripts/am.py`** — the debug CLI: `status`, `eval <js>`,
  `now-playing`, `events`. By default each command starts a Chrome of its
  own on the app's profile through the app's `Engine` (the pipe, on the
  GLib-backed loop) and stops it at the end; it refuses while the app's
  Chrome holds the profile. `--attach PORT` drives the running app through
  the DevTools port `APPLE_MUSIC_DEBUG_PORT` opened, over
  `WebSocketTransport`.

### Events

`bridge.subscribe()` attaches MusicKit listeners once and posts each event
through the binding as `{name, data}`; the client dispatches it as
`'am:<name>'`. All eleven names exist in `MusicKit.Events` of the live page
(checked 2026-09-27). The data, read from the instance at the moment of the
event:

| Event | data |
|---|---|
| `authorizationStatusDidChange` | `{authorized, status}` (status: MusicKit's number, or null) |
| `playbackStateDidChange` | `{state, position, duration}` — state is the `MusicKit.PlaybackStates` name: `none loading playing paused stopped ended seeking waiting stalled completed`; a play walks playing → waiting → loading → playing |
| `nowPlayingItemDidChange` | `{track: Track or null, index}` (the `now-playing` Track shape) |
| `playbackTimeDidChange` | `{position, duration}` in seconds, about four times a second while playing |
| `playbackDurationDidChange` | `{duration}` |
| `queueItemsDidChange` | `{index, items: [Track…]}` (the `queue` shape; index is -1 before playback starts) |
| `queuePositionDidChange` | `{index, oldIndex}` |
| `shuffleModeDidChange` | `{shuffle: "on"\|"off"}` |
| `repeatModeDidChange` | `{repeat: "none"\|"one"\|"all"}` |
| `playbackVolumeDidChange` | `{volume}` (0..1) |
| `mediaPlaybackError` | `{code, message}` (code: MusicKit's `errorCode`, e.g. `CONTENT_UNAVAILABLE`, else the error's name, else '') |
