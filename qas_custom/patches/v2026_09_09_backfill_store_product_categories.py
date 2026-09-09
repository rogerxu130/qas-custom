import frappe


def execute():
	for row in frappe.get_all("Store Product", filters={"primary_category": ["is", "set"]}, fields=["name", "primary_category"], limit_page_length=0):
		doc = frappe.get_doc("Store Product", row.name)
		if not doc.get("categories"):
			doc.append("categories", {"category": row.primary_category})
			doc.save(ignore_permissions=True)
