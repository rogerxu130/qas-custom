"""Reader regression cases: preserve live media while exposing verified removals."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services import term_media


def removal(post, idx=0, kind='photo'):
    return SimpleNamespace(post=post, kind=kind, photo_idx=idx)


class TestTermMediaDisplay(TestCase):
    def setUp(self):
        self.frappe_patch = patch.object(term_media, 'frappe', Mock())
        self.frappe_patch.start()
        self.addCleanup(self.frappe_patch.stop)

    def annotate(self, items, rows):
        with patch.object(term_media.frappe.db, 'exists', return_value=True), patch.object(
            term_media.frappe, 'get_all', return_value=rows
        ) as query:
            result = term_media.annotate_media(items)
            self.assertIs(result, items)
            self.assertEqual(query.call_count, 1)
            return result

    def test_parent_live_count_and_url_strings_survive_partial_cleanup(self):
        url = '/api/photo?photo_post=PARENT&photo_idx=2'
        item = dict(id='PARENT', type='photo_post', photos=[url], photo_count=1, cover_image=url, students=[])
        self.annotate([item], [removal('PARENT', 1)])
        self.assertEqual(item['photos'], [url])
        self.assertEqual(item['photo_count'], 1)
        self.assertEqual(item['cover_image'], url)
        self.assertEqual(item['media_archived_count'], 1)
        self.assertFalse(item['media_archived'])

    def test_teacher_partial_preview_count_keeps_unpreviewed_live_photos(self):
        item = dict(id='TEACHER', type='photo_post', photos=[dict(idx=2, preview_url='live')], photo_count=4)
        self.annotate([item], [removal('TEACHER', 1)])
        self.assertEqual(item['photo_count'], 3)
        self.assertEqual(item['remaining_photo_count'], 2)
        self.assertEqual(item['photos'], [dict(idx=2, preview_url='live')])
        self.assertFalse(item['media_archived'])

    def test_admin_removes_only_cleaned_indices(self):
        item = dict(id='ADMIN', type='photo_post', photos=[dict(idx=1, url='gone'), dict(idx=2, url='live')], photo_count=2)
        self.annotate([item], [removal('ADMIN', 1)])
        self.assertEqual(item['photos'], [dict(idx=2, url='live')])
        self.assertEqual(item['photo_count'], 1)
        self.assertEqual(item['remaining_photo_count'], 0)

    def test_all_parent_photos_cleaned_preserves_post_text(self):
        item = dict(id='EMPTY', type='photo_post', photos=[], photo_count=0, cover_image=None, summary='Class caption', students=[])
        self.annotate([item], [removal('EMPTY', 1), removal('EMPTY', 2)])
        self.assertTrue(item['media_archived'])
        self.assertEqual(item['media_archived_count'], 2)
        self.assertEqual(item['photo_count'], 0)
        self.assertEqual(item['summary'], 'Class caption')

    def test_mixed_video_shapes_remove_every_play_and_download_link(self):
        items = [dict(id='V1', type='video_post', video=dict(url='play', download_url='download')),
                 dict(id='V2', type='video_post', preview_url='play', download_url='download'),
                 dict(id='V3', type='video_post', video_url='play')]
        self.annotate(items, [removal(item['id'], kind='video') for item in items])
        for item in items:
            self.assertTrue(item['media_archived'])
            self.assertEqual(item['media_archived_count'], 1)
            self.assertFalse(item.get('video'))
            for key in ('video_url', 'preview_url', 'download_url'):
                self.assertFalse(item.get(key))

    def test_tombstones_do_not_cross_post_types_or_hide_class_updates(self):
        items = [dict(id='SAME', type='video_post', video_url='live'),
                 dict(id='SAME', type='class_update', summary='Keep this update')]
        self.annotate(items, [removal('SAME', 1, kind='photo')])
        self.assertEqual(items[0]['video_url'], 'live')
        self.assertFalse(items[0].get('media_archived'))
        self.assertNotIn('media_archived_count', items[1])

    def test_guard_does_not_block_unremoved_media(self):
        for state in (None, 'Removing'):
            with patch.object(term_media.frappe.db, 'exists', return_value=True), patch.object(
                term_media.frappe.db, 'get_value', return_value=state
            ), patch.object(term_media.frappe, 'throw') as throw:
                term_media.assert_media_available('POST', 'photo', 2)
                throw.assert_not_called()

    def test_guard_marks_removed_media_gone(self):
        # Frappe's exception handler uses the exception's status, overriding response fields.
        import frappe
        term_media.frappe.DoesNotExistError = frappe.DoesNotExistError
        def throw(message, exception):
            raise exception(message)
        with patch.object(term_media.frappe.db, 'exists', return_value=True), patch.object(
            term_media.frappe.db, 'get_value', return_value='Removed'
        ), patch.object(term_media.frappe, 'throw', side_effect=throw):
            with self.assertRaises(Exception) as raised:
                term_media.assert_media_available('POST', 'video')
        self.assertEqual(getattr(raised.exception, 'http_status_code', None), 410)


class TestTermMediaEndpointGuards(TestCase):
    def endpoints(self):
        from qas_custom.services import teacher_portal, parent_feed, school_admin, campus_admin
        return [
            (teacher_portal, 'get_teacher_photo_content_data', 'photo', ('POST', 1)),
            (teacher_portal, 'get_teacher_video_content_data', 'video', ('POST',)),
            (parent_feed, 'get_parent_feed_photo_content', 'photo', ('POST', 1)),
            (parent_feed, 'get_parent_feed_video_content', 'video', ('POST',)),
            (school_admin, 'get_school_admin_session_photo_content_data', 'photo', ('SESSION', 'POST', 1)),
            (school_admin, 'get_school_admin_session_video_content_data', 'video', ('SESSION', 'POST')),
            (campus_admin, 'get_campus_admin_session_photo_content_data', 'photo', ('SESSION', 'POST', 1)),
            (campus_admin, 'get_campus_admin_session_video_content_data', 'video', ('SESSION', 'POST')),
        ]

    def exercise_endpoint(self, module, name, kind, args, authorized):
        from contextlib import ExitStack
        class AccessDenied(Exception):
            pass
        class Gone(Exception):
            pass
        post = SimpleNamespace(name='POST', photos=[], video=None)
        post.get = lambda field: {'course_session': 'SESSION', 'status': 'Published'}.get(field)
        fake = Mock()
        fake.PermissionError = AccessDenied
        fake.get_doc.return_value = post
        with ExitStack() as stack:
            stack.enter_context(patch.object(module, 'frappe', fake))
            guard = stack.enter_context(patch.object(module, 'assert_media_available', side_effect=Gone))
            auth_helpers = {
                '_require_teacher': SimpleNamespace(name='TEACHER'),
                '_get_owned_session': None,
                '_require_parent': 'PARENT',
                '_validate_parent_session_access': None,
                '_require_school_admin': None,
                '_require_campus_admin_profile': {'campuses': ['CAMPUS']},
                '_get_campus_admin_course_session_access': None,
            }
            for helper, result in auth_helpers.items():
                if hasattr(module, helper):
                    stack.enter_context(patch.object(module, helper, return_value=result,
                        side_effect=None if authorized else AccessDenied))
            with self.assertRaises(Gone if authorized else AccessDenied):
                getattr(module, name)(*args)
            if authorized:
                guard.assert_called_once_with('POST', kind, *([1] if kind == 'photo' else []))
            else:
                guard.assert_not_called()
            fake.db.get_value.assert_not_called()

    def test_all_roles_stop_removed_media_before_missing_file_lookup(self):
        for module, name, kind, args in self.endpoints():
            with self.subTest(endpoint=name):
                self.exercise_endpoint(module, name, kind, args, True)

    def test_all_roles_check_authorization_before_exposing_tombstone(self):
        for module, name, kind, args in self.endpoints():
            with self.subTest(endpoint=name):
                self.exercise_endpoint(module, name, kind, args, False)
