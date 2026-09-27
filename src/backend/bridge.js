/**
 * Page-side helper injected into music.apple.com to bridge MusicKit JS with am.py.
 * Defines window.__appleMusicLibrary idempotently.
 */

(function () {
    // am.py sets __appleMusicLibraryWanted to a hash of this file before injecting,
    // so an engine left running across an update picks up the new bridge
    // instead of keeping the one it was first given.
    if (window.__appleMusicLibrary && window.__appleMusicLibrary.__version === window.__appleMusicLibraryWanted) {
        return;
    }

    function getMusicKit() {
        if (typeof window.MusicKit === 'undefined') {
            return null;
        }
        try {
            return window.MusicKit.getInstance();
        } catch {
            return null;
        }
    }

    // MusicKit v3's `mk.api.music()` resolves to its own request wrapper
    // (`{url, status, statusText, text, json, data}`), where `.data` is the
    // actual Apple Music API response body (`{data: [...]}` for resource
    // endpoints, `{results: {...}}` for search). Every caller in this file
    // wants that body, not the wrapper, so unwrap it in one place.
    async function apiCall(path, params, options) {
        const mk = getMusicKit();
        if (!mk) throw new Error('MusicKit not initialized');
        const wrapped = await mk.api.music(path, params || {}, options || {});
        return (wrapped && typeof wrapped.data !== 'undefined') ? wrapped.data : wrapped;
    }

    // Apple answers library writes with 202 Accepted or 204 No Content and
    // an empty body, and `music()` parses every body as JSON before handing
    // it back: a write that worked throws "Unexpected end of JSON input",
    // while one Apple refused (a 4xx with `{"errors": [...]}`) comes back
    // looking fine. Writes go through MusicKit's own request builder
    // instead, which signs the request the same way but leaves the body
    // alone, and judge the outcome by the status. `body` is a plain object:
    // the builder serializes it and sets the JSON content type, which a
    // pre-stringified body would not get.
    async function apiWrite(path, params, options) {
        const mk = getMusicKit();
        if (!mk) throw new Error('MusicKit not initialized');
        const client = mk.api && mk.api.client;
        if (!client || typeof client.createRequest !== 'function') {
            throw new Error('MusicKit request client unavailable');
        }
        const res = await client.createRequest(path, {
            params: params || {},
            method: options.method,
            body: options.body
        }).send();
        if (!res.ok) throw new Error(await describeFailure(res));
        return { ok: true };
    }

    // "HTTP 403 Forbidden: No active subscription", or just the status line
    // when the body is not Apple's error shape.
    async function describeFailure(res) {
        let detail = res.statusText || '';
        try {
            const body = JSON.parse(await res.text());
            const err = body && body.errors && body.errors[0];
            if (err) detail = [err.title, err.detail].filter(Boolean).join(': ');
        } catch {
            // Not JSON; the status line is all there is.
        }
        return 'HTTP ' + res.status + (detail ? ' ' + detail : '');
    }

    function formatDuration(ms) {
        if (!ms || ms <= 0) return '0:00';
        const totalSec = Math.floor(ms / 1000);
        const sec = totalSec % 60;
        const min = Math.floor(totalSec / 60) % 60;
        const hr = Math.floor(totalSec / 3600);
        const secStr = sec < 10 ? '0' + sec : String(sec);
        if (hr > 0) {
            const minStr = min < 10 ? '0' + min : String(min);
            return hr + ':' + minStr + ':' + secStr;
        }
        return min + ':' + secStr;
    }

    function formatTrack(item, index) {
        if (!item) return null;
        const attrs = item.attributes || {};
        const id = item.id || '';
        const playParams = attrs.playParams || item.playParams || {};
        const catalogId = playParams.catalogId || item.catalogId || (id.startsWith('l.') || id.startsWith('i.') ? null : id) || null;
        // MusicKit v3 reports playbackDuration in milliseconds already (a 30 s preview is 30000).
        const durationMs = attrs.durationInMillis || Math.round(item.playbackDuration || 0);

        return {
            id: id,
            catalogId: catalogId,
            title: item.title || attrs.name || '',
            artist: item.artistName || attrs.artistName || '',
            album: item.albumName || attrs.albumName || '',
            trackNumber: item.trackNumber || attrs.trackNumber || 1,
            discNumber: item.discNumber || attrs.discNumber || 1,
            durationMs: durationMs,
            durationLabel: formatDuration(durationMs),
            explicit: attrs.contentRating === 'explicit',
            // Headless Chrome hands MPRIS its own logo as the art, so the
            // player takes the real cover from here instead.
            artUrl: (attrs.artwork && attrs.artwork.url)
                ? attrs.artwork.url.replace('{w}', '256').replace('{h}', '256') : null,
            index: typeof index === 'number' ? index : 0
        };
    }

    function shuffleModeToString(mode) {
        return mode === 1 ? 'on' : 'off';
    }

    function repeatModeToString(mode) {
        if (mode === 1) return 'one';
        if (mode === 2) return 'all';
        return 'none';
    }

    function parseTimeMs(timeStr) {
        if (!timeStr) return 0;
        timeStr = timeStr.trim();
        if (timeStr.endsWith('s')) {
            return Math.round(parseFloat(timeStr.slice(0, -1)) * 1000);
        }
        const parts = timeStr.split(':');
        if (parts.length === 2) {
            return Math.round(parseFloat(parts[0]) * 60000 + parseFloat(parts[1]) * 1000);
        }
        if (parts.length === 3) {
            return Math.round(parseFloat(parts[0]) * 3600000 + parseFloat(parts[1]) * 60000 + parseFloat(parts[2]) * 1000);
        }
        return Math.round(parseFloat(timeStr) * 1000);
    }

    function parseTtmlLyrics(ttml) {
        const lines = [];
        const pRegex = /<p\b([^>]*)>([\s\S]*?)<\/p>/gi;
        let match;
        let synced = false;
        while ((match = pRegex.exec(ttml)) !== null) {
            const attrsStr = match[1];
            const rawText = match[2].replace(/<[^>]+>/g, '').trim();
            if (!rawText) continue;

            const beginMatch = attrsStr.match(/begin="([^"]+)"/i);
            const endMatch = attrsStr.match(/end="([^"]+)"/i);
            const startMs = beginMatch ? parseTimeMs(beginMatch[1]) : 0;
            const endMs = endMatch ? parseTimeMs(endMatch[1]) : 0;
            if (beginMatch) synced = true;

            lines.push({
                startMs: startMs,
                endMs: endMs,
                text: rawText
            });
        }
        return { synced: synced, lines: lines };
    }

    // The catalog search and its autocomplete, each on its own so that one
    // call from am.py can run both at once (searchAndSuggest) or either
    // alone (search, suggest).
    async function doSearch(term, isLibrary, limit) {
        const mk = getMusicKit();
        if (!mk) throw new Error('MusicKit not initialized');
        const sf = mk.storefrontId || 'us';
        const types = isLibrary
            ? 'library-albums,library-artists,library-playlists,library-songs'
            : 'albums,artists,music-videos,playlists,songs,stations';
        const path = isLibrary ? '/v1/me/library/search' : `/v1/catalog/${sf}/search`;
        const params = { term: term, types: types, limit: limit || 20 };
        if (!isLibrary) params.with = 'topResults';
        return await apiCall(path, params);
    }

    async function doSuggest(term, limit) {
        const mk = getMusicKit();
        if (!mk) throw new Error('MusicKit not initialized');
        const sf = mk.storefrontId || 'us';
        return await apiCall(`/v1/catalog/${sf}/search/suggestions`, {
            term: term,
            kinds: 'terms,topResults',
            types: 'albums,artists,music-videos,playlists,songs,stations',
            limit: limit || 10
        });
    }

    function queueSnapshot(mk) {
        if (!mk || !mk.queue) {
            return { index: 0, items: [] };
        }
        const items = (mk.queue.items || []).map(function (it, idx) {
            return formatTrack(it, idx);
        });
        return {
            index: mk.queue.position || 0,
            items: items
        };
    }

    // MusicKit numbers its playback states; MusicKit.PlaybackStates maps both
    // ways (0 -> 'none', 'none' -> 0), so the name is one lookup.
    function playbackStateName(state) {
        const states = window.MusicKit && window.MusicKit.PlaybackStates;
        if (states && typeof state === 'number' && typeof states[state] === 'string') {
            return states[state];
        }
        return typeof state === 'string' ? state : 'none';
    }

    function describeError(err) {
        if (!err) return 'unknown error';
        if (typeof err === 'string') return err;
        return String(err.message || err.description || err.name || err);
    }

    // The MusicKit events the app follows, each with the plain data it is
    // posted with — the shapes the answers below use (nowPlaying, queue),
    // read from the instance at the moment of the event rather than from
    // the event object, whose shape has varied between MusicKit versions.
    // The playback state is its PlaybackStates name ('playing', 'paused',
    // 'stopped', 'loading', 'ended', 'seeking', 'waiting', 'stalled'…).
    const EVENT_DATA = {
        authorizationStatusDidChange: function (mk, e) {
            return {
                authorized: !!mk.isAuthorized,
                status: (e && typeof e.authorizationStatus === 'number') ? e.authorizationStatus : null
            };
        },
        playbackStateDidChange: function (mk, e) {
            const state = (e && typeof e.state !== 'undefined') ? e.state : mk.playbackState;
            return {
                state: playbackStateName(state),
                position: mk.currentPlaybackTime || 0,
                duration: mk.currentPlaybackDuration || 0
            };
        },
        nowPlayingItemDidChange: function (mk) {
            const item = mk.nowPlayingItem;
            const index = mk.nowPlayingItemIndex ?? 0;
            return { track: item ? formatTrack(item, index) : null, index: index };
        },
        playbackTimeDidChange: function (mk) {
            return {
                position: mk.currentPlaybackTime || 0,
                duration: mk.currentPlaybackDuration || 0
            };
        },
        playbackDurationDidChange: function (mk) {
            return { duration: mk.currentPlaybackDuration || 0 };
        },
        queueItemsDidChange: function (mk) {
            return queueSnapshot(mk);
        },
        queuePositionDidChange: function (mk, e) {
            return {
                index: (e && typeof e.position === 'number') ? e.position : (mk.queue ? mk.queue.position || 0 : 0),
                oldIndex: (e && typeof e.oldPosition === 'number') ? e.oldPosition : null
            };
        },
        shuffleModeDidChange: function (mk) {
            return { shuffle: shuffleModeToString(mk.shuffleMode) };
        },
        repeatModeDidChange: function (mk) {
            return { repeat: repeatModeToString(mk.repeatMode) };
        },
        playbackVolumeDidChange: function (mk) {
            return { volume: typeof mk.volume === 'number' ? mk.volume : 1 };
        },
        mediaPlaybackError: function (mk, e) {
            return { message: describeError(e) };
        }
    };

    // One event to the app: window.__amEvent is the CDP binding the client
    // registers (Runtime.addBinding), which takes one string.
    function postEvent(name, event) {
        if (typeof window.__amEvent !== 'function') return;
        let data = null;
        try {
            const mk = getMusicKit();
            data = mk ? EVENT_DATA[name](mk, event) : null;
        } catch (err) {
            data = { error: describeError(err) };
        }
        try {
            window.__amEvent(JSON.stringify({ name: name, data: data }));
        } catch {
            // The binding is gone with the connection; nothing to tell.
        }
    }

    function detachListeners(listeners) {
        Object.keys(listeners.handlers).forEach(function (name) {
            try {
                listeners.mk.removeEventListener(name, listeners.handlers[name]);
            } catch {
                // The instance may be gone; the handlers with it.
            }
        });
        window.__appleMusicListeners = null;
    }

    window.__appleMusicLibrary = {
        __version: window.__appleMusicLibraryWanted,
        status: function () {
            const mk = getMusicKit();
            return {
                ready: !!(mk && typeof mk.isAuthorized !== 'undefined'),
                engine: true,
                authorized: !!(mk && mk.isAuthorized),
                storefront: (mk && mk.storefrontId) ? mk.storefrontId : 'us',
                bitrate: (mk && typeof mk.bitrate === 'number') ? mk.bitrate : 256
            };
        },

        signin: async function () {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            await mk.authorize();
            return { authorized: !!mk.isAuthorized };
        },

        api: async function (path, params, options) {
            return await apiCall(path, params, options);
        },

        // Several GET requests at once; a failed one is null in its place.
        apiAll: async function (paths) {
            return await Promise.all((paths || []).map(function (path) {
                return apiCall(path).catch(function () { return null; });
            }));
        },

        play: async function (kind, id, options) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            options = options || {};
            const startWith = options.startWith !== undefined ? Number(options.startWith) : 0;
            const shuffle = !!options.shuffle;

            if (shuffle) {
                if (window.MusicKit && window.MusicKit.PlayerShuffleMode) {
                    mk.shuffleMode = window.MusicKit.PlayerShuffleMode.songs;
                } else {
                    mk.shuffleMode = 1;
                }
            }

            const queueObj = { startWith: startWith, startPlaying: true };
            if (kind === 'album') {
                queueObj.album = id;
            } else if (kind === 'playlist') {
                queueObj.playlist = id;
            } else if (kind === 'station') {
                queueObj.station = id;
            } else if (kind === 'song') {
                queueObj.song = id;
            } else if (kind === 'artist') {
                try {
                    const sf = mk.storefrontId || 'us';
                    const isLib = id.startsWith('l.') || id.startsWith('r.');
                    const endpoint = isLib
                        ? `/v1/me/library/artists/${id}/view/top-songs`
                        : `/v1/catalog/${sf}/artists/${id}/view/top-songs`;
                    const res = await apiCall(endpoint, { limit: 100 });
                    if (res && res.data && res.data.length > 0) {
                        queueObj.songs = res.data.map(function (s) { return s.id; });
                    } else {
                        queueObj.station = id;
                    }
                } catch {
                    queueObj.station = id;
                }
            } else {
                queueObj[kind] = id;
            }

            await mk.setQueue(queueObj);
            await mk.play();
            return { ok: true };
        },

        playNext: async function (kind, id) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            const q = {};
            q[kind === 'song' ? 'song' : kind] = id;
            await mk.playNext(q);
            return { ok: true };
        },

        playLater: async function (kind, id) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            const q = {};
            q[kind === 'song' ? 'song' : kind] = id;
            await mk.playLater(q);
            return { ok: true };
        },

        control: async function (action) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            switch (action) {
                case 'play':
                    await mk.play();
                    break;
                case 'pause':
                    await mk.pause();
                    break;
                case 'toggle':
                    if (mk.isPlaying) await mk.pause();
                    else await mk.play();
                    break;
                case 'next':
                    await mk.skipToNextItem();
                    break;
                case 'previous':
                    await mk.skipToPreviousItem();
                    break;
                case 'stop':
                    await mk.stop();
                    break;
                default:
                    throw new Error('Unknown control action: ' + action);
            }
            return { ok: true };
        },

        seek: async function (sec) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            await mk.seekToTime(Number(sec));
            return { ok: true };
        },

        // Answers with the level as MusicKit has it after the set. Apple's
        // page keeps it across restarts itself; a nought is a mute.
        volume: async function (val) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            mk.volume = Math.max(0, Math.min(1, Number(val) || 0));
            return { volume: typeof mk.volume === 'number' ? mk.volume : 1 };
        },

        shuffle: async function (mode) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            if (mode === 'on') {
                mk.shuffleMode = 1;
            } else if (mode === 'off') {
                mk.shuffleMode = 0;
            } else if (mode === 'toggle') {
                mk.shuffleMode = mk.shuffleMode === 1 ? 0 : 1;
            }
            return {
                shuffle: shuffleModeToString(mk.shuffleMode),
                repeat: repeatModeToString(mk.repeatMode)
            };
        },

        repeat: async function (mode) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            if (mode === 'none') {
                mk.repeatMode = 0;
            } else if (mode === 'one') {
                mk.repeatMode = 1;
            } else if (mode === 'all') {
                mk.repeatMode = 2;
            } else if (mode === 'cycle') {
                mk.repeatMode = ((mk.repeatMode || 0) + 1) % 3;
            }
            return {
                shuffle: shuffleModeToString(mk.shuffleMode),
                repeat: repeatModeToString(mk.repeatMode)
            };
        },

        nowPlaying: function () {
            const mk = getMusicKit();
            if (!mk) {
                return {
                    state: 'stopped',
                    track: null,
                    position: 0,
                    duration: 0,
                    shuffle: 'off',
                    repeat: 'none',
                    volume: 1
                };
            }
            const item = mk.nowPlayingItem;
            const track = item ? formatTrack(item, mk.nowPlayingItemIndex ?? 0) : null;
            let state = 'stopped';
            if (mk.isPlaying) {
                state = 'playing';
            } else if (track) {
                state = 'paused';
            }
            return {
                state: state,
                track: track,
                position: mk.currentPlaybackTime || 0,
                duration: mk.currentPlaybackDuration || 0,
                shuffle: shuffleModeToString(mk.shuffleMode),
                repeat: repeatModeToString(mk.repeatMode),
                volume: typeof mk.volume === 'number' ? mk.volume : 1
            };
        },

        queue: function () {
            return queueSnapshot(getMusicKit());
        },

        rating: async function (kind, id, love) {
            const path = '/v1/me/ratings/' + kind + 's/' + id;
            if (love) {
                return await apiWrite(path, {}, {
                    method: 'PUT',
                    body: { type: 'ratings', attributes: { value: 1 } }
                });
            }
            return await apiWrite(path, {}, { method: 'DELETE' });
        },

        addToLibrary: async function (kind, id) {
            const query = {};
            query['ids[' + kind + 's]'] = id;
            return await apiWrite('/v1/me/library', query, { method: 'POST' });
        },

        playlists: async function () {
            const res = await apiCall('/v1/me/library/playlists', { limit: 100 });
            const data = (res && res.data) ? res.data : [];
            const items = data
                .filter(function (p) { return p.attributes && p.attributes.canEdit !== false; })
                .map(function (p) {
                    return {
                        id: p.id,
                        title: p.attributes ? p.attributes.name : ''
                    };
                });
            return { items: items };
        },

        addToPlaylist: async function (playlistId, songId) {
            const path = '/v1/me/library/playlists/' + playlistId + '/tracks';
            return await apiWrite(path, {}, {
                method: 'POST',
                body: { data: [{ id: songId, type: 'songs' }] }
            });
        },

        lyrics: async function (catalogSongId) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            const sf = mk.storefrontId || 'us';
            const path = `/v1/catalog/${sf}/songs/${catalogSongId}/lyrics`;
            try {
                const res = await apiCall(path);
                const data = (res && res.data && res.data[0]) ? res.data[0] : null;
                if (!data || !data.attributes) {
                    return { synced: false, lines: [] };
                }
                const attrs = data.attributes;
                if (attrs.ttml) {
                    return parseTtmlLyrics(attrs.ttml);
                }
                return { synced: false, lines: [] };
            } catch {
                return { synced: false, lines: [] };
            }
        },

        // `limit` is per type. The catalog is also asked for its own pick of
        // the best few hits across every type (`with=topResults`, answered
        // as `results.top`), which is the "Top Results" shelf at the head
        // of Apple Music's own search page; the library's search has no
        // such thing.
        search: async function (term, isLibrary, limit) {
            return await doSearch(term, isLibrary, limit);
        },

        // Apple's own autocomplete for a term half typed: the few searches
        // it would complete it to (`kind: 'terms'`) and its best few hits
        // for it as it stands (`kind: 'topResults'`), which is what the
        // search box on music.apple.com drops down as it is typed into.
        suggest: async function (term, limit) {
            return await doSuggest(term, limit);
        },

        // Both at once, in one round trip from am.py: the search's answer
        // under `search`, the autocomplete's under `suggestions` — or null
        // there, since a search is not lost for want of its completions.
        searchAndSuggest: async function (term, isLibrary, limit, suggestLimit) {
            const results = await Promise.all([
                doSearch(term, isLibrary, limit),
                doSuggest(term, suggestLimit).catch(function () { return null; })
            ]);
            return { search: results[0], suggestions: results[1] };
        },

        // Apple Music's own search page before anything is typed: the
        // "Browse Categories" it offers (Rock, Hip-Hop, Chill, the
        // decades…), which are Apple's own curators, asked for the way
        // music.apple.com asks for them — a recommendation set by name.
        searchLanding: async function () {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            const sf = mk.storefrontId || 'us';
            return await apiCall(`/v1/recommendations/${sf}`, {
                name: 'search-landing',
                types: 'activities,apple-curators,editorial-items',
                extend: 'editorialArtwork',
                platform: 'web'
            });
        },

        // A category's page: the curator with its grouping, whose one tab
        // is a run of editorial elements — Best New Songs, New Releases,
        // Playlists, Stations… — each a shelf. Not with `omit[resource]`:
        // the shelves' contents are what Apple calls autos, and go with it.
        category: async function (id) {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            const sf = mk.storefrontId || 'us';
            return await apiCall(`/v1/catalog/${sf}/apple-curators/${encodeURIComponent(id)}`, {
                include: 'grouping',
                extend: 'editorialArtwork',
                platform: 'web'
            });
        },

        // Forward MusicKit's events (the keys of EVENT_DATA) to the app through
        // window.__amEvent as JSON {name, data}. Attaches the listeners once:
        // calling it again with the same bridge on the same instance is a
        // no-op, and a newer bridge replaces an older bridge's listeners
        // rather than adding to them. Requires MusicKit to be configured.
        subscribe: function () {
            const mk = getMusicKit();
            if (!mk) throw new Error('MusicKit not initialized');
            const current = window.__appleMusicListeners;
            if (current && current.version === window.__appleMusicLibraryWanted && current.mk === mk) {
                return { subscribed: true, events: Object.keys(current.handlers), attached: false };
            }
            if (current) detachListeners(current);
            const handlers = {};
            Object.keys(EVENT_DATA).forEach(function (name) {
                const handler = function (event) { postEvent(name, event); };
                mk.addEventListener(name, handler);
                handlers[name] = handler;
            });
            window.__appleMusicListeners = {
                version: window.__appleMusicLibraryWanted,
                mk: mk,
                handlers: handlers
            };
            return { subscribed: true, events: Object.keys(handlers), attached: true };
        },

        unsubscribe: function () {
            const current = window.__appleMusicListeners;
            if (current) detachListeners(current);
            return { subscribed: false };
        }
    };
})();
