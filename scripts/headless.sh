#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

# Run a command on a private, invisible display: a headless mutter with a virtual monitor, in
# its own D-Bus session. Test windows never appear on (or take focus from) the desktop, and the
# app's MPRIS player and other bus names never reach the real session.
#   scripts/headless.sh scripts/screenshot.py build/shot.png --demo --page albums
#   scripts/headless.sh scripts/a11y_check.py --size 360x640
#   HEADLESS_SIZE=2560x1440 scripts/headless.sh scripts/scroll_test.py --page songs
# Timings measured here (bench.py, scroll_test.py) are only comparable with other headless runs.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ $# -eq 0 ]; then
  echo "usage: scripts/headless.sh COMMAND [ARGUMENT…]" >&2
  exit 2
fi
command -v mutter >/dev/null || { echo "headless.sh: mutter is not installed" >&2; exit 1; }
command -v dbus-run-session >/dev/null || { echo "headless.sh: dbus-run-session is not installed" >&2; exit 1; }
export GDK_BACKEND=wayland GIO_USE_VFS=local
unset DISPLAY  # nothing may fall back to the real X server (Xwayland)
exec dbus-run-session -- mutter --headless --wayland --no-x11 \
  --wayland-display="apple-music-headless-$$" \
  --virtual-monitor "${HEADLESS_SIZE:-1920x1080}" -- "$@" 2> >(grep -v -e '^libmutter-Message' -e 'dbus-daemon\[' -e 'xdg-desktop-portal-WARNING' \
  -e 'high priority EGL context' -e "connection to the bus can't be made" >&2)
