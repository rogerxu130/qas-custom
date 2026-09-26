import frappe
from werkzeug.utils import send_file

from qas_custom.services import term_media as service


@frappe.whitelist()
def overview(term):
    return service.overview(term)


@frappe.whitelist(methods=["POST"])
def start_archive(term, media_type="all"):
    return service.start_archive(term, media_type)


@frappe.whitelist()
def get_archive(archive):
    return service.get_archive(archive)


@frappe.whitelist(methods=["POST"])
def confirm_backup(archive):
    return service.confirm_backup(archive)


@frappe.whitelist()
def preview_cleanup(archive, media_type="all"):
    return service.preview_cleanup(archive, media_type)


@frappe.whitelist(methods=["POST"])
def start_cleanup(archive, media_type="all", confirmation=""):
    return service.start_cleanup(archive, media_type, confirmation)


@frappe.whitelist()
def download_part(archive, part):
    path = service.download_path(archive, part)
    response = send_file(path, environ=frappe.local.request.environ, as_attachment=True,
                         download_name=f"term-media-{archive}-{path.name}", mimetype="application/zip", conditional=True)
    response.headers["Cache-Control"] = "private, no-store"
    return response
