"""Exercise real service authorization queries against small relational fixtures."""
import re
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

import frappe
from qas_custom.services import teaching_resources as service


class Denied(Exception):
    pass


class Database:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        for table, columns in {
            'Teaching Resource': 'name TEXT,title TEXT,summary TEXT,status TEXT,modified TEXT',
            'Teaching Resource Teacher Grant': 'name TEXT,resource TEXT,teacher TEXT',
            'Teaching Resource Assignment': 'name TEXT,resource TEXT,term TEXT,campus TEXT,course TEXT,note TEXT',
            'Term': 'name TEXT,status TEXT,end_date TEXT,term_name TEXT', 'Campus': 'name TEXT,status TEXT,campus_name TEXT', 'Course': 'name TEXT,status TEXT,course_name TEXT',
            'Weekly Timeslot': 'name TEXT,term TEXT,campus TEXT,course TEXT,teacher TEXT',
            'Course Sessions': 'name TEXT,weekly_timeslot TEXT,teacher_override TEXT,status TEXT',
        }.items():
            self.db.execute(f'CREATE TABLE `tab{table}` ({columns})')
        for table, rows in {
            'Teaching Resource': [('R1','Apple','Lesson','Published','2026'), ('R2','Other campus','','Published','2026'), ('DRAFT','Hidden','','Draft','2026')],
            'Term': [('T1','Active','2099-01-01','Term 1')], 'Campus': [('A','Active','Campus A'),('B','Active','Campus B')], 'Course': [('C','Active','Sketching')],
            'Teaching Resource Assignment': [('A1','R1','T1','A','C','Lesson 1'),('A2','R2','T1','B','C','Private note')],
            'Weekly Timeslot': [('W1','T1','A','C','MAIN'),('W2','T1','B','C','OTHER')],
            'Course Sessions': [('S1','W1','SUB','Completed')],
        }.items():
            for row in rows:
                self.db.execute(f'INSERT INTO `tab{table}` VALUES ({",".join("?" for _ in row)})', row)

    def sql(self, sql, values=None, as_dict=False):
        sql = re.sub(r'%\((\w+)\)s', r':\1', sql).replace('%s', '?')
        rows = self.db.execute(sql, values or {}).fetchall()
        if not as_dict:
            return rows
        cursor = self.db.execute(sql, values or {})
        return [frappe._dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]

    def get_value(self, table, name, field):
        rows = self.db.execute(f'SELECT `{field}` FROM `tab{table}` WHERE name=?', (name,)).fetchall()
        return rows[0][0] if rows else None

    def exists(self, table, filters):
        keys = list(filters)
        return self.db.execute(f'SELECT 1 FROM `tab{table}` WHERE ' + ' AND '.join(f'`{key}`=?' for key in keys), [filters[k] for k in keys]).fetchone()

    def change(self, table, field, value):
        self.db.execute(f'UPDATE `tab{table}` SET `{field}`=?', (value,))


class ResourceAuthorization(TestCase):
    def setUp(self):
        self.db = Database()
        fake = SimpleNamespace(db=self.db, PermissionError=Denied, throw=lambda message, *args: (_ for _ in ()).throw(Denied(message)))
        self.patcher = patch.object(service, 'frappe', fake)
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def test_main_teacher_matches_exact_campus(self):
        assignments, ttl = service.teacher_access('R1','MAIN')
        self.assertEqual([r.note for r in assignments], ['Lesson 1'])
        self.assertGreater(ttl, 0)
        with self.assertRaises(Denied): service.teacher_access('R2','MAIN')

    def test_completed_substitute_keeps_access_until_term_expiry(self):
        self.assertTrue(service.teacher_access('R1','SUB')[1])
        self.db.change('Term','end_date','2000-01-01')
        with self.assertRaises(Denied): service.teacher_access('R1','SUB')

    def test_cancelled_or_revoked_substitute_loses_access(self):
        for field, value in [('status','Cancelled'),('teacher_override','ANOTHER')]:
            with self.subTest(field=field):
                old = self.db.get_value('Course Sessions','S1',field)
                self.db.change('Course Sessions',field,value)
                with self.assertRaises(Denied): service.teacher_access('R1','SUB')
                self.db.change('Course Sessions',field,old)

    def test_inactive_states_block_school_assignment(self):
        for table, state in [('Term','Upcoming'),('Term','Completed'),('Term','Archived'),('Campus','Inactive'),('Course','Inactive')]:
            with self.subTest(table=table,state=state):
                self.db.change(table,'status',state)
                with self.assertRaises(Denied): service.teacher_access('R1','MAIN')
                self.db.change(table,'status','Active')

    def test_direct_grant_survives_term_expiry_and_inactive_campus(self):
        self.db.db.execute("INSERT INTO `tabTeaching Resource Teacher Grant` VALUES ('G1','R1','MAIN')")
        self.db.change('Term','end_date','2000-01-01'); self.db.change('Campus','status','Inactive')
        self.assertEqual(service.teacher_access('R1','MAIN')[1], 900)
        self.db.db.execute('DELETE FROM `tabTeaching Resource Teacher Grant`')
        with self.assertRaises(Denied): service.teacher_access('R1','MAIN')

    def test_draft_is_hidden_even_with_direct_grant(self):
        self.db.db.execute("INSERT INTO `tabTeaching Resource Teacher Grant` VALUES ('G1','DRAFT','MAIN')")
        with self.assertRaises(Denied): service.teacher_access('DRAFT','MAIN')

    def test_teacher_catalog_filters_before_pagination(self):
        with patch.object(service,'require_user'), patch.object(service,'teacher_name',return_value='MAIN'):
            result = service.catalog()
            self.assertEqual([r.name for r in result['items']], ['R1'])
            self.assertEqual(service.catalog({'campus':'B'})['items'], [])
            self.assertEqual(service.catalog({'query':"' OR 1=1 --"})['items'], [])

    def test_catalog_includes_only_matching_notes_on_shared_resource(self):
        self.db.db.execute("INSERT INTO `tabTeaching Resource Assignment` VALUES ('A3','R1','T1','B','C','Other campus private note')")
        with patch.object(service, 'require_user'), patch.object(service, 'teacher_name', return_value='MAIN'):
            self.db.sql = Mock(wraps=self.db.sql)
            result = service.catalog()
            row = result['items'][0]
            self.assertEqual([a.name for a in row.assignments], ['A1'])
            self.assertEqual(row.assignments[0].term_label, 'Term 1')
            self.assertEqual(row.assignments[0].campus_label, 'Campus A')
            self.assertEqual(row.assignments[0].course_label, 'Sketching')
            self.assertNotIn('Other campus private note', str(row))
            self.assertEqual(self.db.sql.call_count, 2)

    def test_direct_only_catalog_does_not_expose_unrelated_assignment_notes(self):
        self.db.db.execute("INSERT INTO `tabTeaching Resource Teacher Grant` VALUES ('G1','R2','MAIN')")
        with patch.object(service, 'require_user'), patch.object(service, 'teacher_name', return_value='MAIN'):
            rows = service.catalog()['items']
            direct = next(row for row in rows if row.name == 'R2')
            self.assertEqual(direct.assignments, [])
            self.assertNotIn('Private note', str(direct))

    def test_catalog_batches_assignment_context_after_pagination(self):
        for i in range(35):
            self.db.db.execute("INSERT INTO `tabTeaching Resource` VALUES (?,?,'','Published','2027')", ('P'+str(i), 'Page '+str(i)))
            self.db.db.execute("INSERT INTO `tabTeaching Resource Teacher Grant` VALUES (?,?,'MAIN')", ('G'+str(i), 'P'+str(i)))
        with patch.object(service, 'require_user'), patch.object(service, 'teacher_name', return_value='MAIN'):
            self.db.sql = Mock(wraps=self.db.sql)
            result = service.catalog()
            self.assertEqual(len(result['items']), 30)
            self.assertTrue(result['has_more'])
            self.assertEqual(self.db.sql.call_count, 2)
            context_params = self.db.sql.call_args.args[1]
            self.assertEqual(len([key for key in context_params if key.startswith('resource')]), 30)

    def test_catalog_notes_for_completed_substitute_and_expired_direct_grants(self):
        with patch.object(service, 'require_user'), patch.object(service, 'teacher_name', return_value='SUB'):
            self.assertEqual(service.catalog()['items'][0].assignments[0].note, 'Lesson 1')
            self.db.db.execute("INSERT INTO `tabTeaching Resource Teacher Grant` VALUES ('G1','R1','SUB')")
            self.db.change('Term', 'end_date', '2000-01-01')
            self.assertEqual(service.catalog()['items'][0].assignments, [])


class Publishing(TestCase):
    def test_single_kind_can_publish(self):
        # File checks deliberately do not require separate video and PPT kinds.
        for kind in ('video','ppt','mixed'):
            with self.subTest(kind=kind):
                doc = SimpleNamespace(status='Draft',save=Mock())
                db = Mock(); db.exists.side_effect = [True,False]
                with patch.object(service,'require_user'), patch.object(service,'lock_resource',return_value=doc), patch.object(service,'audit'), patch.object(service,'detail',return_value={'status':'Published'}), patch.object(service,'frappe',SimpleNamespace(db=db)):
                    self.assertEqual(service.lifecycle('publish','R1','version')['status'],'Published')
                    self.assertEqual(doc.status,'Published')
                    for call in db.exists.call_args_list:
                        self.assertNotIn('kind', call.args[1])

    def test_empty_or_incomplete_cannot_publish(self):
        for ready, uploading in [(False,False),(True,True)]:
            db=Mock();db.exists.side_effect=[ready,uploading]
            fake=SimpleNamespace(db=db,throw=lambda *args: (_ for _ in ()).throw(Denied()))
            with patch.object(service,'require_user'), patch.object(service,'lock_resource',return_value=SimpleNamespace(status='Draft')), patch.object(service,'frappe',fake), self.assertRaises(Denied):
                service.lifecycle('publish','R1','version')

    def test_complete_is_idempotent_and_checks_storage_metadata(self):
        doc=SimpleNamespace(name='R1',save=Mock())
        file=SimpleNamespace(name='F1',status='Uploading',upload_id='U1',object_key='private-key',size_bytes=3,mime='video/mp4',replaces=None,filename='a.mp4',save=Mock())
        with patch.object(service,'require_user'), patch.object(service,'file_lock',return_value=(doc,file)), patch.object(service,'audit'), patch.object(service,'detail',return_value={'files':['F1']}), patch.object(service.storage,'parts',return_value=[{'PartNumber':1,'Size':3,'ETag':'etag'}]), patch.object(service.storage,'finish') as finish, patch.object(service.storage,'head',return_value={'ContentLength':3,'ContentType':'video/mp4','Metadata':{'qas-file':'F1'}}):
            service.complete_upload('F1'); service.complete_upload('F1')
            self.assertEqual(file.status,'Ready'); finish.assert_called_once()


class TrustBoundaries(TestCase):
    def test_disabled_guest_and_campus_admin_cannot_manage(self):
        for feature, user, roles in [(0,'admin',['School Admin']),(1,'Guest',[]),(1,'campus',['Campus Admin'])]:
            fake=SimpleNamespace(conf={'teaching_resources_enabled':feature},request=None,session=SimpleNamespace(user=user),db=SimpleNamespace(get_value=lambda *a:1),get_roles=lambda *a:roles,PermissionError=Denied,throw=lambda *a: (_ for _ in ()).throw(Denied()))
            with patch.object(service,'frappe',fake), self.assertRaises(Denied): service.require_user(True)

    def test_support_view_never_signs_resource_links(self):
        fake=SimpleNamespace(conf={'teaching_resources_enabled':1},request=SimpleNamespace(headers={'X-QAS-Support-View-Token':'token'}),PermissionError=Denied,throw=lambda *a: (_ for _ in ()).throw(Denied()))
        with patch.object(service,'frappe',fake), self.assertRaises(Denied): service.require_user(True)

    def test_storage_metadata_mismatch_never_retires_original(self):
        doc=SimpleNamespace(name='R1')
        file=SimpleNamespace(name='F1',status='Uploading',upload_id='U1',object_key='key',size_bytes=3,mime='video/mp4',replaces='OLD')
        fake=SimpleNamespace(throw=lambda *a: (_ for _ in ()).throw(Denied()),get_doc=Mock())
        with patch.object(service,'require_user'),patch.object(service,'file_lock',return_value=(doc,file)),patch.object(service,'frappe',fake),patch.object(service.storage,'parts',return_value=[{'PartNumber':1,'Size':3,'ETag':'etag'}]),patch.object(service.storage,'finish'),patch.object(service.storage,'head',return_value={'ContentLength':3,'ContentType':'video/mp4','Metadata':{'qas-file':'someone-else'}}),self.assertRaises(Denied):
            service.complete_upload('F1')
        fake.get_doc.assert_not_called();self.assertEqual(file.status,'Uploading')

    def test_unsigned_or_unpublished_attachment_cannot_be_downloaded(self):
        for status, visible in [('Uploading',0),('Retired',1),('Ready',0)]:
            fake=SimpleNamespace(get_doc=lambda *a:SimpleNamespace(status=status,is_visible=visible),PermissionError=Denied,throw=lambda *a: (_ for _ in ()).throw(Denied()))
            with patch.object(service,'require_user'),patch.object(service,'frappe',fake),patch.object(service.storage,'get_url') as sign,self.assertRaises(Denied): service.file_link('F1')
            sign.assert_not_called()

    def test_cleanup_does_not_delete_ready_files(self):
        fake=SimpleNamespace(db=SimpleNamespace(get_value=lambda *a:'R1'),get_doc=lambda *a:SimpleNamespace(status='Ready'),delete_doc=Mock())
        with patch.object(service,'frappe',fake),patch.object(service,'lock_resource'),patch.object(service.storage,'delete') as remove: service.cleanup_file('F1')
        remove.assert_not_called();fake.delete_doc.assert_not_called()



class UploadCapacity(TestCase):
    def test_can_replace_at_capacity_without_allowing_an_extra_attachment(self):
        doc=SimpleNamespace(name='R1',status='Published')
        file=SimpleNamespace(name='F1',filename='new.mp4',status='Uploading',object_key='key',insert=Mock(),save=Mock())
        db=Mock();db.get_value.return_value=None;db.count.return_value=100
        db.exists.side_effect=lambda table,filters: filters.get('status')=='Ready'
        fake=SimpleNamespace(db=db,get_doc=lambda *a,**kw:file,throw=lambda *a: (_ for _ in ()).throw(Denied()))
        data={'resource':'R1','filename':'new.mp4','size':10,'request_id':'af0db313-5214-45bc-9942-fbba2cbb32e4','replaces':'OLD'}
        with patch.object(service,'require_user'),patch.object(service,'lock_resource',return_value=doc),patch.object(service,'file_lock',return_value=(doc,file)),patch.object(service,'audit'),patch.object(service,'frappe',fake),patch.object(service.storage,'start',return_value='U1'):
            self.assertEqual(service.init_upload(data)['file'],'F1')
            with self.assertRaises(Denied): service.init_upload({**data,'replaces':None})


if __name__ == '__main__':
    main()
