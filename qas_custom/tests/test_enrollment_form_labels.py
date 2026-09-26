from unittest import TestCase
from unittest.mock import patch, Mock
import json

import frappe
from qas_custom.services import direct_enrollment as subject
from qas_custom.tests.test_direct_enrollment import Doc


class TestFormLabels(TestCase):
    def setUp(self):
        self.campuses = [Doc(name='Upper Mount Gravatt', campus_name='Upper Mount Gravatt')]
        self.courses = [Doc(name='Realistic Art - Beginner', course_name='Realistic Art - Beginner'),
                        Doc(name='Intro to Realistic Art', course_name='Intro to Realistic Art')]
        self.payload = {'form_name': 'Upper Mount Gravatt Realistic Art - Beginner',
                        'available_sessions': 'Sat 13:00–14:30', 'start_date': '2026-10-03'}
        mock = patch.object(frappe, 'get_all', side_effect=lambda doctype, **kwargs: self.campuses if doctype == 'Campus' else self.courses)
        mock.start()
        self.addCleanup(mock.stop)

    def test_title_and_available_sessions_map_without_internal_ids(self):
        result = subject._website_schedule_payload(self.payload)
        self.assertEqual(result['campus'], 'Upper Mount Gravatt')
        self.assertEqual(result['course'], 'Realistic Art - Beginner')
        self.assertEqual(result['class_session'], 'Sat 13:00–14:30')
        self.assertNotIn('campus', self.payload)

    def test_case_spacing_and_dashes_are_presentation_only(self):
        self.payload['form_name'] = '  UPPER  Mount Gravatt — Realistic Art – Beginner '
        self.assertEqual(subject._website_schedule_payload(self.payload)['course'], 'Realistic Art - Beginner')

    def test_intro_course_remains_distinct(self):
        self.payload['form_name'] = 'Upper Mount Gravatt Intro to Realistic Art'
        self.assertEqual(subject._website_schedule_payload(self.payload)['course'], 'Intro to Realistic Art')

    def test_unknown_title_is_not_fuzzy_guessed(self):
        for title in ('Upper Mount Gravatt Realistic', 'Upper Mount Gravat Realistic Art Beginner', 'Other Campus Realistic Art Beginner'):
            with self.subTest(title=title), self.assertRaises(subject.ReviewRequired):
                subject._website_schedule_payload({**self.payload, 'form_name': title})

    def test_duplicate_display_names_are_ambiguous(self):
        self.courses.append(Doc(name='OTHER-ID', course_name='Realistic Art - Beginner'))
        with self.assertRaisesRegex(subject.ReviewRequired, 'uniquely'):
            subject._website_schedule_payload(self.payload)

    def test_conflicting_explicit_campus_is_review(self):
        with patch.object(subject.inquiries, '_resolve_campus', return_value='Other'), self.assertRaisesRegex(subject.ReviewRequired, 'conflicts'):
            subject._website_schedule_payload({**self.payload, 'campus': 'Other'})

    def test_aliases_and_equivalent_session_fields(self):
        payload = {**self.payload, 'submitted_form_name': self.payload['form_name'], 'class_session': 'Saturday 13:00-14:30'}
        self.assertEqual(subject._website_schedule_payload(payload)['course'], 'Realistic Art - Beginner')
        payload.pop('form_name')
        self.assertEqual(subject._website_schedule_payload(payload)['campus'], 'Upper Mount Gravatt')

    def test_conflicting_aliases_are_review(self):
        for extra in ({'submitted_form_name': 'Other'}, {'class_session': 'Fri 13:00-14:30'}, {'class_session': 'Sat 13:00-15:30'}):
            with self.subTest(extra=extra), self.assertRaises(subject.ReviewRequired):
                subject._website_schedule_payload({**self.payload, **extra})

    def test_multiple_or_malformed_options_are_review(self):
        for value in (['Sat 13:00-14:30'], 'Sat 13:00-14:30, Sun 09:00-10:30', 'Sat 13:00-xx', 'Sat 13:00-14:30pm'):
            with self.subTest(value=value), self.assertRaises(subject.ReviewRequired):
                subject._website_schedule_payload({**self.payload, 'available_sessions': value})

    def test_mapped_values_reach_existing_exact_session_matching(self):
        with patch.object(subject.inquiries, '_map_trial_form_session', return_value={'course_session': 'SESSION'}) as mapper:
            self.assertEqual(subject._match(self.payload), 'SESSION')
            self.assertEqual(mapper.call_args.args[0]['campus'], 'Upper Mount Gravatt')
            self.assertEqual(mapper.call_args.args[0]['submitted_class_session'], 'Sat 13:00–14:30')
            self.assertEqual(str(mapper.call_args.args[0]['submitted_trial_date']), '2026-10-03')

    def test_old_explicit_payload_still_works(self):
        payload = {'campus': 'C', 'course': 'K', 'class_session': 'Saturday 09:00-10:00', 'start_date': '2026-10-03'}
        self.assertEqual(subject._website_schedule_payload(payload), payload)

    def test_actual_schedule_query_uses_weekday_and_both_times(self):
        slot = Doc(name='W', course='Realistic Art - Beginner', campus='Upper Mount Gravatt',
                   class_language='English', start_time='13:00:00', end_time='14:30:00', status='Active')
        sessions = [Doc(name='SESSION', weekly_timeslot='W', session_date='2026-10-03', status='Scheduled')]
        calls = {}

        def records(doctype, **kwargs):
            if doctype == 'Campus':
                return self.campuses
            if doctype == 'Course':
                return self.courses
            calls[doctype] = kwargs['filters']
            return [slot] if doctype == 'Weekly Timeslot' else sessions

        with patch.object(frappe, 'get_all', side_effect=records), patch.object(frappe, 'db', Mock()), \
                patch.object(frappe.db, 'exists', return_value=True), \
                patch.object(frappe, 'get_meta', return_value=Mock(has_field=Mock(return_value=True))), \
                patch.object(subject.inquiries, '_course_session_has_started', return_value=False):
            self.assertEqual(subject._match(self.payload), 'SESSION')
            self.assertEqual(calls['Weekly Timeslot']['day_of_week'], 'Saturday')
            self.assertEqual(calls['Weekly Timeslot']['start_time'], '13:00:00')
            self.assertEqual(calls['Weekly Timeslot']['end_time'], '14:30:00')
            self.assertEqual(str(calls['Course Sessions']['session_date']), '2026-10-03')
            with self.assertRaisesRegex(subject.ReviewRequired, 'weekday'):
                subject._match({**self.payload, 'start_date': '2026-10-02'})
            sessions.append(Doc(name='OTHER', weekly_timeslot='W', status='Scheduled'))
            with self.assertRaisesRegex(subject.ReviewRequired, 'Multiple'):
                subject._match(self.payload)

    def test_matching_failure_saves_raw_labels_and_notifies_without_enrolling(self):
        payload = {**self.payload, 'external_submission_id': 'site:1', 'email': 'p@example.com',
                   'parent_name': 'Parent', 'student_name': 'Child', 'date_of_birth': '2020-01-01',
                   'form_name': 'Unrecognized course', 'form_answers': {'token': 'secret', 'Other': 'answer'}}
        doc = Doc(name='INQ', flags=Doc(), insert=Mock(), save=Mock())
        with patch.object(frappe, 'db', Mock()), patch.object(frappe.db, 'get_value', return_value=None), \
                patch.object(subject.inquiries, '_get_payload', return_value=payload), \
                patch.object(subject.inquiries, '_validate_webhook_token'), patch.object(subject, 'validate_email_address'), \
                patch.object(frappe, 'new_doc', return_value=doc), \
                patch.object(subject.inquiries, '_resolve_campus', return_value=None), \
                patch.object(subject.inquiries, '_resolve_course', return_value=None), \
                patch.object(subject.inquiries, '_resolve_parent', return_value='P'), \
                patch.object(subject, '_student', return_value='S'), \
                patch.object(subject, '_complete') as complete, \
                patch.object(subject, 'queue_inquiry_admin_notification') as notify:
            result = subject.create_webhook(payload)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(doc.submitted_form_name, 'Unrecognized course')
        self.assertEqual(doc.submitted_class_session, 'Sat 13:00–14:30')
        raw = json.loads(doc.raw_webhook_payload)
        self.assertEqual(raw['form_name'], payload['form_name'])
        self.assertEqual(raw['available_sessions'], payload['available_sessions'])
        self.assertNotIn('token', raw['form_answers'])
        notify.assert_called_once_with(doc)
        complete.assert_not_called()
