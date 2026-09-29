"""The Player: what is playing, as GObject properties fed by the engine's MusicKit events, and
the playback commands as thin coroutines over the engine.

    player = Player(app)                       # made once, in Application.do_startup
    player.state       # a MusicKit.PlaybackStates name: 'none', 'loading', 'playing', 'paused',
                       # 'stopped', 'ended', 'seeking', 'waiting', 'stalled', 'completed'
    player.resting     # the state with 'seeking' seen through (the one before the seek)
    player.active, player.stopped              # playback under way (the bar shows Pause);
                                               # nothing playing or paused (no item, or ended)
    player.track       # a NowPlaying (id, catalog_id, title, artist, album, duration_ms,
                       # artwork_url, index, explicit), or None
    player.position, player.duration           # seconds, floats
    player.shuffle (bool), player.repeat ('none', 'one', 'all'), player.volume (0 to 1)
    player.position_updated_at                 # time.monotonic() when position last changed
    player.estimated_position()                # position, plus the time since (a second at
                                               # most) while playing: between the events
    player.queue, player.queue_index           # a Gio.ListStore of NowPlaying (the queue's
                                               # entries, in order) and where the item playing
                                               # is in it (-1: nowhere, or nothing playing)
    player.lyrics, player.lyrics_loading       # a lyrics.Lyrics for the track (None: none, or
                                               # not there yet), and whether one is being read
    player.pending                             # a play request is with the engine (the play
                                               # buttons show a spinner)
    await player.play({'kind': 'album', 'id': …}, start_with=2, shuffle=False)  # or True,
                                               # or None: the mode as it is
    await player.queue_jump(3)  # play the queue's entry at index 3
    await player.toggle()       # pause while active (playing, loading…), else play
    await player.pause() / resume() / next() / stop()
    await player.previous()     # the item before, or this one again when a few seconds in
    await player.seek(seconds); await player.set_volume(level)
    await player.set_shuffle(True) / toggle_shuffle(); set_repeat('all') / cycle_repeat()
    await player.play_next(kind, id) / play_later(kind, id)
    await player.ensure_engine()  # start a down engine when signed in (the item actions)

The properties change only from the engine's events (playbackStateDidChange,
nowPlayingItemDidChange, playbackTimeDidChange, playbackDurationDidChange,
shuffleModeDidChange, repeatModeDidChange, playbackVolumeDidChange, queueItemsDidChange,
queuePositionDidChange), plus one now_playing() (and queue()) read when the engine comes up,
so the bar follows what MusicKit does rather than what was asked; nothing here polls. A
null item clears the track only after TRACK_GRACE_MS without a new one (`track_grace_ms`;
MusicKit sends a null between queues, right before the next item), and the previous item's
position, which MusicKit reports once more after a track change, is dropped here
(TRACK_HOLD), so every view sees the same cleaned position. The queue is read again
(queue()) when an item arrives that the queue held does not hold at its index; the lyrics
are asked of the engine (lyrics(), cached on disk) once per catalog song as it starts,
never on a timer. A state or track event that arrives while a read is on its way is newer
than the read's answer, which then sets only the modes and the volume. When the engine
goes down everything resets to nothing playing, at once; so it does when the page loads a
new document (the engine's `bridgeReset` event), and now_playing() is read again for what
the new one holds.
The commands raise EngineError as the engine does; play() starts a down engine first when the
account is signed in (a toast says so) and raises EngineError('not-signed-in') when it is not,
which the window turns into the sign-in flow. `error(message)` is emitted for MusicKit's
mediaPlaybackError. GObject only, no GTK: tests feed it synthetic events.
"""

import asyncio
import logging
import time
from gettext import gettext as _

from gi.repository import Gio, GLib, GObject

from .backend.errors import EngineError
from .lyrics import Lyrics

log = logging.getLogger(__name__)

# How long a null item (nowPlayingItemDidChange with no track) waits before the track is
# cleared: MusicKit sends one between queues, half a second before the next item, and the
# bar, the actions, the heart, the lyrics and the Shell's media controls would all flash
# "Not Playing" in between.
TRACK_GRACE_MS = 800

# After a track change, MusicKit reports the previous item's position (and duration) once
# more with the state transitions of the skip, before the new item's 0. For TRACK_HOLD
# seconds after a new item, a position further than SEEK_JUMP seconds past where the item
# can be is that stale report, and is dropped (with its duration), so the bar, the sheet,
# the lyrics and MPRIS never show it. SEEK_JUMP is also how far a position may land from
# where the last one led before MPRIS calls it a seek.
TRACK_HOLD = 2.0
SEEK_JUMP = 2.0

# Seconds into an item after which Previous restarts it rather than going to the item
# before, as players do (GNOME Music, Apple's own).
PREVIOUS_RESTART = 3.0

PLAYBACK_STATES = ('none', 'loading', 'playing', 'paused', 'stopped', 'ended', 'seeking',
                   'waiting', 'stalled', 'completed')

# The states in which playback is under way (the bar shows Pause): what MusicKit is doing on
# the way to, or in, playing. 'seeking' is a transient of whichever state came before it
# (MusicKit passes through it on every seek and every new queue): `resting` sees through it.
ACTIVE_STATES = ('playing', 'loading', 'waiting', 'stalled')

# The states in which nothing plays or waits to resume: the item finished, the queue ended,
# playback was stopped, or there never was any. Paused is not among them.
STOPPED_STATES = ('none', 'stopped', 'ended', 'completed')

REPEAT_MODES = ('none', 'one', 'all')

# The events the Player takes its state from (MusicKit's, and the engine's own bridgeReset:
# the page loaded a new document, and whatever played is gone with the old one).
EVENTS = ('playbackStateDidChange', 'nowPlayingItemDidChange', 'playbackTimeDidChange',
          'playbackDurationDidChange', 'shuffleModeDidChange', 'repeatModeDidChange',
          'playbackVolumeDidChange', 'mediaPlaybackError', 'queueItemsDidChange',
          'queuePositionDidChange', 'bridgeReset')


def _text(value):
    return value if isinstance(value, str) else '' if value is None else str(value)


def _number(value, default=0.0):
    """A float from JSON, None and anything odd (NaN, a bool) being `default`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if value == value else default


def _duration_of(data):
    """An event's duration, or None when it carries none (the one held is kept)."""
    return _number(data['duration']) if 'duration' in data else None


class NowPlaying(GObject.Object):
    """The item playing: the bridge's Track shape (formatTrack in bridge.js), as properties.

    `artwork_url` is the artwork URL the bridge gives (256 px; remote.fetch_remote re-sizes
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
    queue_index = GObject.Property(type=int, default=-1)
    lyrics = GObject.Property(type=Lyrics, default=None)
    lyrics_loading = GObject.Property(type=bool, default=False)
    pending = GObject.Property(type=bool, default=False)

    def __init__(self, app):
        """`app` gives the engine (`app.engine`), the settings (`signed-in`), `toast()`,
        `spawn()` and `demo`; a test passes a stand-in with those."""
        super().__init__()
        self._app = app
        self._engine = app.engine
        self._resting = 'none'  # the last state that was not 'seeking'
        self.track_grace_ms = TRACK_GRACE_MS  # 0: a null item clears the track at once
        self._clear_source = 0  # the GLib source waiting out the grace, while one does
        self._track_since = float('-inf')  # when the item playing started (_plausible)
        self._events = 0  # state and track events so far: a refresh() answer older than one
        self._queue_events = 0  # is not applied over it (the same for the queue's)
        self._play_lock = asyncio.Lock()  # one play request at a time, in order
        self._play_serial = 0  # the latest request's number: an older one waiting is dropped
        self.position_updated_at = time.monotonic()
        self.queue = Gio.ListStore(item_type=NowPlaying)
        self._lyrics_task = None  # the task reading the track's lyrics, while one runs
        self._lyrics_wanted = None  # the catalog id that task reads
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
        doing before, or nothing) and apply it; an item playing then has the queue read too
        (apply, through _track_queued) and its lyrics. A state or track event that arrived
        while the answer was on its way is newer than the answer, which then sets only the
        modes and the volume. Errors are logged: events will tell."""
        events = self._events
        try:
            answer = await self._engine.now_playing()
        except EngineError as error:
            log.debug('now playing: %s', error)
            return
        if self._events != events and isinstance(answer, dict):
            log.debug('now playing: events arrived meanwhile; the answer sets the modes only')
            answer = {key: answer[key] for key in ('shuffle', 'repeat', 'volume')
                      if key in answer}
        self.apply(answer)

    async def refresh_queue(self):
        """Read the engine's queue() and apply it, unless a queueItemsDidChange arrived
        meanwhile (the event is newer). Errors are logged."""
        events = self._queue_events
        try:
            answer = await self._engine.queue()
        except EngineError as error:
            log.debug('queue: %s', error)
            return
        if self._queue_events != events:
            log.debug('queue: an event arrived meanwhile; the answer is dropped')
            return
        self.apply_queue(answer)

    def apply(self, now_playing):
        """Set everything from a now-playing answer ({state, track, position, duration,
        shuffle, repeat, volume}; a key left out is left alone; a `queue` snapshot and a
        `lyrics` answer are taken too, the lyrics then not asked of the engine), or reset
        to nothing playing (None: the state, track, times, queue and lyrics; shuffle,
        repeat and the volume are Apple's page's, kept across engine restarts, and the
        next refresh() reads them)."""
        if not isinstance(now_playing, dict):
            self._cancel_clear()
            self._set_track(None)
            self._set_state('none')
            self._set_position(0.0, 0.0)
            self.apply_queue(None)
            return
        data = now_playing
        if 'queue' in data:  # before the track, which then finds itself in it
            self.apply_queue(data.get('queue'))
        if 'track' in data:
            self._set_track(data.get('track'), fetch_lyrics='lyrics' not in data)
        if 'lyrics' in data:
            self._set_lyrics(data.get('lyrics'))
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

    def apply_queue(self, snapshot):
        """Set the queue from a {index, items: [Track…]} snapshot (the bridge's queue()
        answer and the queueItemsDidChange event), or empty it (None). Entries showing the
        same items as before, in the same order, are kept (a repeated snapshot rebinds no
        rows); the index is the snapshot's, or the track's place when it says -1."""
        items = snapshot.get('items') if isinstance(snapshot, dict) else None
        items = items if isinstance(items, list) else []
        items = [item for item in items if isinstance(item, dict)]
        entries = [NowPlaying(dict(item, index=position)) for position, item in enumerate(items)]
        current = [entry.id for entry in self.queue]
        if [entry.id for entry in entries] != current:
            self.queue.splice(0, len(current), entries)
        index = snapshot.get('index') if isinstance(snapshot, dict) else None
        index = int(index) if isinstance(index, int) and not isinstance(index, bool) else -1
        if index < 0 and self.track is not None:
            index = self._index_of(self.track)
        self._set_queue_index(index)

    def _index_of(self, track):
        """Where `track` is in the queue: at its own index when the entry there is it, else
        the first entry with its id, else -1."""
        count = self.queue.get_n_items()
        if 0 <= track.index < count and self.queue.get_item(track.index).id == track.id:
            return track.index
        for position, entry in enumerate(self.queue):
            if entry.id == track.id:
                return position
        return -1

    def _set_queue_index(self, index):
        index = index if 0 <= index < self.queue.get_n_items() else -1
        if index != self.queue_index:
            self.queue_index = index

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
            self._events += 1
            self._set_state(data.get('state'))
            if 'position' in data:
                self._take_position(data)
        elif name == 'nowPlayingItemDidChange':
            self._events += 1
            if isinstance(data.get('track'), dict):
                self._set_track(data['track'])
            else:
                self._clear_track_soon()
        elif name == 'bridgeReset':
            # A new document in the page: nothing plays in it, and MusicKit there says
            # nothing of what the old one was doing. Read what it holds.
            self._events += 1
            self.apply(None)
            self._app.spawn(self.refresh())
        elif name == 'playbackTimeDidChange':
            self._take_position(data)
        elif name == 'playbackDurationDidChange':
            self._set_duration(_number(data.get('duration')))
        elif name == 'shuffleModeDidChange':
            self._set_shuffle(data.get('shuffle'))
        elif name == 'repeatModeDidChange':
            self._set_repeat(data.get('repeat'))
        elif name == 'playbackVolumeDidChange':
            self._set_volume(data.get('volume'))
        elif name == 'queueItemsDidChange':
            self._queue_events += 1
            self.apply_queue(data)
        elif name == 'queuePositionDidChange':
            index = data.get('index')
            self._set_queue_index(index if isinstance(index, int) else -1)
        elif name == 'mediaPlaybackError':
            message = _text(data.get('message')) or _('Playback failed')
            log.warning('playback error: %s', message)
            self.emit('error', message)

    def _take_position(self, data):
        """An event's position and duration, unless the position is the previous item's,
        reported once more after a track change (TRACK_HOLD, SEEK_JUMP)."""
        position = _number(data.get('position'))
        if not self._plausible(position):
            log.debug('position %.1f dropped: the item started %.1f s ago', position,
                      time.monotonic() - self._track_since)
            return
        self._set_position(position, _duration_of(data))

    def _plausible(self, position):
        """Whether `position` can be the item playing's: any position once TRACK_HOLD has
        passed since it started, else one within SEEK_JUMP of how far it can have got (from
        0, or from where an answer put it)."""
        elapsed = time.monotonic() - self._track_since
        return (elapsed >= TRACK_HOLD or position <= elapsed + SEEK_JUMP
                or abs(position - self.estimated_position()) <= SEEK_JUMP)

    def _set_state(self, state):
        state = state if isinstance(state, str) and state else 'none'
        if state not in PLAYBACK_STATES:
            log.debug('unknown playback state %r', state)
        if state != 'seeking':
            self._resting = state
        if state != self.state:
            self.position_updated_at = time.monotonic()  # the position runs on, or stops, from here
            self.state = state

    def _set_track(self, data, fetch_lyrics=True):
        """The item playing from an event's or an answer's Track (or None: nothing). The
        track, its position and its duration change together, under one freeze, so a
        `notify::track` handler reads the new item's times: 0 and Apple's length for it
        (0 without one; MusicKit's duration follows). The same song at another queue
        position (the queue reordered under it: a shuffle toggle) keeps its times."""
        if isinstance(data, dict):
            self._cancel_clear()
            track = NowPlaying(data)
            current = self.track
            if current is not None and current.same_as(track) and current.raw == data:
                return
            with self.freeze_notify():
                if current is None or current.id != track.id:
                    self._track_since = time.monotonic()
                    self._reset_times(track.duration_ms / 1000)
                self.track = track
            self._track_queued(track)
            if fetch_lyrics:
                self._want_lyrics(track)
        elif self.track is not None:
            with self.freeze_notify():
                self._reset_times(0.0)
                self.track = None
            self._set_queue_index(-1)
            self._cancel_lyrics()
            self._set_lyrics(None)

    def _clear_track_soon(self):
        """A null item: the track is cleared once track_grace_ms have passed without a new
        item (MusicKit's null between queues is followed by one), or at once without a
        grace. The engine going down (apply(None)) clears at once."""
        if self.track is None or self._clear_source:
            return
        if not self.track_grace_ms:
            self._set_track(None)
            return
        self._clear_source = GLib.timeout_add(self.track_grace_ms, self._on_grace_over)

    def _on_grace_over(self):
        self._clear_source = 0
        self._set_track(None)
        return GLib.SOURCE_REMOVE

    def _cancel_clear(self):
        if self._clear_source:
            GLib.source_remove(self._clear_source)
            self._clear_source = 0

    def _reset_times(self, duration):
        """Position 0 and `duration` for an item that starts (or none), as they are."""
        self.position_updated_at = time.monotonic()
        if self.position != 0.0:
            self.position = 0.0
        if duration != self.duration:
            self.duration = duration

    def _track_queued(self, track):
        """The item playing has changed: point the queue index at it, or read the queue
        again when the entries held do not include it (a new queue whose items event was
        missed, or came before the engine's connection)."""
        index = self._index_of(track)
        if index >= 0:
            self._set_queue_index(index)
        else:
            self._set_queue_index(-1)
            self._app.spawn(self.refresh_queue())

    # -- lyrics ------------------------------------------------------------------------------

    def _want_lyrics(self, track):
        """Lyrics for the item playing: kept when they are the song's already (the same
        song again, or repeat one), else asked of the engine as a task; none for an item
        without a catalog id (a station's ad, say)."""
        catalog_id = track.catalog_id
        if self.lyrics is not None and self.lyrics.catalog_id == catalog_id:
            return
        if self._lyrics_task is not None and not self._lyrics_task.done():
            if self._lyrics_wanted == catalog_id:
                return
        self._cancel_lyrics()
        self._set_lyrics(None)
        if not catalog_id:
            return
        self._set_lyrics_loading(True)
        self._lyrics_wanted = catalog_id
        self._lyrics_task = self._app.spawn(self._load_lyrics(catalog_id))

    def _cancel_lyrics(self):
        task, self._lyrics_task = self._lyrics_task, None
        self._lyrics_wanted = None
        if task is not None and not task.done():
            task.cancel()
        self._set_lyrics_loading(False)

    async def _load_lyrics(self, catalog_id):
        task = asyncio.current_task()
        answer = None
        try:
            answer = await self._engine.lyrics(catalog_id)
        except EngineError as error:
            log.debug('lyrics: %s', error)
        finally:
            if self._lyrics_task is task:
                self._lyrics_task = None
                self._lyrics_wanted = None
                self._set_lyrics_loading(False)
        if self.track is not None and self.track.catalog_id == catalog_id:
            self._set_lyrics(answer, catalog_id)

    def _set_lyrics(self, answer, catalog_id=None):
        """`answer` the engine's lyrics answer (no lines: no lyrics), or None."""
        if isinstance(answer, Lyrics):
            lyrics = answer
        elif isinstance(answer, dict) and answer.get('lines'):
            if catalog_id is None:
                catalog_id = self.track.catalog_id if self.track is not None else ''
            lyrics = Lyrics(answer, catalog_id)
        else:
            lyrics = None
        if lyrics is not self.lyrics:
            self.lyrics = lyrics

    def _set_lyrics_loading(self, loading):
        if loading != self.lyrics_loading:
            self.lyrics_loading = loading

    def _set_position(self, position, duration=None):
        """The position (stamped only when it changes: MusicKit reports whole seconds
        four times a second, and the stamp is when the second began) and the duration."""
        position = max(0.0, position)
        if position != self.position:
            self.position_updated_at = time.monotonic()
            self.position = position
        if duration is not None:
            self._set_duration(duration)

    def _set_duration(self, duration):
        """MusicKit's duration for the item; nothing (0, while an item loads) keeps Apple's
        length for it, when the item has one."""
        duration = max(0.0, duration)
        if duration <= 0 and self.track is not None:
            duration = self.track.duration_ms / 1000
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
    def resting(self):
        """The state with 'seeking' seen through: the state before the seek while it lasts
        (a seek while playing stays playing, one while paused stays paused)."""
        return self._resting if self.state == 'seeking' else self.state

    @property
    def active(self):
        """Whether playback is under way (ACTIVE_STATES, a seek during it included): the
        bar shows Pause."""
        return self.resting in ACTIVE_STATES

    @property
    def stopped(self):
        """Whether nothing plays or is paused (no item, or STOPPED_STATES): what background
        playback waits for before the app quits. MusicKit passes through these between
        queues and at the end of each item, so a caller gives it a moment."""
        return self.track is None or self.resting in STOPPED_STATES

    def estimated_position(self):
        """The position now: the last one reported plus the time since while playing (a
        second at most: the reports are whole seconds, so the truth is within the second
        that began at the stamp, and a stalled stream does not run ahead), never past the
        duration when that is known. What MPRIS answers between events and the lyrics
        follow."""
        position = self.position
        if self.state == 'playing':
            position += min(time.monotonic() - self.position_updated_at, 1.0)
            if self.duration:
                position = min(position, self.duration)
        return position

    # -- commands ------------------------------------------------------------------------

    async def ensure_engine(self):
        """The engine up for a play request (or any command of the account's: the item
        actions use it too): started first when it is down and the account is signed in (a
        toast meanwhile); EngineError('not-signed-in') when it is not."""
        if self._app.demo:
            raise EngineError('engine-down', 'no engine with the demo library')
        if self._engine.state != 'down':
            return
        if not self._app.settings.get_boolean(self._app.account_key('signed-in')):
            raise EngineError('not-signed-in', 'sign in to Apple Music to play')
        self._app.toast(_('Starting playback engine…'))
        await self._engine.start()

    async def play(self, play, start_with=None, shuffle=None):
        """Play what `play` names ({kind, id}: an Item's or a Group's play target), from its
        entry at queue position `start_with`; `shuffle` True shuffled (a Shuffle button),
        False in order (a Play button), None as the mode is (a track row). Requests go to
        the engine one at a time, in order, and one superseded while it waits is dropped
        (the newest wins: a second album clicked while the first's queue loads). `pending`
        is True from a request until the engine has answered it, or it failed."""
        kind = play.get('kind') if isinstance(play, dict) else None
        item_id = play.get('id') if isinstance(play, dict) else None
        if not kind or item_id in (None, ''):
            raise EngineError('usage', 'nothing to play')
        self._play_serial += 1
        serial = self._play_serial
        self._set_pending(True)
        try:
            async with self._play_lock:
                if serial != self._play_serial:
                    log.debug('play %s %s superseded', kind, item_id)
                    return
                await self.ensure_engine()
                log.info('play %s %s%s%s', kind, item_id,
                         f' from {start_with}' if start_with is not None else '',
                         {True: ' shuffled', False: ' in order'}.get(shuffle, ''))
                await self._engine.play(kind, item_id, start_with=start_with, shuffle=shuffle)
        finally:
            if serial == self._play_serial:
                self._set_pending(False)

    def _set_pending(self, pending):
        if pending != self.pending:
            self.pending = pending

    async def play_next(self, kind, item_id):
        await self.ensure_engine()
        await self._engine.play_next(kind, item_id)

    async def play_later(self, kind, item_id):
        await self.ensure_engine()
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
        """The item before, or this one from the top when it is PREVIOUS_RESTART seconds
        in."""
        if self.estimated_position() > PREVIOUS_RESTART:
            await self.seek(0)
        else:
            await self._engine.control('previous')

    async def stop(self):
        await self._engine.control('stop')

    async def queue_jump(self, index):
        """Play the queue's entry at `index` (the Up Next list)."""
        await self._engine.queue_jump(int(index))

    async def seek(self, seconds):
        self._track_since = float('-inf')  # the positions that follow are the seek's
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
