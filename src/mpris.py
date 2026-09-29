"""MPRIS: the app as a media player on the session bus, so GNOME Shell's media controls and
the media keys reach it as itself (the plan's decision: the app owns the service, Chrome's own
is disabled by --disable-features=HardwareMediaKeyHandling).

    mpris = Mpris(app)          # in Application.do_startup, once app.player exists
    mpris.start()               # owns org.mpris.MediaPlayer2.<application id>
    mpris.stop()                # in do_shutdown: the object unregistered, the name released

/org/mpris/MediaPlayer2 implements org.mpris.MediaPlayer2 (Identity, DesktopEntry, Raise →
the window presented, Quit → app.quit) and org.mpris.MediaPlayer2.Player, all of it read from
the Player: PlaybackStatus from `resting` (the active states are Playing, paused is Paused,
the rest Stopped; no track is Stopped; after Stop, Stopped until the music plays again or
the item changes), LoopStatus and Shuffle from `repeat` and `shuffle`, Volume, Position as
int64 microseconds from `estimated_position()` (the last position plus the time since while
playing, so nothing polls), Metadata from `track` (mpris:trackid an object path made of the
queue index and the id, so the same song twice in a row is two tracks; mpris:length the
track's own length, else MusicKit's; mpris:artUrl the file:// URL of the cached remote art
once remote.fetch_remote has it; xesam:title, xesam:artist as a list, xesam:album,
xesam:trackNumber and xesam:discNumber), CanGoNext/CanGoPrevious/CanPlay/CanPause/CanSeek
true while a track exists (CanPlay is what GNOME Shell shows the player by, so "Not Playing"
shows nothing). The methods are the Player's commands, spawned through app.player_command
(an EngineError is toasted): Stop pauses and returns to the start, keeping the item, as the
spec asks (Play then starts it again from the top); PlayPause with no track answers
NotSupported; Seek past the end is Next and before the start the start; SetPosition
ignores a stale trackid and a position outside the track. Setting LoopStatus, Shuffle and
Volume ask the Player; Rate 0 pauses; OpenUri does nothing.

PropertiesChanged is emitted from the Player's notify signals with only the keys whose value
differs from what was last put on the bus; Seeked after a seek asked for here, and when a
position arrives further than SEEK_JUMP seconds from where a client would have extrapolated
it (a seek from the bar or from Apple's page, or a stall the published status hid; the Player
has already dropped the previous item's position that MusicKit reports once more after a
track change). Losing the name (another owner, no bus) is logged and the app runs on without
media controls.
"""

import logging
import os
import re
import time
from gettext import gettext as _

from gi.repository import Gio, GLib

from . import remote
from .player import ACTIVE_STATES, REPEAT_MODES, SEEK_JUMP

log = logging.getLogger(__name__)

OBJECT_PATH = '/org/mpris/MediaPlayer2'
ROOT_INTERFACE = 'org.mpris.MediaPlayer2'
PLAYER_INTERFACE = 'org.mpris.MediaPlayer2.Player'
PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'
NOT_SUPPORTED_ERROR = 'org.freedesktop.DBus.Error.NotSupported'

# Where a track's object path lives; the spec's path for "no track" is never sent, since
# without a track the Metadata is empty.
TRACK_PATH_PREFIX = '/io/github/jackicus/AppleMusic/track/'

# Seconds after a seek asked for here during which stale positions are ignored (the Player
# drops the previous item's position after a track change itself; SEEK_JUMP, how far a
# position may land from where the last one led before it is a seek, is the Player's).
SEEK_HOLD = 1.5

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


class NotSupported(Exception):
    """A method call the spec says to refuse (PlayPause with nothing to play)."""


def bus_name(app_id):
    """The MPRIS bus name of an application id."""
    return f'org.mpris.MediaPlayer2.{app_id}'


def track_path(track_id, index=0):
    """An object path for a queue entry: under TRACK_PATH_PREFIX, its queue `index` (the
    same song twice in a row is two entries, which the spec wants told apart), then its id
    with every character an object path does not allow (anything but letters, digits and
    underscores, the underscore included so the escape is unambiguous) as `_` and two hex
    digits; an empty id is `_`."""
    escaped = re.sub(r'[^A-Za-z0-9]', lambda match: f'_{ord(match.group()):02x}',
                     str(track_id or ''))
    return f'{TRACK_PATH_PREFIX}{max(0, int(index or 0))}/{escaped or "_"}'


def playback_status(state):
    """The MPRIS PlaybackStatus for a Player state, given as `player.resting` (seeking seen
    through): the active states (playing, loading, waiting, stalled, what the bar shows
    Pause for) are Playing, paused is Paused, and the rest (none, stopped, ended, completed)
    Stopped."""
    if state in ACTIVE_STATES:
        return 'Playing'
    if state == 'paused':
        return 'Paused'
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


def track_length(track, duration=None):
    """A track's length in seconds: its own (Apple's duration_ms), else `duration` (what
    MusicKit reports, standing in for a track without one), else None when neither is
    known. What mpris:length publishes and what SetPosition and Seek measure against."""
    if track is None:
        return None
    if track.duration_ms:
        return track.duration_ms / 1000
    return duration if duration and duration > 0 else None


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def metadata(track, art_path=None, duration=None):
    """The Metadata dict (name → GLib.Variant) for a NowPlaying, or {} for None. `art_path` is
    the track's cached artwork file, once it is on disk; the length is track_length()'s; the
    track and disc numbers are the bridge's Track's, when it has them."""
    if track is None:
        return {}
    data = {'mpris:trackid': GLib.Variant('o', track_path(track.id, track.index))}
    length = track_length(track, duration)
    if length:
        data['mpris:length'] = GLib.Variant('x', int(round(length * 1_000_000)))
    url = art_url(art_path)
    if url:
        data['mpris:artUrl'] = GLib.Variant('s', url)
    if track.title:
        data['xesam:title'] = GLib.Variant('s', track.title)
    if track.artist:
        data['xesam:artist'] = GLib.Variant('as', [track.artist])
    if track.album:
        data['xesam:album'] = GLib.Variant('s', track.album)
    raw = track.raw if isinstance(track.raw, dict) else {}
    for key, name in (('trackNumber', 'xesam:trackNumber'), ('discNumber', 'xesam:discNumber')):
        if _positive_int(raw.get(key)):
            data[name] = GLib.Variant('i', raw[key])
    return data


def microseconds(seconds):
    return int(round(max(0.0, float(seconds)) * 1_000_000))


# The properties of each interface, name → a function of the service answering its Variant
# (Get builds only the one asked for, PropertiesChanged only the names given; module tables,
# so a service holds no closure over itself).
ROOT_GETTERS = {
    'CanQuit': lambda service: GLib.Variant('b', True),
    'Fullscreen': lambda service: GLib.Variant('b', False),
    'CanSetFullscreen': lambda service: GLib.Variant('b', False),
    'CanRaise': lambda service: GLib.Variant('b', True),
    'HasTrackList': lambda service: GLib.Variant('b', False),
    'Identity': lambda service: GLib.Variant('s', _('Apple Music')),
    'DesktopEntry': lambda service: GLib.Variant('s', service._app.get_application_id()),
    'SupportedUriSchemes': lambda service: GLib.Variant('as', []),
    'SupportedMimeTypes': lambda service: GLib.Variant('as', []),
}
PLAYER_GETTERS = {
    'PlaybackStatus': lambda service: GLib.Variant('s', service._status),
    'LoopStatus': lambda service: GLib.Variant('s', loop_status(service._player.repeat)),
    'Rate': lambda service: GLib.Variant('d', 1.0),
    'Shuffle': lambda service: GLib.Variant('b', bool(service._player.shuffle)),
    'Metadata': lambda service: GLib.Variant('a{sv}', service._metadata()),
    'Volume': lambda service: GLib.Variant('d', float(service._player.volume)),
    'Position': lambda service: GLib.Variant('x', service._position()),
    'MinimumRate': lambda service: GLib.Variant('d', 1.0),
    'MaximumRate': lambda service: GLib.Variant('d', 1.0),
    'CanGoNext': lambda service: GLib.Variant('b', service._can()),
    'CanGoPrevious': lambda service: GLib.Variant('b', service._can()),
    'CanPlay': lambda service: GLib.Variant('b', service._can()),
    'CanPause': lambda service: GLib.Variant('b', service._can()),
    'CanSeek': lambda service: GLib.Variant('b', service._can()),
    'CanControl': lambda service: GLib.Variant('b', True),
}
GETTERS = {ROOT_INTERFACE: ROOT_GETTERS, PLAYER_INTERFACE: PLAYER_GETTERS}


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
        self._status = 'Stopped'  # the PlaybackStatus last computed
        self._stopped = False    # Stop was called: Stopped until the music plays again
        self._shown = None       # the Player's track as last followed (_on_track)
        self._art_url = None     # the artwork URL the art path is for
        self._art_path = None    # the cached artwork file of the track shown, once on disk
        self._art_task = None
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
        self._shown = player.track
        self._follow_art()
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
        self._sent = {name: value for name, value in self.properties(PLAYER_INTERFACE).items()
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
        if self._stopped:
            return 'Stopped'
        return playback_status(self._player.resting)

    def _metadata(self):
        """The Metadata now: the Player's duration stands in for a track without a length
        of its own (the Player sets it with the track, so it is never the previous item's)."""
        return metadata(self._player.track, self._art_path, self._player.duration)

    def _length(self):
        """The length published for the track playing, in seconds, or None."""
        return track_length(self._player.track, self._player.duration)

    def _track_path(self):
        track = self._player.track
        return track_path(track.id, track.index) if track is not None else None

    def _position(self):
        return microseconds(self._player.estimated_position()) if self._player.track else 0

    def _can(self):
        return self._player.track is not None

    def properties(self, interface):
        """Every property of an interface, name → Variant (what GetAll answers)."""
        return {name: get(self) for name, get in GETTERS.get(interface, {}).items()}

    # -- following the Player ------------------------------------------------------------

    def _changed(self, *names):
        """PropertiesChanged for the Player properties named, those whose value differs from
        what was last put on the bus (Position never: the spec says it does not signal)."""
        changed = {}
        for name in names:
            value = PLAYER_GETTERS[name](self)
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
        if self._stopped and self._player.active:
            self._stopped = False  # the music plays again: Stop is over
        self._status = self._playback_status()
        self._note()  # the position runs on, or stops, from here
        self._changed('PlaybackStatus')

    def _on_track(self, *_args):
        """A new item (its times reset with it, whichever notify comes first: the
        position's and the duration's wait for this one)."""
        self._shown = self._player.track
        self._stopped = False
        self._follow_art()
        self._status = self._playback_status()
        self._note(0.0)
        self._changed('Metadata', 'PlaybackStatus', 'CanGoNext', 'CanGoPrevious', 'CanPlay',
                      'CanPause', 'CanSeek')

    def _on_duration(self, *_args):
        if self._player.track is self._shown:
            self._changed('Metadata')

    def _on_shuffle(self, *_args):
        self._changed('Shuffle')

    def _on_repeat(self, *_args):
        self._changed('LoopStatus')

    def _on_volume(self, *_args):
        self._changed('Volume')

    def _on_position(self, *_args):
        """A position from MusicKit: Seeked when it is not where a client would have it
        (a seek from the bar or from Apple's page; a stall behind a status that stayed
        Playing), except in the moments after a seek asked for here (SEEK_HOLD), when stale
        ones arrive."""
        if self._player.track is not self._shown:
            return  # a new item's 0: its notify follows, and notes it
        position = self._player.position
        expected = self._expected()
        self._note(position)
        if time.monotonic() < self._hold_until:
            return
        if abs(position - expected) > SEEK_JUMP:
            self._seeked(position)

    def _note(self, position=None):
        """Record where the position is now (`position`, or where the last record leads),
        and whether it runs on from here: while the status published is Playing, as a
        client extrapolates it (the spec: the position progresses at the Rate while
        Playing), so a stall behind that status shows as a Seeked once it ends."""
        if position is None:
            position = self._expected()
        self._noted = (position, time.monotonic(), self._status == 'Playing')

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
        self._follow_art(again=True)
        self._changed('Metadata')

    def _follow_art(self, again=False):
        """The track's artwork file: asked of remote.fetch_remote (which finds it on disk,
        off the main loop, or downloads it) in a task that puts the Metadata on the bus again
        with its URL. The file already found is kept for a track with the same artwork (the
        same song at another queue position), unless `again`."""
        track = self._player.track
        url = track.artwork_url if track is not None else None
        if url and url == self._art_url and not again:
            return
        if self._art_task is not None and not self._art_task.done():
            self._art_task.cancel()
        self._art_task = None
        self._art_url = url
        self._art_path = None
        if url:
            self._art_task = self._app.spawn(self._fetch_art(track))

    async def _fetch_art(self, track):
        """The track's artwork from the remote-art cache (the bar asks for the same file, so
        one download serves both), then Metadata again with its URL, if the track shown
        still has that artwork."""
        try:
            path = await remote.fetch_remote(track.artwork_url)
        except Exception:
            log.exception('mpris: fetching the artwork failed')
            return
        if path and self._art_url == track.artwork_url:
            self._art_path = path
            self._changed('Metadata')

    # -- D-Bus ---------------------------------------------------------------------------

    def _on_get_property(self, _connection, _sender, _path, interface, name):
        get = GETTERS.get(interface, {}).get(name)
        if get is None:
            log.warning('mpris: %s.%s asked for', interface, name)
            return None
        return get(self)

    def _on_set_property(self, _connection, _sender, _path, interface, name, value):
        """LoopStatus, Shuffle and Volume ask the Player (its events then change the
        property); Rate 0 pauses, as the spec asks, and any other rate is taken and ignored
        (1.0 is all there is); Fullscreen is taken and ignored. True: the set was
        accepted."""
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
            elif name == 'Rate':
                if value.get_double() == 0.0:
                    self._control(self._player.pause)
            else:
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
        except NotSupported as refusal:
            invocation.return_dbus_error(NOT_SUPPORTED_ERROR, str(refusal))
            return
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
            (PLAYER_INTERFACE, 'PlayPause'): lambda: self._control(self._player.toggle,
                                                                   required=True),
            (PLAYER_INTERFACE, 'Stop'): self.stop_playback,
            (PLAYER_INTERFACE, 'Play'): lambda: self._control(self._player.resume),
            (PLAYER_INTERFACE, 'Seek'): self.seek,
            (PLAYER_INTERFACE, 'SetPosition'): self.set_position,
            (PLAYER_INTERFACE, 'OpenUri'): lambda _uri: None,
        }

    def _command(self, coro):
        return self._app.player_command(coro)

    def _control(self, command, required=False):
        """A transport command, when there is a track (CanPlay and the rest are false
        without one: the spec says a call then has no effect, and for PlayPause, which is
        `required`, an error too)."""
        if self._can():
            self._command(command())
        elif required:
            raise NotSupported('nothing is playing')

    def raise_window(self):
        window = self._app.get_active_window()
        if window is not None:
            window.present()
        else:
            self._app.activate()

    def quit(self):
        self._app.activate_action('quit')

    def stop_playback(self):
        """Stop: pause and back to the start, the item kept, so the Shell keeps the player
        and Play starts it again from the top (the spec); Stopped is published until the
        music plays again or the item changes."""
        if not self._can():
            return
        self._stopped = True
        self._status = 'Stopped'
        self._note()
        self._changed('PlaybackStatus')
        self._command(self._pause_and_rewind())

    async def _pause_and_rewind(self):
        await self._player.pause()
        await self._seek_and_tell(0.0)

    def seek(self, offset):
        """Seek: `offset` microseconds from the position now; past the end is Next, before
        the start the start."""
        if not self._can():
            return
        seconds = max(0.0, self._player.estimated_position() + offset / 1_000_000)
        length = self._length()
        if length and seconds >= length:
            self._command(self._player.next())
            return
        self._command(self._seek_and_tell(seconds))

    def set_position(self, track_id, position):
        """SetPosition: to `position` microseconds, when `track_id` is the track playing
        and the position is within it (a stale call for the entry before, a negative
        position and one past the length are dropped, as the spec asks)."""
        if not self._can() or track_id != self._track_path() or position < 0:
            return
        seconds = position / 1_000_000
        length = self._length()
        if length and seconds > length:
            return
        self._command(self._seek_and_tell(seconds))

    async def _seek_and_tell(self, seconds):
        # Noted and held before the seek goes out: MusicKit's position at the target can
        # arrive while it is awaited, and must not count as a jump of its own.
        self._hold_until = time.monotonic() + SEEK_HOLD
        self._note(seconds)
        await self._player.seek(seconds)
        self._seeked(seconds)
