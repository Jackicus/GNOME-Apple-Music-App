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
            'id': 'i.song1', 'catalogId': '1000000001', 'title': 'Harbour Lights',
            'artist': 'The Invented Band', 'album': 'Tidewater', 'trackNumber': 3,
            'discNumber': 1, 'durationMs': 216000, 'durationLabel': '3:36', 'explicit': True,
            'artUrl': 'https://example.invalid/256x256bb.jpg', 'index': 1})

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

    # -- playback --------------------------------------------------------------------------

    @scenario("""
        await bridge.play('album', 'l.alb1', {startWith: 2});
        await bridge.play('playlist', 'p.pl1');
        await bridge.play('station', 'ra.1');
        await bridge.play('musicVideo', '1000000009');
        mk.apiAnswers['/v1/catalog/gb/artists/42/view/top-songs'] = {data: [{id: '1'}, {id: '2'}]};
        await bridge.play('artist', '42');
        await bridge.play('artist', '43');   // no top songs: the artist's station
        return mk.calls.filter(call => call[0] === 'setQueue').map(call => call[1]);
    """)
    def test_play_queues_each_kind(self, value):
        self.assertEqual(value, [
            {'startWith': 2, 'startPlaying': True, 'album': 'l.alb1'},
            {'startWith': 0, 'startPlaying': True, 'playlist': 'p.pl1'},
            {'startWith': 0, 'startPlaying': True, 'station': 'ra.1'},
            {'startWith': 0, 'startPlaying': True, 'musicVideo': '1000000009'},
            {'startWith': 0, 'startPlaying': True, 'songs': ['1', '2']},
            {'startWith': 0, 'startPlaying': True, 'station': '43'},
        ])

    @scenario("""
        mk.playbackState = 2;
        mk.isPlaying = true;
        mk.nowPlayingItem = SONG;
        mk.nowPlayingItemIndex = 0;
        mk.currentPlaybackTime = 12;
        mk.currentPlaybackDuration = 216;
        const playing = bridge.nowPlaying();
        delete window.MusicKit;
        return {playing, none: bridge.nowPlaying()};
    """)
    def test_now_playing(self, value):
        playing = value['playing']
        self.assertEqual((playing['state'], playing['track']['id'], playing['position'],
                          playing['duration'], playing['shuffle'], playing['repeat']),
                         ('playing', 'i.song1', 12, 216, 'off', 'none'))
        self.assertEqual(value['none'], {'state': 'stopped', 'track': None, 'position': 0,
                                         'duration': 0, 'shuffle': 'off', 'repeat': 'none',
                                         'volume': 1})

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
