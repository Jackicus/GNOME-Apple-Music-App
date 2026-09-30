# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Discord rich presence: what is playing, shown on the user's Discord profile, while the
`discord-presence` setting is on.

    presence = Presence(app)    # in Application.do_startup, after app.player
    presence.start()            # follows the setting; does nothing while it is off
    presence.stop()             # in do_shutdown: the activity cleared, the socket closed

Discord listens on a unix socket in the runtime directory (`discord-ipc-0` … `-9`, and the
same names under the subdirectories its Flatpak and Snap packages use). The protocol is
frames of a little-endian opcode and length followed by JSON: a HANDSHAKE carrying the
application's id, then a FRAME per command. Only SET_ACTIVITY is sent here, and Discord's
answers are read and dropped, since nothing it says changes what the app does.

Everything is best-effort and quiet. Discord is usually not running, and that is not an error
the user should hear about: a socket that is missing, refuses, or breaks leaves the presence
disconnected and the app playing. A broken connection is retried when the track next changes,
never on a timer, so a user without Discord pays nothing.

The activity is the track's title as the details, the artist and album as the state, the
album's cover as the image, and `type` 2, which Discord renders as "Listening to" rather than
"Playing". While the music plays the timestamps carry the start and end of the track in
wall-clock seconds, so Discord counts down by itself and nothing has to be sent for the
progress to stay right; paused, they are left out, which stops the counter where it is.

Only what Discord shows is sent: the title, the artist, the album and the catalogue's own
cover URL, which is public. No library id, no token, nothing of the account's.

APPLICATION_ID is the app's own Discord application, registered by the project at
discord.com/developers. It is the application's id and nothing else: rich presence needs no
bot, no token and no OAuth, so the id is public and belongs in the source. Discord shows the
application's name, so a profile reads "Listening to Music Sleeve". An empty id turns the
feature off and says why once in the log.
"""

import asyncio
import json
import logging
import os
import re
import struct
import time

log = logging.getLogger(__name__)

APPLICATION_ID = '1554953668954955896'

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2

HEADER = struct.Struct('<II')
HEADER_SIZE = HEADER.size
MAX_FRAME = 64 * 1024  # a sane ceiling: Discord's answers are small, and a wild length is a
                       # broken stream, not a message worth reading

LISTENING = 2  # Discord's activity type: "Listening to <the application's name>"

# Discord fetches an external image itself and only over https, so Apple's own artwork URL is
# handed over as it is and nothing is uploaded anywhere. (The tools that do this for MPRIS
# players go to Last.fm, MusicBrainz or an image host, because MPRIS metadata rarely carries a
# URL Discord can fetch; here the player already has one.) The player holds a 256 px URL and
# Discord's card is bigger, so Apple's size segment is asked for larger where it is there.
ARTWORK_SIZE = 512
ARTWORK_SEGMENT = re.compile(r'/\d+x\d+(?=[a-z-]*\.(?:jpg|jpeg|png|webp)$)', re.IGNORECASE)

# Discord's own limits. A field outside them is rejected and the whole activity with it, so
# they are applied here rather than discovered at run time.
FIELD_MIN = 2
FIELD_MAX = 128

SOCKET_NAMES = tuple(f'discord-ipc-{i}' for i in range(10))
# Where each packaging of Discord puts its socket, relative to the runtime directory.
SOCKET_DIRS = ('', 'app/com.discordapp.Discord', 'snap.discord', 'app/com.discordapp.DiscordCanary',
               'app/com.discordapp.DiscordPTB')


def socket_paths(runtime_dir=None):
    """Every path Discord might be listening on, in the order they are worth trying."""
    base = runtime_dir or os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
    return [os.path.join(base, directory, name)
            for directory in SOCKET_DIRS for name in SOCKET_NAMES]


def _field(text):
    """A string Discord will accept, or None. Discord rejects anything under two characters
    and truncates nothing itself, so a one-character title (a real album has them) is padded
    rather than dropped, and a long one is cut on a word where it can be."""
    text = (text or '').strip()
    if not text:
        return None
    if len(text) < FIELD_MIN:
        return text + ' '
    if len(text) <= FIELD_MAX:
        return text
    cut = text[:FIELD_MAX - 1]
    space = cut.rfind(' ')
    return (cut[:space] if space > FIELD_MAX // 2 else cut) + '…'


def artwork_for(url):
    """The image URL to give Discord, or None. Only https is any use: Discord treats an http
    URL as an asset key instead, fails to find one, and shows the application's icon."""
    if not url or not url.startswith('https://'):
        return None
    return ARTWORK_SEGMENT.sub(f'/{ARTWORK_SIZE}x{ARTWORK_SIZE}', url)


def activity_for(track, state, position, duration, now=None):
    """The activity to send for a track, or None when there is nothing to show.

    `state` is the Player's; `position` and `duration` are seconds. Playing, the timestamps
    span the track so Discord runs the clock itself; paused, they are left out and the clock
    stops."""
    if track is None:
        return None
    details = _field(track.title)
    if details is None:
        return None
    activity = {'type': LISTENING, 'details': details}
    by = ' — '.join(part for part in (track.artist, track.album) if part)
    line = _field(by)
    if line is not None:
        activity['state'] = line
    art = artwork_for(getattr(track, 'artwork_url', None))
    if art is not None:
        activity['assets'] = {'large_image': art}
        album = _field(track.album)
        if album is not None:
            activity['assets']['large_text'] = album
    if state == 'playing' and duration > 0:
        started = (now if now is not None else time.time()) - max(position, 0.0)
        activity['timestamps'] = {'start': int(started * 1000),
                                  'end': int((started + duration) * 1000)}
    return activity


class Connection:
    """One connection to Discord's socket: the handshake, then frames out. Reads are drained
    and dropped. Every failure is the same failure — Discord has gone — so callers see False
    or an exception and drop the connection rather than tell them apart."""

    def __init__(self, application_id):
        self.application_id = application_id
        self._reader = None
        self._writer = None
        self._drain = None

    @property
    def open(self):
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, paths=None):
        """Try each socket in turn and handshake with the first that answers. True once
        connected, False when Discord is not there."""
        for path in (paths if paths is not None else socket_paths()):
            if not os.path.exists(path):
                continue
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(path)
                await self._send(OP_HANDSHAKE, {'v': 1, 'client_id': self.application_id})
            except (OSError, asyncio.IncompleteReadError):
                await self.close()
                continue
            log.debug('discord: connected on %s', path)
            self._drain = asyncio.create_task(self._read_forever())
            return True
        return False

    async def set_activity(self, activity):
        """Send an activity, or clear it with None. True if it went."""
        if not self.open:
            return False
        args = {'pid': os.getpid(), 'activity': activity}
        try:
            await self._send(OP_FRAME, {'cmd': 'SET_ACTIVITY', 'args': args,
                                        'nonce': f'{time.time():.6f}'})
        except OSError:
            await self.close()
            return False
        return True

    async def close(self):
        """Close the socket. Safe to call on a connection that never opened."""
        if self._drain is not None:
            self._drain.cancel()
            self._drain = None
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass

    async def _send(self, opcode, payload):
        data = json.dumps(payload).encode()
        self._writer.write(HEADER.pack(opcode, len(data)) + data)
        await self._writer.drain()

    async def _read_forever(self):
        """Read Discord's answers and drop them, so its writes never fill the socket. Ends
        quietly when the connection goes."""
        try:
            while True:
                header = await self._reader.readexactly(HEADER_SIZE)
                opcode, length = HEADER.unpack(header)
                if length > MAX_FRAME:
                    log.debug('discord: frame of %d bytes, dropping the connection', length)
                    break
                if length:
                    await self._reader.readexactly(length)
                if opcode == OP_CLOSE:
                    break
        except (asyncio.IncompleteReadError, OSError, asyncio.CancelledError):
            pass


class Presence:
    """Follows the Player and the `discord-presence` setting, and keeps Discord in step."""

    def __init__(self, app, application_id=APPLICATION_ID):
        self.app = app
        self.application_id = application_id
        self._connection = None
        self._handlers = []
        self._sent = None  # the last activity that went, so an unchanged one is not resent
        self._task = None
        self._warned = False

    def start(self):
        """Watch the setting and the Player. Nothing connects until the setting is on and
        something is playing."""
        settings, player = self.app.settings, self.app.player
        self._handlers = [
            (settings, settings.connect('changed::discord-presence', self._on_setting)),
            (player, player.connect('notify::track', self._on_change)),
            (player, player.connect('notify::state', self._on_change)),
        ]
        if self._enabled():
            self._update_soon()

    def stop(self):
        """Clear the activity and close the socket."""
        for source, handler in self._handlers:
            source.disconnect(handler)
        self._handlers = []
        if self._task is not None:
            self._task.cancel()
            self._task = None
        connection, self._connection = self._connection, None
        self._sent = None
        if connection is not None:
            self.app.spawn(_clear_and_close(connection))

    def _enabled(self):
        return self.app.settings.get_boolean('discord-presence')

    def _on_setting(self, _settings, _key):
        if self._enabled():
            self._update_soon()
        else:
            connection, self._connection = self._connection, None
            self._sent = None
            if connection is not None:
                self.app.spawn(_clear_and_close(connection))

    def _on_change(self, _player, _pspec):
        if self._enabled():
            self._update_soon()

    def _update_soon(self):
        """One update at a time: a track change that lands mid-update replaces it."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = self.app.spawn(self._update())

    async def _update(self):
        player = self.app.player
        activity = activity_for(player.track, player.state,
                                player.position, player.duration)
        if activity == self._sent and self._connection is not None:
            return
        if not self.application_id:
            if not self._warned:
                self._warned = True
                log.info('discord: no application id is set, so rich presence is off')
            return
        if self._connection is None or not self._connection.open:
            if activity is None:
                return  # nothing to say, so nothing to connect for
            self._connection = Connection(self.application_id)
            if not await self._connection.connect():
                self._connection = None
                return  # Discord is not running; try again at the next track
        if await self._connection.set_activity(activity):
            self._sent = activity
        else:
            self._connection = None
            self._sent = None


async def _clear_and_close(connection):
    """Take the activity off the profile before the socket goes, so a quit does not leave the
    user listening to something forever."""
    try:
        await connection.set_activity(None)
    finally:
        await connection.close()
