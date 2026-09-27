#!/usr/bin/env bash
# Everything that should pass before calling a change done.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m py_compile src/*.py
if [ ! -d build ]; then
  meson setup build --prefix="$PWD/build/install" -Dprofile=development
fi
meson compile -C build
meson test -C build --print-errorlogs --suite apple-music
echo "check: ok"
