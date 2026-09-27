from gi.repository import Adw, Gtk


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/player_bar.ui')
class PlayerBar(Adw.Bin):
    """The transport bar under the content. A stub until playback is wired up."""

    __gtype_name__ = 'AppleMusicPlayerBar'

    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()
