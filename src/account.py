"""The account's lifecycle on this computer: signing out, and clearing the cache.

    await account.sign_out(app)      # the engine stopped, the profile and cache wiped
    await account.clear_cache(app)   # the cache wiped, the library synced again

`app` is the Application: its settings, engine, library, library_sync and toast(). No
widgets: the window and the dialogs call these.
"""

import asyncio
import logging
from gettext import gettext as _

from . import cache
from .backend import config
from .backend.errors import EngineError
from .widgets import artwork

log = logging.getLogger(__name__)


async def sign_out(app):
    """Stop the engine, forget the account, wipe the Chrome profile and the cache, and
    empty the library."""
    if app.demo:
        return
    engine = app.engine
    try:
        await engine.stop()
    except EngineError as error:
        log.warning('stopping the engine before signing out: %s', error)
    app.settings.set_boolean('signed-in', False)
    app.settings.set_string('account-name', '')
    await asyncio.to_thread(cache.remove_trees, engine.profile_dir, config.cache_dir())
    await app.library.load()  # nothing left to read: the models empty
    app.toast(_('Signed out'))


async def clear_cache(app):
    """Clear the cache (cache.CACHE_ENTRIES: library.json, the artwork, items, lyrics and
    the day-long answers), after stopping a sync that is running; the library empties and
    `last-sync` is forgotten, and the library is synced again when signed in. Answers False
    (nothing done) in demo mode, whose library is the cache."""
    if app.demo:
        return False
    await app.library_sync.cancel()  # it would write library.json and artwork into what is cleared
    await asyncio.to_thread(cache.clear, config.cache_dir())
    artwork.get_default().clear()
    app.settings.set_string('last-sync', '')
    await app.library.load()  # nothing left to read: the models empty
    if app.settings.get_boolean('signed-in'):
        app.library_sync.start()
    return True
