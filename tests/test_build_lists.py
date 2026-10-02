# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The hand-kept build lists name every source file: the tests import src/ directly, so a
module missing from src/meson.build's install lists, a .blp missing from the blueprint list
or the gresource, or a file with translatable strings missing from po/POTFILES.in, would pass
every other test and break (or go untranslated) only once installed."""

import io
import pathlib
import re
import shutil
import subprocess
import tokenize
import unittest
import xml.etree.ElementTree as ElementTree

from tests import ROOT, SRC

GETTEXT_CALLS = ('_', 'ngettext', 'C_')
# A call in Blueprint: _("…") or C_("…", "…") (not some_name_(…)), outside // comments.
BLUEPRINT_CALL = re.compile(r'(?<![\w.])(?:_|C_)\(')


def translates(path):
    """Whether the .py or .blp file at path calls _(, ngettext( or C_( (in code, not in a
    docstring or a comment)."""
    text = path.read_text(encoding='utf-8')
    if path.suffix == '.blp':
        return any(BLUEPRINT_CALL.search(line.partition('//')[0]) for line in text.splitlines())
    tokens = [token for token in tokenize.generate_tokens(io.StringIO(text).readline)
              if token.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE)]
    return any(token.type == tokenize.NAME and token.string in GETTEXT_CALLS
               and following.string == '(' and previous.string != '.'
               for previous, token, following in zip(tokens, tokens[1:], tokens[2:], strict=False))


def _git_toplevel(git):
    done = subprocess.run([git, 'rev-parse', '--show-toplevel'], cwd=ROOT,
                          capture_output=True, text=True)
    return pathlib.Path(done.stdout.strip()).resolve() if done.returncode == 0 else None


def source_files(*suffixes):
    """The files under src/ with these suffixes, as paths relative to the repository: those
    git tracks or would add (untracked but not ignored), else every one on disk. Git is asked
    only when this tree is a repository's own top level: an unpacked release tarball inside
    another checkout (meson dist's check unpacks into the ignored build/) would otherwise be
    answered for by that checkout, which ignores every file in it."""
    git = shutil.which('git')
    if git is not None and _git_toplevel(git) == ROOT:
        done = subprocess.run(
            [git, 'ls-files', '--cached', '--others', '--exclude-standard', '--', 'src'],
            cwd=ROOT, capture_output=True, text=True)
        if done.returncode == 0:
            paths = {line for line in done.stdout.splitlines()
                     if line.endswith(suffixes) and (ROOT / line).exists()}
            return sorted(paths)
    return sorted(str(path.relative_to(ROOT)) for path in SRC.rglob('*')
                  if path.suffix in suffixes and '__pycache__' not in path.parts)


def quoted(blocks, *suffixes):
    """The single-quoted strings ending in one of suffixes in these meson.build excerpts."""
    return {name for block in blocks for name in re.findall(r"'([^']+)'", block)
            if name.endswith(suffixes)}


class BuildListsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        meson = re.sub(r'#[^\n]*', '', (SRC / 'meson.build').read_text(encoding='utf-8'))
        cls.installed = quoted(re.findall(r'install_data\((.*?)install_dir', meson, re.S),
                               '.py', '.js')
        cls.compiled = quoted(re.findall(r'files\((.*?)\)', meson, re.S), '.blp')
        tree = ElementTree.parse(SRC / 'applemusic.gresource.xml')
        cls.resources = [(node.text, node.get('alias')) for node in tree.iter('file')]
        with open(ROOT / 'po' / 'POTFILES.in', encoding='utf-8') as file:
            cls.potfiles = [line.strip() for line in file
                            if line.strip() and not line.startswith('#')]

    def src_relative(self, paths):
        return {str((ROOT / path).relative_to(SRC)) for path in paths}

    def test_every_module_is_installed(self):
        modules = self.src_relative(source_files('.py', '.js'))
        self.assertEqual(sorted(modules - self.installed), [],
                         "missing from src/meson.build's install_data lists")
        self.assertEqual(sorted(self.installed - modules), [],
                         'listed in src/meson.build but not in src/')

    def test_every_blueprint_is_compiled_and_in_the_gresource(self):
        blueprints = self.src_relative(source_files('.blp'))
        self.assertEqual(sorted(blueprints - self.compiled), [],
                         "missing from src/meson.build's blueprint list")
        self.assertEqual(sorted(self.compiled - blueprints), [],
                         'listed in src/meson.build but not in src/')
        uis = {text for text, _alias in self.resources if text.endswith('.ui')}
        wanted = {blueprint.rpartition('/')[2].removesuffix('.blp') + '.ui'
                  for blueprint in blueprints}
        self.assertEqual(len(wanted), len(blueprints), 'two .blp files share a basename')
        self.assertEqual(sorted(wanted - uis), [], 'missing from applemusic.gresource.xml')
        self.assertEqual(sorted(uis - wanted), [], 'in applemusic.gresource.xml, no .blp')

    def test_every_icon_and_the_stylesheet_are_in_the_gresource(self):
        files = {text for text, _alias in self.resources}
        icons = {str(path.relative_to(SRC)) for path in (SRC / 'icons').glob('*.svg')}
        self.assertEqual(sorted(icons - files), [], 'icons missing from the gresource')
        for text, alias in self.resources:
            if text in icons:
                self.assertEqual(alias, 'icons/scalable/actions/' + text.rpartition('/')[2])
        self.assertIn('style.css', files)

    def test_every_file_with_translatable_strings_is_in_potfiles(self):
        translatable = [path for path in source_files('.py', '.blp')
                        if translates(ROOT / path)]
        self.assertGreater(len(translatable), 20)  # the pattern still finds them
        self.assertEqual(sorted(set(translatable) - set(self.potfiles)), [],
                         'missing from po/POTFILES.in')
        missing = [path for path in self.potfiles if not (ROOT / path).exists()]
        self.assertEqual(missing, [], 'in po/POTFILES.in but not in the tree')


if __name__ == '__main__':
    unittest.main()
