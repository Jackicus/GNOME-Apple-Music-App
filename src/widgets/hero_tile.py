"""AppleMusicHeroTile: a shelf's large card, the cover over a band in the artwork's colour."""

from gi.repository import Graphene, Gtk

from . import artwork
from .cover import Cover  # noqa: F401  registers $AppleMusicCover for the template


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/hero_tile.ui')
class HeroTile(Gtk.Box):
    """A 260 px cover over a two-line caption, the title and the subtitle, on a band of the
    Item's art_color (Apple's colour for the artwork's background) in white or black text,
    whichever reads better on it; without one, on the card colour. Apple's "Top Picks for You"
    cards, for the first shelf of Home and Radio's first stations.

    bind(item) and unbind() are called by a list factory, as for AppleMusicTile. The cover is
    an AppleMusicCover, which decodes the 640 px art while it is mapped (the 320 px thumbnail is
    shown meanwhile when it is decoded already). The band is drawn here rather than by CSS,
    which cannot take a colour per item; the grid tiles do not pay for this Python snapshot.
    """

    __gtype_name__ = 'AppleMusicHeroTile'

    cover = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()

    _colour = None

    def bind(self, item):
        self.cover.set_paths(item.art, item.thumb)  # 260 px at a scale of 2 wants the 640 px art
        self.title_label.set_text(item.title)
        self.subtitle_label.set_text(item.subtitle)
        self._set_colour(artwork.art_colour(item.art_color))

    def unbind(self):
        self.cover.set_paths()

    def _set_colour(self, colour):
        if colour is None and self._colour is None:
            return
        if colour is not None and self._colour is not None and colour.equal(self._colour):
            return
        self._colour = colour
        dark = colour is not None and artwork.is_dark(colour)
        # The text colour for the band: white on a dark one, black on a light one.
        if dark:
            self.add_css_class('dark-art')
        else:
            self.remove_css_class('dark-art')
        if colour is not None and not dark:
            self.add_css_class('light-art')
        else:
            self.remove_css_class('light-art')
        self.queue_draw()

    def do_snapshot(self, snapshot):
        # Under the children, over the CSS background, inside the rounded clip of `overflow`.
        if self._colour is not None:
            bounds = Graphene.Rect().init(0, 0, self.get_width(), self.get_height())
            snapshot.append_color(self._colour, bounds)
        Gtk.Box.do_snapshot(self, snapshot)
