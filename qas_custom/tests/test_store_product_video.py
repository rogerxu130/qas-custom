from io import BytesIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import frappe
from qas_custom.services import material_orders as service

MP4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42'

class TestStoreProductVideo(TestCase):
    def setUp(self):
        translation = patch.object(service, '_', side_effect=lambda text: text)
        translation.start()
        self.addCleanup(translation.stop)
        throwing = patch.object(service.frappe, 'throw', side_effect=ValueError('invalid'))
        throwing.start()
        self.addCleanup(throwing.stop)

    def test_mp4_header_and_size_validation(self):
        service._validate_product_video('clip.MP4', MP4)
        for name, content in [('clip.mp4', b''), ('clip.mp4', b'<html>not video</html>'), ('clip.mov', MP4), ('clip.mp4', MP4[:12])]:
            with self.subTest(name=name, content=content), self.assertRaises(ValueError):
                service._validate_product_video(name, content)
        with patch.object(service, 'MAX_VIDEO_BYTES', 10), self.assertRaises(ValueError):
            service._validate_product_video('clip.mp4', MP4)

    def test_upload_requires_admin_before_reading_file(self):
        with patch.object(service, '_require_school_admin', side_effect=PermissionError), patch.object(service, '_get_product') as get_product:
            with self.assertRaises(PermissionError):
                service.upload_school_admin_store_product_video_data('P1')
            get_product.assert_not_called()

    def test_upload_attaches_public_mp4_to_product_and_appends_video(self):
        doc = Mock(name='product')
        doc.name = 'P1'
        doc.product_name = 'Bag'
        doc.get.return_value = [frappe._dict(display_order=3)]
        upload = BytesIO(MP4)
        upload.filename = 'clip.mp4'
        with patch.object(service, '_require_school_admin'), patch.object(service, '_get_product', return_value=doc), patch.object(service.frappe, 'request', SimpleNamespace(files={'video': upload})), patch.object(service.frappe, 'db', SimpleNamespace(commit=Mock())), patch.object(service, 'save_file', return_value=SimpleNamespace(file_url='/files/clip.mp4')) as save, patch.object(service, '_product_payload', return_value={'name': 'P1'}):
            self.assertEqual(service.upload_school_admin_store_product_video_data('P1'), {'name': 'P1'})
            save.assert_called_once_with('product-video.mp4', MP4, 'Store Product', 'P1', is_private=0, df='videos')
            doc.append.assert_called_once_with('videos', {'label': 'Bag video', 'url': '/files/clip.mp4', 'display_order': 4})

    def test_resaving_uploaded_video_requires_file_attached_to_same_product(self):
        doc = Mock()
        doc.name = 'P1'
        payload = {'videos': [{'label': 'Bag', 'url': '/files/clip.mp4'}]}
        exists = Mock(return_value=True)
        with patch.object(service.frappe, 'db', SimpleNamespace(exists=exists)):
            service._apply_media(doc, payload)
            self.assertEqual(exists.call_args.args[1]['attached_to_name'], 'P1')
            exists.return_value = False
            with self.assertRaises(ValueError):
                service._apply_media(doc, payload)

    def test_legacy_external_links_and_unsafe_urls(self):
        doc = Mock()
        service._apply_media(doc, {'videos': [{'label': 'YouTube', 'url': 'https://youtube.com/shorts/example'}]})
        doc.append.assert_called_once()
        for url in ['javascript:alert(1)', '/private/files/clip.mp4', '/files/../clip.mp4']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                service._apply_media(doc, {'videos': [{'label': 'Clip', 'url': url}]})

    def test_delete_file_success_and_safety_checks(self):
        for case in ['success', 'stale', 'shared', 'other_product', 'external', 'unauthorized']:
            with self.subTest(case=case):
                doc = Mock()
                doc.name = 'P1'
                doc.modified = 'version1'
                doc.get.return_value = [frappe._dict(url='/files/clip.mp4')]
                file = frappe._dict(name='F1', attached_to_doctype='Store Product', attached_to_name='P1', is_private=0)
                db = SimpleNamespace(get_value=Mock(), exists=Mock(return_value=case=='other_product'), commit=Mock())
                with patch.object(service, '_require_school_admin', side_effect=PermissionError if case=='unauthorized' else None), patch.object(service, '_get_product', return_value=doc), patch.object(service.frappe, 'db', db), patch.object(service.frappe, 'get_all', return_value=[file, file] if case=='shared' else [file]), patch.object(service.frappe, 'delete_doc') as delete, patch.object(service, '_product_payload', return_value={'name':'P1'}):
                    args = dict(product='P1', url='https://example.com/video.mp4' if case=='external' else '/files/clip.mp4', modified='old' if case=='stale' else 'version1')
                    if case=='success':
                        self.assertEqual(service.delete_school_admin_store_product_video_data(**args), {'name':'P1'})
                        doc.set.assert_called_once_with('videos', [])
                        delete.assert_called_once_with('File','F1',ignore_permissions=True)
                        db.commit.assert_called_once()
                    else:
                        with self.assertRaises((ValueError, PermissionError)):
                            service.delete_school_admin_store_product_video_data(**args)
                        delete.assert_not_called()
                        doc.save.assert_not_called()
