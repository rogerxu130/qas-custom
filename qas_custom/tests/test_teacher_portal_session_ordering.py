from unittest import TestCase

from qas_custom.services.teacher_portal import _teacher_session_sort_key


class TestTeacherPortalSessionOrdering(TestCase):
	def test_session_sort_key_orders_unpadded_start_times_chronologically(self):
		items = [
			{"id": "SESSION-1440", "session_date": "2026-08-01", "start_time": "14:40:00"},
			{"id": "SESSION-0900", "session_date": "2026-08-01", "start_time": "9:00:00"},
			{"id": "SESSION-1300", "session_date": "2026-08-01", "start_time": "13:00:00"},
			{"id": "SESSION-1040", "session_date": "2026-08-01", "start_time": "10:40:00"},
		]

		ordered = sorted(items, key=_teacher_session_sort_key)

		self.assertEqual(
			[item["id"] for item in ordered],
			["SESSION-0900", "SESSION-1040", "SESSION-1300", "SESSION-1440"],
		)

	def test_session_sort_key_places_missing_or_invalid_time_last(self):
		items = [
			{"id": "SESSION-MISSING", "session_date": "2026-08-01", "start_time": None},
			{"id": "SESSION-INVALID", "session_date": "2026-08-01", "start_time": "not-a-time"},
			{"id": "SESSION-VALID", "session_date": "2026-08-01", "start_time": "9:00"},
		]

		ordered = sorted(items, key=_teacher_session_sort_key)

		self.assertEqual(ordered[0]["id"], "SESSION-VALID")
		self.assertEqual({item["id"] for item in ordered[1:]}, {"SESSION-MISSING", "SESSION-INVALID"})
