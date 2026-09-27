"""The Playlists section's entries (applemusic.sidebar), from invented playlist trees."""

import unittest

import gi

from tests import ROOT  # noqa: F401  registers src/ as applemusic

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from applemusic.library import Item, PlaylistTree  # noqa: E402
from applemusic.sections import Destination  # noqa: E402
from applemusic.sidebar import (SidebarEntry, is_shown, parse_key,  # noqa: E402
                                playlist_entries)


def playlist(playlist_id, favourites=False):
    raw = {'id': playlist_id, 'kind': 'playlist', 'title': playlist_id.upper(), 'groups': []}
    if favourites:
        raw['attributes'] = {'isFavourites': True}
    return Item(raw)


def tree():
    """root: folder a (folder b (p3), p1), p2, favourites; folder c (empty)."""
    playlists = [playlist('p1'), playlist('p2'), playlist('p3'), playlist('fav', True)]
    return PlaylistTree([
        {'id': 'root', 'title': '', 'parent': None, 'children': [
            {'kind': 'folder', 'id': 'a'}, {'kind': 'playlist', 'id': 'p2'},
            {'kind': 'playlist', 'id': 'fav'}, {'kind': 'folder', 'id': 'c'}]},
        {'id': 'a', 'title': 'Folder A', 'parent': 'root', 'children': [
            {'kind': 'folder', 'id': 'b'}, {'kind': 'playlist', 'id': 'p1'}]},
        {'id': 'b', 'title': 'Folder B', 'parent': 'a', 'children': [
            {'kind': 'playlist', 'id': 'p3'}]},
        {'id': 'c', 'title': 'Folder C', 'parent': 'root', 'children': []},
    ], playlists)


class TestEntries(unittest.TestCase):
    def test_depth_first_without_favourite_songs(self):
        entries = playlist_entries(tree())
        self.assertEqual([(e.kind, e.key, e.title, e.depth, e.ancestors) for e in entries], [
            ('folder', 'folder:a', 'Folder A', 0, ()),
            ('folder', 'folder:b', 'Folder B', 1, ('a',)),
            ('playlist', 'playlist:p3', 'P3', 2, ('a', 'b')),
            ('playlist', 'playlist:p1', 'P1', 1, ('a',)),
            ('playlist', 'playlist:p2', 'P2', 0, ()),
            ('folder', 'folder:c', 'Folder C', 0, ()),
        ])
        self.assertEqual([e.icon_name for e in entries],
                         ['folder-symbolic'] * 2 + ['playlist-symbolic'] * 3 + ['folder-symbolic'])
        self.assertEqual([e.folder_id for e in entries], ['a', 'b', None, None, None, 'c'])
        self.assertEqual(entries[2].item.id, 'p3')

    def test_shown_when_every_folder_it_is_in_is_expanded(self):
        entries = playlist_entries(tree())

        def shown(expanded):
            return [e.key for e in entries if is_shown(e, set(expanded))]

        self.assertEqual(shown([]), ['folder:a', 'playlist:p2', 'folder:c'])
        self.assertEqual(shown(['a']), ['folder:a', 'folder:b', 'playlist:p1', 'playlist:p2',
                                        'folder:c'])
        self.assertEqual(shown(['a', 'b']), [e.key for e in entries])
        self.assertEqual(shown(['b']), shown([]))  # b is inside collapsed a

    def test_fixed_entry_and_shape(self):
        entry = SidebarEntry.fixed(Destination('all-playlists', 'All Playlists', 'view-app-grid'))
        self.assertEqual((entry.kind, entry.key, entry.title, entry.depth, entry.item),
                         ('fixed', 'all-playlists', 'All Playlists', 0, None))
        self.assertIsNone(entry.folder_id)
        # A reload of the same tree gives entries of the same shapes, with new Items.
        first, second = playlist_entries(tree()), playlist_entries(tree())
        self.assertEqual([e.shape() for e in first], [e.shape() for e in second])
        self.assertIsNot(first[0].item, second[0].item)

    def test_parse_key(self):
        self.assertEqual(parse_key('playlist:p.1:x'), ('playlist', 'p.1:x'))
        self.assertEqual(parse_key('folder:root'), ('folder', 'root'))
        for key in ('home', 'playlist:', 'album:l.1', '', None):
            with self.subTest(key=key):
                self.assertIsNone(parse_key(key))


if __name__ == '__main__':
    unittest.main()
