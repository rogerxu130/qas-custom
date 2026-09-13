from frappe.model.document import Document


class StoreOrder(Document):
	def after_insert(self):
		from qas_custom.modules.notifications.store_order_notifications import queue_new_order_admin_notification
		queue_new_order_admin_notification(self)
