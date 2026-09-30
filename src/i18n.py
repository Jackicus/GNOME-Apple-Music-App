# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Translations: the one place the gettext domain is bound.

Two gettext implementations read the catalogues. C's libintl serves GtkBuilder, so the
Blueprint strings (`_("…")` in .blp files); Python's gettext module serves every `_()` and
`ngettext()` imported with `from gettext import gettext as _`, which looks up its own current
domain, not C's. setup() binds and selects the domain for both: the launcher calls it before
importing the app, and the developer scripts through scripts/harness.py.
"""

import gettext
import locale

DOMAIN = 'music-sleeve'


def setup(localedir, domain=DOMAIN):
    """Read `domain`'s catalogues from `localedir` (<localedir>/<lang>/LC_MESSAGES/<domain>.mo)
    for C libintl and for Python's gettext, and make it the default domain of both."""
    if hasattr(locale, 'bindtextdomain'):  # not on every platform's Python
        locale.bindtextdomain(domain, localedir)
        locale.textdomain(domain)
    gettext.bindtextdomain(domain, localedir)
    gettext.textdomain(domain)
