"""Private, derived PDF previews. Only background workers handle document bytes."""
import os
import shutil
import signal
import subprocess
import tempfile
import uuid
from functools import partial
from datetime import timedelta
from pathlib import Path

import frappe
from frappe.utils import get_datetime, now_datetime

from qas_custom.services import teaching_resource_storage as storage

MAX_BYTES = 256 * 1024**2
CONVERT_TIMEOUT = 180
LEASE_MINUTES = 30
FILE = 'Teaching Resource File'


class PreviewFailure(Exception):
    pass


def object_key(file):
    return file.object_key + '.preview.pdf'


def summary(file):
    return {'preview_status': file.preview_status or '', 'preview_error': file.preview_error or ''}


def eligible(file):
    if file.kind != 'ppt' or file.status != 'Ready':
        frappe.throw('Select an uploaded PPT to preview. / 请选择上传完成的 PPT。')


def queue(file):
    eligible(file)
    if file.preview_status == 'Ready':
        return summary(file)
    if file.preview_status in ('Pending', 'Processing') and file.preview_started_at:
        age = (now_datetime() - get_datetime(file.preview_started_at)).total_seconds()
        if age < LEASE_MINUTES * 60:
            return summary(file)
    file.preview_status = 'Pending'
    file.preview_error = ''
    file.preview_started_at = now_datetime()
    file.preview_token = uuid.uuid4().hex
    file.save(ignore_permissions=True)
    # A Redis outage must not turn a successful original upload into a failed one.
    frappe.db.after_commit.add(partial(_submit, file.name, file.preview_token))
    return summary(file)


def _submit(name, token):
    try:
        frappe.enqueue('qas_custom.services.teaching_resource_previews.generate', queue='long', timeout=600, name=name, token=token)
    except Exception:
        frappe.log_error(title='PPT preview queue needs retry', message=name)


def request(name):
    from qas_custom.services import teaching_resources as resources
    resources.require_user(True)
    _, file = resources.file_lock(name)
    return queue(file)


def status(name):
    from qas_custom.services import teaching_resources as resources
    resources.require_user(True)
    file = frappe.get_doc(FILE, name)
    eligible(file)
    if frappe.db.get_value('Teaching Resource', file.resource, 'status') in ('Deleting', 'Deleted'):
        frappe.throw('This resource is unavailable.')
    result = summary(file)
    if file.preview_status == 'Ready':
        result.update(url=storage.get_url(object_key(file), Path(file.filename).stem + '.pdf', True, 900), expires_in=900)
    return result


def convert(source, output_dir):
    executable = shutil.which('soffice') or shutil.which('libreoffice')
    if not executable:
        raise PreviewFailure('PDF 转换组件未安装，请更新后端环境。')
    profile = Path(output_dir) / 'profile'
    (profile / 'user').mkdir(parents=True)
    (profile / 'user/registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
</item></oor:items>''')
    command = [executable, '-env:UserInstallation=' + profile.resolve().as_uri(), '--headless', '--norestore', '--nodefault',
        '--convert-to', 'pdf:impress_pdf_Export', '--outdir', str(output_dir), str(source)]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        code = process.wait(timeout=CONVERT_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise PreviewFailure('PDF 生成超时，请重试或下载原 PPT。') from None
    pdf = Path(output_dir) / (Path(source).stem + '.pdf')
    if code != 0 or not pdf.is_file() or not 0 < pdf.stat().st_size <= MAX_BYTES:
        raise PreviewFailure('无法转换此 PPT，请检查文件后重试。')
    with pdf.open('rb') as stream:
        if stream.read(5) != b'%PDF-':
            raise PreviewFailure('PDF 预览校验失败，请重试。')
    return pdf


def _same_job(file, token):
    return file.status == 'Ready' and file.kind == 'ppt' and file.preview_token == token


def generate(name, token):
    from qas_custom.services import teaching_resources as resources
    try:
        _, file = resources.file_lock(name)
        if not _same_job(file, token) or file.preview_status != 'Pending':
            return
        file.preview_status = 'Processing'
        file.preview_started_at = now_datetime()
        file.save(ignore_permissions=True)
        frappe.db.commit()
        if int(file.size_bytes) > MAX_BYTES:
            raise PreviewFailure('PPT 超过 256 MiB 预览上限，请下载原文件。')
        with tempfile.TemporaryDirectory(prefix='qas-ppt-preview-') as directory:
            source = Path(directory) / ('source' + Path(file.filename).suffix.lower())
            actual_size = storage.download_preview_source(file.object_key, source, MAX_BYTES)
            if actual_size != int(file.size_bytes):
                raise PreviewFailure('PPT 文件大小校验失败，请重新上传。')
            pdf = convert(source, directory)
            # Recheck after conversion; replacement/deletion/new attempts invalidate this token.
            _, file = resources.file_lock(name)
            if not _same_job(file, token) or file.preview_status != 'Processing':
                return
            storage.upload_preview(object_key(file), pdf, name)
            info = storage.head(object_key(file))
            if info.get('ContentType') != 'application/pdf' or int(info['ContentLength']) != pdf.stat().st_size or info.get('Metadata', {}).get('qas-preview-of') != name:
                raise PreviewFailure('PDF 存储校验失败，请重试。')
            file.preview_status = 'Ready'
            file.preview_error = ''
            file.preview_size_bytes = pdf.stat().st_size
            file.save(ignore_permissions=True)
            frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        try:
            _, file = resources.file_lock(name)
            if _same_job(file, token) and file.preview_status in ('Pending', 'Processing'):
                file.preview_status = 'Failed'
                file.preview_error = str(exc) if isinstance(exc, PreviewFailure) else '预览生成失败，请重试；原 PPT 仍可下载。'
                file.save(ignore_permissions=True)
                frappe.db.commit()
        except Exception:
            frappe.db.rollback()
        # Provider and converter exceptions may contain private paths or signed URLs.
        frappe.log_error(title='PPT preview generation failed', message=name)


def enqueue_missing():
    from qas_custom.services import teaching_resources as resources
    if not resources.enabled():
        return
    cutoff = now_datetime() - timedelta(minutes=LEASE_MINUTES)
    rows = frappe.db.sql('''SELECT name FROM `tabTeaching Resource File`
        WHERE kind='ppt' AND status='Ready' AND (COALESCE(preview_status,'')=''
        OR (preview_status IN ('Pending','Processing') AND COALESCE(preview_started_at,creation)<%s))
        ORDER BY creation LIMIT 10''', (cutoff,), as_dict=True)
    for row in rows:
        try:
            _, file = resources.file_lock(row.name)
            queue(file)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title='PPT preview backfill needs retry', message=row.name)
