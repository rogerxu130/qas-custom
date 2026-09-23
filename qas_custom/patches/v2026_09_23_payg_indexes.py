"""Create PAYG lookup and idempotency indexes after DocType sync."""

import frappe


INDEXES = (
    ("QAS PAYG Card", "idx_payg_card_lookup", ("family_parent", "course", "status", "expires_on"), False),
    ("QAS PAYG Booking", "idx_payg_booking_card", ("card", "status", "course_session"), False),
    ("QAS PAYG Booking", "idx_payg_booking_student", ("student", "course_session", "status"), False),
    ("QAS PAYG Entry", "idx_payg_entry_card_creation", ("card", "creation"), False),
    ("QAS PAYG Operation", "idx_payg_operation_type_request_unique", ("operation_type", "request_key"), True),
    ("QAS PAYG Operation", "idx_payg_issue_key_unique", ("issue_request_key",), True),
    ("QAS PAYG Operation", "idx_payg_invoice_key_unique", ("invoice_request_key",), True),
    ("QAS PAYG Booking", "idx_payg_booking_request_unique", ("request_key",), True),
    ("QAS PAYG Entry", "idx_payg_entry_operation_unique", ("operation_key",), True),
    ("QAS PAYG Product", "idx_payg_product_course_unique", ("course",), True),
)


def execute():
    for doctype, index_name, columns, unique in INDEXES:
        _ensure_index(doctype, index_name, columns, unique)


def _ensure_index(doctype, index_name, columns, unique):
    # Names and columns are constants from INDEXES, never request data.
    table = "tab" + doctype
    rows = frappe.db.sql(f"show index from `{table}`", as_dict=True)
    by_name = {}
    for row in rows:
        by_name.setdefault(row["Key_name"], []).append(row)
    for existing_name, parts in by_name.items():
        ordered = sorted(parts, key=lambda part: int(part["Seq_in_index"]))
        same_columns = tuple(part["Column_name"] for part in ordered) == columns
        same_unique = bool(ordered[0]["Non_unique"] == 0) == unique
        if existing_name == index_name:
            if not (same_columns and same_unique):
                frappe.throw(f"PAYG index {index_name} has an incompatible definition")
            return
        if same_columns and same_unique:
            return
    kind = "unique index" if unique else "index"
    quoted = ", ".join(f"`{column}`" for column in columns)
    frappe.db.sql(f"alter table `{table}` add {kind} `{index_name}` ({quoted})")
