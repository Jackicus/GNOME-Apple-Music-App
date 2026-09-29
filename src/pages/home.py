"""AppleMusicHomePage: the library's shelves, one under another, as on music.apple.com's Home."""

from gi.repository import Adw, Gtk

from ..library import yield_to_frames
from ..widgets.shelf import Shelf
from . import app

# The shelves bound before the page is drawn: the hero cards and what fits under them. The
# rest are bound one a frame after that, below the fold: a library with 21 shelves made
# 317 tiles at once, and the page took 210 ms to open.
FIRST_SHELVES = 3


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/home.ui')
class HomePage(Adw.NavigationPage):
    """Every shelf of the library with items in it, in library.json's order (Apple's
    recommendations, then Heavy Rotation, Recently Added…), the first as large cards, each
    offering See All.

    The shelves are AppleMusicShelf widgets in a box, kept and rebound in place when a load
    brings new ones, so a reload rebinds their tiles rather than rebuilding them; those past
    FIRST_SHELVES are bound a frame apart. The loading and empty states follow the library's
    state, as the grid pages' do.
    """

    __gtype_name__ = 'AppleMusicHomePage'

    stack = Gtk.Template.Child()
    empty_page = Gtk.Template.Child()
    shelves_box = Gtk.Template.Child()
    title_label = Gtk.Template.Child()

    def __init__(self, library, title, icon_name=None):
        super().__init__(title=title)
        self._library = library
        self._library_handlers = []
        self._shelves = []  # the library.Shelf objects shown
        self._widgets = []  # their AppleMusicShelf widgets, in order
        self._generation = 0  # counts _show()s: an older one's remaining shelves are dropped
        self.title_label.set_label(title)
        self.empty_page.set_icon_name(icon_name)
        self._update()

    # The library outlives the window, so the page listens to it only while it is shown.

    def do_map(self):
        Adw.NavigationPage.do_map(self)
        self._library_handlers = [
            self._library.connect('notify::state', self._update),
            self._library.connect('changed', self._update),
        ]
        self._update()

    def do_unmap(self):
        for handler in self._library_handlers:
            self._library.disconnect(handler)
        self._library_handlers = []
        Adw.NavigationPage.do_unmap(self)

    def _update(self, *_args):
        shelves = [shelf for shelf in self._library.shelves if shelf.items.get_n_items()]
        if shelves != self._shelves:
            self._show(shelves)
        if shelves:
            name = 'items'
        elif self._library.state == 'loading':
            name = 'loading'
        else:
            name = 'empty'
        self.stack.set_visible_child_name(name)

    def _show(self, shelves):
        """Bind the shelves' widgets to shelves, the first FIRST_SHELVES now and the rest a
        frame apart (a newer _show() stops the rest)."""
        self._shelves = shelves
        self._generation += 1
        now = shelves if app() is None else shelves[:FIRST_SHELVES]
        for position, shelf in enumerate(now):
            self._bind(position, shelf)
        if len(now) < len(shelves):
            app().spawn(self._show_rest(shelves, len(now), self._generation))
        else:
            self._trim(len(shelves))

    async def _show_rest(self, shelves, start, generation):
        for position in range(start, len(shelves)):
            await yield_to_frames()
            if generation != self._generation:
                return
            self._bind(position, shelves[position])
        self._trim(len(shelves))

    def _bind(self, position, shelf):
        if position < len(self._widgets):
            widget = self._widgets[position]
        else:
            widget = Shelf(hero=position == 0, see_all=True)
            self.shelves_box.append(widget)
            self._widgets.append(widget)
        widget.bind_shelf(shelf)

    def _trim(self, count):
        """Remove the widgets past the first count."""
        for widget in self._widgets[count:]:
            self.shelves_box.remove(widget)
        del self._widgets[count:]
