# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Unit tests for src/i18n.py: Python's gettext and C's libintl both read the app's domain.

Before i18n.setup(), the launcher bound only C's domain (GtkBuilder) and installed a builtin
`_` nothing used, so every `from gettext import gettext as _` in the modules looked up
Python's default domain and stayed English."""

import gettext
import locale
import os
import pathlib
import struct
import tempfile
import unittest
from unittest import mock

from tests import ROOT, SRC  # noqa: F401  registers src/ as applemusic

from applemusic import i18n

HEADER = ('Content-Type: text/plain; charset=UTF-8\n'
          'Plural-Forms: nplurals=2; plural=(n != 1);\n')


def write_mo(path, messages):
    """A GNU .mo catalogue of `messages` ({msgid: msgstr}; a plural as {(singular, plural):
    (form0, form1)}) at path, as msgfmt writes it (without the optional hash table)."""
    entries = {'': HEADER}
    for msgid, msgstr in messages.items():
        if isinstance(msgid, tuple):
            msgid, msgstr = '\0'.join(msgid), '\0'.join(msgstr)
        entries[msgid] = msgstr
    ids = sorted(entries)  # C's libintl looks them up by binary search
    originals = [msgid.encode() for msgid in ids]
    translations = [entries[msgid].encode() for msgid in ids]
    count = len(ids)
    header_size = 7 * 4
    originals_table = header_size
    translations_table = originals_table + count * 8
    data = translations_table + count * 8
    tables, blob = b'', b''
    for strings in (originals, translations):
        for string in strings:
            tables += struct.pack('<2I', len(string), data + len(blob))
            blob += string + b'\0'
    header = struct.pack('<7I', 0x950412de, 0, count, originals_table, translations_table,
                         0, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + tables + blob)


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.localedir = pathlib.Path(self.tmp.name)
        write_mo(self.localedir / 'xx' / 'LC_MESSAGES' / 'music-sleeve.mo', {
            'Home': 'Accueil',
            ('{} song', '{} songs'): ('{} chanson', '{} chansons'),
        })
        # What setup() changes, put back after.
        domain = gettext.textdomain()
        bound = gettext.bindtextdomain(i18n.DOMAIN)
        self.addCleanup(gettext.textdomain, domain)
        self.addCleanup(gettext.bindtextdomain, i18n.DOMAIN, bound)
        if hasattr(locale, 'bindtextdomain'):
            c_domain = locale.textdomain(None)
            c_bound = locale.bindtextdomain(i18n.DOMAIN, None)
            self.addCleanup(locale.textdomain, c_domain)
            self.addCleanup(locale.bindtextdomain, i18n.DOMAIN, c_bound)
        patcher = mock.patch.dict(os.environ, {'LANGUAGE': 'xx'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_python_strings_are_translated(self):
        self.assertEqual(gettext.gettext('Home'), 'Home')  # not bound yet
        i18n.setup(str(self.localedir))
        self.assertEqual(gettext.gettext('Home'), 'Accueil')
        self.assertEqual(gettext.ngettext('{} song', '{} songs', 1), '{} chanson')
        self.assertEqual(gettext.ngettext('{} song', '{} songs', 3), '{} chansons')
        self.assertEqual(gettext.gettext('Albums'), 'Albums')  # untranslated: the source

    @unittest.skipUnless(hasattr(locale, 'bindtextdomain'), 'no libintl binding here')
    def test_libintl_reads_the_same_domain(self):
        i18n.setup(str(self.localedir))
        self.assertEqual(locale.textdomain(None), 'music-sleeve')
        self.assertEqual(locale.bindtextdomain('music-sleeve', None), str(self.localedir))

    def test_the_launcher_binds_through_it(self):
        launcher = (SRC / 'music-sleeve.in').read_text(encoding='utf-8')
        self.assertIn('i18n.setup(localedir)', launcher)
        self.assertLess(launcher.index('i18n.setup(localedir)'),
                        launcher.index('from applemusic import main'))
        self.assertNotIn('gettext.install', launcher)


if __name__ == '__main__':
    unittest.main()
