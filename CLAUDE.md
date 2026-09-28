# Apple Music for GNOME

A native GNOME client for Apple Music. GTK 4.22, libadwaita 1.9, Python 3.14 with PyGObject 3.56,
Blueprint for UI, Meson, gettext. GPL-2.0-or-later. App ID `io.github.jackicus.AppleMusic`
(`.Devel` under `-Dprofile=development`); resource base path `/io/github/jackicus/AppleMusic`.
It should feel like a GNOME core app (Nautilus, Music, Settings) while laying out its pages like
the Apple Music web player. The phased plan, one ready-to-paste prompt per session, is in
`prompts.md`. This file is what every session needs regardless of phase: what exists and the rules.

## The one hard constraint

Apple Music streams are Widevine-protected. WebKitGTK cannot play them and there is no public
streaming API, so the app cannot embed music.apple.com in a WebView. The playback engine is Google
Chrome (the real build: it ships Widevine, Chromium does not), started by the app with a private
profile and `--remote-debugging-port` on 127.0.0.1, showing music.apple.com. The app drives Apple's
own MusicKit JS object in that page over the DevTools protocol (CDP) through an injected `bridge.js`.
Chrome is visible once for sign-in and headless (`--headless=new`) afterwards; audio comes out of
Chrome. Jack proved this in a GNOME Shell extension whose backend
(`~/Projects/GNOME-Extensions/GNOME-Apple-Music-Library/src/backend/`, README there) is vendored
into `src/backend/`. This app is one long-running process, so it keeps a single persistent,
asynchronous CDP connection instead of the extension's process-per-command.

## Architecture

Parts marked `*` exist only once their phase in `prompts.md` is done.

```
GTK main thread = GLib main loop = asyncio loop (gi.events.GLibEventLoopPolicy)
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Window: AdwBottomSheet (content: AdwToastOverlay > AdwNavigationSplitView;       │
│   bottom-bar: $AppleMusicPlayerBar, always shown, the content inset by its       │
│   height; sheet: $AppleMusicNowPlayingSheet, opened by the bar, a swipe and      │
│   app.now-playing; toasts go into it while it is open)                           │
│   sidebar: AdwSidebar: sections.py's fixed items; the Playlists section bound to │
│            a store of SidebarEntry (sidebar.py): All Playlists, Favourite Songs, │
│            then library.playlist_tree() depth first, folders collapsible         │
│   content: AdwNavigationView; a sidebar item replaces its stack with that        │
│            destination's root page (pages/, built on first visit and kept, each  │
│            with its own header bar): Home and Radio (shelves), GridPages, the    │
│            SongsPage (ColumnView), Favourite Songs and each sidebar playlist (a  │
│            DetailPage), each folder (a GridPage of its folders and playlists),   │
│            placeholders for the rest; tiles → window.open_item() pushes a        │
│            DetailPage (album, playlist), an ArtistPage or a folder's GridPage,   │
│            or plays a station; a shelf's See All →                               │
│            window.open_shelf() pushes a GridPage; track rows, Play, Shuffle,     │
│            stations → window.play_request(play, start_with, shuffle) →          │
│            app.player.play() (the engine started first if down; sign-in if out) │
│   PlayerBar (player_bar.py): transport (app.previous/play-pause/next), the item  │
│            playing with its artwork (Artwork.fetch_remote → remote-art/), seek   │
│            slider and times, a heart (engine.rating/love/unlove of the song),    │
│            shuffle, repeat, volume; follows app.player (the heart: `rated`)      │
│   window.item_actions (actions.py): win.item-* on a (kind, id) target; context   │
│            menus (widgets/context_menu.py) on tiles, rows, top hits and sidebar  │
│            playlists; track rows drag a TrackRef onto sidebar playlists          │
│   NowPlayingSheet (widgets/now_playing.py): 320 px artwork, titles, transport,   │
│            seek (widgets/transport.py, shared with the bar), then Lyrics         │
│            (widgets/lyrics.py: the current line by position, click seeks) and    │
│            Up Next (widgets/queue.py: player.queue, activation jumps) tabs      │
│ app.library: Item/Track GObjects in Gio.ListStores, loaded from library.json     │
│   (read and parsed in a thread from do_startup, held while the main thread runs  │
│   Python before present(); wrapped a section at a time; the Songs store on       │
│   request); --demo reads build/demo instead                                      │
│ Artwork (widgets/artwork.py): thumbnails decoded in threads at the size drawn    │
│   into a 32 MB LRU, asked for by tiles, rows and covers while they are on screen │
│ app.engine (engine.py): Chrome (Gio.Subprocess, headless after sign-in) + the    │
│   async CDPClient ─► bridge.js ─► MusicKit; start/stop/restart, status(),        │
│   item() (a shelf item's groups, on demand), signin(), account_name();           │
│   play/play_next/play_later/control/seek/volume/shuffle/repeat/now_playing/queue │
│   love/unlove/rating (`rated` signal)/add_to_library/playlists/add_to_playlist   │
│   MusicKit events ─► `event` signal ─► app.player (player.py: state, track,      │
│   position, duration, shuffle, repeat, volume, queue + queue_index, lyrics       │
│   (engine.lyrics() once per song, cached); never polled) ─► PlayerBar, the       │
│   sheet and app.mpris (mpris.py: org.mpris.MediaPlayer2.<app id> on the session  │
│   bus, the Shell's media controls and the media keys; its methods run the Player)│
│   Sign-in: dialogs/signin.py (visible Chrome, then headless); the account button │
│   and the "Sign in to see your library" banner follow the signed-in key          │
│   Preferences: dialogs/preferences.py (app.preferences, Ctrl+,), rows bound to   │
│   the settings; background playback: closing the window while playing hides it   │
│   and holds the app (Application.close_window) until MPRIS Raise, a relaunch or  │
│   playback stopping (then quit, engine stopped)                                  │
│ Blocking work (JSON parse, image decode, artwork HTTP) ─► asyncio.to_thread      │
└──────────────────────────────────────────────────────────────────────────────────┘
Disk: $XDG_CACHE_HOME/apple-music/{library.json, art/, thumb/, items/, remote-art/, lyrics/}
      $XDG_DATA_HOME/apple-music/chrome (the Chrome profile; chrome-devel for .Devel)
      engine.json (which Chrome is ours): $XDG_RUNTIME_DIR/apple-music/ for the release
      profile, inside the profile for any other                     GSettings: one schema
```

## Layout, and where new things go

```
meson.build, meson.options     project; -Dprofile=development → .Devel ID, version gets the git rev
src/apple-music.in             launcher configured by Meson: gettext, loads the gresource, main.main()
src/main.py                    Application: app.* actions (quit, about, shortcuts, preferences,
                               sign-in, sign-out, sync, now-playing; play-pause, next, previous,
                               shuffle, repeat, enabled while something plays), every
                               accelerator (shortcuts.ACCELS) and the Keyboard Shortcuts dialog
                               (shortcuts.sections(); its rows named for AT-SPI), GSettings (the
                               engine follows browser-command, engine-port and engine-headless:
                               _make_engine), logging and --debug, --demo
                               (app.demo), app.library, app.engine, app.player and app.mpris
                               (made in do_startup; the library read from there and awaited
                               in do_activate, the engine autostarted in do_activate; MPRIS
                               released in do_shutdown), mark(name) (startup timing, `marks`),
                               spawn(coro), toast(), report(error),
                               player_command(coro), start_sync()/sync_due() (the sync as a
                               task, one at a time, with the banner and the toasts),
                               show_preferences(page) (toasts go into Preferences while it is
                               open), clear_cache() (sync stopped, CACHE_ENTRIES removed in a
                               thread, the library emptied, last-sync forgotten, a sync when
                               signed in), background playback (close_window(window): hide and
                               hold() while playing and background-playback is on, else quit;
                               in_background; the window shown again releases; playback
                               stopped for BACKGROUND_GRACE (10 s) quits), the quit path (hold
                               released, sync cancelled, engine stopped first),
                               use_glib_event_loop()
src/sync.py                    sync_library(engine, library, progress): the whole sync (fetch
                               through the engine, normalise in a thread, thumbnails, library.json,
                               prune, library.reload()); the endpoints and the favourites tag
                               (and Apple's canEdit false, kept as attributes.canEdit);
                               install_scaler() (GdkPixbuf as the backend's scale_image);
                               when to sync: INTERVALS (1, 6, 24, 0 hours: Preferences'
                               choices), interval_index(hours), sync_due(stamp, hours, now),
                               last_sync_text(stamp, now) ("Last refreshed 3 hours ago")
src/engine.py                  Engine (GObject: state down/starting/up/signing-in, authorized,
                               headless, `event` signal): Chrome's lifecycle in the app
                               (start(visible=None): without a mode a running engine is kept
                               and a stopped one starts headless unless prefer_headless is
                               off; set_port(port) and browser_command apply at the next
                               start), the one CDPClient, the commands (status, api, api_pages,
                               api_all, item,
                               signin, account_name; playback: play(kind, id, start_with,
                               shuffle), play_next, play_later, control(action), seek(s),
                               volume(level), shuffle(mode), repeat(mode), now_playing(),
                               queue(), queue_jump(index), lyrics(catalog_song_id) (kept
                               under <cache>/lyrics/ when Apple had any; lyrics_answer()
                               shapes the answer); search(term, library, limit, suggest),
                               suggest(term, limit), and, kept for a day under the cache and
                               answered from there without the engine unless refresh=True:
                               landing() (landing.json), category(id) (categories/),
                               browse() (browse.json: the New page's editorial groupings),
                               made_for_you() (made-for-you.json: the recommendations made of
                               personal mixes and stations); the account's: love(kind, id),
                               unlove(kind, id), rating(kind, id) (1/0/-1; love, unlove and
                               rating emit `rated(kind, id, value)`), add_to_library(kind,
                               catalog id), playlists() (the editable ones), add_to_playlist(
                               playlist id, song id), catalog_url(kind, library id));
                               engine_paths(profile, port) (chrome-devel and port+1 for the
                               .Devel build), item_endpoint(), resource_type(kind, id) (the
                               API type: 'song', 'library-album'…; "i." ids are the library's);
                               the cache: CACHE_ENTRIES (library.json, art, thumb, remote-art,
                               items, lyrics, landing.json, categories, browse.json,
                               made-for-you.json; not library.lock), cache_size(path) and
                               clear_cache(path), both run in a thread
src/player.py                  Player (GObject, no GTK): state (MusicKit PlaybackStates name),
                               track (NowPlaying: id, catalog_id, title, artist, album,
                               duration_ms, artwork_url, index, explicit; or None), position,
                               duration, shuffle, repeat, volume, position_updated_at,
                               active, stopped (no item, or STOPPED_STATES: none, stopped,
                               ended, completed; paused is not), estimated_position(); queue
                               (a Gio.ListStore of
                               NowPlaying, from queueItemsDidChange and queue() when the item
                               playing is not in it) and queue_index; lyrics (a lyrics.Lyrics,
                               asked of engine.lyrics() once per catalog song as it starts)
                               and lyrics_loading; set only from the engine's events and one
                               now_playing() refresh() when it comes up (apply(dict), which
                               also takes `queue` and `lyrics`; apply_queue(snapshot));
                               commands play(play, start_with, shuffle) (starts a down engine
                               when signed in, else not-signed-in: ensure_engine(), which the
                               item actions use too), toggle, pause, resume, next,
                               previous, stop, queue_jump(index), seek, set_volume,
                               set_shuffle, toggle_shuffle, set_repeat, cycle_repeat, play_next,
                               play_later; `error` signal; format_time()
src/lyrics.py                  Lyrics(answer, catalog_id): synced, lines (Gio.ListStore of
                               LyricLine: start_ms, end_ms, text, in time order), text,
                               index_at(seconds) (the last line started, -1 before the first),
                               start_of(index); parse_lines(), line_index_at(); GObject only
src/mpris.py                   Mpris(app): the org.mpris.MediaPlayer2.<application id> service
                               (Gio.bus_own_name; /org/mpris/MediaPlayer2 registered from
                               INTROSPECTION_XML with register_object_with_closures2), both
                               interfaces read from app.player: PlaybackStatus, LoopStatus,
                               Shuffle, Volume, Position (estimated_position(), microseconds),
                               Metadata (metadata(track, art_path, duration): trackid from
                               track_path(id), length, artUrl once the remote art is cached,
                               title, artist as a list, album), the Can*s true with a track;
                               PropertiesChanged from the Player's notify signals with only
                               the keys that differ from what was last sent, Seeked after its
                               own seeks and on a position jump (SEEK_JUMP); the methods and
                               the writable properties run the Player through
                               app.player_command; Raise presents the window, Quit is
                               app.quit; start() in do_startup, stop() in do_shutdown; the
                               name lost is logged and the app runs on
src/dialogs/signin.py + .blp   $AppleMusicSignInDialog: the sign-in flow as a task while shown
src/dialogs/preferences.py + .blp  $AppleMusicPreferencesDialog (Adw.PreferencesDialog, pages
                               `general` and `engine`): background playback, the refresh
                               interval (a ComboRow over sync.INTERVALS, by hand; its subtitle
                               last_sync_text), Refresh Now (app.sync), Cache (size in a thread,
                               Clear asks, then app.clear_cache()); the engine's state with
                               Start/Stop (label bound to engine.state), browser command, DevTools
                               port (the .Devel/APPLE_MUSIC_PORT port named in the subtitle),
                               hidden, autostart, Sign Out (app.sign-out, by its `activated`
                               signal); BINDINGS are Gio.Settings.bind; everything outside the
                               dialog is let go on `closed`
src/library.py                 the model: Library (state empty/loading/ready, 'changed', stores
                               albums artists playlists radio videos, shelves, by_id, shelf,
                               favourite_songs(), track_at(play, index), playlist_tree(),
                               folder_items(id), load() (new objects) and reload() (in place,
                               by id): coroutines whose file is read, in the library's thread,
                               from the call; hold_reading()/resume_reading(); songs filled by
                               async build_songs(), songs-ready), Item
                               (favourites; editable (a library playlist songs can be added
                               to: not Favourite Songs, not canEdit false); merge(data,
                               replace); kind 'folder' for a playlist folder), Group, Track
                               (search_key), Shelf (GType
                               AppleMusicShelfModel: the widget is AppleMusicShelf),
                               PlaylistTree/TreeNode (the folders); SongOrder (the Songs table's
                               orders), fold(), collation_key(), apply_diff(store, items);
                               parse() (json.loads' result a list element at a time),
                               raw_property() (Track's and Item's properties read from `raw`),
                               paused_gc (collections paused, the heap frozen after);
                               GObject/Gio only
src/window.py + window.blp     Window: split view, sidebar (the Playlists section bound to the
                               library's tree; folder expansion in `expanded-folders`), the
                               account button (Sign In, or the name over a Sign Out menu), the
                               signed-out banner and the sync banner (show_sync_progress/
                               hide_sync_progress), the content's AdwNavigationView and its
                               root pages (pages.create, pages.playlist/folder, or a
                               placeholder), last-page restore, open_item(item),
                               open_shelf(shelf), play_request(play, start_with=None,
                               shuffle=False) → app.player.play (sign-in when signed out,
                               app.report otherwise), the bottom sheet (bottom_sheet,
                               player_bar, now_playing; toggle_now_playing(); add_toast()
                               into the sheet while it is open), the playback keys
                               (PLAYBACK_KEYS from shortcuts.PLAYBACK: Space, Ctrl+Right,
                               Ctrl+Left in a capture-phase key controller that leaves an
                               entry's, a toggle's, a menu's and a dialog's keys alone; F10
                               when the primary menu's sidebar is hidden), win.back,
                               win.search, win.focus-sidebar/-content/-player (all off while a
                               dialog is open: _update_actions), the sidebar rows' accessible
                               names and folders' expanded state (_sidebar_rows), toasts,
                               window state, prepare_quit(), hide_for_background() (its dialogs
                               closed, those shown as windows of their own too); do_close_request
                               → app.close_window(self); item_actions (actions.py, made
                               before the sidebar); the Playlists section's menu-model
                               (_sidebar_menu, filled on setup-menu for a playlist or Favourite
                               Songs, closed when empty) and its drop target (TrackRef onto
                               editable playlist entries; drop-enter answers COPY only there)
src/sections.py                the fixed sidebar destinations (key, title, icon), grouped as on the web
src/shortcuts.py               every keyboard shortcut: ACCELS (action -> accelerators, set by
                               main.py), PLAYBACK (the window's playback keys), sections() (the
                               Keyboard Shortcuts dialog, which lists them all and F10, the
                               context-menu keys and Escape), accelerator(key); no GTK
src/sidebar.py                 the Playlists section's model: SidebarEntry (kind fixed/folder/
                               playlist, key, title, icon, depth, item, ancestors),
                               playlist_entries(tree), is_shown(), parse_key(), SidebarItem (an
                               Adw.SidebarItem holding its entry; a folder's arrow suffix;
                               drag-motion-activate off, as on every sidebar item)
src/actions.py                 ItemActions(window, app): the win.* item actions, target "(ss)"
                               (kind, id: an Item's, or 'song' and a Track's id): item-play,
                               item-play-next, item-play-later, item-love, item-unlove,
                               item-add-to-library, item-open-in-browser (Gtk.UriLauncher;
                               launch() is the seam), item-copy-link; item-add-to-playlist
                               "(sss)" (playlist id, kind, id). Each awaits the engine after
                               player.ensure_engine() and toasts, or app.report()s the error;
                               demo mode toasts "Not available…". menu_for(obj) (remembers obj
                               for its actions; asks engine.rating() as the menu opens and
                               swaps Favourite / Remove from Favourites), fill_sidebar_menu(),
                               playlists() (tree order, editable only), drop(playlist, ref),
                               can_drop(), link(). Pure helpers (tests/test_actions.py):
                               build_menu(obj, playlists, loved, storefront), describe(),
                               can_play/queue_target/rating_target/library_target/
                               playlist_song(obj), web_url(obj, storefront) (item url; library
                               playlist → /library/playlist/<id>, library album →
                               /library/albums/<id> or its catalog page; track →
                               /<sf>/song/<catalog id>), set_favourite(); TrackRef (GObject:
                               song_id, title), the drag type
src/player_bar.py + .blp       $AppleMusicPlayerBar: the bottom sheet's bottom bar; set_player(player,
                               app) once; previous/play-pause/next run the app actions; the
                               play button, seek scale, shuffle/repeat toggles and artwork are
                               widgets/transport.py's helpers; a heart (HeartControl); a
                               Gtk.ScaleButton volume; `compact` (the window's 600sp
                               breakpoint) hides the volume, the heart, shuffle, repeat and the
                               times; "Not Playing" and everything insensitive without a track;
                               announces each new item through the window; names the bottom
                               sheet's button around it "Now Playing" (described by the item)
                               and the volume "Volume" with a percentage; grab_bar_focus()
src/widgets/now_playing.py + .blp  $AppleMusicNowPlayingSheet: the bottom sheet's sheet; header
                               with a close button (go-down-symbolic), a toast overlay, a clamp
                               (900) holding the item playing (320 px AppleMusicCover, title-2,
                               "Artist — Album", seek, shuffle/previous/play/next/repeat) and
                               the Lyrics / Up Next Adw.ToggleGroup over a Gtk.Stack; `wide`
                               (window breakpoint min-width 900sp: two columns) and `compact`
                               (600sp: 240 px artwork); measures itself tall so the sheet fills
                               the window; set_player(player, app, bottom_sheet); the focus on
                               its play button as it opens (focus_controls())
src/widgets/lyrics.py          $AppleMusicLyricsView (a Gtk.Stack, built in Python): synced
                               lines in a Gtk.ListView over lyrics.lines (the current line
                               `current`, the rest `dim-label`; a click seeks; the line glides
                               to the middle when it changes, not for 4 s after the user
                               scrolls), unsynced text in a label, a spinner, "No Lyrics";
                               set_player(player, app), set_active(open)
src/widgets/queue.py           $AppleMusicQueueView (a Gtk.Stack): Up Next, a Gtk.ListView over
                               player.queue with QueueRow (number or play icon, title, artist,
                               duration; the entry playing marked), activation →
                               player.queue_jump(position); "Nothing Queued"
src/widgets/transport.py       the transport pieces the bar and the sheet share: run_command(app,
                               coro, on_error), track_subtitle(track), PlayButton, SeekControl
                               (settle, hold and tolerance for a seek), ModeControl (shuffle and
                               repeat toggles), RemoteCover (fetch_remote at the cover size),
                               HeartControl (the bar's heart: unloved at each item, then
                               engine.rating() once, then the `rated` signal; a click loves or
                               unloves the catalog song); each `attach(player, app)`
src/pages/__init__.py          PAGES: destination key → factory; create(destination, library);
                               playlist(library, id, title) and folder(library, id, title, root)
                               for the sidebar's playlists and folders; each page module is
                               imported by its factory; mark_bound(page) (startup timing)
src/pages/grid.py + .blp       $AppleMusicGridPage: title over a Gtk.GridView of tiles, sort drop-down,
                               loading/empty states (albums, artists, recently-added,
                               all-playlists (the root folder), music-videos, folders; pushed
                               with root=False for See All and folder tiles); `model` a
                               Gio.ListModel or a function returning one; the title follows
                               the page's `title`; max-columns follows the width
                               (columns_for(), COLUMN_WIDTH)
src/pages/home.py + .blp       $AppleMusicHomePage: title over a Gtk.Box of AppleMusicShelf, one per
                               non-empty library.shelves, the first as hero cards, all with See All;
                               FIRST_SHELVES bound at once, the rest a frame apart
src/pages/shelves.py + .blp    $AppleMusicShelvesPage(title, fetch, root, icon_name, hero, empty
                               texts): shelves the engine answers with (fetch(refresh) → {shelves}),
                               wrapped by remote_shelves() (Items with artwork under remote-art)
                               and their thumbnails fetched by fetch_shelf_art() (each tile rebound
                               as its file arrives); spinner, Start Engine / Sign In / Try Again
                               status page (waits while the engine is starting; loads itself once
                               it is up or signed in), a refresh button (past the day-long cache);
                               category_page(item) for a search category, pushed
src/pages/new.py               create(destination): a ShelvesPage over engine.browse()
src/pages/made_for_you.py      create(destination): a ShelvesPage over engine.made_for_you()
src/pages/search.py + .blp     $AppleMusicSearchPage: title, a Gtk.SearchEntry and an
                               Adw.ToggleGroup (Apple Music / Your Library) in a 600 px clamp,
                               then a Gtk.Stack: landing (a Gtk.FlowBox of AppleMusicCategoryTile
                               over engine.landing(); a tile opens category_page),
                               suggestions (an Adw.ActionRow list: terms and top hits, after a
                               250 ms debounce, engine.suggest()), results (shelves in Apple's
                               order from engine.search(), Top Results as hero cards, See All →
                               open_shelf), library (Gtk.FilterListModels over the albums,
                               artists, playlists as three shelves, and a StringFilter over the
                               songs' search_key as a list of at most SONG_LIMIT TrackRows; See
                               All → window.open_songs(text)), and one status page; requests
                               are numbered so a late answer is dropped; set_mode(),
                               focus_entry() (win.search), `text`
src/pages/radio.py + .blp      $AppleMusicRadioPage: the first HERO_COUNT (4) of library.radio as a
                               hero shelf ("Recently Played"), the rest in a Gtk.FlowBox of tiles
src/pages/songs.py + .blp      $AppleMusicSongsPage: title and count over a Gtk.ColumnView (Title,
                               Artist, Album, Time), a filter entry in the header; sorted and
                               filtered in Python (see its docstring), rows replaced by _show()
src/pages/detail.py + .blp     $AppleMusicDetailPage: an album or playlist, one Gtk.ListView whose
                               first row is the hero (cover, titles, Play/Shuffle, summary) and
                               then a section per group ("Disc 2" headers); pushed with an item,
                               or a root page following find() (Favourite Songs); an item
                               without groups is fetched through engine.item() (spinner, then
                               Start Engine / Sign In / Try Again states)
src/pages/artist.py + .blp     $AppleMusicArtistPage: round portrait, name, bio, a Gtk.FlowBox of
                               album tiles (the artist's groups, resolved through by_id); fetches
                               the groups as the detail page does
src/widgets/artwork.py         the process-wide Artwork loader (get_default(): get(path, size),
                               get_any(path), request(path, callback, size), cancel; decodes at
                               `size` px, a CACHE_BYTES pixel budget; empty(size) for pictures;
                               async fetch_cover(item) → <cache>/art/, fetch_remote(url, size)
                               → <cache>/remote-art/ by the sized URL's hash, fetch_thumb(item);
                               sized_url(), remote_art_path(), remote_item(dict) (an engine
                               search/browse item's URL artwork → remote-art paths plus thumbUrl
                               and artUrl), thumb_missing(item)); art_colour(item.art_color)
                               → Gdk.RGBA, is_dark(rgba), band_colour(rgba, text_opacity) (the
                               colour made deep or light enough for its caption to read at
                               WCAG AA: readable_band(), contrast_ratio(), luminance())
src/widgets/context_menu.py    attach(view, drag=False): context menus for a view's items (a
                               GridView, ListView, ColumnView, FlowBox, ListBox or one widget):
                               a capture-phase Gtk.GestureClick (button 3) and
                               Gtk.GestureLongPress (touch) picking the widget under the
                               pointer, a Gtk.ShortcutController (Menu, Shift+F10) for the
                               focused row; with drag, a Gtk.DragSource giving a TrackRef
                               (Gdk.ContentProvider.new_for_value; not from a touchscreen).
                               A widget offers a menu through its `context_item` (Item or
                               Track, None when unbound): Tile, HeroTile, TrackRow,
                               SongTitle (the Songs row's other cells find it), the search
                               page's top-hit rows. popup(widget, obj, x, y): a
                               Gtk.PopoverMenu from window.item_actions.menu_for(obj),
                               parented to the widget, no arrow, at the pointer (halign start)
                               or beside the widget; unparented at an idle after it closes
src/widgets/tile.py + .blp     $AppleMusicTile: cover (or round portrait, set_artist(), its
                               Adw.Avatar made then; a folder's big folder icon) and one
                               Gtk.Inscription
src/widgets/hero_tile.py + .blp  $AppleMusicHeroTile: a 260 px AppleMusicCover over a two-line
                               caption band in the item's art colour (drawn in do_snapshot)
src/widgets/category_tile.py + .blp  $AppleMusicCategoryTile: a search category as a landscape
                               tile, its name over its art colour (do_snapshot) and its picture
                               at the right, fetched through fetch_thumb() while mapped; bind(item)
src/widgets/shelf.py + .blp    $AppleMusicShelf: title row (title-2, subtitle, See All) over a
                               horizontal Gtk.ListView of tiles in its own scrolled window;
                               bind_shelf(shelf), `hero`, `see-all`
src/widgets/song_title.py + .blp  $AppleMusicSongTitle: the Songs title cell, 32 px thumbnail,
                               title, explicit badge
src/widgets/cover.py + .blp    $AppleMusicCover: artwork `size` px square over a placeholder card,
                               set_paths(*paths) (the first that decodes), loaded while mapped;
                               CSS classes `small`/`large` for the corners
src/widgets/track_row.py + .blp  $AppleMusicTrackRow: number (albums) or 40 px thumbnail
                               (playlists), title + badge, artist, duration
src/style.css                  auto-loaded app CSS: accent colour and a few small classes
src/icons/*-symbolic.svg       bundled icons, aliased into icons/scalable/actions/ by the gresource
                               (music-note, playlist, broadcast, media-playlist-shuffle,
                               media-playlist-repeat, media-playlist-repeat-song, heart-outline,
                               heart-filled: drawn in 16 px with Adwaita's 2 px stroke, the
                               outline a filled ring with fill-rule evenodd)
src/applemusic.gresource.xml   compiled .ui files (every .blp's, flat in build/src whatever its
                               source directory: window.ui, player_bar.ui, grid.ui, songs.ui,
                               detail.ui, artist.ui, home.ui, radio.ui, signin.ui, tile.ui,
                               song_title.ui, cover.ui, track_row.ui, shelf.ui, hero_tile.ui,
                               now_playing.ui, search.ui, shelves.ui, category_tile.ui,
                               preferences.ui),
                               style.css, icons
src/meson.build                blueprint list (one custom_target per .blp), gresource, install_data
                               lists (app .py; pages/, widgets/, dialogs/, backend/ each their own)
src/backend/                   the engine layer: vendored from the extension plus this app's async
                               layer; no gi, asyncio and stdlib only. __init__.py records provenance
                               and every edit, README.md the command table, error codes, the events
                               and the library.json/Item/Track shapes
  errors.py                    EngineError(code, message): engine-down, not-signed-in, api, timeout,
                               usage
  chrome.py                    find_chrome(), chrome_args(binary, profile, port, headless),
                               EngineState (engine.json load/save/remove, .alive), pid_alive(),
                               async wait_for_devtools/list_targets/find_target/wait_for_target
                               (urllib in a thread); select_target() picks the music.apple.com page
  client.py                    CDPClient: connect(ws_url), call(), evaluate(), bridge(method, *args),
                               on/off(event, cb(name, data)) for CDP events and 'am:<event>' bridge
                               events ('am:*', '*' wildcards), ensure_bridge() (kept across the
                               page's navigations), subscribe(), close(), wait_closed();
                               connect_page(port)
  config.py                    cache_dir() profile_dir() port() state_file(profile), APPLE_MUSIC_CACHE/
                               _PROFILE/_PORT overrides, THUMB_SIZE 320, COVER_SIZE 640, BRIDGE_JS
  cdp.py                       the WebSocket handshake and frame codec as pure functions (shared with
                               client.py) and the extension's blocking client on them
  bridge.js                    injected into music.apple.com (window.__appleMusicLibrary); installed
                               as data beside the Python; subscribe() forwards MusicKit events through
                               the window.__amEvent binding as {name, data}; queueJump(index)
  sync.py                      API answers → Item/Track, artwork cache (.sizes, scale_image hook,
                               download_art with progress/cancelled), library.json writing
                               (save_library under flock, atomic); search_results(),
                               search_suggestions(), search_landing(), category_page(),
                               editorial_shelves() (the New page), made_for_you_shelves(),
                               write_answer()/read_answer() and the *_cache_path()s
data/                          desktop, metainfo, gschema, app icons; Meson tests validate them
data/screenshots/              the metainfo's screenshots (demo library only), see Distribution
po/                            gettext; POTFILES.in must list every file with translatable strings
scripts/run.sh check.sh screenshot.py demo.sh scroll_test.py a11y_check.py bench.py
scripts/am.py                  the engine's debug CLI (no GUI): status, start [--visible], stop,
                               eval <js>, now-playing, events; the app's port and profile
scripts/demo_library.py        invented library.json + drawn artwork (config sizes) into --cache DIR;
                               --albums N adds generated albums and artists, --tracks N sizes
                               them to N songs, --playlists N adds playlists; the last playlist is
                               Favourite Songs (attributes.isFavourites); `folders`: three
                               playlist folders (l.fd002 inside l.fd001) and loose playlists
tests/                         stdlib unittest; __init__.py registers src/ as `applemusic`;
                               fixtures/ holds invented API answers (lyrics.json: invented
                               synced lyrics for the tests and the --now-playing shot)
pyproject.toml                 ruff config only (line length 100, E/F/W; vendored files exempt
                               from E501)
build-aux/flatpak/*.Devel.json Flatpak manifest, development only (why in its "x-comment"); GNOME
                               50 runtime and flatpak-builder not installed here, never built
build-aux/aur/PKGBUILD         the AUR package gnome-apple-music (+ .SRCINFO, regenerated with it)
build-aux/meson/compile-python.py  meson install's byte-compiling of the installed modules
subprojects/blueprint-compiler.wrap   fallback when blueprint-compiler is not on PATH
```

New code goes in: `src/backend/` (engine, CDP, sync; imports no GTK), `src/library.py` (data
model), `src/pages/<name>.py` + `.blp` (one module per sidebar destination or detail page),
`src/widgets/` (reusable: tiles, shelves, track rows, artwork loader), `src/dialogs/` (sign-in,
preferences), `tests/` (stdlib `unittest`), `scripts/` (developer tools). Anything installed
outside the Python module goes in `data/`.

## Commands

```
scripts/run.sh [args]     meson setup (dev profile, prefix build/install) + install + run;
                          `--debug` (or APPLE_MUSIC_DEBUG=1) logs at DEBUG
scripts/check.sh          compileall, ruff (skipped if not installed), unit tests, meson compile,
                          meson tests (desktop/metainfo/schema validation); prints `check: ok`
scripts/demo.sh [args]    run.sh --demo: the app on the invented library in build/demo (generated
                          first when missing); no Chrome, no account. Use it for all UI work
scripts/screenshot.py [out.png] [--light] [--size WxH] [--page KEY] [--demo] [--open KIND:ID]
                      [--expand ID[,ID…]] [--signed-in [NAME]] [--now-playing [lyrics|queue]]
                      [--search TERM] [--context-menu] [--preferences [general|engine]]
                          renders the real window to a PNG; needs a display and a prior run.sh/install;
                          dark by default; GSettings go to a memory backend; animations off;
                          --demo as demo.sh (without it the real cache is read); waits for the
                          library to load; in the narrow layout shows the page when --page or
                          --open is given, else the sidebar. --page takes a last-page value
                          (a destination key, playlist:ID, folder:ID); --expand opens sidebar
                          folders ("first": the library's first). --open album:first (or
                          artist/playlist/station/video/folder, ID an id or "first") calls
                          window.open_item over the page and waits 1.5 s more for artwork.
                          --now-playing (with --demo) puts the demo's first album on the
                          Player (its first track playing 65 s in, the album as the queue,
                          tests/fixtures/lyrics.json as synced lyrics, the cover copied to
                          where fetch_remote would put it) and opens the sheet on Lyrics or
                          Up Next.
                          --search TERM shows the Search page in Your Library mode with
                          TERM typed (the offline filter's results; --page search alone shows
                          the landing's engine-down state under --demo).
                          --context-menu pops up the first tile's or row's context menu (the
                          page's first mapped widget with a context_item) and draws the
                          popover into the shot at its surface's position (a popover is a
                          surface of its own, which the window's WidgetPaintable leaves out).
                          --preferences opens Preferences on General (or Engine) and shoots
                          the dialog: its own window here (the shot's window is fixed-size, so
                          neither maximized nor tiled), or the window when it is inside it;
                          sized to --size when that is under 640 px wide.
                          The sidebar is not scrolled: --size 1100x1000 shows all the demo's
                          playlists. --signed-in [NAME] shows the account button signed in
                          (memory-backend settings only; no engine)
scripts/scroll_test.py [--page KEY] [--speed PX_PER_S] [--distance PX] [--size WxH] [--sidebar]
                          scrolls a page (or, with --sidebar, the sidebar) of the demo library top
                          to bottom (or PX pixels) and reports the app's work per frame (mean, 90th
                          percentile, frames over the refresh interval and over 16.7 ms) and the
                          gaps between frames over 16.7 and 33.3 ms; run it on a big library:
                          APPLE_MUSIC_CACHE=build/demo-big scripts/scroll_test.py --page albums
                          (Songs of 40,000: --page songs --distance 40000, the whole is 2M px;
                          a fling: --speed 20000)
scripts/a11y_check.py [--size WxH] [--light] [--names]
                          walks the keyboard checklist (prompts.md, phase 18) on the demo
                          library, each key dispatched through GTK's own controllers (no input
                          can be sent on this desktop), PASS/FAIL per step, exit 1 on a failure;
                          --size 360x640 for the narrow layout. --names starts a private AT-SPI
                          bus (its own dbus-daemon and registryd, stopped after) and lists each
                          page's focusable controls without an accessible name (libatspi)
python3 -m unittest discover -s tests -v      unit tests alone, from the repo root
scripts/am.py [--debug] status | start [--visible] [--browser CMD] | stop | eval [--no-await] JS |
              now-playing | events
                          drives the engine without the GUI, on the app's port and profile
                          (APPLE_MUSIC_PORT/APPLE_MUSIC_PROFILE override; the .Devel build's
                          are 9229 and chrome-devel). One JSON value per command, or
                          {"error": code, "message"} and exit 1. `start` leaves Chrome running
                          (headless unless --visible) and reuses one already up; `events`
                          prints bridge events one per line until Ctrl+C. Never signs in.
scripts/demo_library.py [--cache DIR] [--albums N] [--playlists N] [--tracks N]
                          writes an invented library (library.json, art/, thumb/, art/.sizes) into
                          DIR, default build/demo; no Chrome. --albums 2000 (about 24,000 songs,
                          10 s: covers drawn in parallel) into build/demo-2000 for measuring;
                          --albums 2500 gives 30,116 songs (build/demo-2500, the Songs page's);
                          phase 19's: --cache build/demo-big --albums 3000 --playlists 300
                          --tracks 40000 (48 MB of library.json, 273 MB with the artwork).
                          Without the new options the output is as it always was
scripts/bench.py [--cache DIR] [--runs N] [--settle MS] [--size WxH] [--profile KEY]
                          startup, page switches and RSS on build/demo-big (or DIR), each run a
                          process of its own (default 3) with medians, beside a bare Adw window
                          (the platform's floor): the Application.mark()s from the process
                          start, every root page shown twice, RSS and anon RSS; --profile KEY
                          prints a cProfile of that page's first switch. Keep its window
                          visible (no frames otherwise: reported after 3 s)
meson setup build --prefix=/usr && meson install -C build --skip-subprojects
                          system install, release profile (byte-compiled; see Distribution)
meson dist -C build       the release tarball in build/meson-dist/ (needs a clean, committed tree)
```

## Conventions

- UI is Blueprint (`.blp`, compiled to `.ui` by Meson). Widgets are `Gtk.Template` classes with
  `__gtype_name__ = 'AppleMusic<Name>'`, `Gtk.Template.Child()` for named children,
  `@Gtk.Template.Callback()` for handlers named in the `.blp`.
- Every user-visible string goes through `_()`: `from gettext import gettext as _` in Python,
  `_("…")` in Blueprint. New files with strings go in `po/POTFILES.in`. Source strings keep the
  scaffold's en-GB spelling ("Favourite").
- GNOME HIG and libadwaita first: `AdwStatusPage`, `AdwSpinner`, `AdwToast`, `AdwDialog`,
  `AdwPreferencesDialog`, style classes (`card`, `flat`, `circular`, `dim-label`, `heading`,
  `title-1`…`title-4`, `caption`, `boxed-list`) before any CSS. CSS lives only in `style.css`.
- Never block the main loop: no `time.sleep`, synchronous sockets or HTTP, `json.load` of the
  library, or image decoding on it. Use `await asyncio.to_thread(...)`; Gio async methods are
  awaitable under the GLib loop (`await file.load_contents_async()`). Touch GTK/GObject only from
  the main thread (creating a `Gdk.Texture` in a thread is fine; it is immutable).
- Collections are models: `Gtk.GridView`/`Gtk.ListView`/`Gtk.ColumnView` over `Gio.ListStore`
  (with `Gtk.SortListModel`/`Gtk.FilterListModel`) and `Gtk.SignalListItemFactory`. Never a
  `Gtk.Box` of hundreds of widgets. Past a few thousand items that the user re-sorts or filters
  interactively (Songs), order and filter in Python and splice the result into the view's
  `Gio.ListStore` (`pages/songs.py`): GTK's sorters and filters read each Python item's
  properties from C at about 3 µs a read, which made them 5 to 100 times slower there.
- The model (`src/library.py`): pages bind `app.library`'s stores, which keep their identity across
  loads and are refilled in place; watch `notify::state` and `changed`. Playlist folders:
  library.json's optional top-level `folders` is a list of {id, title, parent, children: [{kind:
  folder|playlist, id}]} in Apple's order, the entry with id `root` listing the top level
  (playlists in no folder included); the children lists decide, `parent` is informational.
  `library.playlist_tree()` (a new PlaylistTree per load) has `root` (nested TreeNodes: item,
  depth, parent, children, store) and `flat` (depth first, with depth); whatever no list reaches
  goes at the end of the top level, so a library without `folders` is all playlists at the top.
  Folders are Items of kind `folder` (`by_id('folder', id)`, no art, no groups);
  `folder_items(id)` is a folder's Gio.ListStore of folder and playlist Items (`root`: All
  Playlists), replaced by each load, so pages follow a folder by id. `item.groups` (Group:
  `name`, `play`, `entries` store of Track) is wrapped on first access. `library.songs` stays
  empty until `await library.build_songs()` (the Songs page asks when first shown; batched with
  `yield_to_frames()`, one splice at the end), and every load after that refills it;
  `songs-ready` says it is filled, `library.song_count()` counts without building. Track's and
  Item's properties are `library.raw_property`s, read from the object's `raw` dict at each access
  (an Item's are writable into it; `merge()` notifies what changed): nothing is copied, so a
  Track is 455 bytes and 3,000 Items wrap in 9 ms. Group and Shelf use `library.model_property`
  (kept in `_<name>` attributes, assigned directly when wrapping). Never pass properties to
  `GObject.Object.__init__` when wrapping: about 4 µs each, 5-10x slower.
- Sorting: `Gtk.StringSorter`/`Gtk.NumericSorter` (combined with `Gtk.MultiSorter`) over
  `Gtk.PropertyExpression`s of the model properties. `model_property` and `raw_property` make
  real GObject properties with Python getters, so expressions work as they are; key-based
  sorters read each item once and sort in C. Measured on 2,000 albums: 10 ms (title), 25 ms
  (artist, year, title); a Python `Gtk.CustomSorter` 23 ms, and on 24,000 songs 115 ms against
  570 ms. A `Gtk.ClosureExpression` over the attribute halves the read cost if it ever matters.
  A `Gtk.ColumnViewSorter` has no sort keys: a `Gtk.SortListModel` over `column_view.get_sorter()`
  compares pairs and evaluates both items' expressions at every comparison (1.6 to 6 s a click
  on 30,000 songs). Give columns sorters so their headers sort, but read the primary column and
  order from the view's sorter (`changed`) and sort yourself, as the Songs page does with
  `library.SongOrder`.
- Recycled rows and tiles (`Gtk.GridView`, and phase 5's `Gtk.ColumnView`): GTK 4.22's grid
  rebinds each item many times as it scrolls past (2,000 items, 52,000 binds top to bottom), so
  bind must be cheap. Text goes in `Gtk.Inscription`, whose size comes from its line count and
  not its text, so a rebind is a redraw; a wrapping `Gtk.Label` re-measured on every rebind cost
  7 ms a frame at 4,000 px/s against 1 ms. No `_()` in bind (gettext searches the disk on every
  call: look strings up once), no label-backed widgets (`Adw.Avatar` initials) in tiles. Artwork
  is requested on map and released on unmap: the grid binds a few hundred tiles, ~20 on screen.
  A `Gtk.ListView`/`Gtk.ColumnView` keeps 200 rows alive (`GTK_LIST_VIEW_MAX_LIST_ITEMS`), and
  a model change that removes the items they show destroys their widgets and builds new ones
  (about 0.5 ms a Songs row, 100 ms a keystroke); only widgets whose items survive the change are
  recycled. To replace a list's contents, insert the new items first, `scroll_to(0)`, then
  remove the old ones (`SongsPage._show()`): the view then only rebinds (about 800 cell binds,
  20 ms). Give a table's columns fixed widths (`fixed-width` plus `expand`) so they never
  measure their cells.
- Artwork: widgets draw covers through `widgets.artwork.get_default()`: `get(path, size)` (cache
  hit, sync) or `request(path, callback, size)` (decoded in a thread, called back on the main
  loop; shared per path and size) and `cancel(token)` when recycled or unmapped. `size` is the
  edge drawn in pixels (logical size × scale factor): a bigger file is decoded down to it (a
  tile's texture is a quarter of the thumbnail's, a Songs row's a hundredth), and the cache is a
  32 MB pixel budget (`CACHE_BYTES`); `get_any(path)` is any size cached, a stand-in while the
  right one decodes. None means no artwork, including a path not on disk; tiles draw `thumb`
  (320 px), falling back to `art`; a detail page's hero draws `art` (640 px), falling back to
  `thumb` (the tile's texture shown meanwhile). Ask in an idle after the frame (a list maps a few
  hundred rows to show a dozen), not in bind. `widgets.cover.Cover` does this for any square
  artwork; the tiles and Songs cells keep their own copies of the same logic.
- Nothing in a recycled row or tile may change size when its artwork comes or goes: a
  `Gtk.Picture` queues a resize, laying out the whole list, whenever its paintable's intrinsic
  size changes (None to a texture and back), and so does a widget shown or hidden. Show
  `artwork.empty(size)` for no texture and fade the placeholder icon (`set_opacity`); Songs
  scrolled with a third less work per frame for it. Keep costly children out of widgets made by
  the hundred: an `Adw.Avatar` is 18 KB and a third of a tile's making, so a tile makes one only
  when it is an artist's.
- Startup is measured (`scripts/bench.py`) and budgeted: a bare Adw window paints its first frame
  at about 760 ms on this machine, and the target is content at 1 s. The library is read from
  `do_startup` on (`Library.load()` starts the thread when called; `do_activate` awaits the
  coroutine), and between the end of GTK's start and `window.present()` its parse is held
  (`hold_reading()`/`resume_reading()`): Python there shares the GIL with it, at a third of its
  speed. Anything added to `do_startup`, `do_activate` or a restored page's construction is on
  that path: keep it small, and import modules at the top of `main.py`'s import chain (before
  the parse starts) or not at all at startup (page modules are imported by their factory in
  `pages/__init__.py`, and must stay out of `window.py`'s and `main.py`'s top-level imports).
  `Application.mark(name)` notes a startup moment (logged with `--debug`); root pages call
  `pages.mark_bound(self)` at their first bind. A page that builds many widgets builds what
  shows first and the rest a frame apart (Home's `FIRST_SHELVES`).
- Garbage collection: `library.paused_gc()` pauses it around bursts of long-lived objects (a
  load, the Songs build) and freezes the heap after (`gc.freeze()`), so full collections never
  scan the library again (53 ms each over 3,000 albums and 40,000 songs). What is alive at a
  freeze is never collected if it later dies in a reference cycle, so freeze only after
  `load()` and `build_songs()`, not after `reload()` (every sync) or at arbitrary moments.
- Grid pages set `max-columns` to what fits (`grid.columns_for`): `Gtk.GridView` keeps about 32
  rows of `max-columns` tiles alive whatever it shows (385 tiles for 12 columns at 1100 px, 129
  for 4). `COLUMN_WIDTH` is a tile plus Adwaita's grid child padding; change them together.
- Main-thread work in chunks yields with `library.yield_to_frames()`, not a bare
  `await asyncio.sleep(0)`: asyncio runs at `G_PRIORITY_DEFAULT`, above GTK's redraw, so sleep(0)
  alone paints nothing until the task ends (measured: 0 frames against 5 in the same load).
- The backend is reached only through `app.engine` (`src/engine.py`; coroutines: `await
  app.engine.item(kind, id)`, `api(path, params)`, `api_pages(path, params, page=100)`,
  `api_all(paths)`, `play(kind, id, start_with, shuffle)`, `control(action)`, `seek()`,
  `volume()`, `shuffle()`, `repeat()`, `now_playing()`, `queue()`, `queue_jump()`,
  `lyrics()`, `search()`, `suggest()`, `landing()`, `category()`, `browse()`,
  `made_for_you()`), spawned from signal handlers with `app.spawn(coro)`; playback goes through
  `app.player` (`src/player.py`), whose
  commands are thin coroutines over those (`app.player_command(coro)` spawns one and toasts
  its EngineError) and whose properties change only from the engine's `event` signal (the
  bar and MPRIS follow `notify::*`; nothing polls MusicKit). Errors are
  `EngineError(code)` with the README's codes (`engine-down`,
  `not-signed-in`, `api`, `timeout`); `app.report(error)` toasts a sentence for the code (with
  a Sign In button for `not-signed-in`), never a traceback. Library reads are synchronous
  in-memory models. Engine properties: `state` (`down`, `starting`, `up`, `signing-in`),
  `authorized`, `headless`; the `event(name, data)` signal re-emits MusicKit's events without
  the `am:` prefix. `engine.start(visible=None)` reclaims a live Chrome that engine.json names
  when its mode matches, else spawns one (Gio.Subprocess; stderr relayed to the log only at
  DEBUG) and waits for DevTools, the bridge and `status()`; without a mode it keeps an engine
  that runs in either mode and starts a stopped one headless unless `prefer_headless` (the
  `engine-headless` setting) is off, so "make sure it is up" callers pass nothing and only
  sign-in names a mode; the browser command and `set_port()` apply at the next start; `stop()` is close, SIGTERM, 5 s,
  SIGKILL; `kill()` is the synchronous last resort. Under `--demo` the Engine is `demo=True`:
  start/stop do nothing, every command raises `engine-down`.
- Lifecycle: `do_startup` makes `app.engine` on `engine_paths(app.profile, engine-port)` (the
  .Devel build: `chrome-devel` and the port after the setting's, 9229 by default;
  `APPLE_MUSIC_PROFILE`/`APPLE_MUSIC_PORT` win); `do_activate` autostarts it headless when
  `signed-in` and `engine-autostart` are set. `app.quit` (Ctrl+Q, the last window's close
  request, MPRIS Quit, SIGINT/SIGTERM) runs `Application._quit()`: windows `prepare_quit()` (state saved,
  hidden), `engine.stop()` bounded by 6 s, then `Gio.Application.quit`. Never call `quit()`
  from app code; activate the action. Sign-in is `app.sign-in` (`dialogs/signin.py`: restart
  visible, `engine.signin()`, `signed-in` set, `account_name()` best effort, restart headless
  when `engine-headless`); `app.sign-out` asks, then stops the engine, wipes the profile and
  the cache in a thread and reloads the (now empty) library.
- Background playback (phase 17, `background-playback`, off by default): the window's close
  request goes to `app.close_window(window)`. With the setting on and `player.active`, the
  window closes its dialogs (those shown as windows of their own too), hides, and the app is
  `hold()`ed (`app.in_background`); the hidden window stays in the app, so MPRIS Raise and a
  second launch `present()` it, and it becoming visible releases the hold. While hidden, the
  app watches the Player: once `player.stopped` (no item, or none/stopped/ended/completed;
  paused keeps it) has lasted `BACKGROUND_GRACE` (10 s: MusicKit passes through ended and
  stopped between items) it activates `app.quit`, which stops the engine as ever. Otherwise
  closing quits at once, as before.
- Sync (`src/sync.py`, phase 11): `app.sync` ("Refresh Library", `<primary>r`) runs
  `Application.start_sync()`, which starts the engine if it is down and runs
  `sync_library(engine, library, progress)` as one task (a second request while one runs is
  ignored). It also runs after sign-in and when the engine comes up (autostart, or a second
  launch) with `last-sync` older than `sync-interval` hours (`sync_due()`; 0 hours: only when
  asked). The sync fetches every section through `engine.api_pages` (songs with their albums,
  playlists with `extend=tags` and each one's tracks four at a time, the folders from
  `playlist-folders/p.playlistsroot/children` and each folder's `children`, music videos,
  radio, the shelves), normalises in a thread with `backend.sync`'s functions, fetches the
  missing *thumbnails* only (`download_art` over the thumb URLs; progress relayed with
  `call_soon_threadsafe`), writes library.json compact and atomically under the backend's
  flock, prunes, then `library.reload()`. Covers are fetched on demand by the pages that
  show one: `await artwork.get_default().fetch_cover(item)` downloads `item.raw['artUrl']`
  (which the sync writes) to `item.art` in a thread and answers True when it arrived (the
  page then `cover.refresh()`es). Progress is a banner over the content ("Syncing your
  library: songs 300 of 2,000"; sections `sync.PROGRESS_SECTIONS`, named in
  `window.sync_section_names()`), the end a toast with the counts, a failure a toast with the
  message and a Retry button (`not-signed-in` goes through `app.report`). A sync that a
  section's fetch fails (folders, videos, radio, a shelf, a playlist's tracks) keeps last
  time's entry for it; a failed songs or playlists listing, or a lost engine, fails the sync.
  Quitting cancels the sync task (the download thread gives up at its next fetch).
- `library.reload()` (after a sync) matches everything by kind and id: kept Items get
  `merge(raw, replace=True)` (properties set where changed, groups and Tracks kept when the
  group dicts are equal), stores get `apply_diff` (difflib over object ids, so unchanged runs
  are not spliced), changed Items are spliced over themselves so bound rows rebind, Shelf
  objects are kept by key, folder Items by id, the Songs store is left alone when nothing
  moved. `load()` still makes new objects (sign-out, the first load).
- Demo mode: `--demo` sets `app.demo = True`, runs as its own instance (`NON_UNIQUE`) and points
  `APPLE_MUSIC_CACHE` at the launcher's `DEMO_DIR` unless the variable is already set, so
  `APPLE_MUSIC_CACHE=DIR scripts/demo.sh` shows another generated library. `DEMO_DIR` is the
  source tree's `build/demo` in the development profile only; a release build's is empty (no
  source path in an installed launcher) and `--demo` there reads `./build/demo`, relative to
  the working directory. Treat `app.demo` as "no engine": the Engine is a demo one (every command
  `engine-down`), `app.sign-in` toasts, `app.sign-out` does nothing, the banner stays hidden.
- Logging: Python `logging`, `log = logging.getLogger(__name__)`; configured once in `main.main()`
  (`basicConfig`, INFO to stderr as `LEVEL logger: message`; DEBUG with `--debug`, handled in
  `do_handle_local_options`, or with `APPLE_MUSIC_DEBUG` set to anything but empty or `0`). No
  `print` in app code.
- Async: `app.spawn(coro)` runs a coroutine as a task on the GLib-backed asyncio loop, keeps a
  reference until it finishes, logs its exception (cancellation is silent) and returns the task for
  cancelling. Use it from signal handlers instead of threads or `GLib.idle_add`; blocking work
  inside the coroutine goes through `asyncio.to_thread`.
- Pages: a destination's root page is an `Adw.NavigationPage` with its own `Adw.ToolbarView` and
  `Adw.HeaderBar` (`show-title: false`; the header bar still shows the back button to the sidebar
  when collapsed), registered in `pages.PAGES`. Pages listen to `app.library` only while mapped
  (connect in `do_map`, disconnect in `do_unmap`): the library outlives the window. Pushed pages
  (detail, artist) show their title in the header bar and keep what they show in `page.item`.
  Breakpoints need an `Adw.BreakpointBin` inside the page (a navigation page takes none).
  A pushed `GridPage` (`root=False`, See All) shows its title in the header bar as well as in
  the content, as a detail page's hero repeats its header's.
- Engine-driven shelves (New, Made for You, a search category, search results): the engine's
  answers are `{shelves: [{key, title, items: [Item dicts without groups]}]}` whose items
  name a small catalog URL as `art` when the sync has no cover for them (and no `thumb`).
  `widgets.artwork.remote_item(dict)` turns that into paths under `<cache>/remote-art/`
  (`thumb` at 320, `art` at 640, with `thumbUrl`/`artUrl`), `pages.shelves.remote_shelves()`
  wraps them as `library.Shelf`s of `Item`s, and `fetch_shelf_art(shelves)` fetches the missing
  thumbnails a few at a time, splicing each item over itself so its tile rebinds
  (`HeroTile.bind` refreshes its cover when the paths are unchanged for that reason). Opening
  such an item works as for a Home shelf's: `DetailPage` fetches its groups through
  `engine.item()` and merges the sync-style paths in. `ShelvesPage` is the page for any of
  these answers; the landing's categories are Items of kind `category` (opened by
  `window.open_item` → `category_page`), songs Items of kind `song` (they play).
- Shelves: a page of shelves (Home, Radio, New, Made for You, a category, search results) is a
  vertical `Gtk.Box` of `AppleMusicShelf` in a `Gtk.ScrolledWindow` with the `view` style class (the
  lists' background): a handful of shelves, each a horizontal `Gtk.ListView` that recycles its
  tiles in its own scrolled window. Keep the shelf widgets and `bind_shelf()` new Shelf objects
  into them on a reload (`HomePage._show()`). A grid under a shelf in the same scrolled window
  cannot be a `Gtk.GridView` (see below), so Radio's is a `Gtk.FlowBox` of tiles (recent
  stations are tens); anything unbounded belongs on its own page (See All). Tiles sit 18 px
  apart with the first cover on the titles' 24 px margin (`listview.shelf-list` in style.css).
  A shelf item that is also in a section is the section's Item object (`library._fill`).
- Per-item colours (the hero cards' band in `art_color`) are drawn in the widget's own
  `do_snapshot` (`snapshot.append_color`, then chain up), inside its rounded `overflow: hidden`
  clip; CSS cannot take a value per item, and a per-widget CSS provider is out (CSS lives in
  style.css). Keep such Python snapshots off the grid tiles: the hero card is its own class.
- The seams to the rest of the app, on the window (`self.get_root()` from a page):
  `open_item(item)` pushes the item's page (album/playlist → `DetailPage`, artist →
  `ArtistPage`, folder → its `GridPage` (`pages.folder(root=False)`), category → a
  `ShelvesPage` of the curator's grouping); a station or a song has no page and goes to
  `play_request(item.play)`; videos toast for now; the same item twice in a row is pushed
  once); `open_shelf(shelf)` pushes a `GridPage` of
  the shelf's items (See All; a library shelf is followed by key across loads, any other shown
  as it is; round portraits when every item is an artist); `open_songs(text)` shows the Songs
  page filtered by `text` (the library search's See All on its songs);
  `play_request(play, start_with=None, shuffle=False)` is every "play this": a track row passes
  `track.play, start_with=track.index` (its group's target and its place in that queue), Play and
  Shuffle `item.play` (with `shuffle=True`). It toasts until phase 12 hands it to the engine;
  `library.track_at(play, index)` finds the track a request starts with.
- Tests: `tests/test_<module>.py`, stdlib `unittest`, each starting with `from tests import …`
  (e.g. `SRC`, `ROOT`) before any `from applemusic import …`: discovery with `-s tests` imports test
  modules as top-level modules and never runs `tests/__init__.py` on its own. No GTK widgets in
  tests; UI is checked with screenshots.
- Lint: `pyproject.toml` configures ruff; imports after `gi.require_version()` need `# noqa: E402`.
- Settings: one schema `io.github.jackicus.AppleMusic` for both profiles; new keys go in
  `data/…gschema.xml` with a summary, and are read through `app.settings`. Keys: window-width/
  height/maximized, last-page and expanded-folders (written as the window closes or hides, and
  expanded-folders also a second after the last toggle: not at every click), browser-command,
  engine-port, engine-headless,
  engine-autostart, signed-in, account-name, last-sync (ISO 8601, '' before the first),
  sync-interval (hours, 6; 0 = manual only), background-playback (b, false). Preferences
  (`dialogs/preferences.py`) shows background-playback, sync-interval (with last-sync),
  browser-command, engine-port, engine-headless and engine-autostart; the Engine applies the
  engine ones at its next start. A row bound with `Gio.Settings.bind` needs nothing else; an
  `i` key binds to a `double` property (the SpinRow's `value`) as it is.
- Actions: `app.*` in `main.py`, `win.*` in `window.py`. Every shortcut is in `src/shortcuts.py`:
  an accelerator in `ACCELS` (main.py sets them all), a key the window handles itself in
  `PLAYBACK`, and each listed in `sections()`, the Keyboard Shortcuts dialog
  (`tests/test_shortcuts.py` fails when one is not). GTK 4 runs application accelerators in the
  window's *capture* phase, before the focus widget, so a bare key (Space) or an editing chord
  (Ctrl+Left) must not be an accelerator: the playback keys are handled by a capture-phase
  `Gtk.EventControllerKey` on the window that leaves them to an editable or text view, a toggle
  (Space), a popover or a dialog, and skips disabled actions. The shortcuts: Ctrl+Q quit, Ctrl+?
  shortcuts, Ctrl+, Preferences, Ctrl+R refresh (`app.sync`), Ctrl+N Now Playing (toggles the
  sheet; Escape closes it, the sheet's own), Ctrl+W close, Alt+Left back (`win.back`), Ctrl+F
  Search (`win.search`: the entry focused), Ctrl+1/2/3 the focus into the sidebar, the page's
  content, the player bar (`win.focus-sidebar`/`-content`/`-player`), Space play/pause,
  Ctrl+Right/Left next/previous, F10 the main menu (GTK's; the window's own when the collapsed
  layout hides the sidebar), Menu or Shift+F10 a context menu. Window actions are disabled
  while a dialog is open over the window (`Window._update_actions` on `notify::visible-dialog`),
  so their keys never act on the page behind it; add new ones to `_actions` there.
  The item actions (`win.item-*`, actions.py) take their object as a target, not as state:
  a menu item is `Gio.MenuItem.set_action_and_target_value('win.item-love', Variant('(ss)',
  (kind, id)))`. New tiles or rows get context menus by exposing `context_item` and calling
  `context_menu.attach(view)` on their view (with `drag=True` where they are tracks).
- Accessibility (phase 18): every icon-only button has `tooltip-text` (its accessible name);
  a recycled tile or row sets `list_item.set_accessible_label()` (and a description where it
  helps: a track's time) in bind, a `Gtk.ColumnView` row through a `row-factory`, a
  `Gtk.FlowBoxChild` with `update_property([LABEL])`, the format looked up once; decorative
  images (`Gtk.Image`, `Gtk.Picture`, `$AppleMusicCover`, an `Adw.Avatar` beside a name) are
  `accessible-role: presentation`; a slider gets a name and a `VALUE_TEXT` ("1:05 of 3:40",
  "70%"); a state the widget does not show to AT itself (a sidebar folder's expanded) is set
  with `update_state` (EXPANDED takes an int); announcements go through the window
  (`get_root().announce(...)`). Grids and lists have `tab-behavior: item` (Tab leaves after
  one item, arrows move inside). Dialog buttons have mnemonics (`_Label`, `use-underline:
  true`). Text drawn on an item's own colour gets a colour it reads on at WCAG AA
  (`artwork.band_colour`); a dim text in markup uses libadwaita's dim opacity and follows the
  high-contrast setting (`tile.subtitle_markup()`); style.css can use `@media
  (prefers-contrast: more)`. No colour is hard-coded but the accent and text on an item's
  colour. Every page fits 360 px (a `Gtk.FlowBox` of two 160 px tiles needs narrow margins
  there: a `max-width: 400sp` breakpoint). `scripts/a11y_check.py` (and `--names`,
  `--size 360x640`) checks all this; add a step when a new page or key is added.
- Style: 4-space Python, single quotes, no type-annotation ceremony, a docstring where a module or
  function is not obvious. New `.py` files go in `src/meson.build`'s `install_data` list.

## Verifying a change

1. `scripts/check.sh` passes (it grows: tests, lint).
2. Anything visual: `scripts/run.sh` (or `scripts/demo.sh`) builds, then
   `scripts/screenshot.py build/shot.png --demo --page KEY` and look at the PNG with the Read tool;
   also `--light`, and `--size 400x700` for anything adaptive.
3. Backend or model changes get a unit test in `tests/`.
4. Keyboard or accessibility changes (or a new page): `scripts/a11y_check.py`, with
   `--size 360x640` and `--names`.
5. The real engine is only exercised when the phase needs it, and never leaves data in the repo.
6. Anything on the startup path, a page's first build or a list's bind: `scripts/bench.py` on
   build/demo-big (phase 19's numbers are in prompts.md), and `scripts/scroll_test.py` for lists.

## Privacy

No real account data in the repo, tests, docs or committed screenshots: no names, playlist titles,
IDs, tokens, artwork, or contents of the Chrome profile. Fixtures and the demo library are invented.
Live data lives only under `$XDG_CACHE_HOME/apple-music` and `$XDG_DATA_HOME/apple-music`, both
outside the repo; `build/` is git-ignored. Screenshots for the metainfo come from the demo library.

## Things worth knowing

- This machine's floor, which no change to the app removes (phase 19): a bare
  `Adw.ApplicationWindow` paints its first frame about 760 ms after its process starts (GTK's
  start loads the Papirus icon theme, 200 ms; `present()` waits 330 ms for the compositor's
  first configure) and has an RSS of 158 MB (105 MB file-backed; the NVIDIA driver maps about
  50 MB). Timings vary by 100 ms or more from run to run (Unity, Discord and the extension's
  Chrome are usually running): take medians (`bench.py --runs 5`). The compositor sends no frames
  to a window it does not show, so a hidden bench or scroll-test window stalls.

- `AdwSidebar` (libadwaita 1.9): `AdwSidebarSection`s (optional title) hold `AdwSidebarItem`s,
  which are GObjects, not widgets: no children or expanders, but `suffix` (a widget), `subtitle`,
  `visible`, `enabled`, `icon-name`/`icon-paintable`, and the class is derivable. `selected` is the
  index across all sections (`item.get_index()`); `selected-item` is the object; `activated` fires
  on click (the scaffold uses it to show the content pane when collapsed).
  `AdwSidebarSection.bind_model(model, create_func)` mirrors a `Gio.ListModel`; `menu-model` plus
  the `setup-menu` signal give per-item context menus; `setup_drop_target()` accepts drops;
  `mode: page` turns it into boxed lists for the collapsed layout (the window's breakpoint sets it).
  Measured in phase 8 (the Playlists section is bound, `window._update_playlists`):
  - It is a `Gtk.ListBox` of real rows (one per item, hidden ones `visible: false`) in its own
    `Gtk.ScrolledWindow`, not recycled. 169 playlist entries (162 playlists, 7 folders): about
    24 ms to splice in, then a 15-17 ms frame; scrolling 151 visible rows at 2,000-4,000 px/s,
    0.5 ms of work a frame, none over 4.2 ms (`scroll_test.py --sidebar`). Fine at ~150; a few
    thousand playlists would want a lighter path. The same shapes on a reload keep the items
    (only their entries' Items are swapped), so a reload costs 5 ms and loses nothing.
  - A click selects (`notify::selected-item`) and then emits `activated`; a click on the
    selected item only `activated`; `set_selected()` only the notify. So selection shows pages
    and `activated` toggles folders (arrow keys select without toggling).
  - Splicing out the selected bound item sets `selected` to INVALID (items before the splice keep
    theirs), and bind_model makes new items for everything spliced in: reselect by key after.
  - With nothing selected its list selects the row with the focus: the first (Search) when the
    window is shown and again when it becomes active. Never leave it empty on purpose (a
    playlist restored before the library loads keeps All Playlists selected meanwhile), and
    route the window's own selections through `_set_selected` (`_quiet`), which shows nothing.
  - It cannot indent: a folder's contents follow it, the arrow (`pan-end`/`pan-down-symbolic`
    suffix) says whether they show. In page mode the suffix sits before the row's own arrow.
    Selecting does not scroll the list to the item (a restored playlist far down stays off
    screen; the API has no scroll-to).
  - Context menus (phase 16): the triggers are the sidebar's own (each row's
    `Gtk.GestureClick` for any button and touch `Gtk.GestureLongPress`, a sidebar-level
    Menu/Shift+F10 → `menu.popup`); it emits `setup-menu(item)`, then shows one
    `Gtk.PopoverMenu` (parented to the sidebar) built from the section's `menu-model`. An empty
    model still shows an empty popover, so the window pops it down from a high-priority idle
    for a folder or All Playlists. `setup-menu(None)` (closed) can arrive *after* the next
    item's `setup-menu`, and before the chosen action runs: never clear the menu on None. A
    script opens the menu by emitting the row's long-press `pressed(x, y)`
    (`sidebar.activate_action('menu.popup')` from a script did nothing).
  - Drops: `setup_drop_target(Gdk.DragAction.COPY, [TrackRef])` takes Python GObject classes;
    `drop-enter(index)` returns the action (0 refuses the item), `drop(index, value, action)`
    gets the TrackRef itself (emitting it with a `GObject.Value(TrackRef, ref)` works for
    tests). Items' `drag-motion-activate` defaults to TRUE (hovering a drag activates the item,
    switching pages under the drag); every item here sets it FALSE.
- Blueprint 0.22: `template $AppleMusicWindow: Adw.ApplicationWindow {}`; a custom widget used in a
  template (`$AppleMusicPlayerBar`) needs its Python class imported before the template is built
  (hence `from .player_bar import PlayerBar  # noqa: F401` in `window.py`); `[top]`/`[bottom]`/`[end]`
  slots; `styles ["flat"]`; `Adw.Breakpoint { condition ("max-width: 640sp") setters { … } }`;
  `menu primary_menu { … }` at top level; handlers `clicked => $on_clicked();`; bindings
  `label: bind item.title;`. Each new `.blp` goes in `src/meson.build`'s blueprint list and in
  `applemusic.gresource.xml` as `.ui`.
- The source tree is not importable as `applemusic`; the launcher imports it from
  `build/install/share/apple-music/applemusic`, where the gresource also lives. `tests/__init__.py`
  registers `src/` under that name (spec_from_file_location + sys.modules) for the tests;
  `scripts/demo_library.py` does the same to reach `applemusic.backend`.
- The vendored backend and its tests keep upstream's style (double quotes, annotations, long
  lines, which pyproject exempts from E501) so a refresh from the extension diffs cleanly (see the
  "Refresh the vendored backend" prompt); new backend modules follow this file's style. Nothing in
  `src/backend/` imports gi (a test checks); `sync.scale_image` is the hook through which the app
  lends GdkPixbuf for scaling covers into thumbnails (unset, thumbnails are fetched).
- Icons in `src/icons/` resolve by `icon-name` through the resource alias; symbolic SVGs use a `#222`
  fill and are recoloured. Adwaita no longer ships some legacy names (`emblem-favorite-symbolic` is
  gone); bundle anything not in `/usr/share/icons/Adwaita/symbolic/`. Draw with fills only (a
  stroke keeps its colour when recoloured); an outline is a ring, `fill-rule="evenodd"`, with
  Adwaita's 2 px weight at 16 px (compare `non-starred-symbolic`). The hearts are drawn from
  two circles and their tangents (arcs, so they stay crisp).
- Context menus and drags (phase 16, `widgets/context_menu.py`): controllers go on the view,
  not the recycled tiles; the right click and long press in the *capture* phase and claimed,
  so the list item's own click gesture (which selects on release) never sees them. A popover
  can be parented to any widget with a layout manager (a Gtk.Box tile or row); `pointing-to`
  may lie outside the parent (the Songs table's album column points from the title cell).
  Unparent it from an idle after `closed`: the menu item's action runs after the popover
  closes and finds `win.*` through the popover's parent. A `Gtk.PopoverMenu` submenu is named
  by its label (`visible-submenu` 'Add to Playlist' opens it from a script). The DnD type is
  `actions.TrackRef` (GType `AppleMusicTrackRef`: song_id, title), offered with
  `Gdk.ContentProvider.new_for_value(ref)`; the drag icon is a `Gtk.WidgetPaintable` of the
  row. `Gdk.Clipboard.set(text)` works from Python (it is `set_value`, shadowing the varargs
  `set`).
- The web player's own routes (read from its router, 2026-09-28): a library playlist is
  `https://music.apple.com/library/playlist/<p. id>`, a library album
  `https://music.apple.com/library/albums/<l. id>` (only its owner can open either; a library
  album's catalog page comes from `/v1/me/library/albums/<id>/catalog`); a catalog song
  `https://music.apple.com/<sf>/song/<id>` redirects to its canonical address. Ratings: GET
  `/v1/me/ratings/<type>s/<id>` answers 404 for an unrated item, the `?ids=` form `{data: []}`;
  a loved item is `{type: 'ratings', attributes: {value: 1}}`. The library's artist ids
  (`l.art_…`) and loose songs' stand-in albums (`l.alb_…`) are made up by the sync: nothing
  of Apple's answers to them, so they get no rating, link or queue entries.
- `screenshot.py` uses a `.Screenshot` app ID and renders after 1.2 s, and not before the library
  has loaded, so content that arrives later still needs a longer delay; it prints a harmless at-spi
  warning (and, on this desktop, an Adwaita one about gtk-application-prefer-dark-theme). It makes
  the window non-resizable so the desktop's tiling extension (Tiling Shell) honours `--size`; so
  shots have no maximize button.
  It calls `main.use_glib_event_loop()` itself, since it builds the Application without `main()`.
  In the collapsed (narrow) layout it shows the page when `--page` is given, the sidebar otherwise.
  Popovers are surfaces of their own, not in the window's paintable: `--context-menu` draws
  each mapped popover at its `Gdk.Popup` position (relative to the window's surface) plus its
  surface transform, minus the window's.
- A `Gtk.GridView` recycles only as the direct scrollable child of a `Gtk.ScrolledWindow`: in a
  box inside a viewport it creates a few hundred tiles and leaves the rest blank. So the grid
  page's `title-1` title is an overlay child, laid over the grid's CSS top padding (which scrolls
  with the content in GTK 4.22) and moved up by `get-child-position` as the grid scrolls. Row
  heights come from the tiles' minimum heights. After a sort change the grid would follow its old
  top item; `grid_view.scroll_to(0, …)` puts it back at the top.
- A `Gtk.ListView` gives each row its *minimum* height: a widget whose minimum is below its
  natural size is squashed in a row (an `Adw.StatusPage`, a scrolled window inside, got 58 px of
  292). Tab never reaches focusable widgets in a list *header* (`header-factory`), only in items
  (with `tab-behavior: item`, Tab goes through the focused item's widgets, then leaves; arrows
  move between items). Hence the detail page's hero is its list's first item, not a header, and
  a box that looks like a compact status page. A `Gtk.FlattenListModel` is a `Gtk.SectionModel`
  (one section per child model), which is what the "Disc 2" headers hang on.
- Nested scrolling (GTK 4.22, `gtkscrolledwindow.c`): a scrolled window handles a scroll event
  only along an axis it can scroll (its scrollbar is visible), and returns PROPAGATE for one
  wholly along the other, so a vertical wheel over a shelf (`vscrollbar-policy: never`) scrolls
  the page and a horizontal one the shelf, with no code (checked by emitting `scroll` on both
  bubble-phase `Gtk.EventControllerScroll`s: the shelf's declines (0, 1), the page's then
  scrolls 75 px). An event with any horizontal part (a diagonal touchpad swipe) is kept by the
  shelf; libinput locks two-finger scrolling to one axis, and once the page has started a
  touchpad scroll its capture-phase controller keeps the gesture even over a shelf. Emitting
  `scroll` on a controller from Python runs GTK's handler without an event (no modifiers, wheel
  units); pick the bubble-phase one, the capture-phase one only continues a scroll in progress.
- libadwaita's `.card` sets its own `color` (`--card-fg-color`, dark in the light theme), so a
  `card` placeholder on a coloured background needs `color: inherit`; `--card-bg-color` is
  white in the light theme, invisible on the `view` background (the hero cards tint with
  `color-mix(in srgb, currentColor 8%, transparent)` instead).
- `Adw.NavigationView` pops on Escape, Back, `<Alt>Left` (class shortcuts, *local* scope: only
  with the focus inside it) and the mouse back button (a click gesture on every button). A push
  moves the focus into the new page (the detail page's Play button), so the keys work there;
  `win.back` (`<alt>Left`, in the shortcuts dialog) does the same from the sidebar or the player
  bar, and is disabled when there is nowhere to go back to, while a dialog is open and while
  the Now Playing sheet is open, so the keys pass through.
- `Gtk.ColumnView.sort_by_column()` does not tell the previous primary column to drop its sort
  arrow (GTK 4.22's `gtk_column_view_sorter_set_column`); clicking a header does. Use it only
  for the initial order, or call `sort_by_column(None, …)` first.
- Frame timing on this desktop varies with the compositor and the other load (the monitor is
  240 Hz; unfocused windows may get 60), so `scroll_test.py` reports the app's own work per frame,
  which is what the app controls. The desktop's tiling extension resizes windows that are
  resizable when mapped: test scripts make theirs non-resizable in `window-added`.
- The dev build shares the release schema and resource path; only the app ID, desktop file and icons
  differ. `run.sh` sets `GSETTINGS_SCHEMA_DIR` and `XDG_DATA_DIRS` to `build/install`.
- The blueprints are one Meson `custom_target` per `.blp` naming its `.ui` (flat in
  `build/src`), so an edited `.blp` is recompiled on the next build. (Until phase 12 they were
  one target whose output was the directory, which looked up to date whenever anything else
  had been written there, and template changes went missing.) A new `.blp` goes in the list
  in `src/meson.build` and its `.ui` (bare name) in the gresource.
- `Adw.BottomSheet` (libadwaita 1.9): the bottom bar is laid *over* the content, which is not
  inset by it; `window.blp` binds the content's `margin-bottom` to `bottom-bar-height`. With
  `full-width: false` the bar floats as a pill the width of its controls (tried, wrong here).
  Clicking or swiping the bar opens the sheet (`can-open`); Escape and the sheet's own close
  the sheet. `bottom-bar-height` is 0 while the sheet is open, so the content grows under it.
  With `can-open` the bar is wrapped in a focusable `Gtk.Button` (the player bar's parent; Enter
  on it opens the sheet), whose accessible name was everything inside it: the player bar names
  it. Opening the sheet focuses its first focusable widget; closing it restores the focus.
- Several `Adw.Breakpoint`s on the window: only the *last* matching one applies, so the
  narrower breakpoint (600sp: `player_bar.compact`, `now_playing.compact`) repeats the wider
  one's setters (640sp: the collapsed split view). The `min-width: 900sp` one
  (`now_playing.wide`) is disjoint from both.
- `Adw.BottomSheet` sizing (phase 14, measured with a probe): the sheet gets its child's
  *natural* height, clamped to the window less a 30 px top margin (and never under its
  minimum), so a full-height sheet asks for a tall natural height (`NowPlayingSheet`,
  `SHEET_NATURAL_HEIGHT`). The sheet child is mapped and laid out (at that minimum) while
  the sheet is closed, and keeps that geometry for a frame or two after `open` is set, so
  work that depends on the open size waits for the adjustment's `changed`. Escape is a
  local-scope `Gtk.ShortcutController` on the sheet's inner gizmo (`observe_controllers()`
  down the tree), which the modal sheet's focus satisfies.
- A Python `do_measure` on an `Adw.Bin` (or any widget with a layout manager) is never
  called: `gtk_widget_measure` asks the layout manager first. Drop it
  (`set_layout_manager(None)`) and implement both `do_measure` and `do_size_allocate`.
- CSS padding on a `Gtk.ListView` scrolls with its rows and is outside the adjustment's page
  (`get_height()` is the content box: 615 px of scrolled window gave a 443 px page with
  172 px of padding), so a list whose geometry is computed from (`compute_bounds` to the
  list, the page size) carries no padding; pad with margins around it. A `scroll_to` asked
  from inside the adjustment's `changed` handler (the list's allocation) is lost: defer it
  to an idle.
- A `Gtk.Stack` will not switch to a child that is not visible (make it visible first);
  `Gtk.Inscription` has the `label` CSS name, so a `label` selector styles it too;
  `Adw.ToggleGroup { Adw.Toggle { name: …; label: …; } }` compiles with Blueprint 0.22;
  GtkBuilder never runs a Python widget's `__init__`, so a template's custom children that
  build themselves in Python (the sheet's LyricsView and QueueView) are made in the
  template class's `__init__` and added there.
- A `Gtk.Overlay` allocates an overlay child its *natural* size (clamped to the overlay), and
  a `Gtk.Picture`'s natural size is its image's, so a picture laid over a wide tile fills it:
  give it an overlay of its own whose main child is a sized placeholder box (the cover widget
  and the category tile do). A callback connected by GtkBuilder (a template's
  `clicked => $on_x()`) is not found by `handler_block_by_func`: use a flag while the page
  itself sets an entry's text. A `Gtk.StringFilter` over a `Gtk.ClosureExpression.new(str,
  lambda item: item.search_key, None)` filters 30,000 songs in one pass with one Python call
  each (fold the search text first, `ignore-case: false`); `Gtk.AnyFilter` of two
  `StringFilter`s over `PropertyExpression`s serves the small stores. An `Adw.ToggleGroup`'s
  `active-name` is the toggle's `name`; a `Gtk.SearchEntry.grab_focus()` lands on its inner
  `Gtk.Text` (`window.get_focus()` is the text, whose ancestor is the entry).
- An `Adw.Dialog` shown as a window of its own (its parent neither maximized nor tiled) is a
  plain `Gtk.Window` transient for the parent, not one of the application's windows: its
  widgets find no `app.*` actions (an actionable row is insensitive and does nothing; hence
  Preferences' `insert_action_group('app', app)`), `get_active_window()` stays the main
  window (whose toasts are then hidden behind the dialog: `app.toast()` goes into Preferences
  while it is open), and `AdwApplicationWindow.get_visible_dialog()` does not see it (close
  it through `Gtk.Window.list_toplevels()`, transient for the window). An actionable widget's
  sensitivity is its action's: `set_sensitive(False)` on a row with `action-name` is undone
  (Sign Out is wired by its `activated` signal instead). `Gio.Settings.bind_with_mapping`
  exists in PyGObject but its get-mapping closure receives the GValue as a plain int it cannot
  set (and GLib aborts on the rejected default), so a mapped row is bound by hand
  (the refresh interval's ComboRow).
- `screenshot.py --signed-in` also turns `engine-autostart` off in its memory settings: with
  it on, the shot's app started a real headless Chrome on the *release* profile and port
  (`profile` is `default` there), which is never wanted from a screenshot.
- Chrome: `google-chrome-stable` 154 is installed. Its MPRIS player is
  `org.mpris.MediaPlayer2.chromium.instance<pid>`, disabled by
  `--disable-features=HardwareMediaKeyHandling` (confirmed in phase 13: with the engine
  playing, `busctl --user list | grep -i mpris` shows this app's name and no entry for the
  engine's pid; `MediaSessionService` did not need disabling. The user's everyday Chrome may
  show an `instance<pid>` of its own: compare the pid). The extension's engine uses port 9227 and profile
  `$XDG_DATA_HOME/apple-music-library/chrome`; this app uses 9228 and `$XDG_DATA_HOME/apple-music/chrome`
  (the `.Devel` build 9229 and `chrome-devel`) so they can run side by side, which also means a
  separate sign-in per profile.
- The engine layer (phase 9): `EngineState.alive` is the pid *and* `--user-data-dir=<profile>`
  on its `/proc` command line, so a reused pid is never mistaken for our Chrome; the state file
  is keyed by profile (`config.state_file(profile)`: the default profile's in
  `$XDG_RUNTIME_DIR/apple-music/engine.json`, any other's inside the profile). A
  `Runtime.addBinding` is per CDP session: each connection registers `__amEvent` itself and
  the last one to connect owns the page's `window.__amEvent`. `Runtime.enable` replays
  `executionContextCreated` for existing contexts; the client re-injects only once
  `ensure_bridge()` has been asked for, and only for the main frame's default context
  (iframes such as Apple's sign-in get their own). MusicKit's `PlaybackStates` names
  (`none loading playing paused stopped ended seeking waiting stalled completed`) are what
  `am:playbackStateDidChange` carries; a play walks playing → waiting → loading → playing.
  An asyncio subprocess transport kills its child when garbage-collected (as a CLI command
  exits), so `scripts/am.py` spawns Chrome with `subprocess.Popen(start_new_session=True)`;
  the app spawns with `Gio.Subprocess`. Visible Chrome is an `--app=` window (no tabs or
  address bar), as the extension had it. Unauthorised MusicKit plays 30-second catalog
  previews, which is enough to exercise playback and events without signing in.
- The Engine in the app (phase 10): `Gio.Subprocess.wait_async()` is awaitable under the GLib
  loop and an `asyncio.wait_for` timeout on it is clean (then `force_exit()`);
  `Gio.InputStream.read_bytes_async()` is awaitable too (the stderr relay; `read_line_async`
  returns an empty line at EOF, indistinguishable from a blank one). Chrome's helper processes
  write to the profile for a moment after the browser process has exited (the network service
  re-created `Default/Network Persistent State`), so sign-out's `remove_trees` retries. A
  headless start takes about 10 s to the bridge on this machine (Chrome 4 s, the page the
  rest). `Adw.Dialog` (libadwaita 1.9) is presented inside the window (`AdwDialogHost` →
  `AdwFloatingSheet`) only when the window is maximized or tiled; a normal window gets a
  separate toplevel, so a screenshot of the window misses the dialog: snapshot
  `dialog.get_root()`. `gapplication action io.github.jackicus.AppleMusic.Devel quit` (or
  `sign-in`) activates an app action from a shell, which is how live runs are driven without
  input synthesis. Apple's page is Svelte with hashed class names; signed out, the sidebar
  footer holds `div.auth-content > button.signin`; `Engine.account_name()` tries known
  selectors under it and returns '' rather than guessing (the signed-in markup is unverified).
- MPRIS (phase 13, `src/mpris.py`): PyGObject 3.56 marks `Gio.DBusConnection.register_object`
  deprecated (GLib 2.84 replaced the closures variant); `register_object_with_closures2` takes
  the same three callables: `method_call(connection, sender, path, interface, method,
  parameters, invocation)` answering with `invocation.return_value(GLib.Variant or None)`,
  `get_property(…, name)` returning a `GLib.Variant`, `set_property(…, name, value)` returning
  True. GLib serves `Get`/`GetAll`/`Set`, introspection and the "not writable" errors from the
  `Gio.DBusNodeInfo` itself. `Gio.bus_own_name` calls `bus_acquired` (register there) before
  `name_acquired`; `name_lost` with a None connection means no bus at all. PropertiesChanged
  is emitted by hand (`(sa{sv}as)`); Position never appears in it (the spec's annotation),
  clients read it, so `Player.estimated_position()` runs it on between events. The service
  keeps what it last sent and emits only the keys that differ, seeded from the values on the
  bus at registration. Live checks: `gdbus introspect --session --dest
  org.mpris.MediaPlayer2.io.github.jackicus.AppleMusic.Devel --object-path
  /org/mpris/MediaPlayer2`; `gdbus monitor` on the same shows PropertiesChanged and Seeked;
  `gdbus call … --method org.freedesktop.DBus.Properties.Set org.mpris.MediaPlayer2.Player
  Volume "<0.2>"` sets the engine's volume before a test play; gdbus spells an object path
  argument `"objectpath '/…'"`. GNOME Shell's media section lists a player while its
  `CanPlay` is true (so "Not Playing" shows no entry) and looks the app up by
  `<DesktopEntry>.desktop` in the Shell's own data directories, which the dev build's
  `build/install` is not in (the entry shows the Identity; a system install gives the icon).
  `playerctl` is not installed on this machine.
- Python 3.14 deprecates `asyncio.set_event_loop_policy` (removal in 3.16), but it is still how
  PyGObject 3.56 puts asyncio on the GLib loop; `main.use_glib_event_loop()` filters the
  DeprecationWarning and sets the policy. PyGObject's `Gio.Application.run` marks the GLib loop as
  the running asyncio loop only when that policy is set.
- Accessibility plumbing (phase 18): the desktop's own AT-SPI bus is dead on this machine
  (GTK logs "Unable to connect to the accessibility bus"), and pyatspi/Orca are not installed,
  but the `Atspi` typelib is: `a11y_check.py --names` runs a private `dbus-daemon` with
  `/usr/share/defaults/at-spi2/accessibility.conf` on an abstract socket plus
  `/usr/lib/at-spi2-registryd`, and GTK and libatspi both follow `AT_SPI_BUS_ADDRESS`. An
  `Atspi.EventListener` sees events only when it is registered before the app starts (GTK asks
  the registry once), and after `Atspi.init()`. `Gtk.Accessible.announce()` from a widget whose
  accessible object no client has asked for yet is dropped; from the window it is always sent
  (`object:announcement`). AdwSidebar (1.9) names none of its rows in the sidebar mode (the page
  mode's are `Adw.ActionRow`s, named by title) and says nothing of a folder's state; its rows
  follow the items one to one, hidden ones too. `AdwShortcutsDialog` rows are unnamed too
  (main.py names them) and `AdwComboRow` exposes an unnamed inner list item (left). GTK's F10
  (`gtk-window-menubar-accel`) opens a `primary` menu button only while it is mapped. Key
  presses cannot be synthesised here: `a11y_check.py`'s `press()` runs the controllers a real
  event would reach (capture key and shortcut controllers from the window down, shortcut
  controllers from the focus up, a popover's key controller), which is as close as it gets.
- History starts at the "Scaffold: window, sidebar, build" commit; one commit (or a few) per phase.

## Distribution (phase 20)

- Release path: native. `meson install` (release profile) and the AUR package
  `gnome-apple-music` (`build-aux/aur/PKGBUILD`: `arch-meson` with `--wrap-mode nodownload`, so
  `blueprint-compiler` is a makedepend; depends gdk-pixbuf2 glib2 gtk4 libadwaita python
  python-gobject; optdepends google-chrome; check() runs `meson test` and the unit tests,
  which need no display). Its source is GitHub's archive of the tag `v$pkgver` (top directory
  `GNOME-Apple-Music-App-$pkgver`), `sha256sums=('SKIP')` until the tag exists; after any
  change to it, `updpkgsums` and `makepkg --printsrcinfo > .SRCINFO`. Flathub is out of scope
  (settled question 1): the host Chrome and the trademark.
- Version: `meson.build`'s `version` (0.9.0, the first release candidate); the development
  profile appends the git revision. Each release adds a `<release version date>` at the top
  of the metainfo's `<releases>` with a short description (a `<p>` and a `<ul>`). The tag
  is `v<version>`, annotated.
- Bytecode: the app's modules are `install_data`, outside site-packages, so Meson's
  `python.bytecompile` never reaches them; `src/meson.build` adds
  `build-aux/meson/compile-python.py` as an install script (compileall over the installed
  package under `$MESON_INSTALL_DESTDIR_PREFIX`, tracebacks naming the final path; level 0,
  plus -O/-OO when `python.bytecompile` is 1/2, as `arch-meson` sets 1; skipped at -1). Only
  stale files are compiled again, so `run.sh`'s install stays quick. Under makepkg
  (`SOURCE_DATE_EPOCH` set) the .pyc are checked-hash. `ninja uninstall` leaves the
  `__pycache__` directories behind (they are not in Meson's install log).
- The blueprint-compiler wrap, when Meson falls back to it, installs its
  `reference_docs.json` into the prefix's site-packages: packages install with
  `--skip-subprojects` (the PKGBUILD does, and the README's install lines).
- Screenshots: `data/screenshots/{home,albums,album,now-playing}-{light,dark}.png`, 1100×760
  at scale 1 from `scripts/screenshot.py --demo` (`--page home`, `--page albums`, `--page
  albums --open album:first`, `--page albums --now-playing`; `--light`), losslessly shrunk
  (`uv run --no-project --with pyoxipng`, oxipng level 6: about 15 %; no optimiser is
  installed here). The metainfo lists them light first (`environment="gnome"`, the Home one
  `type="default"`), then dark (`gnome:dark`), by
  `https://raw.githubusercontent.com/Jackicus/GNOME-Apple-Music-App/main/data/screenshots/…`,
  so they resolve only once pushed, and renaming one breaks every metainfo already
  installed. Look at every shot before committing it: demo data only, and no text chunks.
- `meson dist -C build` archives HEAD (commit first) without the subprojects: building the
  tarball needs `blueprint-compiler` installed or the network for the wrap. Test a tarball
  in a temporary directory with its own `--prefix`, and a PKGBUILD in a temporary copy with a
  `git archive --prefix=GNOME-Apple-Music-App-<version>/` tarball named as its source
  (makepkg finds it and skips the download); here blueprint-compiler is not installed, so
  `makepkg --nodeps` with a `blueprint-compiler` shim on PATH that runs
  `subprojects/blueprint-compiler/blueprint-compiler.py`. Never `makepkg -i` on this machine.
- The Flatpak manifest (development only): `--talk-name=org.freedesktop.Flatpak`, the MPRIS
  `--own-name` for both IDs, no pulseaudio socket (the host's Chrome makes the sound). In the
  sandbox (`/.flatpak-info`) `chrome.find_chrome()` asks the host's shell for the binary
  (`find_host_chrome`, through `flatpak-spawn --host`; blocking, so the engine calls it in a
  thread) and `chrome_args()` prefixes `flatpak-spawn --host --watch-bus`: the pid the engine
  holds is flatpak-spawn's, whose command line carries `--user-data-dir` (so `pid_alive`
  works), which relays SIGTERM to Chrome, and whose end (SIGKILL, the sandbox closing) ends
  Chrome through `--watch-bus`. The profile under `~/.var/app/<id>/data` and 127.0.0.1 are the
  same on both sides. Untested: no GNOME 50 runtime or flatpak-builder on this machine.
