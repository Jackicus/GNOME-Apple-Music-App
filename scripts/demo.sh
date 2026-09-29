#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

# Run the development build on the invented demo library in build/demo, generating it first if
# it is missing. No Chrome, no account: for UI work and screenshots. An APPLE_MUSIC_CACHE already
# set wins (a bigger generated library, say) and is used as it is.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${APPLE_MUSIC_CACHE:-}" ] && [ ! -f build/demo/library.json ]; then
  scripts/demo_library.py --cache build/demo
fi
exec scripts/run.sh --demo "$@"
