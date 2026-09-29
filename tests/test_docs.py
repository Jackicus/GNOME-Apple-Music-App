# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The documentation keeps up with the code: what drifts is checked here.

- every repository path the docs name (in backticks, in a Markdown link, or in plain text
  starting at one of the tree's top directories) exists; `<name>` placeholders, `*` and
  `{a,b}` stand for whatever matches;
- the keys the docs name are the app's (src/shortcuts.py), or keys GTK and the HIG own;
- CLAUDE.md stays within its line budget;
- no internal review or plan IDs (C123, WP12, P01-2, X6-5, D10) in any of these documents
  (CLAUDE.md, the rules, the skills, docs/, the READMEs), and no phase numbers in the code's
  comments: they mean nothing outside the audit and the build plan that used them.
  docs/history/ is history and is left alone.
"""

import glob
import itertools
import re
import unittest

from tests import ROOT

from applemusic import shortcuts

ROOT_BUDGET = 200  # CLAUDE.md's lines

# The Markdown documents that describe the tree as it is.
DOCS = sorted(
    [ROOT / name for name in ('CLAUDE.md', 'README.md', 'CONTRIBUTING.md', 'SECURITY.md')]
    + [ROOT / 'src' / 'backend' / 'README.md', ROOT / '.github' / 'pull_request_template.md']
    + list((ROOT / 'docs').glob('*.md'))
    + list((ROOT / '.claude' / 'rules').glob('*.md'))
    + list((ROOT / '.claude' / 'skills').glob('*/SKILL.md')))

TOP = ('src/', 'scripts/', 'tests/', 'data/', 'docs/', 'build-aux/', 'po/', 'subprojects/',
       '.claude/', '.github/')
ROOT_FILES = ('CLAUDE.md', 'README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'LICENSE',
              'meson.build', 'meson.options', 'pyproject.toml')
# A path in text: a top directory, then path characters; placeholders (<…>, *, {…}) included.
PATH_RE = re.compile(r'(?<![\w./~$-])(?:' + '|'.join(re.escape(top) for top in TOP)
                     + r')[\w./*<>{},@-]*')
LINK_RE = re.compile(r'\]\(([^)\s#]+)(?:#[^)]*)?\)')
ID_RE = re.compile(r'\b(?:C\d{3}|WP\d{2}|P\d{2}-\d+|X\d-\d+|D\d{1,2})\b')
PHASE_RE = re.compile(r'\bphase \d+|\bphases? \d+ to \d+', re.IGNORECASE)

# Keys the docs may name that are not the app's own: GTK's and libadwaita's navigation, the
# HIG's standard keys the app leaves alone, and a terminal's interrupt.
OTHER_KEYS = {'Shift+Tab', 'Ctrl+N', 'Ctrl+C'}
KEY_RE = re.compile(r'\b((?:(?:Ctrl|Alt|Shift|Super)\+)+(?:[A-Za-z0-9]+\b|[?,.]))')


def expand(pattern):
    """A path pattern as glob patterns: {a,b} alternatives expanded, <name> as *."""
    pattern = re.sub(r'<[^>]*>', '*', pattern)
    parts = re.split(r'(\{[^}]*\})', pattern)
    choices = [part[1:-1].split(',') if part.startswith('{') else [part] for part in parts]
    return [''.join(combination) for combination in itertools.product(*choices)]


def exists(pattern, base=ROOT):
    return any(glob.glob(str(base / candidate)) for candidate in expand(pattern))


def clean(token):
    """A path as the text wrote it, without what follows it in the sentence."""
    token = re.sub(r"'s$", '', token)
    token = token.rstrip('.,:;)')
    return re.sub(r':\d+$', '', token)


def accelerator_names(accelerator):
    """'<primary><shift>n' as 'Ctrl+Shift+N', the way the docs write keys."""
    names = {'primary': 'Ctrl', 'control': 'Ctrl', 'shift': 'Shift', 'alt': 'Alt'}
    keys = {'question': '?', 'comma': ',', 'space': 'Space', 'KP_Space': 'Space'}
    modifiers = re.findall(r'<(\w+)>', accelerator)
    key = re.sub(r'<\w+>', '', accelerator)
    key = keys.get(key, key.upper() if len(key) == 1 else key)
    return '+'.join([names[modifier.lower()] for modifier in modifiers] + [key])


def app_keys():
    """Every key the app binds, as the docs write it."""
    found = set()
    for accelerators in list(shortcuts.ACCELS.values()) + list(shortcuts.PLAYBACK.values()):
        found.update(accelerator_names(accelerator) for accelerator in accelerators)
    for accelerator in (shortcuts.MAIN_MENU, *shortcuts.CONTEXT_MENU.split(), shortcuts.CLOSE):
        found.add(accelerator_names(accelerator))
    return found


class DocsTest(unittest.TestCase):

    def test_the_documents_exist(self):
        self.assertGreater(len(DOCS), 20)
        for path in DOCS:
            self.assertTrue(path.exists(), path)

    def test_every_path_named_exists(self):
        missing = []
        for path in DOCS:
            text = path.read_text(encoding='utf-8')
            for token in PATH_RE.findall(text):
                token = clean(token)
                if token.rstrip('/') in [top.rstrip('/') for top in TOP]:
                    continue
                if not exists(token):
                    missing.append(f'{path.relative_to(ROOT)}: {token}')
            for target in LINK_RE.findall(text):
                if re.match(r'\w+:', target):  # a URL
                    continue
                if not exists(target, path.parent):
                    missing.append(f'{path.relative_to(ROOT)}: ({target})')
            for name in ROOT_FILES:
                if f'`{name}`' in text and not (ROOT / name).exists():
                    missing.append(f'{path.relative_to(ROOT)}: {name}')
        self.assertEqual(missing, [], 'the docs name paths that are not in the tree')

    def test_the_keys_named_are_the_apps(self):
        keys = app_keys() | OTHER_KEYS
        self.assertIn('Ctrl+Shift+N', keys)
        unknown = []
        for path in DOCS + [ROOT / 'data' / 'io.github.jackicus.AppleMusic.metainfo.xml.in']:
            text = re.sub(r'</?kbd>', '', path.read_text(encoding='utf-8'))
            for key in KEY_RE.findall(text):
                if key not in keys:
                    unknown.append(f'{path.relative_to(ROOT)}: {key}')
        self.assertEqual(unknown, [], 'keys the docs name that src/shortcuts.py does not bind')

    def test_the_root_file_keeps_its_budget(self):
        lines = (ROOT / 'CLAUDE.md').read_text(encoding='utf-8').splitlines()
        self.assertLessEqual(len(lines), ROOT_BUDGET,
                             'CLAUDE.md holds rules and pointers: move detail to .claude/rules/')

    def test_no_review_ids_in_the_instructions_or_docs(self):
        found = []
        for path in DOCS:
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                for match in ID_RE.findall(line):
                    found.append(f'{path.relative_to(ROOT)}:{number}: {match}')
        self.assertEqual(found, [])

    def test_no_phase_numbers_in_the_code(self):
        found = []
        for directory in ('src', 'scripts', 'tests'):
            for path in sorted((ROOT / directory).rglob('*')):
                if path.suffix not in ('.py', '.blp', '.js', '.css', '.build', '.sh'):
                    continue
                if '__pycache__' in path.parts or path == ROOT / 'tests' / 'test_docs.py':
                    continue
                for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                    if PHASE_RE.search(line):
                        found.append(f'{path.relative_to(ROOT)}:{number}')
        self.assertEqual(found, [], 'comments describe the code, not the plan it came from')


if __name__ == '__main__':
    unittest.main()
