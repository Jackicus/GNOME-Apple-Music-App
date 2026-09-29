import pathlib
import unittest

from tests import SRC

from applemusic import sections

ADWAITA_SYMBOLIC = pathlib.Path('/usr/share/icons/Adwaita/symbolic')


def all_destinations():
    return [d for _title, destinations in sections.sidebar_sections() for d in destinations]


class SectionsTest(unittest.TestCase):
    def test_keys_unique_and_non_empty(self):
        keys = [d.key for d in all_destinations()]
        self.assertTrue(keys)
        self.assertTrue(all(keys), keys)
        self.assertEqual(len(keys), len(set(keys)), keys)

    def test_home_exists(self):
        self.assertIn('home', [d.key for d in all_destinations()])

    def test_every_destination_has_a_page(self):
        from applemusic import pages

        keys = [d.key for d in all_destinations()]
        self.assertEqual(sorted(keys), sorted(pages.PAGES))

    def test_titles_non_empty(self):
        for destination in all_destinations():
            self.assertTrue(destination.title, destination.key)

    @unittest.skipUnless(ADWAITA_SYMBOLIC.is_dir(), 'Adwaita icon theme not installed')
    def test_icons_exist(self):
        bundled = {p.stem for p in (SRC / 'icons').glob('*.svg')}
        adwaita = {p.stem for p in ADWAITA_SYMBOLIC.rglob('*.svg')}
        gresource = (SRC / 'applemusic.gresource.xml').read_text()
        for destination in all_destinations():
            name = destination.icon_name
            with self.subTest(icon=name):
                self.assertTrue(name in bundled or name in adwaita,
                                f'{name} is neither in src/icons/ nor in Adwaita')
                if name in bundled and name not in adwaita:
                    self.assertIn(f'alias="icons/scalable/actions/{name}.svg"', gresource,
                                  f'{name} is bundled but not aliased in the gresource')


if __name__ == '__main__':
    unittest.main()
