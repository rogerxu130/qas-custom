"""Local-only smoke test. Creates and removes uniquely named fixtures, never real classroom media."""
import logging
import argparse
import io
from PIL import Image
import uuid
from pathlib import Path
from unittest.mock import patch, Mock
from zipfile import ZipFile

import frappe
from frappe.utils.file_manager import save_file

from qas_custom.services import term_media as service

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--site', required=True, help='Local .test site with restored QAS schemas')
SITE=parser.parse_args().site
if not SITE.endswith('.test') or '/' in SITE or '\\' in SITE:
    raise SystemExit('This fixture runner only accepts local .test sites.')
SITES_PATH=str(Path(__file__).resolve().parents[4]/'sites')
PREFIX='TM-TEST-'+uuid.uuid4().hex[:10]
created=[]
frappe.logger=lambda *a,**k: logging.getLogger('term-media-integration')
# This local fixture runner executes our workers inline; the queue service is not running.
frappe.enqueue=Mock()


def insert(dt, name, **fields):
    doc=frappe.get_doc(dict(doctype=dt,name=name,**fields))
    doc.db_insert()
    created.append((dt,name))
    return doc


try:
    frappe.init(site=SITE,sites_path=SITES_PATH)
    if frappe.conf.get('db_host') not in (None, '', 'localhost', '127.0.0.1', '::1'):
        raise SystemExit('Refusing a non-local database host.')
    frappe.connect();frappe.set_user('Administrator')
    for slug in ['term_media_archive','term_media_removal']:
        frappe.reload_doc('qas_custom','doctype',slug)
    frappe.clear_cache()
    term=insert('Term',PREFIX,term_name=PREFIX,start_date='2026-01-01',end_date='2026-02-01',status='Archived')
    course=frappe.db.get_value('Course',{},'name')
    slot=insert('Weekly Timeslot',PREFIX+'-W',term=term.name,course=course,day_of_week='Saturday',start_time='09:00:00',status='Inactive')
    session=insert('Course Sessions',PREFIX+'-S',weekly_timeslot=slot.name,session_date='2026-01-03',status='Completed')
    photo=insert('Session Photo Post',PREFIX+'-P',course_session=session.name,title='Test photo',caption='Keep this caption',status='Published',posted_at='2026-01-03 10:00:00')
    video=insert('Session Video Post',PREFIX+'-V',course_session=session.name,title='Test video',caption='Keep this caption',status='Published',posted_at='2026-01-03 10:00:00')
    # Distinct random binary bytes prevent deduplication against any real file.
    buffer=io.BytesIO(); Image.frombytes('RGB',(8,8),uuid.uuid4().bytes*12).save(buffer,format='JPEG')
    photo_file=save_file(PREFIX+'.jpg',buffer.getvalue(),'Session Photo Post',photo.name,is_private=1)
    video_file=save_file(PREFIX+'.mp4',b'VIDEO test '+uuid.uuid4().bytes,'Session Video Post',video.name,is_private=1)
    created.extend([('File',photo_file.name),('File',video_file.name)])
    image=insert('Session Photo Item',PREFIX+'-I',parent=photo.name,parenttype='Session Photo Post',parentfield='photos',idx=1,image=photo_file.file_url)
    frappe.db.set_value('Session Video Post',video.name,dict(video=video_file.file_url,file_name=video_file.file_name,file_size=video_file.file_size))
    frappe.db.commit()
    original_paths=[Path(photo_file.get_full_path()),Path(video_file.get_full_path())]
    assert all(PREFIX in path.name for path in original_paths)
    state=service.overview(term.name)
    assert state['eligible'] and state['stats']['photos']==1 and state['stats']['videos']==1, state
    with patch.object(frappe,'enqueue'):
        archive=service.start_archive(term.name)
        created.append((service.ARCHIVE,archive['name']))
        frappe.db.commit()
    service.build_archive(archive['name'])
    ready=service.get_archive(archive['name'])
    assert ready['status']=='Completed',ready
    assert ready['summary']['exported']==2,ready
    path=service.download_path(ready['name'],1)
    with ZipFile(path) as zip:
        assert zip.testzip() is None
        assert len([name for name in zip.namelist() if name.endswith(('.jpg','.mp4'))])==2
    print('PASS real Frappe snapshot, private files, ZIP and manifest')
    try:
        service.start_cleanup(ready['name'],confirmation=term.name)
        raise AssertionError('Unbacked cleanup was accepted')
    except frappe.ValidationError:
        frappe.db.rollback()
    service.confirm_backup(ready['name']);frappe.db.commit()
    preview=service.preview_cleanup(ready['name'],'video')
    assert preview['counts']['ready']==1,preview
    with patch.object(frappe,'enqueue'):
        service.start_cleanup(ready['name'],'video',term.name);frappe.db.commit()
    # Verify real Term save cannot reopen a queued cleanup.
    reopen=frappe.get_doc('Term',term.name);reopen.status='Active'
    try:
        reopen.save(ignore_permissions=True)
        raise AssertionError('Reopening during cleanup was accepted')
    except frappe.ValidationError:
        frappe.db.rollback()
    # Simulate process failure after unlink but before the DB transaction commits.
    real_delete=frappe.delete_doc
    def interrupted_delete(dt,name,*args,**kwargs):
        result=real_delete(dt,name,*args,**kwargs)
        if dt=='File' and name==video_file.name:
            raise RuntimeError('Simulated interruption after unlink')
        return result
    with patch.object(frappe,'delete_doc',side_effect=interrupted_delete):
        service.clean_archive(ready['name'])
    partial=service.get_archive(ready['name'])
    assert partial['cleanup_status']=='Partial',partial
    assert not original_paths[1].exists()
    assert frappe.db.exists('File',video_file.name)
    with patch.object(frappe,'enqueue'):
        service.start_cleanup(ready['name'],'video',term.name);frappe.db.commit()
    service.clean_archive(ready['name'])
    outcome=service.get_archive(ready['name'])
    assert outcome['cleanup_status']=='Completed',outcome
    print('PASS interrupted unlink recovery and queued Term reopen protection')
    assert original_paths[0].exists() and not original_paths[1].exists(),outcome
    assert not frappe.db.get_value('Session Video Post',video.name,'video')
    assert frappe.db.get_value('Session Video Post',video.name,'caption')=='Keep this caption'
    print('PASS video-only physical deletion, photo retained, caption retained')
    # A second File record referencing the same physical photo must prevent cleanup.
    alias=insert('File',PREFIX+'-ALIAS',file_name=photo_file.file_name,file_url=photo_file.file_url,
                 content_hash=photo_file.content_hash,is_private=1,attached_to_doctype='Session Photo Post',attached_to_name=photo.name)
    preview=service.preview_cleanup(ready['name'],'all')
    assert preview['counts']['ready']==0,preview
    assert any(item['reason']=='shared_file' for item in preview['items']),preview
    frappe.db.delete('File',{'name':alias.name});frappe.db.commit()
    preview=service.preview_cleanup(ready['name'],'all')
    assert preview['counts']['ready']==1,preview
    print('PASS shared physical photo skipped until the external reference is removed')
    with patch.object(frappe,'enqueue'):
        service.start_cleanup(ready['name'],'all',term.name);frappe.db.commit()
    service.clean_archive(ready['name'])
    outcome=service.get_archive(ready['name'])
    assert outcome['cleanup_status']=='Completed',outcome
    assert not any(path.exists() for path in original_paths),outcome
    assert not frappe.db.get_value('Session Photo Item',image.name,'image')
    print('PASS repeated cleanup skips removed video and removes backed-up photo')
    annotated=service.annotate_media([dict(id=photo.name,type='photo_post',photos=[dict(idx=1,url='old')],photo_count=1),dict(id=video.name,type='video_post',video_url='old')])
    assert all(item['media_archived'] for item in annotated)
    for status in ['Active','Upcoming']:
        frappe.db.set_value('Term',term.name,'status',status);frappe.db.commit()
        assert not service.overview(term.name)['eligible']
    print('PASS persisted tombstones and Active/Upcoming blocks')
finally:
    if getattr(frappe.local,'db',None):
        frappe.db.rollback()
        # Delete ONLY this test's created IDs, with file names checked before any cleanup.
        for row in frappe.get_all(service.REMOVAL,filters={'term':PREFIX},fields=['name']) if frappe.db.exists('DocType',service.REMOVAL) else []:
            frappe.db.delete(service.REMOVAL,{'name':row.name})
        for dt,name in reversed(created):
            if dt=='File' and frappe.db.exists(dt,name):
                file=frappe.get_doc(dt,name)
                assert PREFIX in file.file_name
                frappe.delete_doc(dt,name,ignore_permissions=True)
            elif dt==service.ARCHIVE:
                import shutil
                shutil.rmtree(service._directory(name),ignore_errors=True)
                frappe.db.delete(dt,{'name':name})
            else:
                frappe.db.delete(dt,{'name':name})
        frappe.db.delete('Comment',{'reference_name':['like',PREFIX+'%']})
        frappe.db.commit()
    frappe.destroy()
    print('Test fixture cleanup complete:',PREFIX)
