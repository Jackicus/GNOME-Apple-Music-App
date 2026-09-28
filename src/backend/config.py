"""Where the backend keeps things, the developer's DevTools port, and the artwork sizes.

This replaces the extension's GSettings lookup: nothing in the backend imports gi, so these are
plain functions over the environment, read on every call, so that a test or demo mode can point
them elsewhere before anything uses them. The development build (set_build_profile, called once
as the app starts) keeps a cache and a Chrome profile of its own beside the release build's.

    APPLE_MUSIC_CACHE       the cache directory  (default $XDG_CACHE_HOME/apple-music, or
                            apple-music-devel for the development build)
    APPLE_MUSIC_PROFILE     Chrome's profile     (default $XDG_DATA_HOME/apple-music/chrome, or
                            chrome-devel for the development build)
    APPLE_MUSIC_DEBUG_PORT  a DevTools port on 127.0.0.1 beside the engine's pipe, for a
                            developer to attach to (scripts/am.py --attach); unset by default,
                            and a risk while set: any local program can drive the signed-in
                            session through it
"""

import os
import tempfile
from pathlib import Path

APP_DIR = 'apple-music'
DEVELOPMENT = 'development'  # the build profile (meson -Dprofile) with paths of its own

_build_profile = 'default'

# Artwork edge lengths in pixels. Thumbnails are what tiles and track rows draw (a tile is at
# most 160 logical px, so 320 covers 2x scale); covers are the detail pages' hero.
THUMB_SIZE = 320
COVER_SIZE = 640

# Injected into music.apple.com; installed as data beside this module.
BRIDGE_JS = Path(__file__).with_name('bridge.js')


def _xdg_dir(variable, fallback):
    """An XDG base directory: the variable when it holds an absolute path (the spec says to
    ignore relative ones), otherwise the fallback under $HOME."""
    value = os.environ.get(variable, '')
    if os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def _override(variable):
    value = os.environ.get(variable)
    return Path(os.path.abspath(value)) if value else None


def set_build_profile(profile):
    """The build profile the app was built with ('default', or 'development' for the .Devel
    build), set once as it starts: the paths below follow it."""
    global _build_profile
    _build_profile = profile or 'default'


def build_profile():
    return _build_profile


def cache_dir():
    """library.json, art/, thumb/, remote-art/, items/, lyrics/: apple-music, or
    apple-music-devel for the development build, under $XDG_CACHE_HOME."""
    name = APP_DIR + ('-devel' if _build_profile == DEVELOPMENT else '')
    return _override('APPLE_MUSIC_CACHE') or _xdg_dir('XDG_CACHE_HOME', '.cache') / name


def profile_dir():
    """The Chrome profile the engine runs in (its own, never a browser's everyday one)."""
    return _override('APPLE_MUSIC_PROFILE') or default_profile_dir()


def debug_port():
    """APPLE_MUSIC_DEBUG_PORT as a port number, or None when it is unset or not a port."""
    try:
        value = int(os.environ.get('APPLE_MUSIC_DEBUG_PORT', ''))
    except ValueError:
        return None
    return value if 0 < value < 65536 else None


def default_profile_dir():
    """Where the build's Chrome profile is when nothing overrides it: chrome, or chrome-devel
    for the development build, under $XDG_DATA_HOME/apple-music."""
    name = 'chrome-devel' if _build_profile == DEVELOPMENT else 'chrome'
    return _xdg_dir('XDG_DATA_HOME', '.local/share') / APP_DIR / name


def state_file(profile=None):
    """engine.json, describing the Chrome started on `profile` (default: profile_dir()):
    {pid, port, headless, profile, started}.

    The default profile's is $XDG_RUNTIME_DIR/apple-music/engine.json, which a logout clears.
    Any other profile (APPLE_MUSIC_PROFILE for a test or a demo, the .Devel build's chrome-devel)
    keeps its own inside itself, so two builds never find, and drive, each other's engine
    through the shared runtime directory. The file checks the pid is alive before it is
    believed, so a stale one is harmless wherever it is.
    """
    profile = Path(profile) if profile else profile_dir()
    if profile != default_profile_dir():
        return profile / 'engine.json'
    runtime = os.environ.get('XDG_RUNTIME_DIR', '')
    if os.path.isabs(runtime):
        return Path(runtime) / APP_DIR / 'engine.json'
    return Path(tempfile.gettempdir()) / f'{APP_DIR}-{os.getuid()}' / 'engine.json'
