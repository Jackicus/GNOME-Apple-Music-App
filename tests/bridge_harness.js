// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Jack Tully

// A stand-in page for src/backend/bridge.js, run under gjs by tests/test_bridge.py.
//
// `window` is the global object, as in the page. `MusicKit` is a fake whose instance records
// every call the bridge makes on it (`mk.calls`) and answers from what a scenario sets on it;
// `document` answers the selectors a scenario gives it; `DOMParser` is a small XML parser,
// enough for Apple's TTML lyrics (elements, attributes, text, CDATA, the five XML entities and
// character references; anything else is a <parsererror> document, as in a browser).
//
// test_bridge.py appends the bridge's source as BRIDGE and the scenarios as SCENARIOS, then
// calls run(): each scenario gets a fresh page (a new instance, the bridge injected), and
// their outcomes are printed as one JSON object, {name: {value} or {error}}.

globalThis.window = globalThis;

// MusicKit.PlaybackStates as MusicKit v3 numbers them, both ways (0 -> 'none', 'none' -> 0).
const PLAYBACK_STATES = {
    none: 0, loading: 1, playing: 2, paused: 3, stopped: 4, ended: 5, seeking: 6,
    waiting: 8, stalled: 9, completed: 10,
};

function bothWays(names) {
    const map = {};
    for (const [name, number] of Object.entries(names)) {
        map[name] = number;
        map[number] = name;
    }
    return map;
}

class FakeInstance {
    constructor() {
        this.calls = [];        // [method, ...args] of everything the bridge asked
        this.listeners = {};    // event name -> [handler]
        this.failures = {};     // method -> what it rejects with
        this.apiAnswers = {};   // path -> the body mk.api.music(path) answers
        this.writeStatus = 204; // what a write's request answers
        this.isAuthorized = false;
        this.storefrontId = 'gb';
        this.bitrate = 256;
        this.playbackState = 0;
        this.isPlaying = false;
        this.currentPlaybackTime = 0;
        this.currentPlaybackDuration = 0;
        this.shuffleMode = 0;
        this.repeatMode = 0;
        this.volume = 1;
        this.nowPlayingItem = null;
        this.nowPlayingItemIndex = -1;
        this.queue = { items: [], position: -1 };
        const instance = this;
        this.api = {
            async music(path, params) {
                instance.calls.push(['api.music', path, params || {}]);
                if (!(path in instance.apiAnswers)) throw new Error('404 Not Found: ' + path);
                return { data: instance.apiAnswers[path] };
            },
            client: {
                createRequest(path, options) {
                    return {
                        async send() {
                            instance.calls.push(['request', path, options]);
                            const status = instance.writeStatus;
                            return {
                                ok: status < 400, status: status, statusText: '',
                                async text() { return ''; },
                            };
                        },
                    };
                },
            },
        };
    }

    addEventListener(name, handler) {
        (this.listeners[name] = this.listeners[name] || []).push(handler);
    }

    removeEventListener(name, handler) {
        this.listeners[name] = (this.listeners[name] || []).filter(h => h !== handler);
    }

    // MusicKit firing `name` at every listener.
    fire(name, event) {
        for (const handler of (this.listeners[name] || []).slice())
            handler(event);
    }

    // How many listeners each event has, the events without any left out.
    listenerCounts() {
        const counts = {};
        for (const [name, handlers] of Object.entries(this.listeners)) {
            if (handlers.length)
                counts[name] = handlers.length;
        }
        return counts;
    }

    async _call(method, args) {
        this.calls.push([method, ...args]);
        if (this.failures[method])
            throw this.failures[method];
    }

    setQueue(...args) { return this._call('setQueue', args); }
    play(...args) { return this._call('play', args); }
    pause(...args) { return this._call('pause', args); }
    stop(...args) { return this._call('stop', args); }
    skipToNextItem(...args) { return this._call('skipToNextItem', args); }
    skipToPreviousItem(...args) { return this._call('skipToPreviousItem', args); }
    seekToTime(...args) { return this._call('seekToTime', args); }
    playNext(...args) { return this._call('playNext', args); }
    playLater(...args) { return this._call('playLater', args); }
    changeToMediaAtIndex(...args) { return this._call('changeToMediaAtIndex', args); }
    authorize(...args) { return this._call('authorize', args); }
    unauthorize(...args) { return this._call('unauthorize', args); }
}

// The page's MusicKit global over `instance`.
function fakeMusicKit(instance) {
    return {
        PlaybackStates: bothWays(PLAYBACK_STATES),
        PlayerShuffleMode: bothWays({ off: 0, songs: 1 }),
        getInstance() { return instance; },
    };
}

// The page's document: `elements` maps a selector to the elements it finds ({textContent}).
const page = { elements: {} };
globalThis.document = {
    querySelectorAll(selectors) {
        const found = [];
        for (const selector of selectors.split(','))
            found.push(...(page.elements[selector.trim()] || []));
        return found;
    },
    querySelector(selectors) {
        return this.querySelectorAll(selectors)[0] || null;
    },
};

// -- DOMParser ---------------------------------------------------------------------------------

const XML_ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };

function decodeXml(text) {
    return text.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[A-Za-z]+);|&/g, (whole, name) => {
        if (name === undefined)
            throw new Error('a bare &');
        if (name[0] === '#') {
            const hex = name[1] === 'x';
            return String.fromCodePoint(parseInt(name.slice(hex ? 2 : 1), hex ? 16 : 10));
        }
        if (!(name in XML_ENTITIES))
            throw new Error('undefined entity ' + whole);
        return XML_ENTITIES[name];
    });
}

class XmlNode {
    constructor(nodeType, nodeName, nodeValue) {
        this.nodeType = nodeType;     // 1 element, 3 text, 4 CDATA, 9 document
        this.nodeName = nodeName;
        this.nodeValue = nodeValue === undefined ? null : nodeValue;
        this.childNodes = [];
        this.parentNode = null;
        this.attributes = {};
    }

    get localName() {
        return this.nodeType === 1 ? this.nodeName.split(':').pop() : null;
    }

    get tagName() {
        return this.nodeName;
    }

    get textContent() {
        if (this.nodeType === 3 || this.nodeType === 4)
            return this.nodeValue;
        return this.childNodes.map(child => child.textContent).join('');
    }

    append(child) {
        child.parentNode = this;
        this.childNodes.push(child);
    }

    getAttribute(name) {
        return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null;
    }

    _elements(match) {
        const found = [];
        const walk = node => {
            for (const child of node.childNodes) {
                if (child.nodeType !== 1)
                    continue;
                if (match(child))
                    found.push(child);
                walk(child);
            }
        };
        walk(this);
        return found;
    }

    getElementsByTagName(name) {
        return this._elements(element => name === '*' || element.nodeName === name);
    }

    // Namespaces are not modelled: any namespace, matched by the local name.
    getElementsByTagNameNS(_namespace, name) {
        return this._elements(element => name === '*' || element.localName === name);
    }
}

const XML_TOKEN = new RegExp([
    /<!\[CDATA\[([\s\S]*?)\]\]>/.source,                         // 1 CDATA
    /<!--[\s\S]*?-->|<\?[\s\S]*?\?>/.source,                     // a comment, a declaration
    /<\/([^\s>]+)\s*>/.source,                                   // 2 a closing tag
    /<([^\s/>!?]+)((?:\s+[^\s=/>]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>/.source,  // 3-5 a tag
    /([^<]+)/.source,                                            // 6 text
].join('|'), 'g');

globalThis.DOMParser = class DOMParser {
    parseFromString(text, _type) {
        try {
            return this._parse(String(text));
        } catch (error) {
            // As a browser answers XML it cannot read: a document with a <parsererror>.
            const failed = new XmlNode(9, '#document');
            const root = new XmlNode(1, 'parsererror');
            root.append(new XmlNode(3, '#text', String(error.message)));
            failed.append(root);
            return failed;
        }
    }

    _parse(text) {
        const document = new XmlNode(9, '#document');
        const open = [document];
        let at = 0;
        XML_TOKEN.lastIndex = 0;
        let match;
        while ((match = XML_TOKEN.exec(text)) !== null) {
            if (match.index !== at)
                throw new Error('not well-formed at ' + at);
            at = XML_TOKEN.lastIndex;
            const parent = open[open.length - 1];
            if (match[1] !== undefined) {
                parent.append(new XmlNode(4, '#cdata-section', match[1]));
            } else if (match[2] !== undefined) {
                if (open.length < 2 || parent.nodeName !== match[2])
                    throw new Error('</' + match[2] + '> closes nothing');
                open.pop();
            } else if (match[3] !== undefined) {
                const element = new XmlNode(1, match[3]);
                for (const attribute of match[4].matchAll(/([^\s=]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g))
                    element.attributes[attribute[1]] = decodeXml(attribute[2] ?? attribute[3]);
                parent.append(element);
                if (!match[5])
                    open.push(element);
            } else if (match[6] !== undefined) {
                if (open.length > 1)
                    parent.append(new XmlNode(3, '#text', decodeXml(match[6])));
                else if (match[6].trim())
                    throw new Error('text outside the root element');
            }
        }
        if (at !== text.length || open.length !== 1)
            throw new Error('the document ends inside an element');
        return document;
    }
};

// -- running the scenarios ---------------------------------------------------------------------

const posted = [];  // the events the bridge posted through the binding, parsed

// A page with a fresh MusicKit instance, the bridge at `version` injected into it (as
// client.load_bridge's source is: its version set, then the file evaluated globally).
function newPage(version) {
    delete window.__appleMusicLibrary;
    delete window.__appleMusicListeners;
    posted.length = 0;
    page.elements = {};
    const mk = new FakeInstance();
    window.MusicKit = fakeMusicKit(mk);
    window.__amEvent = json => posted.push(JSON.parse(json));
    inject(version || 'v1');
    return mk;
}

function inject(version) {
    window.__appleMusicLibraryWanted = version;
    (0, eval)(BRIDGE);  // eslint-disable-line no-eval
}

async function run() {
    const outcomes = {};
    for (const [name, scenario] of Object.entries(SCENARIOS)) {
        try {
            const mk = newPage();
            const value = await scenario({ bridge: window.__appleMusicLibrary, mk, posted, page });
            // As JSON now: the next page empties `posted`, which a scenario may return.
            outcomes[name] = JSON.parse(JSON.stringify({ value: value === undefined ? null : value }));
        } catch (error) {
            outcomes[name] = { error: String(error && error.stack ? `${error}\n${error.stack}` : error) };
        }
    }
    print(JSON.stringify(outcomes));
}
