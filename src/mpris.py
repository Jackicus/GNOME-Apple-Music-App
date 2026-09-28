"""MPRIS: the app as a media player on the session bus, so GNOME Shell's media controls and
the media keys reach it as itself (the plan's decision: the app owns the service, Chrome's own
is disabled by --disable-features=HardwareMediaKeyHandling).

    mpris = Mpris(app)          # in Application.do_startup, once app.player exists
    mpris.start()               # owns org.mpris.MediaPlayer2.<application id>
    mpris.stop()                # in do_shutdown: the object unregistered, the name released

/org/mpris/MediaPlayer2 implements org.mpris.MediaPlayer2 (Identity, DesktopEntry, Raise →
the window presented, Quit → app.quit) and org.mpris.MediaPlayer2.Player, all of it read from
the Player: PlaybackStatus from `state` (the active states are Playing, paused is Paused,
seeking keeps the status before it, the rest Stopped; no track is Stopped), LoopStatus and
Shuffle from `repeat` and `shuffle`, Volume, Position as int64 microseconds from
`estimated_position()` (the last position plus the time since while playing, so nothing polls),
Metadata from `track` (mpris:trackid an object path made of the id, mpris:length, mpris:artUrl
the file:// URL of the cached remote art once Artwork.fetch_remote has it, xesam:title,
xesam:artist as a list, xesam:album), CanGoNext/CanGoPrevious/CanPlay/CanPause/CanSeek true
while a track exists (CanPlay is what GNOME Shell shows the player by, so "Not Playing" shows
nothing). The methods are the Player's commands, spawned through app.player_command (an
EngineError is toasted); setting LoopStatus, Shuffle and Volume the same; OpenUri does nothing.

PropertiesChanged is emitted from the Player's notify signals with only the keys whose value
differs from what was last put on the bus; Seeked after a seek asked for here, and when a
position arrives further than SEEK_JUMP seconds from where it should have been (a seek from
the bar or from Apple's page), except in the moments after a track change, when MusicKit
reports the previous item's position once more and then the new one's 0. Losing the name
(another owner, no bus) is logged and the app runs on without media controls.
"""

import logging
import os
import re
import time
from gettext import gettext as _

from gi.repository import Gio, GLib

from .player import ACTIVE_STATES, REPEAT_MODES
from .widgets import artwork

log = logging.getLogger(__name__)

OBJECT_PATH = '/org/mpris/MediaPlayer2'
ROOT_INTERFACE = 'org.mpris.MediaPlayer2'
PLAYER_INTERFACE = 'org.mpris.MediaPlayer2.Player'
PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'

# Where a track's object path lives; the spec's path for "no track" is never sent, since
# without a track the Metadata is empty.
TRACK_PATH_PREFIX = '/io/github/jackicus/AppleMusic/track/'

SEEK_JUMP = 2.0    # seconds a position may land from where it was expected before it is a seek
SEEK_HOLD = 1.5    # seconds after a seek asked for here during which stale positions are ignored
TRACK_HOLD = 2.0   # the same after a track change: MusicKit reports the previous item's
                   # position once more with the state transitions, then the new item's 0

# LoopStatus values by the Player's repeat modes, and back.
LOOP_STATUS = {'none': 'None', 'one': 'Track', 'all': 'Playlist'}
REPEAT_MODE = {status: mode for mode, status in LOOP_STATUS.items()}

INTROSPECTION_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="Fullscreen" type="b" access="readwrite"/>
    <property name="CanSetFullscreen" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" name="Offset" type="x"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri">
      <arg direction="in" name="Uri" type="s"/>
    </method>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read">
      <annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="false"/>
    </property>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read">
      <annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="false"/>
    </property>
  </interface>
</node>
"""


def bus_name(app_id):
    """The MPRIS bus name of an application id."""
    return f'org.mpris.MediaPlayer2.{app_id}'


def track_path(track_id):
    """An object path for a track id: under TRACK_PATH_PREFIX, every character an object path
    does not allow (anything but letters, digits and underscores, the underscore included so
    the escape is unambiguous) as `_` and two hex digits; an empty id is `_`."""
    escaped = re.sub(r'[^A-Za-z0-9]', lambda match: f'_{ord(match.group()):02x}',
                     str(track_id or ''))
    return TRACK_PATH_PREFIX + (escaped or '_')


def playback_status(state, current='Stopped'):
    """The MPRIS PlaybackStatus for a Player state: the active states (playing, loading,
    waiting, stalled, what the bar shows Pause for) are Playing, paused is Paused, seeking
    keeps `current` (it is a transient of either), and the rest (none, stopped, ended,
    completed) Stopped."""
    if state in ACTIVE_STATES:
        return 'Playing'
    if state == 'paused':
        return 'Paused'
    if state == 'seeking':
        return current
    return 'Stopped'


def loop_status(repeat):
    """The LoopStatus for a Player repeat mode (an unknown one is None)."""
    return LOOP_STATUS.get(repeat, 'None')


def repeat_mode(status):
    """The Player repeat mode a LoopStatus asks for, or None for one MPRIS does not name."""
    mode = REPEAT_MODE.get(status)
    return mode if mode in REPEAT_MODES else None


def art_url(path):
    """The file:// URL of an artwork file on disk, or None without one."""
    if not path:
        return None
    return GLib.filename_to_uri(os.path.abspath(path), None)


def metadata(track, art_path=None, duration=None):
    """The Metadata dict (name → GLib.Variant) for a NowPlaying, or {} for None. `art_path` is
    the track's cached artwork file, once it is on disk. The length is the track's own
    duration_ms; `duration` (seconds, what MusicKit reports) stands in for a track without
    one, so a track change never carries the previous item's length."""
    if track is None:
        return {}
    data = {'mpris:trackid': GLib.Variant('o', track_path(track.id))}
    length = track.duration_ms * 1000 if track.duration_ms else (duration or 0) * 1_000_000
    if length > 0:
        data['mpris:length'] = GLib.Variant('x', int(length))
    url = art_url(art_path)
    if url:
        data['mpris:artUrl'] = GLib.Variant('s', url)
    if track.title:
        data['xesam:title'] = GLib.Variant('s', track.title)
    if track.artist:
        data['xesam:artist'] = GLib.Variant('as', [track.artist])
    if track.album:
        data['xesam:album'] = GLib.Variant('s', track.album)
    return data


def microseconds(seconds):
    return int(round(max(0.0, float(seconds)) * 1_000_000))


class Mpris:
    """The MPRIS service over the app's Player. See the module."""

    def __init__(self, app, player=None):
        """`app` gives the application id, the window (get_active_window), `activate()`,
        `activate_action('quit')`, `spawn()` and `player_command()`; a test passes a stand-in
        with those and its own `player`."""
        self._app = app
        self._player = player if player is not None else app.player
        self._node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self._owner = 0
        self._connection = None
        self._registrations = []
        self._handlers = []
        self._sent = {}          # property name → the Variant last put on the bus
        self._status = 'Stopped'  # the PlaybackStatus last computed (seeking keeps it)
        self._art_path = None    # the cached artwork file of the track shown, once on disk
        self._art_task = None
        self._duration_known = True  # the Player's duration is this track's, not the last one's
        self._hold_until = 0.0   # until when positions are not looked at for a seek
        self._noted = (0.0, time.monotonic(), False)  # position, when, running: for Seeked

    # -- lifecycle -----------------------------------------------------------------------

    def start(self):
        """Own the bus name and follow the Player. The object is registered when the bus
        connection is there (before the name is granted, as Gio asks)."""
        name = bus_name(self._app.get_application_id())
        player = self._player
        self._handlers = [
            player.connect('notify::state', self._on_state),
            player.connect('notify::track', self._on_track),
            player.connect('notify::position', self._on_position),
            player.connect('notify::duration', self._on_duration),
            player.connect('notify::shuffle', self._on_shuffle),
            player.connect('notify::repeat', self._on_repeat),
            player.connect('notify::volume', self._on_volume),
        ]
        self._status = self._playback_status()
        self._art_path = self._cached_art(player.track)
        self._note()
        self._owner = Gio.bus_own_name(
            Gio.BusType.SESSION, name, Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired, self._on_name_acquired, self._on_name_lost)
        log.debug('mpris: owning %s', name)

    def stop(self):
        """Release the name and unregister the object; the Player is no longer followed."""
        for handler in self._handlers:
            self._player.disconnect(handler)
        self._handlers = []
        if self._art_task is not None and not self._art_task.done():
            self._art_task.cancel()
        self._art_task = None
        self._unregister()
        if self._owner:
            Gio.bus_unown_name(self._owner)
            self._owner = 0

    def _on_bus_acquired(self, connection, name):
        self._connection = connection
        # What a client reads now is what was "last sent": only changes from here signal.
        self._sent = {name: value for name, value in self._player_properties().items()
                      if name != 'Position'}
        for interface in self._node.interfaces:
            self._registrations.append(connection.register_object_with_closures2(
                OBJECT_PATH, interface, self._on_method_call, self._on_get_property,
                self._on_set_property))

    def _on_name_acquired(self, connection, name):
        log.info('mpris: %s', name)

    def _on_name_lost(self, connection, name):
        """The name refused (another owner) or the bus gone: media controls are off, the
        app runs on. The object stays registered: the name may come back."""
        if connection is None:
            log.warning('mpris: no session bus; media controls are unavailable')
        else:
            log.warning('mpris: %s is owned elsewhere; media controls go there', name)

    def _unregister(self):
        if self._connection is not None:
            for registration in self._registrations:
                self._connection.unregister_object(registration)
        self._registrations = []
        self._connection = None

    @property
    def connected(self):
        """Whether the object is on the bus (a test asks)."""
        return self._connection is not None

    # -- what the Player says --------------------------------------------------------------

    def _playback_status(self):
        if self._player.track is None:
            return 'Stopped'
        return playback_status(self._player.state, self._status)

    def _metadata(self):
        """The Metadata now. The Player's duration stands in for a track without a length
        of its own only once a duration has arrived for this track: from the track's notify
        until then it is still the previous item's."""
        return metadata(self._player.track, self._art_path,
                        self._player.duration if self._duration_known else None)

    def _track_path(self):
        track = self._player.track
        return track_path(track.id) if track is not None else None

    def _position(self):
        return microseconds(self._player.estimated_position()) if self._player.track else 0

    def _can(self):
        return self._player.track is not None

    def _root_properties(self):
        return {
            'CanQuit': GLib.Variant('b', True),
            'Fullscreen': GLib.Variant('b', False),
            'CanSetFullscreen': GLib.Variant('b', False),
            'CanRaise': GLib.Variant('b', True),
            'HasTrackList': GLib.Variant('b', False),
            'Identity': GLib.Variant('s', _('Apple Music')),
            'DesktopEntry': GLib.Variant('s', self._app.get_application_id()),
            'SupportedUriSchemes': GLib.Variant('as', []),
            'SupportedMimeTypes': GLib.Variant('as', []),
        }

    def _player_properties(self):
        can = self._can()
        return {
            'PlaybackStatus': GLib.Variant('s', self._status),
            'LoopStatus': GLib.Variant('s', loop_status(self._player.repeat)),
            'Rate': GLib.Variant('d', 1.0),
            'Shuffle': GLib.Variant('b', bool(self._player.shuffle)),
            'Metadata': GLib.Variant('a{sv}', self._metadata()),
            'Volume': GLib.Variant('d', float(self._player.volume)),
            'Position': GLib.Variant('x', self._position()),
            'MinimumRate': GLib.Variant('d', 1.0),
            'MaximumRate': GLib.Variant('d', 1.0),
            'CanGoNext': GLib.Variant('b', can),
            'CanGoPrevious': GLib.Variant('b', can),
            'CanPlay': GLib.Variant('b', can),
            'CanPause': GLib.Variant('b', can),
            'CanSeek': GLib.Variant('b', can),
            'CanControl': GLib.Variant('b', True),
        }

    def properties(self, interface):
        """Every property of an interface, name → Variant (what GetAll answers)."""
        if interface == ROOT_INTERFACE:
            return self._root_properties()
        if interface == PLAYER_INTERFACE:
            return self._player_properties()
        return {}

    # -- following the Player ------------------------------------------------------------

    def _changed(self, *names):
        """PropertiesChanged for the Player properties named, those whose value differs from
        what was last put on the bus (Position never: the spec says it does not signal)."""
        values = self._player_properties()
        changed = {}
        for name in names:
            value = values[name]
            sent = self._sent.get(name)
            if sent is None or not sent.equal(value):
                changed[name] = value
        if not changed:
            return
        self._sent.update(changed)
        self._emit(PROPERTIES_INTERFACE, 'PropertiesChanged',
                   GLib.Variant('(sa{sv}as)', (PLAYER_INTERFACE, changed, [])))

    def _emit(self, interface, signal, parameters):
        if self._connection is None:
            return
        try:
            self._connection.emit_signal(None, OBJECT_PATH, interface, signal, parameters)
        except GLib.Error as error:
            log.debug('mpris: %s not emitted: %s', signal, error.message)

    def _on_state(self, *_args):
        self._status = self._playback_status()
        self._note()  # the position runs on, or stops, from here
        self._changed('PlaybackStatus')

    def _on_track(self, *_args):
        self._follow_art()
        self._status = self._playback_status()
        self._duration_known = False  # the Player resets it after this notify, or MusicKit does
        self._hold_until = time.monotonic() + TRACK_HOLD  # the reset to 0 is not a seek
        self._note(0.0)
        self._changed('Metadata', 'PlaybackStatus', 'CanGoNext', 'CanGoPrevious', 'CanPlay',
                      'CanPause', 'CanSeek')

    def _on_duration(self, *_args):
        self._duration_known = True
        self._changed('Metadata')

    def _on_shuffle(self, *_args):
        self._changed('Shuffle')

    def _on_repeat(self, *_args):
        self._changed('LoopStatus')

    def _on_volume(self, *_args):
        self._changed('Volume')

    def _on_position(self, *_args):
        """A position from MusicKit: Seeked when it is not where the last one led (a seek
        from the bar or from Apple's page), except in the moments after a track change and
        after a seek asked for here (TRACK_HOLD, SEEK_HOLD), when stale ones arrive."""
        position = self._player.position
        expected = self._expected()
        self._note(position)
        if time.monotonic() < self._hold_until:
            return
        if abs(position - expected) > SEEK_JUMP:
            self._seeked(position)

    def _note(self, position=None):
        """Record where the position is now (`position`, or where the last record leads),
        and whether it runs on from here."""
        if position is None:
            position = self._expected()
        self._noted = (position, time.monotonic(), self._player.state == 'playing')

    def _expected(self):
        position, when, running = self._noted
        if running:
            position += time.monotonic() - when
        return position

    def _seeked(self, seconds):
        self._emit(PLAYER_INTERFACE, 'Seeked', GLib.Variant('(x)', (microseconds(seconds),)))

    def refresh_art(self):
        """The artwork file of the track shown may be gone (the cache was cleared, or the
        account signed out): look for it again, fetch it when it is missing, and put the
        Metadata on the bus again, without a file that is no longer there."""
        self._follow_art()
        self._changed('Metadata')

    def _follow_art(self):
        """The track's artwork file, when it is on disk; else a fetch of it, which puts the
        Metadata on the bus again once it arrives."""
        track = self._player.track
        if self._art_task is not None and not self._art_task.done():
            self._art_task.cancel()
        self._art_task = None
        self._art_path = self._cached_art(track)
        if track is not None and track.artwork_url and self._art_path is None:
            self._art_task = self._app.spawn(self._fetch_art(track))

    @staticmethod
    def _cached_art(track):
        """The remote-art file for a track's artwork, when it is on disk already."""
        if track is None or not track.artwork_url:
            return None
        path = artwork.remote_art_path(track.artwork_url)
        return path if path and os.path.isfile(path) else None

    async def _fetch_art(self, track):
        """The track's artwork into the remote-art cache (the bar asks for the same file, so
        one download serves both), then Metadata again with its URL, if the track is still
        the one playing."""
        try:
            path = await artwork.get_default().fetch_remote(track.artwork_url)
        except Exception:
            log.exception('mpris: fetching the artwork failed')
            return
        if path and self._player.track is track:
            self._art_path = path
            self._changed('Metadata')

    # -- D-Bus ---------------------------------------------------------------------------

    def _on_get_property(self, _connection, _sender, _path, interface, name):
        value = self.properties(interface).get(name)
        if value is None:
            log.warning('mpris: %s.%s asked for', interface, name)
        return value

    def _on_set_property(self, _connection, _sender, _path, interface, name, value):
        """LoopStatus, Shuffle and Volume ask the Player (its events then change the
        property); Rate and Fullscreen are taken and ignored (1.0 and false are all there
        is). True: the set was accepted."""
        if interface == PLAYER_INTERFACE:
            if name == 'LoopStatus':
                mode = repeat_mode(value.get_string())
                if mode is None:
                    log.debug('mpris: LoopStatus %r ignored', value.get_string())
                else:
                    self._command(self._player.set_repeat(mode))
            elif name == 'Shuffle':
                self._command(self._player.set_shuffle(value.get_boolean()))
            elif name == 'Volume':
                self._command(self._player.set_volume(min(1.0, max(0.0, value.get_double()))))
            elif name != 'Rate':
                return False
        elif interface != ROOT_INTERFACE or name != 'Fullscreen':
            return False
        return True

    def _on_method_call(self, _connection, _sender, _path, interface, method, parameters,
                        invocation):
        handler = self._methods().get((interface, method))
        if handler is None:
            invocation.return_dbus_error('org.freedesktop.DBus.Error.UnknownMethod',
                                         f'{interface}.{method} is not known')
            return
        try:
            handler(*parameters.unpack())
        except Exception:
            log.exception('mpris: %s failed', method)
            invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed',
                                         f'{method} failed')
            return
        invocation.return_value(None)

    def _methods(self):
        return {
            (ROOT_INTERFACE, 'Raise'): self.raise_window,
            (ROOT_INTERFACE, 'Quit'): self.quit,
            (PLAYER_INTERFACE, 'Next'): lambda: self._control(self._player.next),
            (PLAYER_INTERFACE, 'Previous'): lambda: self._control(self._player.previous),
            (PLAYER_INTERFACE, 'Pause'): lambda: self._control(self._player.pause),
            (PLAYER_INTERFACE, 'PlayPause'): lambda: self._control(self._player.toggle),
            (PLAYER_INTERFACE, 'Stop'): lambda: self._control(self._player.stop),
            (PLAYER_INTERFACE, 'Play'): lambda: self._control(self._player.resume),
            (PLAYER_INTERFACE, 'Seek'): self.seek,
            (PLAYER_INTERFACE, 'SetPosition'): self.set_position,
            (PLAYER_INTERFACE, 'OpenUri'): lambda _uri: None,
        }

    def _command(self, coro):
        return self._app.player_command(coro)

    def _control(self, command):
        """A transport command, when there is a track (CanPlay and the rest are false
        without one: the spec says a call then has no effect)."""
        if self._can():
            self._command(command())

    def raise_window(self):
        window = self._app.get_active_window()
        if window is not None:
            window.present()
        else:
            self._app.activate()

    def quit(self):
        self._app.activate_action('quit')

    def seek(self, offset):
        """Seek: `offset` microseconds from the position now; past the end is Next, before
        the start the start."""
        if self._can():
            self._seek_to(self._player.estimated_position() + offset / 1_000_000)

    def set_position(self, track_id, position):
        """SetPosition: to `position` microseconds, when `track_id` is the track playing
        (a stale call for the one before is dropped, as the spec asks)."""
        if self._can() and track_id == self._track_path():
            self._seek_to(position / 1_000_000)

    def _seek_to(self, seconds):
        seconds = max(0.0, seconds)
        duration = self._player.duration
        if duration and seconds >= duration:
            self._command(self._player.next())
            return
        self._command(self._seek_and_tell(seconds))

    async def _seek_and_tell(self, seconds):
        # Noted and held before the seek goes out: MusicKit's position at the target can
        # arrive while it is awaited, and must not count as a jump of its own.
        self._hold_until = time.monotonic() + SEEK_HOLD
        self._note(seconds)
        await self._player.seek(seconds)
        self._seeked(seconds)
