# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""src/backend/bridge.js, run under gjs against the fake page in tests/bridge_harness.js.

bridge.js runs in music.apple.com, where no test can reach it; gjs (GNOME's JavaScript, on every
GNOME system) runs it unchanged once `window` is the global object. Each test names a scenario,
an async JavaScript function of {bridge, mk, posted, page}: `bridge` the injected
window.__appleMusicLibrary, `mk` the fake MusicKit instance (its `calls`, `listeners`, `fire()`),
`posted` the events the bridge sent through the binding, `page.elements` what document's
selectors find. All scenarios run in one gjs process, each on a fresh page, and the test gets
what its scenario returned. Skipped without gjs.
"""

import functools
import json
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from tests import ROOT

HARNESS = pathlib.Path(__file__).with_name('bridge_harness.js')
BRIDGE = ROOT / 'src' / 'backend' / 'bridge.js'
GJS = shutil.which('gjs')

EVENTS = ('authorizationStatusDidChange', 'playbackStateDidChange', 'nowPlayingItemDidChange',
          'playbackTimeDidChange', 'playbackDurationDidChange', 'queueItemsDidChange',
          'queuePositionDidChange', 'shuffleModeDidChange', 'repeatModeDidChange',
          'playbackVolumeDidChange', 'mediaPlaybackError')

# Apple's TTML lyrics, as the catalog's /lyrics answers carry them (invented words): two
# stanzas, entities, word-timed spans, a <br>, CDATA, the four time formats, a blank line.
TTML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyric-ttml-internal"'
    ' xml:lang="en"><head><metadata/></head><body dur="1:02.500">'
    '<div begin="0.5s" end="20s">'
    '<p begin="00:00.500" end="00:04.250">Rock &amp; Roll &#8217;til dawn</p>'
    '<p begin="4.5s" end="8s"><span begin="4.5s">Harbour</span> <span begin="5s">lights</span>'
    '</p></div>'
    '<div begin="20s" end="1:02.5">'
    '<p begin="0:20.000" end="0:25.100">Out on the<br/>water</p>'
    '<p begin="1:00:01.5" end="1:00:03"><![CDATA[Fish & chips <tonight>]]></p>'
    '<p begin="30s"> </p></div></body></tt>'
)

# A song as MusicKit's queue and nowPlayingItem hold it (a MediaItem: id, type, attributes).
SONG = """{
    id: 'i.song1', type: 'library-songs',
    attributes: {
        name: 'Harbour Lights', artistName: 'The Invented Band', albumName: 'Tidewater',
        durationInMillis: 216000, trackNumber: 3, discNumber: 1, contentRating: 'explicit',
        artwork: {url: 'https://example.invalid/{w}x{h}bb.jpg'},
        playParams: {id: 'i.song1', kind: 'song', catalogId: '1000000001'},
    },
}"""


def scenario(js):
    """Run the test with what `js` returned: the body of an async function of {bridge, mk,
    posted, page}, run on a fresh page. A scenario that throws fails the test."""
    def decorate(test):
        @functools.wraps(test)
        def run(self):
            outcome = self.outcomes.get(test.__name__)
            if outcome is None:
                self.fail(f'{test.__name__}: no outcome')
            if 'error' in outcome:
                self.fail(f'{test.__name__}: the scenario threw: {outcome["error"]}')
            return test(self, outcome['value'])
        run.scenario = textwrap.dedent(js)
        return run
    return decorate


def run_scenarios(scenarios):
    """{name: outcome} for each scenario, from one gjs run of the harness, the bridge and them."""
    parts = [HARNESS.read_text(encoding='utf-8'),
             f'const BRIDGE = {json.dumps(BRIDGE.read_text(encoding="utf-8"))};',
             f'const SONG = {SONG};',
             f'const TTML = {json.dumps(TTML)};',
             'const SCENARIOS = {']
    for name, body in scenarios.items():
        parts.append(f'{json.dumps(name)}: async ({{bridge, mk, posted, page}}) => {{\n{body}\n}},')
    parts += ['};', 'run();']
    with tempfile.NamedTemporaryFile('w', suffix='.js', encoding='utf-8') as script:
        script.write('\n'.join(parts))
        script.flush()
        done = subprocess.run([GJS, script.name], capture_output=True, text=True, timeout=60)
    if done.returncode != 0 or not done.stdout.strip():
        raise AssertionError(f'gjs failed ({done.returncode}): {done.stderr.strip()}')
    return json.loads(done.stdout.strip().splitlines()[-1])


class BridgeTest(unittest.TestCase):
    outcomes = {}

    @classmethod
    def setUpClass(cls):
        if GJS is None:
            raise unittest.SkipTest('gjs is not installed')
        scenarios = {name: getattr(cls, name).scenario for name in dir(cls)
                     if name.startswith('test_') and hasattr(getattr(cls, name), 'scenario')}
        cls.outcomes = run_scenarios(scenarios)

    # -- status and events -----------------------------------------------------------------

    @scenario("""
        const signedOut = bridge.status();
        mk.isAuthorized = true;
        const signedIn = bridge.status();
        delete window.MusicKit;
        return {signedOut, signedIn, missing: bridge.status()};
    """)
    def test_status(self, value):
        self.assertEqual(value['signedOut'], {'ready': True, 'engine': True, 'authorized': False,
                                              'storefront': 'gb', 'bitrate': 256})
        self.assertTrue(value['signedIn']['authorized'])
        self.assertEqual(value['missing'], {'ready': False, 'engine': True, 'authorized': False,
                                            'storefront': 'us', 'bitrate': 256})

    @scenario("""
        const first = bridge.subscribe();
        const second = bridge.subscribe();
        return {first, second, listeners: mk.listenerCounts()};
    """)
    def test_subscribe_attaches_once(self, value):
        self.assertTrue(value['first']['attached'])
        self.assertFalse(value['second']['attached'])
        self.assertEqual(sorted(value['first']['events']), sorted(EVENTS))
        self.assertEqual(value['listeners'], dict.fromkeys(EVENTS, 1))

    @scenario("""
        bridge.subscribe();
        inject('v2');   // a changed bridge: client.load_bridge's new version
        const newer = window.__appleMusicLibrary;
        const subscribed = newer.subscribe();
        inject('v2');   // the same one again: nothing happens
        return {replaced: newer !== bridge, kept: window.__appleMusicLibrary === newer,
                version: newer.__version, subscribed, listeners: mk.listenerCounts()};
    """)
    def test_a_new_bridge_replaces_the_old_ones_listeners(self, value):
        self.assertTrue(value['replaced'])
        self.assertTrue(value['kept'])
        self.assertEqual(value['version'], 'v2')
        self.assertTrue(value['subscribed']['attached'])
        self.assertEqual(value['listeners'], dict.fromkeys(EVENTS, 1))

    @scenario("""
        bridge.subscribe();
        const answer = bridge.unsubscribe();
        return {answer, listeners: mk.listenerCounts()};
    """)
    def test_unsubscribe(self, value):
        self.assertEqual(value, {'answer': {'subscribed': False}, 'listeners': {}})

    @scenario("""
        bridge.subscribe();
        mk.isAuthorized = true;
        mk.fire('authorizationStatusDidChange', {authorizationStatus: 3});
        mk.currentPlaybackTime = 3;
        mk.currentPlaybackDuration = 30;
        mk.playbackState = 3;
        mk.fire('playbackStateDidChange', {state: 2});
        mk.fire('playbackStateDidChange', {});   // no state on the event: the instance's
        mk.nowPlayingItem = SONG;
        mk.nowPlayingItemIndex = 1;
        mk.fire('nowPlayingItemDidChange', {});
        mk.fire('playbackTimeDidChange', {});
        mk.fire('playbackDurationDidChange', {});
        mk.queue = {items: [SONG], position: 0};
        mk.fire('queueItemsDidChange', {});
        mk.fire('queuePositionDidChange', {position: 1, oldPosition: 0});
        mk.shuffleMode = 1;
        mk.fire('shuffleModeDidChange', {});
        mk.repeatMode = 2;
        mk.fire('repeatModeDidChange', {});
        mk.volume = 0.5;
        mk.fire('playbackVolumeDidChange', {});
        return posted;
    """)
    def test_event_payloads(self, value):
        events = [(event['name'], event['data']) for event in value]
        track = events[3][1]['track']
        self.assertEqual(events, [
            ('authorizationStatusDidChange', {'authorized': True, 'status': 3}),
            ('playbackStateDidChange', {'state': 'playing', 'position': 3, 'duration': 30}),
            ('playbackStateDidChange', {'state': 'paused', 'position': 3, 'duration': 30}),
            ('nowPlayingItemDidChange', {'track': track, 'index': 1}),
            ('playbackTimeDidChange', {'position': 3, 'duration': 30}),
            ('playbackDurationDidChange', {'duration': 30}),
            ('queueItemsDidChange', {'index': 0, 'items': [dict(track, index=0)]}),
            ('queuePositionDidChange', {'index': 1, 'oldIndex': 0}),
            ('shuffleModeDidChange', {'shuffle': 'on'}),
            ('repeatModeDidChange', {'repeat': 'all'}),
            ('playbackVolumeDidChange', {'volume': 0.5}),
        ])
        self.assertEqual(track, {
            'id': 'i.song1', 'type': 'library-songs', 'catalogId': '1000000001',
            'title': 'Harbour Lights',
            'artist': 'The Invented Band', 'album': 'Tidewater', 'trackNumber': 3,
            'discNumber': 1, 'durationMs': 216000, 'durationLabel': '3:36', 'explicit': True,
            'artUrl': 'https://example.invalid/256x256bb.jpg', 'index': 1})

    @scenario("""
        bridge.subscribe();
        mk.fire('mediaPlaybackError', {errorCode: 'CONTENT_UNAVAILABLE', name: 'MKError',
                                       message: 'Content unavailable'});
        mk.fire('mediaPlaybackError', {name: 'NotAllowedError', description: 'No gesture'});
        mk.fire('mediaPlaybackError', 'Playback failed');
        mk.fire('mediaPlaybackError', undefined);
        return posted.map(event => event.data);
    """)
    def test_playback_errors_carry_musickits_code(self, value):
        self.assertEqual(value, [
            {'code': 'CONTENT_UNAVAILABLE', 'message': 'Content unavailable'},
            {'code': 'NotAllowedError', 'message': 'No gesture'},
            {'code': '', 'message': 'Playback failed'},
            {'code': '', 'message': 'unknown error'}])

    @scenario("""
        mk.failures.setQueue = {errorCode: 'CONTENT_UNAVAILABLE', message: 'Content unavailable'};
        const queued = await bridge.play('album', '1000000002');
        delete mk.failures.setQueue;
        mk.failures.play = new Error('The play() request was interrupted');
        const played = await bridge.play('album', '1000000002');
        delete mk.failures.play;
        return {queued, played, fine: await bridge.play('album', '1000000002')};
    """)
    def test_a_refused_play_answers_its_code(self, value):
        self.assertEqual(value, {
            'queued': {'error': 'Content unavailable', 'code': 'CONTENT_UNAVAILABLE'},
            'played': {'error': 'The play() request was interrupted', 'code': 'Error'},
            'fine': {'ok': True}})

    @scenario("""
        bridge.subscribe();
        for (const state of [0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 'ended', 99])
            mk.fire('playbackStateDidChange', {state});
        return posted.map(event => event.data.state);
    """)
    def test_playback_states_are_named(self, value):
        self.assertEqual(value, ['none', 'loading', 'playing', 'paused', 'stopped', 'ended',
                                 'seeking', 'waiting', 'stalled', 'completed', 'ended', 'none'])

    @scenario("""
        bridge.subscribe();
        delete window.__amEvent;   // the binding gone with the connection
        mk.fire('playbackStateDidChange', {state: 2});
        return posted.length;
    """)
    def test_no_binding_posts_nothing(self, value):
        self.assertEqual(value, 0)

    # -- the account -----------------------------------------------------------------------

    @scenario("""
        const names = {};
        page.elements = {'.account-menu .user__name': [{textContent: '  Jo \\n Bloggs '}]};
        names.menu = bridge.accountName();
        // Signed out, or the moment after sign-in: the footer's sign-in button, in any
        // language, and the menu's name is not trusted yet.
        page.elements['button.signin'] = [{textContent: 'Anmelden'}];
        names.signingIn = bridge.accountName();
        // Broader elements are never read, whatever they hold.
        page.elements = {'.auth-content span': [{textContent: 'Se connecter'}],
                         'nav footer button[aria-haspopup] span': [{textContent: 'Jo'}]};
        names.footer = bridge.accountName();
        page.elements = {'.user__name': [{textContent: 'x'.repeat(65)}, {textContent: ''}],
                         '.account-name': [{textContent: 'Sam'}]};
        names.fallback = bridge.accountName();
        page.elements = {};
        names.none = bridge.accountName();
        return names;
    """)
    def test_account_name(self, value):
        self.assertEqual(value, {'menu': 'Jo Bloggs', 'signingIn': None, 'footer': None,
                                 'fallback': 'Sam', 'none': None})

    @scenario("""
        const done = await bridge.signout();
        const calls = mk.calls.map(call => call[0]);
        mk.failures.unauthorize = new Error('Network error');
        const refused = await bridge.signout();
        delete window.MusicKit;
        return {done, calls, refused, missing: await bridge.signout()};
    """)
    def test_signout_revokes_the_session(self, value):
        self.assertEqual(value['done'], {'ok': True})
        self.assertEqual(value['calls'], ['unauthorize'])
        self.assertEqual(value['refused'], {'error': 'Network error'})
        self.assertEqual(value['missing'], {'error': 'MusicKit not initialized'})

    # -- playback --------------------------------------------------------------------------

    @scenario("""
        await bridge.play('album', 'l.alb1', {startWith: 2});
        await bridge.play('playlist', 'p.pl1');
        await bridge.play('station', 'ra.1');
        await bridge.play('musicVideo', '1000000009');
        mk.apiAnswers['/v1/catalog/gb/artists/42/view/top-songs'] = {data: [{id: '1'}, {id: '2'}]};
        await bridge.play('artist', '42');
        await bridge.play('artist', '43');   // no top songs: the artist's station
        await bridge.play('songs', 'i.a,i.b,i.c', {startWith: 1});   // a stand-in album
        await bridge.playNext('songs', 'i.d,i.e');
        await bridge.playLater('album', 'l.alb2');
        await bridge.playNext('song', '1000000001');
        return mk.calls.filter(call => ['setQueue', 'playNext', 'playLater'].includes(call[0]))
            .map(call => [call[0], call[1]]);
    """)
    def test_play_queues_each_kind(self, value):
        self.assertEqual(value, [
            ['setQueue', {'startWith': 2, 'startPlaying': True, 'album': 'l.alb1'}],
            ['setQueue', {'startWith': 0, 'startPlaying': True, 'playlist': 'p.pl1'}],
            ['setQueue', {'startWith': 0, 'startPlaying': True, 'station': 'ra.1'}],
            ['setQueue', {'startWith': 0, 'startPlaying': True, 'musicVideo': '1000000009'}],
            ['setQueue', {'startWith': 0, 'startPlaying': True, 'songs': ['1', '2']}],
            ['setQueue', {'startWith': 0, 'startPlaying': True, 'station': '43'}],
            ['setQueue', {'startWith': 1, 'startPlaying': True, 'songs': ['i.a', 'i.b', 'i.c']}],
            ['playNext', {'songs': ['i.d', 'i.e']}],
            ['playLater', {'album': 'l.alb2'}],
            ['playNext', {'song': '1000000001'}],
        ])

    @scenario("""
        const modes = [];
        await bridge.play('album', 'l.alb1', {shuffle: true});
        modes.push(mk.shuffleMode);
        await bridge.play('album', 'l.alb1', {startWith: 3});   // a track row: as it is
        modes.push(mk.shuffleMode);
        await bridge.play('album', 'l.alb1', {startWith: 3, shuffle: null});
        modes.push(mk.shuffleMode);
        await bridge.play('album', 'l.alb1', {shuffle: false});   // Play: in order
        modes.push(mk.shuffleMode);
        await bridge.play('album', 'l.alb1', {startWith: 1});
        modes.push(mk.shuffleMode);
        delete window.MusicKit.PlayerShuffleMode;   // MusicKit's numbers without the names
        await bridge.play('album', 'l.alb1', {shuffle: true});
        modes.push(mk.shuffleMode);
        return modes;
    """)
    def test_play_turns_shuffle_on_off_or_leaves_it(self, value):
        self.assertEqual(value, [1, 1, 1, 0, 0, 1])

    @scenario("""
        // MusicKit's queue for the album holds the row's song one place later than the
        // library's list has it (it left out an entry before it).
        mk.setQueue = async function (options) {
            this.calls.push(['setQueue', options]);
            this.queue = {position: options.startWith, items: [
                {id: 'i.one'}, {id: 'i.three'}, {id: 'i.four',
                 attributes: {playParams: {id: 'i.four', catalogId: '104'}}}]};
        };
        const moved = await bridge.play('album', 'l.alb1', {startWith: 1, startId: 'i.four'});
        const kept = await bridge.play('album', 'l.alb1', {startWith: 1, startId: 'i.three'});
        const catalog = await bridge.play('album', 'l.alb1', {startWith: 0, startId: '104'});
        const missing = await bridge.play('album', 'l.alb1', {startWith: 1, startId: 'i.gone'});
        const plain = await bridge.play('album', 'l.alb1', {startWith: 1});
        return {answers: [moved, kept, catalog, missing, plain],
                changes: mk.calls.filter(call => call[0] === 'changeToMediaAtIndex')};
    """)
    def test_play_starts_at_the_rows_own_item(self, value):
        self.assertEqual(value['answers'], [
            {'ok': True, 'moved': {'from': 1, 'to': 2}},
            {'ok': True},
            {'ok': True, 'moved': {'from': 0, 'to': 2}},
            {'ok': True},   # not in the queue: where startWith put it
            {'ok': True},
        ])
        self.assertEqual(value['changes'], [['changeToMediaAtIndex', 2]] * 2)

    @scenario("""
        mk.nowPlayingItem = {id: 'i.video1', type: 'library-music-videos',
                             attributes: {name: 'Harbour Lights (Live)'}};
        const video = bridge.nowPlaying().track;
        mk.nowPlayingItem = {id: 'ra.1', type: 'stations', attributes: {name: 'Harbour Radio'}};
        const station = bridge.nowPlaying().track;
        mk.nowPlayingItem = {id: '1', attributes: {name: 'No type'}};
        return [video.type, station.type, bridge.nowPlaying().track.type];
    """)
    def test_a_track_carries_its_type(self, value):
        self.assertEqual(value, ['library-music-videos', 'stations', ''])

    @scenario("""
        mk.playbackState = 2;
        mk.isPlaying = true;
        mk.nowPlayingItem = SONG;
        mk.nowPlayingItemIndex = 0;
        mk.currentPlaybackTime = 12;
        mk.currentPlaybackDuration = 216;
        const playing = bridge.nowPlaying();
        mk.isPlaying = false;   // as while an item loads
        mk.playbackState = 1;
        const loading = bridge.nowPlaying().state;
        mk.playbackState = 5;
        const ended = bridge.nowPlaying().state;
        delete mk.playbackState;   // an instance without one: the coarse guess
        const coarse = [bridge.nowPlaying().state];
        mk.nowPlayingItem = null;
        coarse.push(bridge.nowPlaying().state);
        mk.isPlaying = true;
        coarse.push(bridge.nowPlaying().state);
        delete window.MusicKit;
        return {playing, loading, ended, coarse, none: bridge.nowPlaying()};
    """)
    def test_now_playing(self, value):
        self.assertEqual((value['loading'], value['ended']), ('loading', 'ended'))
        self.assertEqual(value['coarse'], ['paused', 'stopped', 'playing'])
        playing = value['playing']
        self.assertEqual((playing['state'], playing['track']['id'], playing['position'],
                          playing['duration'], playing['shuffle'], playing['repeat']),
                         ('playing', 'i.song1', 12, 216, 'off', 'none'))
        self.assertEqual(value['none'], {'state': 'stopped', 'track': None, 'position': 0,
                                         'duration': 0, 'shuffle': 'off', 'repeat': 'none',
                                         'volume': 1})

    # -- lyrics ----------------------------------------------------------------------------

    @scenario("""
        const path = '/v1/catalog/gb/songs/1000000001/lyrics';
        mk.apiAnswers[path] = {data: [{id: '1000000001', attributes: {ttml: TTML}}]};
        const synced = await bridge.lyrics('1000000001');
        mk.apiAnswers[path] = {data: [{attributes: {
            ttml: '<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p>One</p><p>Two</p>'
                  + '</div></body></tt>'}}]};
        const unsynced = await bridge.lyrics('1000000001');
        mk.apiAnswers[path] = {data: [{attributes: {ttml: '<tt><p>Rock & Roll</p></tt>'}}]};
        const unreadable = await bridge.lyrics('1000000001');
        mk.apiAnswers[path] = {data: []};
        const none = await bridge.lyrics('1000000001');
        delete mk.apiAnswers[path];   // Apple refuses: no lyrics either
        return {synced, unsynced, unreadable, none, refused: await bridge.lyrics('1000000001')};
    """)
    def test_lyrics_from_ttml(self, value):
        self.assertEqual(value['synced'], {'synced': True, 'lines': [
            {'startMs': 500, 'endMs': 4250, 'text': 'Rock & Roll \u2019til dawn',
             'stanza': True},
            {'startMs': 4500, 'endMs': 8000, 'text': 'Harbour lights'},
            {'startMs': 20000, 'endMs': 25100, 'text': 'Out on the water', 'stanza': True},
            {'startMs': 3601500, 'endMs': 3603000, 'text': 'Fish & chips <tonight>'}]})
        self.assertEqual(value['unsynced'], {'synced': False, 'lines': [
            {'startMs': 0, 'endMs': 0, 'text': 'One', 'stanza': True},
            {'startMs': 0, 'endMs': 0, 'text': 'Two'}]})
        for key in ('unreadable', 'none', 'refused'):
            self.assertEqual(value[key], {'synced': False, 'lines': []}, key)

    # -- search ----------------------------------------------------------------------------

    @scenario("""
        mk.apiAnswers['/v1/catalog/gb/search'] = {results: {}};
        mk.apiAnswers['/v1/catalog/gb/search/suggestions'] = {results: {}};
        const search = await bridge.search('harbour', 5);
        const suggest = await bridge.suggest('harb');
        return {search, suggest, calls: mk.calls};
    """)
    def test_search_and_suggest_ask_the_catalog(self, value):
        self.assertEqual(value['search'], {'results': {}})
        self.assertEqual(value['calls'], [
            ['api.music', '/v1/catalog/gb/search',
             {'term': 'harbour', 'types': 'albums,artists,music-videos,playlists,songs,stations',
              'limit': 5, 'with': 'topResults'}],
            ['api.music', '/v1/catalog/gb/search/suggestions',
             {'term': 'harb', 'kinds': 'terms,topResults',
              'types': 'albums,artists,music-videos,playlists,songs,stations', 'limit': 10}]])

    # -- writes ----------------------------------------------------------------------------

    @scenario("""
        await bridge.rating('library-song', 'i.song1', true);
        await bridge.rating('album', '1000000002', false);
        await bridge.addToLibrary('music-video', '1000000009');
        await bridge.addToPlaylist('p.pl1', 'i.song1', 'library-songs');
        mk.writeStatus = 403;
        let refused = null;
        try { await bridge.rating('song', '1', true); } catch (error) { refused = String(error); }
        return {calls: mk.calls, refused};
    """)
    def test_writes_go_through_the_request_builder(self, value):
        self.assertEqual(value['calls'][:4], [
            ['request', '/v1/me/ratings/library-songs/i.song1',
             {'params': {}, 'method': 'PUT', 'body': {'type': 'ratings',
                                                      'attributes': {'value': 1}}}],
            ['request', '/v1/me/ratings/albums/1000000002', {'params': {}, 'method': 'DELETE'}],
            ['request', '/v1/me/library', {'params': {'ids[music-videos]': '1000000009'},
                                           'method': 'POST'}],
            ['request', '/v1/me/library/playlists/p.pl1/tracks',
             {'params': {}, 'method': 'POST',
              'body': {'data': [{'id': 'i.song1', 'type': 'library-songs'}]}}],
        ])
        self.assertIn('HTTP 403', value['refused'])


if __name__ == '__main__':
    unittest.main()
