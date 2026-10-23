"""Local-only schema and rollback smoke test. Never runs on production sites.

Run from bench/sites with the bench Python. Storage deletion is mocked.
"""

def run():
    from unittest.mock import patch
    import frappe

    frappe.init(site='qas-restore.test')
    frappe.connect()
    assert frappe.local.site == 'qas-restore.test'
    frappe.set_user('Administrator')
    frappe.flags.in_test=True
    from qas_custom.patches.v2026_07_14_add_course_session_teacher_override import execute as ensure_override
    ensure_override()
    from qas_custom.patches.v2026_10_23_teaching_resources import execute
    execute()
    frappe.db.commit()
    print('LOCAL_SCHEMA_READY')
    from qas_custom.services import teaching_resources as s
    frappe.conf.teaching_resources_enabled=1
    frappe.db.savepoint('resource_smoke')
    try:
        with patch.object(s, 'require_user'):
            doc=s.save({'title':'Resource smoke PPT-only','tags':['Basic','basic'],'assignments':[],'teachers':[]})
            assert doc['tags']==['basic']
            resource=doc['name']
            file=frappe.get_doc(dict(doctype=s.FILE,resource=resource,filename='one.pptx',label='Only PPT',kind='ppt',size_bytes=20,object_key='smoke-only-no-storage/'+resource,status='Ready',request_id=frappe.generate_hash(length=32)))
            file.insert(ignore_permissions=True)
            doc=s.detail(resource,True)
            doc=s.lifecycle('publish',resource,str(doc['modified']))
            assert doc['status']=='Published' and doc['files'][0]['is_visible']==1
            try:
                s.save(dict(doc,title='Stale title',modified='2000-01-01 00:00:00'))
                raise AssertionError('Stale editor accepted')
            except frappe.TimestampMismatchError:
                pass
            assert any(r.name==resource for r in s.catalog({'query':'Resource smoke'},admin=True)['items'])
            doc=s.lifecycle('archive',resource,str(doc['modified']))
            assert doc['status']=='Archived'
            doc=s.lifecycle('restore',resource,str(doc['modified']))
            assert doc['status']=='Draft'
            assignments=s.teacher_options if False else None
            with patch.object(s,'teacher_name',return_value='missing-teacher'):
                assert s.catalog()['items']==[]
                assert s.teacher_options()=={'term':[],'campus':[],'course':[]}
            assert s.options()['tags']
            replacement=frappe.get_doc(dict(doctype=s.FILE,resource=resource,filename='new.pptx',label='Pending replacement',kind='ppt',size_bytes=20,object_key='smoke-only-no-storage/new/'+resource,status='Uploading',upload_id='test-only',replaces=file.name,request_id=frappe.generate_hash(length=32)))
            replacement.insert(ignore_permissions=True)
            doc=frappe.get_doc(s.RESOURCE,resource)
            doc.status='Deleting';doc.save(ignore_permissions=True)
            with patch.object(s.storage,'delete') as delete, patch.object(s.storage,'abort') as abort:
                s.cleanup_resource(resource)
                assert delete.call_count==2
                abort.assert_called_once()
            assert frappe.db.get_value(s.RESOURCE,resource,'status')=='Deleted'
            assert not frappe.db.exists(s.FILE,{'resource':resource})
            assert not any(r.name==resource for r in s.catalog({'query':'Resource smoke'},admin=True)['items'])
            assert frappe.db.exists('Comment',{'reference_doctype':s.RESOURCE,'reference_name':resource})
            print('LOCAL_CATALOG_PUBLISH_ARCHIVE_RESTORE_DELETE_AUDIT_CONCURRENCY_SQL_OK')
    finally:
        frappe.db.rollback(save_point='resource_smoke')
        frappe.destroy()
    print('LOCAL_FIXTURES_ROLLED_BACK')


if __name__ == "__main__":
    run()
