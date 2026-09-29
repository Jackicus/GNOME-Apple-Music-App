# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The engine's backend: Chrome DevTools, the page bridge, and turning Apple's answers into data.

Pure Python and the standard library: nothing here imports gi (GTK stays in the app), and
importing the package imports none of its modules.

    errors.py     EngineError(code, message): engine-down, no-browser, not-signed-in, api,
                  timeout, usage
    api.py        the Apple Music API apart from any connection: library ids, an item's
                  endpoint, resource types, a page's data, Apple's error answers
    chrome.py     finding Chrome, its argv (--remote-debugging-pipe; the host's Chrome through
                  flatpak-spawn from a Flatpak sandbox), which Chrome holds a profile
                  (profile_owner: its SingletonLock), select_page() (the music.apple.com
                  target), get_json() (a DevTools port's /json, for the developer attach)
    client.py     the transports (PipeTransport: Chrome's DevTools pipe; WebSocketTransport: a
                  DevTools port) and CDPClient: one asynchronous CDP connection, attached to
                  the page by session (calls, evaluate, events, the bridge kept in the page
                  across navigations); open_page(transport), attach_devtools(port)
    cdp.py        the RFC 6455 handshake and frame codec as pure functions, for client.py's
                  WebSocketTransport (the developer attach)
    bridge.js     injected into music.apple.com; drives the page's own MusicKit instance and
                  forwards its events (subscribe). Installed as data beside the Python, found
                  through config.BRIDGE_JS; tests/test_bridge.py runs it under gjs
    normalize.py  Apple Music API answers as the library's Item, Track and shelf shapes; the
                  artwork cache (naming, fetching, pruning), library.json, the kept answers
    store.py      writing the cache: one atomic write (a temporary file, fsync'd, renamed
                  over the target) for every file
    config.py     paths, the developer's DevTools port and the artwork sizes
    README.md     the bridge's calls, the error codes, the events, and the library.json, Item
                  and Track shapes

A fork of the GNOME-Apple-Music-Library extension's backend, owned by this app since 0.9;
history in `git log -- src/backend`.
"""
