from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.modules.makeup import commands, eligibility


class TestParentMakeupSessionRoster(TestCase):
    def row(self, **values):
        return dict(student="S", enrollment_type="Full-Term", status="To be started", **values)

    def test_regular_trial_and_payg_qualify(self):
        for kind in ("Full-Term", "Trial", "Pay-as-you-go"):
            self.assertTrue(eligibility.has_regular_or_trial_student([dict(student="S", enrollment_type=kind, status="To be started")]))

    def test_empty_inactive_and_makeup_only_do_not_qualify(self):
        self.assertFalse(eligibility.has_regular_or_trial_student([]))
        for changes in ({"status": "Cancelled"}, {"status": "Leave"}, {"enrollment_type": "Makeup"}, {"makeup_voucher": "MV"}, {"source_doctype": "Makeup Voucher"}, {"student": None}):
            self.assertFalse(eligibility.has_regular_or_trial_student([{**self.row(), **changes}]))
        self.assertTrue(eligibility.has_regular_or_trial_student([self.row(), {**self.row(), "enrollment_type": "Makeup"}]))

    def test_bulk_roster_query_returns_eligible_session_ids(self):
        rows = [{**self.row(), "course_session": "occupied"}, {**self.row(), "course_session": "makeup", "makeup_voucher": "MV"}]
        with patch.object(eligibility.frappe, "get_all", return_value=rows) as query:
            self.assertEqual(eligibility.sessions_with_regular_or_trial_students(["occupied", "empty", "makeup"]), {"occupied"})
        self.assertEqual(query.call_args.kwargs["limit_page_length"], 0)

    def test_parent_list_hides_empty_sessions_and_admin_list_keeps_them(self):
        sessions = [dict(name=name, weekly_timeslot="W", status="Scheduled") for name in ("occupied", "empty")]
        with ExitStack() as stack:
            stack.enter_context(patch.object(commands.frappe, "get_all", return_value=sessions))
            stack.enter_context(patch.object(commands, "today", return_value="2026-09-09"))
            stack.enter_context(patch.object(commands, "get_weekly_timeslot_map", return_value={"W": {"course": "Art"}}))
            stack.enter_context(patch.object(commands, "get_teacher_name_map", return_value={}))
            stack.enter_context(patch.object(commands, "_course_accepts_makeup_voucher", return_value=True))
            stack.enter_context(patch.object(commands, "_student_session_can_redeem_voucher", return_value=True))
            roster = stack.enter_context(patch.object(commands, "sessions_with_regular_or_trial_students", return_value={"occupied"}))
            voucher = dict(student="S", course="Art")
            parent = commands._get_redeemable_makeup_sessions(voucher, "S")
            admin = commands._get_redeemable_makeup_sessions(voucher, "S", allow_empty_sessions=True)
        self.assertEqual([row["session_id"] for row in parent], ["occupied"])
        self.assertEqual([row["session_id"] for row in admin], ["occupied", "empty"])
        self.assertEqual(roster.call_count, 2)
        self.assertTrue(admin[0]["has_regular_or_trial_students"])
        self.assertFalse(admin[1]["has_regular_or_trial_students"])

    def test_locked_check_rejects_session_emptied_after_preview(self):
        fake = SimpleNamespace(db=SimpleNamespace(sql=Mock(return_value=[])), throw=Mock(side_effect=ValueError("empty session")))
        with patch.object(eligibility, "frappe", fake):
            with self.assertRaisesRegex(ValueError, "empty session"):
                eligibility.validate_parent_session_roster("CS")
        self.assertIn("FOR UPDATE", fake.db.sql.call_args.args[0])

    def test_submit_checks_live_roster_even_if_list_still_includes_session(self):
        with patch.object(commands, "validate_parent_session_roster", side_effect=ValueError("empty")), patch.object(commands, "_get_redeemable_makeup_sessions", return_value=[{"session_id": "CS"}]) as listing:
            with self.assertRaises(ValueError):
                commands._validate_session_can_redeem_voucher({}, "CS", "S")
        listing.assert_not_called()

    def test_admin_submit_bypasses_roster_check(self):
        with patch.object(commands, "validate_parent_session_roster") as roster, patch.object(commands, "_get_redeemable_makeup_sessions", return_value=[{"session_id": "CS"}]) as listing:
            commands._validate_session_can_redeem_voucher({}, "CS", "S", allow_empty_sessions=True)
        roster.assert_not_called()
        self.assertTrue(listing.call_args.kwargs["allow_empty_sessions"])

    def test_live_regular_student_allows_submission(self):
        fake = SimpleNamespace(db=SimpleNamespace(sql=Mock(return_value=[self.row()])), throw=Mock())
        with patch.object(eligibility, "frappe", fake):
            eligibility.validate_parent_session_roster("CS")
        fake.throw.assert_not_called()

    def test_concentrated_parent_options_include_empty_and_makeup_only_sessions(self):
        import frappe
        from qas_custom.services import concentrated_makeup as service
        from qas_custom.services import parent_portal_read as parents
        voucher = frappe._dict(name="MV", student="S", status="Valid", course="Art")
        course = frappe._dict(is_makeup_course=1, accepted_makeup_course=[{"course": "Art"}])
        fake = SimpleNamespace(get_all=Mock(side_effect=[["MV"], ["occupied", "makeup-only", "empty"]]), get_doc=Mock(side_effect=[voucher, course, course, course]))
        with ExitStack() as stack:
            stack.enter_context(patch.object(service, "frappe", fake))
            stack.enter_context(patch.object(service, "today", return_value="2026-09-09"))
            stack.enter_context(patch.object(parents, "_require_parent", return_value=SimpleNamespace(name="P")))
            stack.enter_context(patch.object(parents, "_get_parent_students", return_value=[{"name": "S"}]))
            stack.enter_context(patch.object(commands, "_build_makeup_voucher_payload", return_value={"voucher_id": "MV", "course": "Art"}))
            stack.enter_context(patch.object(commands, "_build_redeem_session_payload", side_effect=lambda sid: {"session_id": sid, "session_date": "2026-10-01", "start_time": "10:00"}))
            stack.enter_context(patch.object(service, "session_context", side_effect=lambda sid: (frappe._dict(name=sid, concentrated_makeup_capacity=5), frappe._dict(course="Makeup"))))
            stack.enter_context(patch.object(service, "session_is_future", return_value=True))
            stack.enter_context(patch.object(service, "student_has_conflict", return_value=False))
            stack.enter_context(patch.object(service, "active_rows", side_effect=lambda sid: [] if sid == "empty" else ([self.row()] if sid == "occupied" else [{**self.row(), "enrollment_type": "Makeup"}])))
            result = service.get_options()
        self.assertEqual([row["session_id"] for row in result["sessions"]], ["empty", "makeup-only", "occupied"])
        self.assertEqual([row["spots_left"] for row in result["sessions"]], [5, 4, 5])
