"""The Player: what is playing, as GObject properties fed by the engine's MusicKit events, and
the playback commands as thin coroutines over the engine.

    player = Player(app)                       # made once, in Application.do_startup
    player.state       # a MusicKit.PlaybackStates name: 'none', 'loading', 'playing', 'paused',
                       # 'stopped', 'ended', 'seeking', 'waiting', 'stalled', 'completed'
    player.track       # a NowPlaying (id, catalog_id, title, artist, album, duration_ms,
                       # artwork_url, index, explicit), or None
    player.position, player.duration           # seconds, floats
    player.shuffle (bool), player.repeat ('none', 'one', 'all'), player.volume (0 to 1)
    player.position_updated_at                 # time.monotonic() when position last arrived
    player.estimated_position()                # position, plus the time since while playing
    await player.play({'kind': 'album', 'id': …}, start_with=2, shuffle=False)
    await player.toggle()       # pause while active (playing, loading…), else play
    await player.pause() / resume() / next() / previous() / stop()
    await player.seek(seconds); await player.set_volume(level)
    await player.set_shuffle(True) / toggle_shuffle(); set_repeat('all') / cycle_repeat()
    await player.play_next(kind, id) / play_later(kind, id)

The properties change only from the engine's events (playbackStateDidChange,
nowPlayingItemDidChange, playbackTimeDidChange, playbackDurationDidChange,
shuffleModeDidChange, repeatModeDidChange, playbackVolumeDidChange), plus one now_playing()
read when the engine comes up, so the bar follows what MusicKit does rather than what was
asked; nothing here polls. When the engine goes down everything resets to nothing playing.
The commands raise EngineError as the engine does; play() starts a down engine first when the
account is signed in (a toast says so) and raises EngineError('not-signed-in') when it is not,
which the window turns into the sign-in flow. `error(message)` is emitted for MusicKit's
mediaPlaybackError. GObject only, no GTK: tests feed it synthetic events.
"""

import logging
import time
from gettext import gettext as _

from gi.repository import GObject

from .backend.errors import EngineError

log = logging.getLogger(__name__)

PLAYBACK_STATES = ('none', 'loading', 'playing', 'paused', 'stopped', 'ended', 'seeking',
                   'waiting', 'stalled', 'completed')

# The states in which playback is under way (the bar shows Pause): what MusicKit is doing on
# the way to, or in, playing. 'seeking' is a transient of either playing or paused.
ACTIVE_STATES = ('playing', 'loading', 'waiting', 'stalled')

REPEAT_MODES = ('none', 'one', 'all')

# The events the Player takes its state from, and the handler for each.
EVENTS = ('playbackStateDidChange', 'nowPlayingItemDidChange', 'playbackTimeDidChange',
          'playbackDurationDidChange', 'shuffleModeDidChange', 'repeatModeDidChange',
          'playbackVolumeDidChange', 'mediaPlaybackError')


def _text(value):
    return value if isinstance(value, str) else '' if value is None else str(value)


def _number(value, default=0.0):
    """A float from JSON, None and anything odd (NaN, a bool) being `default`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if value == value else default


class NowPlaying(GObject.Object):
    """The item playing: the bridge's Track shape (formatTrack in bridge.js), as properties.

    `artwork_url` is the artwork URL the bridge gives (256 px; Artwork.fetch_remote re-sizes
    it), None without artwork. `duration_ms` is Apple's for the item; the Player's `duration`
    is what MusicKit reports while playing, which is what the seek bar follows.
    """

    __gtype_name__ = 'AppleMusicNowPlaying'

    id = GObject.Property(type=str, default='')
    catalog_id = GObject.Property(type=str, default='')
    title = GObject.Property(type=str, default='')
    artist = GObject.Property(type=str, default='')
    album = GObject.Property(type=str, default='')
    duration_ms = GObject.Property(type=int, default=0)
    artwork_url = GObject.Property(type=str, default=None)
    index = GObject.Property(type=int, default=0)
    explicit = GObject.Property(type=bool, default=False)

    def __init__(self, data):
        data = data if isinstance(data, dict) else {}
        super().__init__(
            id=_text(data.get('id')),
            catalog_id=_text(data.get('catalogId')),
            title=_text(data.get('title')),
            artist=_text(data.get('artist')),
            album=_text(data.get('album')),
            duration_ms=int(_number(data.get('durationMs'))),
            artwork_url=data.get('artUrl') if isinstance(data.get('artUrl'), str) else None,
            index=int(_number(data.get('index'))),
            explicit=bool(data.get('explicit')),
        )
        self.raw = data

    def same_as(self, other):
        """Whether `other` is this item at the same queue position (a repeat of one song is
        a new play of the same item)."""
        return (other is not None and other.id == self.id and other.index == self.index)


class Player(GObject.Object):
    """The playback state and commands. See the module."""

    __gtype_name__ = 'AppleMusicPlayer'

    __gsignals__ = {
        'error': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    state = GObject.Property(type=str, default='none')
    track = GObject.Property(type=NowPlaying, default=None)
    position = GObject.Property(type=float, default=0.0)
    duration = GObject.Property(type=float, default=0.0)
    shuffle = GObject.Property(type=bool, default=False)
    repeat = GObject.Property(type=str, default='none')
    volume = GObject.Property(type=float, default=1.0)

    def __init__(self, app):
        """`app` gives the engine (`app.engine`), the settings (`signed-in`), `toast()`,
        `spawn()` and `demo`; a test passes a stand-in with those."""
        super().__init__()
        self._app = app
        self._engine = app.engine
        self.position_updated_at = time.monotonic()
        self._engine.connect('event', self._on_event)
        self._engine.connect('notify::state', self._on_engine_state)
        if self._engine.state == 'up':
            self._app.spawn(self.refresh())

    # -- state from the engine -------------------------------------------------------------

    def _on_engine_state(self, engine, _pspec):
        if engine.state == 'up':
            self._app.spawn(self.refresh())
        elif engine.state == 'down':
            self.apply(None)

    async def refresh(self):
        """Read the engine's now_playing() once (as the engine comes up: whatever it was
        doing before, or nothing) and apply it. Errors are logged: events will tell."""
        try:
            answer = await self._engine.now_playing()
        except EngineError as error:
            log.debug('now playing: %s', error)
            return
        self.apply(answer)

    def apply(self, now_playing):
        """Set everything from a now-playing answer ({state, track, position, duration,
        shuffle, repeat, volume}; a key left out is left alone), or reset to nothing
        playing (None: the state, track and times; shuffle, repeat and the volume are
        Apple's page's, kept across engine restarts, and the next refresh() reads them)."""
        if not isinstance(now_playing, dict):
            self._set_track(None)
            self._set_state('none')
            self._set_position(0.0, 0.0)
            return
        data = now_playing
        if 'track' in data:
            self._set_track(data.get('track'))
        if 'state' in data:
            self._set_state(data.get('state'))
        if 'position' in data or 'duration' in data:
            self._set_position(_number(data.get('position', self.position)),
                               _number(data.get('duration', self.duration)))
        if 'shuffle' in data:
            self._set_shuffle(data.get('shuffle'))
        if 'repeat' in data:
            self._set_repeat(data.get('repeat'))
        if 'volume' in data:
            self._set_volume(data.get('volume'))

    def _on_event(self, _engine, name, data):
        if name not in EVENTS:
            return
        data = data if isinstance(data, dict) else {}
        if 'error' in data and len(data) == 1:
            log.warning('%s: the bridge could not read the state: %s', name, data['error'])
            return
        log.debug('event %s: %s', name, data if name != 'nowPlayingItemDidChange'
                  else {'index': data.get('index'), 'track': bool(data.get('track'))})
        if name == 'playbackStateDidChange':
            self._set_state(data.get('state'))
            if 'position' in data:
                self._set_position(_number(data.get('position')), _number(data.get('duration')))
        elif name == 'nowPlayingItemDidChange':
            self._set_track(data.get('track'))
        elif name == 'playbackTimeDidChange':
            self._set_position(_number(data.get('position')), _number(data.get('duration')))
        elif name == 'playbackDurationDidChange':
            self._set_duration(_number(data.get('duration')))
        elif name == 'shuffleModeDidChange':
            self._set_shuffle(data.get('shuffle'))
        elif name == 'repeatModeDidChange':
            self._set_repeat(data.get('repeat'))
        elif name == 'playbackVolumeDidChange':
            self._set_volume(data.get('volume'))
        elif name == 'mediaPlaybackError':
            message = _text(data.get('message')) or _('Playback failed')
            log.warning('playback error: %s', message)
            self.emit('error', message)

    def _set_state(self, state):
        state = state if isinstance(state, str) and state else 'none'
        if state not in PLAYBACK_STATES:
            log.debug('unknown playback state %r', state)
        if state != self.state:
            self.state = state

    def _set_track(self, data):
        if isinstance(data, dict):
            track = NowPlaying(data)
            current = self.track
            if current is not None and current.same_as(track) and current.raw == data:
                return
            # A new item starts from the top; the time events correct this at once.
            self.track = track
            self._set_position(0.0, track.duration_ms / 1000 if track.duration_ms else None)
        elif self.track is not None:
            self.track = None
            self._set_position(0.0, 0.0)

    def _set_position(self, position, duration=None):
        position = max(0.0, position)
        self.position_updated_at = time.monotonic()
        if position != self.position:
            self.position = position
        if duration is not None:
            self._set_duration(duration)

    def _set_duration(self, duration):
        duration = max(0.0, duration)
        if duration != self.duration:
            self.duration = duration

    def _set_shuffle(self, value):
        shuffle = value == 'on' if isinstance(value, str) else bool(value)
        if shuffle != self.shuffle:
            self.shuffle = shuffle

    def _set_repeat(self, value):
        repeat = value if value in REPEAT_MODES else 'none'
        if repeat != self.repeat:
            self.repeat = repeat

    def _set_volume(self, value):
        volume = min(1.0, max(0.0, _number(value, 1.0)))
        if volume != self.volume:
            self.volume = volume

    # -- derived -------------------------------------------------------------------------

    @property
    def active(self):
        """Whether playback is under way (ACTIVE_STATES): the bar shows Pause."""
        return self.state in ACTIVE_STATES

    def estimated_position(self):
        """The position now: the last one reported, plus the time since while playing
        (MPRIS asks between events), never past the duration when that is known."""
        position = self.position
        if self.state == 'playing':
            position += time.monotonic() - self.position_updated_at
            if self.duration:
                position = min(position, self.duration)
        return position

    # -- commands ------------------------------------------------------------------------

    async def _ensure_engine(self):
        """The engine up for a play request: started first when it is down and the account
        is signed in (a toast meanwhile); EngineError('not-signed-in') when it is not."""
        if self._app.demo:
            raise EngineError('engine-down', 'no engine with the demo library')
        if self._engine.state != 'down':
            return
        if not self._app.settings.get_boolean('signed-in'):
            raise EngineError('not-signed-in', 'sign in to Apple Music to play')
        self._app.toast(_('Starting playback engine…'))
        await self._engine.start()

    async def play(self, play, start_with=None, shuffle=False):
        """Play what `play` names ({kind, id}: an Item's or a Group's play target), from its
        entry at queue position `start_with`, or shuffled."""
        kind = play.get('kind') if isinstance(play, dict) else None
        item_id = play.get('id') if isinstance(play, dict) else None
        if not kind or item_id in (None, ''):
            raise EngineError('usage', 'nothing to play')
        await self._ensure_engine()
        log.info('play %s %s%s%s', kind, item_id,
                 f' from {start_with}' if start_with is not None else '',
                 ' shuffled' if shuffle else '')
        await self._engine.play(kind, item_id, start_with=start_with, shuffle=shuffle)

    async def play_next(self, kind, item_id):
        await self._ensure_engine()
        await self._engine.play_next(kind, item_id)

    async def play_later(self, kind, item_id):
        await self._ensure_engine()
        await self._engine.play_later(kind, item_id)

    async def toggle(self):
        """Pause when playback is under way (ACTIVE_STATES: what the bar's button shows),
        play otherwise. Decided here, not by the bridge's toggle: MusicKit's isPlaying is
        false while an item loads, so a toggle then would ask it to play again."""
        await self._engine.control('pause' if self.active else 'play')

    async def pause(self):
        await self._engine.control('pause')

    async def resume(self):
        await self._engine.control('play')

    async def next(self):
        await self._engine.control('next')

    async def previous(self):
        await self._engine.control('previous')

    async def stop(self):
        await self._engine.control('stop')

    async def seek(self, seconds):
        await self._engine.seek(seconds)

    async def set_volume(self, level):
        return await self._engine.volume(level)

    async def set_shuffle(self, on):
        return await self._engine.shuffle('on' if on else 'off')

    async def toggle_shuffle(self):
        return await self._engine.shuffle('toggle')

    async def set_repeat(self, mode):
        return await self._engine.repeat(mode)

    async def cycle_repeat(self):
        return await self._engine.repeat('cycle')

    def next_repeat(self):
        """The repeat mode after this one in the cycle none → one → all → none."""
        position = REPEAT_MODES.index(self.repeat) if self.repeat in REPEAT_MODES else 0
        return REPEAT_MODES[(position + 1) % len(REPEAT_MODES)]


def format_time(seconds, remaining=False):
    """Seconds as m:ss (h:mm:ss past an hour), the remaining time with a leading minus."""
    total = max(0, int(_number(seconds) + 0.5))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        text = f'{hours}:{minutes:02d}:{secs:02d}'
    else:
        text = f'{minutes}:{secs:02d}'
    return f'-{text}' if remaining else text
