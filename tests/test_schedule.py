import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('collector_schedule', Path(__file__).resolve().parents[1]/'scripts/collector_schedule.py')
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class ScheduleTests(unittest.TestCase):
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
