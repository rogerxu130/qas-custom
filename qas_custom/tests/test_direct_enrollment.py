from datetime import date
import json
from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import direct_enrollment as subject


class Doc(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__


class DatabaseTestCase(TestCase):
    def setUp(self):
        db = patch.object(frappe, 'db', Mock())
        db.start()
        self.addCleanup(db.stop)


class TestDirectEnrollment(DatabaseTestCase):
    def test_australian_dates_are_day_first_and_keep_iso_compatibility(self):
        for value, expected in (
            ('05/10/2026', date(2026, 10, 5)),
            ('10/05/2026', date(2026, 5, 10)),
            ('29/02/2024', date(2024, 2, 29)),
            ('2026-10-05', date(2026, 10, 5)),
        ):
            with self.subTest(value=value):
                self.assertEqual(subject._date(value), expected)

    def test_invalid_australian_dates_are_not_guessed_or_rolled_forward(self):
        for value in ('31/04/2026', '29/02/2026', '10/31/2026', '00/10/2026',
                      '05/00/2026', '05/10/26', '2026/10/05', '05-10-2026',
                      '05/10/2026 09:00', '20261005', None, ''):
            with self.subTest(value=value), self.assertRaises(subject.ReviewRequired):
                subject._date(value)

    def test_australian_start_date_selects_the_exact_calendar_day(self):
        with patch.object(subject.frappe.db, 'exists', return_value=True), \
             patch.object(subject.frappe, 'get_doc', return_value=Doc(day_of_week='Monday')), \
             patch.object(subject.frappe, 'get_all', return_value=['S1']) as sessions:
            self.assertEqual(subject._match({'weekly_timeslot': 'SLOT', 'start_date': '05/10/2026'}), 'S1')
        self.assertEqual(sessions.call_args.kwargs['filters']['session_date'], date(2026, 10, 5))

    def test_australian_dob_matches_existing_student_without_creating_another(self):
        with patch.object(subject, 'getdate', return_value=date(2026, 9, 27)), \
             patch.object(subject.frappe, 'get_all', return_value=[Doc(name='S1', student_name='Alice')]) as students, \
             patch.object(subject.inquiries, '_resolve_student') as create:
            self.assertEqual(subject._student('P', 'Alice', '05/10/2018'), 'S1')
        self.assertEqual(students.call_args.kwargs['filters']['date_of_birth'], date(2018, 10, 5))
        create.assert_not_called()

    def test_invalid_australian_dob_does_not_create_student(self):
        with patch.object(subject.inquiries, '_resolve_student') as create:
            with self.assertRaisesRegex(subject.ReviewRequired, 'Student date of birth'):
                subject._student('P', 'Alice', '31/02/2018')
        create.assert_not_called()

    def test_bad_start_dates_do_not_roll_forward(self):
        for value in (None, '', '2026-02-30', 'next saturday', '2026-09-19T09:00:00'):
            with self.subTest(value=value), self.assertRaises(subject.ReviewRequired):
                subject._date(value)
        self.assertEqual(subject._date('2026-09-19'), date(2026, 9, 19))

    def test_malformed_class_time_requires_review(self):
        with patch.object(subject, '_match_schedule', side_effect=ValueError('hour must be in 0..23')):
            with self.assertRaises(subject.ReviewRequired):
                subject._match({'class_session': 'Saturday 99:00'})

    def test_raw_answers_remove_nested_credentials(self):
        self.assertEqual(subject._safe_answers({'token': 'secret', 'name': 'A', 'form_answers': [{'password': 'hidden', 'start_date': 'bad'}]}),
                         {'name': 'A', 'form_answers': [{'start_date': 'bad'}]})

    def test_duplicate_response_uses_current_completed_state_without_family_data(self):
        doc = Doc(name='INQ', status='Converted', converted_enrollment='ENR', converted_invoice='INV', contact_email='private')
        result = subject._response(doc, duplicate=True)
        self.assertEqual(result['status'], 'enrolled')
        self.assertTrue(result['duplicate'])
        self.assertFalse(result['review_required'])
        self.assertNotIn('contact_email', result)

    def test_weekday_mismatch_is_review(self):
        slot = Doc(name='SLOT', day_of_week='Saturday')
        with patch.object(subject.frappe.db, 'exists', return_value=True), patch.object(subject.frappe, 'get_doc', return_value=slot):
            with self.assertRaisesRegex(subject.ReviewRequired, 'weekday'):
                subject._match({'weekly_timeslot': 'SLOT', 'start_date': '2026-09-16'})

    def test_zero_or_multiple_exact_sessions_require_review(self):
        for rows in ([], ['S1', 'S2']):
            with self.subTest(rows=rows), patch.object(subject.frappe.db, 'exists', return_value=True), patch.object(subject.frappe, 'get_doc', return_value=Doc(day_of_week='Saturday')), patch.object(subject.frappe, 'get_all', return_value=rows):
                with self.assertRaisesRegex(subject.ReviewRequired, 'exactly one'):
                    subject._match({'weekly_timeslot': 'SLOT', 'start_date': '2026-09-19'})

    def test_exact_session_is_retained(self):
        with patch.object(subject.frappe.db, 'exists', return_value=True), patch.object(subject.frappe, 'get_doc', return_value=Doc(day_of_week='Saturday')), patch.object(subject.frappe, 'get_all', return_value=['S1']):
            self.assertEqual(subject._match({'weekly_timeslot': 'SLOT', 'start_date': '2026-09-19'}), 'S1')

    def test_student_matching_distinguishes_twins(self):
        rows = [Doc(name='TWIN1', student_name='Alice'), Doc(name='TWIN2', student_name='Bob')]
        with patch.object(subject, 'getdate', return_value=date(2026, 9, 15)), patch.object(subject.frappe, 'get_all', return_value=rows):
            self.assertEqual(subject._student('P', ' bob ', '2020-01-01'), 'TWIN2')

    def test_ambiguous_student_identity_never_selects_first(self):
        rows = [Doc(name='S1', student_name='Alice'), Doc(name='S2', student_name='Alice')]
        with patch.object(subject, 'getdate', return_value=date(2026, 9, 15)), patch.object(subject.frappe, 'get_all', return_value=rows):
            with self.assertRaisesRegex(subject.ReviewRequired, 'More than one'):
                subject._student('P', 'Alice', '2020-01-01')

    def test_webhook_auth_precedes_database_work(self):
        with patch.object(subject.inquiries, '_get_payload', return_value={}), patch.object(subject.inquiries, '_validate_webhook_token', side_effect=PermissionError), patch.object(subject.frappe.db, 'get_value') as db:
            with self.assertRaises(PermissionError):
                subject.create_webhook({})
            db.assert_not_called()

    def test_duplicate_submission_does_not_create_any_record(self):
        payload = {'external_submission_id': 'site:form:1', 'email': 'parent@example.com', 'parent_name': 'Parent'}
        doc = Doc(name='INQ', inquiry_type=subject.DIRECT, contact_email='parent@example.com', status='Needs Review')
        with patch.object(subject.inquiries, '_get_payload', return_value=payload), patch.object(subject.inquiries, '_validate_webhook_token'), patch.object(subject, 'validate_email_address'), patch.object(subject.frappe.db, 'get_value', return_value='INQ'), patch.object(subject.frappe, 'get_doc', return_value=doc), patch.object(subject.frappe, 'new_doc') as create:
            result = subject.create_webhook(payload)
            create.assert_not_called()
            self.assertTrue(result['duplicate'])

    def test_review_does_not_create_enrollment_or_invoice(self):
        payload = {'external_submission_id': 'site:form:1', 'email': 'parent@example.com', 'parent_name': 'Parent', 'date_of_birth': '2020-01-01'}
        doc = Doc(name='INQ', flags=Doc(), insert=Mock(), save=Mock())
        with patch.object(subject.inquiries, '_get_payload', return_value=payload), patch.object(subject.inquiries, '_validate_webhook_token'), patch.object(subject, 'validate_email_address'), patch.object(subject.frappe.db, 'get_value', return_value=None), patch.object(subject.frappe.db, 'savepoint'), patch.object(subject.frappe.db, 'sql'), patch.object(subject.frappe, 'new_doc', return_value=doc), patch.object(subject.inquiries, '_resolve_campus', return_value=None), patch.object(subject.inquiries, '_resolve_course', return_value=None), patch.object(subject.inquiries, '_resolve_parent', return_value='P'), patch.object(subject, '_student', return_value='S'), patch.object(subject, '_match', side_effect=subject.ReviewRequired('Wrong date')), patch.object(subject, '_complete') as complete, patch.object(subject, 'queue_inquiry_admin_notification') as notify:
            result = subject.create_webhook(payload)
            self.assertEqual(result['status'], 'needs_review')
            self.assertEqual(doc.review_reason, 'Wrong date')
            notify.assert_called_once_with(doc)
            doc.save.assert_called_once()
            complete.assert_not_called()

    def test_infrastructure_error_is_not_reported_as_review_success(self):
        with patch.object(subject.inquiries, '_get_payload', return_value={}), patch.object(subject.inquiries, '_validate_webhook_token', side_effect=RuntimeError('database unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
                subject.create_webhook({})

    def test_repeated_manual_completion_is_idempotent(self):
        doc = Doc(name='INQ', converted_enrollment='ENR')
        with patch.object(subject, '_admin_doc', return_value=doc), patch.object(subject.inquiries, 'build_inquiry_detail', return_value={}), patch.object(subject, '_complete') as complete:
            self.assertTrue(subject.complete('INQ')['duplicate'])
            complete.assert_not_called()

    def test_manual_student_cannot_cross_family(self):
        with patch.object(subject.frappe.db, 'get_value', return_value='OTHER'), patch.object(subject.frappe, 'throw', side_effect=ValueError):
            with self.assertRaises(ValueError):
                subject._manual_student(Doc(parent='P'), {'student': 'S'})


class TestCapacity(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.first = Doc(name='S1', weekly_timeslot='W', session_date=date(2026, 9, 19), status='Scheduled', reload=Mock())
        self.second = Doc(name='S2', weekly_timeslot='W', session_date=date(2026, 9, 26), status='Scheduled')
        self.slot = Doc(name='W', term='T', status='Active', start_time='09:00', end_time='10:00', day_of_week='Saturday')
        self.term = Doc(status='Active', start_date='2026-09-01', end_date='2026-12-01')
        self.doc = Doc(parent='P', student='NEW')
        objects = {'S1': self.first, 'S2': self.second, 'W': self.slot, 'T': self.term}
        patches = [patch.object(subject.frappe.db, 'exists', side_effect=lambda doctype, *args, **kwargs: doctype == 'Course Sessions'),
                   patch.object(subject.frappe.db, 'sql'), patch.object(subject.frappe, 'get_doc', side_effect=lambda doctype, name, **kw: objects[name]),
                   patch.object(subject.frappe, 'get_all', return_value=['ENROLLED']),
                   patch('qas_custom.modules.course_schedule.queries.get_remaining_sessions', return_value=[self.first, self.second]),
                   patch('qas_custom.modules.course_schedule.session_resources.classroom_capacity', return_value=3),
                   patch('qas_custom.modules.course_schedule.session_resources.session_is_future', return_value=True),
                   patch('qas_custom.modules.course_schedule.session_resources.student_has_conflict', return_value=False)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_later_full_session_blocks_whole_enrollment(self):
        with patch('qas_custom.modules.course_schedule.session_resources.active_rows', side_effect=lambda name, **kw: [] if name == 'S1' else [Doc(student='A'), Doc(student='B')]):
            with self.assertRaisesRegex(subject.ReviewRequired, '2026-09-26'):
                subject._context(self.doc, 'S1')

    def test_duplicate_attendance_counts_child_once(self):
        with patch('qas_custom.modules.course_schedule.session_resources.active_rows', return_value=[Doc(student='ENROLLED'), Doc(student='ENROLLED')]):
            self.assertEqual(len(subject._context(self.doc, 'S1')[2]), 2)

    def test_closed_term_blocks(self):
        self.term.status = 'Archived'
        with self.assertRaisesRegex(subject.ReviewRequired, 'not open'):
            subject._context(self.doc, 'S1')

    def test_planned_duplicate_blocks(self):
        with patch.object(subject.frappe.db, 'exists', return_value=True):
            with self.assertRaisesRegex(subject.ReviewRequired, 'Planned or Active'):
                subject._context(self.doc, 'S1')

    def test_missing_capacity_requires_review(self):
        with patch('qas_custom.modules.course_schedule.session_resources.classroom_capacity', return_value=0):
            with self.assertRaisesRegex(subject.ReviewRequired, 'capacity must be configured'):
                subject._context(self.doc, 'S1')

    def test_started_session_requires_review(self):
        with patch('qas_custom.modules.course_schedule.session_resources.session_is_future', return_value=False):
            with self.assertRaisesRegex(subject.ReviewRequired, 'must not have started'):
                subject._context(self.doc, 'S1')

    def test_existing_attendance_conflict_requires_review(self):
        with patch('qas_custom.modules.course_schedule.session_resources.active_rows', return_value=[]), patch('qas_custom.modules.course_schedule.session_resources.student_has_conflict', return_value=True):
            with self.assertRaisesRegex(subject.ReviewRequired, 'already has a class'):
                subject._context(self.doc, 'S1')


class TestCompletion(DatabaseTestCase):
    def test_success_creates_course_records_without_trial_rewards_or_commit(self):
        from contextlib import ExitStack
        doc = Doc(name='INQ', parent='P', student='S', status='Needs Review')
        session = Doc(name='SESSION', session_date='2026-09-19')
        slot = Doc(name='W', campus='C', course='Drawing', term='T', start_time='09:00')
        enrollment, invoice = Doc(name='ENR'), Doc(name='INV')
        with ExitStack() as stack:
            create_enrollment = stack.enter_context(patch('qas_custom.modules.enrollment.commands.create_full_term_enrollment', return_value=enrollment))
            create_invoice = stack.enter_context(patch('qas_custom.modules.billing.commands.create_prorata_invoice', return_value=invoice))
            attendance = stack.enter_context(patch('qas_custom.modules.attendance.commands.create_full_term_attendance_entries'))
            for name in ('qas_custom.modules.enrollment.commands.link_invoice_to_enrollment', 'qas_custom.modules.inquiry.notes.add_conversion_note', 'qas_custom.modules.inquiry.notes.add_conversion_internal_note', 'qas_custom.modules.workflows.trial_conversion.apply_conversion_invoice_note'):
                stack.enter_context(patch(name))
            stack.enter_context(patch('qas_custom.modules.inquiry.commands.mark_converted', side_effect=lambda doc, en, inv: doc.update(status='Converted', converted_enrollment=en.name, converted_invoice=inv.name)))
            stack.enter_context(patch.object(frappe, 'session', Doc(user='Guest')))
            reward = stack.enter_context(patch('qas_custom.modules.trial_referrals.award_referral_conversion_reward'))
            terms = stack.enter_context(patch('qas_custom.modules.notifications.enrollment_terms.queue_enrollment_terms_notice'))
            result = subject._complete(doc, (session, slot, [session]))
            self.assertEqual(result['status'], 'enrolled')
            create_enrollment.assert_called_once()
            create_invoice.assert_called_once_with(doc, enrollment, 'Drawing', 'T', 'SESSION', 1)
            attendance.assert_called_once_with([session], 'S', 'ENR')
            terms.assert_called_once_with(enrollment, invoice)
            reward.assert_not_called()
            frappe.db.commit.assert_not_called()

    def test_changed_preview_prevents_financial_writes(self):
        doc = Doc(name='INQ', parent='P', student='S', status='Needs Review')
        context = (Doc(name='S'), Doc(course='Drawing'), [Doc(name='S')])
        with patch.object(subject, '_admin_doc', return_value=doc), patch.object(subject.inquiries, '_get_payload', return_value={'expected_amount': 99, 'expected_sessions': 1}), patch.object(subject, '_manual_student'), patch.object(subject, '_context', return_value=context), patch('qas_custom.modules.billing.commands.get_prorata_invoice_context', return_value={'invoice_amount': 100}), patch('frappe.utils.flt', side_effect=lambda value, *args: float(value)), patch.object(frappe, 'throw', side_effect=ValueError), patch.object(subject, '_complete') as complete:
            with self.assertRaises(ValueError):
                subject.complete('INQ', {})
            complete.assert_not_called()


class TestApplicationReview(DatabaseTestCase):
    def test_australian_website_dates_preserve_raw_input_and_enroll_when_valid(self):
        for start_date, expected_status in (('05/10/2026', 'Converted'), ('31/02/2026', 'Needs Review')):
            with self.subTest(start_date=start_date), ExitStack() as stack:
                payload = {'external_submission_id': 'web:12:1', 'email': 'parent@example.com',
                           'parent_name': 'Parent', 'student_name': 'Child', 'date_of_birth': '05/10/2018',
                           'form_name': 'Upper Mount Gravatt Realistic Art - Beginner',
                           'available_sessions': 'Mon 16:00–17:30', 'start_date': start_date}
                doc = Doc(name='INQ', flags=Doc(), insert=Mock(), save=Mock())
                first = Doc(name='CS', session_date='2026-10-05')
                slot = Doc(campus='C', course='Art', start_time='16:00')
                def records(doctype, **kwargs):
                    if doctype == 'Student':
                        self.assertEqual(kwargs['filters']['date_of_birth'], date(2018, 10, 5))
                        return [Doc(name='S', student_name='Child')]
                    if doctype == 'Campus':
                        return [Doc(name='C', campus_name='Upper Mount Gravatt')]
                    if doctype == 'Course':
                        return [Doc(name='Art', course_name='Realistic Art - Beginner')]
                    raise AssertionError(doctype)
                for obj, name, value in (
                    (subject.inquiries, '_get_payload', payload), (subject.inquiries, '_validate_webhook_token', None),
                    (subject, 'validate_email_address', None), (frappe.db, 'get_value', None),
                    (frappe.db, 'savepoint', None), (frappe.db, 'sql', None), (frappe, 'new_doc', doc),
                    (subject.inquiries, '_resolve_campus', None), (subject.inquiries, '_resolve_course', None),
                    (subject.inquiries, '_resolve_parent', 'P'), (subject, 'getdate', date(2026, 9, 27)),
                    (subject, '_context', (first, slot, [first]))):
                    stack.enter_context(patch.object(obj, name, return_value=value))
                stack.enter_context(patch.object(frappe, 'get_all', side_effect=records))
                mapper = stack.enter_context(patch.object(subject.inquiries, '_map_trial_form_session', return_value={'course_session': 'CS'}))
                def finish_application(doc, context):
                    self.assertEqual(doc.status, 'Needs Review')
                    doc.update(status='Converted', converted_enrollment='ENR', converted_invoice='INV')
                complete = stack.enter_context(patch.object(subject, '_complete', side_effect=finish_application))
                notice = stack.enter_context(patch.object(subject, 'queue_inquiry_admin_notification'))
                result = subject.create_webhook(payload)
                self.assertEqual(result['inquiry_status'], expected_status)
                self.assertEqual(result['enrollment'], 'ENR' if expected_status == 'Converted' else None)
                self.assertEqual(result['invoice'], 'INV' if expected_status == 'Converted' else None)
                self.assertEqual(doc.requested_start_date, start_date)
                self.assertEqual(doc.submitted_student_dob, date(2018, 10, 5))
                self.assertEqual(json.loads(doc.raw_webhook_payload)['date_of_birth'], '05/10/2018')
                self.assertEqual(json.loads(doc.raw_webhook_payload)['start_date'], start_date)
                if expected_status == 'Converted':
                    self.assertEqual(mapper.call_args.args[0]['submitted_trial_date'], date(2026, 10, 5))
                else:
                    mapper.assert_not_called()
                if expected_status == 'Converted':
                    complete.assert_called_once()
                    notice.assert_called_once_with(doc)
                else:
                    notice.assert_called_once_with(doc)
                    complete.assert_not_called()

    def test_valid_submission_automatically_completes_enrollment(self):
        payload = {'external_submission_id': 'web:12:1', 'email': 'parent@example.com',
                   'parent_name': 'Parent', 'student_name': 'Child', 'date_of_birth': '2020-01-01',
                   'start_date': '2026-10-10'}
        doc = Doc(name='INQ', flags=Doc(), insert=Mock(), save=Mock())
        first = Doc(name='CS', session_date='2026-10-10')
        slot = Doc(campus='C', course='Art', start_time='09:00')
        with ExitStack() as stack:
            for obj, name, value in (
                (subject.inquiries, '_get_payload', payload), (subject.inquiries, '_validate_webhook_token', None),
                (subject, 'validate_email_address', None), (frappe.db, 'get_value', None),
                (frappe.db, 'savepoint', None), (frappe.db, 'sql', None), (frappe, 'new_doc', doc),
                (subject.inquiries, '_resolve_campus', 'C'), (subject.inquiries, '_resolve_course', 'Art'),
                (subject.inquiries, '_resolve_parent', 'P'), (subject, '_student', 'S'),
                (subject, '_match', 'CS'), (subject, '_context', (first, slot, [first]))):
                stack.enter_context(patch.object(obj, name, return_value=value))
            complete = stack.enter_context(patch.object(subject, '_complete', side_effect=lambda doc, context: doc.update(status='Converted', converted_enrollment='ENR', converted_invoice='INV')))
            notice = stack.enter_context(patch.object(subject, 'queue_inquiry_admin_notification'))
            result = subject.create_webhook(payload)
        self.assertEqual(result['status'], 'enrolled')
        self.assertEqual(result['inquiry_status'], 'Converted')
        self.assertFalse(result['review_required'])
        self.assertEqual(result['enrollment'], 'ENR')
        self.assertEqual(result['invoice'], 'INV')
        self.assertEqual(doc.course_session, 'CS')
        self.assertEqual(doc.requested_start_date, '2026-10-10')
        self.assertTrue(doc.flags.defer_admin_notification)
        doc.save.assert_called_once()
        notice.assert_called_once_with(doc)
        complete.assert_called_once_with(doc, (first, slot, [first]))

    def test_both_pending_states_can_be_reviewed_but_closed_states_cannot(self):
        for status in ('Planned', 'Needs Review', 'Cancelled', 'Inactive'):
            with self.subTest(status=status), patch('qas_custom.services.school_admin._require_school_admin'), \
                 patch.object(frappe, 'get_doc', return_value=Doc(inquiry_type=subject.DIRECT, status=status)), \
                 patch.object(frappe, 'throw', side_effect=ValueError):
                if status in ('Planned', 'Needs Review'):
                    self.assertEqual(subject._admin_doc('INQ').status, status)
                else:
                    with self.assertRaises(ValueError):
                        subject._admin_doc('INQ')

    def test_planned_submission_retry_does_not_notify_or_create_again(self):
        payload = {'external_submission_id': 'web:12:1', 'email': 'parent@example.com', 'parent_name': 'Parent'}
        doc = Doc(name='INQ', status='Planned', inquiry_type=subject.DIRECT, contact_email='parent@example.com')
        with patch.object(subject.inquiries, '_get_payload', return_value=payload), \
             patch.object(subject.inquiries, '_validate_webhook_token'), patch.object(subject, 'validate_email_address'), \
             patch.object(frappe.db, 'get_value', return_value='INQ'), patch.object(frappe, 'get_doc', return_value=doc), \
             patch.object(frappe, 'new_doc') as create, patch.object(subject, 'queue_inquiry_admin_notification') as notify:
            result = subject.create_webhook(payload)
        self.assertEqual(result['status'], 'planned')
        self.assertTrue(result['duplicate'])
        create.assert_not_called()
        notify.assert_not_called()


class TestReviewRevalidation(DatabaseTestCase):
    def test_expired_or_full_selection_remains_review_without_financial_writes(self):
        for operation in (subject.preview, subject.complete):
            with self.subTest(operation=operation.__name__):
                doc = Doc(name='INQ', status='Planned', save=Mock())
                with patch.object(subject, '_admin_doc', return_value=doc), \
                     patch.object(subject.inquiries, '_get_payload', return_value={}), \
                     patch.object(subject.inquiries, 'build_inquiry_detail', return_value={'inquiry': {'status': 'Needs Review'}}), \
                     patch.object(subject, '_manual_student'), \
                     patch.object(subject, '_context', side_effect=subject.ReviewRequired('Class is full')), \
                     patch.object(subject, '_complete') as create, \
                     patch.object(subject, 'queue_inquiry_admin_notification') as notify:
                    result = operation('INQ', {})
                self.assertTrue(result['review_required'])
                self.assertEqual(doc.status, 'Needs Review')
                self.assertEqual(doc.review_reason, 'Class is full')
                doc.save.assert_called_once()
                create.assert_not_called()
                notify.assert_not_called()

    def test_preview_cannot_demote_a_completed_application(self):
        doc = Doc(name='INQ', status='Converted', converted_enrollment='ENR')
        with patch.object(subject, '_admin_doc', return_value=doc), \
             patch.object(frappe, 'throw', side_effect=ValueError), \
             patch.object(subject, '_context') as context:
            with self.assertRaises(ValueError):
                subject.preview('INQ', {})
        context.assert_not_called()


class TestAutomaticEnrollmentFailure(DatabaseTestCase):
    def test_validation_failure_rolls_back_conversion_and_keeps_review_application(self):
        payload = {'external_submission_id': 'web:3:88', 'email': 'parent@example.com',
                   'parent_name': 'Parent', 'date_of_birth': '2018-06-20'}
        doc = Doc(name='INQ', flags=Doc(), insert=Mock(), save=Mock(), reload=Mock())
        context = (Doc(name='CS', session_date='2026-10-11'),
                   Doc(campus='C', course='Art', start_time='09:00'), [])
        with ExitStack() as stack:
            for obj, name, value in (
                (subject.inquiries, '_get_payload', payload), (subject.inquiries, '_validate_webhook_token', None),
                (subject, 'validate_email_address', None), (frappe.db, 'get_value', None),
                (frappe, 'new_doc', doc), (subject.inquiries, '_resolve_campus', 'C'),
                (subject.inquiries, '_resolve_course', 'Art'), (subject.inquiries, '_resolve_parent', 'P'),
                (subject, '_student', 'S'), (subject, '_match', 'CS'), (subject, '_context', context)):
                stack.enter_context(patch.object(obj, name, return_value=value))
            stack.enter_context(patch.object(subject, '_complete', side_effect=frappe.ValidationError('Course fee missing')))
            notify = stack.enter_context(patch.object(subject, 'queue_inquiry_admin_notification'))
            result = subject.create_webhook(payload)
        frappe.db.rollback.assert_called_once_with(save_point='direct_auto_enrollment')
        doc.reload.assert_called_once()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(doc.review_reason, 'Course fee missing')
        self.assertIsNone(result['enrollment'])
        notify.assert_called_once_with(doc)

    def test_website_source_note_preserves_existing_family_invoice_remarks(self):
        doc = Doc(name='INQ-123', submitted_form_name='<Form>', external_form_id='3',
                  external_submission_id='web:3:88')
        invoice = Doc(remarks='Existing sibling note', save=Mock())
        invoice.set = lambda field, value: invoice.update({field: value})
        with patch('qas_custom.modules.billing.commands.run_invoice_mutation_as_administrator', side_effect=lambda fn: fn()):
            subject._website_invoice_note(doc, invoice)
        self.assertTrue(invoice.remarks.startswith('Existing sibling note\n'))
        self.assertIn('Website enrollment via Fluent Form', invoice.remarks)
        self.assertIn('INQ-123', invoice.remarks)
        self.assertIn('web:3:88', invoice.remarks)
        self.assertIn('&lt;Form&gt;', invoice.remarks)
        invoice.save.assert_called_once_with(ignore_permissions=True)

    def test_completion_links_draft_invoice_and_creates_remaining_attendance(self):
        doc = Doc(name='INQ', student='S', source='Fluent Form')
        first = Doc(name='CS1', session_date='2026-10-11')
        slot = Doc(campus='C', course='Art', term='T', start_time='09:00')
        remaining = [first, Doc(name='CS2')]
        enrollment, invoice = Doc(name='ENR'), Doc(name='INV', docstatus=0)
        with ExitStack() as stack:
            create = stack.enter_context(patch('qas_custom.modules.enrollment.commands.create_full_term_enrollment', return_value=enrollment))
            billing = stack.enter_context(patch('qas_custom.modules.billing.commands.create_prorata_invoice', return_value=invoice))
            link = stack.enter_context(patch('qas_custom.modules.enrollment.commands.link_invoice_to_enrollment'))
            attendance = stack.enter_context(patch('qas_custom.modules.attendance.commands.create_full_term_attendance_entries'))
            stack.enter_context(patch('qas_custom.modules.inquiry.commands.mark_converted', side_effect=lambda d,e,i: d.update(status='Converted', converted_enrollment=e.name, converted_invoice=i.name)))
            for target in ('qas_custom.modules.inquiry.notes.add_conversion_note',
                           'qas_custom.modules.inquiry.notes.add_conversion_internal_note',
                           'qas_custom.modules.workflows.trial_conversion.apply_conversion_invoice_note',
                           'qas_custom.modules.notifications.enrollment_terms.queue_enrollment_terms_notice'):
                stack.enter_context(patch(target))
            source_note = stack.enter_context(patch.object(subject, '_website_invoice_note'))
            stack.enter_context(patch.object(frappe, 'session', Doc(user='Guest')))
            result = subject._complete(doc, (first, slot, remaining))
        self.assertEqual(result['status'], 'enrolled')
        self.assertEqual(invoice.docstatus, 0)
        billing.assert_called_once_with(doc, enrollment, 'Art', 'T', 'CS1', 2)
        link.assert_called_once_with(enrollment, invoice)
        attendance.assert_called_once_with(remaining, 'S', 'ENR')
        source_note.assert_called_once_with(doc, invoice)
