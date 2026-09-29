# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""widgets/queue.py: Up Next, the Player's queue from the entry playing on, follows the queue
while it is open, whichever entry plays: entries added at the queue's end and a new queue
reach the list. (The slice's start is tested in test_transport.py's QueueSliceTest.)"""

import unittest

from tests import ROOT  # noqa: F401  registers src/ as applemusic
from tests.gtk import requires_gtk, wait_for
from tests.test_player import TRACK, make_player


def entries(count):
    """An invented queue of `count` entries, as the bridge's queue() shapes it."""
    return [dict(TRACK, id=f'i.queue{number:04}', catalogId=str(2000000000 + number),
                 title=f'Entry {number}', index=number) for number in range(count)]


@requires_gtk
class QueueViewTest(unittest.TestCase):
    def setUp(self):
        from gi.repository import Gtk

        from applemusic.widgets.queue import QueueView

        self.player, _engine, app = make_player()
        self.player.apply_queue({'index': 2, 'items': entries(5)})  # Entry 2 plays
        self.view = QueueView()
        self.view.set_player(self.player, app)
        window = Gtk.Window(default_width=400, default_height=600, child=self.view)
        window.present()
        self.addCleanup(window.destroy)
        self.assertTrue(wait_for(window.get_mapped))
        # The count the list view has been told: its model's items-changed, added up.
        self.model = self.view.list_view.get_model()
        self.told = self.model.get_n_items()
        self.model.connect('items-changed', self.on_items_changed)

    def on_items_changed(self, _model, _position, removed, added):
        self.told += added - removed

    def rows(self):
        """The titles of the rows the list view has bound, in order."""
        rows = self.view._rows
        return [rows[position].title_label.get_label() for position in sorted(rows)]

    def test_an_entry_added_at_the_end_reaches_the_list(self):
        from applemusic.player import NowPlaying

        self.assertEqual(self.told, 3)
        self.assertTrue(wait_for(lambda: self.rows() == ['Entry 2', 'Entry 3', 'Entry 4']))
        self.player.queue.append(NowPlaying(entries(6)[5]))
        self.assertEqual(self.model.get_n_items(), 4)
        self.assertEqual(self.told, 4)
        self.assertTrue(wait_for(lambda: self.rows()[-1:] == ['Entry 5']), self.rows())

    def test_a_longer_queue_from_musickit_reaches_the_list(self):
        # Play Later: MusicKit sends the whole queue again, one entry longer.
        self.player.apply_queue({'index': 2, 'items': entries(6)})
        self.assertEqual(self.told, 4)
        # The next entry plays, and another is added.
        self.player.apply_queue({'index': 3, 'items': entries(6)})
        self.assertEqual(self.told, 3)
        self.player.apply_queue({'index': 3, 'items': entries(7)})
        self.assertEqual(self.model.get_n_items(), 4)
        self.assertEqual(self.told, 4)
        self.assertTrue(wait_for(
            lambda: self.rows() == ['Entry 3', 'Entry 4', 'Entry 5', 'Entry 6']), self.rows())

    def test_a_shorter_queue_from_the_top(self):
        self.player.apply_queue({'index': 0, 'items': entries(2)})
        self.assertEqual(self.told, 2)
        self.assertTrue(wait_for(lambda: self.rows() == ['Entry 0', 'Entry 1']), self.rows())


if __name__ == '__main__':
    unittest.main()
