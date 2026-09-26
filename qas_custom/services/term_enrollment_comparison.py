"""Read-only, full-population comparison of two Term rosters."""
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from qas_custom.services.school_admin_reporting import (
    _has_field, _parent_map, _require_school_admin, _safe_fields, _student_map, _teacher_name_map, _validate_term,
)

CLASS_FIELDS = ('course', 'enrollment_type', 'campus', 'day_of_week', 'start_time', 'end_time', 'class_language')


def _invoice_payment_category(invoice, linked_name=None):
    """Classify the actual invoice; Enrollment.invoice_status can be stale."""
    if not linked_name:
        return 'no_invoice'
    if not invoice:
        return 'missing_invoice'
    docstatus = cint(invoice.get('docstatus'))
    if docstatus == 2:
        return 'cancelled'
    if docstatus == 0:
        return 'draft'
    if docstatus != 1:
        return 'unknown'
    total = flt(invoice.get('grand_total'))
    outstanding = flt(invoice.get('outstanding_amount'))
    if total <= 0.01:
        return 'no_charge'
    if outstanding <= 0.01:
        return 'paid'
    if outstanding < total - 0.01:
        return 'partial'
    return 'outstanding'


def _attach_departure_context(items):
    """Fetch finance and marked attendance only for students missing from the target Term."""
    departed = [item for item in items if 'not_continuing' in item['tags']]
    source_rows = [row for item in departed for row in item['before']]
    if not source_rows:
        return
    enrollment_ids = [row['name'] for row in source_rows]
    linked_invoices = defaultdict(set)
    if _has_field('Sales Invoice Item', 'enrollment'):
        for invoice_item in frappe.get_all('Sales Invoice Item',
            filters={'enrollment': ['in', enrollment_ids]},
            fields=['parent', 'enrollment'], limit_page_length=0):
            if invoice_item.get('parent') and invoice_item.get('enrollment'):
                linked_invoices[invoice_item['enrollment']].add(invoice_item['parent'])
    invoice_ids = sorted({name for row in source_rows for name in
        ([row.get('invoice')] if row.get('invoice') else []) + list(linked_invoices[row['name']]) if name})
    invoices = {row['name']: dict(row) for row in frappe.get_all(
        'Sales Invoice', filters={'name': ['in', invoice_ids]},
        fields=_safe_fields('Sales Invoice', ['name', 'docstatus', 'status', 'grand_total', 'outstanding_amount', 'creation']),
        limit_page_length=0,
    )} if invoice_ids else {}
    attendance = defaultdict(lambda: {'present': 0, 'absent': 0, 'leave': 0, 'unmarked': 0})
    for entry in frappe.get_all('Class Attendance Entry',
        filters={'source_doctype': 'Enrollment', 'source_document': ['in', enrollment_ids]},
        fields=['source_document', 'status'], limit_page_length=0):
        status = entry.get('status')
        key = {'Present': 'present', 'Late': 'present', 'Absent': 'absent',
               'Leave': 'leave', 'To be started': 'unmarked'}.get(status)
        if key:
            attendance[entry['source_document']][key] += 1
    for item in departed:
        for row in item['before']:
            related = [invoices[name] for name in linked_invoices[row['name']] if name in invoices]
            related.sort(key=lambda invoice: str(invoice.get('creation') or ''), reverse=True)
            current = invoices.get(row.get('invoice'))
            invoice = current or next((candidate for candidate in related if cint(candidate.get('docstatus')) != 2), None) or (related[0] if related else None)
            row['review_invoice'] = (invoice or {}).get('name') or row.get('invoice') or ''
            row['related_invoices'] = [{'name': candidate['name'], 'docstatus': cint(candidate.get('docstatus')),
                                        'status': candidate.get('status') or ''} for candidate in related]
            row['payment_category'] = _invoice_payment_category(invoice, row['review_invoice'])
            row['invoice_actual_status'] = (invoice or {}).get('status') or ''
            row['invoice_total'] = flt((invoice or {}).get('grand_total')) if invoice else None
            row['invoice_outstanding'] = flt((invoice or {}).get('outstanding_amount')) if invoice else None
            row['attendance_summary'] = dict(attendance[row['name']])
        item['all_source_invoices_paid'] = all(row['payment_category'] == 'paid' for row in item['before'])


def _signature(row):
    if not row.get('weekly_timeslot') or not all(row.get(k) for k in ('course', 'campus', 'day_of_week', 'start_time')):
        return None
    return tuple(str(row.get(key) or '') for key in CLASS_FIELDS)


def _copied(a, b):
    return bool(a.get('weekly_timeslot') and b.get('weekly_timeslot') and a.get('enrollment_type') == b.get('enrollment_type') and (
        b.get('copied_from_weekly_timeslot') == a['weekly_timeslot']
        or a.get('copied_from_weekly_timeslot') == b['weekly_timeslot']
    ))


def compare_student(before, after):
    """Only pair reciprocal unique candidates. Never arbitrarily zip multi-class changes."""
    left, right = list(before), list(after)
    retained, changed = [], []
    review = any(_signature(row) is None for row in left + right)
    for rows in (left, right):
        keys = [(r.get('weekly_timeslot'), r.get('enrollment_type')) for r in rows if r.get('weekly_timeslot')]
        review = review or len(keys) != len(set(keys))

    def pair(a, b, basis):
        fields = [key for key in CLASS_FIELDS if str(a.get(key) or '') != str(b.get(key) or '')]
        item = {'before': a, 'after': b, 'basis': basis, 'changed_fields': fields}
        (changed if fields else retained).append(item)
        left.remove(a)
        right.remove(b)

    for basis, match in (
        ('copied_class', _copied),
        ('same_class_details', lambda a, b: _signature(a) is not None and _signature(a) == _signature(b)),
        ('same_course', lambda a, b: _signature(a) is not None and _signature(b) is not None and a.get('course') == b.get('course') and a.get('enrollment_type') == b.get('enrollment_type')),
    ):
        for a in list(left):
            candidates = [b for b in right if match(a, b)]
            if len(candidates) == 1 and sum(match(x, candidates[0]) for x in left) == 1:
                pair(a, candidates[0], basis)
    if len(left) == len(right) == 1 and not review:
        pair(left[0], right[0], 'single_remaining_pair')
    if left and right:
        review = True
    tags = []
    if before and not after:
        tags.append('not_continuing')
    elif after and not before:
        tags.append('new_to_term')
    else:
        if len(after) > len(before):
            tags.append('increased')
        if len(after) < len(before):
            tags.append('decreased')
        if changed:
            tags.append('changed')
        if not left and not right and not changed:
            tags.append('unchanged')
    if review:
        tags.append('needs_review')
    return {'before': before, 'after': after, 'before_count': len(before), 'after_count': len(after),
            'retained': retained, 'changed': changed, 'added': right, 'removed': left, 'tags': tags}


def build_comparison(enrollments, source_term, target_term, statuses, enrollment_type='Full-Term'):
    students = defaultdict(lambda: {'before': [], 'after': [], 'excluded_before': [], 'excluded_after': []})
    for enrollment in enrollments:
        if enrollment.get('term') not in (source_term, target_term):
            continue
        if enrollment_type and enrollment.get('enrollment_type') != enrollment_type:
            continue
        student = enrollment.get('student')
        if not student:
            continue
        side = 'before' if enrollment['term'] == source_term else 'after'
        if enrollment.get('status') not in statuses:
            side = 'excluded_' + side
        students[student][side].append(enrollment)
    result = []
    for student, sides in students.items():
        if not sides['before'] and not sides['after']:
            continue
        result.append({'student': student, **compare_student(sides['before'], sides['after']),
                       'excluded_before': sides['excluded_before'], 'excluded_after': sides['excluded_after']})
    return result


def get_term_enrollment_comparison(source_term=None, target_term=None, include_planned=1, include_inactive=0, enrollment_type='Full-Term'):
    _require_school_admin()
    _validate_term(source_term)
    _validate_term(target_term)
    if source_term == target_term:
        frappe.throw(_('Choose two different Terms.'))
    if enrollment_type not in ('Full-Term', 'all'):
        frappe.throw(_('Choose Full-Term or all enrollment types.'))
    statuses = ['Active', 'Completed']
    if cint(include_planned):
        statuses.append('Planned')
    if cint(include_inactive):
        statuses.append('Inactive')
    enrollments = [dict(row) for row in frappe.get_all('Enrollment',
        filters={'term': ['in', [source_term, target_term]]},
        fields=['name', 'student', 'parent', 'term', 'course', 'weekly_timeslot', 'enrollment_type', 'status', 'invoice', 'invoice_status'],
        order_by='student asc, name asc', limit_page_length=0)]
    slot_ids = sorted({row['weekly_timeslot'] for row in enrollments if row.get('weekly_timeslot')})
    slots = {row['name']: dict(row) for row in frappe.get_all('Weekly Timeslot',
        filters={'name': ['in', slot_ids]},
        fields=_safe_fields('Weekly Timeslot', ['name', 'campus', 'day_of_week', 'start_time', 'end_time', 'class_language', 'teacher', 'copied_from_weekly_timeslot']),
        limit_page_length=0)} if slot_ids else {}
    teacher_names = _teacher_name_map({row.get('teacher') for row in slots.values() if row.get('teacher')})
    course_ids = sorted({row['course'] for row in enrollments if row.get('course')})
    courses = {row['name']: row.get('course_name') or row['name'] for row in frappe.get_all('Course',
        filters={'name': ['in', course_ids]}, fields=_safe_fields('Course', ['name', 'course_name']), limit_page_length=0)} if course_ids else {}
    for row in enrollments:
        slot = slots.get(row.get('weekly_timeslot'), {})
        row.update({key: str(value) if value is not None else '' for key, value in slot.items() if key != 'name'})
        row['course_label'] = courses.get(row.get('course'), row.get('course') or '')
        row['teacher_name'] = teacher_names.get(row.get('teacher'), row.get('teacher') or '')
    items = build_comparison(enrollments, source_term, target_term, statuses, '' if enrollment_type == 'all' else enrollment_type)
    _attach_departure_context(items)
    student_map = _student_map([row['student'] for row in items])
    parent_ids = {row.get('parent') for row in enrollments if row.get('parent')}
    parent_ids.update(s.get('guardian') or s.get('parent') for s in student_map.values())
    parents = _parent_map(sorted(p for p in parent_ids if p))
    for item in items:
        student = student_map.get(item['student'], {})
        item['student_name'] = student.get('student_name') or item['student']
        ids = {r.get('parent') for side in ('before', 'after', 'excluded_before', 'excluded_after') for r in item[side] if r.get('parent')}
        guardian = student.get('guardian') or student.get('parent')
        if guardian:
            ids.add(guardian)
        item['parents'] = [parents.get(key, {'name': key, 'parent_name': key}) for key in sorted(ids)]
        item['campuses'] = sorted({r.get('campus') for r in item['before'] + item['after'] if r.get('campus')})
    items.sort(key=lambda row: (row['student_name'].casefold(), row['student']))
    summary = {'source_students': sum(bool(r['before']) for r in items), 'target_students': sum(bool(r['after']) for r in items)}
    for tag in ('not_continuing', 'new_to_term', 'increased', 'decreased', 'changed', 'unchanged', 'needs_review'):
        summary[tag] = sum(tag in r['tags'] for r in items)
    return {'source_term': source_term, 'target_term': target_term, 'statuses': statuses,
            'enrollment_type': enrollment_type, 'generated_at': str(now_datetime()),
            'summary': summary, 'items': items,
            'missing_student_records': sum(not r.get('student') for r in enrollments)}
