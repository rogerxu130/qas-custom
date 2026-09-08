"""Read-only, full-population comparison of two Term rosters."""
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from qas_custom.services.school_admin_reporting import (
    _parent_map, _require_school_admin, _safe_fields, _student_map, _validate_term,
)

CLASS_FIELDS = ('course', 'enrollment_type', 'campus', 'day_of_week', 'start_time', 'end_time', 'class_language')


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
        fields=_safe_fields('Weekly Timeslot', ['name', 'campus', 'day_of_week', 'start_time', 'end_time', 'class_language', 'copied_from_weekly_timeslot']),
        limit_page_length=0)} if slot_ids else {}
    course_ids = sorted({row['course'] for row in enrollments if row.get('course')})
    courses = {row['name']: row.get('course_name') or row['name'] for row in frappe.get_all('Course',
        filters={'name': ['in', course_ids]}, fields=_safe_fields('Course', ['name', 'course_name']), limit_page_length=0)} if course_ids else {}
    for row in enrollments:
        slot = slots.get(row.get('weekly_timeslot'), {})
        row.update({key: str(value) if value is not None else '' for key, value in slot.items() if key != 'name'})
        row['course_label'] = courses.get(row.get('course'), row.get('course') or '')
    items = build_comparison(enrollments, source_term, target_term, statuses, '' if enrollment_type == 'all' else enrollment_type)
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
