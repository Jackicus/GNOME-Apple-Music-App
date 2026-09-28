#!/usr/bin/env bash
# Everything that should pass before calling a change done.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m compileall -q src scripts tests
if command -v ruff >/dev/null; then
  ruff check .
elif command -v uvx >/dev/null && uvx --offline ruff --version >/dev/null 2>&1; then
  uvx --offline ruff check .  # the ruff uv has cached
else
  echo "check: ruff not installed, lint skipped"
fi
prefix="$PWD/build/install"
if [ ! -f build/build.ninja ]; then  # build/ alone may be just build/demo
  meson setup build --prefix="$prefix" -Dprofile=development
elif ! meson introspect build --buildoptions | python3 -c '
import json, sys
options = {option["name"]: option["value"] for option in json.load(sys.stdin)}
sys.exit(options.get("prefix") != sys.argv[1] or options.get("profile") != "development")
' "$prefix"; then
  # Configured otherwise (a release install into /usr, say): back to the development
  # profile in build/install rather than building into the wrong place.
  meson configure build -Dprefix="$prefix" -Dprofile=development
fi
# Before the unit tests: the widget tests (tests/gtk.py) load build/src's gresource.
meson compile -C build
python3 -m unittest discover -s tests
meson test -C build --print-errorlogs --suite apple-music --no-suite unit  # the data files
echo "check: ok"
