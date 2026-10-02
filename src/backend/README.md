# The backend

Everything that reaches Chrome, MusicKit or Apple's API, below the app's `Engine`
(`src/engine.py`). The standard library and asyncio only: nothing here imports gi
(`tests/test_backend.py` checks), and importing the package imports none of its modules. The
app reaches Chrome only through the Engine; other modules may import the pure parts (`api`,
`config`, `errors`, `normalize`, `store`).

## What is here

| File | What it holds |
|---|---|
| `errors.py` | `EngineError(code, message, status=None, musickit_code=None)`, the one error that leaves the backend, and its codes |
| `api.py` | the Apple Music API apart from any connection: library ids, an item's endpoint, resource types, the endpoints the pages use, Apple's `{errors}` answers as an `EngineError` (`api_error()`), and which failures are final (`is_final()`) |
| `chrome.py` | finding Chrome (`find_chrome()`), its argv (`chrome_args()`: `--remote-debugging-pipe`, `--headless=new`, an `--app=` window when visible; `flatpak-spawn --host` from a Flatpak sandbox), its environment (`chrome_environment()`: the session bus of `APPLE_MUSIC_HOST_SESSION_BUS`), whether a profile's `Local State` records the keyring's key (`profile_used_keyring()`), which Chrome holds a profile (`profile_owner()`, from its `SingletonLock`), `select_page()`, and `get_json()` for the developer attach |
| `client.py` | the transports (`PipeTransport`, Chrome's DevTools pipe on its descriptors 3 and 4; `WebSocketTransport`, a DevTools port) and `CDPClient`: one asynchronous CDP connection attached to the music.apple.com page, its calls, events, and the bridge kept in the page across navigations |
| `cdp.py` | the RFC 6455 handshake and frame codec as pure functions, for `WebSocketTransport`, and `exception_message()`, a page's JavaScript exception in one line |
| `bridge.js` | the only code that runs in the page: `window.__appleMusicLibrary` over the page's MusicKit instance, and MusicKit's events posted through the `__amEvent` binding |
| `normalize.py` | Apple's answers as the library's Item, Track and shelf shapes; the artwork cache (naming, fetching, pruning); library.json; the kept answers; `prune_caches()` |
| `store.py` | every write into the cache: `atomic_write()` / `atomic_create()`, the cache's generation (`cache_generation()`, `bump_cache_generation()`), `CacheGone` and `Cancelled` |
| `config.py` | the cache and profile directories for each build profile, the developer's DevTools port, Chrome's session bus when the app runs on another (`host_session_bus()`), the artwork sizes, `BRIDGE_JS` |

## Error codes

`backend/errors.py`; `src/errors.py`'s `error_message()` gives each one its sentence and button.

| Code | Meaning |
|---|---|
| `engine-down` | Chrome is not running, exited, or the connection closed (also every command in demo mode) |
| `no-browser` | no Google Chrome to run, or it could not be started |
| `no-keyring` | the keyring that encrypts the profile's sign-in is not reachable on Chrome's session bus: Chrome was not started, as it would delete the sign-in |
| `not-signed-in` | MusicKit is not authorized |
| `api` | Apple, MusicKit or the page said no (`status`: Apple's HTTP status; `musickit_code`: MusicKit's code for a refused play) |
| `timeout` | Chrome or the page took too long |
| `usage` | a bad argument (`scripts/am.py`) |

## The bridge

`client.load_bridge()` injects `bridge.js` with its hash as `__appleMusicLibraryWanted`: the same
file injected twice is a no-op, a changed one replaces the old. `CDPClient.bridge(name, *args)`
calls `window.__appleMusicLibrary.<name>(...)`; the Engine's commands are thin calls into these.

| Call | Answer |
|---|---|
| `status()` | `{ready, engine, authorized, storefront, bitrate}` |
| `accountName()` | the account's name as the page shows it (`.account-menu .user__name`), or null; null while a sign-in control is on the page |
| `signin()` | `mk.authorize()`, then `{authorized}` |
| `signout()` | `mk.unauthorize()`, which revokes the session at Apple's end: `{ok: true}` or `{error}`, never a throw |
| `api(path, params, options)`, `apiAll(paths)` | the API's answer body (`mk.api.music()`); `apiAll` answers null in place of a failed read |
| `play(kind, id, {startWith, startId, shuffle})` | `{ok: true}` (with `moved: {from, to}` when the queue held another item than `startId` at `startWith` and was moved to it), or `{error, code}` when MusicKit refuses. `kind`: `album playlist station song musicVideo artist songs` (`songs`: ids joined by commas; `artist`: its top songs, else its station). `shuffle` true turns shuffle on, false off, null leaves it |
| `playNext(kind, id)`, `playLater(kind, id)` | `{ok: true}` |
| `control(action)` | `play pause toggle next previous stop`: `{ok: true}` |
| `seek(seconds)` | `{ok: true}` |
| `volume(level)` | `{volume}`: MusicKit's level after the set (0 to 1; the page keeps it) |
| `shuffle(on\|off\|toggle)`, `repeat(none\|one\|all\|cycle)` | `{shuffle, repeat}` |
| `nowPlaying()` | `{state, track, position, duration, shuffle, repeat, volume}`; `state` is MusicKit's `PlaybackStates` name |
| `queue()` | `{index, items: [Track]}` |
| `queueJump(index)` | `{ok: true}` (`mk.changeToMediaAtIndex`) |
| `rating(kind, id, love)`, `addToLibrary(kind, id)`, `addToPlaylist(playlistId, songId, type)` | `{ok: true}`, or a throw with Apple's status and message. Writes go through `mk.api.client.createRequest(...).send()`: Apple answers them 202 or 204 with no body, which `music()` cannot read |
| `lyrics(catalogSongId)` | `{synced, lines: [{startMs, endMs, text, stanza?}]}` (the TTML read with the page's XML parser; `stanza: true` on a verse's first line) |
| `search(term, limit)`, `suggest(term, limit)`, `searchLanding()`, `category(id)` | the API's answer, shaped by `normalize.search_results()`, `search_suggestions()`, `search_landing()`, `category_page()` |
| `subscribe()`, `unsubscribe()` | MusicKit's listeners on or off, once per instance |

## Events

The client dispatches each bridge event as `am:<name>`; the Engine re-emits it as
`event(name, data)`. The data is read from the instance at the moment of the event.

| Event | Data |
|---|---|
| `authorizationStatusDidChange` | `{authorized, status}` |
| `playbackStateDidChange` | `{state, position, duration}`; a play walks `playing`, `waiting`, `loading`, `playing` |
| `nowPlayingItemDidChange` | `{track: Track or null, index}` |
| `playbackTimeDidChange` | `{position, duration}` in seconds, about four times a second while playing |
| `playbackDurationDidChange` | `{duration}` |
| `queueItemsDidChange` | `{index, items: [Track]}` (index -1 before playback starts) |
| `queuePositionDidChange` | `{index, oldIndex}` |
| `shuffleModeDidChange`, `repeatModeDidChange` | `{shuffle: "on"\|"off"}`, `{repeat: "none"\|"one"\|"all"}` |
| `playbackVolumeDidChange` | `{volume}` |
| `mediaPlaybackError` | `{code, message}` (`code`: MusicKit's `errorCode`, such as `CONTENT_UNAVAILABLE`, else '') |
| `bridgeReset` | `{}`: the client's own, after it put the bridge back into a new document |

## library.json

`<cache>/library.json`, written by `src/sync.py` (and `scripts/demo_library.py`), read by
`src/library.py`. Version 2: an album's track `index` counts on across its discs; the app
syncs an older file again at once.

```jsonc
{
  "version": 2, "generated": "2026-09-25T12:00:00Z", "storefront": "us",
  "sections": {"albums": [Item], "artists": [Item], "playlists": [Item], "radio": [Item],
               "videos": [Item]},
  "shelves": [{"key": "rec-<id>", "title": "…", "items": [Item]},   // Apple's recommendations
              {"key": "heavy-rotation", "title": "", …}, {"key": "recently-added", …}],
                                                // empty titles: the app titles them by key
  "folders": [{"id": "root", "title": "", "parent": null,
               "children": [{"kind": "playlist", "id": "p.…"}, {"kind": "folder", "id": "p.…"}]}]
}

Item = {
  "id": "l.…",                  // library id, or catalog id when not in the library
  "kind": "album" | "playlist" | "artist" | "station" | "video" | "song" | "link",
  "title": "…", "subtitle": "…", "year": 2007, "genre": "…", "summary": "…" /* or null */,
  "art": "<cache>/art/<sha1>.jpg" /* or null */,     // 640 px, the pages' hero
  "thumb": "<cache>/thumb/<sha1>.jpg" /* or null */, // 320 px, tiles and rows
  "artUrl": "https://…/640x640bb.jpg",               // for fetching `art` on demand
  "artColor": "#1a1a1a" /* or null */, "explicit": false,
  "trackCount": 12, "durationMs": 2580000,           // albums, playlists (videos: length)
  "albumCount": 3,                                   // artists
  "catalogId": "…" /* or null */, "url": "https://music.apple.com/…" /* or null */,
  "attributes": {"isFavourites": true, "canEdit": false},   // playlists, when they apply
  "modified": "2025-05-01T09:00:00Z",                // playlists: Apple's lastModifiedDate as
                                                     // listed when `groups` was last read; the
                                                     // sync keeps them while it holds (sync.py)
  "play": {"kind": "album", "id": "l.…"},            // what the bridge's play() takes
  "groups": [{"name": "Disc 1", "play": {…}, "entries": [Track]}]   // an album's discs, a
                                                     // playlist's "Tracks", an artist's albums
}
Track = {"id": "i.…", "catalogId": "…" /* or null */, "title": "…", "artist": "…",
         "album": "…", "trackNumber": 1, "discNumber": 1, "durationMs": 216000,
         "durationLabel": "3:36", "explicit": true,
         "index": 0,                     // its place in its group's `play` queue
         "thumb": "…" /* or null */,     // a playlist's rows
         "type": "library-songs"}        // the API's: songs, music-videos, library-…, or ''
                                         // (the item playing may carry MusicKit's own
                                         // 'song' or 'musicVideo' instead)
```

An artist's page (`Engine.artist_page()`, `normalize.artist_page()`) is
`{id, artist, latest: {key, title, item} | null, topSongs: {key, title, items, more} | null,
shelves: [{key, title, items, more}]}`: `artist` an Item without groups whose `summary` is
the biography, with `origin`, `bornOrFormed` (Apple's own words) and `isGroup` where Apple
has them; `latest` the featured or latest release, with its `releaseDate`; the top songs
song Items with their `album`; each shelf one of the artist's views in music.apple.com's
order, titled as Apple titles it, `more` when `Engine.artist_view()` has more of it. A
video about the artist (an interview, a film) is a `link` Item: its `url` is its page,
its subtitle its length, and it has nothing to play.

The app shows none of the data's own words: a missing name is '', the app names shelves by
key, writes the captions from the counts and titles an album's discs from `discNumber`. A song without an album of its own sits under a stand-in album
(`l.alb_…`) that plays `{"kind": "songs", "id": "<id>,<id>…"}`; a video plays as MusicKit's
`musicVideo`. Older files may hold an English `countLabel` or `sections.songs` (loose Tracks);
the model still reads both.

## The caches

All under `config.cache_dir()`, every file written through `store.py` (a dot-named temporary
file beside it, renamed over it; directories 0700, files 0600), with the writer's cache
generation, so a write after Clear Cache or sign-out lands nowhere.

| Path | Written by | Read by | Kept |
|---|---|---|---|
| `library.json` | the sync | `library.py` | until the next sync |
| `art/`, `thumb/`, `art/.sizes` | the sync (thumbnails), the pages (covers) | the artwork loader | pruned against library.json after every sync |
| `remote-art/` | `src/remote.py`, `Engine.item()` | the pages, MPRIS | trimmed to 32 MB, oldest first |
| `lyrics/<id>.json` | `Engine.lyrics()` | the same | fetched again after 30 days; the 2,000 played last |
| `landing.json`, `categories/`, `browse.json`, `made-for-you.json`, `artists/` | `Engine._kept_answer()` | the same, `cache.read_kept()` | a day; an older one only when Apple cannot be asked (`stale: True`); in demo mode, only what the demo library invented (marked `demo`), at any age |

`normalize.prune_caches()` runs after the library's first load and after every sync; it also
removes what older versions kept (`items/`) and temporary files over an hour old.

## Environment

Read by `config.py` on every call:

| Variable | What it does |
|---|---|
| `APPLE_MUSIC_CACHE` | the cache directory (default `$XDG_CACHE_HOME/apple-music`, `apple-music-devel` for the development build) |
| `APPLE_MUSIC_PROFILE` | Chrome's profile (default `$XDG_DATA_HOME/apple-music/chrome`, `chrome-devel` for the development build) |
| `APPLE_MUSIC_DEBUG_PORT` | also opens a DevTools port on 127.0.0.1, for `scripts/am.py --attach`. Developers only: while it is set, any local program can drive the signed-in session |
| `APPLE_MUSIC_HOST_SESSION_BUS` | the D-Bus address Chrome runs with as its session bus when the app runs on another one (`scripts/headless.sh` sets it to the desktop's): the keyring that encrypts the profile's cookies answers there. Unset, Chrome inherits the app's |

## Provenance

Forked on 2026-09-27 from the backend of the owner's GNOME Shell extension, Apple Music
Library, at its commit 906bfa9 (`cdp.py`, `bridge.js`, `sync.py`, now `normalize.py`, and this
README). This app has rewritten most of it since and maintains it for itself; the history is
`git log -- src/backend`.
