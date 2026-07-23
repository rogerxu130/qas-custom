import json
from pathlib import Path
from unittest import TestCase


DOCTYPE_ROOT = Path(__file__).resolve().parents[1] / "qas_custom" / "doctype"


class TestLeaveVoucherNaming(TestCase):
	def test_leave_request_uses_compact_year_series(self):
		definition = self._doctype_definition("leave_request")

		self.assertEqual(definition["autoname"], "LV-.YYYY.-.#####")
		self.assertNotIn("{student}", definition["autoname"])
		self.assertNotIn("{course_session}", definition["autoname"])

	def test_makeup_voucher_uses_compact_year_series(self):
		definition = self._doctype_definition("makeup_voucher")

		self.assertEqual(definition["autoname"], "MV-.YYYY.-.#####")
		self.assertNotIn("{student}", definition["autoname"])
		self.assertNotIn("{course}", definition["autoname"])

	def test_source_class_links_are_preserved(self):
		leave_request = self._doctype_definition("leave_request")
		makeup_voucher = self._doctype_definition("makeup_voucher")
		leave_fields = {field["fieldname"]: field for field in leave_request["fields"]}
		voucher_fields = {field["fieldname"]: field for field in makeup_voucher["fields"]}

		self.assertEqual(leave_fields["course_session"]["options"], "Course Sessions")
		self.assertEqual(leave_fields["weekly_timeslot"]["options"], "Weekly Timeslot")
		self.assertEqual(leave_fields["course"]["options"], "Course")
		self.assertEqual(leave_fields["session_date"]["fieldtype"], "Date")
		self.assertEqual(voucher_fields["original_session"]["options"], "Course Sessions")
		self.assertEqual(voucher_fields["leave_request"]["options"], "Leave Request")

	def _doctype_definition(self, doctype):
		path = DOCTYPE_ROOT / doctype / f"{doctype}.json"
		return json.loads(path.read_text())
