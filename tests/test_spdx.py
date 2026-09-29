# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Every source file names its licence and copyright holder in SPDX form.

The header is the file's first lines (after a shebang): a comment in the file's own syntax,
`# …` in Python, shell, Meson and the launcher, `// …` in Blueprint and JavaScript, `/* … */`
in CSS.
"""

import unittest

from tests import ROOT

LICENSE = 'SPDX-License-Identifier: GPL-2.0-or-later'
HOLDER = 'SPDX-FileCopyrightText: 2026 Jack Tully'
PATTERNS = ('*.py', '*.blp', '*.js', '*.css', '*.sh', 'meson.build', 'meson.options',
            'apple-music.in')
DIRECTORIES = ('src', 'scripts', 'tests', 'build-aux', 'data', 'po')
COMMENT = {'.blp': '// {}', '.js': '// {}', '.css': '/* {} */'}


def source_files():
    """The source files of the tree: the top directory's Meson files and every matching file
    under DIRECTORIES, bytecode caches left out."""
    found = {ROOT / 'meson.build', ROOT / 'meson.options'}
    for directory in DIRECTORIES:
        for pattern in PATTERNS:
            found.update(path for path in (ROOT / directory).rglob(pattern)
                         if '__pycache__' not in path.parts)
    return sorted(found)


class SpdxHeaderTest(unittest.TestCase):

    def test_every_source_file_has_the_header(self):
        files = source_files()
        self.assertGreater(len(files), 100)
        missing = []
        for path in files:
            lines = path.read_text(encoding='utf-8').splitlines()
            if lines and lines[0].startswith('#!'):
                lines = lines[1:]
            comment = COMMENT.get(path.suffix, '# {}')
            if lines[:2] != [comment.format(LICENSE), comment.format(HOLDER)]:
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [], 'these files lack the SPDX header at the top')


if __name__ == '__main__':
    unittest.main()
