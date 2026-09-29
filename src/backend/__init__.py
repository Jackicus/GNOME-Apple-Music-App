# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The engine's backend: Chrome DevTools, the page bridge, and Apple's answers as data.

The standard library and asyncio only, never gi; importing the package imports none of its
modules. README.md beside it lists the modules, the bridge's calls, the events and the shapes.
"""
