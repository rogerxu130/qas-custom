"""Teaching resource catalog, transactional grants and resumable direct uploads."""
import json
import uuid
from datetime import datetime

import frappe
from frappe.utils import cint, get_datetime, now_datetime, add_days

from qas_custom.services import teaching_resource_storage as storage
from qas_custom.services.teaching_resource_rules import MAX_FILES, PART_SIZE, ZONE, link_lifetime, part_count, validate_file, validate_parts

RESOURCE = 'Teaching Resource'
FILE = 'Teaching Resource File'
ASSIGN = 'Teaching Resource Assignment'
GRANT = 'Teaching Resource Teacher Grant'
TAG = 'Teaching Resource Tag'
TAG_LINK = 'Teaching Resource Tag Link'


def enabled():
    return bool(cint(frappe.conf.get('teaching_resources_enabled')))


def require_user(admin=False):
    if not enabled():
        frappe.throw('Teaching resources are not enabled on this site.')
    if frappe.request and frappe.request.headers.get('X-QAS-Support-View-Token'):
        frappe.throw('Teaching resources are unavailable in support view.', frappe.PermissionError)
    user = frappe.session.user
    if user == 'Guest' or not frappe.db.get_value('User', user, 'enabled'):
        frappe.throw('An active login is required.', frappe.PermissionError)
    if admin and not set(frappe.get_roles(user)).intersection({'School Admin', 'System Manager'}):
        frappe.throw('School Admin access is required.', frappe.PermissionError)


def teacher_name():
    from qas_custom.services.teacher_portal import _require_teacher
    require_user()
    return _require_teacher().name


def payload(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            frappe.throw('Invalid request data.')
    if not isinstance(value, dict):
        frappe.throw('Request data must be an object.')
    return value


def text(value, maximum, required=False):
    value = str(value or '').strip()
    if (required and not value) or len(value) > maximum:
        frappe.throw(f'Enter text between {1 if required else 0} and {maximum} characters.')
    return value


def lock_resource(name, modified=None):
    rows = frappe.db.sql('SELECT name FROM `tabTeaching Resource` WHERE name=%s FOR UPDATE', (name,))
    if not rows:
        frappe.throw('Teaching resource not found.', frappe.DoesNotExistError)
    doc = frappe.get_doc(RESOURCE, name)
    if modified is not None and str(doc.modified) != str(modified):
        frappe.throw('This resource changed. Reload before editing.', frappe.TimestampMismatchError)
    return doc


def require_editable(doc):
    if doc.status in ('Deleting', 'Deleted'):
        frappe.throw('Deletion is in progress. Retry deletion before making other changes.')


def audit(doc, message):
    doc.add_comment('Info', message)


def scope_sql(teacher, params):
    params.update(teacher=teacher, today=datetime.now(ZONE).date())
    return '''a.term IN (SELECT name FROM `tabTerm` WHERE status='Active' AND end_date >= %(today)s)
    AND EXISTS (SELECT 1 FROM `tabCampus` c WHERE c.name=a.campus AND c.status='Active')
    AND EXISTS (SELECT 1 FROM `tabCourse` c WHERE c.name=a.course AND c.status='Active')
    AND EXISTS (SELECT 1 FROM `tabWeekly Timeslot` w WHERE w.term=a.term AND w.campus=a.campus AND w.course=a.course
      AND (w.teacher=%(teacher)s OR EXISTS (SELECT 1 FROM `tabCourse Sessions` cs WHERE cs.weekly_timeslot=w.name AND cs.teacher_override=%(teacher)s AND cs.status <> 'Cancelled')))'''


def resource_access_sql(teacher, params):
    scope = scope_sql(teacher, params)
    return f'''r.status='Published' AND (
      EXISTS (SELECT 1 FROM `tabTeaching Resource Teacher Grant` g WHERE g.resource=r.name AND g.teacher=%(teacher)s)
      OR EXISTS (SELECT 1 FROM `tabTeaching Resource Assignment` a WHERE a.resource=r.name AND {scope}))'''


def teacher_access(name, teacher):
    params = {'resource': name}
    scope = scope_sql(teacher, params)
    if frappe.db.get_value(RESOURCE, name, 'status') != 'Published':
        frappe.throw('This resource is unavailable.', frappe.PermissionError)
    direct = bool(frappe.db.exists(GRANT, {'resource': name, 'teacher': teacher}))
    assignments = frappe.db.sql(f'''SELECT a.name,a.term,a.campus,a.course,a.note,t.end_date
        FROM `tabTeaching Resource Assignment` a JOIN `tabTerm` t ON t.name=a.term
        WHERE a.resource=%(resource)s AND {scope} ORDER BY a.term,a.campus,a.course''', params, as_dict=True)
    lifetime = link_lifetime([r.end_date for r in assignments], direct=direct)
    if not lifetime:
        frappe.throw('You no longer have access to this resource.', frappe.PermissionError)
    return assignments, lifetime


def catalog(filters=None, offset=0, admin=False):
    require_user(admin)
    filters = payload(filters or {})
    params = {'offset': max(0, cint(offset))}
    conditions = ["r.status <> 'Deleted'"]
    if not admin:
        conditions.append(resource_access_sql(teacher_name(), params))
    elif filters.get('status'):
        conditions.append('r.status=%(status)s')
        params['status'] = filters['status']
    if filters.get('query'):
        conditions.append('r.title LIKE %(query)s')
        params['query'] = '%' + text(filters['query'], 200) + '%'
    if admin and filters.get('tag'):
        conditions.append('EXISTS (SELECT 1 FROM `tabTeaching Resource Tag Link` l JOIN `tabTeaching Resource Tag` t ON t.name=l.tag WHERE l.resource=r.name AND t.tag=%(tag)s)')
        params['tag'] = text(filters['tag'], 100).casefold()
    selected = []
    for key in ('term', 'campus', 'course'):
        if filters.get(key):
            selected.append(f'a.{key}=%({key})s')
            params[key] = filters[key]
    if selected:
        scope = '' if admin else ' AND ' + scope_sql(params['teacher'], params)
        conditions.append('EXISTS (SELECT 1 FROM `tabTeaching Resource Assignment` a WHERE a.resource=r.name AND ' + ' AND '.join(selected) + scope + ')')
    rows = frappe.db.sql('SELECT r.name,r.title,r.summary,r.status,r.modified FROM `tabTeaching Resource` r WHERE ' + ' AND '.join(conditions) + ' ORDER BY r.modified DESC,r.name LIMIT 31 OFFSET %(offset)s', params, as_dict=True)
    return {'items': rows[:30], 'has_more': len(rows) > 30}


def detail(name, admin=False):
    require_user(admin)
    allowed, _ = ([], 0) if admin else teacher_access(name, teacher_name())
    doc = frappe.get_doc(RESOURCE, name)
    result = {key: doc.get(key) for key in ('name', 'title', 'summary', 'status', 'modified')}
    file_filters = {'resource': name, 'status': ['in', ['Uploading', 'Ready']] if admin else 'Ready'}
    if not admin:
        file_filters['is_visible'] = 1
    result['files'] = frappe.get_all(FILE, filters=file_filters, fields=['name', 'filename', 'label', 'kind', 'size_bytes', 'status', 'sort_order', 'last_modified', 'replaces', 'is_visible'], order_by='sort_order asc,creation asc', limit_page_length=0)
    result['assignments'] = frappe.get_all(ASSIGN, filters={'resource': name}, fields=['name', 'term', 'campus', 'course', 'note'], limit_page_length=0) if admin else allowed
    # Older teacher pages expect this key; never send their internal tag values.
    result['tags'] = []
    if admin:
        result['admin_note'] = doc.get('admin_note') or ''
        result['tags'] = [r.tag for r in frappe.db.sql('SELECT t.tag FROM `tabTeaching Resource Tag Link` l JOIN `tabTeaching Resource Tag` t ON t.name=l.tag WHERE l.resource=%s ORDER BY t.tag', (name,), as_dict=True)]
        result['teachers'] = frappe.get_all(GRANT, filters={'resource': name}, pluck='teacher', limit_page_length=0)
    return result


def save(data):
    require_user(True)
    data = payload(data)
    if data.get('name'):
        if not data.get('modified'):
            frappe.throw('Reload this resource before saving.')
        doc = lock_resource(data['name'], data['modified'])
        require_editable(doc)
    else:
        doc = frappe.new_doc(RESOURCE)
        doc.status = 'Draft'
    doc.title = text(data.get('title'), 200, True)
    doc.summary = text(data.get('summary'), 2000)
    if 'admin_note' in data:
        doc.admin_note = text(data['admin_note'], 2000)
    assignments = data.get('assignments', [])
    teachers = data.get('teachers', [])
    tags = data.get('tags', [])
    if not all(isinstance(v, list) for v in (assignments, teachers, tags)) or max(len(assignments), len(teachers), len(tags)) > 100:
        frappe.throw('Use lists with at most 100 assignments, teachers or tags.')
    unique = set()
    for row in assignments:
        if not isinstance(row, dict):
            frappe.throw('Invalid assignment data.')
        for key, doctype in (('term', 'Term'), ('campus', 'Campus'), ('course', 'Course')):
            if not frappe.db.exists(doctype, row.get(key)):
                frappe.throw(f'Select a valid {key}.')
        combination = tuple(row[k] for k in ('term', 'campus', 'course'))
        if combination in unique:
            frappe.throw('Each term, campus and course combination can be assigned once.')
        unique.add(combination)
        row['note'] = text(row.get('note'), 2000)
    for teacher in teachers:
        if not isinstance(teacher, str) or not frappe.db.exists('Teacher', teacher):
            frappe.throw('Select a valid teacher.')
    if any(not isinstance(tag, str) for tag in tags):
        frappe.throw('Tags must be text.')
    clean_tags = sorted({text(t, 100, True).casefold() for t in tags})
    doc.save(ignore_permissions=True)
    for doctype in (ASSIGN, GRANT, TAG_LINK):
        for child in frappe.get_all(doctype, filters={'resource': doc.name}, pluck='name', limit_page_length=0):
            frappe.delete_doc(doctype, child, ignore_permissions=True)
    for row in assignments:
        frappe.get_doc(dict(doctype=ASSIGN, resource=doc.name, **{k: row[k] for k in ('term', 'campus', 'course', 'note')})).insert(ignore_permissions=True)
    for teacher in set(teachers):
        frappe.get_doc(dict(doctype=GRANT, resource=doc.name, teacher=teacher)).insert(ignore_permissions=True)
    for tag in clean_tags:
        tag_id = frappe.db.get_value(TAG, {'tag': tag}, 'name')
        if not tag_id:
            tag_id = frappe.get_doc(dict(doctype=TAG, tag=tag)).insert(ignore_permissions=True).name
        frappe.get_doc(dict(doctype=TAG_LINK, resource=doc.name, tag=tag_id)).insert(ignore_permissions=True)
    audit(doc, 'Resource details, teaching assignments and teacher grants saved.')
    return detail(doc.name, True)


def lifecycle(action, name, modified, confirmation=''):
    require_user(True)
    if not modified:
        frappe.throw('Reload this resource before changing its status.')
    doc = lock_resource(name, modified)
    if action == 'delete':
        if doc.status not in ('Archived', 'Deleting') or confirmation != doc.title:
            frappe.throw('Archive first, then type the resource title to permanently delete its files.')
        doc.status = 'Deleting'
        doc.save(ignore_permissions=True)
        audit(doc, 'Permanent deletion requested.')
        frappe.db.commit()
        cleanup_resource(name)
        return {'deleted': frappe.db.get_value(RESOURCE, name, 'status') == 'Deleted'}
    require_editable(doc)
    if action == 'publish':
        if not frappe.db.exists(FILE, {'resource': name, 'status': 'Ready'}) or frappe.db.exists(FILE, {'resource': name, 'status': 'Uploading'}):
            frappe.throw('Finish or cancel uploads, and keep at least one ready file before publishing.')
        doc.status = 'Published'
        frappe.db.sql("UPDATE `tabTeaching Resource File` SET is_visible=1 WHERE resource=%s AND status='Ready'", (name,))
    elif action == 'archive':
        doc.status = 'Archived'
    elif action == 'restore' and doc.status == 'Archived':
        doc.status = 'Draft'
    else:
        frappe.throw('Invalid resource action.')
    doc.save(ignore_permissions=True)
    audit(doc, f'Resource {action}.')
    return detail(name, True)


def options():
    require_user(True)
    result = {}
    for key, doctype, label in (('terms', 'Term', 'term_name'), ('campuses', 'Campus', 'campus_name'), ('courses', 'Course', 'course_name'), ('teachers', 'Teacher', 'teacher_name')):
        meta = frappe.get_meta(doctype)
        label = label if meta.has_field(label) else 'name'
        result[key] = frappe.get_all(doctype, fields=list(dict.fromkeys(['name', label] + (['status'] if meta.has_field('status') else []))), limit_page_length=0)
    result['tags'] = frappe.get_all(TAG, pluck='tag', order_by='tag', limit_page_length=0)
    return result


def file_lock(name):
    resource = frappe.db.get_value(FILE, name, 'resource')
    if not resource:
        frappe.throw('File not found.')
    doc = lock_resource(resource)
    require_editable(doc)
    frappe.db.sql('SELECT name FROM `tabTeaching Resource File` WHERE name=%s FOR UPDATE', (name,))
    return doc, frappe.get_doc(FILE, name)


def init_upload(data):
    require_user(True)
    data = payload(data)
    doc = lock_resource(data.get('resource'))
    require_editable(doc)
    try:
        size, kind, mime = validate_file(data.get('filename'), data.get('size'))
        uuid.UUID(str(data.get('request_id')))
    except ValueError as exc:
        frappe.throw(str(exc))
    existing = frappe.db.get_value(FILE, {'request_id': data['request_id']}, 'name')
    if existing:
        file = frappe.get_doc(FILE, existing)
        if file.resource != doc.name or file.filename != data['filename'] or int(file.size_bytes) != size:
            frappe.throw('This upload identifier belongs to a different file.')
        if file.status in ('Cancelled', 'Retired', 'Deleting'):
            frappe.throw('Start a new upload for this file.')
        return {'file': file.name, 'status': file.status, 'part_size': PART_SIZE}
    replaces = data.get('replaces')
    if replaces and not frappe.db.exists(FILE, {'name': replaces, 'resource': doc.name, 'status': 'Ready', 'kind': kind}):
        frappe.throw('Select a ready file of the same type to replace.')
    if replaces and frappe.db.exists(FILE, {'replaces': replaces, 'status': 'Uploading'}):
        frappe.throw('Resume or cancel the pending replacement before replacing this file again.')
    active_files = frappe.db.count(FILE, {'resource': doc.name, 'status': ['in', ['Ready', 'Uploading']]})
    if active_files - bool(replaces) >= MAX_FILES:
        frappe.throw('A resource can contain at most 100 files.')
    file = frappe.get_doc(dict(doctype=FILE, resource=doc.name, filename=data['filename'], label=data['filename'], size_bytes=size, kind=kind, mime=mime, object_key=f'teaching/{doc.name}/{uuid.uuid4().hex}', request_id=data['request_id'], last_modified=str(data.get('last_modified') or ''), replaces=replaces, status='Uploading', sort_order=frappe.db.count(FILE, {'resource': doc.name})))
    file.insert(ignore_permissions=True)
    # Persist identity before contacting storage, so failed transactions never lose object ownership.
    frappe.db.commit()
    _, file = file_lock(file.name)
    try:
        file.upload_id = storage.start(file.object_key, mime, file.name)
        file.save(ignore_permissions=True)
        audit(doc, 'Upload started: ' + file.filename)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        raise
    return {'file': file.name, 'status': file.status, 'part_size': PART_SIZE}


def upload_state(name, number=None):
    require_user(True)
    _, file = file_lock(name)
    if file.status == 'Ready':
        return {'status': 'Ready', 'parts': [], 'part_size': PART_SIZE}
    if file.status != 'Uploading':
        frappe.throw('This upload is no longer active.')
    if not file.upload_id:
        file.upload_id = storage.start(file.object_key, file.mime, file.name)
        file.save(ignore_permissions=True)
        frappe.db.commit()
    if number is not None:
        number = cint(number)
        if not 1 <= number <= part_count(file.size_bytes):
            frappe.throw('Invalid part number.')
        file.db_set('modified', now_datetime(), update_modified=False)
        return {'url': storage.part_url(file.object_key, file.upload_id, number)}
    try:
        rows = storage.parts(file.object_key, file.upload_id)
    except Exception as exc:
        if not storage.absent(exc, 'NoSuchUpload'):
            raise
        # Complete may have succeeded even if its response was lost. Never restart it blindly.
        info = storage.head(file.object_key)
        if int(info['ContentLength']) != int(file.size_bytes) or info.get('Metadata', {}).get('qas-file') != file.name:
            frappe.throw('Stored file verification failed.')
        return {'status': 'Verifying', 'parts': [], 'part_size': PART_SIZE}
    return {'status': file.status, 'parts': [{'number': r['PartNumber'], 'size': r['Size']} for r in rows], 'part_size': PART_SIZE}


def complete_upload(name):
    require_user(True)
    doc, file = file_lock(name)
    if file.status == 'Ready':
        return detail(doc.name, True)
    if file.status != 'Uploading' or not file.upload_id:
        frappe.throw('Upload is not ready to complete.')
    try:
        rows = storage.parts(file.object_key, file.upload_id)
        validate_parts(rows, file.size_bytes)
        storage.finish(file.object_key, file.upload_id, rows)
    except Exception as exc:
        if not storage.absent(exc, 'NoSuchUpload'):
            raise
    info = storage.head(file.object_key)
    if int(info['ContentLength']) != int(file.size_bytes) or info.get('Metadata', {}).get('qas-file') != file.name or info.get('ContentType') != file.mime:
        frappe.throw('Stored file verification failed. The file has not been published.')
    if file.replaces:
        old = frappe.get_doc(FILE, file.replaces)
        if old.resource != doc.name or old.status != 'Ready':
            frappe.throw('The original file changed. Cancel this upload and reload.')
        file.sort_order = old.sort_order
        file.is_visible = old.is_visible
        old.status = 'Retired'
        old.save(ignore_permissions=True)
    file.status = 'Ready'
    file.replaces = ''
    file.upload_id = ''
    file.save(ignore_permissions=True)
    doc.save(ignore_permissions=True)
    audit(doc, 'File ready: ' + file.filename)
    return detail(doc.name, True)


def cancel_upload(name):
    require_user(True)
    doc, file = file_lock(name)
    if file.status != 'Uploading':
        frappe.throw('Only incomplete uploads can be cancelled.')
    if file.upload_id:
        storage.abort(file.object_key, file.upload_id)
    storage.delete(file.object_key)
    file.status = 'Cancelled'
    file.upload_id = ''
    file.save(ignore_permissions=True)
    doc.save(ignore_permissions=True)
    audit(doc, 'Upload cancelled: ' + file.filename)
    return detail(doc.name, True)


def edit_files(data):
    require_user(True)
    data = payload(data)
    doc = lock_resource(data['resource'], data['modified'])
    require_editable(doc)
    rows = data.get('files', [])
    if not isinstance(rows, list) or len(rows) > MAX_FILES:
        frappe.throw('Invalid file list.')
    for index, row in enumerate(rows):
        if not frappe.db.exists(FILE, {'name': row.get('name'), 'resource': doc.name, 'status': 'Ready'}):
            frappe.throw('Reload the file list before editing.')
        frappe.db.set_value(FILE, row['name'], {'label': text(row.get('label'), 200, True), 'sort_order': index})
    doc.save(ignore_permissions=True)
    audit(doc, 'File names and order updated.')
    return detail(doc.name, True)


def file_link(name, admin=False):
    require_user(admin)
    file = frappe.get_doc(FILE, name)
    if file.status != 'Ready' or (not admin and not file.is_visible):
        frappe.throw('This file is unavailable.', frappe.PermissionError)
    lifetime = 900 if admin else teacher_access(file.resource, teacher_name())[1]
    if admin and frappe.db.get_value(RESOURCE, file.resource, 'status') == 'Deleting':
        frappe.throw('Deletion is in progress.')
    return {'url': storage.get_url(file.object_key, file.filename, file.kind == 'video', lifetime), 'expires_in': lifetime, 'kind': file.kind}


def cleanup_file(name):
    # Lock parent first, like all user mutations, to avoid a deletion/upload deadlock.
    resource = frappe.db.get_value(FILE, name, 'resource')
    if not resource:
        return
    lock_resource(resource)
    file = frappe.get_doc(FILE, name)
    if file.status == 'Uploading' and get_datetime(file.modified) >= get_datetime(add_days(now_datetime(), -7)):
        return
    if file.status not in ('Retired', 'Deleting', 'Cancelled', 'Uploading'):
        return
    if file.upload_id:
        storage.abort(file.object_key, file.upload_id)
    storage.delete(file.object_key)
    frappe.delete_doc(FILE, name, ignore_permissions=True)


def cleanup_resource(name):
    doc = lock_resource(name)
    if doc.status != 'Deleting':
        return
    # Pending replacements may still link to the original; all files are being deleted together.
    frappe.db.sql('UPDATE `tabTeaching Resource File` SET replaces=NULL WHERE resource=%s', (name,))
    for file in frappe.get_all(FILE, filters={'resource': name}, pluck='name', limit_page_length=0):
        frappe.db.set_value(FILE, file, 'status', 'Deleting')
        cleanup_file(file)
    for doctype in (ASSIGN, GRANT, TAG_LINK):
        for child in frappe.get_all(doctype, filters={'resource': name}, pluck='name', limit_page_length=0):
            frappe.delete_doc(doctype, child, ignore_permissions=True)
    doc.status = 'Deleted'
    doc.save(ignore_permissions=True)
    audit(doc, 'All files permanently deleted; minimal resource record retained for audit.')


def cleanup():
    if not enabled():
        return
    jobs = [(cleanup_resource, r) for r in frappe.get_all(RESOURCE, filters={'status': 'Deleting'}, pluck='name', limit_page_length=50)]
    jobs += [(cleanup_file, r) for r in frappe.get_all(FILE, filters={'status': ['in', ['Retired', 'Cancelled']]}, pluck='name', limit_page_length=100)]
    jobs += [(cleanup_file, r) for r in frappe.get_all(FILE, filters={'status': 'Uploading', 'modified': ['<', add_days(now_datetime(), -7)]}, pluck='name', limit_page_length=100)]
    for function, name in jobs:
        try:
            function(name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            # Avoid logging provider exception messages, which can contain signed request URLs.
            frappe.log_error(title='Teaching resource cleanup needs retry', message=f'{function.__name__}: {name}')


def teacher_options():
    teacher = teacher_name()
    params = {}
    scope = scope_sql(teacher, params)
    rows = frappe.db.sql(f"""SELECT DISTINCT a.term,a.campus,a.course FROM `tabTeaching Resource Assignment` a
        JOIN `tabTeaching Resource` r ON r.name=a.resource WHERE r.status='Published' AND {scope}""", params, as_dict=True)
    return {key: sorted({row[key] for row in rows}) for key in ('term', 'campus', 'course')}
