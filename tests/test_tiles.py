"""The artwork widgets: tiles, cards, covers, the Songs title cell, category tiles and the
artist portrait, each drawing through an ArtworkSlot, with the Artwork loader replaced by
test_artwork's FakeLoader (nothing is decoded: the tests answer the requests).

Each test shows its widget in a presented window (tests/gtk.py) and lets the main context run.
"""

import gc
import time
import unittest
from unittest import mock

from tests import ROOT  # noqa: F401  registers src/ as applemusic
from tests.gtk import pump, requires_gtk, wait_for
from tests.test_artwork import FakeLoader

from applemusic.library import Item

ALBUM = {'id': 'l.a1', 'kind': 'album', 'title': 'Invented Album', 'subtitle': 'Invented Artist',
         'thumb': '/cache/thumb/a1.jpg', 'art': '/cache/art/a1.jpg'}


def texture(size=4):
    """A real texture of `size` px square: the widgets hand it to GTK."""
    from gi.repository import Gdk, GLib

    data = GLib.Bytes.new(bytes(size * size * 4))
    return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.R8G8B8A8, data, size * 4)


@requires_gtk
class WidgetTestCase(unittest.TestCase):
    """A presented window with a box to show widgets in, and a FakeLoader as the loader."""

    @classmethod
    def setUpClass(cls):
        from gi.repository import Gtk

        cls.window = Gtk.Window(default_width=600, default_height=400)
        cls.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        cls.window.set_child(cls.box)
        cls.window.present()
        wait_for(cls.window.get_mapped, timeout=2)

    @classmethod
    def tearDownClass(cls):
        cls.window.destroy()
        pump()
        del cls.window, cls.box

    def setUp(self):
        from applemusic.widgets import artwork

        self.loader = FakeLoader()
        patcher = mock.patch.object(artwork, '_default', self.loader)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.clear)

    def clear(self):
        child = self.box.get_first_child()
        while child is not None:
            self.box.remove(child)
            child = self.box.get_first_child()
        pump()

    def show(self, widget):
        self.box.append(widget)
        self.assertTrue(wait_for(widget.get_mapped))
        pump()
        return widget

    def scale(self, widget):
        return widget.get_scale_factor()

    def drop(self, widget):
        """Remove the widget from the window: a GObject weak reference to it (widgets/util.py).
        The caller then lets go of its own reference, and assert_freed(ref)."""
        ref = widget.weak_ref()
        widget.get_parent().remove(widget)
        return ref

    def assert_freed(self, ref):
        deadline = time.monotonic() + 0.5
        while ref() is not None and time.monotonic() < deadline:
            pump(10)
            gc.collect()
        self.assertIsNone(ref(), 'still alive')


class CoverTest(WidgetTestCase):
    def test_a_cached_path_is_drawn_at_once(self):
        from applemusic.widgets.cover import Cover

        cover = self.show(Cover(size=40))
        shown = texture()
        self.loader.cache[('/a/thumb', 40 * self.scale(cover))] = shown
        cover.set_paths('/a/art', '/a/thumb')
        self.assertIs(cover.picture.get_paintable(), shown)
        self.assertEqual(cover.placeholder_icon.get_opacity(), 0)

    def test_a_new_size_asks_again(self):
        from applemusic.widgets.cover import Cover

        cover = self.show(Cover(size=40))
        scale = self.scale(cover)
        cover.set_paths('/a/art')
        pump()
        self.assertEqual(self.loader.requests, [('/a/art', 40 * scale)])
        paintable = cover.picture.get_paintable()
        self.assertEqual(paintable.get_intrinsic_width(), 40 * scale)  # empty(), not None
        cover.set_property('size', 60)
        pump()
        self.assertEqual(self.loader.requests[-1], ('/a/art', 60 * scale))
        self.loader.answer('/a/art', texture())
        self.assertEqual(cover.placeholder_icon.get_opacity(), 0)

    def test_a_cover_built_by_a_template_follows_its_size(self):
        from applemusic.widgets.track_row import TrackRow

        row = self.show(TrackRow())
        row.cover.set_visible(True)
        row.cover.set_paths('/a/thumb')
        pump()
        self.assertEqual(self.loader.requests, [('/a/thumb', 40 * self.scale(row))])

    def test_freed(self):
        from applemusic.widgets.cover import Cover

        cover = self.show(Cover(size=40))
        cover.set_paths('/a/thumb')
        pump()  # a decode asked for, still waiting
        ref = self.drop(cover)
        del cover
        self.assert_freed(ref)


class SongTitleTest(WidgetTestCase):
    def test_the_thumbnail_keeps_its_size_and_the_title_follows_it(self):
        from applemusic.library import Track
        from applemusic.widgets.song_title import SongTitle

        cell = SongTitle(width_request=400)
        cell.bind(Track({'id': 'i.1', 'title': 'Invented Song', 'thumb': '/a/thumb'}))
        self.show(cell)
        self.assertTrue(wait_for(lambda: cell.label.get_width() > 0))  # laid out
        self.assertEqual(cell.cover.get_width(), 32)
        self.assertEqual(self.loader.requests, [('/a/thumb', 32 * self.scale(cell))])
        placed, bounds = cell.label.compute_bounds(cell)
        self.assertTrue(placed)
        self.assertLess(bounds.get_x(), 32 + 12 + 1)  # right after the thumbnail and spacing

    def test_freed(self):
        from applemusic.library import Track
        from applemusic.widgets.song_title import SongTitle

        cell = SongTitle()
        cell.bind(Track({'id': 'i.1', 'title': 'Invented Song', 'thumb': '/a/thumb'}))
        self.show(cell)
        ref = self.drop(cell)
        del cell
        self.assert_freed(ref)


class TileArtTest(WidgetTestCase):
    def test_the_thumbnail_is_asked_for_at_the_size_drawn(self):
        from applemusic.widgets.tile import ART_SIZE, Tile

        tile = self.show(Tile())
        tile.bind(Item(dict(ALBUM)))
        paintable = tile.picture.get_paintable()
        self.assertEqual(paintable.get_intrinsic_width(), ART_SIZE * self.scale(tile))
        self.assertEqual(tile.placeholder_icon.get_opacity(), 1)
        pump()
        self.assertEqual(self.loader.requests, [(ALBUM['thumb'], ART_SIZE * self.scale(tile))])
        shown = texture()
        self.loader.answer(ALBUM['thumb'], shown)
        self.assertIs(tile.picture.get_paintable(), shown)
        self.assertEqual(tile.placeholder_icon.get_opacity(), 0)
        tile.unbind()
        self.assertIsNot(tile.picture.get_paintable(), shown)

    def test_an_artist_tile_shows_its_portrait(self):
        from applemusic.widgets.tile import Tile

        tile = self.show(Tile(artist=True))
        tile.bind(Item({'id': 'l.r1', 'kind': 'artist', 'title': 'Invented Artist',
                        'thumb': '/a/portrait'}))
        pump()
        self.assertIsNone(tile.avatar.get_custom_image())
        shown = texture()
        self.loader.answer('/a/portrait', shown)
        self.assertIs(tile.avatar.get_custom_image(), shown)
        tile.unbind()
        self.assertIsNone(tile.avatar.get_custom_image())

    def test_unmapped_it_lets_go(self):
        from applemusic.widgets.tile import Tile

        tile = self.show(Tile())
        tile.bind(Item(dict(ALBUM)))
        pump()
        tile.set_visible(False)
        pump()
        self.assertEqual(len(self.loader.cancelled), 1)
        self.assertEqual(self.loader.waiting, {})

    def test_freed(self):
        from applemusic.widgets.tile import Tile

        tile = self.show(Tile())
        tile.bind(Item(dict(ALBUM)))
        pump()
        ref = self.drop(tile)
        del tile
        self.assert_freed(ref)


class FollowTest(WidgetTestCase):
    """A bound tile or card follows its Item's notify signals: a list rebinds nothing for an
    Item that changed in place (a reload's merge, a fetched thumbnail)."""

    def test_a_tile_follows_a_merge(self):
        from applemusic.widgets.tile import Tile

        item = Item(dict(ALBUM))
        tile = self.show(Tile())
        tile.bind(item)
        item.merge(dict(ALBUM, title='New Title', subtitle='New Artist'), replace=True)
        self.assertEqual(tile.label.get_text(), 'New Title\nNew Artist')
        item.merge(dict(ALBUM, title='Newer', subtitle=''), replace=True)
        self.assertEqual(tile.label.get_text(), 'Newer')
        # A new thumbnail path is shown (the old file is pruned by the sync).
        pump()
        item.merge(dict(ALBUM, title='Newer', subtitle='', thumb='/cache/thumb/new.jpg'),
                   replace=True)
        pump()
        self.assertEqual(self.loader.requests[-1][0], '/cache/thumb/new.jpg')

        tile.unbind()
        item.merge(dict(ALBUM, title='After Unbind'), replace=True)
        self.assertEqual(tile.label.get_text(), 'Newer')

    def test_a_tile_looks_again_when_its_thumbnail_arrives(self):
        from applemusic.widgets.tile import Tile

        item = Item(dict(ALBUM))
        tile = self.show(Tile())
        tile.bind(item)
        pump()
        self.loader.answer(ALBUM['thumb'], None)  # not on disk yet
        self.assertEqual(len(self.loader.requests), 1)
        item.notify('thumb')  # remote.fetch_shelf_art: the file has arrived, same path
        pump()
        self.assertEqual(len(self.loader.requests), 2)
        shown = texture()
        self.loader.answer(ALBUM['thumb'], shown)
        self.assertIs(tile.picture.get_paintable(), shown)

    def test_a_tile_rebound_follows_only_its_new_item(self):
        from applemusic.widgets.tile import Tile

        first, second = Item(dict(ALBUM)), Item(dict(ALBUM, id='l.a2', title='Second'))
        tile = self.show(Tile())
        tile.bind(first)
        tile.unbind()
        tile.bind(second)
        first.merge(dict(ALBUM, title='Not Shown'), replace=True)
        self.assertTrue(tile.label.get_text().startswith('Second'))

    def test_a_freed_tile_is_let_go_of(self):
        from applemusic.widgets.tile import Tile

        item = Item(dict(ALBUM))
        tile = self.show(Tile())
        tile.bind(item)  # and never unbound: dropped with its view
        ref = self.drop(tile)
        del tile
        self.assert_freed(ref)
        item.merge(dict(ALBUM, title='Later'), replace=True)  # the handler goes, quietly

    def test_a_hero_card_follows_its_item(self):
        from applemusic.widgets.hero_tile import HeroTile

        item = Item(dict(ALBUM, artColor='#1b4965'))
        card = self.show(HeroTile())
        card.bind(item)
        self.assertTrue(card.has_css_class('dark-art'))
        item.merge(dict(ALBUM, title='New Title', subtitle='New Artist', artColor='#ffd166'),
                   replace=True)
        self.assertEqual(card.title_label.get_text(), 'New Title')
        self.assertEqual(card.subtitle_label.get_text(), 'New Artist')
        self.assertTrue(card.has_css_class('light-art'))
        pump()
        count = len(self.loader.requests)
        item.notify('thumb')
        pump()
        self.assertGreater(len(self.loader.requests), count)
        card.unbind()
        item.merge(dict(ALBUM, title='After Unbind'), replace=True)
        self.assertEqual(card.title_label.get_text(), 'New Title')

    def test_a_category_tile_follows_its_item(self):
        from applemusic.widgets.category_tile import CategoryTile

        item = Item({'id': 'c1', 'kind': 'category', 'title': 'Invented Category',
                     'thumb': '/a/category', 'artColor': '#1b4965'})
        tile = CategoryTile()
        tile.bind(item)
        self.show(tile)
        item.merge({'id': 'c1', 'kind': 'category', 'title': 'Renamed', 'thumb': '/a/category',
                    'artColor': '#ffd166'}, replace=True)
        self.assertEqual(tile.title_label.get_text(), 'Renamed')
        self.assertTrue(tile.has_css_class('light-art'))
        count = len(self.loader.requests)
        item.notify('thumb')
        pump()
        self.assertGreater(len(self.loader.requests), count)


class HeroCoverTest(WidgetTestCase):
    """A hero card fetches its 640 px cover when the thumbnail is too small for it."""

    def setUp(self):
        super().setUp()
        from applemusic.widgets import hero_tile

        self.fetched = []
        self.spawned = []

        async def fetch_cover(item):
            self.fetched.append(item.id)
            return True

        app = mock.Mock(spawn=self.spawned.append)
        for target, name, value in ((hero_tile, 'fetch_cover', fetch_cover),
                                    (hero_tile.Gio.Application, 'get_default', lambda: app)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_spawned(self):
        import asyncio

        async def run_all():
            for coroutine in self.spawned:
                await coroutine

        asyncio.run(run_all())
        self.spawned.clear()

    def card(self, scale):
        from applemusic.widgets.hero_tile import HeroTile

        patcher = mock.patch.object(HeroTile, 'get_scale_factor', lambda _self: scale)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.show(HeroTile())

    def test_a_mapped_card_fetches_its_cover_once_bound(self):
        card = self.card(scale=2)
        card.bind(Item(dict(ALBUM, artUrl='https://x.invalid/a1/640x640bb.jpg')))
        self.assertEqual(len(self.spawned), 1)
        pump()
        count = len(self.loader.requests)
        self.run_spawned()
        self.assertEqual(self.fetched, [ALBUM['id']])
        pump()
        self.assertGreater(len(self.loader.requests), count)  # the cover looked for again
        self.assertEqual(self.loader.requests[-1][0], ALBUM['art'])

    def test_no_fetch_without_a_url_or_at_one_times(self):
        card = self.card(scale=2)
        card.bind(Item(dict(ALBUM)))  # no artUrl: a library item the sync did not name
        card.unbind()
        self.assertEqual(self.spawned, [])
        card = self.card(scale=1)  # 260 px: the 320 px thumbnail is enough
        card.bind(Item(dict(ALBUM, artUrl='https://x.invalid/a1/640x640bb.jpg')))
        self.assertEqual(self.spawned, [])

    def test_freed(self):
        card = self.card(scale=2)
        card.bind(Item(dict(ALBUM)))
        pump()
        ref = self.drop(card)
        del card
        self.assert_freed(ref)

    def test_a_card_rebound_meanwhile_is_left_alone(self):
        card = self.card(scale=2)
        card.bind(Item(dict(ALBUM, artUrl='https://x.invalid/a1/640x640bb.jpg')))
        card.unbind()
        card.bind(Item(dict(ALBUM, id='l.a2', art='/cache/art/a2.jpg',
                            thumb='/cache/thumb/a2.jpg')))
        pump()
        count = len(self.loader.requests)
        self.run_spawned()
        pump()
        self.assertEqual(len(self.loader.requests), count)


class TileLabelTest(WidgetTestCase):
    """The title and subtitle, one Gtk.Inscription: markup for a subtitle, text without."""

    def attributes(self, tile):
        attributes = tile.label.get_attributes()
        return [] if attributes is None else attributes.get_attributes()

    def test_a_plain_title_after_a_subtitled_one_has_no_attributes(self):
        from applemusic.widgets.tile import Tile

        tile = Tile()
        tile.bind(Item({'id': 'l.p1', 'kind': 'playlist', 'title': 'Mix',
                        'subtitle': 'Invented Curator'}))
        self.assertNotEqual(self.attributes(tile), [])  # the subtitle's size and dimming
        tile.unbind()
        tile.bind(Item({'id': 'l.f1', 'kind': 'folder', 'title': 'Weekend Roadtrip Folder'}))
        self.assertEqual(self.attributes(tile), [])
        self.assertEqual(tile.label.get_text(), 'Weekend Roadtrip Folder')

    def test_an_artist_after_a_subtitled_tile_has_no_attributes(self):
        from applemusic.widgets.tile import Tile

        tile = Tile()
        tile.bind(Item(dict(ALBUM)))
        tile.unbind()
        tile.set_artist(True)
        tile.bind(Item({'id': 'l.r1', 'kind': 'artist', 'title': 'Invented Artist Name',
                        'subtitle': '3 albums'}))
        self.assertEqual(self.attributes(tile), [])
        self.assertEqual(tile.label.get_text(), 'Invented Artist Name')


class CategoryTileArtTest(WidgetTestCase):
    CATEGORY = {'id': 'c1', 'kind': 'category', 'title': 'Invented Category',
                'thumb': '/a/category', 'artColor': '#1b4965'}

    def test_without_a_colour_the_theme_colours_the_text(self):
        from applemusic.widgets.category_tile import CategoryTile

        item = Item(dict(self.CATEGORY, artColor=None))
        tile = CategoryTile()
        tile.bind(item)
        self.assertFalse(tile.has_css_class('dark-art'))
        self.assertFalse(tile.has_css_class('light-art'))
        # A colour that arrives later, and goes again.
        item.merge(dict(self.CATEGORY, artColor='#ffd166'), replace=True)
        self.assertTrue(tile.has_css_class('light-art'))
        item.merge(dict(self.CATEGORY, artColor=None), replace=True)
        self.assertFalse(tile.has_css_class('dark-art') or tile.has_css_class('light-art'))

    def test_decoded_at_its_size_and_never_none(self):
        from applemusic.widgets.category_tile import ART_SIZE, CategoryTile

        tile = CategoryTile()
        tile.bind(Item(dict(self.CATEGORY)))
        self.show(tile)
        paintable = tile.picture.get_paintable()
        self.assertIsNotNone(paintable)
        self.assertEqual(paintable.get_intrinsic_width(), ART_SIZE * self.scale(tile))
        self.assertEqual(self.loader.requests, [('/a/category', ART_SIZE * self.scale(tile))])
        shown = texture()
        self.loader.answer('/a/category', shown)
        self.assertIs(tile.picture.get_paintable(), shown)

    def test_freed(self):
        from applemusic.widgets.category_tile import CategoryTile

        tile = CategoryTile()
        tile.bind(Item(dict(self.CATEGORY)))
        self.show(tile)
        ref = self.drop(tile)
        del tile
        self.assert_freed(ref)


class PortraitTest(WidgetTestCase):
    def test_the_thumbnail_shows_while_the_cover_is_missing(self):
        from gi.repository import Adw

        from applemusic.library import Library
        from applemusic.pages.artist import ArtistPage

        item = Item({'id': 'l.r1', 'kind': 'artist', 'title': 'Invented Artist',
                     'art': '/a/cover', 'thumb': '/a/thumb', 'groups': []})
        view = Adw.NavigationView()
        with mock.patch.object(ArtistPage, '_fetch'):  # an artist without albums asks for them
            page = ArtistPage(Library(), item)
        view.add(page)
        self.show(view)
        self.assertTrue(wait_for(page.avatar.get_mapped))
        pump()
        size = page.avatar.get_size() * self.scale(page)
        self.assertEqual(self.loader.requests, [('/a/cover', size)])
        self.loader.answer('/a/cover', None)  # not on disk
        self.assertEqual(self.loader.requests[-1], ('/a/thumb', size))
        shown = texture()
        self.loader.answer('/a/thumb', shown)
        self.assertIs(page.avatar.get_custom_image(), shown)


if __name__ == '__main__':
    unittest.main()
