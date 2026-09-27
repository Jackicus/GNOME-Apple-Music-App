"""Where the backend keeps things, which port the engine listens on, and the artwork sizes.

This replaces the extension's GSettings lookup: nothing in the backend imports gi, so these are
plain functions over the environment, read on every call, so that a test or demo mode can point
them elsewhere before anything uses them.

    APPLE_MUSIC_CACHE    the cache directory  (default $XDG_CACHE_HOME/apple-music)
    APPLE_MUSIC_PROFILE  Chrome's profile     (default $XDG_DATA_HOME/apple-music/chrome)
    APPLE_MUSIC_PORT     the debugging port   (default 9228)
"""

import os
import tempfile
from pathlib import Path

APP_DIR = 'apple-music'
DEFAULT_PORT = 9228

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


def cache_dir():
    """library.json, art/, thumb/, remote-art/, items/, lyrics/."""
    return _override('APPLE_MUSIC_CACHE') or _xdg_dir('XDG_CACHE_HOME', '.cache') / APP_DIR


def profile_dir():
    """The Chrome profile the engine runs in (its own, never a browser's everyday one)."""
    return (_override('APPLE_MUSIC_PROFILE')
            or _xdg_dir('XDG_DATA_HOME', '.local/share') / APP_DIR / 'chrome')


def port():
    """Chrome's remote-debugging port on 127.0.0.1."""
    try:
        value = int(os.environ.get('APPLE_MUSIC_PORT', ''))
    except ValueError:
        return DEFAULT_PORT
    return value if 0 < value < 65536 else DEFAULT_PORT


def default_profile_dir():
    """Where the release build's profile is when nothing overrides it."""
    return _xdg_dir('XDG_DATA_HOME', '.local/share') / APP_DIR / 'chrome'


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
