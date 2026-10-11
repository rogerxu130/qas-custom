import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services import activity


class TestSchoolAdminActivity(TestCase):
    def setUp(self):
        self.meta = NS(track_changes=True, get_field=lambda name: {
            "status": NS(label="Status", fieldtype="Select"),
            "password": NS(label="Password", fieldtype="Password"),
            "items": NS(label="Items", fieldtype="Table", options="Invoice Item"),
        }.get(name))
        self.doc = NS(meta=self.meta, owner="admin@example.com", modified_by="admin@example.com",
            creation="2026-01-01 10:00:00", modified="2026-01-01 10:00:00")
        self.guard = patch("qas_custom.services.school_admin._require_school_admin")
        self.guard_mock = self.guard.start()
        self.addCleanup(self.guard.stop)
        self.fake = NS(get_doc=Mock(return_value=self.doc), get_all=Mock(), ValidationError=ValueError,
            throw=Mock(side_effect=lambda message, exception: (_ for _ in ()).throw(exception(message))))

    def fetch(self, versions=(), comments=(), **kwargs):
        self.fake.get_all.side_effect = lambda source, **kw: {
            "Version": list(versions)[:kw["limit_page_length"]],
            "Comment": list(comments)[:kw["limit_page_length"]],
            "User": [NS(name="admin@example.com", full_name="School Admin")],
        }[source]
        with patch.object(activity, "frappe", self.fake):
            return activity.get_activity("Enrollment", "ENR-1", **kwargs)

    def test_role_guard_runs_before_reading_records(self):
        self.guard_mock.side_effect = PermissionError("Denied")
        with self.assertRaises(PermissionError), patch.object(activity, "frappe", self.fake):
            activity.get_activity("Enrollment", "ENR-1")
        self.fake.get_doc.assert_not_called()
        self.fake.get_all.assert_not_called()

    def test_disallowed_record_types_and_offsets_never_query(self):
        with patch.object(activity, "frappe", self.fake):
            for doctype, name, offset in [("User", "admin", 0), ("Parent", "", 0), ("Parent", "P-1", -1), ("Parent", "P-1", 10001)]:
                with self.assertRaises(ValueError):
                    activity.get_activity(doctype, name, offset)
        self.fake.get_doc.assert_not_called()

    def test_version_field_labels_and_password_redaction(self):
        changes = activity._changes({"changed": [["status", "Pending", "Paid"], ["password", "secret", "new-secret"], ["missing", "a", "b"]], "added": [["items", {}]]}, self.meta)
        self.assertEqual(changes, [{"label": "Status", "before": "Pending", "after": "Paid"}, {"label": "Items", "before": None, "after": "Row added"}])

    def test_activity_is_merged_newest_first_with_plain_text(self):
        version = NS(name="V-1", creation="2026-03-02 10:00:00", owner="admin@example.com", data=json.dumps({"changed": [["status", "Pending", "Paid"]]}))
        comment = NS(name="C-1", creation="2026-03-03 10:00:00", owner="admin@example.com", comment_by=None, comment_type="Info", content="<b>Paid</b> &amp; confirmed")
        result = self.fetch([version], [comment])
        self.assertEqual([row["id"] for row in result["items"]], ["comment:C-1", "version:V-1", "created:ENR-1"])
        self.assertEqual(result["items"][0]["message"], "Paid & confirmed")
        self.assertEqual(result["items"][0]["actor"], "School Admin")
        self.assertFalse(result["has_more"])
        self.assertTrue(result["track_changes"])
        calls = self.fake.get_all.call_args_list
        self.assertEqual(calls[0].kwargs["filters"], {"ref_doctype": "Enrollment", "docname": "ENR-1"})
        self.assertEqual(calls[1].kwargs["filters"], {"reference_doctype": "Enrollment", "reference_name": "ENR-1"})

    def test_merged_pagination_has_no_missing_or_duplicate_rows(self):
        versions = [NS(name=f"V-{i:02}", creation=f"2026-03-{30-i:02} 10:00:00", owner="admin@example.com", data="{}") for i in range(25)]
        comments = [NS(name=f"C-{i:02}", creation=f"2026-03-{30-i:02} 11:00:00", owner="admin@example.com", comment_by=None, comment_type="Info", content="Updated") for i in range(25)]
        pages = [self.fetch(versions, comments, offset=i) for i in [0, 20, 40]]
        ids = [item["id"] for page in pages for item in page["items"]]
        self.assertEqual(len(ids), 51)
        self.assertEqual(len(set(ids)), 51)
        self.assertEqual([page["has_more"] for page in pages], [True, True, False])

    def test_last_edit_is_not_duplicated_when_version_exists(self):
        self.doc.modified = "2026-03-02 10:00:00"
        version = NS(name="V-1", creation=self.doc.modified, owner="admin@example.com", data="{}")
        self.assertNotIn("edited:ENR-1", [row["id"] for row in self.fetch([version])["items"]])
        self.assertIn("edited:ENR-1", [row["id"] for row in self.fetch()["items"]])

    def test_child_row_changes_show_values_without_passwords(self):
        with patch.object(activity.frappe, "get_meta", return_value=self.meta):
            changes = activity._changes({"row_changed": [["items", 0, "ITEM-1", [["status", "Pending", "Paid"], ["password", "secret", "new"]]]]}, self.meta)
        self.assertEqual(changes, [{"label": "Items · row 1 · Status", "before": "Pending", "after": "Paid"}])
