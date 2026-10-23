import unittest
from datetime import datetime
from qas_custom.services.teaching_resource_rules import *


class ResourceRules(unittest.TestCase):
    def test_multiple_formats_and_sizes(self):
        for name, kind in [('a.MP4', 'video'), ('a.ppt', 'ppt'), ('a.pptx', 'ppt')]:
            self.assertEqual(validate_file(name, 100)[1], kind)
        for name, size in [('a.exe', 1), ('folder/a.mp4', 1), ('a.mp4', 0), ('a.mp4', MAX_SIZE + 1), ('a.mp4', True), ('a.mp4', 1.5), ('a\n.mp4', 10)]:
            with self.subTest(name=name, size=size), self.assertRaises(ValueError):
                validate_file(name, size)

    def test_system_sidecars_are_not_accepted_as_video_or_ppt(self):
        for name in ['._02_Eagle_Part_1_EN.mp4', '._02_Eagle_EN.pptx', '~$02_Eagle_EN.pptx', '.DS_Store', 'Thumbs.db']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_file(name, 4096)

    def test_parts_must_match_size_and_order(self):
        validate_parts([{'PartNumber': 1, 'Size': PART_SIZE}, {'PartNumber': 2, 'Size': 7}], PART_SIZE + 7)
        for rows in [[], [{'PartNumber': 1, 'Size': 1}], [{'PartNumber': 2, 'Size': 7}, {'PartNumber': 1, 'Size': PART_SIZE}]]:
            with self.assertRaises(ValueError):
                validate_parts(rows, PART_SIZE + 7)

    def test_end_day_is_inclusive_in_brisbane(self):
        now = datetime(2026, 10, 23, 23, 59, 40, tzinfo=ZONE)
        self.assertEqual(link_lifetime(['2026-10-23'], now=now), 20)
        self.assertEqual(link_lifetime(['2026-10-23'], now=datetime(2026, 10, 24, tzinfo=ZONE)), 0)

    def test_direct_and_other_live_terms_survive_expiry(self):
        now = datetime(2026, 10, 24, tzinfo=ZONE)
        self.assertEqual(link_lifetime(['2026-10-23'], direct=True, now=now), 900)
        self.assertEqual(link_lifetime(['2026-10-23', '2026-11-01'], now=now), 900)
        self.assertEqual(link_lifetime(['invalid', None], now=now), 0)


if __name__ == '__main__':
    unittest.main()
