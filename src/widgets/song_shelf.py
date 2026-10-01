# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicSongShelf: a titled grid of songs, three rows high, scrolling sideways (an
artist's Top Songs); AppleMusicSongRow: one song in it."""

from gettext import gettext as _
from gettext import pgettext as C_

from gi.repository import Gtk, Pango

from . import context_menu
from .cover import Cover
from .labels import song_label
from .shelf import PagedRow
from .util import connect_weak

# A song's width in the grid: two columns and a margin fit a 720 px window, one a 360 px.
ROW_WIDTH = 300
COVER_SIZE = 48


def song_caption(item):
    """A song row's second line: its album and year ("Album · 2026"), or whichever it has."""
    parts = [item.raw.get('album') or '', str(item.year) if item.year else '']
    return ' · '.join(part for part in parts if part)


class SongRow(Gtk.Box):
    """A song in the grid: its album's cover, its title (with the explicit badge) and its
    album and year. bind(item) and unbind() as the grid recycles it; it follows the Item for
    a thumbnail that arrives later (notify::thumb)."""

    __gtype_name__ = 'AppleMusicSongRow'

    def __init__(self):
        super().__init__(spacing=12, width_request=ROW_WIDTH)
        self.add_css_class('song-row')
        self._item = None
        self._handler = None
        self.cover = Cover(size=COVER_SIZE, valign=Gtk.Align.CENTER,
                           accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.cover.add_css_class('small')
        self.append(self.cover)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                       hexpand=True, spacing=2)
        line = Gtk.Box(spacing=6)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                               single_line_mode=True)
        line.append(self.title)
        # Decoration: the row's name says "explicit" (song_label).
        self.badge = Gtk.Label(
            # Translators: the badge marking a song with explicit lyrics, as Apple Music shows it
            label=C_('explicit badge', 'E'), tooltip_text=_('Explicit'), visible=False,
            valign=Gtk.Align.CENTER, accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.badge.add_css_class('caption')
        self.badge.add_css_class('explicit-badge')
        line.append(self.badge)
        text.append(line)
        self.caption = Gtk.Inscription(
            xalign=0, text_overflow=Gtk.InscriptionOverflow.ELLIPSIZE_END)
        self.caption.add_css_class('caption')
        self.caption.add_css_class('dimmed')
        text.append(self.caption)
        self.append(text)

    @property
    def context_item(self):
        """The song shown, for its context menu (widgets/context_menu.py)."""
        return self._item

    def bind(self, item):
        if self._item is not None:
            self.unbind()
        self._item = item
        # The Item outlives the row (the page's shelf holds it): it holds the row weakly.
        self._handler = connect_weak(item, 'notify::thumb', self._on_thumb)
        self.title.set_text(item.title)
        self.badge.set_visible(item.explicit)
        self.caption.set_text(song_caption(item))
        self.cover.set_paths(item.thumb, item.art)

    def unbind(self):
        if self._handler is not None:
            self._item.disconnect(self._handler)
            self._handler = None
        self._item = None
        self.cover.set_paths()

    def _on_thumb(self, item, _pspec):
        if not self.cover.set_paths(item.thumb, item.art):
            self.cover.refresh()


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/song_shelf.ui')
class SongShelf(PagedRow, Gtk.Box):
    """A shelf of songs (any object with `title`, `items`, a Gio.ListStore of song Items, and
    `more`, whether Apple has more than these), as a grid three rows high under its title,
    with a shelf's paging arrows and See All (window.open_shelf, offered when the grid does
    not show everything: wider than the window, or `more`).

    Activating a song plays them all from it, in their order, as music.apple.com does: a
    queue of their ids ({kind: songs}) started at that song (window.play_request); a right
    click, a long press or the Menu key opens its context menu."""

    __gtype_name__ = 'AppleMusicSongShelf'

    title_label = Gtk.Template.Child()
    previous_button = Gtk.Template.Child()
    next_button = Gtk.Template.Child()
    see_all_button = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
    grid_view = Gtk.Template.Child()

    shelf = None
    _see_all = True
    _followed = None  # (items, handler id)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        factory = Gtk.SignalListItemFactory()
        connect_weak(factory, 'setup', self._on_setup)
        connect_weak(factory, 'bind', self._on_bind)
        connect_weak(factory, 'unbind', self._on_unbind)
        self.grid_view.set_factory(factory)
        connect_weak(self.grid_view, 'activate', self._on_activate)
        connect_weak(self.see_all_button, 'clicked', self._on_see_all_clicked)
        self._connect_controls()
        self._row = self.grid_view
        context_menu.attach(self.grid_view)

    def bind_shelf(self, shelf):
        if shelf is self.shelf:
            return
        self.clear()
        self.shelf = shelf
        self._followed = (shelf.items, connect_weak(shelf.items, 'items-changed',
                                                    self._on_items_changed))
        self.title_label.set_label(shelf.title)
        self.grid_view.update_property([Gtk.AccessibleProperty.LABEL], [shelf.title])
        self.grid_view.set_model(Gtk.NoSelection(model=shelf.items))
        self.scrolled_window.get_hadjustment().set_value(0)
        self._update_controls()

    def clear(self):
        if self._followed is not None:
            items, handler = self._followed
            items.disconnect(handler)
            self._followed = None
        self.shelf = None
        self.grid_view.set_model(None)

    def _truncated(self):
        return self.shelf is not None and bool(getattr(self.shelf, 'more', False))

    def _on_items_changed(self, *_args):
        self._update_controls()

    def _on_setup(self, _factory, list_item):
        list_item.set_child(SongRow())

    def _on_bind(self, _factory, list_item):
        item = list_item.get_item()
        list_item.get_child().bind(item)
        list_item.set_accessible_label(song_label(item))

    def _on_unbind(self, _factory, list_item):
        list_item.get_child().unbind()

    def queue(self):
        """The songs as one queue: {kind: songs, id: their ids, joined}."""
        items = self.shelf.items if self.shelf is not None else []
        return {'kind': 'songs', 'id': ','.join(item.id for item in items)}

    def _on_activate(self, _grid_view, position):
        item = self.shelf.items.get_item(position) if self.shelf is not None else None
        if item is not None:
            self.get_root().play_request(self.queue(), start_with=position, start_id=item.id)

    def _on_see_all_clicked(self, _button):
        if self.shelf is not None:
            self.get_root().open_shelf(self.shelf)

