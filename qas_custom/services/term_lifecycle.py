"""Term-level availability. Child records keep their original business status."""
from contextvars import ContextVar
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import cint, get_time, getdate, now_datetime

OPEN_STATUSES = ("Upcoming", "Active")
CLOSED_STATUSES = ("Completed", "Archived")
_confirmed_transition = ContextVar("qas_confirmed_term_transition", default=None)


def lock_term(term):
    if not term:
        frappe.throw(_("Term is required."))
    rows = frappe.db.sql("SELECT name, status FROM `tabTerm` WHERE name=%s FOR UPDATE", (term,), as_dict=True)
    if not rows:
        frappe.throw(_("Term was not found."))
    return rows[0]


def require_open_term(term):
    if term and lock_term(term).status not in OPEN_STATUSES:
        frappe.throw(_("This term is closed. Reopen it before starting a new enrollment or class operation."))


def require_open_timeslot(timeslot):
    if timeslot:
        term = frappe.db.get_value("Weekly Timeslot", timeslot, "term")
        if not term:
            frappe.throw(_("The weekly timeslot has no valid term."))
        require_open_term(term)


def _unfinished_session(row, current_time):
    if row.get("status") in ("Completed", "Cancelled"):
        return False
    if not row.get("session_date"):
        return True
    day = getdate(row.session_date)
    if day < current_time.date():
        return False
    if day > current_time.date():
        return True
    if not row.get("end_time"):
        return True
    try:
        return datetime.combine(day, get_time(row.end_time)) > current_time
    except (TypeError, ValueError):
        return True


def closure_preview(term, *, for_update=False):
    # One joined query, across ALL timeslots, including inactive ones.
    rows = frappe.db.sql("""
        SELECT s.name, s.session_date, s.status, w.name AS weekly_timeslot,
               w.course, w.start_time, w.end_time
        FROM `tabCourse Sessions` s JOIN `tabWeekly Timeslot` w ON w.name=s.weekly_timeslot
        WHERE w.term=%s AND COALESCE(s.status, 'Scheduled') NOT IN ('Completed', 'Cancelled')
          AND (s.session_date >= %s OR s.session_date IS NULL)
        ORDER BY s.session_date, w.start_time, s.name
    """ + (" FOR UPDATE" if for_update else ""), (term, now_datetime().date()), as_dict=True)
    current_time = now_datetime()
    blockers = [dict(row) for row in rows if _unfinished_session(row, current_time)]
    return {
        "term": term,
        "can_close": not blockers,
        "blockers": blockers,
        "weekly_timeslots": frappe.db.count("Weekly Timeslot", {"term": term}),
        "enrollments": frappe.db.count("Enrollment", {"term": term}),
    }


@frappe.whitelist()
def preview(term):
    from qas_custom.services.school_admin import _require_school_admin
    _require_school_admin()
    if not frappe.db.exists("Term", term):
        frappe.throw(_("Term was not found."))
    result = closure_preview(term)
    result["status"] = frappe.db.get_value("Term", term, "status")
    return result


@frappe.whitelist(methods=["POST"])
def transition(term, action, confirmed=0, reopen_status=None):
    from qas_custom.services.school_admin import _require_school_admin
    _require_school_admin()
    if not cint(confirmed):
        frappe.throw(_("Please confirm this term operation."))
    if action not in ("close", "reopen"):
        frappe.throw(_("Invalid term operation."))
    if not frappe.get_meta("Term").has_field("closed_from_status"):
        frappe.throw(_("Update QAS Custom and run site migration before closing or reopening terms."))
    frappe.db.savepoint("term_transition")
    try:
        lock_term(term)
        doc = frappe.get_doc("Term", term, for_update=True)
        if action == "close":
            if doc.status in CLOSED_STATUSES:
                return {"term": term, "status": doc.status, "unchanged": True}
            result = closure_preview(term, for_update=True)
            if not result["can_close"]:
                frappe.throw(_("Cannot close this term: {0} future or unfinished class(es) remain. Refresh the close preview for details.").format(len(result["blockers"])))
            doc.closed_from_status = doc.status
            doc.closed_at = now_datetime()
            doc.closed_by = frappe.session.user
            doc.status = "Archived"
        else:
            if doc.status in OPEN_STATUSES:
                return {"term": term, "status": doc.status, "unchanged": True}
            target = doc.get("closed_from_status") or reopen_status
            if target not in OPEN_STATUSES:
                frappe.throw(_("Choose Active or Upcoming when reopening a legacy term."))
            doc.status = target
        token = _confirmed_transition.set((doc.name, doc.status))
        try:
            doc.save(ignore_permissions=True)
        finally:
            _confirmed_transition.reset(token)
        doc.add_comment("Info", _("Term {0}; related enrollments, weekly timeslots, sessions and attendance were not modified.").format(action))
    except Exception:
        frappe.db.rollback(save_point="term_transition")
        raise
    return {"term": term, "status": doc.status}


def validate_term(doc):
    old = doc.get_doc_before_save()
    if not old:
        if doc.status in CLOSED_STATUSES:
            frappe.throw(_("Create an open term, then use Close Term."))
        return
    if old.status == doc.status:
        return
    if old.status in CLOSED_STATUSES or doc.status in CLOSED_STATUSES:
        if _confirmed_transition.get() != (doc.name, doc.status):
            frappe.throw(_("Use Close Term or Reopen Term and confirm the operation."))


def validate_term_child(doc, method=None):
    """Serialize new operational writes with closure; allow historical corrections."""
    old = doc.get_doc_before_save()
    if doc.doctype == "Enrollment":
        term = doc.get("term")
        slot = doc.get("weekly_timeslot")
        if slot and term and (not old or old.get("term") != term or old.get("weekly_timeslot") != slot):
            slot_term = frappe.db.get_value("Weekly Timeslot", slot, "term")
            if slot_term != term:
                frappe.throw(_("Enrollment term must match its weekly timeslot's term."))
        if doc.get("enrollment_type") == "Full-Term" and not term:
            frappe.throw(_("Full-Term enrollment requires a term."))
        operational_fields = ("term", "weekly_timeslot", "student", "course", "enrollment_type", "start_course_session")
        new_operation = not old or any(doc.get(key) != old.get(key) for key in operational_fields)
        new_operation = new_operation or (old and doc.get("status") in ("Active", "Planned") and old.get("status") != doc.get("status"))
        if new_operation:
            if old:
                require_open_term(old.get("term"))
            require_open_term(term)
            require_open_timeslot(slot)
    elif doc.doctype == "Weekly Timeslot":
        fields = ("term", "course", "campus", "classroom", "teacher", "day_of_week", "start_time", "end_time")
        if not old or any(doc.get(key) != old.get(key) for key in fields) or (doc.get("status") == "Active" and old.get("status") != "Active"):
            if old:
                require_open_term(old.get("term"))
            require_open_term(doc.get("term"))
    elif doc.doctype == "Course Sessions":
        if not old or any(doc.get(key) != old.get(key) for key in ("weekly_timeslot", "session_date")) or (doc.get("status") == "Scheduled" and old.get("status") != "Scheduled"):
            if old:
                require_open_timeslot(old.get("weekly_timeslot"))
            require_open_timeslot(doc.get("weekly_timeslot"))
    elif doc.doctype == "Class Attendance Entry" and (not old or old.get("course_session") != doc.get("course_session")):
        slot = frappe.db.get_value("Course Sessions", doc.get("course_session"), "weekly_timeslot")
        require_open_timeslot(slot)


def open_enrollment_or_filters():
    """Keep legitimate termless non-term bookings; no per-enrollment lookup."""
    terms = frappe.get_all("Term", filters={"status": ["in", OPEN_STATUSES]}, pluck="name", limit_page_length=0)
    return [["term", "in", terms or ["__no_open_term__"]], ["term", "is", "not set"]]


@frappe.whitelist()
def family_enrollments(parent=None, student=None, customer=None, include_closed=0, start=0):
    from qas_custom.services.school_admin import (
        _require_school_admin, _resolve_family_context, _get_family_students, _get_enrollment_rows,
    )
    _require_school_admin()
    context = _resolve_family_context(parent=parent, student=student, customer=customer)
    if not any(context.get(key) for key in ("parent", "student", "customer")):
        frappe.throw(_("Family was not found."))
    students = [row["name"] for row in _get_family_students(context.get("parent"), context.get("student"))]
    if not students and not context.get("parent"):
        return {"items": [], "has_more": False}
    show_history = bool(cint(include_closed))
    rows = _get_enrollment_rows(
        parent=context.get("parent"), students=students,
        filters={} if show_history else {"status": ["in", ["Active", "Planned"]]},
        open_terms_only=not show_history, limit=51, start=max(0, cint(start)),
    )
    page = rows[:50]
    term_names = sorted({row.get("term") for row in page if row.get("term")})
    term_statuses = {row.name: row.status for row in frappe.get_all(
        "Term", filters={"name": ["in", term_names]}, fields=["name", "status"], limit_page_length=0,
    )} if term_names else {}
    for row in page:
        row["term_closed"] = term_statuses.get(row.get("term")) in CLOSED_STATUSES
    return {"items": page, "has_more": len(rows) > 50}
