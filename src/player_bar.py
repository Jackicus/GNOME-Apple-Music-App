"""AppleMusicPlayerBar: the transport bar, following the Player.

The window hands it the Player (set_player) once, after the template is built. Everything
shown comes from the Player's properties: the play button's icon from `state`, the title,
artist and artwork from `track` (the artwork fetched by remote.fetch_remote at the cover
size, so the Now Playing sheet and MPRIS find the same file), the seek slider and the times
from `position` and `duration`, the toggles from `shuffle` and `repeat`, the volume button
from `volume`, the heart from the engine's rating of the track (HeartControl). The previous,
play and next buttons run the app's actions (app.previous, app.play-pause, app.next: enabled
while something plays); the toggles and the sliders call the Player's coroutines, and a
failure is toasted by the app. The play button, the seek slider, the toggles, the heart and
the artwork are the pieces widgets/transport.py holds, shared (but the heart) with the Now
Playing sheet.

For assistive technology: the bar announces each new item (Gtk.Accessible.announce, "Now
playing …"); the button Adw.BottomSheet puts around the bar (a click on the bar opens the
sheet) is named "Now Playing" and described by the item playing, where it would otherwise be
named after everything in the bar; the volume button and its slider are named "Volume" and
read their level as a percentage; the seek slider reads "1:05 of 3:40" (SeekControl).
"""

from gettext import gettext as _

from gi.repository import Adw, GObject, Gtk

from .widgets.cover import Cover  # noqa: F401  registers $AppleMusicCover for the template
from .widgets.transport import (HeartControl, ModeControl, PlayButton, RemoteCover, SeekControl,
                                run_command, track_subtitle)


@Gtk.Template(resource_path='/io/github/jackicus/AppleMusic/player_bar.ui')
class PlayerBar(Adw.Bin):
    """The transport bar under the content. See the module."""

    __gtype_name__ = 'AppleMusicPlayerBar'

    previous_button = Gtk.Template.Child()
    play_button = Gtk.Template.Child()
    next_button = Gtk.Template.Child()
    cover = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()
    seek_box = Gtk.Template.Child()
    seek_scale = Gtk.Template.Child()
    seek_adjustment = Gtk.Template.Child()
    elapsed_label = Gtk.Template.Child()
    remaining_label = Gtk.Template.Child()
    heart_button = Gtk.Template.Child()
    shuffle_button = Gtk.Template.Child()
    repeat_button = Gtk.Template.Child()
    volume_button = Gtk.Template.Child()
    volume_adjustment = Gtk.Template.Child()

    _volume_scale = None  # the slider in the volume button's popover

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._player = None
        self._app = None
        self._syncing = False       # the volume is being set from the Player
        self._announced = None      # the id of the item last announced
        self._play = PlayButton(self.play_button)
        self._seek = SeekControl(self.seek_scale, self.seek_adjustment, self.elapsed_label,
                                 self.remaining_label)
        self._modes = ModeControl(self.shuffle_button, self.repeat_button)
        self._heart = HeartControl(self.heart_button)
        self._art = RemoteCover(self.cover)

    def _get_compact(self):
        return not self.volume_button.get_visible()

    def _set_compact(self, compact):
        # The title keeps the room: at 360 px it showed "Not Play…" with them. Shuffle and
        # repeat stay in the Now Playing sheet, which the bar opens.
        for widget in (self.volume_button, self.heart_button, self.shuffle_button,
                       self.repeat_button, self.elapsed_label, self.remaining_label):
            widget.set_visible(not compact)

    compact = GObject.Property(type=bool, default=False, getter=_get_compact, setter=_set_compact,
                               nick='Compact',
                               blurb='Hide the volume, the heart, shuffle, repeat and the times '
                                     '(narrow)')

    def set_player(self, player, app):
        """Follow `player`; `app` spawns the commands and reports their failures."""
        self._player = player
        self._app = app
        self._play.attach(player)
        self._seek.attach(player, app)
        self._modes.attach(player, app)
        self._heart.attach(player, app)
        self._art.attach(player, app)
        self._strings = {
            'now-playing': _('Now Playing'),
            'not-playing': _('Not Playing'),
            'announce': _('Now playing: {title} by {artist}'),
            'announce-title': _('Now playing: {title}'),
            'volume': _('Volume'),
            'percent': _('{percent}%'),
        }
        self._label_volume()
        button = self._bar_button()
        if button is not None:
            button.update_property([Gtk.AccessibleProperty.LABEL], [self._strings['now-playing']])
        player.connect('notify::track', lambda *_: self._update_track())
        player.connect('notify::volume', lambda *_: self._update_volume())
        self.volume_adjustment.connect('value-changed', lambda *_: self._describe_volume())
        self._update_track()
        self._update_volume()
        self._describe_volume()

    def _bar_button(self):
        """The button Adw.BottomSheet puts around the bar (a click on it opens the sheet),
        or None."""
        parent = self.get_parent()
        return parent if isinstance(parent, Gtk.Button) else None

    def grab_bar_focus(self):
        """Put the focus on the bar itself (it opens Now Playing): win.focus-player with
        nothing playing, when the bar's own buttons are off."""
        button = self._bar_button()
        return button.grab_focus() if button is not None else self.grab_focus()

    def _label_volume(self):
        """Name the volume button and the slider in its popover."""
        label = self._strings['volume']
        self.volume_button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self._volume_scale = _find(self.volume_button.get_popup(), Gtk.Scale)
        if self._volume_scale is not None:
            self._volume_scale.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def _describe_volume(self):
        text = self._strings['percent'].format(
            percent=round(self.volume_adjustment.get_value() * 100))
        for widget in (self.volume_button, self._volume_scale):
            if widget is not None:
                widget.update_property([Gtk.AccessibleProperty.VALUE_TEXT], [text])

    # -- following the Player --------------------------------------------------------------

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
        self.volume_button.set_sensitive(playing)
        button = self._bar_button()
        if button is not None:
            description = (', '.join(part for part in (track.title, track_subtitle(track))
                                     if part) if playing else self._strings['not-playing'])
            button.update_property([Gtk.AccessibleProperty.DESCRIPTION], [description])
        if playing and track.id != self._announced:
            self._announced = track.id
            self._announce(track)

    def _announce(self, track):
        """Tell assistive technology what plays now, as a screen reader user cannot see the
        bar change. Through the window: GTK drops an announcement from a widget whose
        accessible object no client has asked for yet (the bar's, usually), and the window's
        always is."""
        title = self.title_label.get_label()
        if track.artist:
            message = self._strings['announce'].format(title=title, artist=track.artist)
        else:
            message = self._strings['announce-title'].format(title=title)
        (self.get_root() or self).announce(message, Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def _update_volume(self):
        if self._player is None:
            return
        if abs(self.volume_adjustment.get_value() - self._player.volume) < 0.001:
            return
        self._syncing = True
        try:
            self.volume_adjustment.set_value(self._player.volume)
        finally:
            self._syncing = False

    # -- the user ----------------------------------------------------------------------------

    @Gtk.Template.Callback()
    def on_volume_changed(self, _button, value):
        if self._syncing or self._player is None:
            return
        run_command(self._app, self._player.set_volume(value), self._update_volume)


def _find(widget, cls):
    """The first descendant of widget (itself included) that is a cls, or None."""
    if widget is None or isinstance(widget, cls):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find(child, cls)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None
