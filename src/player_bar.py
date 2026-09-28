"""AppleMusicPlayerBar: the transport bar, following the Player.

The window hands it the Player (set_player) once, after the template is built. Everything
shown comes from the Player's properties: the play button's icon from `state`, the title,
artist and artwork from `track` (the artwork fetched by Artwork.fetch_remote at the cover
size, so the Now Playing sheet and MPRIS find the same file), the seek slider and the times
from `position` and `duration`, the toggles from `shuffle` and `repeat`, the volume button
from `volume`, the heart from the engine's rating of the track (HeartControl). The previous,
play and next buttons run the app's actions (app.previous, app.play-pause, app.next: enabled
while something plays); the toggles and the sliders call the Player's coroutines, and a
failure is toasted by the app. The play button, the seek slider, the toggles, the heart and
the artwork are the pieces widgets/transport.py holds, shared (but the heart) with the Now
Playing sheet.
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

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._player = None
        self._app = None
        self._syncing = False       # the volume is being set from the Player
        self._play = PlayButton(self.play_button)
        self._seek = SeekControl(self.seek_scale, self.seek_adjustment, self.elapsed_label,
                                 self.remaining_label)
        self._modes = ModeControl(self.shuffle_button, self.repeat_button)
        self._heart = HeartControl(self.heart_button)
        self._art = RemoteCover(self.cover)

    def _get_compact(self):
        return not self.volume_button.get_visible()

    def _set_compact(self, compact):
        self.volume_button.set_visible(not compact)
        self.heart_button.set_visible(not compact)  # the title keeps the room (400 px)
        self.elapsed_label.set_visible(not compact)
        self.remaining_label.set_visible(not compact)

    compact = GObject.Property(type=bool, default=False, getter=_get_compact, setter=_set_compact,
                               nick='Compact',
                               blurb='Hide the volume, the heart and the times (narrow)')

    def set_player(self, player, app):
        """Follow `player`; `app` spawns the commands and reports their failures."""
        self._player = player
        self._app = app
        self._play.attach(player)
        self._seek.attach(player, app)
        self._modes.attach(player, app)
        self._heart.attach(player, app)
        self._art.attach(player, app)
        player.connect('notify::track', lambda *_: self._update_track())
        player.connect('notify::volume', lambda *_: self._update_volume())
        self._update_track()
        self._update_volume()

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
