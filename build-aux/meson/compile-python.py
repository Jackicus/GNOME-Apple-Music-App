#!/usr/bin/env python3
"""Byte-compile the app's installed modules: meson install's last step (src/meson.build).

    compile-python.py MODULEDIR [LEVEL]

MODULEDIR is the package's directory relative to the prefix (share/apple-music/applemusic),
found under $MESON_INSTALL_DESTDIR_PREFIX (a package's staging directory) and compiled with
tracebacks naming it under $MESON_INSTALL_PREFIX. LEVEL is the python.bytecompile option: 0
plain bytecode, 1 also -O, 2 also -OO. The modules are installed with install_data, outside
Python's site-packages, so Meson's own byte-compiling never sees them; without bytecode a
system install (whose __pycache__ Python cannot write) compiles every module at every start.
Only stale files are compiled again, so a development install stays quick.
"""

import compileall
import os
import sys


def main(argv):
    moduledir = argv[1]
    level = int(argv[2]) if len(argv) > 2 else 0
    target = os.path.join(os.environ['MESON_INSTALL_DESTDIR_PREFIX'], moduledir)
    final = os.path.join(os.environ['MESON_INSTALL_PREFIX'], moduledir)
    quiet = 2 if os.environ.get('MESON_INSTALL_QUIET') else 1
    levels = [0, 1, 2][:level + 1]
    ok = compileall.compile_dir(target, ddir=final, quiet=quiet, optimize=levels)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
