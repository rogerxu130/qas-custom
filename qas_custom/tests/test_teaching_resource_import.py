import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services.teaching_resource_import import MAX_XML_BYTES, parse_course_xml
from qas_custom.services import teaching_resources as service

SAMPLE = (Path(__file__).parent / 'fixtures/teaching_course.xml').read_text()
MANIFEST = [
    {'filename': '18_Surface_Construction_Case_Study_6_Part_1_EN.mp4', 'size': 1922146327},
    {'filename': '18_Surface_Construction_Case_Study_6_Part_2_EN.mp4', 'size': 1604714416},
    {'filename': '18_Surface_Construction_Case_Study_6_EN.pptx', 'size': 2972766},
]


class XmlValidation(TestCase):
    def test_real_sample_maps_fields_names_and_order(self):
        course = parse_course_xml(SAMPLE, MANIFEST)
        self.assertEqual(course['sequence'], 18)
        self.assertEqual(course['source_id'], 'industrial-design-sketching-day-18')
        self.assertEqual(course['title'], 'Industrial Design Sketching - Day 18 - Surface Construction Case Study 6')
        self.assertIn('鼠标与鞋', course['admin_note'])
        self.assertEqual(course['tags'], sorted(['digital art with ipad', '电脑板绘', '工业设计手绘：线条、透视与产品马克笔基础']))
        self.assertEqual([file['kind'] for file in course['files']], ['video', 'video', 'ppt'])
        self.assertEqual([file['order'] for file in course['files']], [1, 2, 3])
        self.assertEqual(course['files'][2]['label'], 'Surface Construction Case Study 6 - Slides')

    def test_ppt_only_and_video_only(self):
        from xml.etree import ElementTree as ET
        for kind in ('video', 'presentation'):
            root = ET.fromstring(SAMPLE)
            for file in list(root.find('files')):
                if file.get('type') != kind: root.find('files').remove(file)
            local = [item for item in MANIFEST if item['filename'].endswith('.pptx') == (kind == 'presentation')]
            self.assertTrue(parse_course_xml(ET.tostring(root, encoding='unicode'), local)['files'])

    def test_malformed_unsafe_and_unknown_schema(self):
        for xml in ['<course>', '<!DOCTYPE course [<!ENTITY x "secret">]>' + SAMPLE, SAMPLE.replace('schema_version="1"', 'schema_version="2"'), SAMPLE.replace('id="industrial-design-sketching-day-18"', 'id="../escape"'), SAMPLE + 'x', ' ' * (MAX_XML_BYTES + 1)]:
            with self.subTest(xml=xml[:80]), self.assertRaises(ValueError): parse_course_xml(xml, MANIFEST)

    def test_missing_extra_duplicate_and_wrong_size(self):
        cases = [MANIFEST[:1], MANIFEST + [{'filename': 'extra.mp4', 'size': 4}], MANIFEST + MANIFEST[:1], [{**MANIFEST[0], 'size': 1}, *MANIFEST[1:]]]
        for files in cases:
            with self.subTest(files=files), self.assertRaises(ValueError): parse_course_xml(SAMPLE, files)

    def test_duplicate_order_type_mismatch_paths_and_invalid_hash(self):
        for xml in [SAMPLE.replace('order="2"', 'order="1"'), SAMPLE.replace('type="presentation"', 'type="video"'), SAMPLE.replace('<bytes>2972766</bytes>', '<bytes>0</bytes>'), SAMPLE.replace('18_Surface_Construction_Case_Study_6_EN.pptx', '../slides.pptx'), SAMPLE.replace('55b228714de0df9c2339e851a2468df31f9fe8d7f8de7316c3c1e2bd4f066834', 'bad')]:
            with self.subTest(xml=xml[-120:]), self.assertRaises(ValueError): parse_course_xml(xml, MANIFEST)


class ImportService(TestCase):
    def setUp(self):
        self.course = parse_course_xml(SAMPLE, MANIFEST)
        self.doc = SimpleNamespace(name='R1', status='Draft', import_data=json.dumps({'course': self.course, 'complete': False}), save=Mock())
        self.db = Mock()
        self.db.get_value.return_value = 'R1'
        self.fake = SimpleNamespace(db=self.db, throw=lambda message, *args: (_ for _ in ()).throw(ValueError(message)))
        self.patches = [patch.object(service, 'frappe', self.fake), patch.object(service, 'require_user'), patch.object(service, 'lock_resource', return_value=self.doc), patch.object(service, 'detail', return_value={'name': 'R1', 'status': 'Draft'}), patch.object(service, 'audit')]
        for item in self.patches: item.start(); self.addCleanup(item.stop)

    def test_retry_preserves_reviewed_metadata_and_never_saves(self):
        with patch.object(service, 'save') as save:
            result = service.import_draft({'xml': SAMPLE, 'files': MANIFEST})
            self.assertTrue(result['import_resumed'])
            save.assert_not_called()
            self.doc.save.assert_not_called()

    def test_changed_xml_or_non_draft_is_not_overwritten(self):
        with self.assertRaisesRegex(ValueError, 'different XML'):
            service.import_draft({'xml': SAMPLE.replace('using views', 'with views'), 'files': MANIFEST})
        self.doc.status = 'Published'
        with self.assertRaisesRegex(ValueError, 'not a Draft'): service.import_draft({'xml': SAMPLE, 'files': MANIFEST})
        self.doc.save.assert_not_called()

    def test_new_draft_has_only_xml_metadata_no_grants(self):
        self.db.get_value.return_value = None
        with patch.object(service, 'save', return_value={'name': 'R1'}) as save:
            service.import_draft({'xml': SAMPLE, 'files': MANIFEST})
            self.assertEqual(set(save.call_args.args[0]), {'title', 'summary', 'admin_note', 'tags'})
            self.assertEqual(self.doc.import_id, self.course['source_id'])
            self.assertFalse(json.loads(self.doc.import_data)['complete'])
            self.assertEqual(self.doc.status, 'Draft')

    def test_failed_file_before_initialization_still_blocks_publishing(self):
        self.db.exists.return_value = True
        with self.assertRaisesRegex(ValueError, 'every XML attachment'): service.lifecycle('publish', 'R1', 'version')
        self.assertEqual(self.doc.status, 'Draft')
        self.db.sql.assert_not_called()

    def test_all_expected_files_mark_import_complete_once(self):
        ready = [SimpleNamespace(filename=item['filename'], size_bytes=item['size']) for item in MANIFEST]
        self.fake.get_all = Mock(return_value=ready[:1])
        service.mark_import_complete(self.doc)
        self.assertFalse(json.loads(self.doc.import_data)['complete'])
        self.fake.get_all.return_value = ready
        service.mark_import_complete(self.doc)
        self.assertTrue(json.loads(self.doc.import_data)['complete'])
        self.fake.get_all.reset_mock()
        service.mark_import_complete(self.doc)
        self.fake.get_all.assert_not_called()

    def test_import_parser_runs_only_after_admin_check(self):
        with patch.object(service, 'require_user', side_effect=PermissionError), patch('qas_custom.services.teaching_resource_import.parse_course_xml') as parse:
            with self.assertRaises(PermissionError): service.import_draft({'xml': SAMPLE, 'files': MANIFEST})
            parse.assert_not_called()

    def test_source_id_has_database_unique_constraint(self):
        schema = json.loads((Path(__file__).parents[1] / 'qas_custom/doctype/teaching_resource/teaching_resource.json').read_text())
        field = next(item for item in schema['fields'] if item['fieldname'] == 'import_id')
        self.assertEqual(field['unique'], 1)

    def test_upload_initialization_applies_source_label_and_order(self):
        def create(values):
            return SimpleNamespace(**values, name='F1', insert=Mock(), save=Mock())
        self.db.get_value.return_value = None
        self.db.count.return_value = 0
        self.fake.get_doc = Mock(side_effect=create)
        source = self.course['files'][1]
        file = SimpleNamespace(name='F1', status='Uploading', object_key='key', mime='video/mp4', save=Mock(), filename=source['filename'])
        with patch.object(service, 'file_lock', return_value=(self.doc, file)), patch.object(service.storage, 'start', return_value='U1'):
            result = service.init_upload({'resource': 'R1', 'filename': source['filename'], 'size': source['size'], 'request_id': 'af0db313-5214-45bc-9942-fbba2cbb32e4'})
        self.assertEqual(result['file'], 'F1')
        values = self.fake.get_doc.call_args.args[0]
        self.assertEqual(values['label'], source['label'])
        self.assertEqual(values['sort_order'], 2)
        self.assertNotIn('is_visible', values)

    def test_wrong_source_size_or_extra_upload_cannot_bypass_manifest(self):
        self.db.get_value.return_value = None
        self.db.count.return_value = 0
        self.fake.get_doc = Mock()
        for filename, size in [('other.mp4', 3), (MANIFEST[0]['filename'], 3)]:
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, 'original XML attachments'):
                service.init_upload({'resource': 'R1', 'filename': filename, 'size': size, 'request_id': 'af0db313-5214-45bc-9942-fbba2cbb32e4'})
        self.fake.get_doc.assert_not_called()

    def test_source_metadata_stays_out_of_teacher_details(self):
        source_doc = SimpleNamespace(get=lambda key: {'name': 'R1', 'title': 'Lesson', 'status': 'Published', 'admin_note': 'PRIVATE', 'import_data': self.doc.import_data}.get(key))
        fake = SimpleNamespace(get_doc=Mock(return_value=source_doc), get_all=Mock(return_value=[]))
        # Exercise the real detail method rather than the return-value mock used above.
        self.patches[3].stop()
        with patch.object(service, 'frappe', fake), patch.object(service, 'teacher_name', return_value='TEACHER'), patch.object(service, 'teacher_access', return_value=([], 900)):
            detail = service.detail('R1')
        self.assertNotIn('admin_note', detail)
        self.assertNotIn('import_source', detail)
        self.assertNotIn('import_data', detail)
        self.assertNotIn('import_id', detail)

    def test_another_browser_resumes_source_file_instead_of_duplicating(self):
        self.db.get_value.side_effect = lambda table, filters, field: 'EXISTING' if 'filename' in filters else None
        self.db.count.return_value = 100
        existing = SimpleNamespace(name='EXISTING', status='Uploading')
        self.fake.get_doc = Mock(return_value=existing)
        source = self.course['files'][0]
        with patch.object(service.storage, 'start') as start:
            result = service.init_upload({'resource': 'R1', 'filename': source['filename'], 'size': source['size'], 'request_id': 'af0db313-5214-45bc-9942-fbba2cbb32e4', 'import_source_id': self.course['source_id']})
        self.assertEqual(result['file'], 'EXISTING')
        start.assert_not_called()
        self.doc.save.assert_not_called()
