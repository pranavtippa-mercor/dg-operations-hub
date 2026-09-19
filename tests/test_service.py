import fcntl
import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('service',Path(__file__).resolve().parents[1]/'scripts/service.py')
service=importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ServiceTests(unittest.TestCase):
    def test_waits_for_process_disappearance_without_additional_signals(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(service,'PRIVATE',Path(folder)), \
             patch.object(service,'active_pid',side_effect=[123,123,None,None]), \
             patch.object(service.os,'kill') as kill, \
             patch.object(service.time,'monotonic',return_value=0), \
             patch.object(service.time,'sleep') as sleep:
            service.stop(wait=True,timeout=300)
            kill.assert_called_once_with(123,signal.SIGTERM)
            sleep.assert_called_once_with(1)

    def test_lock_must_be_free_even_after_process_disappears(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(service,'PRIVATE',Path(folder)), \
             patch.object(service,'active_pid',return_value=None):
            with (Path(folder)/'publisher.lock').open('a') as lock:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertFalse(service.publisher_stopped())
            self.assertTrue(service.publisher_stopped())

    def test_lingering_process_times_out_without_forced_kill(self):
        with patch.object(service,'active_pid',return_value=123), \
             patch.object(service.os,'kill') as kill, \
             patch.object(service.time,'monotonic',side_effect=[0,0,1,2]), \
             patch.object(service.time,'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'shutdown was not verified'):
                service.stop(wait=True,timeout=2)
            self.assertEqual([call.args[0] for call in sleep.call_args_list],[1,1])
            kill.assert_called_once_with(123,signal.SIGTERM)

    def test_nonwaiting_stop_preserves_immediate_behavior(self):
        with patch.object(service,'active_pid',return_value=123), \
             patch.object(service.os,'kill') as kill, \
             patch.object(service,'publisher_stopped') as stopped, \
             patch.object(service.time,'sleep') as sleep:
            service.stop()
            kill.assert_called_once_with(123,signal.SIGTERM)
            stopped.assert_not_called()
            sleep.assert_not_called()

    def test_disappearance_before_signal_is_safe(self):
        with patch.object(service,'active_pid',return_value=123), \
             patch.object(service.os,'kill',side_effect=ProcessLookupError), \
             patch.object(service,'publisher_stopped',return_value=True):
            service.stop(wait=True)

    def test_cloud_marker_prevents_duplicate_local_start(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(service,'PRIVATE',Path(folder)), \
             patch.object(service.subprocess,'Popen') as launch, \
             patch.object(service,'active_pid') as active:
            (Path(folder)/'hosted-active.json').write_text(json.dumps({'active':True}))
            service.start()
            launch.assert_not_called()
            active.assert_not_called()

    def test_stop_cli_reports_timeout_as_failure(self):
        with patch.object(service,'stop',side_effect=RuntimeError('Shutdown incomplete.')) as stop:
            self.assertEqual(service.main(['stop','--wait','--timeout','300']),1)
            stop.assert_called_once_with(wait=True,timeout=300)


if __name__=='__main__':
    unittest.main()
