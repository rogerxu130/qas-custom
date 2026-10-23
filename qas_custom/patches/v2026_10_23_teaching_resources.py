import frappe


def execute():
    names = ['Teaching Resource', 'Teaching Resource File', 'Teaching Resource Tag', 'Teaching Resource Tag Link', 'Teaching Resource Assignment', 'Teaching Resource Teacher Grant']
    for name in names:
        frappe.reload_doc('qas_custom', 'doctype', name.lower().replace(' ', '_'))
    for doctype, fields in [('Weekly Timeslot', ['teacher', 'term', 'campus', 'course']), ('Course Sessions', ['teacher_override', 'weekly_timeslot']), ('Teaching Resource Assignment', ['term', 'campus', 'course', 'resource']), ('Teaching Resource Teacher Grant', ['teacher', 'resource']), ('Teaching Resource File', ['resource', 'status'])]:
        frappe.db.add_index(doctype, fields)
    for doctype, fields in [('Teaching Resource Tag Link', ['resource', 'tag']), ('Teaching Resource Assignment', ['resource', 'term', 'campus', 'course']), ('Teaching Resource Teacher Grant', ['resource', 'teacher'])]:
        frappe.db.add_unique(doctype, fields)
