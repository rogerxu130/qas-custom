from frappe.model.document import Document


class StoreProduct(Document):
	def validate(self):
		# Keep the legacy API field aligned when categories are edited in Desk.
		self.primary_category = self.categories[0].category if self.get("categories") else None
