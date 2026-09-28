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

from gi.repository import Adw, Gio, GObject, Gtk, Pango

from ..backend.errors import EngineError
from ..library import Track
from ..widgets import artwork, context_menu
from ..widgets.cover import Cover  # noqa: F401  registers $AppleMusicCover for the template
from ..widgets.track_row import TrackRow

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
        self._library_handlers = []
        self._album_artist = None  # an album's artist, whose name its rows leave out
        self._starts = []  # the list position of each section of tracks
        self._headings = []  # and its heading
        self._fetching = None  # the Item whose tracks the engine is fetching
        self._status = None  # what the status box says: 'loading', an error code, or 'empty'

        self.header_bar.set_show_title(not root)
        self.empty_page.set_icon_name(icon_name)
        self.empty_page.set_title(empty_title or self.get_title())
        self.empty_page.set_description(empty_description)

        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self._on_setup)
        factory.connect('bind', self._on_bind)
        factory.connect('unbind', self._on_unbind)
        self.list_view.set_factory(factory)
        context_menu.attach(self.list_view, drag=True)  # the tracks' menus, dragged to playlists
        self._header_factory = Gtk.SignalListItemFactory()
        self._header_factory.connect('setup', self._on_setup_header)
        self._header_factory.connect('bind', self._on_bind_header)

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
            self._library_handlers = [
                self._library.connect('notify::state', self._follow),
                self._library.connect('changed', self._follow),
            ]
            self._follow()

    def do_unmap(self):
        for handler in self._library_handlers:
            self._library.disconnect(handler)
        self._library_handlers = []
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
            Gio.Application.get_default().spawn(self._fetch_cover(item))
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
            self._set_status('empty')
        elif item.groups:
            self._set_status('empty')
        self.status_box.set_visible(not groups)

        self.list_view.set_header_factory(self._header_factory if len(groups) > 1 else None)
        self._sections.splice(0, self._sections.get_n_items(),
                              [self._hero_section] + [group.entries for group in groups])
        self._update_state()

    async def _fetch_cover(self, item):
        if await artwork.get_default().fetch_cover(item) and self.item is item:
            self.cover.refresh()

    # Fetching the tracks of an item that came without them.

    def _fetch(self, item):
        self._fetching = item
        self._set_status('loading')
        Gio.Application.get_default().spawn(self._fetch_groups(item))

    async def _fetch_groups(self, item):
        app = Gio.Application.get_default()
        try:
            answer = await app.engine.item(item.kind, item.id)
        except EngineError as error:
            log.info('tracks of %s %s: %s', item.kind, item.id, error)
            if self._fetching is item:
                self._fetching = None
                if self.item is item:
                    self._set_status(error.code, error.message)
            return
        if self._fetching is item:
            self._fetching = None
        item.merge(answer)
        if self.item is item:
            self._show(item)

    def _set_status(self, status, message=''):
        """The status box for `status`: 'loading' (a spinner), an EngineError code with a
        button that helps ('engine-down': Start Engine; 'not-signed-in': Sign In; anything
        else: Try Again), or 'empty' (no tracks at all)."""
        self._status = status
        self.status_spinner.set_visible(status == 'loading')
        self.status_icon.set_visible(status != 'loading')
        if status == 'loading':
            title, description, button = _('Loading…'), '', None
        elif status == 'engine-down':
            title = _('Engine Not Running')
            description = _('Start the engine to load the songs')
            button = _('Start Engine')
        elif status == 'not-signed-in':
            title = _('Sign In to Load This')
            description = _('The songs appear once you sign in to Apple Music')
            button = _('Sign In')
        elif status == 'empty':
            title, description, button = _('No Songs'), '', None
        else:
            title = _('Could Not Load the Songs')
            description = message
            button = _('Try Again')
        self.status_title.set_label(title)
        self.status_description.set_label(description)
        self.status_description.set_visible(bool(description))
        self.status_button.set_label(button or '')
        self.status_button.set_visible(bool(button))

    @Gtk.Template.Callback()
    def on_status_clicked(self, _button):
        app = Gio.Application.get_default()
        if self._status == 'not-signed-in':
            app.activate_action('sign-in')
        elif self._status == 'engine-down':
            self._fetching = self.item
            self._set_status('loading')
            app.spawn(self._start_and_fetch(self.item))
        elif self.item is not None:
            self._fetch(self.item)

    async def _start_and_fetch(self, item):
        app = Gio.Application.get_default()
        try:
            await app.engine.start()
        except EngineError as error:
            app.report(error)
            if self._fetching is item:
                self._fetching = None
                if self.item is item:
                    self._set_status(error.code, error.message)
            return
        await self._fetch_groups(item)

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
        else:
            row.show_hero(self.hero)

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

    @Gtk.Template.Callback()
    def on_activate(self, _list_view, position):
        track = self._rows.get_item(position)
        if isinstance(track, Track):
            self.get_root().play_request(track.play, start_with=track.index)

    @Gtk.Template.Callback()
    def on_play_clicked(self, _button):
        self.get_root().play_request(self.item.play)

    @Gtk.Template.Callback()
    def on_shuffle_clicked(self, _button):
        self.get_root().play_request(self.item.play, shuffle=True)
