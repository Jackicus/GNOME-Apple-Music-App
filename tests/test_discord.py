# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""Discord rich presence (src/discord.py): the activity built for a track, the fields Discord's
limits allow, where the socket is looked for, and the frames that go down it. A fake Discord
listens on a real unix socket in a temporary directory, so the connection, the handshake and
SET_ACTIVITY are exercised without Discord."""

import asyncio
import json
import os
import struct
import tempfile
import unittest

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)

from gi.events import GLibEventLoop  # noqa: E402
from gi.repository import Gio, GObject  # noqa: E402

from applemusic import discord  # noqa: E402

from tests.gtk import SCHEMA_ID  # noqa: E402


class Track:
    """What the Player's NowPlaying gives the presence."""

    def __init__(self, title='Low Tide Warning', artist='The Midnight Archipelago',
                 album='Signal from the Shallows',
                 artwork_url='https://example.invalid/image/thumb/aW52ZW50ZWQ/256x256bb.jpg'):
        self.title = title
        self.artist = artist
        self.album = album
        self.artwork_url = artwork_url


class FieldTest(unittest.TestCase):
    def test_a_field_discord_would_reject_is_made_acceptable(self):
        self.assertIsNone(discord._field(''))
        self.assertIsNone(discord._field('   '))
        self.assertIsNone(discord._field(None))
        # Discord rejects anything under two characters; a real album has one-letter titles.
        self.assertEqual(discord._field('4'), '4 ')
        self.assertEqual(discord._field('OK'), 'OK')

    def test_a_long_field_is_cut_on_a_word(self):
        text = 'Signal ' * 40
        cut = discord._field(text)
        self.assertLessEqual(len(cut), discord.FIELD_MAX)
        self.assertTrue(cut.endswith('…'))
        self.assertNotIn('Signa…', cut)  # cut between words, not through one

    def test_a_long_field_without_spaces_is_still_cut(self):
        cut = discord._field('x' * 400)
        self.assertLessEqual(len(cut), discord.FIELD_MAX)
        self.assertTrue(cut.endswith('…'))


class ActivityTest(unittest.TestCase):
    def test_nothing_playing_is_no_activity(self):
        self.assertIsNone(discord.activity_for(None, 'playing', 0, 0))

    def test_a_track_without_a_title_is_no_activity(self):
        self.assertIsNone(discord.activity_for(Track(title=''), 'playing', 0, 200))

    def test_the_track_becomes_details_and_state(self):
        activity = discord.activity_for(Track(), 'playing', 65.0, 303.0, now=1000.0)
        self.assertEqual(activity['type'], discord.LISTENING)
        self.assertEqual(activity['details'], 'Low Tide Warning')
        self.assertEqual(activity['state'], 'The Midnight Archipelago')

    def test_it_is_listening_to_the_service_not_the_app(self):
        activity = discord.activity_for(Track(), 'playing', 65.0, 303.0, now=1000.0)
        self.assertEqual(activity['name'], 'Apple Music')

    def test_the_status_line_is_the_artist(self):
        activity = discord.activity_for(Track(), 'playing', 65.0, 303.0, now=1000.0)
        self.assertEqual(activity['status_display_type'], discord.STATUS_STATE)

    def test_the_album_is_on_the_cover(self):
        activity = discord.activity_for(Track(), 'playing', 65.0, 303.0, now=1000.0)
        self.assertEqual(activity['assets']['large_text'], 'Signal from the Shallows')

    def test_playing_carries_the_span_of_the_track(self):
        activity = discord.activity_for(Track(), 'playing', 65.0, 303.0, now=1000.0)
        # Started 65 s ago, ends 303 s after it started: Discord counts down by itself.
        self.assertEqual(activity['timestamps'],
                         {'start': 935000, 'end': 1238000})

    def test_paused_has_no_timestamps_so_the_clock_stops(self):
        activity = discord.activity_for(Track(), 'paused', 65.0, 303.0, now=1000.0)
        self.assertNotIn('timestamps', activity)

    def test_a_track_of_no_length_has_no_timestamps(self):
        activity = discord.activity_for(Track(), 'playing', 0.0, 0.0, now=1000.0)
        self.assertNotIn('timestamps', activity)

    def test_a_track_with_no_artist_has_no_state(self):
        # The status line then falls back to the name, "Apple Music".
        activity = discord.activity_for(Track(artist=''), 'playing', 0, 0)
        self.assertEqual(activity['details'], 'Low Tide Warning')
        self.assertNotIn('state', activity)
        self.assertNotIn('status_display_type', activity)

    def test_nothing_of_the_account_is_sent(self):
        # The artwork is the catalogue's own cover URL, which is public; no library id, no
        # token and nothing of the account's goes with it.
        activity = discord.activity_for(Track(), 'playing', 65.0, 303.0, now=1000.0)
        self.assertEqual(set(activity),
                         {'type', 'name', 'details', 'state', 'status_display_type',
                          'timestamps', 'assets'})
        self.assertEqual(set(activity['assets']), {'large_image', 'large_text'})


class ArtworkTest(unittest.TestCase):
    def test_an_https_url_is_asked_for_larger(self):
        self.assertEqual(
            discord.artwork_for('https://example.invalid/a/b/256x256bb.jpg'),
            'https://example.invalid/a/b/512x512bb.jpg')

    def test_a_url_without_a_size_is_left_alone(self):
        url = 'https://example.invalid/cover.png'
        self.assertEqual(discord.artwork_for(url), url)

    def test_http_is_refused_because_discord_would_show_its_own_icon(self):
        self.assertIsNone(discord.artwork_for('http://example.invalid/a/256x256bb.jpg'))

    def test_no_artwork_is_no_artwork(self):
        self.assertIsNone(discord.artwork_for(None))
        self.assertIsNone(discord.artwork_for(''))

    def test_a_track_without_artwork_still_makes_an_activity(self):
        activity = discord.activity_for(Track(artwork_url=None), 'playing', 0, 200)
        self.assertNotIn('assets', activity)
        self.assertEqual(activity['details'], 'Low Tide Warning')

    def test_the_cover_and_the_album_go_together(self):
        activity = discord.activity_for(Track(), 'playing', 0, 200)
        self.assertEqual(activity['assets']['large_image'],
                         'https://example.invalid/image/thumb/aW52ZW50ZWQ/512x512bb.jpg')
        self.assertEqual(activity['assets']['large_text'], 'Signal from the Shallows')


class SocketPathTest(unittest.TestCase):
    def test_every_packaging_of_discord_is_looked_for(self):
        paths = discord.socket_paths('/run/user/1000')
        self.assertIn('/run/user/1000/discord-ipc-0', paths)
        self.assertIn('/run/user/1000/app/com.discordapp.Discord/discord-ipc-0', paths)
        self.assertIn('/run/user/1000/snap.discord/discord-ipc-0', paths)
        # Ten sockets each, since Discord takes the first free one.
        self.assertIn('/run/user/1000/discord-ipc-9', paths)

    def test_the_plain_runtime_directory_is_tried_first(self):
        paths = discord.socket_paths('/run/user/1000')
        self.assertEqual(paths[0], '/run/user/1000/discord-ipc-0')


class FakeDiscord:
    """A unix socket that speaks enough of the protocol to record what arrives. Like Discord,
    it answers the handshake with READY after a moment, and drops without a word any frame
    that comes before its READY has gone (`dropped`)."""

    READY_DELAY = 0.05

    def __init__(self, path, answer_handshake=True):
        self.path = path
        self.answer_handshake = answer_handshake
        self.frames = []
        self.dropped = []
        self._server = None
        self._arrived = asyncio.Event()

    async def start(self):
        self._server = await asyncio.start_unix_server(self._serve, self.path)

    async def wait_for(self, count, timeout=5):
        """Wait until `count` frames have arrived. A bare sleep(0) is not enough: the server
        is a coroutine of its own and has to be given the loop."""
        async with asyncio.timeout(timeout):
            while len(self.frames) < count:
                self._arrived.clear()
                await self._arrived.wait()

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader, writer):
        ready = None
        try:
            while True:
                header = await reader.readexactly(discord.HEADER_SIZE)
                opcode, length = struct.unpack('<II', header)
                body = await reader.readexactly(length) if length else b''
                payload = json.loads(body) if body else None
                if opcode == discord.OP_HANDSHAKE:
                    if self.answer_handshake:
                        ready = asyncio.create_task(self._ready(writer))
                elif ready is None or not ready.done():
                    self.dropped.append((opcode, payload))
                    continue
                else:
                    self._answer(writer, {'cmd': payload.get('cmd'), 'evt': None,
                                          'nonce': payload.get('nonce')})
                self.frames.append((opcode, payload))
                self._arrived.set()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if ready is not None:
                ready.cancel()
            writer.close()  # the server's own end, or the suite warns of an unclosed socket
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _ready(self, writer):
        await asyncio.sleep(self.READY_DELAY)
        self._answer(writer, {'cmd': 'DISPATCH', 'evt': 'READY', 'data': {'v': 1}})

    @staticmethod
    def _answer(writer, payload):
        answer = json.dumps(payload).encode()
        writer.write(struct.pack('<II', discord.OP_FRAME, len(answer)) + answer)


class ConnectionTest(unittest.IsolatedAsyncioTestCase):
    # The loop the app really runs on (main.use_glib_event_loop), as test_engine.py does.
    # asyncio's own loop hangs here under scripts/headless.sh.
    loop_factory = GLibEventLoop

    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'discord-ipc-0')
        self.discord = FakeDiscord(self.path)
        self.connections = []
        await self.discord.start()

    async def asyncTearDown(self):
        # Every client goes before the server does: asyncio.Server.wait_closed() waits for its
        # handlers, and a handler reading from a client that is still open never returns.
        for connection in self.connections:
            await connection.close()
        await self.discord.stop()
        self.dir.cleanup()

    async def connected(self):
        """A connection to the fake, closed again however the test ends."""
        connection = discord.Connection('123456')
        self.connections.append(connection)
        self.assertTrue(await connection.connect([self.path]))
        return connection

    async def test_it_handshakes_with_the_application_id(self):
        await self.connected()
        await self.discord.wait_for(1)
        self.assertEqual(self.discord.frames[0],
                         (discord.OP_HANDSHAKE, {'v': 1, 'client_id': '123456'}))

    async def test_an_activity_goes_as_a_frame(self):
        connection = await self.connected()
        activity = discord.activity_for(Track(), 'playing', 0.0, 303.0, now=1000.0)
        self.assertTrue(await connection.set_activity(activity))
        await self.discord.wait_for(2)
        opcode, payload = self.discord.frames[1]
        self.assertEqual(opcode, discord.OP_FRAME)
        self.assertEqual(payload['cmd'], 'SET_ACTIVITY')
        self.assertEqual(payload['args']['pid'], os.getpid())
        self.assertEqual(payload['args']['activity']['details'], 'Low Tide Warning')
        self.assertTrue(payload['nonce'])

    async def test_nothing_is_sent_before_discord_is_ready(self):
        connection = await self.connected()
        await connection.set_activity({'type': discord.LISTENING, 'details': 'Low Tide Warning'})
        await self.discord.wait_for(2)
        self.assertEqual(self.discord.dropped, [])

    async def test_a_discord_that_never_answers_is_given_up(self):
        await self.discord.stop()
        self.discord = FakeDiscord(self.path, answer_handshake=False)
        await self.discord.start()
        connection = discord.Connection('123456')
        self.connections.append(connection)
        original, discord.READY_TIMEOUT = discord.READY_TIMEOUT, 0.1
        self.addCleanup(setattr, discord, 'READY_TIMEOUT', original)
        self.assertFalse(await connection.connect([self.path]))
        self.assertFalse(connection.open)

    async def test_clearing_sends_a_null_activity(self):
        connection = await self.connected()
        await connection.set_activity(None)
        await self.discord.wait_for(2)
        self.assertIsNone(self.discord.frames[1][1]['args']['activity'])

    async def test_no_socket_is_not_an_error(self):
        connection = discord.Connection('123456')
        missing = os.path.join(self.dir.name, 'discord-ipc-7')
        self.assertFalse(await connection.connect([missing]))
        self.assertFalse(connection.open)

    async def test_sending_without_a_connection_says_so_rather_than_raising(self):
        connection = discord.Connection('123456')
        self.assertFalse(await connection.set_activity({'details': 'x'}))

    async def test_closing_a_connection_that_never_opened_is_safe(self):
        await discord.Connection('123456').close()


if __name__ == '__main__':
    unittest.main()


class ApplicationIdTest(unittest.TestCase):
    def test_the_application_id_is_a_discord_snowflake(self):
        """The app's own Discord application. It needs no bot and no token, so the id is
        public and belongs in the source; what it must not be is empty, or the feature is
        silently inert."""
        self.assertTrue(discord.APPLICATION_ID.isdigit(), discord.APPLICATION_ID)
        self.assertGreaterEqual(len(discord.APPLICATION_ID), 17)


class FakePlayer(GObject.Object):
    state = GObject.Property(type=str, default='none')
    track = GObject.Property(type=object)
    position = GObject.Property(type=float, default=0.0)
    duration = GObject.Property(type=float, default=0.0)
    pending = GObject.Property(type=bool, default=False)

    @property
    def resting(self):
        """The real Player smooths the states a queue swap passes through; nothing here
        needs that, so the state stands for it."""
        return self.state


class FakeApp:
    """Only what Presence reaches for."""

    def __init__(self):
        self.settings = Gio.Settings.new(SCHEMA_ID)
        self.settings.set_boolean('discord-presence', False)
        self.player = FakePlayer()
        self.tasks = []

    def spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def settle(self):
        while self.tasks:
            tasks, self.tasks = self.tasks, []
            await asyncio.gather(*tasks, return_exceptions=True)


class PresenceTest(unittest.IsolatedAsyncioTestCase):
    """The whole of it against a fake Discord: the setting, the Player's signals and the
    socket, with only Discord itself standing in."""

    loop_factory = GLibEventLoop

    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'discord-ipc-0')
        self.discord = FakeDiscord(self.path)
        await self.discord.start()
        self.app = FakeApp()
        self.presence = discord.Presence(self.app, application_id='123456')
        # Look only where the fake is: the real runtime directory is not the test's business.
        self.presence._paths = [self.path]
        original = discord.Connection.connect
        paths = [self.path]

        async def connect(connection, _paths=None):
            return await original(connection, paths)

        discord.Connection.connect = connect
        self.addCleanup(setattr, discord.Connection, 'connect', original)

    async def asyncTearDown(self):
        self.presence.stop()
        await self.app.settle()
        if self.presence._connection is not None:
            await self.presence._connection.close()
        await self.discord.stop()
        self.dir.cleanup()

    def play(self, track=None):
        self.app.player.track = track if track is not None else Track()
        self.app.player.duration = 303.0
        self.app.player.state = 'playing'

    async def test_nothing_is_sent_while_the_setting_is_off(self):
        self.presence.start()
        self.play()
        await self.app.settle()
        self.assertEqual(self.discord.frames, [])

    async def test_the_track_reaches_discord_when_the_setting_is_on(self):
        self.app.settings.set_boolean('discord-presence', True)
        self.presence.start()
        self.play()
        await self.app.settle()
        await self.discord.wait_for(2)
        self.assertEqual(self.discord.frames[0][0], discord.OP_HANDSHAKE)
        activity = self.discord.frames[1][1]['args']['activity']
        self.assertEqual(activity['details'], 'Low Tide Warning')
        self.assertEqual(activity['type'], discord.LISTENING)

    async def test_turning_the_setting_off_clears_the_activity(self):
        self.app.settings.set_boolean('discord-presence', True)
        self.presence.start()
        self.play()
        await self.app.settle()
        await self.discord.wait_for(2)
        self.app.settings.set_boolean('discord-presence', False)
        await self.app.settle()
        await self.discord.wait_for(3)
        self.assertIsNone(self.discord.frames[-1][1]['args']['activity'])

    async def test_stopping_clears_the_activity(self):
        self.app.settings.set_boolean('discord-presence', True)
        self.presence.start()
        self.play()
        await self.app.settle()
        await self.discord.wait_for(2)
        self.presence.stop()
        await self.app.settle()
        await self.discord.wait_for(3)
        self.assertIsNone(self.discord.frames[-1][1]['args']['activity'])


class SameActivityTest(unittest.TestCase):
    """What counts as a change worth a frame."""

    def make(self, start=1000, **kw):
        a = {'type': 2, 'details': 'Low Tide Warning', 'state': 'The Midnight Archipelago'}
        a.update(kw)
        if start is not None:
            a['timestamps'] = {'start': start, 'end': start + 300000}
        return a

    def test_a_start_that_drifted_is_the_same_activity(self):
        # Worked out from the position each time, so it moves by a moment. Not a seek.
        self.assertTrue(discord.same_activity(self.make(1000), self.make(1900)))

    def test_a_real_seek_is_a_change(self):
        self.assertFalse(discord.same_activity(self.make(1000), self.make(60000)))

    def test_a_different_track_is_a_change(self):
        self.assertFalse(discord.same_activity(self.make(), self.make(details='Other')))

    def test_losing_the_timestamps_is_a_change(self):
        # Playing to paused: the clock has to stop.
        self.assertFalse(discord.same_activity(self.make(), self.make(start=None)))

    def test_nothing_equals_nothing(self):
        self.assertTrue(discord.same_activity(None, None))
        self.assertFalse(discord.same_activity(None, self.make()))


class SettlingTest(unittest.IsolatedAsyncioTestCase):
    """A track change arrives in pieces; Discord must see one frame, not four."""

    loop_factory = GLibEventLoop

    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'discord-ipc-0')
        self.discord = FakeDiscord(self.path)
        await self.discord.start()
        self.app = FakeApp()
        self.app.settings.set_boolean('discord-presence', True)
        self.presence = discord.Presence(self.app, application_id='123456')
        original = discord.Connection.connect
        paths = [self.path]

        async def connect(connection, _paths=None):
            return await original(connection, paths)

        discord.Connection.connect = connect
        self.addCleanup(setattr, discord.Connection, 'connect', original)

    async def asyncTearDown(self):
        self.presence.stop()
        await self.app.settle()
        if self.presence._connection is not None:
            await self.presence._connection.close()
        await self.discord.stop()
        self.dir.cleanup()

    async def test_a_track_change_in_pieces_is_one_frame(self):
        self.presence.start()
        # As the Player really does it: the track arrives, the duration is still 0, then it
        # lands and the state turns over. Live this sent four frames, two of them without
        # timestamps, and Discord's progress bar flickered at every track change.
        self.app.player.track = Track()
        self.app.player.state = 'playing'
        self.app.player.duration = 303.0
        await self.app.settle()
        await self.discord.wait_for(2)
        await asyncio.sleep(discord.UPDATE_GRACE_MS / 1000 + 0.2)
        await self.app.settle()
        activities = [f[1]['args']['activity'] for f in self.discord.frames
                      if f[0] == discord.OP_FRAME]
        self.assertEqual(len(activities), 1, activities)
        self.assertIn('timestamps', activities[0])


class FinishedTest(unittest.TestCase):
    """Playback over means nothing on the profile, whatever the Player still holds."""

    def test_stopping_shows_nothing(self):
        for state in discord.FINISHED_STATES:
            with self.subTest(state=state):
                self.assertIsNone(
                    discord.activity_for(Track(), state, 10.0, 300.0))

    def test_paused_still_shows_the_track(self):
        activity = discord.activity_for(Track(), 'paused', 10.0, 300.0)
        self.assertEqual(activity['details'], 'Low Tide Warning')
        self.assertNotIn('timestamps', activity)
