import frappe


COURSE_CHINESE_NAMES = {
	"Anime art - Beginner": "动漫绘画 - 初级",
	"Anime Art - Intermediate": "动漫绘画 - 中级",
	"makeup cource": "补课课程",
}


def execute():
	if not frappe.db.has_column("Course", "course_name_zh"):
		return

	for course, chinese_name in COURSE_CHINESE_NAMES.items():
		if frappe.db.exists("Course", course):
			frappe.db.set_value("Course", course, "course_name_zh", chinese_name, update_modified=False)
