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
            'Teaching Resource': 'name TEXT,title TEXT,summary TEXT,status TEXT,modified TEXT,admin_note TEXT',
            'Teaching Resource Tag': 'name TEXT,tag TEXT',
            'Teaching Resource Tag Link': 'name TEXT,resource TEXT,tag TEXT',
            'Teaching Resource Teacher Grant': 'name TEXT,resource TEXT,teacher TEXT',
            'Teaching Resource Assignment': 'name TEXT,resource TEXT,term TEXT,campus TEXT,course TEXT,note TEXT',
            'Term': 'name TEXT,status TEXT,end_date TEXT,term_name TEXT', 'Campus': 'name TEXT,status TEXT,campus_name TEXT', 'Course': 'name TEXT,status TEXT,course_name TEXT',
            'Weekly Timeslot': 'name TEXT,term TEXT,campus TEXT,course TEXT,teacher TEXT',
            'Course Sessions': 'name TEXT,weekly_timeslot TEXT,teacher_override TEXT,status TEXT',
        }.items():
            self.db.execute(f'CREATE TABLE `tab{table}` ({columns})')
        for table, rows in {
            'Teaching Resource': [('R1','Apple','Lesson','Published','2026','内部备课苹果'), ('R2','Other campus','','Published','2026','其他校区备注'), ('DRAFT','Hidden','','Draft','2026','草稿备注')],
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

    def test_catalog_searches_title_and_description_without_widening_access(self):
        self.db.db.execute("UPDATE `tabTeaching Resource` SET summary='苹果静物写生' WHERE name='R1'")
        self.db.db.execute("UPDATE `tabTeaching Resource` SET summary='苹果高级私有课' WHERE name='R2'")
        self.db.db.execute("UPDATE `tabTeaching Resource` SET summary='苹果未发布' WHERE name='DRAFT'")
        with patch.object(service, 'require_user'), patch.object(service, 'teacher_name', return_value='MAIN'):
            self.assertEqual([r.name for r in service.catalog({'query': 'Apple'})['items']], ['R1'])
            self.assertEqual([r.name for r in service.catalog({'query': '苹果'})['items']], ['R1'])
            self.assertEqual(service.catalog({'query': '高级私有课'})['items'], [])
            self.assertEqual(service.catalog({'query': "' OR 1=1 --"})['items'], [])
            self.assertEqual({r.name for r in service.catalog({'query': '苹果'}, admin=True)['items']}, {'R1', 'R2', 'DRAFT'})

    def test_admin_note_is_returned_and_searchable_only_for_admins(self):
        with patch.object(service, 'require_user'), patch.object(service, 'teacher_name', return_value='MAIN'):
            admin_items = service.catalog({'query': '内部备课苹果'}, admin=True)['items']
            self.assertEqual([r.name for r in admin_items], ['R1'])
            self.assertEqual(admin_items[0].admin_note, '内部备课苹果')
            self.assertEqual(service.catalog({'query': '内部备课苹果'})['items'], [])
            teacher_items = service.catalog({'query': 'Apple'})['items']
            self.assertEqual([r.name for r in teacher_items], ['R1'])
            self.assertNotIn('admin_note', teacher_items[0])
            self.assertEqual(service.catalog({'query': '内部备课苹果', 'campus': 'B'}, admin=True)['items'], [])

    def test_admin_catalog_returns_all_resource_tags_in_one_query(self):
        tags = [f'标签{i:02d}' for i in range(40)]
        for index, tag in enumerate(tags):
            self.db.db.execute('INSERT INTO `tabTeaching Resource Tag` VALUES (?,?)', (str(index), tag))
            self.db.db.execute('INSERT INTO `tabTeaching Resource Tag Link` VALUES (?,?,?)', (str(index), 'R1', str(index)))
        with patch.object(service, 'require_user'), patch.object(service, 'teacher_name', return_value='MAIN'), patch.object(self.db, 'sql', wraps=self.db.sql) as sql:
            items = service.catalog(admin=True)['items']
            by_name = {row.name: row for row in items}
            self.assertEqual(by_name['R1'].tags, tags)
            self.assertEqual(by_name['R2'].tags, [])
            self.assertEqual(sum('JOIN `tabTeaching Resource Tag`' in call.args[0] for call in sql.call_args_list), 1)
            sql.reset_mock()
            teacher_items = service.catalog()['items']
            self.assertNotIn('tags', teacher_items[0])
            self.assertFalse(any('Teaching Resource Tag' in call.args[0] for call in sql.call_args_list))

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
            self.db.db.execute("INSERT INTO `tabTeaching Resource` VALUES (?,?,'','Published','2027','')", ('P'+str(i), 'Page '+str(i)))
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


class ResourceSearchPriority(TestCase):
    setUp = ResourceAuthorization.setUp

    def add_resource(self, name, title='', summary='', note='', modified='2090'):
        self.db.db.execute('INSERT INTO `tabTeaching Resource` VALUES (?,?,?,?,?,?)', (name, title, summary, 'Published', modified, note))

    def add_tag(self, resource, name, tag):
        self.db.db.execute('INSERT INTO `tabTeaching Resource Tag` VALUES (?,?)', (name, tag))
        self.db.db.execute('INSERT INTO `tabTeaching Resource Tag Link` VALUES (?,?,?)', (name, resource, name))

    def test_priority_is_note_tag_title_description_even_when_lower_matches_are_newer(self):
        self.add_resource('NOTE', note='needle', modified='2000')
        self.add_resource('TAG', modified='2090')
        self.add_tag('TAG', 'TG1', 'prefix-needle-tag')
        self.add_tag('TAG', 'TG2', 'another-needle-tag')
        self.add_resource('TITLE', title='needle', modified='2091')
        self.add_resource('DESCRIPTION', summary='needle', modified='2092')
        with patch.object(service, 'require_user'):
            items = service.catalog({'query': 'needle'}, admin=True)['items']
            self.assertEqual([r.name for r in items], ['NOTE', 'TAG', 'TITLE', 'DESCRIPTION'])
            self.assertEqual(service.catalog(admin=True)['items'][0].name, 'DESCRIPTION')
            self.assertEqual(service.catalog({'query': '   '}, admin=True)['items'][0].name, 'DESCRIPTION')
            # A combination of lower-priority matches still uses its highest matching field.
            self.db.db.execute("UPDATE `tabTeaching Resource` SET title='needle',summary='needle' WHERE name='TAG'")
            self.assertEqual([r.name for r in service.catalog({'query': 'needle'}, admin=True)['items']], ['NOTE', 'TAG', 'TITLE', 'DESCRIPTION'])

    def test_ranking_happens_before_pagination_and_ties_remain_stable(self):
        self.add_resource('NOTE', note='needle', modified='2000')
        for index in range(35):
            self.add_resource(f'DESC{index:02d}', summary='needle', modified='2099')
        with patch.object(service, 'require_user'):
            first = service.catalog({'query': 'needle'}, admin=True)
            second = service.catalog({'query': 'needle'}, offset=30, admin=True)
            self.assertTrue(first['has_more'])
            self.assertFalse(second['has_more'])
            self.assertEqual([r.name for r in first['items']] + [r.name for r in second['items']], ['NOTE'] + [f'DESC{i:02d}' for i in range(35)])

    def test_tag_search_keeps_filters_and_private_teacher_search_boundaries(self):
        self.add_tag('R1', 'PRIVATE', 'needle-private-tag')
        with patch.object(service, 'require_user'), patch.object(service, 'teacher_name', return_value='MAIN'):
            self.assertEqual([r.name for r in service.catalog({'query': 'needle-private'}, admin=True)['items']], ['R1'])
            self.assertEqual(service.catalog({'query': 'needle-private', 'campus': 'B'}, admin=True)['items'], [])
            self.assertEqual(service.catalog({'query': 'needle-private'})['items'], [])
            self.assertEqual([r.name for r in service.catalog({'query': 'Apple'})['items']], ['R1'])


class AdminTermCatalog(TestCase):
    setUp = ResourceAuthorization.setUp

    def test_new_term_keeps_unassigned_resources_visible_with_history(self):
        def get_all(doctype, filters, **kwargs):
            return [a for a in self.db.sql('SELECT resource,term,campus,course FROM `tabTeaching Resource Assignment`', as_dict=True) if a.resource in filters['resource'][1]]
        with patch.object(service, 'require_user'), patch.object(service.frappe, 'get_all', get_all, create=True):
            result = service.catalog({'assignment_term': 'NEW'}, admin=True)
            self.assertEqual(len(result['items']), 3)
            apple = next(r for r in result['items'] if r.name == 'R1')
            self.assertEqual(apple.term_assignment_count, 0)
            self.assertEqual(apple.other_term_assignments[0].term, 'T1')
            self.assertEqual(service.catalog({'assignment_term': 'NEW', 'assignment_status': 'assigned'}, admin=True)['items'], [])
            current = service.catalog({'assignment_term': 'T1', 'assignment_status': 'assigned', 'campus': 'A'}, admin=True)
            self.assertEqual([r.name for r in current['items']], ['R1'])
            self.assertEqual(service.catalog({'assignment_term': 'NEW', 'campus': 'A'}, admin=True)['items'], [])

    def test_teacher_requests_never_include_admin_history(self):
        with patch.object(service, 'require_user'), patch.object(service, 'teacher_name', return_value='MAIN'):
            items = service.catalog({'assignment_term': 'NEW'})['items']
            self.assertEqual([r.name for r in items], ['R1'])
            self.assertNotIn('other_term_assignments', items[0])
            self.assertNotIn('term_assignment_count', items[0])


class TermAssignmentHistory(TestCase):
    def setUp(self):
        self.assignments = [dict(name='OLD', resource='R1', term='PAST', campus='A', course='C', note='Original history'), dict(name='CURRENT', resource='R1', term='NOW', campus='B', course='C', note='Old current note')]
        self.doc = SimpleNamespace(name='R1', status='Published', save=Mock())
        self.state = frappe._dict(status='Active', end_date='2099-01-01')
        self.deleted = []
        def get_all(doctype, filters, **kwargs):
            if doctype != service.ASSIGN:
                return []
            matches = [a for a in self.assignments if all(a.get(k) == v for k, v in filters.items())]
            return [a['name'] for a in matches] if kwargs.get('pluck') else matches
        def delete(doctype, name, **kwargs):
            self.deleted.append((doctype, name))
            self.assignments[:] = [a for a in self.assignments if a['name'] != name]
        def insert(data):
            def apply(**kwargs):
                if data['doctype'] == service.ASSIGN:
                    self.assignments.append(dict(data, name='INSERTED'))
            return SimpleNamespace(insert=apply)
        fake = SimpleNamespace(db=SimpleNamespace(sql=lambda *a, **kw: [self.state], exists=lambda *a: True), get_all=get_all, delete_doc=delete, get_doc=insert, throw=lambda message, *a: (_ for _ in ()).throw(Denied(message)))
        for patcher in [patch.object(service, 'frappe', fake), patch.object(service, 'require_user'), patch.object(service, 'lock_resource', return_value=self.doc), patch.object(service, 'detail', side_effect=lambda *a: self.assignments.copy()), patch.object(service, 'audit')]:
            patcher.start(); self.addCleanup(patcher.stop)

    def data(self, **overrides):
        return dict(name='R1', modified='version', title='Apple', summary='Updated', tags=[], teachers=[], **overrides)

    def test_saving_one_term_keeps_other_terms_and_notes(self):
        service.save(self.data(assignment_term='NOW', assignments=[dict(term='NOW', campus='A', course='C', note='New note')]))
        self.assertEqual(self.assignments[0], dict(name='OLD', resource='R1', term='PAST', campus='A', course='C', note='Original history'))
        self.assertEqual(self.assignments[1]['note'], 'New note')
        self.assertEqual(self.deleted, [(service.ASSIGN, 'CURRENT')])

    def test_clearing_new_term_does_not_clear_history(self):
        service.save(self.data(assignment_term='NEW', assignments=[]))
        self.assertEqual(len(self.assignments), 2)
        self.assertEqual(self.deleted, [])

    def test_metadata_only_save_leaves_all_assignments(self):
        service.save(self.data())
        self.assertEqual(len(self.assignments), 2)
        self.assertEqual(self.deleted, [])

    def test_rejects_cross_term_payload_and_old_client_whole_resource_save(self):
        for extra in [dict(assignment_term='NOW', assignments=[dict(term='PAST', campus='A', course='C')]), dict(assignments=[])]:
            with self.subTest(extra=extra), self.assertRaises(Denied):
                service.save(self.data(**extra))
        self.doc.save.assert_not_called()
        self.assertEqual(self.deleted, [])

    def test_closed_expired_and_invalid_terms_cannot_change_history(self):
        for status, end_date in [('Completed', '2099-01-01'), ('Archived', '2099-01-01'), ('Active', '2000-01-01'), ('Active', None)]:
            self.state.update(status=status, end_date=end_date)
            with self.subTest(status=status, end_date=end_date), self.assertRaises(Denied):
                service.save(self.data(assignment_term='PAST', assignments=[]))
        self.doc.save.assert_not_called()
        self.assertEqual(len(self.assignments), 2)

    def test_upcoming_term_can_be_planned_without_copying_previous_term(self):
        self.state.status = 'Upcoming'
        service.save(self.data(assignment_term='NEXT', assignments=[dict(term='NEXT', campus='B', course='C', note='Next lesson')]))
        self.assertEqual(len(self.assignments), 3)
        self.assertEqual(self.assignments[0]['note'], 'Original history')


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
