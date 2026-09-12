import frappe
from frappe import _
from frappe.model.document import Document


class StoreProduct(Document):
	def validate(self):
		# Keep the legacy API field aligned when categories are edited in Desk.
		self.primary_category = self.categories[0].category if self.get("categories") else None

	def on_trash(self):
		if frappe.db.exists("Store Order Item", {"store_product": self.name}):
			frappe.throw(_("This product has been used in an order and cannot be deleted. Turn off Available to order instead."))

		from qas_custom.services.material_orders import preserve_shared_product_files
		preserve_shared_product_files(self.name)
