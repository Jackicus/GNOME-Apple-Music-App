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
python3 -m unittest discover -s tests
if [ ! -d build ]; then
  meson setup build --prefix="$PWD/build/install" -Dprofile=development
fi
meson compile -C build
meson test -C build --print-errorlogs --suite apple-music
echo "check: ok"
