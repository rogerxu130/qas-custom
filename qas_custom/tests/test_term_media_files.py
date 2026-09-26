from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zipfile import ZipFile
import csv
import io

from qas_custom.services.term_media_files import cleanup_eligibility, segment, hash_file, write_parts


class TestTermMediaFiles(TestCase):
    def test_eligibility_requires_closed_status_and_past_date(self):
        for status in ['Active', 'Upcoming', '', None, 'deleted']:
            self.assertEqual(cleanup_eligibility(dict(status=status, end_date='2026-01-01'), date(2026, 9, 26)), (False, 'term_not_closed'))
        for status in ['Completed', 'Archived']:
            self.assertEqual(cleanup_eligibility(dict(status=status, end_date='2026-09-25'), '2026-09-26'), (True, ''))
            for end in ['2026-09-26', '2026-09-27']:
                self.assertEqual(cleanup_eligibility(dict(status=status, end_date=end), '2026-09-26'), (False, 'term_not_ended'))
            for end in ['', None, 'invalid']:
                self.assertEqual(cleanup_eligibility(dict(status=status, end_date=end), '2026-09-26'), (False, 'invalid_end_date'))

    def row(self, index):
        return dict(key=str(index), term='Term 3', course_label='Art', course='COURSE', timeslot_label='Sat 9',
                    timeslot='W1', session_date='2026-07-01', session='S1', post='P1', idx=index, kind='photo',
                    filename='photo.jpg', bytes=4, status='pending')

    def test_names_cannot_traverse_and_collisions_remain_distinct(self):
        for name in ['../../outside', '\\absolute', '\x00\n/evil', '.', ' ', 'a'*1000]:
            safe = segment(name)
            self.assertNotIn('/', safe)
            self.assertNotIn('\\', safe)
            self.assertNotIn('\x00', safe)
            self.assertLessEqual(len(safe), 83)
        self.assertNotEqual(segment('same', 'ID1'), segment('same', 'ID2'))
        self.assertNotEqual(segment('a/b'), segment('a\\b'))

    def test_split_archives_validate_content_and_full_manifest(self):
        with TemporaryDirectory() as temp:
            source = Path(temp) / 'source'; source.write_bytes(b'abcd')
            rows = [self.row(1), self.row(2)]
            parts = write_parts(Path(temp)/'out', rows, lambda _: source, part_bytes=4,
                                notes={'Term/notes.txt': 'Teacher notes'})
            self.assertEqual(len(parts), 2)
            self.assertEqual([r['status'] for r in rows], ['exported','exported'])
            for part in parts:
                with ZipFile(Path(temp)/'out'/part['filename']) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(archive.read('Term/notes.txt'), b'Teacher notes')
                    manifest = list(csv.DictReader(io.StringIO(archive.read('all-files.csv').decode('utf-8-sig'))))
                    self.assertEqual(len(manifest), 2)
                    self.assertTrue(all(row['status']=='exported' for row in manifest))
            self.assertEqual(rows[0]['sha256'], hash_file(source)[0])

    def test_missing_and_changed_sources_never_qualify_for_cleanup(self):
        with TemporaryDirectory() as temp:
            source = Path(temp)/'source'; source.write_bytes(b'abcd')
            rows = [dict(self.row(1), sha256='old'), self.row(2)]
            def resolve(row):
                return source if row['idx']==1 else Path(temp)/'missing'
            parts = write_parts(Path(temp)/'out', rows, resolve)
            self.assertTrue(parts)
            self.assertEqual([r['status'] for r in rows], ['failed','failed'])
            self.assertEqual(rows[0]['reason'], 'file_changed')

    def test_zero_media_still_exports_notes(self):
        with TemporaryDirectory() as temp:
            parts = write_parts(temp, [], lambda _: None, notes={'notes.txt':'Class update'})
            with ZipFile(Path(temp)/parts[0]['filename']) as archive:
                self.assertEqual(archive.read('notes.txt'), b'Class update')
