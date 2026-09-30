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

from applemusic import discord  # noqa: E402


class Track:
    """What the Player's NowPlaying gives the presence."""

    def __init__(self, title='Low Tide Warning', artist='The Midnight Archipelago',
                 album='Signal from the Shallows'):
        self.title = title
        self.artist = artist
        self.album = album


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
        self.assertEqual(activity['state'],
                         'The Midnight Archipelago — Signal from the Shallows')

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

    def test_a_track_with_no_artist_or_album_has_no_state(self):
        activity = discord.activity_for(Track(artist='', album=''), 'playing', 0, 0)
        self.assertEqual(activity['details'], 'Low Tide Warning')
        self.assertNotIn('state', activity)

    def test_nothing_of_the_account_is_sent(self):
        activity = discord.activity_for(Track(), 'playing', 65.0, 303.0, now=1000.0)
        self.assertEqual(set(activity), {'type', 'details', 'state', 'timestamps'})


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
    """A unix socket that speaks enough of the protocol to record what arrives."""

    def __init__(self, path):
        self.path = path
        self.frames = []
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
        try:
            while True:
                header = await reader.readexactly(discord.HEADER_SIZE)
                opcode, length = struct.unpack('<II', header)
                body = await reader.readexactly(length) if length else b''
                self.frames.append((opcode, json.loads(body) if body else None))
                self._arrived.set()
                # Discord answers every frame; the app reads and drops these.
                answer = json.dumps({'evt': 'READY'}).encode()
                writer.write(struct.pack('<II', discord.OP_FRAME, len(answer)) + answer)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


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
