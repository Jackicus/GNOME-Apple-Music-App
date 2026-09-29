"""AppleMusicWindow: the main window, holding the sidebar, the content's navigation view, the
player bar and the Now Playing sheet, the account button and the banners.

The pages reach the window through get_root() and these seams, not its internals:

    open_item(item)                   show an album, artist, playlist, folder or category;
                                      play a station, song or video
    open_shelf(shelf)                 a shelf's items as a grid (See All)
    open_songs(text)                  the Songs page, filtered
    play_request(play, start_with=None, shuffle=None)
                                      every "play this": the Player, the sign-in, the toasts
    add_toast(toast)                  a toast over the content, or in the open sheet
    announce(text, priority)          Gtk.Accessible's, for assistive technology
    item_actions                      the win.item-* actions and their menus (actions.py)
    content_width()                   the content pane's width, before it is laid out too

The sidebar itself is sidebar_view.SidebarController's, which shows pages through
show_root(key, pop=False), reads `shown` (the key whose root page is at the bottom of the
navigation stack) and `split_view`, and names the account's settings by account_key(). The
window keeps the root pages (one per key, built on the first visit), the window's actions and
their keys (keyboard.py), the account button and the banners, and its own state.
"""

import logging
from collections import OrderedDict
from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import keyboard, pages
from .actions import ItemActions
from .backend.errors import EngineError
from .player import playback_error_text
from .player_bar import PlayerBar  # noqa: F401  registers $AppleMusicPlayerBar for the template
from .widgets.now_playing import NowPlayingSheet  # noqa: F401  registers the sheet's type
from .sections import HOME
from .sidebar import parse_key, restore_target, stale_roots
from .sidebar_view import SidebarController
from .sync import progress_text
from .widgets.util import first_descendant, has_ancestor

log = logging.getLogger(__name__)

# The sidebar playlists' and folders' root pages kept once shown: past this many, the least
# recently shown is dropped (and freed), and built again if it is shown again. The fixed
# destinations' pages are kept for good.
ROOT_LIMIT = 8
# The fixed destinations whose pages show the engine's answers for the account signed in,
# forgotten at sign-out (forget_account_pages).
ACCOUNT_PAGES = ('new', 'made-for-you', 'search')


def sign_in_title(expired):
    """The sign-in banner's title: signed out, or signed in but the session expired."""
    if expired:
        return _('Your Apple Music sign-in has expired')
    return _('Sign in to see your library')


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/window.ui')
class Window(Adw.ApplicationWindow):
    __gtype_name__ = 'AppleMusicWindow'

    toast_overlay = Gtk.Template.Child()
    primary_menu_button = Gtk.Template.Child()
    bottom_sheet = Gtk.Template.Child()
    player_bar = Gtk.Template.Child()
    now_playing = Gtk.Template.Child()
    split_view = Gtk.Template.Child()
    sidebar = Gtk.Template.Child()
    content_page = Gtk.Template.Child()
    navigation_view = Gtk.Template.Child()
    account_stack = Gtk.Template.Child()
    account_button = Gtk.Template.Child()
    account_avatar = Gtk.Template.Child()
    account_label = Gtk.Template.Child()
    sign_in_banner = Gtk.Template.Child()
    sync_banner = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        app = self.get_application()
        self._settings = app.settings
        # The build's key for a setting of the account's (the .Devel build has its own).
        self.account_key = app.account_key
        self._library = app.library
        self._roots = {}  # sidebar key -> its root Adw.NavigationPage, once visited
        self._recent_roots = OrderedDict()  # playlist/folder keys in _roots, least recent first
        self.shown = None  # the key whose root page is at the bottom of the navigation stack
        self._quitting = False

        # The win.item-* actions the context menus run (actions.py), before the sidebar,
        # whose playlists' menu is theirs.
        self.item_actions = ItemActions(self, app)
        self._sidebar = SidebarController(self, self.sidebar, self._library, self._settings,
                                          self.item_actions)
        self.player_bar.set_player(app.player, app)
        self.now_playing.set_player(app.player, app, self.bottom_sheet)
        self._library_handler = self._library.connect('changed', self._on_library_changed)
        # The banners live under the visible page's header bar, whichever page that is.
        self._banner_host = None
        self.navigation_view.connect('notify::visible-page', self._dock_banners)
        self._account_menu = Gio.Menu()
        self.account_button.set_menu_model(self._account_menu)
        self._restore_window_state()
        self._restore_page(self._settings.get_string(self.account_key('last-page')))

        # The account button and the banner follow the signed-in and account-name keys and
        # whether the engine, up, finds the sign-in still good; the button is off while
        # signing out; the sync banner follows the app's sync (sync.LibrarySync).
        self._settings_handlers = [
            self._settings.connect('changed::' + self.account_key('signed-in'),
                                   self._update_account),
            self._settings.connect('changed::' + self.account_key('account-name'),
                                   self._update_account),
        ]
        self._app_handlers = [
            (app, app.connect('notify::signing-out', self._update_account)),
            (app.engine, app.engine.connect('notify::authorized', self._update_account)),
            (app.engine, app.engine.connect('notify::state', self._update_account)),
            (app.library_sync, app.library_sync.connect('progress', self._on_sync_progress)),
            (app.library_sync,
             app.library_sync.connect('notify::running', self._on_sync_running)),
        ]
        self._update_account()

        # The window's actions (their keys are shortcuts.ACCELS, set in main.py). Alt+Left:
        # the navigation views pop on their own only while the focus is in them; this goes
        # back from anywhere in the window. Ctrl+F: the Search page, with the cursor in its
        # entry. Ctrl+1, 2, 3: the focus into the sidebar, the page, the player bar. Each is
        # off while a dialog is open over the window (its keys are the dialog's then), and
        # back is off when there is nowhere to go or the Now Playing sheet is open, so the
        # keys reach the focus.
        self._actions = {}
        for name, callback in (('back', self._on_back), ('search', self._on_search),
                               ('focus-sidebar', self._on_focus_sidebar),
                               ('focus-content', self._on_focus_content),
                               ('focus-player', self._on_focus_player)):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            self.add_action(action)
            self._actions[name] = action
        self.navigation_view.connect('notify::visible-page', self._update_actions)
        self.split_view.connect('notify::collapsed', self._update_actions)
        self.split_view.connect('notify::show-content', self._update_actions)
        self.bottom_sheet.connect('notify::open', self._update_actions)
        self.connect('notify::visible-dialog', self._update_actions)
        self._update_actions()

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.on_key_pressed)
        self.add_controller(keys)

    # -- keys ------------------------------------------------------------------------------

    def on_key_pressed(self, _controller, keyval, _keycode, state):
        """The playback keys (keyboard.PLAYBACK_KEYS) run the playback actions while
        something plays, unless the keys belong to the focus (keyboard.playback_action). A
        key nothing handles goes on to the focus widget. F10 opens the primary menu when
        the sidebar holding it is hidden (GTK's own F10 needs it shown)."""
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if keyboard.is_main_menu(keyval, mods):
            return self._show_primary_menu()
        app = self.get_application()
        focus = self.get_focus()

        def enabled(name):
            action = app.lookup_action(name)
            return action is not None and action.get_enabled()

        name = keyboard.playback_action(keyval, mods, type(focus),
                                        self.get_visible_dialog() is not None,
                                        has_ancestor(focus, Gtk.Popover), enabled)
        if name is None:
            return False
        app.activate_action(name)
        return True

    def _show_primary_menu(self):
        """F10 while the primary menu's button is not shown (the collapsed layout showing a
        page): show the sidebar, then open the menu there. True when it did."""
        button = self.primary_menu_button
        if button.get_mapped() or self.get_visible_dialog() is not None:
            return False  # GTK's own F10 opens it
        if not self.split_view.get_collapsed():
            return False
        self._close_sheet()
        self.split_view.set_show_content(False)
        GLib.idle_add(self._popup_primary_menu)
        return True

    def _popup_primary_menu(self):
        self.primary_menu_button.popup()
        return GLib.SOURCE_REMOVE

    # -- toasts and the sheet --------------------------------------------------------------

    def toast(self, title):
        """A plain toast (never markup): Application.toast() is the app's way to make one."""
        self.add_toast(Adw.Toast(title=title, use_markup=False))

    def add_toast(self, toast):
        """Show a toast over the content, or inside the Now Playing sheet while that is open
        (the sheet is modal: the window's overlay is under it)."""
        if self.bottom_sheet.get_open():
            self.now_playing.add_toast(toast)
        else:
            self.toast_overlay.add_toast(toast)

    def toggle_now_playing(self):
        """Open the Now Playing sheet, or close it (app.now-playing); not while a dialog is
        open over the window."""
        if self.get_visible_dialog() is None:
            self.bottom_sheet.set_open(not self.bottom_sheet.get_open())

    def _close_sheet(self):
        if self.bottom_sheet.get_open():
            self.bottom_sheet.set_open(False)

    # -- the seams the pages use -----------------------------------------------------------

    def content_width(self):
        """The content pane's width in px, for a page sizing itself before it is laid out
        (the grids' columns): its allocation once it has one, else an estimate
        (pages.estimate_width: the default width, or the largest monitor's while maximized or
        fullscreen, less the sidebar's while it shows)."""
        width = self.navigation_view.get_width()
        if width > 0:
            return width
        maximized = (self.is_maximized() or self.is_fullscreen()
                     or self._settings.get_boolean('window-maximized'))
        monitors = Gdk.Display.get_default().get_monitors()
        monitor = max((monitors.get_item(n).get_geometry().width
                       for n in range(monitors.get_n_items())), default=0)
        sidebar = 0 if self.split_view.get_collapsed() else self.split_view.get_max_sidebar_width()
        return pages.estimate_width(self.get_default_size()[0], maximized, monitor, sidebar)

    def open_songs(self, search=''):
        """Show the Songs page filtered by `search` (Your Library results' See All)."""
        self.select_page('songs')
        self.split_view.set_show_content(True)
        page = self._roots.get('songs')
        if page is not None and hasattr(page, 'set_filter'):
            page.set_filter(search)

    def open_item(self, item):
        """Show an album, artist, playlist, folder, category, station, song or video: what
        activating a tile does.

        Albums and playlists push a DetailPage, artists an ArtistPage, playlist folders their
        grid of folders and playlists (the page follows the folder Item: a rename, a
        deletion), search categories their page of shelves, over the page shown. A station,
        a song (a search hit, a Best New Songs tile) or a music video has no page: it plays,
        as on music.apple.com (a video as its audio, in the headless engine, as the context
        menu's Play does). Anything else is named in a toast.
        """
        visible = self.navigation_view.get_visible_page()
        if getattr(visible, 'item', None) is item:
            return  # a double activation
        if item.kind in ('album', 'playlist'):
            from .pages.detail import DetailPage

            page = DetailPage(self._library, item)
        elif item.kind == 'artist':
            from .pages.artist import ArtistPage

            page = ArtistPage(self._library, item)
        elif item.kind == 'folder':  # a folder's tile in a folder's page
            page = pages.folder(self._library, item.id, item.title, root=False)
            page.item = item
        elif item.kind == 'category':
            from .pages.shelves import category_page

            page = category_page(item)
        elif item.kind in ('station', 'song', 'video'):
            self.play_request(item.play)
            return
        else:
            self.get_application().toast(item.title)
            return
        self.navigation_view.push(page)

    def open_shelf(self, shelf):
        """Show a shelf's items as a grid, pushed over the page shown: a shelf's See All.

        A shelf of the library's is followed by its key, so the page shows what a later load
        puts on it; any other (a search's results, a category's) is shown as it is.
        """
        visible = self.navigation_view.get_visible_page()
        if getattr(visible, 'shelf', None) is shelf:
            return  # a double activation
        if shelf in self._library.shelves:
            key = shelf.key

            def model():
                found = self._library.shelf(key)
                return found.items if found else None
        else:
            model = shelf.items
        from .pages.grid import GridPage

        # A shelf of artists (a search's, a category's) gets the round portraits.
        artist = (shelf.items.get_n_items() > 0
                  and all(item.kind == 'artist' for item in shelf.items))
        page = GridPage(self._library, shelf.title, model, root=False, artist=artist,
                        icon_name='view-grid-symbolic', empty_title=_('Nothing Here'),
                        empty_description=_('This shelf is empty now'))
        page.shelf = shelf
        self.navigation_view.push(page)

    def play_request(self, play, start_with=None, shuffle=None):
        """Play what play names ({kind, id}: an Item's or a Group's play target), from its entry at
        queue position start_with (a track row: track.play, track.index); `shuffle` True
        shuffled (a Shuffle button), False in order (a Play button), None as the mode is (a
        track row, a tile).

        The one way into playback from the pages (a Play button, a track row, a station's
        tile): the Player plays it through the engine, starting that first if need be;
        signed out, the sign-in flow opens instead; a failure is toasted, a play MusicKit
        refused as what its code means for the user (player.playback_error_text). The bar
        follows the engine's events.
        """
        app = self.get_application()
        if app.refuse_in_demo():
            return
        if not play or not play.get('kind') or not play.get('id'):
            app.toast(_('This cannot be played'))
            return
        app.spawn(self._play(play, start_with, shuffle))

    async def _play(self, play, start_with, shuffle):
        app = self.get_application()
        try:
            await app.player.play(play, start_with=start_with, shuffle=shuffle)
        except EngineError as error:
            if error.code == 'api' and error.musickit_code:
                log.warning('play refused: %s', error)
                app.toast(playback_error_text(error.musickit_code))
            else:
                app.report(error)  # signed out, that opens the sign-in

    # -- the account and the banners -------------------------------------------------------

    def _update_account(self, *_args):
        """The account button and the sign-in banner: Sign In while signed out; the name
        over a menu with Sign Out once signed in; and, when the engine is up but Apple no
        longer takes the sign-in (the session expired: signed in here, not authorized
        there), the banner says so with Sign In and the menu offers Sign In Again, until
        the account is signed in again or out."""
        app = self.get_application()
        signed_in = self._settings.get_boolean(self.account_key('signed-in'))
        name = self._settings.get_string(self.account_key('account-name'))
        expired = (signed_in and not app.demo and app.engine.state == 'up'
                   and not app.engine.authorized)
        self.account_stack.set_visible_child_name('account' if signed_in else 'sign-in')
        self.account_label.set_label(name or _('Signed In'))
        self.account_avatar.set_text(name)
        self.account_avatar.set_show_initials(bool(name))
        self.account_button.set_sensitive(not app.signing_out)
        self._account_menu.remove_all()
        if expired:
            section = Gio.Menu()
            section.append(_('Sign _In Again'), 'app.sign-in')
            self._account_menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_('Sign _Out'), 'app.sign-out')
        self._account_menu.append_section(None, section)
        self.sign_in_banner.set_title(sign_in_title(expired))
        self.sign_in_banner.set_revealed((not signed_in or expired) and not app.demo)

    def _dock_banners(self, *_args):
        """The banners under the visible page's header bar: every page has its own, in its
        ToolbarView, and a banner above it would push the window controls and the back
        button down. Moved from page to page as the visible page changes (a page dropped
        meanwhile has let go of them already)."""
        page = self.navigation_view.get_visible_page()
        toolbar = first_descendant(page, Adw.ToolbarView) if page is not None else None
        if toolbar is self._banner_host:
            return
        for banner in (self.sign_in_banner, self.sync_banner):
            host = banner.get_ancestor(Adw.ToolbarView)
            if host is not None:
                host.remove(banner)
            if toolbar is not None:
                toolbar.add_top_bar(banner)  # after the header bar, its first top bar
        self._banner_host = toolbar

    # The sync's progress, on a banner over the content, while the app's sync runs.

    def _on_sync_progress(self, _sync, section, done, total):
        self.sync_banner.set_title(progress_text(section, done, total))
        self.sync_banner.set_revealed(True)

    def _on_sync_running(self, library_sync, _pspec):
        if not library_sync.props.running:
            self.sync_banner.set_revealed(False)

    # -- the root pages, one per sidebar key -----------------------------------------------

    def select_page(self, key):
        """Select key's sidebar item (Home's when there is none) and show its page."""
        self._sidebar.select(key)

    def _on_library_changed(self, _library):
        self._sidebar.update_playlists()
        # The page shown keeps showing, its item selected again (or for the first time, when
        # it was restored before the library had loaded), unless it is gone: then Home.
        if self._sidebar.selected_key() != self.shown:
            self._sidebar.select(self.shown)
            self._sidebar.reveal_selected()
        entries = self._sidebar.entries_by_key()
        for key in stale_roots(self._roots, entries, self.shown):
            self._drop_root(key)
        for key, page in self._roots.items():
            entry = entries.get(key) if parse_key(key) is not None else None
            if entry is not None:
                page.set_title(entry.title)  # a renamed playlist or folder
        self.content_page.set_title(self._roots[self.shown].get_title())

    def _root(self, key):
        """The root page for a sidebar key, built on its first visit and kept in the view: a
        fixed destination's for good, a playlist's or a folder's while it is among the
        ROOT_LIMIT most recently shown (_trim_roots)."""
        page = self._roots.get(key)
        if page is None:
            destination = self._sidebar.destination(key)
            if destination is not None:
                page = pages.create(destination, self._library)
            else:
                kind, item_id = parse_key(key)
                entry = self._sidebar.entry(key)
                if kind == 'playlist':
                    # Restored before the library has loaded: titled once it has.
                    title = entry.title if entry is not None else _('Playlist')
                    page = pages.playlist(self._library, item_id, title)
                else:
                    title = entry.title if entry is not None else _('Folder')
                    page = pages.folder(self._library, item_id, title)
            page.set_tag(key)
            self.navigation_view.add(page)
            self._roots[key] = page
        if parse_key(key) is not None:
            self._recent_roots[key] = None
            self._recent_roots.move_to_end(key)
            self._trim_roots(key)
        return page

    def _trim_roots(self, keep):
        """Drop the least recently shown playlist and folder root pages past ROOT_LIMIT, but
        never keep's, the one shown or one in the navigation stack."""
        surplus = len(self._recent_roots) - ROOT_LIMIT
        if surplus <= 0:
            return
        stack = self.navigation_view.get_navigation_stack()
        in_stack = [stack.get_item(position) for position in range(stack.get_n_items())]
        for key in list(self._recent_roots):
            if surplus <= 0:
                break
            if key in (keep, self.shown):
                continue
            page = self._roots[key]
            if any(page is shown for shown in in_stack):
                continue
            self._drop_root(key)
            surplus -= 1

    def _drop_root(self, key):
        """Remove a root page from the view and forget it: nothing else holds it, so it is
        freed (widgets/util.py)."""
        page = self._roots.pop(key)
        self._recent_roots.pop(key, None)
        self.navigation_view.remove(page)

    def forget_account_pages(self):
        """After a sign-out: forget the pages that show what the account had, to be built
        again on the next visit (from the empty library, or the next account's): the pages
        pushed over the one shown, the pages of the engine's answers (ACCOUNT_PAGES) and
        every playlist's and folder's root page. Home is shown when the page shown was one
        of them. The folders shown open are forgotten too."""
        stack = self.navigation_view.get_navigation_stack()
        if stack.get_n_items() > 1:
            self.navigation_view.pop_to_page(stack.get_item(0))
        forget = [key for key in self._roots
                  if key in ACCOUNT_PAGES or parse_key(key) is not None]
        if self.shown in forget:
            self._sidebar.select(HOME)
        for key in forget:
            self._drop_root(key)
        self._sidebar.forget_expanded()

    def _restore_page(self, key):
        """Show the page last-page names (sidebar.restore_target).

        A playlist's or folder's item exists only once the library has loaded: until then its
        page shows (loading) with All Playlists selected, and _on_library_changed selects its
        item, or goes Home when it is gone. Not with nothing selected: the sidebar's list
        selects the row with the focus when nothing is, which is its first (Search) as the
        window is shown, or when it becomes active.
        """
        select, show = restore_target(key, self._sidebar.entries_by_key(),
                                      self._library.state == 'ready')
        if select == show:
            self._sidebar.select(select)
        else:
            self._sidebar.select(select, show=False)
            self.show_root(show)
        self._sidebar.reveal_selected()

    def show_root(self, key, pop=False):
        """Make key's root page the navigation view's root. Pages pushed over it stay when it is
        the root already (a reload selecting the page shown again must not pop them), unless
        `pop`: the user asked for that page, so they are popped."""
        root = self._root(key)
        stack = self.navigation_view.get_navigation_stack()
        if not stack.get_n_items() or stack.get_item(0) is not root:
            self.navigation_view.replace([root])
        elif pop and stack.get_n_items() > 1:
            self.navigation_view.pop_to_page(root)
        self.content_page.set_title(root.get_title())
        self.shown = key  # written to last-page when the window closes (_save_window_state)

    # -- the window's actions --------------------------------------------------------------

    def _on_search(self, *_args):
        """win.search (Ctrl+F): the cursor in the filter of the page shown when it has one
        (the Songs page's, as GNOME's apps with an in-page filter do), else in the Search
        page's entry, that page shown over anything pushed on it (the Now Playing sheet
        closed first; the content shown first in the collapsed layout)."""
        self._close_sheet()
        page = self.navigation_view.get_visible_page()
        if page is not None and page is self._roots.get('songs') and page.focus_filter():
            self._show_content_then(page.focus_filter)  # again, once the content is mapped
            return
        self._sidebar.select('search', pop=True)
        page = self._roots.get('search')
        if page is not None and hasattr(page, 'focus_entry'):
            self._show_content_then(page.focus_entry)

    def _show_content_then(self, focus):
        """Show the content in the collapsed layout, then run focus (a page's grab), from an
        idle when the content had to be shown first (it is mapped by then)."""
        if self.split_view.get_collapsed() and not self.split_view.get_show_content():
            self.split_view.set_show_content(True)
            GLib.idle_add(lambda: focus() and False)
        else:
            focus()

    def _can_go_back(self):
        return (len(self.navigation_view.get_navigation_stack()) > 1
                or (self.split_view.get_collapsed() and self.split_view.get_show_content()))

    def _update_actions(self, *_args):
        """The window's actions are off while a dialog is open over it (their keys go to the
        dialog, not to the pages behind it); back also while the Now Playing sheet is open
        (Escape closes that) or there is nowhere to go back to."""
        free = self.get_visible_dialog() is None
        for action in self._actions.values():
            action.set_enabled(free)
        self._actions['back'].set_enabled(
            free and not self.bottom_sheet.get_open() and self._can_go_back())

    # Ctrl+1, 2, 3: the focus into the sidebar, the page, the player bar.

    def _on_focus_sidebar(self, *_args):
        """win.focus-sidebar: the focus on the selected sidebar row (the sidebar shown first
        in the collapsed layout, the sheet closed)."""
        self._close_sheet()
        if self.split_view.get_collapsed() and self.split_view.get_show_content():
            self.split_view.set_show_content(False)
            GLib.idle_add(self._sidebar.focus)  # once the sidebar's page is shown
        else:
            self._sidebar.focus()

    def _on_focus_content(self, *_args):
        """win.focus-content: the focus on the page shown, its content first (a grid, a
        list, an entry) rather than its header bar (the content shown first in the collapsed
        layout, the sheet closed). Nothing moves when the focus is in the page's content
        already; from its header bar (Back, Sort By, a filter) it moves into the content."""
        self._close_sheet()
        self._show_content_then(self._focus_content)

    def _focus_content(self):
        page = self.navigation_view.get_visible_page()
        if page is None:
            return GLib.SOURCE_REMOVE
        toolbar = first_descendant(page, Adw.ToolbarView)
        content = toolbar.get_content() if toolbar is not None else None
        focus = self.get_focus()
        if focus is not None and content is not None and (
                focus is content or focus.is_ancestor(content)):
            return GLib.SOURCE_REMOVE
        if content is None or not content.child_focus(Gtk.DirectionType.TAB_FORWARD):
            page.child_focus(Gtk.DirectionType.TAB_FORWARD)
        return GLib.SOURCE_REMOVE

    def _on_focus_player(self, *_args):
        """win.focus-player: the focus on the play button, the Now Playing sheet's while it
        is open, else the bar's; with nothing playing, on the bar itself (it opens the
        sheet)."""
        if self.bottom_sheet.get_open():
            self.now_playing.focus_controls()
        elif not self.player_bar.play_button.grab_focus():
            self.player_bar.grab_bar_focus()

    def _on_back(self, *_args):
        if len(self.navigation_view.get_navigation_stack()) > 1:
            self.navigation_view.pop()
        elif self.split_view.get_collapsed():
            self.split_view.set_show_content(False)

    # -- the window's state ----------------------------------------------------------------

    def _restore_window_state(self):
        self.set_default_size(
            self._settings.get_int('window-width'),
            self._settings.get_int('window-height'),
        )
        if self._settings.get_boolean('window-maximized'):
            self.maximize()

    def _save_window_state(self):
        """The settings the window keeps, written as it closes or hides: its size, the page
        shown (last-page) and the expanded folders. Written then rather than as they change
        (a click, a toggle): only the last matters, so this is fewer writes (a write is
        asynchronous; nothing waits on it)."""
        width, height = self.get_default_size()
        self._settings.set_int('window-width', width)
        self._settings.set_int('window-height', height)
        self._settings.set_boolean('window-maximized', self.is_maximized())
        if self.shown is not None:
            self._settings.set_string(self.account_key('last-page'), self.shown)
        self._sidebar.write_expanded()

    def prepare_quit(self):
        """The app is quitting (app.quit, or this window closing): remember the window's
        state, close its dialogs and hide it now, so nothing shows while the engine stops."""
        if self._quitting:
            return
        self._quitting = True
        self._close_dialogs()
        self._save_window_state()
        self._library.disconnect(self._library_handler)  # the library outlives the window
        for handler in self._settings_handlers:
            self._settings.disconnect(handler)
        self._settings_handlers = []
        for source, handler in self._app_handlers:
            source.disconnect(handler)
        self._app_handlers = []
        self.set_visible(False)

    def hide_for_background(self):
        """The window closes while the music plays on (background playback): the dialog on
        top of it and those shown as windows of their own are closed, its state remembered,
        and it hides, to be presented again as it was."""
        self._close_dialogs()
        self._save_window_state()
        self.set_visible(False)

    def _close_dialogs(self):
        """Close the dialog on top of the window (only the topmost: one under it stays in
        the hidden window) and every dialog shown as a window of its own."""
        dialog = self.get_visible_dialog()
        if dialog is not None:
            dialog.force_close()
        # A dialog shown as a window of its own (this one neither maximized nor tiled) is
        # not among this window's dialogs: its window is transient for this one.
        for toplevel in Gtk.Window.list_toplevels():
            if toplevel is not self and toplevel.get_transient_for() is self:
                toplevel.close()

    def do_close_request(self):
        # Closing the last window quits, and quitting stops the engine first: the window
        # stays (hidden) until the app has, so the close is declined here. With background
        # playback on and something playing, the app hides the window instead
        # (Application.close_window).
        self.get_application().close_window(self)
        return True
