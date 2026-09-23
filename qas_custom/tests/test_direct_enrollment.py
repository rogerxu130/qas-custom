from datetime import date
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
        doc = Doc(name='INQ', insert=Mock(), save=Mock())
        with patch.object(subject.inquiries, '_get_payload', return_value=payload), patch.object(subject.inquiries, '_validate_webhook_token'), patch.object(subject, 'validate_email_address'), patch.object(subject.frappe.db, 'get_value', return_value=None), patch.object(subject.frappe.db, 'savepoint'), patch.object(subject.frappe.db, 'sql'), patch.object(subject.frappe, 'new_doc', return_value=doc), patch.object(subject.inquiries, '_resolve_campus', return_value=None), patch.object(subject.inquiries, '_resolve_course', return_value=None), patch.object(subject.inquiries, '_resolve_parent', return_value='P'), patch.object(subject, '_student', return_value='S'), patch.object(subject, '_match', side_effect=subject.ReviewRequired('Wrong date')), patch.object(subject, '_complete') as complete:
            result = subject.create_webhook(payload)
            self.assertEqual(result['status'], 'needs_review')
            self.assertEqual(doc.review_reason, 'Wrong date')
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
            result = subject._complete(doc, (session, slot, [session]))
            self.assertEqual(result['status'], 'enrolled')
            create_enrollment.assert_called_once()
            create_invoice.assert_called_once_with(doc, enrollment, 'Drawing', 'T', 'SESSION', 1)
            attendance.assert_called_once_with([session], 'S', 'ENR')
            reward.assert_not_called()
            frappe.db.commit.assert_not_called()

    def test_changed_preview_prevents_financial_writes(self):
        doc = Doc(name='INQ', parent='P', student='S', status='Needs Review')
        context = (Doc(name='S'), Doc(course='Drawing'), [Doc(name='S')])
        with patch.object(subject, '_admin_doc', return_value=doc), patch.object(subject.inquiries, '_get_payload', return_value={'expected_amount': 99, 'expected_sessions': 1}), patch.object(subject, '_manual_student'), patch.object(subject, '_context', return_value=context), patch('qas_custom.modules.billing.commands.get_prorata_invoice_context', return_value={'invoice_amount': 100}), patch('frappe.utils.flt', side_effect=lambda value, *args: float(value)), patch.object(frappe, 'throw', side_effect=ValueError), patch.object(subject, '_complete') as complete:
            with self.assertRaises(ValueError):
                subject.complete('INQ', {})
            complete.assert_not_called()
