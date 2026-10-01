# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The account's lifecycle on this computer: signing in and out, and clearing the cache.

    await account.sign_in(app, on_status)   # True once signed in; the rest goes on by itself
    await account.sign_out(app)      # the Apple session revoked, the profile and cache wiped
    await account.clear_cache(app)   # the cache wiped, the library synced again

Signing out and clearing the cache race every job that writes the cache (the sync and its
thread, an item's artwork, the kept answers, lyrics, the covers the pages fetch), so both
begin the same way: the cache's generation bumped (store.bump_cache_generation: from then on
no write of those jobs lands), the sync cancelled and waited for (its thread included), and
only then the wipe. No sync starts meanwhile (LibrarySync.hold()), nor while signing in.

`app` is the Application: its settings (account_key()), engine, library, library_sync, mpris,
spawn(), toast(), report() and the active window; `revocation` and `sign_in_finish` are the
tasks it keeps for quitting. No widgets: the window and the dialogs call these.
"""

import asyncio
import logging
from gettext import gettext as _

from . import cache
from .backend import config, store
from .backend.errors import EngineError
from .sidebar import parse_key
from .widgets import artwork

log = logging.getLogger(__name__)

# Signing out revokes the Apple session through the engine. A stopped engine is started for
# that (headless), for this long at most; whatever happens, the wipe follows.
REVOKE_TIMEOUT = 20.0


async def sign_in(app, on_status):
    """The sign-in flow, to its commit point: a running sync stopped (its engine is about to
    restart), Chrome restarted in a window on music.apple.com, and a wait until MusicKit
    says the account is authorized (engine.signin(): the person signs in in that window).
    Then the account is signed in (`signed-in` set, "Signed in" toasted) and the flow is
    committed: this returns True, and the rest runs as a task of the app's (_finish_sign_in:
    the account's name, the engine in the mode the settings ask for, the first sync), which
    nothing cancels. Before that, cancelling this (the dialog's Cancel, or its closing)
    stops the engine, and a failure is toasted in words for a sign-in, stops the engine and
    answers False. `on_status(text)` is told each step, for the dialog to show. No sync
    starts while it runs, and `app.signing_in` is true to the end of the task."""
    engine = app.engine
    app.signing_in = True
    app.library_sync.hold()
    committed = False
    signing = False  # past the start: Chrome's window is the person's to close
    try:
        await app.library_sync.cancel()
        on_status(_('Starting Chrome…'))
        await engine.restart(visible=True)
        signing = True
        on_status(_('Sign in with your Apple Account in the Chrome window'))
        await engine.signin()
        app.settings.set_boolean(app.account_key('signed-in'), True)
        committed = True
    except asyncio.CancelledError:
        log.info('sign-in cancelled')
        app.spawn(engine.stop())
        raise
    except EngineError as error:
        log.warning('sign-in: %s', error)
        if error.code == 'timeout':
            app.toast(_('Sign-in timed out'))
        elif error.code == 'engine-down' and signing:
            app.toast(_('Sign-in cancelled: the Chrome window was closed'))
        else:
            app.report(error)
        app.spawn(engine.stop())
        return False
    except Exception:
        log.exception('sign-in failed')
        app.toast(_('Could not sign in'))
        app.spawn(engine.stop())
        return False
    finally:
        if not committed:
            app.signing_in = False
            app.library_sync.release()
    # The rest is the finish's, which releases the hold and signing_in however it ends
    # (quitting cancels it); nothing here may come between the commit and it.
    app.sign_in_finish = app.spawn(_finish_sign_in(app))
    log.info('signed in')
    on_status(_('Signed in'))
    app.toast(_('Signed in'))
    return True


# How long the page is given to show the account's name after sign-in (it renders it late).
ACCOUNT_NAME_WAIT = 15


async def _finish_sign_in(app):
    """After the commit: the account's name as the page shows it (best effort), the engine
    headless unless engine-headless is off (or started again, if its window was closed),
    then the first sync."""
    engine = app.engine
    settings = app.settings
    try:
        try:
            name = await engine.account_name(wait=ACCOUNT_NAME_WAIT)
        except EngineError as error:
            log.info('no account name read after sign-in: %s', error)
            name = ''
        settings.set_string(app.account_key('account-name'), name)
        if not name:
            log.info('no account name found on the page; the button says Signed In')
        try:
            await engine.start(visible=not settings.get_boolean('engine-headless'))
        except EngineError as error:
            app.report(error)
    finally:
        app.signing_in = False
        app.library_sync.release()
    app.library_sync.start()


async def sign_out(app):
    """Sign out: stop a running sync (revoking the session would fail it, loudly), revoke
    the session at Apple's (MusicKit's unauthorize()), stop the engine, wipe the Chrome
    profile and the cache, forget the account's settings and pages, and empty the library.
    The account's actions are off meanwhile (`app.signing_out`), no sync starts, and the
    engine refuses to start from the stop on (a Chrome started then would write the profile
    back). Quitting meanwhile cancels only the revocation (`app.revocation`): the rest,
    which leaves nothing of the account behind, runs to its end, and the quit waits for it."""
    if app.demo:
        return
    engine = app.engine
    cache_dir = config.cache_dir()
    app.signing_out = True
    try:
        with app.library_sync.held():
            await app.library_sync.cancel()
            revocation = app.revocation = asyncio.ensure_future(_revoke(app))
            try:
                await asyncio.wait([revocation])
            finally:
                app.revocation = None
            if revocation.cancelled():
                log.info('quitting: the Apple session is not revoked')
            elif revocation.exception() is not None:
                log.error('could not sign out of Apple Music', exc_info=revocation.exception())
            engine.refuse_starts = 'signing out'
            store.bump_cache_generation()  # before anything is wiped
            try:
                await engine.stop()
            except EngineError as error:
                log.warning('stopping the engine before signing out: %s', error)
            await asyncio.to_thread(_wipe, engine.profile_dir, cache_dir)
            _forget(app)
            await app.library.load()  # nothing left to read: the models empty
            window = app.get_active_window()
            if window is not None and hasattr(window, 'forget_account_pages'):
                window.forget_account_pages()
            # A page showing the account's library until now may have fetched a cover since
            # the bump: nothing shows the account any more, so once more is the last.
            store.bump_cache_generation()
            await asyncio.to_thread(cache.clear, cache_dir)
    finally:
        engine.refuse_starts = None
        app.signing_out = False
    app.toast(_('Signed out'))


async def _revoke(app):
    """MusicKit's unauthorize() in the engine, best effort (it logs what fails): an engine
    that is down is started for it when the account is signed in, REVOKE_TIMEOUT at most."""
    engine = app.engine
    if engine.state == 'down':
        if not app.settings.get_boolean(app.account_key('signed-in')):
            return
        try:
            await asyncio.wait_for(engine.start(visible=False), REVOKE_TIMEOUT)
        except (EngineError, TimeoutError) as error:
            log.warning('could not start the engine to sign out of Apple Music: %s',
                        str(error) or 'it took too long')
            return
    try:
        await asyncio.wait_for(engine.unauthorize(), REVOKE_TIMEOUT)
    except TimeoutError:
        log.warning('could not sign out of Apple Music: the engine took too long')


def _wipe(profile_dir, cache_dir):
    """In a thread: the Chrome profile, and what the cache holds (its known entries and
    temporary files: cache.clear; never the directory or anything else in it)."""
    cache.remove_trees(profile_dir)
    cache.clear(cache_dir)


def _forget(app):
    """The settings that name the account or its library: the sign-in, the name, the last
    sync, the expanded folders, and the last page when it is a playlist or a folder."""
    settings = app.settings
    for key in ('signed-in', 'account-name', 'last-sync', 'expanded-folders'):
        settings.reset(app.account_key(key))
    last_page = app.account_key('last-page')
    if parse_key(settings.get_string(last_page)) is not None:
        settings.reset(last_page)
    _forget_artwork(app)


def _forget_artwork(app):
    """The decoded artwork, and the file MPRIS names, may be the wiped cache's."""
    artwork.get_default().clear()
    if app.mpris is not None:
        app.mpris.refresh_art()


async def clear_cache(app):
    """Clear the cache (cache.CACHE_ENTRIES: library.json, the artwork, lyrics and the
    day-long answers, and temporary files) with every job writing it stopped first; the
    library empties and `last-sync` is forgotten, and the library is synced again when
    signed in. Answers False (nothing done) in demo mode, whose library is the cache."""
    if app.demo:
        return False
    with app.library_sync.held():
        store.bump_cache_generation()  # before anything is cancelled or wiped
        await app.library_sync.cancel()
        await asyncio.to_thread(cache.clear, config.cache_dir())
        _forget_artwork(app)
        app.settings.reset(app.account_key('last-sync'))
        await app.library.load()  # nothing left to read: the models empty
    if app.settings.get_boolean(app.account_key('signed-in')):
        app.library_sync.start()
    return True
