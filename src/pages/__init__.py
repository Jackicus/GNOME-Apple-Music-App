# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The content pane's root pages, one per sidebar destination (PAGES).

create(destination, library) builds a destination's page. The window builds each page on its
first visit and keeps the fixed destinations' for good; the sidebar's playlists and folders get
theirs from playlist() and folder() (which a folder's tiles use too), and the window keeps only
the most recently shown few of those (window.ROOT_LIMIT), building one again when it is shown
again.

Each page's module is imported when its first page is built, not here: the app starts with one
page, and importing every page module (their templates and widgets) cost about 20 ms of startup.

Pages reach the application through app(), and the window through get_root() and its seams
(open_item, open_shelf, open_songs, play_request, add_toast, item_actions).

A library page's empty state (Home's, Radio's, the grids', Songs', Favourite Songs') offers
the way out while the account is signed out: SignInOffer puts Sign In… on its status page.
"""

from gettext import gettext as _

from gi.repository import Gio, GLib, GObject, Gtk

from ..library import ROOT_FOLDER
from ..sidebar import FOLDER_ICON, PLAYLIST_ICON
from ..widgets.util import MappedHandlers, connect_weak


def app():
    """The application (Gio.Application.get_default()): its engine, settings, spawn() and
    report(). How every page reaches it; None only in a test without one."""
    return Gio.Application.get_default()


def signed_out():
    """Whether the account is signed out (the signed-in setting, by the app's account_key());
    False in the demo, which has no account to sign in to, and without an application."""
    application = app()
    return (application is not None and not application.demo
            and not application.settings.get_boolean(application.account_key('signed-in')))


class SignInOffer:
    """What a library page's empty state, an Adw.StatusPage, offers while the account is
    signed out: a Sign In… button (app.sign-in; the ellipsis, as the sign-in's window
    follows) and, in place of the page's own description, what signing in does. Signed in,
    the page's own words show and there is no button: a first sync filling the library is
    the page's loading state, and the sync banner says so. In the demo, which has no account,
    and in a widget test without an application, the page's words show as they are.

        self._sign_in = SignInOffer(self.empty_page)   # once the page's description is set
        self._sign_in.set_description(text)            # a page whose description changes

    The signed-in setting is followed only while the status page is mapped (its stack shows
    it), weakly, and showing it again catches up. The page holds this object, which holds the
    status page and the button, its children: a dropped page takes them with it.
    """

    def __init__(self, status_page):
        self._status_page = status_page
        self._description = status_page.get_description()  # the page's own words
        self._button = Gtk.Button(label=_('Sign In…'), action_name='app.sign-in',
                                  halign=Gtk.Align.CENTER, visible=False,
                                  css_classes=['pill', 'suggested-action'])
        status_page.set_child(self._button)
        self._handlers = MappedHandlers(status_page)
        application = app()
        if application is not None and not application.demo:
            self._handlers.add(application.settings,
                               'changed::' + application.account_key('signed-in'), self._update)
        connect_weak(status_page, 'map', self._update)
        self._update()

    @property
    def button(self):
        """The Sign In… button (visible while signed out)."""
        return self._button

    def set_description(self, description):
        """The page's own description, shown once signed in."""
        self._description = description
        self._update()

    def _update(self, *_args):
        offered = signed_out()
        self._button.set_visible(offered)
        self._status_page.set_description(
            _('Sign in to Apple Music to see your library') if offered else self._description)


def estimate_width(default_width, maximized, monitor_width, sidebar):
    """The content pane's width before the window has laid it out (the page restored at
    startup): the window's default width, or the largest monitor's while it is maximized or
    fullscreen (the default width stays the unmaximized one), less the sidebar's width while
    it shows (0 when collapsed)."""
    width = monitor_width if maximized and monitor_width > 0 else default_width
    return max(0, width - sidebar)


def show_notes(parent, title, text):
    """An album's notes or an artist's biography in full, in a dialog over `parent` (the page
    shows three lines and More)."""
    from gi.repository import Adw, Gtk

    label = Gtk.Label(label=text, wrap=True, xalign=0, selectable=True, margin_start=18,
                      margin_end=18, margin_top=6, margin_bottom=18, valign=Gtk.Align.START)
    toolbar = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=label,
                                                         hscrollbar_policy=Gtk.PolicyType.NEVER,
                                                         propagate_natural_height=True))
    toolbar.add_top_bar(Adw.HeaderBar())
    dialog = Adw.Dialog(title=title, child=toolbar, content_width=480, content_height=420)
    dialog.present(parent)
    return dialog


def mark_bound(page):
    """Note a page's first tile or row being bound, for the startup timing
    (Application.mark: '<tag>-bound', and '<tag>-painted' at the end of that frame), when
    the page is a root page (the window tags those with their key)."""
    tag = page.get_tag()
    mark = getattr(app(), 'mark', None)
    if tag and mark is not None:
        mark(f'{tag}-bound', painted=f'{tag}-painted')


def _search(destination, library):
    from .search import SearchPage

    return SearchPage(library, destination.title, icon_name=destination.icon_name)


def _home(destination, library):
    from .home import HomePage

    return HomePage(library, destination.title, icon_name=destination.icon_name)


def _new(destination, library):
    from . import new

    return new.create(destination)


def _made_for_you(destination, library):
    from . import made_for_you

    return made_for_you.create(destination)


def _radio(destination, library):
    from .radio import RadioPage

    return RadioPage(library, destination.title, icon_name=destination.icon_name)


def _albums(destination, library):
    from .grid import GridPage

    return GridPage(library, destination.title, lambda: library.albums,
                    sorts=('title', 'artist', 'year'), icon_name=destination.icon_name,
                    empty_title=_('No Albums'),
                    empty_description=_('Albums in your library appear here'))


def _artists(destination, library):
    from .artists import ArtistsPage

    return ArtistsPage(library, destination.title, icon_name=destination.icon_name)


def _recently_added(destination, library):
    # Apple's order, newest first: not sorted here.
    from .grid import GridPage

    def model():
        shelf = library.shelf('recently-added')
        return shelf.items if shelf else None

    return GridPage(library, destination.title, model, icon_name=destination.icon_name,
                    empty_title=_('Nothing Recently Added'),
                    empty_description=_('Music you add to your library appears here'))


def _songs(destination, library):
    from .songs import SongsPage

    return SongsPage(library, destination.title, icon_name=destination.icon_name)


def _all_playlists(destination, library):
    # The top level of the playlist folders: Favourite Songs, the folders, then the playlists in
    # none, each by title, as the sidebar lists them (library.PlaylistTree).
    from .grid import GridPage

    page = GridPage(library, destination.title, lambda: library.folder_items(ROOT_FOLDER),
                    icon_name=destination.icon_name,
                    empty_title=_('No Playlists'),
                    empty_description=_('Playlists in your library appear here'))
    add_folder_buttons(page, library, ROOT_FOLDER)
    return page


def _favourite_songs(destination, library):
    # The flagged playlist's page, as a root page that follows the library.
    from .detail import DetailPage

    return DetailPage(library, find=library.favourite_songs, root=True, title=destination.title,
                      icon_name=destination.icon_name,
                      empty_title=_('No Favourite Songs'),
                      empty_description=_('Songs you mark as favourites appear here'))


def _music_videos(destination, library):
    from .grid import GridPage

    return GridPage(library, destination.title, lambda: library.videos,
                    sorts=('title', 'artist', 'year'), icon_name=destination.icon_name,
                    empty_title=_('No Music Videos'),
                    empty_description=_('Music videos in your library appear here'))


def playlist(library, playlist_id, title):
    """A sidebar playlist's root page: its detail page, following the library by the id (a
    load after sign-out makes new Items; a reload keeps them, and the page follows the Item's
    signals), with an empty state once the playlist is gone."""
    from .detail import DetailPage

    return DetailPage(library, find=lambda: library.by_id('playlist', playlist_id), root=True,
                      title=title, icon_name=PLAYLIST_ICON,
                      empty_title=_('Playlist Not Found'),
                      empty_description=_('This playlist is no longer in your library'))


def folder(library, folder_id, title, root=True):
    """A folder's page: its folders and playlists as tiles, in the sidebar's order, following the
    library by the folder's id, with a missing state once the folder is gone. A sidebar
    folder's is a root page (the window retitles it); a folder opened from a tile is pushed
    (root=False) over the page it was in, and follows its folder Item's title (a reload keeps
    the Item)."""
    from .grid import GridPage

    page = GridPage(library, title, lambda: library.folder_items(folder_id), root=root,
                    icon_name=FOLDER_ICON, empty_title=_('Empty Folder'),
                    empty_description=_('Playlists you put in this folder appear here'),
                    missing_title=_('Folder Not Found'),
                    missing_description=_('This folder is no longer in your library'))
    item = library.by_id('folder', folder_id) if not root else None
    if item is not None:
        item.bind_property('title', page, 'title', GObject.BindingFlags.SYNC_CREATE)
    add_folder_buttons(page, library, folder_id)
    return page


def add_folder_buttons(page, library, folder_id):
    """The header buttons of All Playlists (`folder_id` ROOT_FOLDER) and of a folder's page
    (a GridPage): New Playlist, in that folder, and a folder's More Options menu (Rename…,
    Delete Folder…: the item actions' menu for the folder Item, looked up by its id as it
    opens, so it follows a reload)."""
    new = Gtk.Button(icon_name='list-add-symbolic', tooltip_text=_('New Playlist'),
                     action_name='win.item-new-playlist')
    new.set_action_target_value(GLib.Variant('(ss)', ('folder', folder_id)))
    page.header_bar.pack_start(new)
    if folder_id == ROOT_FOLDER:
        return
    more = Gtk.MenuButton(icon_name='view-more-symbolic', tooltip_text=_('More Options'))
    # Holds the library and the id, never the page: the page frees as it should.
    more.set_create_popup_func(lambda button: _folder_menu(button, library, folder_id))
    page.header_bar.pack_end(more)


def _folder_menu(button, library, folder_id):
    actions = getattr(button.get_root(), 'item_actions', None)
    folder = library.by_id('folder', folder_id)
    button.set_menu_model(actions.menu_for(folder) if actions and folder else None)


PAGES = {
    'search': _search,
    'home': _home,
    'new': _new,
    'radio': _radio,
    'made-for-you': _made_for_you,
    'albums': _albums,
    'artists': _artists,
    'recently-added': _recently_added,
    'songs': _songs,
    'all-playlists': _all_playlists,
    'favourite-songs': _favourite_songs,
    'music-videos': _music_videos,
}


def create(destination, library):
    """A new root page for the destination (KeyError for a key PAGES does not know)."""
    return PAGES[destination.key](destination, library)
