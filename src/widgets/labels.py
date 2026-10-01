# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""What tiles and rows read to assistive technology: the rules in one place, the words
looked up once (a list binds its rows by the thousand, and gettext is slow when hot).

    list_item.set_accessible_label(accessible_label(item, artist=tile_is_an_artist))
    bind_label(list_item, item, artist)   # the same, following a renamed Item (unbind_label)
    list_item.set_accessible_label(track_label(track, show_artist=True, show_album=True))
    child = flow_child(tile, accessible_label(item))   # a Gtk.FlowBox's create function

No template here, so the unit tests import it without a display.
"""

from gettext import gettext as _

from gi.repository import Gtk

_formats = {}


def _format(name):
    if not _formats:
        _formats.update({
            # Translators: what a screen reader says for a tile: its title, then its
            # subtitle (an album's artist, a playlist's curator): "Album, Artist".
            'subtitle': _('{title}, {subtitle}'),
            # Translators: what a screen reader says for a song's row is its title, then its
            # artist and album, each added to what comes before it with this, e.g.
            # "Song, Artist" then "Song, Artist, Album".
            'part': _('{label}, {part}'),
            # Translators: added to what a screen reader says for a song with explicit lyrics.
            'explicit': _('{label}, explicit'),
        })
    return _formats[name]


def accessible_label(item, artist=False):
    """A tile's name: the title and the subtitle; the title alone for an artist (`artist`, or
    an Item of kind 'artist': the name is all there is) or an item without a subtitle."""
    if artist or item.kind == 'artist' or not item.subtitle:
        return item.title
    return _format('subtitle').format(title=item.title, subtitle=item.subtitle)


def track_label(track, show_artist=True, show_album=True):
    """A track row's name: its title, then its artist and its album when the row shows them
    and they are known (a missing one is left out, not read as an empty field), and
    "explicit" for explicit lyrics (the badge the row shows)."""
    parts = [track.title, track.artist if show_artist else '',
             track.album if show_album else '']
    parts = [part for part in parts if part]
    label = parts[0] if parts else ''
    for part in parts[1:]:
        label = _format('part').format(label=label, part=part)
    if track.explicit:
        label = _format('explicit').format(label=label)
    return label


def song_label(item):
    """A song Item's row name (an artist's Top Songs): its title, its album when known, and
    "explicit" for explicit lyrics, as track_label() words a track."""
    label = item.title
    album = item.raw.get('album') if isinstance(item.raw, dict) else None
    if album:
        label = _format('part').format(label=label, part=album)
    if item.explicit:
        label = _format('explicit').format(label=label)
    return label


def flow_child(widget, label):
    """A Gtk.FlowBoxChild holding widget, named `label` for assistive technology (a flow box's
    children are not list items: their name is set on the child)."""
    child = Gtk.FlowBoxChild(child=widget)
    child.update_property([Gtk.AccessibleProperty.LABEL], [label])
    return child


def bind_label(list_item, item, artist=False):
    """Name a recycled tile's list item accessible_label(item, artist), and name it again
    whenever the Item's title or subtitle changes while it is bound: a reload merges new
    values into the Item and a list does not rebind it (the tile follows its Item the same
    way). unbind_label() in the factory's unbind stops it."""
    unbind_label(list_item)
    list_item.set_accessible_label(accessible_label(item, artist))

    def on_notify(notified, pspec):
        if pspec.name in ('title', 'subtitle'):
            list_item.set_accessible_label(accessible_label(notified, artist))

    list_item.label_handler = (item, item.connect('notify', on_notify))


def unbind_label(list_item):
    """Stop following what bind_label() followed."""
    followed = getattr(list_item, 'label_handler', None)
    if followed is not None:
        item, handler = followed
        item.disconnect(handler)
        list_item.label_handler = None
