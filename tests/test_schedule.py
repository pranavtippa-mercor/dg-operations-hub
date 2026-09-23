import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))

spec = importlib.util.spec_from_file_location('collector_schedule', Path(__file__).resolve().parents[1]/'scripts/collector_schedule.py')
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class ScheduleTests(unittest.TestCase):
    def test_finite_run_only_waits_for_due_sources_and_persists_cadence(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json'
            path.write_text(json.dumps({'modules':{'ok':True,'next_at':2000,'last_success_at':'prior'}}))
            calls=[]
            def run(name,config):
                calls.append(name)
                return {'ok':True}
            first=scheduler.CollectorScheduler({},runner=run,clock=lambda:1000,status_path=path)
            try:
                result=first.run_due()
                self.assertEqual(result,{'ok':True,'attempted':['slack'],'failed':[], 'attempt_failed':[], 'changed':True})
                self.assertEqual(first.futures,{})
                self.assertEqual(first.status['modules']['last_success_at'],'prior')
            finally:
                first.close()
            second=scheduler.CollectorScheduler({},runner=run,clock=lambda:1001,status_path=path)
            try:
                self.assertEqual(second.run_due()['attempted'],[])
                self.assertEqual(calls,['slack'])
                second.run_due(force=True)
                self.assertEqual(calls.count('modules'),1)
                self.assertEqual(calls.count('slack'),2)
            finally:
                second.close()

    def test_partial_receipt_is_preserved_without_advancing_success(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json'
            path.write_text(json.dumps({'slack':{'ok':True,'last_success_at':'prior','receipt':{'ok':True}}}))
            partial={'ok':False,'errors':2,'threads_complete':3,'missing_roots':1}
            s=scheduler.CollectorScheduler({},runner=lambda name,config:partial if name=='slack' else {'ok':True},clock=lambda:1000,status_path=path)
            try:
                result=s.run_due()
                self.assertFalse(result['ok'])
                self.assertEqual(result['failed'],['slack'])
                self.assertEqual(s.status['slack']['last_attempt_receipt'],partial)
                self.assertEqual(s.status['slack']['last_success_at'],'prior')
                self.assertEqual(s.status['slack']['receipt'],{'ok':True})
                self.assertEqual(s.status['slack']['next_at'],1300)
            finally:
                s.close()

    def test_cooldown_retains_degraded_health_without_a_new_failed_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json'
            path.write_text(json.dumps({'slack':{'ok':False,'last_success_at':'prior','next_at':1300},
                                        'modules':{'ok':True,'next_at':2000}}))
            s=scheduler.CollectorScheduler({},runner=lambda *_:self.fail('Cooldown source was retried'),
                                           clock=lambda:1100,status_path=path)
            try:
                result=s.run_due()
                self.assertFalse(result['ok'])
                self.assertEqual(result['failed'],['slack'])
                self.assertEqual(result['attempt_failed'],[])
                self.assertEqual(result['attempted'],[])
                self.assertEqual(s.status['slack']['last_success_at'],'prior')
            finally:
                s.close()

    def test_separate_cadences_and_no_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            now, calls = [1000], []
            def run(name, config):
                calls.append(name)
                return {'complete': True}
            s = scheduler.CollectorScheduler({'schedules': {'modules_seconds': 3600, 'slack_seconds': 900}}, runner=run, clock=lambda: now[0], status_path=Path(folder)/'status.json')
            try:
                s.refresh_all()
                self.assertCountEqual(calls, ['modules', 'slack'])
                s.tick()
                self.assertEqual(len(calls), 2)
                now[0] += 901
                s.tick()
                self.assertEqual(set(s.futures), {'slack'})
                s.tick()  # Running or completed, it cannot run twice in this interval.
            finally:
                s.close()
            self.assertEqual(calls.count('slack'), 2)
            self.assertEqual(calls.count('modules'), 1)

    def test_failure_retains_last_success_and_backs_off(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'status.json'
            path.write_text(json.dumps({'modules': {'ok': True, 'last_success_at': 'old', 'next_at': 0}}))
            def run(name, config):
                if name == 'modules':
                    raise RuntimeError('private source content')
                return {'complete': True}
            s = scheduler.CollectorScheduler({}, runner=run, clock=lambda: 1000, status_path=path)
            try:
                with self.assertRaises(RuntimeError):
                    s.refresh_all()
                self.assertEqual(s.status['modules']['last_success_at'], 'old')
                self.assertEqual(s.status['modules']['next_at'], 1300)
                self.assertNotIn('private source content', path.read_text())
                self.assertFalse(s.status['modules']['ok'])
            finally:
                s.close()

    def test_partial_receipt_cannot_pass_retirement_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            s = scheduler.CollectorScheduler({}, runner=lambda name, config: {'ok': name != 'slack'}, clock=lambda: 1000, status_path=Path(folder)/'status.json')
            try:
                with self.assertRaises(RuntimeError):
                    s.refresh_all()
                self.assertFalse(s.status['slack']['ok'])
                self.assertNotIn('last_success_at', s.status['slack'])
            finally:
                s.close()


if __name__ == '__main__':
    unittest.main()
