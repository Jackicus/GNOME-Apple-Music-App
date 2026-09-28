"""pages/shelves.py: the headings of the engine's shelves, in the app's words."""

import unittest
from unittest import mock

from tests import ROOT  # noqa: F401  (registers src/ as the applemusic package)
from tests.gtk import requires_gtk


@requires_gtk  # the module defines a template class: it needs the gresource
class ShelfTitleTest(unittest.TestCase):
    def setUp(self):
        from applemusic.pages import shelves

        self.shelves = shelves
        # Every word through gettext: marked, so a missed one shows.
        patcher = mock.patch.object(shelves, '_', lambda text: f'<{text}>')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_app_names_what_the_backend_keys(self):
        title = self.shelves.shelf_title
        self.assertEqual(title({'key': 'top', 'title': ''}), '<Top Results>')
        self.assertEqual(title({'key': 'music-videos', 'title': ''}), '<Music Videos>')
        self.assertEqual(title({'key': 'new-banners', 'title': '', 'featured': True}),
                         '<Featured>')
        # Apple's own titles come in the account's language, and stay.
        self.assertEqual(title({'key': 'new-best', 'title': 'Best New Songs'}), 'Best New Songs')
        # A recommendation Apple left untitled (Made for You's).
        self.assertEqual(title({'key': 'rec-1', 'title': ''}), '<Made for You>')

    def test_remote_shelves_are_titled_by_it(self):
        shelves = self.shelves.remote_shelves([
            {'key': 'albums', 'title': '', 'items': [{'id': '1', 'kind': 'album', 'title': 'A'}]},
            {'key': 'empty', 'title': 'Nothing', 'items': []},
        ])
        self.assertEqual([(shelf.key, shelf.title) for shelf in shelves], [('albums', '<Albums>')])


if __name__ == '__main__':
    unittest.main()
