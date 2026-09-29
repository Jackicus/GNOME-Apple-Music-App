# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Widget tests: the `requires_gtk` decorator, isolated settings, and main-loop helpers.

Most logic is tested without GTK, in non-widget classes and pure functions with stand-ins
(test_mpris, test_actions). A test that must build widgets is decorated with `requires_gtk`
(a TestCase class or one test method). It is skipped unless GTK can open a display and the
compiled gresource exists (build/src/applemusic.gresource, which `meson compile -C build`
writes and scripts/check.sh builds before the tests; else build/install's), which it then
registers once, with libadwaita initialised. Modules that define Gtk.Template classes need
the gresource at import, so a widget test imports them inside the test (or its setUpClass),
never at the top of the file. Each widget test must finish in under a second.

Settings never reach the desktop's: tests/__init__.py calls use_test_settings() before any
test module can touch GSettings, so every test process uses the memory backend and sees this
tree's schema (data/*.gschema.xml, compiled into a temporary directory), and
`Gio.Settings.new('io.github.jackicus.AppleMusic')` works in a test.

pump() runs what is pending on the default main context; wait_for() runs it until a
condition holds.
"""

import atexit
import functools
import inspect
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from tests import ROOT

SCHEMA_ID = 'io.github.jackicus.AppleMusic'
RESOURCES = (
    ROOT / 'build' / 'src' / 'applemusic.gresource',
    ROOT / 'build' / 'install' / 'share' / 'apple-music' / 'applemusic.gresource',
)

_schema_dir = None
_unavailable = []  # [reason or None], once GTK has been tried


def use_test_settings():
    """Point GSettings at the memory backend and at this tree's schema, compiled into a
    temporary directory (removed at exit). Must run before anything in the process uses
    GSettings (GTK's start may): the schema directories are read once."""
    global _schema_dir
    os.environ['GSETTINGS_BACKEND'] = 'memory'
    if _schema_dir is not None:
        return
    compiler = shutil.which('glib-compile-schemas')
    if compiler is None:
        return  # requires_gtk says why its tests skip
    directory = tempfile.mkdtemp(prefix='apple-music-test-schemas-')
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    done = subprocess.run([compiler, '--strict', '--targetdir', directory,
                           str(ROOT / 'data')], capture_output=True, text=True)
    if done.returncode != 0:
        return
    _schema_dir = directory
    dirs = os.environ.get('GSETTINGS_SCHEMA_DIR')
    os.environ['GSETTINGS_SCHEMA_DIR'] = directory + (os.pathsep + dirs if dirs else '')


def _prepare():
    """None when widget tests can run here (GTK started, the gresource registered), else
    why not."""
    resource = next((path for path in RESOURCES if path.exists()), None)
    if resource is None:
        return 'no compiled gresource: run meson compile -C build (scripts/check.sh does)'
    import gi

    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, Gdk, Gio, Gtk

    # A second init_check() answers True whatever the first found (importing Gtk has made
    # one), so the display is what tells.
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        return 'GTK cannot open a display'
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(SCHEMA_ID, True) is None:
        return f'the {SCHEMA_ID} schema is not visible (glib-compile-schemas missing or failed?)'
    Adw.init()
    Gio.Resource.load(str(resource))._register()
    return None


def gtk_unavailable():
    """Why widget tests cannot run in this process, or None when they can. Tried once."""
    if not _unavailable:
        _unavailable.append(_prepare())
    return _unavailable[0]


def _skip_unless_ready():
    reason = gtk_unavailable()
    if reason is not None:
        raise unittest.SkipTest(reason)


def requires_gtk(target):
    """Skip a TestCase class, or a single test method (plain or async), unless GTK is usable;
    see the module."""
    if isinstance(target, type):
        set_up_class = target.__dict__.get('setUpClass')

        def setUpClass(cls):
            _skip_unless_ready()
            if set_up_class is not None:
                set_up_class.__func__(cls)
            else:
                super(target, cls).setUpClass()

        target.setUpClass = classmethod(setUpClass)
        return target
    if inspect.iscoroutinefunction(target):
        @functools.wraps(target)
        async def run_async(*args, **kwargs):
            _skip_unless_ready()
            return await target(*args, **kwargs)
        return run_async

    @functools.wraps(target)
    def run(*args, **kwargs):
        _skip_unless_ready()
        return target(*args, **kwargs)
    return run


def pump(timeout_ms=200):
    """Dispatch what is pending on the default main context until it is idle, for at most
    timeout_ms."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout_ms / 1000
    while context.pending() and time.monotonic() < deadline:
        context.iteration(False)


def wait_for(predicate, timeout=1.0):
    """Run the default main context until predicate() is true (True) or timeout seconds
    have passed (False)."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    wake = GLib.timeout_add(10, lambda: GLib.SOURCE_CONTINUE)  # a blocking iteration returns
    try:
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            context.iteration(True)
        return True
    finally:
        GLib.source_remove(wake)
