# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The Radio page (pages/radio.py) over an invented library.json: it shows the library's own
store, so a reload that renames stations reaches the cards and the tiles, and "More Stations"
shows only when there are more than the cards."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.page_harness import PageTestCase


def station(number, title):
    return {'id': f'ra.{number}', 'kind': 'station', 'title': title,
            'subtitle': 'Invented Radio', 'art': None, 'thumb': None,
            'play': {'kind': 'station', 'id': f'ra.{number}'}}


class RadioPageTest(PageTestCase):
    async def asyncSetUp(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.cache = cache.name
        patcher = mock.patch.dict(os.environ, {'APPLE_MUSIC_CACHE': self.cache})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, titles):
        data = {'version': 1, 'generated': '2026-01-01T00:00:00Z', 'storefront': 'gb',
                'sections': {'albums': [], 'playlists': [],
                             'radio': [station(n, title) for n, title in enumerate(titles)]},
                'shelves': []}
        Path(self.cache, 'library.json').write_text(json.dumps(data), encoding='utf-8')

    async def radio_page(self, titles):
        from applemusic.pages.radio import RadioPage

        self.write(titles)
        await self.library.load()
        return await self.show_root(RadioPage(self.library, 'Radio'))

    def card_title(self, page):
        from applemusic.widgets.hero_tile import HeroTile

        cards = [widget for widget in _descendants(page.hero_shelf.list_view)
                 if isinstance(widget, HeroTile) and widget.context_item is not None]
        return next(card.title_label.get_text() for card in cards
                    if card.context_item.id == 'ra.0')

    def tile_title(self, page):
        label = page.flow_box.get_child_at_index(0).get_child().label.get_text()
        return label.split('\n')[0]  # the title, over the subtitle

    async def test_a_reload_that_renames_stations_reaches_the_cards_and_the_tiles(self):
        page = await self.radio_page(['S0', 'S1', 'S2', 'S3', 'S4'])
        self.assertTrue(await self.until(lambda: self.card_title(page) == 'S0'))
        self.assertEqual(self.tile_title(page), 'S4')
        self.write(['S0 Renamed', 'S1', 'S2', 'S3', 'S4 Renamed'])
        await self.library.reload()
        self.assertTrue(await self.until(lambda: self.card_title(page) == 'S0 Renamed'))
        self.assertTrue(await self.until(lambda: self.tile_title(page) == 'S4 Renamed'))

    async def test_more_stations_only_with_more_than_the_cards(self):
        page = await self.radio_page(['S0', 'S1', 'S2', 'S3'])
        self.assertFalse(page.more_box.get_visible())
        self.write(['S0', 'S1', 'S2', 'S3', 'S4'])
        await self.library.reload()
        self.assertTrue(await self.until(page.more_box.get_visible))


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


if __name__ == '__main__':
    unittest.main()
