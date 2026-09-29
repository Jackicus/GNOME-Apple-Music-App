# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""widgets/shelf.py: a Shelf follows its shelf and offers See All and the paging arrows only
when the row does not show everything (no arrows in a narrow window); a ShelfColumn keeps the
widgets of the shelves it keeps, and never shows a shelf twice while it binds the rest a frame
apart."""

import unittest

from tests.page_harness import PageTestCase, album


def model(key, count, title=None):
    from applemusic.library import Item, ShelfModel

    items = [Item(dict(album(n), id=f'{key}.{n}')) for n in range(count)]
    return ShelfModel(key, title or f'Invented {key}', items)


class ShelfTest(PageTestCase):
    async def shown(self, shelf_model, width=None, **options):
        """A Shelf of shelf_model shown in the window, `width` pixels wide (or the window's
        width)."""
        from gi.repository import Adw

        from applemusic.widgets.shelf import Shelf

        shelf = Shelf(see_all=True, **options)
        shelf.bind_shelf(shelf_model)
        if width is None:
            self.window.root_box.append(shelf)
        else:
            self.window.root_box.append(Adw.Clamp(maximum_size=width,
                                                  tightening_threshold=width, child=shelf))
        adjustment = shelf.scrolled_window.get_hadjustment()
        self.assertTrue(await self.until(lambda: adjustment.get_page_size() > 0))
        await self.turn()
        return shelf

    async def test_a_short_row_offers_no_see_all_nor_arrows(self):
        shelf = await self.shown(model('short', 1))
        self.assertFalse(shelf.see_all_button.get_visible())
        self.assertFalse(shelf.next_button.get_visible())

    async def test_a_long_row_shows_its_first_tiles_see_all_and_the_arrows(self):
        from applemusic.widgets.shelf import ROW_LIMIT

        shelf = await self.shown(model('long', 40))
        self.assertEqual(shelf.list_view.get_model().get_n_items(), ROW_LIMIT)
        self.assertTrue(shelf.see_all_button.get_visible())
        self.assertTrue(shelf.next_button.get_visible())
        self.assertFalse(shelf.previous_button.get_sensitive())  # at the start
        self.assertTrue(shelf.next_button.get_sensitive())
        shelf.next_button.emit('clicked')  # animations are off: at once
        adjustment = shelf.scrolled_window.get_hadjustment()
        self.assertTrue(await self.until(lambda: adjustment.get_value() > 0))
        self.assertTrue(shelf.previous_button.get_sensitive())
        shelf.see_all_button.emit('clicked')
        self.assertEqual(len(self.window.shelves_opened), 1)

    async def test_a_narrow_row_leaves_the_arrows_out_for_its_title(self):
        from gi.repository import Gtk

        from applemusic.widgets.shelf import ARROWS_MIN_WIDTH, text_scale

        shelf = await self.shown(model('narrow', 40, title='Invented Rotations'), width=360)
        scale = text_scale(shelf.get_settings())
        adjustment = shelf.scrolled_window.get_hadjustment()
        self.assertLessEqual(shelf.get_width(), ARROWS_MIN_WIDTH * scale)
        self.assertGreater(adjustment.get_upper(), adjustment.get_page_size())  # overflows
        self.assertTrue(shelf.see_all_button.get_visible())
        self.assertFalse(shelf.previous_button.get_visible())
        self.assertFalse(shelf.next_button.get_visible())
        # The title has the room the arrows would have taken: all it asks for, here.
        title = shelf.title_label
        self.assertGreaterEqual(title.get_width(),
                                title.measure(Gtk.Orientation.HORIZONTAL, -1)[1])

    async def test_a_row_a_little_wider_keeps_its_arrows(self):
        from gi.repository import Gdk, Gtk

        from applemusic.widgets.shelf import ARROWS_MIN_WIDTH, text_scale

        # The app's stylesheet, whose padding on the row takes 30 px off its adjustment's page.
        provider = Gtk.CssProvider()
        provider.load_from_resource('/io/github/jackicus/AppleMusic/style.css')
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(display, provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.addCleanup(Gtk.StyleContext.remove_provider_for_display, display, provider)
        width = round(ARROWS_MIN_WIDTH * text_scale(Gtk.Settings.get_default())) + 20
        shelf = await self.shown(model('wider', 40), width=width)
        self.assertEqual(shelf.get_width(), width)  # the list's padding not taken off
        self.assertTrue(shelf.previous_button.get_visible())
        self.assertTrue(shelf.next_button.get_visible())

    def test_text_scale_follows_the_xft_dpi(self):
        from types import SimpleNamespace

        from applemusic.widgets.shelf import text_scale

        def settings(dpi):
            return SimpleNamespace(props=SimpleNamespace(gtk_xft_dpi=dpi))

        self.assertEqual(text_scale(settings(96 * 1024)), 1.0)
        self.assertEqual(text_scale(settings(144 * 1024)), 1.5)
        self.assertEqual(text_scale(settings(-1)), 1.0)  # not set
        self.assertEqual(text_scale(None), 1.0)

    async def test_see_all_follows_the_items(self):
        shelf_model = model('growing', 1)
        shelf = await self.shown(shelf_model)
        self.assertFalse(shelf.see_all_button.get_visible())
        more = model('more', 39)
        shelf_model.update(shelf_model.title, [shelf_model.items.get_item(0)]
                           + list(more.items))
        self.assertTrue(await self.until(shelf.see_all_button.get_visible))

    async def test_a_retitled_shelf_updates_its_heading(self):
        shelf_model = model('titled', 3, title='Old Title')
        shelf = await self.shown(shelf_model)
        self.assertEqual(shelf.title_label.get_label(), 'Old Title')
        shelf_model.update('New Title', list(shelf_model.items))
        self.assertEqual(shelf.title_label.get_label(), 'New Title')


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


class ShelfColumnTest(PageTestCase):
    def column(self):
        from gi.repository import Gtk

        from applemusic.widgets.shelf import ShelfColumn

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.anchor = Gtk.Label(label='Invented Page')
        self.box.append(self.anchor)
        self.window.root_box.append(self.box)
        return ShelfColumn(self.box, anchor=self.anchor)

    def shown(self):
        """The shelves the box shows, in order."""
        shown = []
        child = self.anchor.get_next_sibling()
        while child is not None:
            if child.get_visible():
                shown.append(child.shelf)
            child = child.get_next_sibling()
        return shown

    async def test_a_reshow_keeps_widgets_never_shows_a_shelf_twice_and_hides_extras(self):
        column = self.column()
        shelves = [model(f's{n}', 2) for n in range(6)]
        column.show(shelves, hero_first=True)
        self.assertEqual(self.shown(), shelves[:3])  # the first few at once
        self.assertTrue(await self.until(lambda: self.shown() == shelves))
        widgets = {shelf: widget for shelf, widget in zip(shelves, column.widgets,
                                                          strict=True)}
        self.assertTrue(widgets[shelves[0]].props.hero)

        # A new shelf at position 1, and the last one gone.
        inserted = model('new', 2)
        again = [shelves[0], inserted] + shelves[1:5]
        column.show(again, hero_first=True)
        seen = []
        while True:
            shown = self.shown()
            seen.append(shown)
            self.assertEqual(len(shown), len(set(shown)), shown)  # never twice
            self.assertNotIn(shelves[5], shown)  # gone at once
            if shown == again:
                break
            self.assertTrue(await self.until(lambda last=shown: self.shown() != last))
        for shelf in shelves[:5]:
            self.assertIs(column.widgets[again.index(shelf)], widgets[shelf])  # kept

        column.show(again[:2], hero_first=True)
        self.assertEqual(self.shown(), again[:2])
        hidden = [child for child in _descendants(self.box)
                  if child.get_parent() is self.box and not child.get_visible()]
        self.assertEqual(len(hidden), 4)  # kept, hidden, for later


if __name__ == '__main__':
    unittest.main()
