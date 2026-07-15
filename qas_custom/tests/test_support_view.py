from unittest import TestCase
from unittest.mock import patch

from qas_custom.services.support_view import _support_view_portal_url


class TestSupportView(TestCase):
	def test_parent_and_campus_views_use_the_configured_parent_portal_url(self):
		with patch("qas_custom.services.support_view.frappe.conf", {"qas_parent_portal_url": "https://staging-portal.example.com/"}):
			self.assertEqual(_support_view_portal_url("Parent"), "https://staging-portal.example.com")
			self.assertEqual(_support_view_portal_url("Campus Admin"), "https://staging-portal.example.com")

	def test_teacher_view_uses_the_configured_teacher_portal_url(self):
		with patch("qas_custom.services.support_view.frappe.conf", {"teacher_portal_base_url": "https://staging-teacher.example.com/"}):
			self.assertEqual(_support_view_portal_url("Teacher"), "https://staging-teacher.example.com")
