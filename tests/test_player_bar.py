# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The player bar as a widget: what it shows with and without an item, and the compact
layout's room for the title (a smaller cover, the right-hand box gone). The bar's reveal is
the window's (Window._reveal_player_bar: shown only while something plays), which
scripts/a11y_check.py walks on the real window."""

import unittest

from tests.gtk import requires_gtk
from tests.test_player import TRACK, FakeApp
from tests.test_transport import RatingEngine

from applemusic.player import Player


@requires_gtk
class PlayerBarTest(unittest.TestCase):
    def setUp(self):
        from gi.repository import Adw, Gtk

        from applemusic.player_bar import PlayerBar

        self.app = FakeApp(RatingEngine())
        self.player = Player(self.app)
        self.player.track_grace_ms = 0
        self.bar = PlayerBar()
        # As the window holds it: a bottom sheet's bar, wrapped in the sheet's own button.
        self.sheet = Adw.BottomSheet(bottom_bar=self.bar, content=Gtk.Label())
        self.window = Adw.Window(content=self.sheet)
        self.addCleanup(self.window.destroy)
        self.bar.set_player(self.player, self.app)

    def test_with_nothing_playing_the_slider_and_the_heart_are_hidden(self):
        self.assertEqual(self.bar.title_label.get_label(), 'Not Playing')
        self.assertFalse(self.bar.seek_box.get_visible())
        self.assertFalse(self.bar.heart_button.get_visible())
        self.assertFalse(self.bar.explicit_badge.get_visible())

    def test_an_item_shows_its_titles_the_slider_and_the_heart(self):
        self.player.apply({'state': 'playing', 'track': dict(TRACK, explicit=True),
                           'position': 10, 'duration': 214})
        self.assertEqual(self.bar.title_label.get_label(), 'Harbour Lights')
        self.assertTrue(self.bar.subtitle_label.get_visible())
        self.assertTrue(self.bar.seek_box.get_visible())
        self.assertTrue(self.bar.heart_button.get_visible())
        self.assertTrue(self.bar.explicit_badge.get_visible())
        self.player.apply(None)
        self.assertEqual(self.bar.title_label.get_label(), 'Not Playing')
        self.assertFalse(self.bar.seek_box.get_visible())
        self.assertFalse(self.bar.explicit_badge.get_visible())

    def test_compact_shrinks_the_cover_and_drops_the_right_hand_box(self):
        from applemusic.player_bar import COMPACT_COVER_SIZE, COVER_SIZE

        self.assertEqual(self.bar.cover.size, COVER_SIZE)
        self.assertTrue(self.bar.controls_box.get_visible())
        self.bar.props.compact = True
        self.assertEqual(self.bar.cover.size, COMPACT_COVER_SIZE)
        self.assertFalse(self.bar.controls_box.get_visible())
        self.assertFalse(self.bar.elapsed_label.get_visible())
        self.assertFalse(self.bar.remaining_label.get_visible())
        self.bar.props.compact = False
        self.assertEqual(self.bar.cover.size, COVER_SIZE)
        self.assertTrue(self.bar.controls_box.get_visible())
        self.assertTrue(self.bar.elapsed_label.get_visible())


if __name__ == '__main__':
    unittest.main()
