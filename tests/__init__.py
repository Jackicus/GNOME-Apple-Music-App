"""Test package. Importing it registers src/ as the ``applemusic`` package.

The source tree is laid out for installation (src/ becomes applemusic/), so
tests import it under that name without a build. `python3 -m unittest
discover -s tests` imports test modules as top-level modules and never runs
this file itself, so every test module starts with ``from tests import …``
(run from the repository root, as scripts/check.sh does).
"""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / 'src'

if 'applemusic' not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        'applemusic', SRC / '__init__.py', submodule_search_locations=[str(SRC)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules['applemusic'] = _module
    _spec.loader.exec_module(_module)
