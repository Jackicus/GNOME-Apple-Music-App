import json
import os
import shutil
import tempfile
import unittest

from tests import SRC  # noqa: F401  (registers src/ as the applemusic package)

from applemusic.backend import config, sync

# The sizes these tests were written against (the extension's defaults). The
# app's own defaults come from config; TestArtSizes covers those.
UPSTREAM_SIZES = {"cover": 512, "thumb": 256}

try:
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except (ImportError, ValueError):  # pragma: no cover - depends on the system
    GdkPixbuf = None


def pixbuf_scaler(src_path, dest_path, size):
    """A sync.scale_image, as the app installs one."""
    scaled = GdkPixbuf.Pixbuf.new_from_file_at_scale(src_path, size, size, True)
    scaled.savev(dest_path, "jpeg", ["quality"], ["90"])


class TestSync(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        sync.ART_SIZES.update(UPSTREAM_SIZES)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)
        sync.ART_SIZES.update(sync.DEFAULT_ART_SIZES)

    def test_format_duration(self):
        self.assertEqual(sync.format_duration(0), "0:00")
        self.assertEqual(sync.format_duration(-10), "0:00")
        self.assertEqual(sync.format_duration(45000), "0:45")
        self.assertEqual(sync.format_duration(216000), "3:36")
        self.assertEqual(sync.format_duration(3600000), "1:00:00")
        self.assertEqual(sync.format_duration(3661000), "1:01:01")

    def test_format_count_label(self):
        self.assertEqual(sync.format_count_label(1), "1 song")
        self.assertEqual(sync.format_count_label(10), "10 songs")
        self.assertEqual(sync.format_count_label(12, 43 * 60 * 1000), "12 songs, 43 min")
        self.assertEqual(sync.format_count_label(15, 65 * 60 * 1000), "15 songs, 1 hr 5 min")
        self.assertEqual(sync.format_count_label(20, 120 * 60 * 1000), "20 songs, 2 hr")

    def test_format_color(self):
        self.assertIsNone(sync.format_color(None))
        self.assertIsNone(sync.format_color(""))
        self.assertEqual(sync.format_color("1a1a1a"), "#1a1a1a")
        self.assertEqual(sync.format_color("#1a1a1a"), "#1a1a1a")

    def test_strip_html(self):
        self.assertIsNone(sync.strip_html(None))
        self.assertIsNone(sync.strip_html(""))
        self.assertEqual(sync.strip_html("<p>Hello <b>World</b>&amp;Friends</p>"), "Hello World&Friends")

    def test_artwork_url_and_path(self):
        art_obj = {"url": "https://example.com/art/{w}x{h}bb.{f}", "bgColor": "222222"}
        formatted_url = sync.format_artwork_url(art_obj, 512, 512)
        self.assertEqual(formatted_url, "https://example.com/art/512x512bb.jpg")

        path = sync.artwork_cache_path(formatted_url, self.tmp_dir)
        self.assertTrue(path.endswith(".jpg"))
        self.assertTrue(path.startswith(os.path.join(self.tmp_dir, "art")))

    def test_normalize_track(self):
        raw = {
            "id": "1724040711",
            "type": "songs",
            "attributes": {
                "name": "Heroes Get Remembered",
                "artistName": "Paper Parachutes",
                "albumName": "Static Skyline",
                "trackNumber": 3,
                "discNumber": 1,
                "durationInMillis": 216000,
                "contentRating": "explicit",
                "playParams": {"catalogId": "1724040711"},
            },
        }
        track = sync.normalize_track(raw, index=2)
        self.assertEqual(track["id"], "1724040711")
        self.assertEqual(track["catalogId"], "1724040711")
        self.assertEqual(track["title"], "Heroes Get Remembered")
        self.assertEqual(track["artist"], "Paper Parachutes")
        self.assertEqual(track["album"], "Static Skyline")
        self.assertEqual(track["trackNumber"], 3)
        self.assertEqual(track["discNumber"], 1)
        self.assertEqual(track["durationMs"], 216000)
        self.assertEqual(track["durationLabel"], "3:36")
        self.assertTrue(track["explicit"])
        self.assertEqual(track["index"], 2)
        self.assertIsNone(track["thumb"])

    def test_normalize_track_names_thumbnail_when_asked(self):
        sync.ART_URLS.clear()
        raw = {
            "id": "i.one",
            "attributes": {"name": "One", "artwork": {"url": "https://x/{w}x{h}bb.jpg"}},
        }
        track = sync.normalize_track(raw, index=0, cache_dir=self.tmp_dir)
        self.assertTrue(track["thumb"].startswith(os.path.join(self.tmp_dir, "thumb")))
        self.assertEqual(sync.ART_URLS[track["thumb"]], "https://x/256x256bb.jpg")
        # The same cover's full-size file has the same name, one folder over.
        self.assertEqual(os.path.basename(track["thumb"]),
                         os.path.basename(sync.artwork_cache_path("https://x/512x512bb.jpg", self.tmp_dir)))

    def test_normalize_album(self):
        raw_album = {
            "id": "l.alb123",
            "type": "library-albums",
            "attributes": {
                "name": "Static Skyline",
                "artistName": "Paper Parachutes",
                "releaseDate": "2007-09-18",
                "genreNames": ["Rock"],
                "contentRating": "explicit",
                "trackCount": 2,
                "artwork": {"url": "https://example.com/{w}x{h}.{f}", "bgColor": "112233"},
                "editorialNotes": {"standard": "<p>A classic easycore record.</p>"},
            },
        }
        raw_tracks = [
            {
                "id": "t2",
                "attributes": {
                    "name": "Track Two",
                    "artistName": "Paper Parachutes",
                    "trackNumber": 2,
                    "discNumber": 1,
                    "durationInMillis": 180000,
                },
            },
            {
                "id": "t1",
                "attributes": {
                    "name": "Track One",
                    "artistName": "Paper Parachutes",
                    "trackNumber": 1,
                    "discNumber": 1,
                    "durationInMillis": 120000,
                },
            },
            {
                "id": "t3",
                "attributes": {
                    "name": "Bonus Track",
                    "artistName": "Paper Parachutes",
                    "trackNumber": 1,
                    "discNumber": 2,
                    "durationInMillis": 200000,
                },
            },
        ]

        item = sync.normalize_album(raw_album, cache_dir=self.tmp_dir, tracks=raw_tracks)
        self.assertEqual(item["id"], "l.alb123")
        self.assertEqual(item["kind"], "album")
        self.assertEqual(item["title"], "Static Skyline")
        self.assertEqual(item["subtitle"], "Paper Parachutes")
        self.assertEqual(item["year"], 2007)
        self.assertEqual(item["genre"], "Rock")
        self.assertEqual(item["summary"], "A classic easycore record.")
        self.assertEqual(item["artColor"], "#112233")
        self.assertEqual(item["play"], {"kind": "album", "id": "l.alb123"})

        # Two discs
        self.assertEqual(len(item["groups"]), 2)
        disc1 = item["groups"][0]
        self.assertEqual(disc1["name"], "Disc 1")
        self.assertEqual(len(disc1["entries"]), 2)
        # Check sorting by trackNumber
        self.assertEqual(disc1["entries"][0]["title"], "Track One")
        self.assertEqual(disc1["entries"][0]["index"], 0)
        self.assertEqual(disc1["entries"][1]["title"], "Track Two")
        self.assertEqual(disc1["entries"][1]["index"], 1)

        disc2 = item["groups"][1]
        self.assertEqual(disc2["name"], "Disc 2")
        self.assertEqual(len(disc2["entries"]), 1)
        self.assertEqual(disc2["entries"][0]["title"], "Bonus Track")
        self.assertEqual(disc2["entries"][0]["index"], 0)

    def test_normalize_playlist(self):
        raw_playlist = {
            "id": "p.pl123",
            "type": "library-playlists",
            "attributes": {
                "name": "Workout Mix",
                "curatorName": "Jordan",
                "lastModifiedDate": "2024-05-01",
                "trackCount": 1,
            },
        }
        raw_tracks = [
            {
                "id": "s1",
                "attributes": {
                    "name": "Energy",
                    "artistName": "Artist",
                    "trackNumber": 1,
                    "durationInMillis": 210000,
                },
            }
        ]

        item = sync.normalize_playlist(raw_playlist, cache_dir=self.tmp_dir, tracks=raw_tracks)
        self.assertEqual(item["id"], "p.pl123")
        self.assertEqual(item["kind"], "playlist")
        self.assertEqual(item["title"], "Workout Mix")
        self.assertEqual(item["subtitle"], "Jordan")
        self.assertEqual(item["year"], 2024)
        self.assertEqual(len(item["groups"]), 1)
        self.assertEqual(item["groups"][0]["name"], "Tracks")
        self.assertEqual(item["groups"][0]["entries"][0]["title"], "Energy")
        self.assertIn("thumb", item)
        self.assertIn("thumb", item["groups"][0]["entries"][0])

    def test_recommendation_shelves_follow_apples_order_and_titles(self):
        def album(i):
            return {"id": f"a{i}", "type": "albums", "attributes": {"name": f"Album {i}", "artistName": "X"}}
        raw = [
            {"id": "one", "attributes": {"title": {"stringForDisplay": "New Releases for You"}},
             "relationships": {"contents": {"data": [album(1), album(2)]}}},
            # A group: its members are shelves of their own, in place of it.
            {"id": "grp", "attributes": {"isGroupRecommendation": True, "title": {"stringForDisplay": "Genres"}},
             "relationships": {"recommendations": {"data": [
                 {"id": "rock", "attributes": {"title": {"stringForDisplay": "Rock"}},
                  "relationships": {"contents": {"data": [album(3)]}}},
                 {"id": "empty", "attributes": {"title": {"stringForDisplay": "Nothing"}},
                  "relationships": {"contents": {"data": []}}},
             ]}}},
            {"id": "st", "attributes": {"title": {"stringForDisplay": "Stations for You"}},
             "relationships": {"contents": {"data": [
                 {"id": "ra.1", "type": "stations", "attributes": {"name": "Alt Station"}}]}}},
        ]
        shelves = sync.recommendation_shelves(raw)
        self.assertEqual([s["key"] for s in shelves], ["rec-one", "rec-rock", "rec-st"])
        self.assertEqual([s["title"] for s in shelves], ["New Releases for You", "Rock", "Stations for You"])
        self.assertEqual([it["title"] for it in shelves[0]["items"]], ["Album 1", "Album 2"])
        self.assertEqual(shelves[2]["items"][0]["kind"], "station")
        self.assertEqual(shelves[0]["items"][0]["groups"], [])

    def test_search_results_are_shelved_in_apples_order(self):
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "search_results.json")) as f:
            raw = json.load(f)
        # The catalog's own Top Results — the same song again, and the album
        # — and its own order for the shelves, songs ahead of albums.
        song = raw["results"]["songs"]["data"][0]
        album = raw["results"]["albums"]["data"][0]
        raw["results"]["topResults"] = {"data": [song, album]}
        raw["meta"] = {"results": {"order": ["topResults", "artists", "songs", "albums", "playlists"]}}
        # A cover the sync has fetched already, and its thumbnail.
        cover_url = sync.template_artwork_url(album["attributes"]["artwork"]["url"], 512, 512)
        thumb_path = sync.thumb_cache_path(cover_url, self.tmp_dir)
        os.makedirs(os.path.dirname(thumb_path))
        with open(thumb_path, "wb") as f:
            f.write(b"jpg")

        out = sync.search_results(raw, self.tmp_dir)

        self.assertEqual([s["key"] for s in out["shelves"]], ["top", "artists", "songs", "albums", "playlists"])
        self.assertEqual([s["title"] for s in out["shelves"]], ["Top Results", "Artists", "Songs", "Albums", "Playlists"])
        top = out["shelves"][0]["items"]
        self.assertEqual([it["kind"] for it in top], ["song", "album"])
        self.assertEqual(top[0]["groups"], [])
        # Flat, no repeats, the top results first.
        self.assertEqual([(it["kind"], it["id"]) for it in out["items"]],
                         [("song", song["id"]), ("album", album["id"]), ("artist", "123456"),
                          ("playlist", "pl.rock-essentials")])
        # The album's thumbnail is on disk and stays; its cover is not, so a
        # small catalog URL stands in. The song carries no artwork at all.
        hit_album = out["shelves"][3]["items"][0]
        self.assertEqual(hit_album["thumb"], thumb_path)
        self.assertTrue(hit_album["art"].startswith("https://"))
        self.assertIn("256x256", hit_album["art"])
        hit_song = out["shelves"][2]["items"][0]
        self.assertIsNone(hit_song["thumb"])
        self.assertIsNone(hit_song["art"])

    def test_search_results_with_nothing(self):
        self.assertEqual(sync.search_results(None, self.tmp_dir), {"shelves": [], "items": []})
        self.assertEqual(sync.search_results({"results": {"albums": {"data": []}}}, self.tmp_dir),
                         {"shelves": [], "items": []})

    def test_search_results_without_apples_order_take_the_usual(self):
        raw = {"results": {
            "albums": {"data": [{"id": "1", "type": "albums", "attributes": {"name": "A", "artistName": "X"}}]},
            "artists": {"data": [{"id": "2", "type": "artists", "attributes": {"name": "X"}}]},
            "stations": {"data": [{"id": "ra.3", "type": "stations", "attributes": {"name": "S"}}]},
        }}
        out = sync.search_results(raw, self.tmp_dir)
        self.assertEqual([s["key"] for s in out["shelves"]], ["artists", "albums", "stations"])

    def test_search_results_shelve_music_videos_as_videos(self):
        raw = {"results": {
            "music-videos": {"data": [{"id": "9", "type": "music-videos", "attributes": {
                "name": "Orbit Parade", "artistName": "Hollow Satellites", "durationInMillis": 240000,
                "artwork": {"url": "https://x/{w}x{h}bb.{f}"}}}]},
        }}
        out = sync.search_results(raw, self.tmp_dir)
        self.assertEqual([(s["key"], s["title"]) for s in out["shelves"]], [("music-videos", "Music Videos")])
        video = out["shelves"][0]["items"][0]
        self.assertEqual(video["kind"], "video")
        self.assertEqual(video["title"], "Orbit Parade")
        self.assertEqual(video["subtitle"], "Hollow Satellites")
        self.assertEqual(video["play"], {"kind": "musicVideo", "id": "9"})
        self.assertIn("256x256", video["art"])

    def test_search_suggestions_are_terms_and_top_hits(self):
        raw = {"results": {"suggestions": [
            {"kind": "terms", "searchTerm": "glow", "displayTerm": "glow"},
            {"kind": "terms", "searchTerm": "glow in the dark", "displayTerm": "glow in the dark"},
            {"kind": "terms", "searchTerm": "Glow", "displayTerm": "Glow"},
            {"kind": "topResults", "content": {"id": "1", "type": "songs", "attributes": {
                "name": "Glow", "artistName": "Lantern Theory", "durationInMillis": 1000}}},
            {"kind": "topResults", "content": {"id": "1", "type": "songs", "attributes": {
                "name": "Glow", "artistName": "Lantern Theory", "durationInMillis": 1000}}},
            {"kind": "topResults", "content": {"id": "2", "type": "artists", "attributes": {"name": "Glow"}}},
            {"kind": "topResults", "content": "not a resource"},
            "not a suggestion",
        ]}}
        out = sync.search_suggestions(raw, self.tmp_dir)
        self.assertEqual(out["terms"], [{"term": "glow", "display": "glow"},
                                        {"term": "glow in the dark", "display": "glow in the dark"}])
        self.assertEqual([(it["kind"], it["id"], it["title"]) for it in out["items"]],
                         [("song", "1", "Glow"), ("artist", "2", "Glow")])
        self.assertEqual(out["items"][0]["groups"], [])
        self.assertIsNone(out["items"][0]["art"])

    def test_search_suggestions_with_nothing(self):
        self.assertEqual(sync.search_suggestions(None, self.tmp_dir), {"terms": [], "items": []})
        self.assertEqual(sync.search_suggestions({"results": {}}, self.tmp_dir), {"terms": [], "items": []})

    def test_search_landing_is_apples_curators_in_order(self):
        curator = lambda cid, name, short=None, bg="dd6848": {  # noqa: E731
            "id": cid, "type": "apple-curators", "attributes": {
                "name": name, "shortName": short or name, "url": f"https://music.apple.com/gb/curator/x/{cid}",
                "artwork": {"url": "https://x/{w}x{h}{c}.{f}", "bgColor": bg, "width": 1080, "height": 1080}}}
        raw = {"data": [
            {"id": "r1", "type": "personal-recommendation", "attributes": {"title": {"stringForDisplay": "Browse Categories"}},
             "relationships": {"contents": {"data": [curator("1", "Apple Music Live")]}}},
            {"id": "r2", "type": "personal-recommendation",
             "relationships": {"contents": {"data": [
                 curator("2", "Apple Music Rock", "Rock"),
                 {"id": "e1", "type": "editorial-items", "attributes": {"editorialArtwork": {}}},
                 curator("2", "Apple Music Rock", "Rock"),
                 {"id": "3", "type": "apple-curators", "attributes": {"artwork": {}}},
                 "junk",
             ]}}},
        ]}
        out = sync.search_landing(raw, self.tmp_dir)
        self.assertEqual([(c["id"], c["title"], c["subtitle"]) for c in out["categories"]],
                         [("1", "Apple Music Live", None), ("2", "Rock", "Apple Music Rock")])
        rock = out["categories"][1]
        self.assertEqual(rock["kind"], "category")
        self.assertEqual(rock["art"], "https://x/320x320bb.jpg")
        self.assertEqual(rock["artColor"], "#dd6848")
        self.assertEqual(rock["url"], "https://music.apple.com/gb/curator/x/2")
        self.assertEqual(sync.search_landing(None, self.tmp_dir), {"categories": []})

    def test_category_page_is_the_groupings_shelves(self):
        element = lambda eid, name, contents: {  # noqa: E731
            "id": eid, "type": "editorial-elements", "attributes": {"editorialElementKind": "326", "name": name},
            "relationships": {"contents": {"data": contents}}}
        raw = {"data": [{
            "id": "900000101", "type": "apple-curators",
            "attributes": {"name": "Apple Music Rock", "shortName": "Rock"},
            "relationships": {"grouping": {"data": [{
                "id": "g", "type": "groupings", "attributes": {"name": "Rock"},
                "relationships": {"tabs": {"data": [{
                    "id": "t", "type": "editorial-elements",
                    "relationships": {"children": {"data": [
                        element("hero", "", []),
                        element("songs", "Best New Songs", [
                            {"id": "s1", "type": "songs", "attributes": {"name": "Quiet Engines", "artistName": "A", "durationInMillis": 1000}},
                            {"id": "s2", "type": "songs"},
                        ]),
                        element("albums", "New Releases", [
                            {"id": "a1", "type": "albums", "attributes": {"name": "Borrowed Lanterns", "artistName": "B",
                                                                           "artwork": {"url": "https://x/{w}x{h}{c}.{f}"}}},
                        ]),
                        element("empty", "Nothing Here", []),
                    ]}}}]}}}]}},
        }]}
        out = sync.category_page(raw, self.tmp_dir)
        self.assertEqual(out["id"], "900000101")
        self.assertEqual(out["title"], "Rock")
        self.assertEqual([(s["key"], s["title"], len(s["items"])) for s in out["shelves"]],
                         [("cat-songs", "Best New Songs", 1), ("cat-albums", "New Releases", 1)])
        self.assertEqual(out["shelves"][0]["items"][0]["kind"], "song")
        album = out["shelves"][1]["items"][0]
        self.assertEqual(album["groups"], [])
        self.assertIn("256x256", album["art"])
        self.assertEqual(sync.category_page(None, self.tmp_dir), {"id": "", "title": "", "shelves": []})

    def test_normalize_station(self):
        raw_station = {
            "id": "ra.12345",
            "type": "stations",
            "attributes": {
                "name": "Apple Music 1",
                "stationProviderName": "Apple Music",
                "description": {"standard": "The new music that matters."},
            },
        }
        item = sync.normalize_station(raw_station, cache_dir=self.tmp_dir)
        self.assertEqual(item["id"], "ra.12345")
        self.assertEqual(item["kind"], "station")
        self.assertEqual(item["title"], "Apple Music 1")
        self.assertEqual(item["subtitle"], "Apple Music")
        self.assertEqual(item["summary"], "The new music that matters.")
        self.assertEqual(item["groups"], [])

    def test_group_songs_into_albums_and_artists(self):
        songs = [
            {
                "id": "s1",
                "attributes": {
                    "name": "Track A",
                    "artistName": "Artist One",
                    "albumName": "Album One",
                    "trackNumber": 1,
                    "discNumber": 1,
                    "durationInMillis": 120000,
                    "releaseDate": "2020-01-01",
                    "genreNames": ["Pop"],
                },
                "relationships": {
                    "albums": {
                        "data": [{
                            "id": "l.alb1",
                            "type": "library-albums",
                            "attributes": {
                                "name": "Album One",
                                "artistName": "Artist One",
                                "releaseDate": "2020-01-01",
                                "genreNames": ["Pop"],
                            },
                        }]
                    }
                },
            },
            {
                "id": "s2",
                "attributes": {
                    "name": "Track B",
                    "artistName": "Artist One",
                    "albumName": "Album One",
                    "trackNumber": 2,
                    "discNumber": 1,
                    "durationInMillis": 180000,
                    "releaseDate": "2020-01-01",
                    "genreNames": ["Pop"],
                },
                "relationships": {
                    "albums": {
                        "data": [{
                            "id": "l.alb1",
                            "type": "library-albums",
                            "attributes": {
                                "name": "Album One",
                                "artistName": "Artist One",
                                "releaseDate": "2020-01-01",
                                "genreNames": ["Pop"],
                            },
                        }]
                    }
                },
            },
        ]

        albums, artists = sync.group_songs_into_albums_and_artists(songs, self.tmp_dir)
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["title"], "Album One")
        self.assertEqual(len(albums[0]["groups"][0]["entries"]), 2)

        self.assertEqual(len(artists), 1)
        self.assertEqual(artists[0]["title"], "Artist One")
        self.assertEqual(len(artists[0]["groups"]), 1)
        self.assertEqual(artists[0]["groups"][0]["name"], "Album One")

    def test_artist_takes_first_albums_thumbnail_with_its_cover(self):
        sync.ART_URLS.clear()
        song = {
            "id": "s1", "type": "library-songs",
            "attributes": {
                "name": "Track A", "artistName": "Artist One", "albumName": "Album One",
                "trackNumber": 1, "discNumber": 1, "durationInMillis": 1000,
                "artwork": {"url": "https://x/{w}x{h}bb.jpg", "bgColor": "123456"},
            },
            "relationships": {"albums": {"data": [{
                "id": "l.alb1", "type": "library-albums",
                "attributes": {"name": "Album One", "artistName": "Artist One",
                               "artwork": {"url": "https://x/{w}x{h}bb.jpg", "bgColor": "123456"}},
            }]}},
        }
        albums, artists = sync.group_songs_into_albums_and_artists([song], self.tmp_dir)
        self.assertEqual(artists[0]["art"], albums[0]["art"])
        self.assertEqual(artists[0]["thumb"], albums[0]["thumb"])
        self.assertTrue(artists[0]["thumb"].startswith(os.path.join(self.tmp_dir, "thumb")))
        self.assertEqual(artists[0]["artColor"], "#123456")

    def test_save_library_and_merge(self):
        initial = {
            "version": 1,
            "generated": "2026-09-25T12:00:00Z",
            "storefront": "us",
            "sections": {
                "albums": [{"id": "a1", "title": "Album 1"}],
                "artists": [{"id": "art1", "title": "Artist 1"}],
                "playlists": [{"id": "p1", "title": "Playlist 1"}],
                "radio": [{"id": "r1", "title": "Radio 1"}],
            },
            "shelves": [{"key": "heavy-rotation", "title": "Heavy Rotation", "items": []}],
        }
        sync.save_library(initial, self.tmp_dir)

        lib_file = os.path.join(self.tmp_dir, "library.json")
        self.assertTrue(os.path.exists(lib_file))
        with open(lib_file, "r") as f:
            data = json.load(f)
        self.assertEqual(len(data["sections"]["albums"]), 1)

        # Merge with only albums updated
        updated = {
            "version": 1,
            "generated": "2026-09-25T13:00:00Z",
            "storefront": "us",
            "sections": {
                "albums": [
                    {"id": "a1", "title": "Album 1"},
                    {"id": "a2", "title": "Album 2"},
                ],
                "artists": [],
                "playlists": [],
                "radio": [],
            },
            "shelves": [],
        }
        sync.save_library(updated, self.tmp_dir, only="albums")

        with open(lib_file, "r") as f:
            merged = json.load(f)

        self.assertEqual(len(merged["sections"]["albums"]), 2)
        # playlists still preserved
        self.assertEqual(len(merged["sections"]["playlists"]), 1)
        self.assertEqual(len(merged["shelves"]), 1)

    def test_prune_art(self):
        art_dir = os.path.join(self.tmp_dir, "art")
        thumb_dir = os.path.join(self.tmp_dir, "thumb")
        os.makedirs(art_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)
        used_file = os.path.join(art_dir, "used.jpg")
        unused_file = os.path.join(art_dir, "unused.jpg")
        used_thumb = os.path.join(thumb_dir, "used.jpg")
        unused_thumb = os.path.join(thumb_dir, "unused.jpg")
        row_thumb = os.path.join(thumb_dir, "row.jpg")

        for path in (used_file, unused_file, used_thumb, unused_thumb, row_thumb):
            with open(path, "w") as f:
                f.write("test")

        lib_data = {
            "sections": {
                "albums": [{"art": used_file, "thumb": used_thumb}],
                "artists": [],
                "playlists": [{"art": None, "thumb": None,
                               "groups": [{"entries": [{"thumb": row_thumb}]}]}],
                "radio": [],
            },
            "shelves": [],
        }

        marker = os.path.join(art_dir, ".sizes")
        with open(marker, "w") as f:
            f.write('{"cover": 512, "thumb": 256}')

        pruned = sync.prune_art(lib_data, self.tmp_dir)
        self.assertEqual(pruned, 2)
        self.assertTrue(os.path.exists(marker))
        self.assertTrue(os.path.exists(used_file))
        self.assertTrue(os.path.exists(used_thumb))
        self.assertTrue(os.path.exists(row_thumb))
        self.assertFalse(os.path.exists(unused_file))
        self.assertFalse(os.path.exists(unused_thumb))


class TestArtworkDownload(unittest.TestCase):
    """Normalisation names the file; download_art fetches what is missing."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        sync.ART_URLS.clear()
        sync.ART_SIZES.update(UPSTREAM_SIZES)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)
        sync.ART_URLS.clear()
        sync.ART_SIZES.update(sync.DEFAULT_ART_SIZES)

    def _album(self, art_url):
        return {
            "id": "l.one", "type": "library-albums",
            "attributes": {"name": "One", "artistName": "A", "artwork": {"url": art_url}},
        }

    def test_extract_registers_url_for_path(self):
        item = sync.normalize_album(self._album("https://x/{w}x{h}bb.jpg"), cache_dir=self.tmp_dir)
        self.assertTrue(item["art"].startswith(os.path.join(self.tmp_dir, "art")))
        self.assertEqual(sync.ART_URLS[item["art"]], "https://x/512x512bb.jpg")
        self.assertTrue(item["thumb"].startswith(os.path.join(self.tmp_dir, "thumb")))
        self.assertEqual(sync.ART_URLS[item["thumb"]], "https://x/256x256bb.jpg")
        self.assertEqual(os.path.basename(item["thumb"]), os.path.basename(item["art"]))

    def test_download_art_fetches_only_missing(self):
        item = sync.normalize_album(self._album("https://x/{w}x{h}bb.jpg"), cache_dir=self.tmp_dir)
        other = sync.normalize_album(dict(self._album("https://y/{w}x{h}bb.jpg"), id="l.two"), cache_dir=self.tmp_dir)
        os.makedirs(os.path.dirname(other["art"]), exist_ok=True)
        with open(other["art"], "wb") as f:
            f.write(b"already here")
        lib = {"sections": {"albums": [item, other]}, "shelves": []}

        fetched = []

        def fake_cache(url, cache_dir, timeout=10.0, dest_path=None):
            fetched.append(url)
            path = dest_path or sync.artwork_cache_path(url, cache_dir)
            with open(path, "wb") as f:
                f.write(b"img")
            return path

        def fake_thumb(url, cache_dir, dest_path):
            fetched.append(url)
            with open(dest_path, "wb") as f:
                f.write(b"img")
            return dest_path

        real = sync.cache_artwork, sync.cache_thumbnail
        sync.cache_artwork, sync.cache_thumbnail = fake_cache, fake_thumb
        try:
            counts = sync.download_art(lib, self.tmp_dir, log=lambda m: None)
        finally:
            sync.cache_artwork, sync.cache_thumbnail = real
        # The one missing cover, then the thumbnails of both, in that order.
        self.assertEqual(fetched[0], "https://x/512x512bb.jpg")
        self.assertEqual(sorted(fetched[1:]), ["https://x/256x256bb.jpg", "https://y/256x256bb.jpg"])
        self.assertEqual(counts, {"wanted": 4, "fetched": 3, "failed": 0})
        self.assertTrue(os.path.exists(item["art"]))
        self.assertTrue(os.path.exists(item["thumb"]))

    def test_download_art_counts_failures(self):
        item = sync.normalize_album(self._album("https://x/{w}x{h}bb.jpg"), cache_dir=self.tmp_dir)
        lib = {"sections": {"albums": [item]}, "shelves": []}
        logged = []
        real = sync.cache_artwork, sync.cache_thumbnail
        sync.cache_artwork = lambda url, cache_dir, timeout=10.0, dest_path=None: None
        sync.cache_thumbnail = lambda url, cache_dir, dest_path: None
        try:
            counts = sync.download_art(lib, self.tmp_dir, log=logged.append)
        finally:
            sync.cache_artwork, sync.cache_thumbnail = real
        # The cover and its thumbnail.
        self.assertEqual(counts["failed"], 2)
        self.assertEqual(len(logged), 2)

    @unittest.skipIf(GdkPixbuf is None, "GdkPixbuf not available")
    def test_thumbnail_is_scaled_from_the_cached_cover(self):
        self.addCleanup(setattr, sync, "scale_image", sync.scale_image)
        sync.scale_image = pixbuf_scaler
        item = sync.normalize_album(self._album("https://x/{w}x{h}bb.jpg"), cache_dir=self.tmp_dir)
        os.makedirs(os.path.dirname(item["art"]), exist_ok=True)
        cover = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 512, 512)
        cover.fill(0x336699ff)
        cover.savev(item["art"], "jpeg", ["quality"], ["80"])
        # Nothing is fetched: the cover is on disk.
        real = sync.cache_artwork
        sync.cache_artwork = lambda *a, **k: self.fail("fetched a thumbnail it could have scaled")
        try:
            counts = sync.download_art({item["thumb"]: sync.ART_URLS[item["thumb"]]}, self.tmp_dir)
        finally:
            sync.cache_artwork = real
        self.assertEqual(counts, {"wanted": 1, "fetched": 1, "failed": 0})
        thumb = GdkPixbuf.Pixbuf.new_from_file(item["thumb"])
        self.assertEqual((thumb.get_width(), thumb.get_height()), (sync.ART_SIZES["thumb"], sync.ART_SIZES["thumb"]))

    def test_thumbnail_is_fetched_without_a_scaler(self):
        # The backend imports no gi: with no scale_image installed, a cover on
        # disk does not help, and the thumbnail is fetched at its own size.
        self.addCleanup(setattr, sync, "scale_image", sync.scale_image)
        sync.scale_image = None
        item = sync.normalize_album(self._album("https://x/{w}x{h}bb.jpg"), cache_dir=self.tmp_dir)
        os.makedirs(os.path.dirname(item["art"]), exist_ok=True)
        with open(item["art"], "wb") as f:
            f.write(b"a cover")
        self.assertFalse(sync.make_thumbnail(item["art"], item["thumb"]))
        fetched = []
        real = sync.cache_artwork
        sync.cache_artwork = lambda url, cache_dir, dest_path=None: fetched.append((url, dest_path)) or dest_path
        try:
            self.assertEqual(sync.cache_thumbnail(sync.ART_URLS[item["thumb"]], self.tmp_dir, item["thumb"]), item["thumb"])
        finally:
            sync.cache_artwork = real
        self.assertEqual(fetched, [("https://x/256x256bb.jpg", item["thumb"])])

    def test_download_item_art_takes_the_rows_thumbnails_too(self):
        raw = {"id": "p.one", "type": "library-playlists",
               "attributes": {"name": "Mix", "artwork": {"url": "https://p/{w}x{h}bb.jpg"}}}
        tracks = [{"id": "i.1", "attributes": {"name": "A", "artwork": {"url": "https://a/{w}x{h}bb.jpg"}}},
                  {"id": "i.2", "attributes": {"name": "B", "artwork": {"url": "https://b/{w}x{h}bb.jpg"}}}]
        item = sync.normalize_playlist(raw, cache_dir=self.tmp_dir, tracks=tracks)
        asked = []
        real = sync.download_art
        sync.download_art = lambda urls, cache_dir, **k: asked.append(sorted(urls.values()))
        try:
            sync.download_item_art(item, self.tmp_dir)
        finally:
            sync.download_art = real
        self.assertEqual(asked, [[
            "https://a/256x256bb.jpg", "https://b/256x256bb.jpg",
            "https://p/256x256bb.jpg", "https://p/512x512bb.jpg",
        ]])

    def test_album_stub_without_attributes_takes_song_name(self):
        songs = [{
            "id": "i.1", "type": "library-songs",
            "attributes": {
                "name": "First Light", "albumName": "Collected Postcards", "artistName": "Isla Marren",
                "trackNumber": 1, "discNumber": 1, "durationInMillis": 1000,
                "artwork": {"url": "https://x/{w}x{h}bb.jpg"},
            },
            "relationships": {"albums": {"data": [{"id": "l.gone", "type": "library-albums"}]}},
        }]
        albums, artists = sync.group_songs_into_albums_and_artists(songs, self.tmp_dir)
        self.assertEqual(albums[0]["id"], "l.gone")
        self.assertEqual(albums[0]["title"], "Collected Postcards")
        self.assertEqual(albums[0]["subtitle"], "Isla Marren")
        self.assertIsNotNone(albums[0]["art"])
        self.assertEqual(artists[0]["title"], "Isla Marren")


class TestArtSizes(unittest.TestCase):
    """The cover and thumbnail sizes: set per sync, recorded beside the
    covers, read back by every other command."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        sync.ART_URLS.clear()
        sync.ART_SIZES.update(sync.DEFAULT_ART_SIZES)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)
        sync.ART_URLS.clear()
        sync.ART_SIZES.update(sync.DEFAULT_ART_SIZES)

    def test_defaults_without_a_marker(self):
        self.assertEqual(sync.load_art_sizes(self.tmp_dir), {"cover": config.COVER_SIZE, "thumb": config.THUMB_SIZE})

    def test_apply_writes_the_marker_and_clamps(self):
        self.assertEqual(sync.apply_art_sizes(self.tmp_dir, 768, 128), {"cover": 768, "thumb": 128})
        with open(os.path.join(self.tmp_dir, "art", ".sizes")) as f:
            self.assertEqual(json.load(f), {"cover": 768, "thumb": 128})
        sync.ART_SIZES.update(sync.DEFAULT_ART_SIZES)
        self.assertEqual(sync.load_art_sizes(self.tmp_dir), {"cover": 768, "thumb": 128})
        self.assertEqual(sync.ART_SIZES, {"cover": 768, "thumb": 128})
        # Out of range, or not a number: the nearest sane size, or the one standing.
        self.assertEqual(sync.apply_art_sizes(self.tmp_dir, 9000, 10), {"cover": 1024, "thumb": 96})
        self.assertEqual(sync.apply_art_sizes(self.tmp_dir, "x", None), {"cover": 1024, "thumb": 96})

    def test_sizes_name_the_urls(self):
        sync.apply_art_sizes(self.tmp_dir, 640, 192)
        item = sync.normalize_album({"id": "l.one", "type": "library-albums", "attributes": {
            "name": "One", "artistName": "A", "artwork": {"url": "https://x/{w}x{h}bb.jpg"}}}, cache_dir=self.tmp_dir)
        self.assertEqual(sync.ART_URLS[item["art"]], "https://x/640x640bb.jpg")
        self.assertEqual(sync.ART_URLS[item["thumb"]], "https://x/192x192bb.jpg")
        # A search hit without a cover on disk takes the thumbnail's size.
        hit = {"art": item["art"], "thumb": item["thumb"]}
        sync._settle_search_art(hit, {"attributes": {"artwork": {"url": "https://x/{w}x{h}bb.jpg"}}})
        self.assertEqual(hit["art"], "https://x/192x192bb.jpg")
        self.assertIsNone(hit["thumb"])

    def test_changed_thumb_size_wipes_the_thumbnails(self):
        thumb_dir = os.path.join(self.tmp_dir, "thumb")
        os.makedirs(thumb_dir)
        with open(os.path.join(thumb_dir, "a.jpg"), "wb") as f:
            f.write(b"old")
        # The same size as the (implied) default: nothing happens.
        sync.apply_art_sizes(self.tmp_dir, config.COVER_SIZE, config.THUMB_SIZE)
        self.assertTrue(os.path.exists(os.path.join(thumb_dir, "a.jpg")))
        # A changed cover size alone: the thumbnails stay too.
        sync.apply_art_sizes(self.tmp_dir, 768, config.THUMB_SIZE)
        self.assertTrue(os.path.exists(os.path.join(thumb_dir, "a.jpg")))
        # A changed thumbnail size: they go, to be rebuilt from the covers.
        sync.apply_art_sizes(self.tmp_dir, 768, 128)
        self.assertEqual(os.listdir(thumb_dir), [])


class TestOtherCaches(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_prune_remote_art_keeps_the_newest_within_budget(self):
        folder = os.path.join(self.tmp_dir, "remote-art")
        os.makedirs(folder)
        for i in range(5):
            path = os.path.join(folder, f"{i}.img")
            with open(path, "wb") as f:
                f.write(b"x" * 100)
            os.utime(path, (1000 + i, 1000 + i))
        self.assertEqual(sync.prune_remote_art(self.tmp_dir, max_bytes=250), 3)
        self.assertEqual(sorted(os.listdir(folder)), ["3.img", "4.img"])
        self.assertEqual(sync.prune_remote_art(self.tmp_dir, max_bytes=250), 0)
        self.assertEqual(sync.prune_remote_art(os.path.join(self.tmp_dir, "nowhere")), 0)

    def test_answer_paths_are_safe_names(self):
        self.assertEqual(sync.item_cache_path(self.tmp_dir, "album", "l.abc/../x"),
                         os.path.join(self.tmp_dir, "items", "album-l.abc_.._x.json"))
        self.assertEqual(sync.category_cache_path(self.tmp_dir, "98 85/81"),
                         os.path.join(self.tmp_dir, "categories", "98_85_81.json"))
        self.assertEqual(sync.landing_cache_path(self.tmp_dir), os.path.join(self.tmp_dir, "landing.json"))

    def test_answers_are_kept_and_age_out(self):
        path = sync.item_cache_path(self.tmp_dir, "album", "1")
        self.assertIsNone(sync.read_answer(path, 60))
        kept = sync.write_answer(path, {"id": "1", "groups": []})
        self.assertIn("cached", kept)
        self.assertEqual(sync.read_answer(path, 60)["id"], "1")
        # Older than allowed: as good as none.
        with open(path, "r+", encoding="utf-8") as f:
            data = json.load(f)
            data["cached"] = "2000-01-01T00:00:00Z"
            f.seek(0)
            f.truncate()
            json.dump(data, f)
        self.assertIsNone(sync.read_answer(path, 60))
        # Unreadable: none.
        with open(path, "w") as f:
            f.write("{not json")
        self.assertIsNone(sync.read_answer(path, 60))


if __name__ == "__main__":
    unittest.main()
