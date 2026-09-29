# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicNowPlayingSheet: the bottom sheet's Now Playing view, following the Player.

The window hands it the Player, the app and the Adw.BottomSheet it sits in (set_player)
once, after the template is built. The artwork, titles, transport, seek slider, toggles,
heart and volume follow the Player as the player bar's do (the pieces are shared:
widgets/transport.py); the Lyrics and Up Next tabs are a LyricsView and a QueueView (built
here: GtkBuilder does not run a Python widget's __init__) in an Adw.ViewStack an inline
view switcher switches. The close button sets the bottom sheet's `open` false; Escape and a
swipe down are the bottom sheet's own.

Sizing: an AdwBottomSheet gives its sheet its natural height (clamped to the window less a
margin), so this widget asks for a tall one (SHEET_NATURAL_HEIGHT) and the tabs' lists take
what is left under the controls. It measures and allocates its child itself: an Adw.Bin's
layout manager answers gtk_widget_measure before the class's measure vfunc, so the bin's is
dropped (set_layout_manager(None)). The window's breakpoints set three properties: `wide`
(900sp and up) lays the item and the tabs side by side; `compact` (600sp and under) shrinks
the artwork so a phone-sized window keeps room for the tabs; `short` (a window too low for
the artwork over the titles: window.blp's height conditions) puts a small cover beside the
titles instead, so the transport is never cut off and the tabs keep a few lines.
"""

from gettext import gettext as _

from gi.repository import Adw, GLib, GObject, Gtk

from .cover import Cover  # noqa: F401  registers $AppleMusicCover for the template
from .lyrics import LyricsView
from .queue import QueueView
from .transport import (HeartControl, ModeControl, PlayButton, RemoteCover, SeekControl,
                        TrackTitles, VolumeControl)

SHEET_NATURAL_HEIGHT = 10000   # as tall as the window allows
COVER_SIZE = 320
COVER_SIZE_COMPACT = 240
COVER_SIZE_SHORT = 96          # beside the titles, in a short window


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/now_playing.ui')
class NowPlayingSheet(Adw.Bin):
    """The Now Playing sheet. See the module."""

    __gtype_name__ = 'AppleMusicNowPlayingSheet'

    toast_overlay = Gtk.Template.Child()
    close_button = Gtk.Template.Child()
    heart_button = Gtk.Template.Child()
    volume_button = Gtk.Template.Child()
    volume_adjustment = Gtk.Template.Child()
    layout = Gtk.Template.Child()
    player_column = Gtk.Template.Child()
    item_box = Gtk.Template.Child()
    titles_box = Gtk.Template.Child()
    cover = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()
    seek_box = Gtk.Template.Child()
    seek_scale = Gtk.Template.Child()
    seek_adjustment = Gtk.Template.Child()
    elapsed_label = Gtk.Template.Child()
    remaining_label = Gtk.Template.Child()
    shuffle_button = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    repeat_button = Gtk.Template.Child()
    tab_stack = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_layout_manager(None)  # do_measure and do_size_allocate below
        self._player = None
        self._bottom_sheet = None
        self._open = False
        self._wide = False
        self._compact = False
        self._short = False
        self.lyrics_view = LyricsView()
        self.queue_view = QueueView()
        self.tab_stack.add_titled(self.lyrics_view, 'lyrics', _('Lyrics'))
        self.tab_stack.add_titled(self.queue_view, 'queue', _('Up Next'))
        self.tab_stack.connect('notify::visible-child-name', lambda *_: self._on_open_changed())
        self._play = PlayButton(self.play_button)
        self._titles = TrackTitles(self.title_label, self.subtitle_label,
                                   sensitive=(self.seek_box, self.shuffle_button,
                                              self.repeat_button, self.volume_button),
                                   tooltips=True)
        self._seek = SeekControl(self.seek_scale, self.seek_adjustment, self.elapsed_label,
                                 self.remaining_label)
        self._modes = ModeControl(self.shuffle_button, self.repeat_button)
        self._heart = HeartControl(self.heart_button)
        self._volume = VolumeControl(self.volume_button, self.volume_adjustment)
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

    def _get_short(self):
        return self._short

    def _set_short(self, short):
        if short != self._short:
            self._short = short
            self._apply_layout()

    short = GObject.Property(type=bool, default=False, getter=_get_short, setter=_set_short,
                             nick='Short', blurb='A small cover beside the titles')

    def _apply_layout(self):
        wide, short = self._wide, self._short
        self.layout.set_orientation(
            Gtk.Orientation.HORIZONTAL if wide else Gtk.Orientation.VERTICAL)
        self.layout.set_spacing(36 if wide else 12 if self._compact or short else 18)
        self.player_column.set_valign(Gtk.Align.CENTER if wide else Gtk.Align.START)
        self.player_column.set_spacing(12 if (self._compact or short) and not wide else 18)
        self.player_column.set_hexpand(False)
        self.player_column.set_halign(Gtk.Align.FILL if short and not wide
                                      else Gtk.Align.CENTER)
        # The cover beside the titles (short), else over them.
        self.item_box.set_orientation(Gtk.Orientation.HORIZONTAL if short
                                      else Gtk.Orientation.VERTICAL)
        self.item_box.set_spacing(12 if short else 18)
        self.item_box.set_halign(Gtk.Align.FILL if short else Gtk.Align.CENTER)
        self.titles_box.set_hexpand(short)
        self.titles_box.set_halign(Gtk.Align.START if short else Gtk.Align.CENTER)
        for label in (self.title_label, self.subtitle_label):
            label.set_xalign(0 if short else 0.5)
            label.set_justify(Gtk.Justification.LEFT if short else Gtk.Justification.CENTER)
        self.cover.set_property(
            'size', COVER_SIZE_SHORT if short
            else COVER_SIZE_COMPACT if self._compact and not wide else COVER_SIZE)

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
        self._titles.attach(player)
        self._seek.attach(player, app)
        self._modes.attach(player, app)
        self._heart.attach(player, app)
        self._volume.attach(player, app)
        self._art.attach(player, app)
        self.lyrics_view.set_player(player, app)
        self.queue_view.set_player(player, app)
        bottom_sheet.connect('notify::open', lambda *_: self._on_open_changed())
        self._on_open_changed()

    def _on_open_changed(self):
        is_open = self._bottom_sheet is not None and self._bottom_sheet.get_open()
        shown = self.tab_stack.get_visible_child_name()
        self.lyrics_view.set_active(is_open and shown == 'lyrics')
        self.queue_view.set_active(is_open and shown == 'queue')
        if is_open and not self._open:
            # The bottom sheet puts the focus on the sheet's first focusable widget (the seek
            # slider); the play button is where the keyboard wants to start. After the
            # sheet's own grab, from an idle.
            GLib.idle_add(self._focus_on_open)
        self._open = is_open

    def _focus_on_open(self):
        if self._bottom_sheet is not None and self._bottom_sheet.get_open():
            self.focus_controls()
        return GLib.SOURCE_REMOVE

    def focus_controls(self):
        """Put the focus on the play button, or on the close button with nothing playing
        (Ctrl+3 while the sheet is open, and as it opens)."""
        if not self.play_button.grab_focus():
            self.close_button.grab_focus()

    def add_toast(self, toast):
        self.toast_overlay.add_toast(toast)

    # -- the user ----------------------------------------------------------------------------

    @Gtk.Template.Callback()
    def on_close_clicked(self, _button):
        if self._bottom_sheet is not None:
            self._bottom_sheet.set_open(False)
