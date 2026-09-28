"""AppleMusicNowPlayingSheet: the bottom sheet's Now Playing view, following the Player.

The window hands it the Player, the app and the Adw.BottomSheet it sits in (set_player)
once, after the template is built. The artwork, titles, transport, seek slider and toggles
follow the Player as the player bar's do (the pieces are shared: widgets/transport.py); the
Lyrics and Up Next tabs are a LyricsView and a QueueView (built here: GtkBuilder does not
run a Python widget's __init__) in a stack an Adw.ToggleGroup switches. The close button
sets the bottom sheet's `open` false; Escape and a swipe down are the bottom sheet's own.

Sizing: an AdwBottomSheet gives its sheet its natural height (clamped to the window less a
margin), so this widget asks for a tall one (SHEET_NATURAL_HEIGHT) and the tabs' lists take
what is left under the controls. It measures and allocates its child itself: an Adw.Bin's
layout manager answers gtk_widget_measure before the class's measure vfunc, so the bin's is
dropped (set_layout_manager(None)). `wide` (set by the window's 900sp breakpoint) lays the
item and the tabs side by side; `compact` (600sp) shrinks the artwork so a phone-sized window
keeps room for the tabs.
"""

from gettext import gettext as _

from gi.repository import Adw, GObject, Gtk

from .cover import Cover  # noqa: F401  registers $AppleMusicCover for the template
from .lyrics import LyricsView
from .queue import QueueView
from .transport import ModeControl, PlayButton, RemoteCover, SeekControl, track_subtitle

SHEET_NATURAL_HEIGHT = 10000   # as tall as the window allows
COVER_SIZE = 320
COVER_SIZE_COMPACT = 240


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/now_playing.ui')
class NowPlayingSheet(Adw.Bin):
    """The Now Playing sheet. See the module."""

    __gtype_name__ = 'AppleMusicNowPlayingSheet'

    toast_overlay = Gtk.Template.Child()
    close_button = Gtk.Template.Child()
    layout = Gtk.Template.Child()
    player_column = Gtk.Template.Child()
    tabs_column = Gtk.Template.Child()
    cover = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()
    seek_box = Gtk.Template.Child()
    seek_scale = Gtk.Template.Child()
    seek_adjustment = Gtk.Template.Child()
    elapsed_label = Gtk.Template.Child()
    remaining_label = Gtk.Template.Child()
    shuffle_button = Gtk.Template.Child()
    previous_button = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    next_button = Gtk.Template.Child()
    repeat_button = Gtk.Template.Child()
    tabs = Gtk.Template.Child()
    tab_stack = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_layout_manager(None)  # do_measure and do_size_allocate below
        self._player = None
        self._bottom_sheet = None
        self._wide = False
        self._compact = False
        self.lyrics_view = LyricsView()
        self.queue_view = QueueView()
        self.tab_stack.add_named(self.lyrics_view, 'lyrics')
        self.tab_stack.add_named(self.queue_view, 'queue')
        self._play = PlayButton(self.play_button)
        self._seek = SeekControl(self.seek_scale, self.seek_adjustment, self.elapsed_label,
                                 self.remaining_label)
        self._modes = ModeControl(self.shuffle_button, self.repeat_button)
        self._art = RemoteCover(self.cover)
        self._apply_layout()

    # -- layout ------------------------------------------------------------------------------

    def _get_wide(self):
        return self._wide

    def _set_wide(self, wide):
        if wide != self._wide:
            self._wide = wide
            self._apply_layout()

    wide = GObject.Property(type=bool, default=False, getter=_get_wide, setter=_set_wide,
                            nick='Wide', blurb='The item and the tabs side by side')

    def _get_compact(self):
        return self._compact

    def _set_compact(self, compact):
        if compact != self._compact:
            self._compact = compact
            self._apply_layout()

    compact = GObject.Property(type=bool, default=False, getter=_get_compact,
                               setter=_set_compact, nick='Compact', blurb='Smaller artwork')

    def _apply_layout(self):
        wide = self._wide
        self.layout.set_orientation(
            Gtk.Orientation.HORIZONTAL if wide else Gtk.Orientation.VERTICAL)
        self.layout.set_spacing(36 if wide else 12 if self._compact else 18)
        self.player_column.set_valign(Gtk.Align.CENTER if wide else Gtk.Align.START)
        self.player_column.set_spacing(12 if self._compact and not wide else 18)
        self.player_column.set_hexpand(False)
        self.cover.set_property('size', COVER_SIZE_COMPACT if self._compact and not wide
                                else COVER_SIZE)

    def do_measure(self, orientation, for_size):
        child = self.get_child()
        if child is None:
            return 0, 0, -1, -1
        minimum, natural, minimum_baseline, natural_baseline = child.measure(orientation,
                                                                             for_size)
        if orientation == Gtk.Orientation.VERTICAL:
            natural = max(natural, SHEET_NATURAL_HEIGHT)
        return minimum, natural, minimum_baseline, natural_baseline

    def do_size_allocate(self, width, height, baseline):
        child = self.get_child()
        if child is not None:
            child.allocate(width, height, baseline, None)

    # -- the Player --------------------------------------------------------------------------

    def set_player(self, player, app, bottom_sheet):
        """Follow `player`; `app` spawns the commands and reports their failures;
        `bottom_sheet` is the Adw.BottomSheet this is the sheet of."""
        self._player = player
        self._bottom_sheet = bottom_sheet
        self._play.attach(player)
        self._seek.attach(player, app)
        self._modes.attach(player, app)
        self._art.attach(player, app)
        self.lyrics_view.set_player(player, app)
        self.queue_view.set_player(player, app)
        player.connect('notify::track', lambda *_: self._update_track())
        bottom_sheet.connect('notify::open', lambda *_: self._on_open_changed())
        self._update_track()
        self._on_open_changed()

    def _update_track(self):
        track = self._player.track if self._player is not None else None
        playing = track is not None
        if playing:
            self.title_label.set_label(track.title or _('Unknown Title'))
            self.subtitle_label.set_label(track_subtitle(track))
        else:
            self.title_label.set_label(_('Not Playing'))
            self.subtitle_label.set_label('')
        self.subtitle_label.set_visible(playing and bool(self.subtitle_label.get_label()))
        self.seek_box.set_sensitive(playing)
        self.shuffle_button.set_sensitive(playing)
        self.repeat_button.set_sensitive(playing)

    def _on_open_changed(self):
        is_open = self._bottom_sheet is not None and self._bottom_sheet.get_open()
        shown = self.tab_stack.get_visible_child_name()
        self.lyrics_view.set_active(is_open and shown == 'lyrics')
        self.queue_view.set_active(is_open and shown == 'queue')

    def add_toast(self, toast):
        self.toast_overlay.add_toast(toast)

    # -- the user ----------------------------------------------------------------------------

    @Gtk.Template.Callback()
    def on_close_clicked(self, _button):
        if self._bottom_sheet is not None:
            self._bottom_sheet.set_open(False)

    @Gtk.Template.Callback()
    def on_tab_changed(self, tabs, _pspec):
        name = tabs.get_active_name() or 'lyrics'
        self.tab_stack.set_visible_child_name(name)
        self._on_open_changed()
