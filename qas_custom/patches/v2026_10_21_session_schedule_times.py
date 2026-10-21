"""Capture the currently known session times without overwriting prior snapshots."""
import frappe


def execute():
    frappe.reload_doc("qas_custom", "doctype", "course_sessions")
    frappe.reload_doc("qas_custom", "doctype", "weekly_timeslot")
    frappe.db.sql("""
        UPDATE `tabCourse Sessions` s
        INNER JOIN `tabWeekly Timeslot` w ON w.name = s.weekly_timeslot
        SET s.start_time = COALESCE(s.start_time, w.start_time),
            s.end_time = COALESCE(s.end_time, w.end_time)
        WHERE s.start_time IS NULL OR s.end_time IS NULL
    """)
    from qas_custom.services.class_record_labels import refresh_linked_course_session_labels
    for name in frappe.get_all("Weekly Timeslot", pluck="name", limit_page_length=0):
        refresh_linked_course_session_labels(frappe._dict(name=name))
