from __future__ import annotations

import frappe

from qas_custom.modules.billing.store_credit import get_store_credit_balance


def get_parent_info_data():
    if frappe.session.user == "Guest":
        frappe.throw("Login required.", frappe.PermissionError)

    parent_name = frappe.db.get_value("Parent", {"linked_user": frappe.session.user}, "name")
    if not parent_name:
        frappe.throw("No parent record is linked to this account.", frappe.PermissionError)

    parent = frappe.get_cached_doc("Parent", parent_name)

    students = frappe.get_all(
        "Student",
        filters={"guardian": parent_name},
        fields=["name", "student_name", "age", "status"],
        order_by="student_name asc",
    )

    student_names = [student["name"] for student in students]
    enrollments_by_student: dict[str, list[dict]] = {student_name: [] for student_name in student_names}

    if student_names:
        enrollments = frappe.get_all(
            "Enrollment",
            filters={"student": ["in", student_names], "status": "Active"},
            fields=["name", "student", "course", "enrollment_type"],
            order_by="modified desc",
        )

        for enrollment in enrollments:
            enrollments_by_student.setdefault(enrollment["student"], []).append(
                {
                    "name": enrollment.get("name"),
                    "course": enrollment.get("course"),
                    "enrollment_type": enrollment.get("enrollment_type"),
                }
            )

    payload_students = []
    for student in students:
        payload_students.append(
            {
                "name": student.get("name"),
                "student_name": student.get("student_name"),
                "age": student.get("age") or 0,
                "status": student.get("status"),
                "enrollments": enrollments_by_student.get(student.get("name"), []),
            }
        )

    return {
        "parent_name": parent.get("parent_name"),
        "store_credit": float(get_store_credit_balance(parent=parent.name, customer=parent.get("customer")) or 0),
        "students": payload_students,
    }
