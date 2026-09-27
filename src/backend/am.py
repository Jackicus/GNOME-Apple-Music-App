# REFERENCE ONLY. Vendored from the GNOME Shell extension, where it was the
# process-per-command CLI the shell spawned. This app keeps one long-running
# asynchronous CDP connection instead, so nothing here is installed, imported
# (the package's __init__ does not import it) or linted (pyproject.toml), and
# it is not meant to run. Its engine start/stop, sync orchestration and
# command bodies are what phases 9 to 11 of prompts.md port into the app;
# delete it once they have. Edits: package imports, and config.py in place
# of the extension's GSettings schema (see src/backend/__init__.py).
"""Apple Music CLI driver for Apple Music Library GNOME Shell extension.

Every command prints ONE JSON object on stdout and exits 0.
On failure prints {"error": "<code>", "message": "..."} and exits 1.
Error codes: engine-down, not-signed-in, api, timeout, usage.
"""

from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import sys
import time

from .cdp import CDPClient, connect_to_chrome, http_get_json
from . import config, sync

# Nothing else is imported at load. Every command is its own process, and
# most — the player's poll, a search, a volume set — are over in a moment
# once the interpreter is up; what starting or stopping Chrome needs is
# imported by the two functions that do that.


class AmError(Exception):
    """Custom error matching the Apple Music Library error contract."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = str(message)


# ---------------------------------------------------------------------------
# Path & Environment Resolution
# ---------------------------------------------------------------------------


# Paths and the port come from config.py (environment overrides included).
def get_cache_dir():
    return str(config.cache_dir())


def get_profile_dir():
    return str(config.profile_dir())


def get_state_file():
    return str(config.state_file())


# The extension read these from its own GSettings schema through gi; the
# backend imports no gi, so they are the defaults. The app's own keys arrive
# with the Engine (phase 10) and the sync (phase 11), read in the app.
SETTING_DEFAULTS = {
    "browser-command": "google-chrome-stable",
    "engine-port": config.DEFAULT_PORT,
    "engine-headless": True,
    "engine-autostart": True,
    "cover-size": config.COVER_SIZE,
    "thumb-size": config.THUMB_SIZE,
}


def get_setting(key):
    """A setting's default."""
    return SETTING_DEFAULTS[key]


def set_setting(key, value):
    """Nothing: there are no settings to write here (last-sync is the app's)."""


def get_port():
    return config.port()


# ---------------------------------------------------------------------------
# Engine Process Management
# ---------------------------------------------------------------------------


def is_pid_running(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_state():
    state_file = get_state_file()
    if not os.path.exists(state_file):
        return None
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state):
    state_file = get_state_file()
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    tmp_path = state_file + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp_path, state_file)


def remove_state():
    state_file = get_state_file()
    if os.path.exists(state_file):
        try:
            os.remove(state_file)
        except OSError:
            pass


def is_port_responding(port, timeout=1):
    try:
        http_get_json(port, "/json", timeout=timeout)
        return True
    except Exception:
        return False


def engine_status():
    """{running, pid, port, headless}. A running engine is described from its
    state file alone, so the common case costs no settings lookup."""
    state = get_state()
    if state:
        pid = state.get("pid")
        port = state.get("port") or get_port()
        if is_pid_running(pid) and is_port_responding(port):
            return {"running": True, "pid": pid, "port": port, "headless": bool(state.get("headless", True))}
    return {"running": False, "pid": None, "port": get_port(), "headless": get_setting("engine-headless")}


def engine_stop():
    import signal
    state = get_state()
    if state:
        pid = state.get("pid")
        if pid and is_pid_running(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(30):
                    time.sleep(0.1)
                    if not is_pid_running(pid):
                        break
                if is_pid_running(pid):
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    remove_state()
    return {"running": False, "pid": None, "port": get_port(), "headless": get_setting("engine-headless")}


def wait_for_chrome(port, deadline_sec=15):
    """Poll until Chrome's debugging port accepts a CDP connection.

    Chrome takes a moment after exec to open its debug port, so a single
    connect attempt right after Popen() usually meets connection refused.
    Retries until deadline_sec elapses, returning a connected CDPClient or
    None on timeout.
    """
    deadline = time.time() + deadline_sec
    last_exc = None
    while time.time() < deadline:
        try:
            return connect_to_chrome(port, timeout=2)
        except Exception as e:
            last_exc = e
            time.sleep(0.2)
    return None


def ensure_bridge(client, timeout=15):
    """Inject bridge.js into page and poll until MusicKit is ready."""
    with open(config.BRIDGE_JS, "r", encoding="utf-8") as f:
        bridge_code = f.read()
    version = hashlib.sha1(bridge_code.encode("utf-8")).hexdigest()[:12]
    bridge_code = f"window.__appleMusicLibraryWanted = {json.dumps(version)};\n" + bridge_code

    # The page navigates on its own while it starts (music.apple.com redirects
    # to /<storefront>/new), and a navigation wipes `window`, so an injection
    # made during start-up is lost. Keep re-injecting (it is idempotent) until
    # the bridge reports MusicKit ready. A page that already has this bridge
    # answers the probe alone, which spares every later command the source.
    probe = (f"(window.__appleMusicLibrary && window.__appleMusicLibrary.__version === {json.dumps(version)})"
             " ? window.__appleMusicLibrary.status() : null")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st = client.evaluate(probe, await_promise=False)
            if st and st.get("ready"):
                return
            client.evaluate(bridge_code, await_promise=False)
            st = client.evaluate("window.__appleMusicLibrary ? window.__appleMusicLibrary.status() : null")
            if st and st.get("ready"):
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise AmError("timeout", "music.apple.com did not become ready (MusicKit not loaded)")


def engine_start(headless=None):
    import shutil
    import subprocess

    if headless is None:
        headless = get_setting("engine-headless")

    curr = engine_status()
    if curr["running"]:
        if curr["headless"] == headless:
            try:
                client = connect_to_chrome(curr["port"], timeout=5)
                try:
                    ensure_bridge(client, timeout=15)
                finally:
                    client.close()
            except Exception:
                pass
            return curr
        else:
            engine_stop()

    browser_cmd = get_setting("browser-command")
    browser_bin = shutil.which(browser_cmd)
    if not browser_bin:
        for candidate in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
            b = shutil.which(candidate)
            if b:
                browser_bin = b
                break
    if not browser_bin:
        raise AmError("engine-down", f"Browser command not found: {browser_cmd}")

    profile_dir = get_profile_dir()
    os.makedirs(profile_dir, exist_ok=True)
    port = get_port()

    args = [
        browser_bin,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--autoplay-policy=no-user-gesture-required",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args.append("--headless=new")
        args.append("https://music.apple.com/")
    else:
        args.append("--app=https://music.apple.com/")

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        raise AmError("engine-down", f"Failed to start Chrome: {e}")

    state = {
        "pid": proc.pid,
        "port": port,
        "headless": headless,
        "profile": profile_dir,
        "started": int(time.time()),
    }
    save_state(state)

    client = wait_for_chrome(port, deadline_sec=15)
    if client is None:
        engine_stop()
        raise AmError("timeout", f"Timed out waiting for Chrome to open its debugging port {port}")

    try:
        ensure_bridge(client, timeout=15)
    finally:
        client.close()

    return {"running": True, "pid": proc.pid, "port": port, "headless": headless}


def ensure_engine_running(no_start=False):
    st = engine_status()
    if st["running"]:
        return st
    if no_start:
        raise AmError("engine-down", "Chrome engine is not running and --no-start was passed")
    if not get_setting("engine-autostart"):
        raise AmError("engine-down", "Chrome engine is not running and autostart is disabled")
    return engine_start()


def get_bridge_client(no_start=False):
    st = ensure_engine_running(no_start=no_start)
    port = st["port"]
    try:
        client = connect_to_chrome(port, timeout=15)
    except Exception as e:
        raise AmError("engine-down", f"Could not connect to Chrome on port {port}: {e}")
    ensure_bridge(client, timeout=15)
    return client


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------


def handle_signin():
    engine_stop()
    engine_start(headless=False)
    port = get_port()
    try:
        client = connect_to_chrome(port, timeout=15)
    except Exception as e:
        raise AmError("timeout", f"Failed to connect to visible Chrome: {e}")

    try:
        ensure_bridge(client, timeout=15)
        res = client.evaluate("window.__appleMusicLibrary.signin()", await_promise=True, timeout=120)
        auth = bool(res.get("authorized")) if isinstance(res, dict) else False
        return {"authorized": auth}
    finally:
        client.close()


def handle_status():
    st = engine_status()
    if not st["running"]:
        return {"engine": False, "authorized": False, "storefront": "", "bitrate": 0}

    try:
        client = connect_to_chrome(st["port"], timeout=5)
        try:
            ensure_bridge(client, timeout=5)
            res = client.evaluate("window.__appleMusicLibrary.status()")
            return {
                "engine": True,
                "authorized": bool(res.get("authorized")),
                "storefront": str(res.get("storefront", "us")),
                "bitrate": int(res.get("bitrate", 256)),
            }
        finally:
            client.close()
    except Exception:
        return {"engine": False, "authorized": False, "storefront": "", "bitrate": 0}


def _log(message):
    """A line on stderr: am.py's stdout is the one JSON object, nothing else."""
    sys.stderr.write(f"am.py: {message}\n")
    sys.stderr.flush()


def _api(client, path, params=None, retries=3):
    """One Apple Music API call through the bridge, retried.

    MusicKit answers a failed request with `{"errors": [...]}` and a 200, so
    that is a failure here as much as a thrown promise is. Each retry waits a
    little longer than the last; the final failure raises AmError("api").
    """
    expr = f"window.__appleMusicLibrary.api({json.dumps(path)}, {json.dumps(params or {})})"
    last = None
    for attempt in range(max(1, retries)):
        try:
            res = client.evaluate(expr, await_promise=True)
            if isinstance(res, dict) and res.get("errors"):
                first = res["errors"][0] if isinstance(res["errors"], list) and res["errors"] else {}
                raise AmError("api", f"{first.get('status', '?')} {first.get('title', 'error')}: {first.get('detail', '')}".strip())
            return res if isinstance(res, dict) else {}
        except Exception as e:
            last = e
            if attempt + 1 < retries:
                time.sleep(0.5 * (2 ** attempt))
    raise AmError("api", f"{path}: {last}")


def _api_all(client, path, params=None, page=100, limit=None):
    """Every page of a paged endpoint, following `next` by offset."""
    out = []
    offset = 0
    while True:
        p = dict(params or {}, limit=page, offset=offset)
        res = _api(client, path, p)
        data = res.get("data") or []
        out.extend(data)
        if not data or not res.get("next") or (limit and len(out) >= limit):
            break
        offset += len(data)
    return out


def _previous_library(cache_dir):
    """What library.json holds now, or {} — a failed fetch keeps its old entry."""
    try:
        with open(os.path.join(cache_dir, "library.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def handle_sync(only=None, no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        st = client.evaluate("window.__appleMusicLibrary.status()")
        if not st or not st.get("authorized"):
            raise AmError("not-signed-in", "User is not signed in to Apple Music")

        cache_dir = get_cache_dir()
        # The sizes this sync builds at, from the settings, before anything
        # names a file: a changed thumbnail size wipes the old thumbnails.
        sync.apply_art_sizes(cache_dir, get_setting("cover-size"), get_setting("thumb-size"))
        previous = _previous_library(cache_dir)
        counts = {"albums": 0, "artists": 0, "playlists": 0, "radio": 0, "shelves": 0}
        sections = {}
        shelves = []

        # 1. Albums & Artists from songs
        if not only or only in ("albums", "artists"):
            songs = _api_all(client, "/v1/me/library/songs", {"include": "albums"})
            albums, artists = sync.group_songs_into_albums_and_artists(songs, cache_dir)
            sections["albums"] = albums
            sections["artists"] = artists
            counts["albums"] = len(albums)
            counts["artists"] = len(artists)

        # 2. Playlists
        if not only or only == "playlists":
            raw_playlists = _api_all(client, "/v1/me/library/playlists")
            old_playlists = {
                p.get("id"): p for p in (previous.get("sections") or {}).get("playlists") or []
                if isinstance(p, dict)
            }
            playlists = []
            for p in raw_playlists:
                p_id = p.get("id")
                try:
                    tracks = _api_all(client, f"/v1/me/library/playlists/{p_id}/tracks")
                except AmError as e:
                    # The listing stands; the tracks it had last time stay
                    # rather than turning into an empty playlist.
                    _log(f"sync: playlist {p_id} tracks: {e.message}")
                    old = old_playlists.get(p_id)
                    if old and old.get("groups"):
                        playlists.append(old)
                        continue
                    tracks = []
                playlists.append(sync.normalize_playlist(p, cache_dir, tracks=tracks))
            sections["playlists"] = playlists
            counts["playlists"] = len(playlists)

        # 3. Radio
        if not only or only == "radio":
            try:
                raw_stations = _api(client, "/v1/me/recent/radio-stations").get("data") or []
            except AmError as e:
                _log(f"sync: radio: {e.message}")
                raw_stations = None
            if raw_stations is None:
                sections["radio"] = (previous.get("sections") or {}).get("radio") or []
            else:
                sections["radio"] = [sync.normalize_station(st_obj, cache_dir) for st_obj in raw_stations]
            counts["radio"] = len(sections["radio"])

        # 4. Shelves: Apple's own home page first — one shelf per
        # recommendation, in Apple's order and with Apple's titles, Recently
        # Played among them — then the library's own Heavy Rotation and
        # Recently Added, which the home page does not carry.
        if not only or only == "shelves":
            old_shelves = [sh for sh in previous.get("shelves") or [] if isinstance(sh, dict)]
            try:
                raw_recs = _api(client, "/v1/me/recommendations", {"limit": 25}).get("data") or []
                shelves.extend(sync.recommendation_shelves(raw_recs, cache_dir))
            except AmError as e:
                # Last time's stand, rather than a home page with nothing on it.
                _log(f"sync: recommendations: {e.message}")
                shelves.extend(sh for sh in old_shelves if str(sh.get("key", "")).startswith("rec-"))
            # Each endpoint has a page cap of its own (a bigger `limit` is a
            # 400, not a clamp): 10 for heavy rotation.
            shelf_defs = [
                ("heavy-rotation", "Heavy Rotation", "/v1/me/history/heavy-rotation", 10),
                ("recently-added", "Recently Added", "/v1/me/library/recently-added", 25),
            ]
            for key, title, endpoint, page in shelf_defs:
                try:
                    raw_items = _api(client, endpoint, {"limit": page}).get("data") or []
                    items = [sync.normalize_item(it, cache_dir, include_groups=False) for it in raw_items]
                except AmError as e:
                    _log(f"sync: shelf {key}: {e.message}")
                    items = next((sh.get("items") or [] for sh in old_shelves if sh.get("key") == key), [])
                shelves.append({"key": key, "title": title, "items": items})
            counts["shelves"] = sum(len(s["items"]) for s in shelves)

        now_iso = datetime.now(timezone.utc).isoformat()
        lib_data = {
            "version": 1,
            "generated": now_iso,
            "storefront": st.get("storefront", "us"),
            "sections": sections,
            "shelves": shelves,
        }
        # The artwork first, in threads, and the listing only once it is on
        # disk: the shell rebuilds on every write of library.json and looks
        # for a tile's artwork as it builds the tile, so a listing written
        # ahead of its covers showed placeholders until the next sync. A
        # failure fetching is only logged; the listing is written regardless.
        try:
            counts["art"] = sync.download_art(lib_data, cache_dir, log=_log)
        except Exception as e:
            _log(f"sync: artwork: {e}")
            counts["art"] = {"wanted": 0, "fetched": 0, "failed": 0}
        sync.save_library(lib_data, cache_dir, only=only)
        # The pruning last, against the merged file; and the covers the
        # shell fetched for itself, trimmed to a budget.
        sync.prune_art(_previous_library(cache_dir) or lib_data, cache_dir)
        sync.prune_remote_art(cache_dir)
        set_setting("last-sync", now_iso)

        return {"counts": counts, "generated": now_iso}
    finally:
        client.close()


def handle_item(kind, item_id, no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        st = client.evaluate("window.__appleMusicLibrary.status()")
        sf = st.get("storefront", "us") if st else "us"
        cache_dir = get_cache_dir()
        sync.load_art_sizes(cache_dir)

        is_lib = item_id.startswith("l.") or item_id.startswith("p.") or item_id.startswith("r.")
        if kind == "album":
            endpoint = f"/v1/me/library/albums/{item_id}?include=tracks,artists" if is_lib else f"/v1/catalog/{sf}/albums/{item_id}?include=tracks,artists"
        elif kind == "playlist":
            endpoint = f"/v1/me/library/playlists/{item_id}?include=tracks" if is_lib else f"/v1/catalog/{sf}/playlists/{item_id}?include=tracks"
        elif kind == "artist":
            endpoint = f"/v1/me/library/artists/{item_id}?include=albums" if is_lib else f"/v1/catalog/{sf}/artists/{item_id}?include=albums"
        elif kind == "station":
            endpoint = f"/v1/catalog/{sf}/stations/{item_id}"
        elif kind == "song":
            endpoint = f"/v1/me/library/songs/{item_id}" if is_lib else f"/v1/catalog/{sf}/songs/{item_id}"
        else:
            endpoint = f"/v1/catalog/{sf}/{kind}s/{item_id}"

        try:
            res = client.evaluate(f"window.__appleMusicLibrary.api({json.dumps(endpoint)})", await_promise=True)
        except Exception as e:
            raise AmError("api", f"API error fetching item {kind} {item_id}: {e}")

        data = res.get("data", []) if res else []
        if not data:
            raise AmError("api", f"Item not found: {kind} {item_id}")

        raw_obj = data[0]
        if kind == "artist":
            # The artist resource itself carries the artist's own attributes;
            # its included albums are stubs (no track relationships), so
            # fetch each album's full track list before normalizing.
            artist_obj = {
                "id": item_id,
                "type": raw_obj.get("type", "artists"),
                "attributes": raw_obj.get("attributes", {}),
            }
            album_stubs = raw_obj.get("relationships", {}).get("albums", {}).get("data", [])
            endpoints = []
            for stub in album_stubs:
                alb_id = stub.get("id")
                alb_is_lib = isinstance(alb_id, str) and (alb_id.startswith("l.") or alb_id.startswith("p."))
                endpoints.append(
                    f"/v1/me/library/albums/{alb_id}?include=tracks"
                    if alb_is_lib
                    else f"/v1/catalog/{sf}/albums/{alb_id}?include=tracks"
                )
            # All at once in the page, not one round trip per album: an
            # artist with a couple of dozen albums took seconds one by one.
            try:
                answers = client.evaluate(f"window.__appleMusicLibrary.apiAll({json.dumps(endpoints)})",
                                          await_promise=True, timeout=60)
            except Exception:
                answers = []
            if not isinstance(answers, list):
                answers = []
            full_albums = []
            for i, stub in enumerate(album_stubs):
                res_i = answers[i] if i < len(answers) else None
                alb_data = res_i.get("data", []) if isinstance(res_i, dict) else []
                full_albums.append(alb_data[0] if alb_data else stub)
            item = sync.normalize_artist(artist_obj, cache_dir, albums=full_albums)
        else:
            item = sync.normalize_item(raw_obj, cache_dir, include_groups=True)
        # Fetched fresh whenever asked, and the answer kept where the shell
        # reads it back without a process: the second look at a shelf's
        # album is instant, and the first after a restart too.
        sync.download_item_art(item, cache_dir)
        return sync.write_answer(sync.item_cache_path(cache_dir, kind, item_id), item)
    finally:
        client.close()


def handle_play(kind, item_id, start_with=0, shuffle=False, no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        opts = {"startWith": start_with, "shuffle": shuffle}
        expr = f"window.__appleMusicLibrary.play({json.dumps(kind)}, {json.dumps(item_id)}, {json.dumps(opts)})"
        client.evaluate(expr, await_promise=True)
        return {"ok": True}
    except Exception as e:
        raise AmError("api", f"Play failed: {e}")
    finally:
        client.close()


def handle_play_next(kind, item_id, no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        expr = f"window.__appleMusicLibrary.playNext({json.dumps(kind)}, {json.dumps(item_id)})"
        client.evaluate(expr, await_promise=True)
        return {"ok": True}
    except Exception as e:
        raise AmError("api", f"Play next failed: {e}")
    finally:
        client.close()


def handle_play_later(kind, item_id, no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        expr = f"window.__appleMusicLibrary.playLater({json.dumps(kind)}, {json.dumps(item_id)})"
        client.evaluate(expr, await_promise=True)
        return {"ok": True}
    except Exception as e:
        raise AmError("api", f"Play later failed: {e}")
    finally:
        client.close()


def handle_control(action, no_start=False):
    if action not in ("play", "pause", "toggle", "next", "previous", "stop"):
        raise AmError("usage", f"Invalid control action: {action}")
    client = get_bridge_client(no_start=no_start)
    try:
        client.evaluate(f"window.__appleMusicLibrary.control({json.dumps(action)})", await_promise=True)
        return {"ok": True}
    except Exception as e:
        raise AmError("api", f"Control {action} failed: {e}")
    finally:
        client.close()


def handle_seek(sec, no_start=False):
    try:
        val = float(sec)
    except ValueError:
        raise AmError("usage", f"Invalid seek seconds: {sec}")
    client = get_bridge_client(no_start=no_start)
    try:
        client.evaluate(f"window.__appleMusicLibrary.seek({val})", await_promise=True)
        return {"ok": True}
    except Exception as e:
        raise AmError("api", f"Seek failed: {e}")
    finally:
        client.close()


def handle_volume(val, no_start=False):
    try:
        vol = float(val)
        if not (0.0 <= vol <= 1.0):
            raise ValueError()
    except ValueError:
        raise AmError("usage", f"Volume must be a number between 0 and 1: {val}")
    client = get_bridge_client(no_start=no_start)
    try:
        res = client.evaluate(f"window.__appleMusicLibrary.volume({vol})", await_promise=True)
        return res if isinstance(res, dict) else {"volume": vol}
    except Exception as e:
        raise AmError("api", f"Volume change failed: {e}")
    finally:
        client.close()


def handle_shuffle(mode, no_start=False):
    if mode not in ("on", "off", "toggle"):
        raise AmError("usage", f"Invalid shuffle mode: {mode}")
    client = get_bridge_client(no_start=no_start)
    try:
        res = client.evaluate(f"window.__appleMusicLibrary.shuffle({json.dumps(mode)})", await_promise=True)
        return res if isinstance(res, dict) else {"shuffle": "off", "repeat": "none"}
    except Exception as e:
        raise AmError("api", f"Shuffle failed: {e}")
    finally:
        client.close()


def handle_repeat(mode, no_start=False):
    if mode not in ("none", "one", "all", "cycle"):
        raise AmError("usage", f"Invalid repeat mode: {mode}")
    client = get_bridge_client(no_start=no_start)
    try:
        res = client.evaluate(f"window.__appleMusicLibrary.repeat({json.dumps(mode)})", await_promise=True)
        return res if isinstance(res, dict) else {"shuffle": "off", "repeat": "none"}
    except Exception as e:
        raise AmError("api", f"Repeat failed: {e}")
    finally:
        client.close()


def handle_now_playing(no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        res = client.evaluate("window.__appleMusicLibrary.nowPlaying()")
        return res if isinstance(res, dict) else {
            "state": "stopped",
            "track": None,
            "position": 0,
            "duration": 0,
            "shuffle": "off",
            "repeat": "none",
            "volume": 1,
        }
    except Exception as e:
        raise AmError("api", f"Now playing failed: {e}")
    finally:
        client.close()


def handle_queue(no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        res = client.evaluate("window.__appleMusicLibrary.queue()")
        return res if isinstance(res, dict) else {"index": 0, "items": []}
    except Exception as e:
        raise AmError("api", f"Queue failed: {e}")
    finally:
        client.close()


def handle_love(kind, item_id, love=True, no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        client.evaluate(f"window.__appleMusicLibrary.rating({json.dumps(kind)}, {json.dumps(item_id)}, {json.dumps(love)})", await_promise=True)
        return {"ok": True}
    except Exception as e:
        raise AmError("api", f"Rating failed: {e}")
    finally:
        client.close()


def handle_add_to_library(kind, item_id, no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        client.evaluate(f"window.__appleMusicLibrary.addToLibrary({json.dumps(kind)}, {json.dumps(item_id)})", await_promise=True)
        return {"ok": True}
    except Exception as e:
        raise AmError("api", f"Add to library failed: {e}")
    finally:
        client.close()


def handle_playlists(no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        res = client.evaluate("window.__appleMusicLibrary.playlists()", await_promise=True)
        return res if isinstance(res, dict) else {"items": []}
    except Exception as e:
        raise AmError("api", f"Playlists failed: {e}")
    finally:
        client.close()


def handle_add_to_playlist(playlist_id, song_id, no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        client.evaluate(f"window.__appleMusicLibrary.addToPlaylist({json.dumps(playlist_id)}, {json.dumps(song_id)})", await_promise=True)
        return {"ok": True}
    except Exception as e:
        raise AmError("api", f"Add to playlist failed: {e}")
    finally:
        client.close()


def handle_lyrics(song_id, no_start=False):
    cache_dir = get_cache_dir()
    lyrics_file = os.path.join(cache_dir, "lyrics", f"{song_id}.json")
    if os.path.exists(lyrics_file):
        try:
            with open(lyrics_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    client = get_bridge_client(no_start=no_start)
    try:
        res = client.evaluate(f"window.__appleMusicLibrary.lyrics({json.dumps(song_id)})", await_promise=True)
        if not isinstance(res, dict):
            res = {"synced": False, "lines": []}
        try:
            os.makedirs(os.path.dirname(lyrics_file), exist_ok=True)
            with open(lyrics_file, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
        except Exception:
            pass
        return res
    finally:
        client.close()


def handle_search(term, library=False, limit=20, suggest=0, no_start=False):
    """`suggest` > 0 asks for that many of Apple's completions of the term
    as well, in the same round trip, answered as `terms` beside the
    shelves — one process for the shell's search instead of two."""
    client = get_bridge_client(no_start=no_start)
    try:
        cache_dir = get_cache_dir()
        sync.load_art_sizes(cache_dir)
        if suggest > 0:
            res = client.evaluate(
                f"window.__appleMusicLibrary.searchAndSuggest({json.dumps(term)}, {json.dumps(library)}, "
                f"{int(limit)}, {int(suggest)})", await_promise=True)
            res = res if isinstance(res, dict) else {}
            out = sync.search_results(res.get("search"), cache_dir)
            out["terms"] = sync.search_suggestions(res.get("suggestions"), cache_dir)["terms"][:int(suggest)]
            return out
        res = client.evaluate(f"window.__appleMusicLibrary.search({json.dumps(term)}, {json.dumps(library)}, {int(limit)})", await_promise=True)
        return sync.search_results(res, cache_dir)
    except Exception as e:
        raise AmError("api", f"Search failed: {e}")
    finally:
        client.close()


def handle_suggest(term, limit=10, no_start=False):
    client = get_bridge_client(no_start=no_start)
    try:
        cache_dir = get_cache_dir()
        sync.load_art_sizes(cache_dir)
        res = client.evaluate(f"window.__appleMusicLibrary.suggest({json.dumps(term)}, {int(limit)})", await_promise=True)
        return sync.search_suggestions(res, cache_dir)
    except Exception as e:
        raise AmError("api", f"Suggestions failed: {e}")
    finally:
        client.close()


# How long a kept answer stands for: Apple's search page changes its
# categories rarely, and a category's shelves are the week's.
ANSWER_MAX_AGE = 24 * 60 * 60


def handle_landing(no_start=False):
    cache_dir = get_cache_dir()
    kept = sync.read_answer(sync.landing_cache_path(cache_dir), ANSWER_MAX_AGE)
    if kept is not None:
        return kept
    client = get_bridge_client(no_start=no_start)
    try:
        sync.load_art_sizes(cache_dir)
        res = client.evaluate("window.__appleMusicLibrary.searchLanding()", await_promise=True)
        answer = sync.search_landing(res, cache_dir)
    except Exception as e:
        raise AmError("api", f"Search landing failed: {e}")
    finally:
        client.close()
    return sync.write_answer(sync.landing_cache_path(cache_dir), answer)


def handle_category(category_id, no_start=False):
    cache_dir = get_cache_dir()
    kept = sync.read_answer(sync.category_cache_path(cache_dir, category_id), ANSWER_MAX_AGE)
    if kept is not None:
        return kept
    client = get_bridge_client(no_start=no_start)
    try:
        sync.load_art_sizes(cache_dir)
        res = client.evaluate(f"window.__appleMusicLibrary.category({json.dumps(category_id)})", await_promise=True)
        answer = sync.category_page(res, cache_dir)
    except Exception as e:
        raise AmError("api", f"Category failed: {e}")
    finally:
        client.close()
    return sync.write_answer(sync.category_cache_path(cache_dir, category_id), answer)


# ---------------------------------------------------------------------------
# CLI Argument Parsing & Dispatch
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse that reports usage errors as AmError('usage') instead of
    printing to stderr and exiting 2 -- every failure is one JSON object."""

    def error(self, message):
        raise AmError("usage", message)


def _build_parser():
    p = _Parser(prog="am.py", add_help=False)
    sub = p.add_subparsers(dest="command", parser_class=_Parser, required=True)

    def cmd(name):
        return sub.add_parser(name, add_help=False)

    engine = cmd("engine").add_subparsers(dest="sub", parser_class=_Parser, required=True)
    for name in ("start", "stop", "status"):
        s = engine.add_parser(name, add_help=False)
        if name == "start":
            mode = s.add_mutually_exclusive_group()
            mode.add_argument("--visible", dest="headless", action="store_false", default=None)
            mode.add_argument("--headless", dest="headless", action="store_true", default=None)

    cmd("signin")
    cmd("status")
    cmd("sync").add_argument("--only", choices=("albums", "artists", "playlists", "radio", "shelves"))

    for name in ("item", "play", "play-next", "play-later", "love", "unlove", "add-to-library"):
        s = cmd(name)
        s.add_argument("kind")
        s.add_argument("item_id", metavar="id")
        if name == "play":
            s.add_argument("--start-with", type=int, default=0, metavar="N")
            s.add_argument("--shuffle", action="store_true")

    cmd("control").add_argument("action")
    cmd("seek").add_argument("sec")
    cmd("volume").add_argument("val")
    cmd("shuffle").add_argument("mode")
    cmd("repeat").add_argument("mode")
    cmd("now-playing")
    cmd("queue")
    cmd("playlists")
    s = cmd("add-to-playlist")
    s.add_argument("playlist_id", metavar="playlistId")
    s.add_argument("song_id", metavar="songId")
    cmd("lyrics").add_argument("song_id", metavar="catalogSongId")
    s = cmd("search")
    s.add_argument("term", nargs="+")
    s.add_argument("--library", action="store_true")
    s.add_argument("--limit", type=int, default=20, metavar="N")
    s.add_argument("--suggest", type=int, default=0, metavar="N")
    s = cmd("suggest")
    s.add_argument("term", nargs="+")
    s.add_argument("--limit", type=int, default=10, metavar="N")
    cmd("landing")
    cmd("category").add_argument("category_id", metavar="id")
    return p


def run_cli(argv):
    # --no-start is accepted anywhere, before or after the command.
    ns = "--no-start" in argv
    argv = [x for x in argv if x != "--no-start"]
    if not argv:
        raise AmError("usage", "No command provided")
    a = _build_parser().parse_args(argv)
    c = a.command

    if c == "engine":
        return {"start": lambda: engine_start(headless=a.headless),
                "stop": engine_stop, "status": engine_status}[a.sub]()
    if c == "signin":
        return handle_signin()
    if c == "status":
        return handle_status()
    if c == "sync":
        return handle_sync(only=a.only, no_start=ns)
    if c == "item":
        return handle_item(a.kind, a.item_id, no_start=ns)
    if c == "play":
        return handle_play(a.kind, a.item_id, start_with=a.start_with, shuffle=a.shuffle, no_start=ns)
    if c == "play-next":
        return handle_play_next(a.kind, a.item_id, no_start=ns)
    if c == "play-later":
        return handle_play_later(a.kind, a.item_id, no_start=ns)
    if c == "control":
        return handle_control(a.action, no_start=ns)
    if c == "seek":
        return handle_seek(a.sec, no_start=ns)
    if c == "volume":
        return handle_volume(a.val, no_start=ns)
    if c == "shuffle":
        return handle_shuffle(a.mode, no_start=ns)
    if c == "repeat":
        return handle_repeat(a.mode, no_start=ns)
    if c == "now-playing":
        return handle_now_playing(no_start=ns)
    if c == "queue":
        return handle_queue(no_start=ns)
    if c in ("love", "unlove"):
        return handle_love(a.kind, a.item_id, love=c == "love", no_start=ns)
    if c == "add-to-library":
        return handle_add_to_library(a.kind, a.item_id, no_start=ns)
    if c == "playlists":
        return handle_playlists(no_start=ns)
    if c == "add-to-playlist":
        return handle_add_to_playlist(a.playlist_id, a.song_id, no_start=ns)
    if c == "lyrics":
        return handle_lyrics(a.song_id, no_start=ns)
    if c == "search":
        term = " ".join(a.term).strip()
        if not term:
            raise AmError("usage", "search requires a search term")
        return handle_search(term, library=a.library, limit=a.limit, suggest=a.suggest, no_start=ns)
    if c == "suggest":
        term = " ".join(a.term).strip()
        if not term:
            raise AmError("usage", "suggest requires a search term")
        return handle_suggest(term, limit=a.limit, no_start=ns)
    if c == "landing":
        return handle_landing(no_start=ns)
    if c == "category":
        return handle_category(a.category_id, no_start=ns)
    raise AmError("usage", f"Unknown command: {c}")


def main():
    try:
        res = run_cli(sys.argv[1:])
        print(json.dumps(res))
        sys.exit(0)
    except AmError as e:
        print(json.dumps({"error": e.code, "message": e.message}))
        sys.exit(1)
    except TimeoutError as e:
        print(json.dumps({"error": "timeout", "message": str(e)}))
        sys.exit(1)
    except Exception as e:
        msg = str(e)
        code = "api"
        if "connection refused" in msg.lower() or "engine" in msg.lower():
            code = "engine-down"
        print(json.dumps({"error": code, "message": msg}))
        sys.exit(1)


if __name__ == "__main__":
    main()
