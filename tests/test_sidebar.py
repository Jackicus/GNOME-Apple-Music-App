# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Jack Tully

"""The Playlists section's entries (applemusic.sidebar), from invented playlist trees."""

import unittest

import gi

from tests import ROOT  # noqa: F401  registers src/ as applemusic

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from applemusic.library import Item, PlaylistTree  # noqa: E402
from applemusic.sections import ALL_PLAYLISTS, HOME, Destination  # noqa: E402
from applemusic.sidebar import (SidebarEntry, SidebarItem, is_shown,  # noqa: E402
                                parse_key, plan_update, playlist_entries, restore_target,
                                reveal, stale_roots, tooltip_markup)


def playlist(playlist_id, favourites=False, title=None):
    raw = {'id': playlist_id, 'kind': 'playlist', 'title': title or playlist_id.upper(),
           'groups': []}
    if favourites:
        raw['attributes'] = {'isFavourites': True}
    return Item(raw)


def tree(titles=None, folders=None):
    """root: folder a (folder b (p3), p1), p2, favourites; folder c (empty). `titles` renames
    playlists by id; `folders` replaces the folders' entries."""
    titles = titles or {}
    playlists = [playlist('p1', title=titles.get('p1')), playlist('p2', title=titles.get('p2')),
                 playlist('p3', title=titles.get('p3')), playlist('fav', True)]
    return PlaylistTree(folders or [
        {'id': 'root', 'title': '', 'parent': None, 'children': [
            {'kind': 'folder', 'id': 'a'}, {'kind': 'playlist', 'id': 'p2'},
            {'kind': 'playlist', 'id': 'fav'}, {'kind': 'folder', 'id': 'c'}]},
        {'id': 'a', 'title': 'Folder A', 'parent': 'root', 'children': [
            {'kind': 'folder', 'id': 'b'}, {'kind': 'playlist', 'id': 'p1'}]},
        {'id': 'b', 'title': 'Folder B', 'parent': 'a', 'children': [
            {'kind': 'playlist', 'id': 'p3'}]},
        {'id': 'c', 'title': 'Folder C', 'parent': 'root', 'children': []},
    ], playlists)


def by_key(entries):
    return {entry.key: entry for entry in entries}


class PlanUpdateTest(unittest.TestCase):
    """plan_update: the fewest changes that make the section list the new entries."""

    def test_identical_lists_need_nothing(self):
        retitles, splices = plan_update(playlist_entries(tree()), playlist_entries(tree()))
        self.assertEqual((retitles, splices), ([], []))

    def test_a_rename_is_one_retitle_and_no_splice(self):
        old = playlist_entries(tree())
        new = playlist_entries(tree(titles={'p1': 'Long Drive'}))
        retitles, splices = plan_update(old, new)
        self.assertEqual(splices, [])
        self.assertEqual([(index, entry.key, entry.title) for index, entry in retitles],
                         [(3, 'playlist:p1', 'Long Drive')])

    def test_an_insert_is_one_splice(self):
        old = playlist_entries(tree())
        folders = [
            {'id': 'root', 'title': '', 'parent': None, 'children': [
                {'kind': 'folder', 'id': 'a'}, {'kind': 'playlist', 'id': 'p2'},
                {'kind': 'playlist', 'id': 'p4'}, {'kind': 'folder', 'id': 'c'}]},
            {'id': 'a', 'title': 'Folder A', 'parent': 'root', 'children': [
                {'kind': 'folder', 'id': 'b'}, {'kind': 'playlist', 'id': 'p1'}]},
            {'id': 'b', 'title': 'Folder B', 'parent': 'a', 'children': [
                {'kind': 'playlist', 'id': 'p3'}]},
            {'id': 'c', 'title': 'Folder C', 'parent': 'root', 'children': []},
        ]
        new_tree = PlaylistTree(folders, [playlist('p1'), playlist('p2'), playlist('p3'),
                                          playlist('p4')])
        retitles, splices = plan_update(old, playlist_entries(new_tree))
        self.assertEqual(retitles, [])
        self.assertEqual([(index, count, [e.key for e in entries])
                          for index, count, entries in splices],
                         [(6, 0, ['playlist:p4'])])

    def test_a_rename_that_moves_an_entry_is_a_move(self):
        # Folder C renamed "Archive" goes before Folder A (folders sort by title): the
        # entry leaves its place and a new one comes in at the new place, under its new title.
        old = playlist_entries(tree())
        folders = [
            {'id': 'root', 'title': '', 'parent': None, 'children': [
                {'kind': 'folder', 'id': 'a'}, {'kind': 'playlist', 'id': 'p2'},
                {'kind': 'playlist', 'id': 'fav'}, {'kind': 'folder', 'id': 'c'}]},
            {'id': 'a', 'title': 'Folder A', 'parent': 'root', 'children': [
                {'kind': 'folder', 'id': 'b'}, {'kind': 'playlist', 'id': 'p1'}]},
            {'id': 'b', 'title': 'Folder B', 'parent': 'a', 'children': [
                {'kind': 'playlist', 'id': 'p3'}]},
            {'id': 'c', 'title': 'Archive', 'parent': 'root', 'children': []},
        ]
        new = playlist_entries(tree(folders=folders))
        self.assertEqual([e.key for e in new][:2], ['folder:c', 'folder:a'])
        retitles, splices = plan_update(old, new)
        self.assertEqual(retitles, [])
        entries = list(old)
        for index, count, inserted in splices:
            entries[index:index + count] = inserted
        self.assertEqual([e.shape() for e in entries], [e.shape() for e in new])
        self.assertEqual(sum(count for _index, count, _entries in splices), 1)  # only c went

    def test_a_move_between_folders_is_a_splice(self):
        old = playlist_entries(tree())
        folders = [  # p1 moves from folder a to folder c
            {'id': 'root', 'title': '', 'parent': None, 'children': [
                {'kind': 'folder', 'id': 'a'}, {'kind': 'playlist', 'id': 'p2'},
                {'kind': 'folder', 'id': 'c'}]},
            {'id': 'a', 'title': 'Folder A', 'parent': 'root', 'children': [
                {'kind': 'folder', 'id': 'b'}]},
            {'id': 'b', 'title': 'Folder B', 'parent': 'a', 'children': [
                {'kind': 'playlist', 'id': 'p3'}]},
            {'id': 'c', 'title': 'Folder C', 'parent': 'root', 'children': [
                {'kind': 'playlist', 'id': 'p1'}]},
        ]
        new = playlist_entries(tree(folders=folders))
        retitles, splices = plan_update(old, new)
        self.assertEqual(retitles, [])
        self.assertEqual([(index, count, [e.key for e in entries])
                          for index, count, entries in splices],
                         [(5, 0, ['playlist:p1']), (3, 1, [])])  # last first
        # Applied in that order, the old list becomes the new one.
        entries = list(old)
        for index, count, inserted in splices:
            entries[index:index + count] = inserted
        self.assertEqual([e.place() for e in entries], [e.place() for e in new])

    def test_everything_gone_is_one_splice(self):
        old = playlist_entries(tree())
        retitles, splices = plan_update(old, [])
        self.assertEqual(retitles, [])
        self.assertEqual([(index, count) for index, count, _entries in splices], [(0, 6)])


class DecisionsTest(unittest.TestCase):
    """restore_target, reveal and stale_roots over the invented tree."""

    def setUp(self):
        self.entries = by_key(playlist_entries(tree()))
        self.entries['home'] = Destination(HOME, 'Home', 'go-home-symbolic')
        self.entries[ALL_PLAYLISTS] = Destination(ALL_PLAYLISTS, 'All Playlists', 'x')

    def test_restore_a_key_the_sidebar_has(self):
        self.assertEqual(restore_target('home', self.entries, False), ('home', 'home'))
        self.assertEqual(restore_target('playlist:p1', self.entries, True),
                         ('playlist:p1', 'playlist:p1'))

    def test_restore_a_playlist_before_the_library_is_ready(self):
        self.assertEqual(restore_target('playlist:p9', {}, False), (ALL_PLAYLISTS, 'playlist:p9'))
        self.assertEqual(restore_target('folder:z', {}, False), (ALL_PLAYLISTS, 'folder:z'))

    def test_restore_what_is_gone_goes_home(self):
        self.assertEqual(restore_target('playlist:p9', self.entries, True), (HOME, HOME))
        self.assertEqual(restore_target('no-such-page', self.entries, False), (HOME, HOME))
        self.assertEqual(restore_target('', self.entries, True), (HOME, HOME))

    def test_reveal_the_folders_an_entry_is_in(self):
        self.assertEqual(reveal('playlist:p3', self.entries), ('a', 'b'))
        self.assertEqual(reveal('playlist:p1', self.entries), ('a',))
        self.assertEqual(reveal('playlist:p2', self.entries), ())
        self.assertEqual(reveal('home', self.entries), ())
        self.assertEqual(reveal('playlist:p9', self.entries), ())

    def test_stale_roots_are_the_playlists_and_folders_gone(self):
        roots = ['home', 'playlist:p1', 'playlist:p9', 'folder:c', 'folder:z']
        self.assertEqual(stale_roots(roots, self.entries, 'home'),
                         ['playlist:p9', 'folder:z'])
        # The page shown stays (it shows that its playlist is gone).
        self.assertEqual(stale_roots(roots, self.entries, 'playlist:p9'), ['folder:z'])


class TestEntries(unittest.TestCase):
    def test_depth_first_without_favourite_songs(self):
        # Folders first, then playlists, each by title, at every level (library.PlaylistTree).
        entries = playlist_entries(tree())
        self.assertEqual([(e.kind, e.key, e.title, e.depth, e.ancestors) for e in entries], [
            ('folder', 'folder:a', 'Folder A', 0, ()),
            ('folder', 'folder:b', 'Folder B', 1, ('a',)),
            ('playlist', 'playlist:p3', 'P3', 2, ('a', 'b')),
            ('playlist', 'playlist:p1', 'P1', 1, ('a',)),
            ('folder', 'folder:c', 'Folder C', 0, ()),
            ('playlist', 'playlist:p2', 'P2', 0, ()),
        ])
        self.assertEqual([e.icon_name for e in entries],
                         ['folder-symbolic'] * 2 + ['playlist-symbolic'] * 2
                         + ['folder-symbolic', 'playlist-symbolic'])
        self.assertEqual([e.folder_id for e in entries], ['a', 'b', None, None, 'c', None])
        self.assertEqual(entries[2].item.id, 'p3')

    def test_shown_when_every_folder_it_is_in_is_expanded(self):
        entries = playlist_entries(tree())

        def shown(expanded):
            return [e.key for e in entries if is_shown(e, set(expanded))]

        self.assertEqual(shown([]), ['folder:a', 'folder:c', 'playlist:p2'])
        self.assertEqual(shown(['a']), ['folder:a', 'folder:b', 'playlist:p1', 'folder:c',
                                        'playlist:p2'])
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

    def test_nested_entries_name_their_folder(self):
        entries = {entry.key: entry for entry in playlist_entries(tree())}
        self.assertEqual(entries['playlist:p3'].parent_title, 'Folder B')
        self.assertEqual(entries['folder:b'].parent_title, 'Folder A')
        self.assertEqual(entries['playlist:p2'].parent_title, '')  # the top level
        self.assertEqual(entries['folder:a'].parent_title, '')

    def test_tooltip_is_the_whole_name_escaped(self):
        self.assertEqual(tooltip_markup('A & B <C>'), 'A &amp; B &lt;C&gt;')
        self.assertEqual(tooltip_markup(None), '')
        # A playlist's item (no widget inside: a folder's arrow would need a display).
        item = SidebarItem(SidebarEntry('playlist', 'playlist:p9', 'A & B <C>', 'x', 1,
                                        playlist('p9', title='A & B <C>'), ('a',), 'Folder A'))
        self.assertEqual(item.get_tooltip(), 'A &amp; B &lt;C&gt;')
        self.assertEqual(item.get_subtitle(), 'Folder A')
        item.retitle('C <D>')
        self.assertEqual((item.get_title(), item.entry.title, item.get_tooltip()),
                         ('C <D>', 'C <D>', 'C &lt;D&gt;'))
        fixed = SidebarItem(SidebarEntry.fixed(Destination('all-playlists', 'All', 'x')))
        self.assertFalse(fixed.get_tooltip())
        self.assertFalse(fixed.get_subtitle())


if __name__ == '__main__':
    unittest.main()
