"""AppleMusicDetailPage: an album or a playlist, its cover, titles, Play and Shuffle over its
tracks.

One Gtk.ListView holds the whole page. Its model flattens a store of sections
(Gtk.FlattenListModel, whose sections are its models): first a store of one marker item, whose
row the factory fills with the page's hero, then each group's store of tracks, headed "Disc 1",
"Disc 2"… when there is more than one. Why not the hero above a list per group in a box: a
Gtk.ListView recycles its rows only as the scrollable child of a Gtk.ScrolledWindow; in a box it
builds a row for every track, up to 200, and leaves the rest blank, and a playlist can hold
thousands of songs. Why not the hero in a list header: Tab never reaches the buttons of a
header, where it does reach an item's.
"""

import bisect
import logging
from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GObject, Gtk, Pango

from ..backend.errors import EngineError
from ..library import Track
from ..remote import fetch_cover
from ..widgets import context_menu
from ..widgets.cover import Cover  # noqa: F401  registers $AppleMusicCover for the template
from ..widgets.engine_status import EngineStatus
from ..widgets.track_row import TrackRow
from ..widgets.util import MappedHandlers, connect_weak
from . import app

log = logging.getLogger(__name__)

# The kinds whose tracks the engine's item() fetches when an Item came without them.
FETCHED_KINDS = ('album', 'playlist')


class _Hero(GObject.Object):
    """The first item of a detail page's list: where the hero goes."""

    __gtype_name__ = 'AppleMusicDetailHero'


class _Row(Gtk.Box):
    """A row of a detail page's list: a TrackRow, or the page's hero in its place."""

    def __init__(self):
        super().__init__()
        self.track_row = TrackRow(hexpand=True)
        self.append(self.track_row)

    def show_hero(self, hero):
        parent = hero.get_parent()
        if parent is not self:
            if parent is not None:
                parent.remove(hero)
            self.append(hero)
        self.track_row.set_visible(False)

    def show_track(self, track, album_artist):
        self.track_row.set_visible(True)
        self.track_row.bind(track, album_artist)

    def clear(self, hero):
        if hero.get_parent() is self:
            self.remove(hero)
        self.track_row.unbind()


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/detail.ui')
class DetailPage(Adw.NavigationPage):
    """An album's or a playlist's page.

    DetailPage(library, item) shows item, pushed over the page it was opened from, its title in
    the header bar. As a destination's root page (Favourite Songs), DetailPage(library,
    find=function, root=True, title=…) shows whatever find() returns, asked again whenever the
    library changes: a spinner while it loads, an empty state (icon_name, empty_title,
    empty_description) when find() has nothing, and no title in the header bar.
    """

    __gtype_name__ = 'AppleMusicDetailPage'

    header_bar = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    list_view = Gtk.Template.Child()
    hero = Gtk.Template.Child()
    cover = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()
    caption_label = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    shuffle_button = Gtk.Template.Child()
    summary_label = Gtk.Template.Child()
    status_box = Gtk.Template.Child()
    status_icon = Gtk.Template.Child()
    status_spinner = Gtk.Template.Child()
    status_title = Gtk.Template.Child()
    status_description = Gtk.Template.Child()
    status_button = Gtk.Template.Child()

    def __init__(self, library, item=None, find=None, root=False, title=None, icon_name=None,
                 empty_title=None, empty_description=None):
        super().__init__(title=title or (item.title if item is not None else ''))
        self.item = None  # what the page shows
        self._library = library
        self._find = find
        self._root = root
        self._album_artist = None  # an album's artist, whose name its rows leave out
        self._starts = []  # the list position of each section of tracks
        self._headings = []  # and its heading
        self._fetching = None  # the Item whose tracks the engine is fetching
        # What the status box says when the engine cannot answer, and what its button does.
        self._engine_status = EngineStatus(app(), self._show_status, self._refetch, {
            'engine-down': _('Start the engine to load the songs'),
            'not-signed-in': _('The songs appear once you sign in to Apple Music'),
            'failed': _('Could Not Load the Songs'),
        })

        self.header_bar.set_show_title(not root)
        self.empty_page.set_icon_name(icon_name)
        self.empty_page.set_title(empty_title or self.get_title())
        self.empty_page.set_description(empty_description)

        # Every signal of a child or of an object the page holds is connected weakly
        # (widgets/util.py): a bound method would keep the page alive once popped.
        factory = Gtk.SignalListItemFactory()
        connect_weak(factory, 'setup', self._on_setup)
        connect_weak(factory, 'bind', self._on_bind)
        connect_weak(factory, 'unbind', self._on_unbind)
        self.list_view.set_factory(factory)
        connect_weak(self.list_view, 'activate', self._on_activate)
        context_menu.attach(self.list_view, drag=True)  # the tracks' menus, dragged to playlists
        self._header_factory = Gtk.SignalListItemFactory()
        connect_weak(self._header_factory, 'setup', self._on_setup_header)
        connect_weak(self._header_factory, 'bind', self._on_bind_header)
        connect_weak(self.play_button, 'clicked', self._on_play_clicked)
        connect_weak(self.shuffle_button, 'clicked', self._on_shuffle_clicked)
        connect_weak(self.status_button, 'clicked', self._on_status_clicked)
        # What a track's row reads to assistive technology (looked up once: rows bind often).
        self._label_formats = {
            'artist': _('{title}, {artist}'),
            'explicit': _('{label}, explicit'),
        }
        # Tab from Shuffle goes on into the tracks (see _on_list_key_pressed).
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        connect_weak(keys, 'key-pressed', self._on_list_key_pressed)
        self.list_view.add_controller(keys)

        self._handlers = MappedHandlers(self)
        if find is not None:
            self._handlers.add(library, 'notify::state', self._follow)
            self._handlers.add(library, 'changed', self._follow)

        self._hero_section = Gio.ListStore(item_type=GObject.Object)
        self._hero_section.append(_Hero())
        self._sections = Gio.ListStore(item_type=Gio.ListModel)
        self._rows = Gtk.FlattenListModel(model=self._sections)
        self.list_view.set_model(Gtk.NoSelection(model=self._rows))

        self._show(item)

    # A root page follows the library, but only while it is shown: the library outlives the
    # window.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        if self._find is not None:
            self._follow()
        self._engine_status.watch()

    def do_unmap(self):
        self._engine_status.unwatch()
        Adw.NavigationPage.do_unmap(self)

    def _follow(self, *_args):
        item = self._find()
        if item is not self.item:
            self._show(item)
        self._update_state()

    def _update_state(self):
        if self.item is not None:
            name = 'item'
        elif self._library.state == 'loading':
            name = 'loading'
        else:
            name = 'empty'
        self.stack.set_visible_child_name(name)

    def _show(self, item):
        self.item = item
        if item is None:
            self._sections.remove_all()
            self._update_state()
            return
        if not self._root:
            self.set_title(item.title)
        self._album_artist = item.subtitle if item.kind == 'album' else None

        self.cover.set_paths(item.art, item.thumb)  # the 640 px cover, else the thumbnail
        if item.raw.get('artUrl'):
            # A sync fetches thumbnails only: the cover comes now, if it is not on disk yet.
            app().spawn(self._fetch_cover(item))
        self.title_label.set_label(item.title)
        self.subtitle_label.set_label(item.subtitle)
        self.subtitle_label.set_visible(bool(item.subtitle))
        details = [item.genre, str(item.year) if item.year else None, item.count_label]
        self.caption_label.set_label(' · '.join(detail for detail in details if detail))
        self.caption_label.set_visible(any(details))
        self.summary_label.set_label(item.summary or '')
        self.summary_label.set_visible(bool(item.summary))
        self.play_button.set_sensitive(bool(item.play))
        self.shuffle_button.set_sensitive(bool(item.play))

        groups = [group for group in item.groups if group.entries.get_n_items()]
        self._starts = []
        self._headings = []
        position = 1  # after the hero
        for number, group in enumerate(groups, 1):
            self._starts.append(position)
            self._headings.append(self._heading(item, group, number))
            position += group.entries.get_n_items()

        # An item that came without its tracks (a shelf's) gets them from the engine.
        if not item.groups and item.kind in FETCHED_KINDS and self._fetching is not item:
            self._fetch(item)
        elif not item.groups and self._fetching is not item:
            self._show_empty()
        elif item.groups:
            self._show_empty()
        self.status_box.set_visible(not groups)

        self.list_view.set_header_factory(self._header_factory if len(groups) > 1 else None)
        self._sections.splice(0, self._sections.get_n_items(),
                              [self._hero_section] + [group.entries for group in groups])
        self._update_state()

    async def _fetch_cover(self, item):
        if await fetch_cover(item) and self.item is item:
            self.cover.refresh()

    # Fetching the tracks of an item that came without them.

    def _fetch(self, item):
        self._fetching = item
        self._engine_status.loading()
        app().spawn(self._fetch_groups(item))

    def _refetch(self):
        """EngineStatus's retry: fetch the tracks again."""
        if self.item is not None:
            self._fetch(self.item)

    async def _fetch_groups(self, item):
        try:
            answer = await app().engine.item(item.kind, item.id)
        except EngineError as error:
            log.info('tracks of %s %s: %s', item.kind, item.id, error)
            if self._fetching is item:
                self._fetching = None
                if self.item is item:
                    self._engine_status.fail(error)
            return
        if self._fetching is item:
            self._fetching = None
        item.merge(answer)
        if self.item is item:
            self._show(item)

    def _show_empty(self):
        self._engine_status.clear()
        self._show_status('empty', _('No Songs'), '', None)

    def _show_status(self, status, title, description, button):
        """The status box: the spinner ('loading'), EngineStatus's states, or 'empty' (no
        tracks at all)."""
        loading = status == 'loading'
        if loading:
            title, description, button = _('Loading…'), '', None
        self.status_spinner.set_visible(loading)
        self.status_icon.set_visible(not loading)
        self.status_title.set_label(title)
        self.status_description.set_label(description)
        self.status_description.set_visible(bool(description))
        self.status_button.set_label(button or '')
        self.status_button.set_visible(bool(button))

    def _on_status_clicked(self, _button):
        self._engine_status.activate()

    def _heading(self, item, group, number):
        """An album's discs are numbered, whatever their groups are called; anything else's
        groups keep their names."""
        if item.kind == 'album':
            disc = group.entries.get_item(0).disc_number or number
            return _('Disc {number}').format(number=disc)
        return group.name

    # The list.

    def _on_setup(self, _factory, list_item):
        list_item.set_child(_Row())

    def _on_bind(self, _factory, list_item):
        entry = list_item.get_item()
        row = list_item.get_child()
        is_track = isinstance(entry, Track)
        list_item.set_activatable(is_track)
        list_item.set_focusable(is_track)  # the hero's buttons take the focus, not its row
        if is_track:
            row.show_track(entry, self._album_artist)
            list_item.set_accessible_label(self._track_label(entry))
            list_item.set_accessible_description(entry.duration_label or '')
        else:
            row.show_hero(self.hero)
            list_item.set_accessible_label('')
            list_item.set_accessible_description('')

    def _track_label(self, track):
        """A track row's accessible name: the title, the artist when the row shows one, and
        whether it is explicit (the badge's)."""
        label = track.title
        if track.artist and (self._album_artist is None or track.artist != self._album_artist):
            label = self._label_formats['artist'].format(title=label, artist=track.artist)
        if track.explicit:
            label = self._label_formats['explicit'].format(label=label)
        return label

    def _on_list_key_pressed(self, _controller, keyval, _keycode, state):
        """Tab from the hero's last button (Shuffle) into the tracks. The list's Tab leaves
        it after the focused item (tab-behavior item), and the hero is its first item, so the
        tracks would otherwise be reached only with Down."""
        if keyval not in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab):
            return False
        if state & Gtk.accelerator_get_default_mod_mask():
            return False
        focus = self.get_root().get_focus() if self.get_root() is not None else None
        if focus is None or not (focus is self.shuffle_button
                                 or focus.is_ancestor(self.shuffle_button)):
            return False
        if self._rows.get_n_items() < 2:
            return False
        self.list_view.scroll_to(1, Gtk.ListScrollFlags.FOCUS, None)
        return True

    def _on_unbind(self, _factory, list_item):
        list_item.get_child().clear(self.hero)

    def _on_setup_header(self, _factory, header):
        label = Gtk.Label(xalign=0, margin_start=24, margin_end=24, margin_top=18,
                          margin_bottom=6, ellipsize=Pango.EllipsizeMode.END)
        label.add_css_class('heading')
        header.set_child(label)

    def _on_bind_header(self, _factory, header):
        label = header.get_child()
        section = bisect.bisect_right(self._starts, header.get_start()) - 1
        label.set_visible(section >= 0)  # the hero's section has no heading
        label.set_label(self._headings[section] if section >= 0 else '')

    def _on_activate(self, _list_view, position):
        track = self._rows.get_item(position)
        if isinstance(track, Track):
            self.get_root().play_request(track.play, start_with=track.index)

    def _on_play_clicked(self, _button):
        self.get_root().play_request(self.item.play)

    def _on_shuffle_clicked(self, _button):
        self.get_root().play_request(self.item.play, shuffle=True)
