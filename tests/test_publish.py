import importlib.util, tempfile, unittest, sys, os
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import collector_schedule
spec=importlib.util.spec_from_file_location('publisher',Path(__file__).resolve().parents[1]/'scripts/publish.py')
publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)
class PublisherTests(unittest.TestCase):
 def test_hosted_subprocess_output_is_private_and_caller_redirects_are_preserved(self):
  with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'},clear=True),patch.object(publisher.subprocess,'run') as command:
   publisher.run(['example'])
   self.assertEqual(command.call_args.kwargs['stdout'],publisher.subprocess.PIPE)
   self.assertEqual(command.call_args.kwargs['stderr'],publisher.subprocess.PIPE)
   publisher.run(['example'],stdout=publisher.subprocess.DEVNULL)
   self.assertEqual(command.call_args.kwargs['stdout'],publisher.subprocess.DEVNULL)
   self.assertEqual(command.call_args.kwargs['stderr'],publisher.subprocess.PIPE)
   publisher.run(['example'],capture_output=True)
   self.assertNotIn('stdout',command.call_args.kwargs)
   self.assertNotIn('stderr',command.call_args.kwargs)
 def test_local_subprocess_output_is_unchanged(self):
  with patch.dict(os.environ,{},clear=True),patch.object(publisher.subprocess,'run') as command:
   publisher.run(['example'])
   self.assertNotIn('stdout',command.call_args.kwargs)
   self.assertNotIn('stderr',command.call_args.kwargs)
   self.assertNotIn('capture_output',command.call_args.kwargs)
 def test_hosted_subprocess_failure_cannot_expose_output_or_arguments(self):
  with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'},clear=True):
   with self.assertRaises(publisher.subprocess.CalledProcessError) as raised:
    publisher.run([sys.executable,'-c',"import sys; print('synthetic-private-output'); sys.stderr.write('synthetic-private-error'); sys.exit(3)"])
   self.assertEqual(raised.exception.returncode,3)
   self.assertNotIn('synthetic-private',str(raised.exception))
   self.assertIsNone(raised.exception.stdout)
   self.assertIsNone(raised.exception.stderr)
 def test_hosted_key_comes_from_environment_without_keychain(self):
  with patch.dict(os.environ,{'DG_HUB_ACCESS_KEY':'x'*40,'GITHUB_ACTIONS':'true'},clear=True),patch.object(publisher.subprocess,'run') as command:
   self.assertEqual(publisher.access_key(),'x'*40)
   command.assert_not_called()
 def test_missing_or_short_hosted_key_never_uses_keychain(self):
  for env,hosted in [({'GITHUB_ACTIONS':'true'},False),({},True),({'DG_HUB_ACCESS_KEY':'short'},True)]:
   with patch.dict(os.environ,env,clear=True),patch.object(publisher.subprocess,'run') as command:
    with self.assertRaises((RuntimeError,ValueError)):publisher.access_key(hosted=hosted)
    command.assert_not_called()
 def test_finite_partial_source_failure_still_publishes_and_fails_job(self):
  schedule=MagicMock();schedule.run_due.return_value={'ok':False,'failed':['slack']}
  with patch.object(collector_schedule,'CollectorScheduler',return_value=schedule),patch.object(publisher,'publish') as send:
   result=publisher.run_once('synthetic-key',{},'config.json',True)
   self.assertEqual(result,1)
   send.assert_called_once_with('synthetic-key',True,True,None,config_path='config.json')
   schedule.run_due.assert_called_once_with(force=False)
   schedule.close.assert_called_once()
 def test_finite_task_failure_publishes_other_updates_with_retained_tasks(self):
  schedule=MagicMock();schedule.run_due.return_value={'ok':True}
  with patch.object(collector_schedule,'CollectorScheduler',return_value=schedule),patch.object(publisher,'publish',side_effect=[publisher.SnapshotCollectionError(),'digest']) as send:
   self.assertEqual(publisher.run_once('synthetic-key',{},'config.json',True,force=True),1)
   self.assertEqual([call.args[1] for call in send.call_args_list],[True,False])
   schedule.run_due.assert_called_once_with(force=True)
   schedule.close.assert_called_once()
 def test_finite_healthy_run_succeeds(self):
  schedule=MagicMock();schedule.run_due.return_value={'ok':True}
  with patch.object(collector_schedule,'CollectorScheduler',return_value=schedule),patch.object(publisher,'publish'):
   self.assertEqual(publisher.run_once('synthetic-key',{},'config.json',False),0)
 def test_retained_source_status_during_cooldown_is_not_a_new_run_failure(self):
  schedule=MagicMock();schedule.run_due.return_value={'ok':False,'failed':['slack'],'attempt_failed':[]}
  with patch.object(collector_schedule,'CollectorScheduler',return_value=schedule),patch.object(publisher,'publish') as send:
   self.assertEqual(publisher.run_once('synthetic-key',{},'config.json',True),0)
   send.assert_called_once()
 def test_workflow_outputs_are_fixed_booleans_and_never_used_locally(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'output'
   with patch.dict(os.environ,{'GITHUB_ACTIONS':'true','GITHUB_OUTPUT':str(path)},clear=True):
    publisher.workflow_output('snapshot_ready',True)
    publisher.workflow_output('snapshot_published',False)
    self.assertEqual(path.read_text(),'snapshot_ready=true\nsnapshot_published=false\n')
    with self.assertRaises(ValueError):publisher.workflow_output('secret','synthetic-private')
   with patch.dict(os.environ,{'GITHUB_OUTPUT':str(path)},clear=True):
    publisher.workflow_output('snapshot_ready',False)
   self.assertNotIn('snapshot_ready=false',path.read_text())
 def test_prepared_snapshot_is_reported_even_when_remote_publication_fails(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);(root/'.private').mkdir()
   (root/'.private/snapshot.json').write_text('{"synthetic":true}')
   (root/'.private/snapshot.enc.json').write_text('synthetic-ciphertext')
   output=root/'output'
   with patch.object(publisher,'ROOT',root),patch.object(publisher,'run'),patch.dict(os.environ,{'GITHUB_ACTIONS':'true','GITHUB_OUTPUT':str(output)},clear=True),patch.object(publisher,'api',side_effect=[None,{'sha':'a'*40},RuntimeError('publication failed')]):
    with self.assertRaises(RuntimeError):publisher.publish('synthetic-key',True,True,None)
   self.assertEqual(output.read_text(),'snapshot_ready=true\n')
 def test_published_flag_requires_successful_branch_update(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);(root/'.private').mkdir()
   (root/'.private/snapshot.json').write_text('{"synthetic":true}')
   (root/'.private/snapshot.enc.json').write_text('synthetic-ciphertext')
   output=root/'output'
   with patch.object(publisher,'ROOT',root),patch.object(publisher,'run'),patch.dict(os.environ,{'GITHUB_ACTIONS':'true','GITHUB_OUTPUT':str(output)},clear=True),patch.object(publisher,'api',side_effect=[None,{'sha':'a'*40},{'sha':'b'*40},{}]):
    publisher.publish('synthetic-key',True,True,None)
   self.assertEqual(output.read_text(),'snapshot_ready=true\nsnapshot_published=true\n')
 def test_due_only_rejects_resident_or_cached_modes_before_access(self):
  for flag in ('--watch','--cached'):
   with patch.object(publisher,'access_key') as key:
    with self.assertRaises(SystemExit):publisher.main(['--due-only',flag])
    key.assert_not_called()
 def test_only_one_publisher_then_release(self):
  with tempfile.TemporaryDirectory() as directory:
   original=publisher.ROOT;publisher.ROOT=Path(directory)
   try:
    first=publisher.acquire_publisher_lock()
    try:
     with self.assertRaisesRegex(RuntimeError,'already running'):publisher.acquire_publisher_lock()
    finally:first.close()
    second=publisher.acquire_publisher_lock();second.close()
   finally:publisher.ROOT=original
if __name__=='__main__':unittest.main()
