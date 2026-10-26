import frappe
from qas_custom.services import teaching_resources as service


@frappe.whitelist()
def bootstrap():
    service.require_user(True)
    from frappe.sessions import get_csrf_token
    return {'csrf_token': get_csrf_token(), 'options': service.options()}


@frappe.whitelist()
def admin_list(filters=None, offset=0):
    return service.catalog(filters, offset, True)


@frappe.whitelist()
def admin_detail(resource):
    return service.detail(resource, True)


@frappe.whitelist(methods=['POST'])
def manage(action, data=None):
    service.require_user(True)
    data = service.payload(data or {})
    if action == 'prepare_preview':
        from qas_custom.services.teaching_resource_previews import request
        return request(data.get('file'))
    if action == 'save':
        return service.save(data)
    if action in ('publish', 'archive', 'restore', 'delete'):
        return service.lifecycle(action, data.get('name'), data.get('modified'), data.get('confirmation', ''))
    if action == 'init_upload':
        return service.init_upload(data)
    if action == 'upload_state':
        return service.upload_state(data.get('file'), data.get('part'))
    if action == 'complete_upload':
        return service.complete_upload(data.get('file'))
    if action == 'cancel_upload':
        return service.cancel_upload(data.get('file'))
    if action == 'delete_files':
        return service.delete_files(data)
    if action == 'cleanup_metadata':
        return service.cleanup_metadata(data)
    if action == 'edit_files':
        return service.edit_files(data)
    frappe.throw('Unknown teaching resource action.')


@frappe.whitelist()
def admin_file(file):
    return service.file_link(file, True)


@frappe.whitelist()
def teacher_list(filters=None, offset=0):
    return service.catalog(filters, offset)


@frappe.whitelist()
def teacher_detail(resource):
    return service.detail(resource)


@frappe.whitelist()
def teacher_file(file):
    return service.file_link(file)


@frappe.whitelist()
def teacher_options():
    return service.teacher_options()


@frappe.whitelist()
def admin_preview(file):
    from qas_custom.services.teaching_resource_previews import status
    return status(file)
