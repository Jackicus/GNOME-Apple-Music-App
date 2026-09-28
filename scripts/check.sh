#!/usr/bin/env bash
# Everything that should pass before calling a change done.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m compileall -q src scripts tests
if command -v ruff >/dev/null; then
  ruff check .
else
  echo "check: ruff not installed, lint skipped"
fi
if [ ! -f build/build.ninja ]; then  # build/ alone may be just build/demo
  meson setup build --prefix="$PWD/build/install" -Dprofile=development
fi
# Before the unit tests: the widget tests (tests/gtk.py) load build/src's gresource.
meson compile -C build
python3 -m unittest discover -s tests
meson test -C build --print-errorlogs --suite apple-music
echo "check: ok"
