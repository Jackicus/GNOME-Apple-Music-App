"""AppleMusicPlayerBar: the transport bar, following the Player.

The window hands it the Player (set_player) once, after the template is built. Everything
shown comes from the Player's properties: the play button's icon from `state`, the title,
artist and artwork from `track` (the artwork fetched by Artwork.fetch_remote at the cover
size, so the Now Playing sheet finds the same file), the seek slider and the times from
`position` and `duration`, the toggles from `shuffle` and `repeat`, the volume button from
`volume`. The previous, play and next buttons run the app's actions (app.previous,
app.play-pause, app.next: enabled while something plays); the toggles and the sliders call
the Player's coroutines, and a failure is toasted by the app. While the slider is dragged
the incoming positions are ignored, and the seek is sent when the drag settles.
"""

import logging
import time
from gettext import gettext as _

from gi.repository import Adw, GLib, GObject, Gtk

from .backend import config
from .backend.errors import EngineError
from .player import format_time
from .widgets import artwork
from .widgets.cover import Cover  # noqa: F401  registers $AppleMusicCover for the template

log = logging.getLogger(__name__)

SEEK_SETTLE = 0.25       # seconds after the last slider movement before the seek is sent
SEEK_HOLD = 1.5          # seconds after a seek during which stale positions are ignored
SEEK_TOLERANCE = 2.0     # a reported position this close to the seek's target is the seek's

REPEAT_ICONS = {
    'none': 'media-playlist-repeat-symbolic',
    'all': 'media-playlist-repeat-symbolic',
    'one': 'media-playlist-repeat-song-symbolic',
}


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
    shuffle_button = Gtk.Template.Child()
    repeat_button = Gtk.Template.Child()
    volume_button = Gtk.Template.Child()
    volume_adjustment = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._player = None
        self._app = None
        self._syncing = False       # the toggles and the volume are being set from the Player
        self._seek_timeout = 0      # the GLib source waiting for the drag to settle
        self._seek_target = None    # the position asked for while a seek is pending or fresh
        self._seek_until = 0.0      # until when a stale position is ignored after a seek
        self._art_url = None        # the artwork URL being shown or fetched

    def _get_compact(self):
        return not self.volume_button.get_visible()

    def _set_compact(self, compact):
        self.volume_button.set_visible(not compact)
        self.elapsed_label.set_visible(not compact)
        self.remaining_label.set_visible(not compact)

    compact = GObject.Property(type=bool, default=False, getter=_get_compact, setter=_set_compact,
                               nick='Compact', blurb='Hide the volume and the times (narrow)')

    def set_player(self, player, app):
        """Follow `player`; `app` spawns the commands and reports their failures."""
        self._player = player
        self._app = app
        player.connect('notify::state', lambda *_: self._update_state())
        player.connect('notify::track', lambda *_: self._update_track())
        player.connect('notify::position', lambda *_: self._update_position())
        player.connect('notify::duration', lambda *_: self._update_position())
        player.connect('notify::shuffle', lambda *_: self._update_modes())
        player.connect('notify::repeat', lambda *_: self._update_modes())
        player.connect('notify::volume', lambda *_: self._update_volume())
        self._update_track()
        self._update_state()
        self._update_modes()
        self._update_volume()

    # -- following the Player --------------------------------------------------------------

    def _update_state(self):
        active = self._player is not None and self._player.active
        self.play_button.set_icon_name(
            'media-playback-pause-symbolic' if active else 'media-playback-start-symbolic')
        self.play_button.set_tooltip_text(_('Pause') if active else _('Play'))

    def _update_track(self):
        track = self._player.track if self._player is not None else None
        playing = track is not None
        if playing:
            self.title_label.set_label(track.title or _('Unknown Title'))
            subtitle = track.artist
            if track.album and track.album != track.title:
                subtitle = f'{track.artist} — {track.album}' if track.artist else track.album
            self.subtitle_label.set_label(subtitle)
        else:
            self.title_label.set_label(_('Not Playing'))
            self.subtitle_label.set_label('')
        self.subtitle_label.set_visible(playing and bool(self.subtitle_label.get_label()))
        self.seek_box.set_sensitive(playing)
        self.shuffle_button.set_sensitive(playing)
        self.repeat_button.set_sensitive(playing)
        self.volume_button.set_sensitive(playing)
        self._cancel_seek()
        self._update_position()
        self._show_artwork(track.artwork_url if playing else None)

    def _update_position(self):
        if self._player is None:
            return
        if self._seek_target is not None:
            # A seek is on its way: the positions from before it are stale until MusicKit
            # reports one near the target, or the hold runs out.
            if (abs(self._player.position - self._seek_target) > SEEK_TOLERANCE
                    and time.monotonic() < self._seek_until):
                return
            self._seek_target = None
        self._show_position(self._player.position, self._player.duration)

    def _show_position(self, position, duration):
        self._syncing = True
        try:
            self.seek_adjustment.set_upper(max(duration, 1.0))
            self.seek_adjustment.set_value(min(position, max(duration, 1.0)))
        finally:
            self._syncing = False
        self.elapsed_label.set_label(format_time(position))
        self.remaining_label.set_label(format_time(max(duration - position, 0), remaining=True))

    def _update_modes(self):
        if self._player is None:
            return
        self._syncing = True
        try:
            self.shuffle_button.set_active(self._player.shuffle)
            self._show_repeat(self._player.repeat)
        finally:
            self._syncing = False

    def _show_repeat(self, mode):
        self.repeat_button.set_active(mode != 'none')
        self.repeat_button.set_icon_name(REPEAT_ICONS.get(mode, REPEAT_ICONS['none']))
        self.repeat_button.set_tooltip_text({
            'none': _('Repeat'),
            'all': _('Repeat All'),
            'one': _('Repeat One'),
        }.get(mode, _('Repeat')))

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

    def _show_artwork(self, url):
        if url == self._art_url:
            return
        self._art_url = url
        self.cover.set_paths()
        if url and self._app is not None:
            self._app.spawn(self._fetch_artwork(url))

    async def _fetch_artwork(self, url):
        path = await artwork.get_default().fetch_remote(url, config.COVER_SIZE)
        if path and self._art_url == url:
            self.cover.set_paths(path)

    # -- the user ----------------------------------------------------------------------------

    def _run(self, coro, on_error=None):
        """A Player command as a task; an EngineError is toasted, and on_error() puts the
        widget back to the Player's state."""
        async def command():
            try:
                await coro
            except EngineError as error:
                self._app.report(error)
                if on_error is not None:
                    on_error()
        self._app.spawn(command())

    @Gtk.Template.Callback()
    def on_seek_change_value(self, _scale, _scroll, value):
        """The slider moved by the user (a drag, a click, the keys): show the time it points
        at now, and seek there once it has settled for SEEK_SETTLE seconds."""
        if self._syncing or self._player is None:
            return False
        duration = self._player.duration
        value = min(max(value, 0.0), duration if duration else value)
        self._seek_target = value
        self._seek_until = float('inf')  # nothing incoming until the seek has been sent
        self.elapsed_label.set_label(format_time(value))
        self.remaining_label.set_label(format_time(max(duration - value, 0), remaining=True))
        if self._seek_timeout:
            GLib.source_remove(self._seek_timeout)
        self._seek_timeout = GLib.timeout_add(int(SEEK_SETTLE * 1000), self._send_seek)
        return False  # the default handler sets the adjustment's value

    def _send_seek(self):
        self._seek_timeout = 0
        target = self._seek_target
        if target is None:
            return GLib.SOURCE_REMOVE
        self._seek_until = time.monotonic() + SEEK_HOLD
        log.debug('seek to %.1f s', target)
        self._run(self._player.seek(target), self._update_position)
        return GLib.SOURCE_REMOVE

    def _cancel_seek(self):
        if self._seek_timeout:
            GLib.source_remove(self._seek_timeout)
            self._seek_timeout = 0
        self._seek_target = None

    @Gtk.Template.Callback()
    def on_shuffle_toggled(self, button):
        if self._syncing or self._player is None:
            return
        self._run(self._player.set_shuffle(button.get_active()), self._update_modes)

    @Gtk.Template.Callback()
    def on_repeat_toggled(self, button):
        """A click cycles none → one → all → none; the button shows the mode it asked for
        (a toggle alone would go dark between one and all) until MusicKit confirms."""
        if self._syncing or self._player is None:
            return
        mode = self._player.next_repeat()
        self._syncing = True
        try:
            self._show_repeat(mode)
        finally:
            self._syncing = False
        self._run(self._player.set_repeat(mode), self._update_modes)

    @Gtk.Template.Callback()
    def on_volume_changed(self, _button, value):
        if self._syncing or self._player is None:
            return
        self._run(self._player.set_volume(value), self._update_volume)

