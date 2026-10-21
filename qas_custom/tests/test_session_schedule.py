from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import Mock, MagicMock, patch

import frappe

from qas_custom.services.session_schedule import (
    session_timeslot, plan_schedule_change, initialise_session_times,
    preserve_session_times, guard_weekly_schedule_change, _check_resource_conflicts,
    apply_session_changes,
    _check_pass_booking_dates,
)


def slot(**updates):
    result = frappe._dict(name="CLASS-1", day_of_week="Saturday", start_time="14:00:00",
                          end_time="15:30:00", term="TERM", classroom="ROOM", teacher="TEACHER")
    result.update(updates)
    return result


def session(name, date, **updates):
    result = frappe._dict(name=name, session_date=date, status="Scheduled", weekly_timeslot="CLASS-1",
                          start_time="14:00:00", end_time="15:30:00")
    result.update(updates)
    return result


class TestSessionSchedule(TestCase):
    def setUp(self):
        self.old = slot()
        self.new = slot(start_time="16:00:00", end_time="17:30:00")
        self.term = frappe._dict(start_date="2026-10-01", end_date="2026-12-20", status="Active")
        self.now = datetime(2026, 10, 21, 10)
        self.rows = [session("HISTORY", "2026-10-17"), session("FIRST", "2026-10-24"), session("SECOND", "2026-10-31")]
        self.translate = patch("qas_custom.services.session_schedule._", side_effect=lambda s:s).start()
        patch("qas_custom.services.session_schedule.frappe.throw", side_effect=ValueError).start()
        self.addCleanup(patch.stopall)

    def plan(self, **kwargs):
        return plan_schedule_change(self.old, self.new, kwargs.pop("rows", self.rows), kwargs.pop("locked", set()),
            kwargs.pop("effective_date", "2026-10-24"), kwargs.pop("term", self.term), now=kwargs.pop("now", self.now), **kwargs)

    def test_time_change_is_inclusive_and_preserves_historical_records(self):
        result = self.plan()
        self.assertEqual([r["session"] for r in result["changes"]], ["FIRST", "SECOND"])
        self.assertEqual(result["changes"][0]["session_date"], "2026-10-24")
        self.assertEqual(result["changes"][0]["start_time"], "16:00:00")
        self.assertEqual(result["retained"][0]["old_start_time"], "14:00:00")
        self.assertEqual(self.rows[1].start_time, "14:00:00")

    def test_future_effective_date_keeps_intervening_session_at_old_time(self):
        result = self.plan(effective_date="2026-10-31")
        self.assertEqual([r["session"] for r in result["changes"]], ["SECOND"])
        self.assertEqual([r["session"] for r in result["retained"]], ["HISTORY", "FIRST"])

    def test_weekday_change_preserves_ids_and_number_of_sessions(self):
        self.new.day_of_week = "Sunday"
        result = self.plan()
        self.assertEqual([(r["session"], r["session_date"]) for r in result["changes"]], [("FIRST", "2026-10-25"), ("SECOND", "2026-11-01")])
        self.assertEqual(result["blocking_errors"], [])

    def test_locked_session_keeps_position_without_compressing_other_sessions(self):
        self.new.day_of_week = "Sunday"
        result = self.plan(locked={"FIRST"})
        self.assertEqual([(r["session"], r["session_date"]) for r in result["changes"]], [("SECOND", "2026-11-01")])
        self.assertEqual(result["skipped"], 1)

    def test_completed_cancelled_and_started_sessions_are_preserved(self):
        rows = [session("DONE", "2026-10-24", status="Completed"), session("CANCELLED", "2026-10-31", status="Cancelled"), session("STARTED", "2026-10-21")]
        result = self.plan(rows=rows, effective_date="2026-10-21", now=datetime(2026,10,21,15))
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["skipped"], 3)

    def test_target_after_term_end_blocks_instead_of_losing_a_session(self):
        self.new.day_of_week = "Sunday"
        self.term.end_date = "2026-10-31"
        self.assertIn("after the term ends", " ".join(self.plan()["blocking_errors"]))

    def test_retained_cancelled_target_date_blocks_move(self):
        self.new.day_of_week = "Sunday"
        result = self.plan(rows=self.rows + [session("CLOSED", "2026-10-25", status="Cancelled")])
        self.assertIn("retained or cancelled session", " ".join(result["blocking_errors"]))

    def test_invalid_effective_dates_and_times_are_rejected(self):
        for effective in (None, "2026-10-20", "2027-01-01"):
            with self.subTest(effective=effective), self.assertRaises(ValueError):
                self.plan(effective_date=effective)
        self.new.end_time = "15:00"
        with self.assertRaises(ValueError):
            self.plan()

    def test_same_day_cannot_move_a_session_to_a_time_already_passed(self):
        self.new.start_time = "09:00:00"
        result = self.plan(rows=[session("TODAY", "2026-10-21")], effective_date="2026-10-21")
        self.assertIn("already passed", " ".join(result["blocking_errors"]))

    def test_empty_class_still_has_valid_new_weekly_schedule(self):
        result = self.plan(rows=[])
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["after"]["start_time"], "16:00:00")

    def test_repeated_change_retains_each_sessions_saved_times(self):
        self.old = self.new
        self.new = slot(start_time="18:00:00", end_time="19:30:00")
        rows = [self.rows[0], session("FIRST", "2026-10-24", start_time="16:00:00", end_time="17:30:00"), session("SECOND", "2026-10-31", start_time="16:00:00", end_time="17:30:00")]
        result = self.plan(rows=rows, effective_date="2026-10-31")
        self.assertEqual([r["old_start_time"] for r in result["retained"]], ["14:00:00", "16:00:00"])
        self.assertEqual(result["changes"][0]["old_start_time"], "16:00:00")

    def test_render_uses_snapshot_without_mutating_shared_class(self):
        historical = session_timeslot(self.rows[0], self.new)
        future = session_timeslot(session("FUTURE", "2026-10-31", start_time="16:00:00"), self.new)
        self.assertEqual(historical.start_time, "14:00:00")
        self.assertEqual(future.start_time, "16:00:00")
        self.assertEqual(self.new.start_time, "16:00:00")
        self.assertIsNot(historical, self.new)

    def test_midnight_snapshot_is_not_treated_as_missing(self):
        actual = session_timeslot(session("MIDNIGHT", "2026-10-24", start_time=timedelta(0)), self.new)
        self.assertEqual(actual.start_time, timedelta(0))

    def test_legacy_unsnapshotted_session_uses_old_class_time(self):
        result = self.plan(rows=[session("LEGACY", "2026-10-24", start_time=None, end_time=None)])
        self.assertEqual(result["changes"][0]["old_start_time"], "14:00:00")

    def test_new_session_snapshots_time_once_and_subsequent_save_keeps_it(self):
        doc = Mock()
        values = {"weekly_timeslot": "CLASS-1"}
        doc.get.side_effect = values.get
        doc.set.side_effect = values.__setitem__
        doc.is_new.return_value = True
        db = Mock()
        db.get_value.return_value = self.old
        with patch("qas_custom.services.session_schedule.frappe.db", db):
            initialise_session_times(doc)
            db.get_value.return_value = self.new
            initialise_session_times(doc)
        self.assertEqual(values["start_time"], "14:00:00")
        db.get_value.assert_called_once()

    def test_backfill_only_updates_missing_values(self):
        db = Mock()
        with patch("qas_custom.services.session_schedule.frappe.db", db):
            preserve_session_times(self.old)
        query, values = db.sql.call_args.args
        self.assertIn("COALESCE(start_time", query)
        self.assertEqual(values, ("14:00:00", "15:30:00", "CLASS-1"))

    def test_normalised_unchanged_times_do_not_trigger_direct_edit_guard(self):
        doc = Mock()
        doc.get_doc_before_save.return_value = slot(start_time=timedelta(hours=14), end_time=timedelta(hours=15,minutes=30))
        doc.get.side_effect = self.old.get
        db = Mock()
        with patch("qas_custom.services.session_schedule.frappe.db", db):
            guard_weekly_schedule_change(doc)
        db.exists.assert_not_called()

    def test_conflict_uses_other_sessions_saved_time_and_teacher_override(self):
        plan = self.plan()
        other = session("OTHER", "2026-10-24", weekly_timeslot="CLASS-2", start_time="16:30:00", end_time="18:00:00", teacher_override="TEACHER")
        other_slot = slot(name="CLASS-2", start_time="08:00:00", end_time="09:00:00", classroom="OTHER-ROOM", teacher="OTHER-TEACHER")
        with patch("qas_custom.services.session_schedule.frappe.get_all", side_effect=[[other],[other_slot]]), patch("qas_custom.services.session_schedule.frappe.db", Mock(get_value=lambda dt, name, field: name)):
            _check_resource_conflicts(self.new, self.rows, plan)
        self.assertIn("OTHER", " ".join(plan["blocking_errors"]))


    def test_conflict_details_identify_resources_courses_and_actual_times(self):
        for room, teacher, expected, absent in [
            ("ROOM", "OTHER-TEACHER", "教室 Indooroopilly / R5", "老师 Roman在"),
            ("OTHER-ROOM", "TEACHER", "老师 Roman", "教室 Indooroopilly / R5在"),
            ("ROOM", "TEACHER", "老师 Roman、教室 Indooroopilly / R5", None),
        ]:
            with self.subTest(room=room, teacher=teacher):
                plan = self.plan()
                self.new.update(course="STORY", campus="CAMPUS", class_language="English")
                other = session("OTHER", "2026-10-24", weekly_timeslot="CLASS-2", start_time="16:30", end_time="18:00")
                other_slot = slot(name="CLASS-2", course="ART", campus="CAMPUS", classroom=room, teacher=teacher, start_time="08:00", end_time="09:00")
                names = {"STORY": "Storytelling", "ART": "Realistic Art", "CAMPUS": "Indooroopilly", "ROOM": "R5", "TEACHER": "Roman"}
                db = Mock(get_value=lambda dt, name, field: names.get(name, name))
                with patch("qas_custom.services.session_schedule.frappe.get_all", side_effect=[[other], [other_slot]]), patch("qas_custom.services.session_schedule.frappe.db", db):
                    _check_resource_conflicts(self.new, self.rows, plan)
                message = plan["blocking_errors"][0]
                self.assertIn(expected, message.split("在以下")[0])
                if absent:
                    self.assertNotIn(absent, message)
                for text in ("2026-10-24", "本次调整：Storytelling", "16:00–17:30", "已有课程：Realistic Art", "16:30–18:00", "课次 OTHER", "班级 CLASS-2", "尚未保存"):
                    self.assertIn(text, message)
                self.assertNotIn("08:00–09:00", message)

    def test_adjacent_sessions_do_not_report_conflict(self):
        plan = self.plan()
        other = session("OTHER", "2026-10-24", weekly_timeslot="CLASS-2", start_time="17:30", end_time="19:00")
        with patch("qas_custom.services.session_schedule.frappe.get_all", side_effect=[[other], [slot(name="CLASS-2")]]):
            _check_resource_conflicts(self.new, self.rows, plan)
        self.assertEqual(plan["blocking_errors"], [])

    def test_saving_updates_dependencies_without_changing_enrollment(self):
        plan = self.plan(rows=[self.rows[1]])
        db = Mock()
        def get_all(doctype, **kwargs):
            return {"Inquiry": ["TRIAL"], "Adhoc Booking": ["PAYG"], "QAS PAYG Booking": ["PASS-BOOKING"], "Leave Request": ["LEAVE"]}.get(doctype, [])
        with patch("qas_custom.services.session_schedule.frappe.db", db), patch("qas_custom.services.session_schedule.frappe.get_all", side_effect=get_all), patch("qas_custom.services.session_schedule.frappe.get_doc", return_value=self.rows[1]), patch("qas_custom.services.session_schedule.frappe.clear_document_cache"), patch("qas_custom.services.class_record_labels.build_course_session_label", return_value="New label"):
            apply_session_changes(plan)
        touched = [call.args[0] for call in db.set_value.call_args_list]
        self.assertNotIn("Enrollment", touched)
        booking = next(call for call in db.set_value.call_args_list if call.args[0] == "Adhoc Booking")
        self.assertEqual(booking.args[2]["cancellable_until"], datetime(2026,10,21,16))
        ten_class = next(call for call in db.set_value.call_args_list if call.args[0] == "QAS PAYG Booking")
        self.assertEqual(ten_class.args[2:], ("cancellable_until", datetime(2026,10,21,16)))
        db.commit.assert_not_called()

    def test_shared_booking_resources_use_saved_session_time(self):
        from qas_custom.modules.course_schedule.session_resources import session_context
        with patch("qas_custom.modules.course_schedule.session_resources.frappe.get_doc", side_effect=[self.rows[0], self.new]):
            occurrence, actual = session_context("HISTORY")
        self.assertEqual(actual.start_time, "14:00:00")
        self.assertEqual(self.new.start_time, "16:00:00")
        self.assertEqual(occurrence.name, "HISTORY")

    def test_ten_class_pass_booking_cannot_be_moved_beyond_card_expiry(self):
        plan = self.plan(rows=[self.rows[1]])
        booking = frappe._dict(name="PASS-BOOKING", course_session="FIRST", card="CARD", status="Reserved")
        card = frappe._dict(name="CARD", expires_on="2026-10-23")
        with patch("qas_custom.services.session_schedule.frappe.get_all", side_effect=[[booking],[card]]):
            references = _check_pass_booking_dates(plan)
        self.assertIn("ten-class pass expiry", " ".join(plan["blocking_errors"]))
        self.assertEqual(references[0]["card_expiry"], "2026-10-23")

    def test_ten_class_pass_history_uses_saved_session_time(self):
        from qas_custom.services.payg_read_models import _session_context
        self.rows[0].teacher_override = None
        self.new.course = "ART"
        def rows(doctype, *args, **kwargs):
            return {"Course Sessions": [self.rows[0]], "Weekly Timeslot": [self.new], "Teacher": []}.get(doctype, [])
        with patch("qas_custom.services.payg_read_models._rows", side_effect=rows), patch("qas_custom.services.payg_read_models._course_labels", return_value={}):
            context = _session_context({"HISTORY"})
        self.assertEqual(context["HISTORY"]["start_time"], "14:00:00")

    def test_teacher_portal_reads_historical_time_instead_of_current_class_time(self):
        from qas_custom.services.teacher_portal import get_teacher_sessions_data
        historical = self.rows[0]
        with patch("qas_custom.services.teacher_portal._require_teacher", return_value=frappe._dict(name="TEACHER")), patch("qas_custom.services.teacher_portal._get_teacher_timeslots", return_value=[self.new]), patch("qas_custom.services.teacher_portal._get_timeslot_map", return_value={"CLASS-1": self.new}), patch("qas_custom.services.teacher_portal.frappe.get_all", side_effect=[[historical],[]]), patch("qas_custom.services.teacher_portal._get_attendance_by_session", return_value={}), patch("qas_custom.services.teacher_portal._get_student_map", return_value={}):
            result = get_teacher_sessions_data("2026-10-01", "2026-10-31")
        self.assertEqual(result["items"][0]["start_time"], "14:00:00")
        self.assertEqual(result["items"][0]["end_time"], "15:30:00")

    def test_leave_deadline_and_link_label_use_actual_session_time(self):
        from qas_custom.modules.makeup.commands import _get_session_start
        from qas_custom.services.class_record_labels import build_course_session_label
        historical = self.rows[0]
        self.assertEqual(_get_session_start(historical, self.new), datetime(2026,10,17,14))
        db = Mock()
        db.get_value.side_effect = lambda doctype, name, fields, **kwargs: self.new if doctype == "Weekly Timeslot" else name
        with patch("qas_custom.services.class_record_labels.frappe.db", db):
            label = build_course_session_label(historical)
        self.assertIn("14:00-15:30", label)
        self.assertNotIn("16:00", label)

    def test_deletion_and_combined_change_guard_use_saved_start_boundary(self):
        from qas_custom.services.weekly_timeslot_course_change import _session_has_started
        historical = session("TODAY", "2026-10-24")
        self.assertTrue(_session_has_started(historical, "16:00", datetime(2026,10,24,15)))

    def test_regeneration_does_not_create_new_weekday_sessions_before_effective_date(self):
        from qas_custom.services.school_admin import generate_school_admin_course_sessions_data
        future_slot = slot(day_of_week="Sunday", schedule_effective_date="2026-10-31")
        with patch("qas_custom.services.school_admin._require_school_admin"), patch("qas_custom.services.school_admin.frappe.get_doc", return_value=future_slot), patch("qas_custom.services.school_admin._ensure_course_session", return_value={"created":False}) as ensure, patch("qas_custom.services.school_admin.frappe.db", Mock()):
            generate_school_admin_course_sessions_data("CLASS-1", "2026-10-01", "2026-11-08")
        self.assertEqual([str(call.args[1]) for call in ensure.call_args_list], ["2026-11-01", "2026-11-08"])


class TestScheduleSaveReview(TestCase):
    def test_combined_preterm_change_preserves_times_and_uses_shared_plan(self):
        from contextlib import ExitStack
        from qas_custom.services import weekly_timeslot_course_change as service
        plan = {"changes": [{"session": "FUTURE", "session_date": "2026-10-31", "start_time": "16:00:00", "end_time": "17:30:00"}]}
        report = {"blocking_errors": [], "confirmation_token": "token", "effective_date": "2026-10-31",
                  "intended_payload": {"start_time": "16:00", "end_time": "17:30"}, "changes": {"start_time": {}},
                  "sessions": [], "session_date_mapping": [], "enrollments": [], "schedule_plan": plan}
        doc = Mock()
        doc.name = "CLASS-1"
        with ExitStack() as stack:
            for name in ("_require_school_admin", "_apply_weekly_timeslot_payload", "_add_comment", "refresh_linked_course_session_labels"):
                stack.enter_context(patch.object(service, name))
            stack.enter_context(patch.object(service, "_preview", return_value=report))
            stack.enter_context(patch.object(service.frappe, "get_doc", return_value=doc))
            stack.enter_context(patch.object(service.frappe, "db", Mock()))
            stack.enter_context(patch.object(service, "_", side_effect=lambda s:s))
            preserve = stack.enter_context(patch.object(service, "preserve_session_times"))
            apply = stack.enter_context(patch.object(service, "apply_session_changes"))
            service.execute.__wrapped__("CLASS-1", "ART", "token")
        preserve.assert_called_once_with(doc)
        apply.assert_called_once_with(plan)
        self.assertTrue(doc.flags.reviewed_schedule_change)
        self.assertEqual(doc.schedule_effective_date, "2026-10-31")

    def test_stale_preview_is_rejected_before_any_save(self):
        from qas_custom.services.school_admin import update_school_admin_weekly_timeslot_data
        doc = Mock()
        doc.name = "CLASS-1"
        doc.get.side_effect = slot().get
        db = Mock()
        cache = MagicMock()
        with patch("qas_custom.services.school_admin._require_school_admin"), patch("qas_custom.services.school_admin._validate_weekly_timeslot_change"), patch("qas_custom.services.school_admin._weekly_timeslot_value_changed", return_value=True), patch("qas_custom.services.school_admin.frappe.get_doc", return_value=doc), patch("qas_custom.services.school_admin.frappe.cache", cache), patch("qas_custom.services.school_admin.frappe.db", db), patch("qas_custom.services.session_schedule.build_schedule_preview", return_value={"blocking_errors": [], "confirmation_token": "new-token"}), patch("qas_custom.services.school_admin._", side_effect=lambda s:s), patch("qas_custom.services.school_admin.frappe.throw", side_effect=ValueError):
            with self.assertRaises(ValueError):
                update_school_admin_weekly_timeslot_data("CLASS-1", {"start_time":"16:00", "schedule_confirmation_token":"old-token"})
        doc.save.assert_not_called()
        db.set_value.assert_not_called()
        db.commit.assert_not_called()
