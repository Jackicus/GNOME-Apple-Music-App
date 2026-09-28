"""Test package. Importing it registers src/ as the ``applemusic`` package.

The source tree is laid out for installation (src/ becomes applemusic/), so
tests import it under that name without a build. `python3 -m unittest
discover -s tests` imports test modules as top-level modules and never runs
this file itself, so every test module starts with ``from tests import …``
(run from the repository root, as scripts/check.sh does).

It also keeps every test off the desktop's settings: GSettings goes to the memory backend,
with this tree's schema visible (tests/gtk.py's use_test_settings(), called here, before
any test module can start GTK or GSettings). Widget tests use tests/gtk.py's requires_gtk.
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

from tests.gtk import use_test_settings  # noqa: E402  (needs ROOT, above)

use_test_settings()
