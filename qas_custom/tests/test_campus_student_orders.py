from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
from qas_custom.api import campus_students as api
from qas_custom.services import campus_admin, order_invoices


class CampusStudentOrdersTests(TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(frappe, "throw", side_effect=ValueError).start()
        patch.object(api, "_", lambda value: value).start()
        self.row = {"student": "STUDENT", "student_name": "Amy", "term": "Term 4", "campus": "North", "name": "ENR"}
        self.manage = api.manage.__wrapped__

    def test_search_rejects_empty_short_and_wildcards_without_reading_students(self):
        with patch.object(frappe, "get_all") as get:
            for query in ("", " ", "a", "%", "__", "Amy%", "Am_", "**", "??", "Amy\\"):
                self.assertEqual(campus_admin._get_campus_admin_enrollment_search_students(query), [])
            get.assert_not_called()

    def test_search_uses_student_name_only(self):
        with patch.object(frappe, "get_all", return_value=[]) as get:
            campus_admin._get_campus_admin_enrollment_search_students("Amy")
        self.assertEqual(get.call_args.kwargs["or_filters"], [["Student", "student_name", "like", "%Amy%"]])

    def test_no_enrollment_denies_contact_access_before_loading_parent(self):
        with patch.object(api, "get_campus_admin_current_term_enrollments_data", return_value={"items": []}) as search, patch.object(frappe, "db", SimpleNamespace(get_value=Mock())) as db:
            with self.assertRaises(ValueError): self.manage("detail", {"student": "OTHER"})
            db.get_value.assert_not_called()
            search.assert_called_once_with(query="OTHER", student="OTHER")

    def test_detail_returns_only_contact_fields_and_permitted_enrollments(self):
        recipient = {"parent_name": "Parent", "email": "parent@example.com", "for_user": "PRIVATE"}
        with patch.object(api, "_student_context", return_value=([self.row], "PARENT")), patch.object(frappe, "db", SimpleNamespace(get_value=Mock(return_value="0412345678"))), patch("qas_custom.modules.notifications.makeup_parent_notifications._parent_recipient", return_value=recipient):
            result = self.manage("detail", {"student": "STUDENT"})
        self.assertEqual(result["enrollments"], [self.row])
        self.assertEqual(result["parent"], {"name": "PARENT", "parent_name": "Parent", "email": "parent@example.com", "phone": "0412345678"})

    def test_guardian_is_derived_from_exact_student_after_scoped_access(self):
        with patch.object(api, "get_campus_admin_current_term_enrollments_data", return_value={"items": [self.row]}), patch.object(frappe, "db", SimpleNamespace(get_value=Mock(return_value="PARENT"))) as db:
            rows, parent = api._student_context("STUDENT")
        self.assertEqual(parent, "PARENT")
        db.get_value.assert_called_once_with("Student", "STUDENT", "guardian")

    def test_cross_campus_order_is_rejected(self):
        with patch.object(api, "_student_context", return_value=([self.row], "PARENT")), patch("qas_custom.services.support_view.reject_support_view_write"), patch("qas_custom.services.material_orders._create_store_order") as create:
            with self.assertRaises(ValueError): self.manage("create", {"student": "STUDENT", "pickup_campus": "South", "creation_key": "key"})
            create.assert_not_called()

    def test_create_ignores_submitted_parent_price_discount_and_order(self):
        with patch.object(api, "_student_context", return_value=([self.row], "PARENT")), patch("qas_custom.services.support_view.reject_support_view_write"), patch("qas_custom.services.material_orders._get_parent", return_value="guardian") as get_parent, patch("qas_custom.services.material_orders._create_store_order", return_value={"name": "ORDER", "private_financial_data": 123}) as create:
            result = self.manage("create", {"student": "STUDENT", "parent": "OTHER", "pickup_campus": "North", "creation_key": "key", "items": [{"store_product": "KIT", "qty": 2}], "discount_value": 100, "order": "OTHER"})
        get_parent.assert_called_once_with("PARENT")
        create.assert_called_once_with("guardian", {"pickup_campus": "North", "creation_key": "key", "items": [{"store_product": "KIT", "qty": 2}]}, admin=True, student="STUDENT")
        self.assertEqual(result, {"name": "ORDER"})

    def test_create_requires_idempotency_key(self):
        with patch.object(api, "_student_context", return_value=([self.row], "PARENT")), patch("qas_custom.services.support_view.reject_support_view_write"):
            with self.assertRaises(ValueError): self.manage("create", {"student": "STUDENT", "pickup_campus": "North"})

    def test_invalid_actions_and_payloads_are_rejected(self):
        for action, payload in (("list", {}), ("export", {}), ("detail", []), ("detail", ["x"]), ("pay", {})):
            with self.subTest(action=action, payload=payload):
                with self.assertRaises(ValueError): self.manage(action, payload)

    def test_campus_family_picker_does_not_list_all_families(self):
        with patch.object(frappe, "db", SimpleNamespace(sql=Mock())), patch.object(frappe, "get_all") as get:
            for query in ("", " ", "%", "__", "a", "Am%"):
                self.assertEqual(order_invoices._families(["North"], query), [])
            get.assert_not_called()
            frappe.db.sql.assert_not_called()

    def test_campus_family_picker_uses_permitted_student_name_search(self):
        with patch.object(campus_admin, "get_campus_admin_current_term_enrollments_data", return_value={"items": [self.row]}) as search, patch.object(frappe, "get_all", side_effect=[["PARENT"], [{"name": "PARENT"}]]) as get:
            result = order_invoices._families(["North"], "Amy")
        search.assert_called_once_with(query="Amy")
        self.assertEqual(get.call_args_list[0].kwargs["filters"], {"name": ["in", ["STUDENT"]]})
        self.assertEqual(result, [{"name": "PARENT"}])

    def test_options_only_offer_active_permitted_campuses(self):
        with patch.object(api, "_student_context", return_value=([self.row], "PARENT")), patch.object(frappe, "get_all", side_effect=[[], ["North"]]) as get:
            result = self.manage("options", {"student": "STUDENT"})
        self.assertEqual(result, {"items": [], "campuses": ["North"]})
        self.assertEqual(get.call_args.kwargs["filters"], {"name": ["in", ["North"]], "status": "Active"})

    def test_exact_student_detail_still_filters_term_and_campus(self):
        timeslot = {"name": "TS", "campus": "North"}
        enrollment = {"name": "ENR", "student": "STUDENT", "weekly_timeslot": "TS"}
        with patch.object(campus_admin, "_require_campus_admin_profile", return_value={"campuses": ["North"]}), patch.object(campus_admin, "_get_campus_admin_current_active_term", return_value={"state": "ready", "term": "Term 4"}), patch.object(frappe, "get_all", side_effect=[[{"name": "STUDENT", "student_name": "Amy"}], [timeslot], [enrollment]]) as get, patch.object(campus_admin, "_get_campus_admin_enrollment_search_students") as search:
            result = campus_admin.get_campus_admin_current_term_enrollments_data(query="STUDENT", student="STUDENT")
        search.assert_not_called()
        self.assertEqual(get.call_args_list[0].kwargs["filters"], {"name": "STUDENT"})
        self.assertEqual(get.call_args_list[1].kwargs["filters"], {"term": "Term 4", "campus": ["in", ["North"]]})
        self.assertEqual(get.call_args_list[2].kwargs["filters"], {"term": "Term 4", "student": ["in", ["STUDENT"]], "weekly_timeslot": ["in", ["TS"]]})
        self.assertEqual(result["items"][0]["campus"], "North")

    def test_duplicate_submission_reuses_order_without_insert_or_notification(self):
        from qas_custom.services import material_orders
        key = "e0a84f82-a130-4d01-929d-49356235b194"
        existing = frappe._dict(name="ORDER", parent="PARENT", created_by="staff")
        with patch("qas_custom.services.support_view.reject_support_view_write"), patch.object(frappe, "session", SimpleNamespace(user="staff")), patch.object(frappe, "db", SimpleNamespace(get_value=Mock(return_value=existing))), patch.object(material_orders, "_get_order", return_value="existing"), patch.object(order_invoices, "order_payload", return_value={"name": "ORDER"}), patch.object(frappe, "get_doc") as insert, patch("qas_custom.modules.notifications.store_order_notifications.queue_new_order_admin_notification") as notify:
            result = material_orders._create_store_order(SimpleNamespace(name="PARENT"), {"creation_key": key}, admin=True, student="STUDENT")
        self.assertEqual(result, {"name": "ORDER"})
        insert.assert_not_called()
        notify.assert_not_called()

    def test_order_email_identifies_student_and_creator_with_html_escaping(self):
        from qas_custom.modules.notifications import store_order_notifications as mail
        doc = frappe._dict(name="ORDER", parent="PARENT", pickup_campus="North", created_by="staff@example.com", items=[frappe._dict(product_name="Kit", qty=2, unit_price=25, amount=50)], flags=frappe._dict(campus_order_student="Amy <Chen>"))
        with patch.object(frappe, "db", SimpleNamespace(get_value=Mock(return_value="Jane"))), patch.object(mail, "_parent_portal_url", side_effect=lambda path: "https://example.com" + path):
            content = mail.new_order_email_content(doc)
        self.assertIn("Amy &lt;Chen&gt;", content)
        self.assertIn("staff@example.com", content)
        self.assertIn("A$50.00", content)
        self.assertNotIn("Amy <Chen>", content)

    def test_missing_active_terms_is_an_explicit_family_search_error(self):
        with patch.object(campus_admin, "get_campus_admin_current_term_enrollments_data", return_value={"items": [], "state": "no_active_term", "message": "No active term is configured."}), patch.object(order_invoices, "_", lambda value: value), patch.object(frappe, "get_all") as get:
            with self.assertRaises(ValueError):
                order_invoices._families(["North"], "Isabella")
            frappe.throw.assert_called_once_with("No active term is configured.")
            get.assert_not_called()

    def test_student_detail_reports_all_permitted_active_terms(self):
        extra = {**self.row, "term": "Makeup Term", "name": "MAKEUP-ENR"}
        with patch.object(api, "_student_context", return_value=([self.row, extra], "PARENT")), patch.object(frappe, "db", SimpleNamespace(get_value=Mock(return_value="0412345678"))), patch("qas_custom.modules.notifications.makeup_parent_notifications._parent_recipient", return_value={"parent_name": "Parent", "email": "parent@example.com"}):
            result = self.manage("detail", {"student": "STUDENT"})
        self.assertEqual(result["term"], "Term 4, Makeup Term")
        self.assertEqual(result["enrollments"], [self.row, extra])
