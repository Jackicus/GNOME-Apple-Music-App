# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""AppleMusicPlayerBar: the transport bar, following the Player.

The window hands it the Player (set_player) once, after the template is built. Everything
shown comes from the Player's properties: the play button's icon from `state`, the title,
artist and artwork from `track` (the artwork fetched by remote.fetch_remote at the cover
size, so the Now Playing sheet and MPRIS find the same file), the seek slider and the times
from `position` and `duration`, the toggles from `shuffle` and `repeat`, the volume button
from `volume`, the heart from the engine's rating of the track (HeartControl). The previous,
play and next buttons run the app's actions (app.previous, app.play-pause, app.next: enabled
while something plays); the toggles and the sliders call the Player's coroutines, and a
failure is toasted by the app. The play button, the seek slider, the toggles, the volume,
the heart and the artwork are the pieces widgets/transport.py holds, shared with the Now
Playing sheet.

A right click, a long press or the Menu key on the bar opens the item playing's menu (its
`context_item`: context_menu on the bar's button), as the Now Playing sheet's More Options
button does.

For assistive technology: the bar announces each new item (Gtk.Accessible.announce, "Now
playing …"); the button Adw.BottomSheet puts around the bar (a click on the bar opens the
sheet) is named "Now Playing" and described by the item playing, where it would otherwise be
named after everything in the bar; the volume button and its slider are named "Volume" and
read their level as a percentage (VolumeControl); the seek slider reads "1:05 of 3:40"
(SeekControl).
"""

from gettext import gettext as _

from gi.repository import Adw, GObject, Gtk

from .actions import now_playing_track
from .widgets import context_menu
from .widgets.cover import Cover  # noqa: F401  registers $AppleMusicCover for the template
from .widgets.transport import (HeartControl, ModeControl, PlayButton, RemoteCover, SeekControl,
                                TrackTitles, VolumeControl, track_subtitle)


@Gtk.Template(resource_path='/io/github/jackicus/MusicSleeve/player_bar.ui')
class PlayerBar(Adw.Bin):
    """The transport bar under the content. See the module."""

    __gtype_name__ = 'AppleMusicPlayerBar'

    # The item playing's menu is a queued one: no Play (context_menu).
    context_queued = True

    play_button = Gtk.Template.Child()
    cover = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    explicit_badge = Gtk.Template.Child()
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
        self._compact = False
        self._announced = None      # the id of the item last announced
        self._play = PlayButton(self.play_button)
        self._titles = TrackTitles(self.title_label, self.subtitle_label,
                                   sensitive=(self.seek_box, self.shuffle_button,
                                              self.repeat_button, self.volume_button))
        self._seek = SeekControl(self.seek_scale, self.seek_adjustment, self.elapsed_label,
                                 self.remaining_label)
        self._modes = ModeControl(self.shuffle_button, self.repeat_button)
        self._heart = HeartControl(self.heart_button)
        self._art = RemoteCover(self.cover)
        self._volume = VolumeControl(self.volume_button, self.volume_adjustment)

    def _get_compact(self):
        return self._compact

    def _set_compact(self, compact):
        # The title keeps the room: at 360 px it showed "Not Play…" with them. Shuffle and
        # repeat stay in the Now Playing sheet, which the bar opens.
        self._compact = compact
        for widget in (self.volume_button, self.shuffle_button, self.repeat_button,
                       self.elapsed_label, self.remaining_label):
            widget.set_visible(not compact)
        self._show_playing_controls()

    compact = GObject.Property(type=bool, default=False, getter=_get_compact, setter=_set_compact,
                               nick='Compact',
                               blurb='Hide the volume, the heart, shuffle, repeat and the times '
                                     '(narrow)')

    def set_player(self, player, app):
        """Follow `player`; `app` spawns the commands and reports their failures."""
        self._player = player
        self._app = app
        self._play.attach(player)
        self._titles.attach(player)
        self._seek.attach(player, app)
        self._modes.attach(player, app)
        self._heart.attach(player, app)
        self._art.attach(player, app)
        self._volume.attach(player, app)
        self._strings = {
            'now-playing': _('Now Playing'),
            'not-playing': _('Not Playing'),
            'announce': _('Now playing: {title} by {artist}'),
            'announce-title': _('Now playing: {title}'),
        }
        button = self._bar_button()
        if button is not None:
            button.update_property([Gtk.AccessibleProperty.LABEL], [self._strings['now-playing']])
            context_menu.attach(button)  # on the button: the Menu key reaches it, focused
        player.connect('notify::track', lambda *_: self._update_track())
        self._update_track()

    @property
    def context_item(self):
        """The item playing, as the Track its menu is for (actions.now_playing_track), or
        None."""
        return now_playing_track(self._player.track if self._player is not None else None)

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

    # -- following the Player --------------------------------------------------------------

    def _update_track(self):
        """What the bar adds to TrackTitles: the explicit badge, the controls that show only
        with an item, the bar button's description, and the announcement of a new item
        (once per item; the same song after a stop is announced again)."""
        track = self._player.track if self._player is not None else None
        playing = track is not None
        self.explicit_badge.set_visible(playing and track.explicit)
        self._show_playing_controls()
        button = self._bar_button()
        if button is not None:
            description = (', '.join(part for part in (track.title, track_subtitle(track))
                                     if part) if playing else self._strings['not-playing'])
            button.update_property([Gtk.AccessibleProperty.DESCRIPTION], [description])
        if not playing:
            self._announced = None
        elif track.id != self._announced:
            self._announced = track.id
            self._announce(track)

    def _show_playing_controls(self):
        """The seek slider and the heart only with an item (the idle bar shows no dead
        slider); the focus, if it was on a control now hidden or insensitive, moves to
        the bar itself (the Now Playing button), never off the window."""
        playing = self._player is not None and self._player.track is not None
        self.seek_box.set_visible(playing)
        self.heart_button.set_visible(playing and not self._compact)
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        if (focus is not None and focus.is_ancestor(self)
                and not (focus.get_mapped() and focus.is_sensitive())):
            self.grab_bar_focus()

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
