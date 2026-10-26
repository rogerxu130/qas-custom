"""Preview lifecycle, private delivery and bounded conversion tests without a site."""
import io
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from qas_custom.services import teaching_resource_previews as previews
from qas_custom.services import teaching_resources as resources


class Denied(Exception):
    pass


def file(**changes):
    values = dict(name='F1', resource='R1', filename='苹果 lesson.pptx', kind='ppt', status='Ready', size_bytes=13,
        object_key='private/source', upload_id='', preview_status='', preview_error='', preview_token='', preview_started_at=None,
        preview_size_bytes=0, save=Mock())
    values.update(changes)
    return SimpleNamespace(**values)


class Previews(TestCase):
    def setUp(self):
        self.file = file()
        self.doc = SimpleNamespace(name='R1')
        self.frappe = SimpleNamespace(db=SimpleNamespace(after_commit=SimpleNamespace(add=Mock()), commit=Mock(), rollback=Mock(),
            get_value=Mock(return_value='Published')), enqueue=Mock(), get_doc=Mock(return_value=self.file), log_error=Mock(),
            throw=lambda message,*args: (_ for _ in ()).throw(Denied(message)))
        for p in [patch.object(previews, 'frappe', self.frappe), patch.object(resources, 'file_lock', return_value=(self.doc,self.file)),
            patch.object(resources, 'require_user')]:
            p.start(); self.addCleanup(p.stop)

    def test_queue_after_commit_and_duplicate_pending_requests_are_idempotent(self):
        result = previews.request('F1')
        self.assertEqual(result['preview_status'], 'Pending')
        resources.require_user.assert_called_once_with(True)
        token = self.file.preview_token
        self.assertTrue(token)
        self.frappe.enqueue.assert_not_called()
        previews.request('F1')
        self.frappe.db.after_commit.add.assert_called_once()
        self.frappe.db.after_commit.add.call_args.args[0]()
        self.frappe.enqueue.assert_called_once_with('qas_custom.services.teaching_resource_previews.generate', queue='long', timeout=600, name='F1', token=token)

    def test_stale_job_gets_new_token_but_ready_preview_is_not_reconverted(self):
        self.file.preview_status='Processing'; self.file.preview_started_at=datetime.now()-timedelta(hours=1); self.file.preview_token='stale'
        previews.queue(self.file)
        self.assertNotEqual(self.file.preview_token,'stale')
        self.file.preview_status='Ready'
        previews.queue(self.file)
        self.frappe.db.after_commit.add.assert_called_once()

    def test_queue_outage_does_not_raise_or_delete_original(self):
        self.frappe.enqueue.side_effect=RuntimeError('Redis failed')
        previews._submit('F1','token')
        self.frappe.log_error.assert_called_once()
        self.assertEqual(self.file.status,'Ready')

    def test_ready_preview_is_inline_private_pdf_and_not_the_source_ppt(self):
        self.file.preview_status='Ready'
        with patch.object(previews.storage,'get_url',return_value='signed-pdf') as sign:
            result=previews.status('F1')
        self.assertEqual(result['url'],'signed-pdf')
        sign.assert_called_once_with('private/source.preview.pdf','苹果 lesson.pdf',True,900)
        self.assertNotIn('preview_token',result)

    def test_preview_authorizes_admin_before_signing_and_rejects_removed_source(self):
        with patch.object(resources,'require_user',side_effect=Denied), patch.object(previews.storage,'get_url') as sign:
            with self.assertRaises(Denied): previews.status('F1')
            sign.assert_not_called()
        for state in ['Retired','Deleting','Uploading']:
            self.file.status=state
            with self.subTest(state=state), self.assertRaises(Denied): previews.status('F1')
        self.file.status='Ready'; self.frappe.db.get_value.return_value='Deleting'
        with self.assertRaises(Denied): previews.status('F1')

    def run_job(self, conversion=None, stored_info=None):
        directory=tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        pdf=Path(directory.name)/'preview.pdf'; pdf.write_bytes(b'%PDF-1.7\nmock')
        self.file.preview_status='Pending'; self.file.preview_token='token'
        patches=[patch.object(previews.storage,'download_preview_source',return_value=13),
            patch.object(previews,'convert',side_effect=conversion,return_value=pdf),
            patch.object(previews.storage,'upload_preview'),
            patch.object(previews.storage,'head',return_value=stored_info if stored_info is not None else {'ContentType':'application/pdf','ContentLength':pdf.stat().st_size,'Metadata':{'qas-preview-of':'F1'}})]
        mocks=[]
        for p in patches:
            mocks.append(p.start()); self.addCleanup(p.stop)
        previews.generate('F1','token')
        return pdf,mocks

    def test_conversion_upload_is_verified_before_ready_and_keeps_source(self):
        pdf,mocks=self.run_job()
        self.assertEqual(self.file.preview_status,'Ready')
        self.assertEqual(self.file.preview_size_bytes,pdf.stat().st_size)
        self.assertEqual(self.file.status,'Ready')
        mocks[2].assert_called_once_with('private/source.preview.pdf',pdf,'F1')

    def test_replacement_deletion_or_new_token_cancels_stale_result(self):
        for change in [dict(status='Retired'),dict(preview_token='new-attempt',preview_status='Pending')]:
            self.file=file()
            resources.file_lock.return_value=(self.doc,self.file)
            def convert(*args):
                for key,value in change.items(): setattr(self.file,key,value)
                return Path('/unused')
            with self.subTest(change=change):
                _,mocks=self.run_job(convert)
                mocks[2].assert_not_called()
                self.assertNotEqual(self.file.preview_status,'Ready')

    def test_failed_conversion_keeps_original_and_sanitizes_provider_errors(self):
        _,mocks=self.run_job(RuntimeError('https://private.example/?signature=secret'))
        self.assertEqual(self.file.preview_status,'Failed')
        self.assertEqual(self.file.status,'Ready')
        self.assertNotIn('secret',self.file.preview_error)
        mocks[2].assert_not_called()

    def test_storage_metadata_mismatch_cannot_become_ready(self):
        self.run_job(stored_info={'ContentType':'application/pdf','ContentLength':13,'Metadata':{'qas-preview-of':'OTHER'}})
        self.assertEqual(self.file.preview_status,'Failed')
        self.assertEqual(self.file.status,'Ready')

    def test_missing_converter_and_timeout_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(previews.shutil,'which',return_value=None):
            with self.assertRaises(previews.PreviewFailure): previews.convert(Path(directory)/'source.pptx',directory)
        with tempfile.TemporaryDirectory() as directory, patch.object(previews.shutil,'which',return_value='/soffice'), patch.object(previews.subprocess,'Popen') as popen, patch.object(previews.os,'killpg') as kill:
            popen.return_value.pid=123
            popen.return_value.wait.side_effect=[subprocess.TimeoutExpired('soffice',180),0]
            with self.assertRaises(previews.PreviewFailure): previews.convert(Path(directory)/'source.pptx',directory)
            kill.assert_called_once_with(123,previews.signal.SIGKILL)
            settings=(Path(directory)/'profile/user/registrymodifications.xcu').read_text()
            self.assertIn('DisableMacrosExecution',settings)
            self.assertIn('<value>true</value>',settings)

    def test_completed_ppt_upload_automatically_queues_preview_once(self):
        original=file(status='Uploading',size_bytes=3,upload_id='upload-id')
        parent=SimpleNamespace(name='R1',save=Mock())
        original.replaces='';original.mime='application/vnd.openxmlformats-officedocument.presentationml.presentation'
        with patch.object(resources,'require_user'),patch.object(resources,'file_lock',return_value=(parent,original)),patch.object(resources,'audit'),patch.object(resources,'detail',return_value={}),patch.object(previews,'queue') as enqueue,patch.object(resources.storage,'parts',return_value=[{'PartNumber':1,'Size':3,'ETag':'tag'}]),patch.object(resources.storage,'finish'),patch.object(resources.storage,'head',return_value={'ContentLength':3,'ContentType':original.mime,'Metadata':{'qas-file':'F1'}}):
            resources.complete_upload('F1')
            resources.complete_upload('F1')
            enqueue.assert_called_once_with(original)
            self.assertEqual(original.status,'Ready')

    def test_cleanup_removes_preview_before_losing_source_tracking(self):
        source=file(status='Retired')
        fake=SimpleNamespace(db=SimpleNamespace(get_value=lambda *args:'R1'),get_doc=lambda *args:source,delete_doc=Mock())
        with patch.object(resources,'frappe',fake),patch.object(resources,'lock_resource'),patch.object(resources.storage,'delete') as remove:
            resources.cleanup_file('F1')
            self.assertEqual([call.args[0] for call in remove.call_args_list],['private/source.preview.pdf','private/source'])
            fake.delete_doc.assert_called_once()
        fake.delete_doc.reset_mock()
        with patch.object(resources,'frappe',fake),patch.object(resources,'lock_resource'),patch.object(resources.storage,'delete',side_effect=RuntimeError('storage unavailable')) as remove:
            with self.assertRaises(RuntimeError): resources.cleanup_file('F1')
            remove.assert_called_once_with('private/source.preview.pdf')
            fake.delete_doc.assert_not_called()


class PreviewStorage(TestCase):
    def test_download_is_bounded_and_closes_response(self):
        for payload,declared,maximum,ok in [(b'1234',4,4,True),(b'12345',4,4,False),(b'12345',5,4,False)]:
            body=io.BytesIO(payload)
            s3=Mock();s3.get_object.return_value={'Body':body,'ContentLength':declared}
            with self.subTest(payload=payload,declared=declared), tempfile.TemporaryDirectory() as directory,patch.object(previews.storage,'config',return_value=(s3,'bucket')):
                if ok:
                    self.assertEqual(previews.storage.download_preview_source('key',Path(directory)/'source',maximum),4)
                else:
                    with self.assertRaises(ValueError): previews.storage.download_preview_source('key',Path(directory)/'source',maximum)
                self.assertTrue(body.closed)
