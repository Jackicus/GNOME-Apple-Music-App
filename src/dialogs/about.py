# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The About dialog (app.about): built from the app's metainfo, which the gresource carries
(metainfo.xml), so its name, developer, links, licence and release notes are the metainfo's;
with the translators' credits, and the debug information a bug report wants (versions, the
engine's state, never anything of the account's).

    about.present(app, parent)
"""

import platform
from gettext import gettext as _

import gi
from gi.repository import Adw, GLib, Gtk

METAINFO = '/io/github/jackicus/AppleMusic/metainfo.xml'
DEBUG_INFO_FILENAME = 'apple-music-debug-info.txt'


def release_version(version):
    """The release a build's version belongs to, whose notes the dialog shows: '0.9.0' for
    the development build's '0.9.0-1a2b3c4'."""
    return version.partition('-')[0]


def debug_info(app, browser=None):
    """The dialog's Troubleshooting text: the app's and the toolkit's versions, and the
    engine's state (`browser`, Chrome's product and version, when it is known)."""
    engine = app.engine
    if app.demo:
        engine_state = 'none (demo library)'
    elif engine.state == 'up':
        engine_state = ', '.join(('up', 'headless' if engine.headless else 'in a window',
                                  'signed in' if engine.authorized else 'not signed in'))
    else:
        engine_state = engine.state
    gtk = f'{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}'
    adw = f'{Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}'
    glib = f'{GLib.MAJOR_VERSION}.{GLib.MINOR_VERSION}.{GLib.MICRO_VERSION}'
    lines = [
        f'Apple Music {app.version} ({app.get_application_id()})',
        f'Python {platform.python_version()}, PyGObject {gi.__version__}',
        f'GTK {gtk}, libadwaita {adw}, GLib {glib}',
        f'Engine: {engine_state}',
    ]
    if browser:
        lines.append(f'Browser: {browser}')
    return '\n'.join(lines) + '\n'


def present(app, parent):
    """Build the dialog and present it over `parent`; returns it."""
    about = Adw.AboutDialog.new_from_appdata(METAINFO, release_version(app.version))
    about.set_version(app.version)  # the build's, with the development build's revision
    about.set_application_icon(app.get_application_id())
    about.set_copyright('© 2026 Jack Tully')
    about.set_comments(_('Not affiliated with Apple. Apple Music is a trademark of Apple Inc.'))
    # Translators: your names, one per line (with an address if you like), for the About
    # dialog's credits.
    credits = _('translator-credits')
    if credits != 'translator-credits':
        about.set_translator_credits(credits)
    about.set_debug_info(debug_info(app))
    about.set_debug_info_filename(DEBUG_INFO_FILENAME)
    about.present(parent)
    if not app.demo and app.engine.state == 'up':
        app.spawn(_add_browser(app, about))
    return about


async def _add_browser(app, about):
    """Chrome's version into the debug information, once the engine has said it."""
    browser = await app.engine.browser_version()
    if browser:
        about.set_debug_info(debug_info(app, browser))
