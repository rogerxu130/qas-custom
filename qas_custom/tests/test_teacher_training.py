from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services.teacher_training import publish_school_admin_training_article_data


class TestTeacherTrainingPublish(TestCase):
	def test_publish_uses_current_editor_payload_without_prior_draft_save(self):
		article = SimpleNamespace(name="TTA-0001", status="Draft", published_at=None, save=Mock())
		fake_db = SimpleNamespace(savepoint=Mock(), commit=Mock(), rollback=Mock())
		fake_frappe = SimpleNamespace(
			db=fake_db,
			session=SimpleNamespace(user="school-admin@example.com"),
			new_doc=Mock(return_value=article),
		)
		current_payload = {"title": "Current title", "content": "<p>Current content</p>"}

		with patch("qas_custom.services.teacher_training.frappe", fake_frappe), patch(
			"qas_custom.services.teacher_training._require_school_admin"
		), patch(
			"qas_custom.services.teacher_training._apply_article"
		) as apply_payload, patch(
			"qas_custom.services.teacher_training._validate_article"
		), patch(
			"qas_custom.services.teacher_training.now_datetime", return_value="2026-08-03 12:00:00"
		), patch(
			"qas_custom.services.teacher_training._article_payload",
			return_value={"name": "TTA-0001", "status": "Published"},
		):
			result = publish_school_admin_training_article_data(payload=current_payload)

		apply_payload.assert_called_once_with(article, current_payload)
		article.save.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(article.status, "Published")
		self.assertEqual(result["status"], "Published")
