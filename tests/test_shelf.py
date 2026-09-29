# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""widgets/shelf.py: a Shelf follows its shelf and offers See All and the paging arrows only
when the row does not show everything; a ShelfColumn keeps the widgets of the shelves it
keeps, and never shows a shelf twice while it binds the rest a frame apart."""

import unittest

from tests.page_harness import PageTestCase, album


def model(key, count, title=None):
    from applemusic.library import Item, ShelfModel

    items = [Item(dict(album(n), id=f'{key}.{n}')) for n in range(count)]
    return ShelfModel(key, title or f'Invented {key}', items)


class ShelfTest(PageTestCase):
    async def shown(self, shelf_model, **options):
        from applemusic.widgets.shelf import Shelf

        shelf = Shelf(see_all=True, **options)
        shelf.bind_shelf(shelf_model)
        self.window.root_box.append(shelf)
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
