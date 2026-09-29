"""The transport pieces the player bar and the Now Playing sheet share, each a plain object
over widgets a template built: the play/pause button's icon (PlayButton), the title and
subtitle labels (TrackTitles), the seek slider with its times (SeekControl), the shuffle
and repeat toggles (ModeControl), the volume button (VolumeControl), a cover following the
item's remote artwork (RemoteCover), and the heart (HeartControl, which talks to the
engine rather than the Player). `attach(player,
app)` makes one follow the Player's properties and send its commands through the Player,
run by `app.player_command(coro, on_error)`: a command that fails is toasted by the app
and the widget put back to the Player's state.

The heart (HeartControl) shows whether the item playing is loved: unloved as each item
starts, then what the engine's rating() answers (one read per item, nothing polled), and
whatever its `rated` signal says after (the heart's own love, a context menu's); a click
loves or unloves the item's catalog song, and puts the heart back when that fails.

The seek slider's decisions are a SeekGuard (no GTK, tested on its own): nothing incoming
counts while a seek waits to be sent (SEEK_SETTLE after the last movement), and after it
is sent until MusicKit reports a position near the target (SEEK_TOLERANCE) or SEEK_HOLD
passes; a seek that fails releases the guard. The volume button sends one command at a
time and the latest level after it (a Coalescer), and ignores the echoes of older levels
meanwhile. The repeat button cycles none → one → all → none, showing the mode it asked for
until MusicKit confirms. The artwork is fetched at the cover size (remote.fetch_remote into
remote-art/), so the bar, the sheet and MPRIS share one file.
"""

import logging
import time
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from ..backend import config
from ..backend.errors import EngineError
from ..player import format_time
from ..remote import fetch_remote

log = logging.getLogger(__name__)

SEEK_SETTLE = 0.25       # seconds after the last slider movement before the seek is sent
SEEK_HOLD = 1.5          # seconds after a seek during which stale positions are ignored
SEEK_TOLERANCE = 2.0     # a reported position this close to the seek's target is the seek's


class SeekGuard:
    """Which incoming positions the seek slider shows while a seek is about: see the
    module. `target` is the position asked for while a seek waits to be sent or has just
    gone out, else None."""

    def __init__(self, hold=SEEK_HOLD, tolerance=SEEK_TOLERANCE):
        self._hold = hold
        self._tolerance = tolerance
        self.target = None
        self._until = None  # None while the seek waits to be sent; then the hold's end

    def moved(self, value):
        """The slider moved to `value`: a seek there is being prepared."""
        self.target = value
        self._until = None

    def sent(self, now):
        """The seek has gone out: stale positions are ignored until the hold passes."""
        self._until = now + self._hold

    def release(self):
        """Forget the seek (it failed, or the item changed)."""
        self.target = None
        self._until = None

    def accept(self, position, now):
        """Whether an incoming `position` is to be shown: always without a seek about;
        never while one waits to be sent; once it is sent, when the position is within the
        tolerance of the target (the seek has taken) or the hold has passed, either of
        which releases the guard."""
        if self.target is None:
            return True
        if self._until is None:
            return False
        if abs(position - self.target) <= self._tolerance or now >= self._until:
            self.release()
            return True
        return False


class Coalescer:
    """One command in flight at a time, and the latest value asked for meanwhile sent when
    it returns: what the volume button does with a drag's stream of levels."""

    def __init__(self):
        self.in_flight = None  # the value with the engine, while one is
        self.waiting = None    # the latest value asked for while one was in flight

    def moved(self, value):
        """A new value: the one to send now, or None while another is in flight."""
        if self.in_flight is None:
            self.in_flight = value
            return value
        self.waiting = value
        return None

    def done(self):
        """The command in flight returned: the value to send next (now in flight), or None
        when none waits."""
        self.in_flight, self.waiting = self.waiting, None
        return self.in_flight

    @property
    def target(self):
        """The level the engine is being asked for, or None when idle."""
        return self.waiting if self.waiting is not None else self.in_flight

REPEAT_ICONS = {
    'none': 'media-playlist-repeat-symbolic',
    'all': 'media-playlist-repeat-symbolic',
    'one': 'media-playlist-repeat-song-symbolic',
}


def track_subtitle(track):
    """"Artist — Album" for the item playing, the album left out when it repeats the title
    (a single) or there is none."""
    subtitle = track.artist
    if track.album and track.album != track.title:
        subtitle = f'{track.artist} — {track.album}' if track.artist else track.album
    return subtitle


class TrackTitles:
    """The title and subtitle labels of the bar and the sheet: the item's title ("Unknown
    Title" without one) and track_subtitle(), or "Not Playing" and no subtitle; the
    `sensitive` widgets (the seek box, the toggles, the volume) follow whether there is an
    item. With `tooltips`, the labels carry their full text as a tooltip (the sheet's wrap
    to two lines and may still cut a long one)."""

    def __init__(self, title_label, subtitle_label, sensitive=(), tooltips=False):
        self.title_label = title_label
        self.subtitle_label = subtitle_label
        self._sensitive = tuple(sensitive)
        self._tooltips = tooltips
        self._player = None
        self.playing = False  # whether an item is shown

    def attach(self, player):
        self._player = player
        player.connect('notify::track', lambda *_: self.update())
        self.update()

    def update(self):
        track = self._player.track if self._player is not None else None
        self.playing = track is not None
        title = (track.title or _('Unknown Title')) if self.playing else _('Not Playing')
        subtitle = track_subtitle(track) if self.playing else ''
        self.title_label.set_label(title)
        self.subtitle_label.set_label(subtitle)
        self.subtitle_label.set_visible(bool(subtitle))
        if self._tooltips:
            self.title_label.set_tooltip_text(title if self.playing else None)
            self.subtitle_label.set_tooltip_text(subtitle or None)
        for widget in self._sensitive:
            widget.set_sensitive(self.playing)


class PlayButton:
    """A play/pause button whose icon and tooltip follow the Player's state, with a spinner
    in place of the icon while a play request is with the engine (`pending`); its action
    (app.play-pause) is the template's."""

    def __init__(self, button):
        self.button = button
        self._player = None
        self._spinner = None

    def attach(self, player):
        self._player = player
        player.connect('notify::state', lambda *_: self.update())
        player.connect('notify::pending', lambda *_: self.update())
        self.update()

    def update(self):
        player = self._player
        active = player is not None and player.active
        if player is not None and player.pending:
            if self._spinner is None:
                self._spinner = Adw.Spinner()
            if self.button.get_child() is not self._spinner:
                self.button.set_child(self._spinner)
        else:
            self.button.set_icon_name(
                'media-playback-pause-symbolic' if active else 'media-playback-start-symbolic')
        self.button.set_tooltip_text(_('Pause') if active else _('Play'))


class SeekControl:
    """A Gtk.Scale over an adjustment, with optional elapsed and remaining labels, following
    the Player's position and duration and seeking when the user moves it."""

    def __init__(self, scale, adjustment, elapsed_label=None, remaining_label=None):
        self.scale = scale
        self.adjustment = adjustment
        self.elapsed_label = elapsed_label
        self.remaining_label = remaining_label
        self._value_format = _('{position} of {duration}')  # looked up once: this runs often
        self._value_text = None  # what the slider reads to assistive technology
        self._player = None
        self._app = None
        self._syncing = False       # the slider is being set from the Player
        self._seek_timeout = 0      # the GLib source waiting for the drag to settle
        self._guard = SeekGuard()
        scale.connect('change-value', self._on_change_value)

    def attach(self, player, app):
        self._player = player
        self._app = app
        player.connect('notify::position', lambda *_: self.update())
        player.connect('notify::duration', lambda *_: self.update())
        player.connect('notify::track', lambda *_: self._on_track())
        self._on_track()

    def _on_track(self):
        self.cancel()
        self.update()

    def update(self):
        """Show the Player's position, unless a seek is about (SeekGuard): the positions
        from before it are stale until MusicKit reports one near the target, or the hold
        runs out."""
        if self._player is None:
            return
        if not self._guard.accept(self._player.position, time.monotonic()):
            return
        self.show(self._player.position, self._player.duration)

    def show(self, position, duration):
        self._syncing = True
        try:
            self.adjustment.set_upper(max(duration, 1.0))
            self.adjustment.set_value(min(position, max(duration, 1.0)))
        finally:
            self._syncing = False
        self._show_times(position, duration)

    def _show_times(self, position, duration):
        elapsed = format_time(position)
        if self.elapsed_label is not None:
            self.elapsed_label.set_label(elapsed)
        if self.remaining_label is not None:
            self.remaining_label.set_label(
                format_time(max(duration - position, 0), remaining=True))
        # Assistive technology reads the slider's value as "1:05 of 3:40", not as seconds;
        # set when the text changes (once a second at most), not at every position event.
        text = self._value_format.format(position=elapsed, duration=format_time(duration))
        if text != self._value_text:
            self._value_text = text
            self.scale.update_property([Gtk.AccessibleProperty.VALUE_TEXT], [text])

    def _on_change_value(self, _scale, _scroll, value):
        """The slider moved by the user (a drag, a click, the keys): show the time it points
        at now, and seek there once it has settled for SEEK_SETTLE seconds."""
        if self._syncing or self._player is None:
            return False
        duration = self._player.duration
        value = min(max(value, 0.0), duration if duration else value)
        self._guard.moved(value)
        self._show_times(value, duration)
        if self._seek_timeout:
            GLib.source_remove(self._seek_timeout)
        self._seek_timeout = GLib.timeout_add(int(SEEK_SETTLE * 1000), self._send_seek)
        return False  # the default handler sets the adjustment's value

    def _send_seek(self):
        self._seek_timeout = 0
        target = self._guard.target
        if target is None:
            return GLib.SOURCE_REMOVE
        self._guard.sent(time.monotonic())
        log.debug('seek to %.1f s', target)
        self._app.player_command(self._player.seek(target), self._on_seek_failed)
        return GLib.SOURCE_REMOVE

    def _on_seek_failed(self):
        self._guard.release()
        self.update()

    def cancel(self):
        """Forget a seek being prepared (the item changed)."""
        if self._seek_timeout:
            GLib.source_remove(self._seek_timeout)
            self._seek_timeout = 0
        self._guard.release()


class ModeControl:
    """Shuffle and repeat Gtk.ToggleButtons: a click asks the Player, the events set them."""

    def __init__(self, shuffle_button, repeat_button):
        self.shuffle_button = shuffle_button
        self.repeat_button = repeat_button
        self._player = None
        self._app = None
        self._syncing = False
        shuffle_button.connect('toggled', self._on_shuffle_toggled)
        repeat_button.connect('toggled', self._on_repeat_toggled)

    def attach(self, player, app):
        self._player = player
        self._app = app
        player.connect('notify::shuffle', lambda *_: self.update())
        player.connect('notify::repeat', lambda *_: self.update())
        self.update()

    def update(self):
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

    def _on_shuffle_toggled(self, button):
        if self._syncing or self._player is None:
            return
        self._app.player_command(self._player.set_shuffle(button.get_active()), self.update)

    def _on_repeat_toggled(self, _button):
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
        self._app.player_command(self._player.set_repeat(mode), self.update)


class VolumeControl:
    """A Gtk.ScaleButton (with the slider in its popover) following the Player's volume
    and setting it, one command at a time (Coalescer); the button and the slider are named
    "Volume" for assistive technology and read their level as a percentage."""

    def __init__(self, button, adjustment):
        self.button = button
        self.adjustment = adjustment
        self._player = None
        self._app = None
        self._syncing = False  # the adjustment is being set from the Player
        self._coalescer = Coalescer()
        self._scale = None  # the slider in the button's popover
        self._strings = {}
        button.connect('value-changed', self._on_changed)
        adjustment.connect('value-changed', lambda *_: self._describe())

    def attach(self, player, app):
        self._player = player
        self._app = app
        self._strings = {'volume': _('Volume'), 'percent': _('{percent}%')}
        self._label()
        player.connect('notify::volume', lambda *_: self.update())
        self.update()
        self._describe()

    def _label(self):
        label = self._strings['volume']
        self.button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self._scale = find_descendant(self.button.get_popup(), Gtk.Scale)
        if self._scale is not None:
            self._scale.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def _describe(self):
        if not self._strings:
            return
        text = self._strings['percent'].format(percent=round(self.adjustment.get_value() * 100))
        for widget in (self.button, self._scale):
            if widget is not None:
                widget.update_property([Gtk.AccessibleProperty.VALUE_TEXT], [text])

    def update(self):
        """Show the Player's volume, unless a level of the user's is still with the engine
        and this is the echo of an older one."""
        if self._player is None:
            return
        volume = self._player.volume
        target = self._coalescer.target
        if target is not None and abs(volume - target) > 0.001:
            return
        if abs(self.adjustment.get_value() - volume) < 0.001:
            return
        self._syncing = True
        try:
            self.adjustment.set_value(volume)
        finally:
            self._syncing = False

    def _on_changed(self, _button, value):
        if self._syncing or self._player is None:
            return
        to_send = self._coalescer.moved(value)
        if to_send is not None:
            self._send(to_send)

    def _send(self, value):
        async def command():
            try:
                await self._player.set_volume(value)
            finally:
                following = self._coalescer.done()
                if following is not None:
                    self._send(following)
        self._app.player_command(command(), self.update)


def find_descendant(widget, cls):
    """The first descendant of widget (itself included) that is a cls, or None."""
    if widget is None or isinstance(widget, cls):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = find_descendant(child, cls)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


class HeartControl:
    """A Gtk.ToggleButton that loves the item playing: see the module. Its accessible name
    is the template's ("Favourite"); the tooltip says what a click does."""

    def __init__(self, button):
        self.button = button
        self._player = None
        self._app = None
        self._syncing = False
        self._reading = None  # the task reading the item's rating
        self._shown = None    # the target the heart shows, read once per item
        button.connect('toggled', self._on_toggled)

    def attach(self, player, app):
        self._player = player
        self._app = app
        player.connect('notify::track', lambda *_: self._on_track())
        app.engine.connect('rated', self._on_rated)
        app.engine.connect('notify::state', lambda *_: self._on_engine_state())
        self._on_track()

    def target(self):
        """(kind, id) for the item playing, `kind` 'song' or 'video' (NowPlaying.kind) and
        the id its catalog id, else its own; None for an item that cannot be loved (a
        station's segment, an ad), which leaves the heart insensitive."""
        track = self._player.track if self._player is not None else None
        if track is None or track.kind not in ('song', 'video'):
            return None
        item_id = track.catalog_id or track.id
        return (track.kind, item_id) if item_id else None

    def _on_engine_state(self):
        self._shown = None  # a fresh engine: read the rating again
        self._on_track()

    def _on_track(self):
        """A new item: unloved until the engine's rating() answers (one read per item);
        the same item again (re-created at another queue index, or after the gap between
        queues) keeps what the heart shows."""
        target = self.target()
        if target is not None and target == self._shown:
            return
        self._shown = target
        self.show(False)
        self.button.set_sensitive(target is not None)
        self._cancel_read()
        engine = self._app.engine if self._app is not None else None
        if (target is not None and engine is not None and not self._app.demo
                and engine.state == 'up' and engine.authorized):
            self._reading = self._app.spawn(self._read(target))

    def _cancel_read(self):
        if self._reading is not None and not self._reading.done():
            self._reading.cancel()
        self._reading = None

    async def _read(self, target):
        try:
            await self._app.engine.rating(*target)  # answered through `rated`
        except EngineError as error:
            log.debug('rating of the item playing: %s', error)

    def _on_rated(self, _engine, kind, item_id, value):
        if (kind, item_id) == self.target():
            self.show(value == 1)

    def show(self, loved):
        self._syncing = True
        try:
            self.button.set_active(loved)
        finally:
            self._syncing = False
        self.button.set_icon_name('heart-filled-symbolic' if loved else 'heart-outline-symbolic')
        self.button.set_tooltip_text(_('Remove from Favourites') if loved else _('Favourite'))

    def _on_toggled(self, button):
        """A click loves or unloves the item playing. The click is what counts: a rating
        read still out for the item is dropped, and a failure puts the heart back only
        while the item is still the one clicked."""
        if self._syncing:
            return
        target = self.target()
        if target is None or self._app is None:
            return
        self._cancel_read()
        loved = button.get_active()
        self.show(loved)
        engine = self._app.engine
        coro = engine.love(*target) if loved else engine.unlove(*target)

        def put_back():
            if self.target() == target:
                self.show(not loved)

        self._app.player_command(coro, put_back)


class RemoteCover:
    """An AppleMusicCover showing the item playing's artwork, fetched at the cover size."""

    def __init__(self, cover):
        self.cover = cover
        self._app = None
        self._url = None  # the artwork URL being shown or fetched

    def attach(self, player, app):
        self._app = app
        player.connect('notify::track', lambda *_: self._on_track(player.track))
        self._on_track(player.track)

    def _on_track(self, track):
        self.show(track.artwork_url if track is not None else None)

    def show(self, url):
        if url == self._url:
            return
        self._url = url
        self.cover.set_paths()
        if url and self._app is not None:
            self._app.spawn(self._fetch(url))

    async def _fetch(self, url):
        path = await fetch_remote(url, config.COVER_SIZE)
        if path and self._url == url:
            self.cover.set_paths(path)
