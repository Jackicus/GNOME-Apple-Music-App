"""AppleMusicHomePage: the library's shelves, one under another, as on music.apple.com's Home."""

from gi.repository import Adw, Gtk

from ..widgets.shelf import Shelf


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/home.ui')
class HomePage(Adw.NavigationPage):
    """Every shelf of the library with items in it, in library.json's order (Apple's
    recommendations, then Heavy Rotation, Recently Added…), the first as large cards, each
    offering See All.

    The shelves are AppleMusicShelf widgets in a box, kept and rebound in place when a load
    brings new ones, so a reload rebinds their tiles rather than rebuilding them. The loading
    and empty states follow the library's state, as the grid pages' do.
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
        for position, shelf in enumerate(shelves):
            if position < len(self._widgets):
                widget = self._widgets[position]
            else:
                widget = Shelf(hero=position == 0, see_all=True)
                self.shelves_box.append(widget)
                self._widgets.append(widget)
            widget.bind_shelf(shelf)
        for widget in self._widgets[len(shelves):]:
            self.shelves_box.remove(widget)
        del self._widgets[len(shelves):]
        self._shelves = shelves
