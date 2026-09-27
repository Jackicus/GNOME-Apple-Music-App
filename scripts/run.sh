#!/usr/bin/env bash
# Build the development profile into ./build and run it from there, no root needed.
set -euo pipefail
cd "$(dirname "$0")/.."

prefix="$PWD/build/install"
if [ ! -f build/build.ninja ]; then  # build/ alone may be just build/demo
  meson setup build --prefix="$prefix" -Dprofile=development
fi
meson install -C build --quiet

export GSETTINGS_SCHEMA_DIR="$prefix/share/glib-2.0/schemas"
export XDG_DATA_DIRS="$prefix/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
exec "$prefix/bin/apple-music" "$@"
