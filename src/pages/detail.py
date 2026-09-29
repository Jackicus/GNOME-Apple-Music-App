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
from ..widgets.labels import track_label
from ..widgets.track_row import TrackRow
from ..widgets.util import HeaderTitle, MappedHandlers, connect_weak, weak_method
from . import app, show_notes

log = logging.getLogger(__name__)

# The kinds whose tracks the engine's item() fetches when an Item came without them.
FETCHED_KINDS = ('album', 'playlist')


def resolve_artist(library, item):
    """The artist Item an album's subtitle names, for the link to their page: by the artist
    id the album carries, if any, else the library's artist of that name (case folded), else
    None (the subtitle stays plain text). Only albums: a playlist's subtitle is its curator."""
    if item is None or item.kind != 'album' or not item.subtitle:
        return None
    artist_id = item.raw.get('artistId')
    if artist_id:
        found = library.by_id('artist', artist_id)
        if found is not None:
            return found
    name = item.subtitle.casefold()
    return next((artist for artist in library.artists if artist.title.casefold() == name),
                None)


def should_fetch(item, fetched):
    """Whether a page showing `item` asks the engine for its tracks: it came without them (a
    shelf's, a search's, an artist's album the library lacks), it is of a kind the engine
    fetches, and this page has not asked for this Item already (`fetched`): an answer
    without tracks (an empty album or playlist) is shown as such, not asked for again."""
    return (item is not None and not item.groups and item.kind in FETCHED_KINDS
            and fetched is not item)


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

    Either way the page follows the Item it shows while it is shown: a reload keeps the Item
    and tells what changed, through `notify` (the hero's labels and cover) and
    `groups-changed` (the tracks), and do_map catches up with what changed while it was
    hidden. An Item that came without its tracks has them fetched once (should_fetch());
    the fetch is cancelled when the page is hidden (do_hidden), and asked again when it
    shows.

    The hero: an album's artist links to their page (resolve_artist()), a More Options menu
    button offers the item's own menu (window.item_actions), and the notes show three lines,
    with More for the whole text. The header bar shows the title once the hero's has scrolled
    away (HeaderTitle). Tab from the hero's last button goes on into the tracks, and Shift+Tab
    from the first track back to it.
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
    artist_button = Gtk.Template.Child()
    artist_label = Gtk.Template.Child()
    caption_label = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    shuffle_button = Gtk.Template.Child()
    more_button = Gtk.Template.Child()
    summary_box = Gtk.Template.Child()
    summary_label = Gtk.Template.Child()
    more_notes_button = Gtk.Template.Child()
    scrolled_window = Gtk.Template.Child()
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
        self._fetched = None  # the Item this page asked the engine for (once: should_fetch)
        self._fetch_task = None
        self._shown_groups = None  # item.groups when the tracks were shown: a new list is new
        self._artist = None  # the artist Item the subtitle links to
        self._painted = None  # (frame clock, handler): the notes' More follows each paint
        self._focused = False  # the page has put the focus on Play once, as it was pushed
        # What the status box says when the engine cannot answer, and what its button does.
        self._engine_status = EngineStatus(app(), self._show_status, self._refetch, {
            'engine-down': _('Start the engine to load the songs'),
            'not-signed-in': _('The songs appear once you sign in to Apple Music'),
            'failed': _('Could Not Load the Songs'),
        })

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
        connect_weak(self.artist_button, 'clicked', self._on_artist_clicked)
        connect_weak(self.more_notes_button, 'clicked', self._on_more_notes_clicked)
        self.more_button.set_create_popup_func(weak_method(self._on_more_popup))
        self._header_title = HeaderTitle(self.header_bar, self.title_label, self.scrolled_window)
        # Tab from the hero's last button goes on into the tracks (_on_list_key_pressed).
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        connect_weak(keys, 'key-pressed', self._on_list_key_pressed)
        self.list_view.add_controller(keys)

        # The library, and the Item shown (_watch), followed while the page is shown.
        self._handlers = MappedHandlers(self)
        self._handlers.add(library, 'notify::state', self._follow)
        self._handlers.add(library, 'changed', self._follow)

        self._hero_section = Gio.ListStore(item_type=GObject.Object)
        self._hero_section.append(_Hero())
        self._sections = Gio.ListStore(item_type=Gio.ListModel)
        self._rows = Gtk.FlattenListModel(model=self._sections)
        self.list_view.set_model(Gtk.NoSelection(model=self._rows))

        self._show(item)

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._follow()  # what a reload changed while the page was hidden
        self._engine_status.watch()
        if should_fetch(self.item, self._fetched):
            self._fetch(self.item)  # a fetch cancelled when the page was hidden
        clock = self.get_frame_clock()
        if clock is not None and self._painted is None:
            self._painted = (clock, connect_weak(clock, 'after-paint', self._on_painted))

    def do_unmap(self):
        self._engine_status.unwatch()
        if self._painted is not None:
            clock, handler = self._painted
            clock.disconnect(handler)
            self._painted = None
        Adw.NavigationPage.do_unmap(self)

    def do_shown(self):
        # A push focuses the page's first button, the artist's link: Play is the hero's.
        if not self._focused:
            self._focused = True
            focus = self.get_root().get_focus() if self.get_root() is not None else None
            if focus is not None and (focus is self.artist_button
                                      or focus.is_ancestor(self.artist_button)):
                self.play_button.grab_focus()
        Adw.NavigationPage.do_shown(self)

    def do_hidden(self):
        # Left (popped, covered, or another destination shown), not just unmapped (a push
        # maps, unmaps and maps a page again): the fetch stops, asked again when shown.
        if self._fetch_task is not None and not self._fetch_task.done():
            self._fetch_task.cancel()
            self._fetch_task = None
            self._fetched = None
            self._engine_status.clear()
        Adw.NavigationPage.do_hidden(self)

    def _follow(self, *_args):
        item = self._find() if self._find is not None else self.item
        if item is not self.item or (item is not None and item.groups is not self._shown_groups):
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

    def _watch(self, old, item):
        """Follow the Item shown (and no longer the one shown before)."""
        if old is not None:
            self._handlers.remove(old)
        if item is not None:
            self._handlers.add(item, 'groups-changed', self._on_groups_changed)
            self._handlers.add(item, 'notify', self._on_item_notify)

    def _on_groups_changed(self, item):
        if item is self.item:
            self._show(item)

    def _on_item_notify(self, item, _pspec):
        if item is self.item:
            self._show_hero(item)

    def _show(self, item):
        if item is not self.item:
            self._watch(self.item, item)
            self.item = item
        if item is None:
            self._shown_groups = None
            self._sections.remove_all()
            self._update_state()
            return
        self._album_artist = item.subtitle if item.kind == 'album' else None
        self._show_hero(item)

        self._shown_groups = item.groups
        groups = [group for group in item.groups if group.entries.get_n_items()]
        self._starts = []
        self._headings = []
        position = 1  # after the hero
        for number, group in enumerate(groups, 1):
            self._starts.append(position)
            self._headings.append(self._heading(item, group, number))
            position += group.entries.get_n_items()

        # An item that came without its tracks (a shelf's) gets them from the engine, once.
        if should_fetch(item, self._fetched):
            self._fetch(item)
        elif groups:
            self._engine_status.clear()
        elif self._engine_status.status is None:
            self._show_empty()  # no tracks: none came, or an answer brought none
        self.status_box.set_visible(not groups)
        self._update_buttons()

        self.list_view.set_header_factory(self._header_factory if len(groups) > 1 else None)
        self._sections.splice(0, self._sections.get_n_items(),
                              [self._hero_section] + [group.entries for group in groups])
        self._update_state()

    def _show_hero(self, item):
        """The hero's cover, labels and buttons for item."""
        if not self._root:
            self.set_title(item.title)
        self.cover.set_paths(item.art, item.thumb)  # the 640 px cover, else the thumbnail
        if item.raw.get('artUrl'):
            # A sync fetches thumbnails only: the cover comes now, if it is not on disk yet.
            app().spawn(self._fetch_cover(item))
        self.title_label.set_label(item.title)
        self._artist = resolve_artist(self._library, item)
        self.subtitle_label.set_label(item.subtitle)
        self.subtitle_label.set_visible(bool(item.subtitle) and self._artist is None)
        self.artist_label.set_label(item.subtitle)
        self.artist_button.set_visible(self._artist is not None)
        details = [item.genre, str(item.year) if item.year else None, item.count_label]
        self.caption_label.set_label(' · '.join(detail for detail in details if detail))
        self.caption_label.set_visible(any(details))
        self.summary_label.set_label(item.summary or '')
        self.summary_box.set_visible(bool(item.summary))
        self._update_buttons()

    def _update_buttons(self):
        """Play and Shuffle: with something to play, and tracks to play (or on their way: the
        engine plays an item by its id)."""
        item = self.item
        playable = item is not None and bool(item.play) and (
            any(group.entries.get_n_items() for group in item.groups)
            or self._engine_status.status == 'loading')
        self.play_button.set_sensitive(playable)
        self.shuffle_button.set_sensitive(playable)

    async def _fetch_cover(self, item):
        if await fetch_cover(item) and self.item is item:
            self.cover.refresh()

    # Fetching the tracks of an item that came without them.

    def _fetch(self, item):
        self._fetched = item
        if self._fetch_task is not None and not self._fetch_task.done():
            self._fetch_task.cancel()
        self._engine_status.loading()
        self._fetch_task = app().spawn(self._fetch_groups(item))

    def _refetch(self):
        """EngineStatus's retry (Try Again, the engine up, signed in): ask again."""
        self._fetched = None
        if self.item is not None:
            self._fetch(self.item)

    async def _fetch_groups(self, item):
        try:
            answer = await app().engine.item(item.kind, item.id)
        except EngineError as error:
            log.info('tracks of %s %s: %s', item.kind, item.id, error)
            if self.item is item:
                self._engine_status.fail(error)
            return
        if self.item is not item:
            return  # the page shows something else now
        self._engine_status.clear()
        item.merge(answer)  # new groups emit groups-changed, which shows them while mapped
        if item.groups is not self._shown_groups or not item.groups:
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
        self._update_buttons()

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
            # The artist when the row shows one (not an album's own): TrackRow's rule.
            show_artist = self._album_artist is None or entry.artist != self._album_artist
            list_item.set_accessible_label(track_label(entry, show_artist, show_album=False))
            list_item.set_accessible_description(entry.duration_label or '')
        else:
            row.show_hero(self.hero)
            list_item.set_accessible_label('')
            list_item.set_accessible_description('')

    def _last_button(self):
        """The hero's last button that takes the focus: More Options, Shuffle or Play."""
        for button in (self.more_button, self.shuffle_button, self.play_button):
            if button.get_visible() and button.get_sensitive():
                return button
        return None

    def _on_list_key_pressed(self, _controller, keyval, _keycode, state):
        """Tab from the hero's last button into the tracks, and Shift+Tab from the first
        track back to it. The list's Tab leaves it after the focused item (tab-behavior
        item), and the hero is its first item, so the tracks would otherwise be reached only
        with Down, and the hero's buttons backwards only with Up."""
        mods = state & Gtk.accelerator_get_default_mod_mask()
        forward = keyval in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab) and not mods
        backward = (keyval == Gdk.KEY_ISO_Left_Tab
                    or (keyval in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab)
                        and mods == Gdk.ModifierType.SHIFT_MASK))
        if not (forward or backward) or self._rows.get_n_items() < 2:
            return False
        focus = self.get_root().get_focus() if self.get_root() is not None else None
        last = self._last_button()
        if focus is None or last is None:
            return False
        if forward and (focus is last or focus.is_ancestor(last)):
            self.list_view.scroll_to(1, Gtk.ListScrollFlags.FOCUS, None)
            return True
        if backward:
            row = focus if isinstance(focus, _Row) else focus.get_ancestor(_Row)
            if row is None and focus.get_first_child() is not None:
                row = focus.get_first_child()  # the list item's own widget: its row inside
            first = self._rows.get_item(1)
            if isinstance(row, _Row) and row.track_row.context_item is first:
                last.grab_focus()
                return True
        return False

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

    # The hero's links and menus.

    def _on_artist_clicked(self, _button):
        if self._artist is not None:
            self.get_root().open_item(self._artist)

    def _on_more_popup(self, button):
        """The item's menu, made as it opens: whether it is a favourite is asked then."""
        actions = getattr(self.get_root(), 'item_actions', None)
        button.set_menu_model(actions.menu_for(self.item) if actions and self.item else None)

    def _on_more_notes_clicked(self, _button):
        if self.item is not None:
            show_notes(self, self.item.title, self.item.summary or '')

    def _on_painted(self, _clock):
        """More under the notes, while they are cut to their three lines."""
        layout = self.summary_label.get_layout()
        cut = self.summary_label.get_mapped() and layout is not None and layout.is_ellipsized()
        if cut != self.more_notes_button.get_visible():
            self.more_notes_button.set_visible(cut)

    def _on_play_clicked(self, _button):
        self.get_root().play_request(self.item.play, shuffle=False)

    def _on_shuffle_clicked(self, _button):
        self.get_root().play_request(self.item.play, shuffle=True)
