import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import refresh_report


class RefreshReportTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        self.env = dict(PUBLISH_ENABLED='true', RESTORE_OUTCOME='success', SAVE_OUTCOME='success',
                        SNAPSHOT_READY='true', SNAPSHOT_PUBLISHED='true', VERIFY_OUTCOME='success',
                        COLLECT_OUTCOME='success')
        self.snapshot = {'sources': {key: {'at': self.now.isoformat()} for key in
                                    ('tasks', 'activity', 'slack', 'modules')}, 'private': 'never-export'}
        self.status = {key: {'last_success_at': self.now.isoformat()} for key in ('slack', 'modules')}

    def flags(self):
        return refresh_report.report(self.env, self.snapshot, collector_status=self.status, now=self.now)['conditions']

    def test_partial_collection_with_fresh_retained_sources_is_warning_only(self):
        self.env['COLLECT_OUTCOME'] = 'failure'
        self.assertFalse(any(self.flags().values()))
        self.assertNotIn('never-export', str(refresh_report.report(self.env, self.snapshot)))

    def test_source_age_thresholds_and_activity_are_independent(self):
        for condition, (names, seconds) in refresh_report.SOURCE_LIMITS.items():
            for name in names:
                self.snapshot['sources'][name]['at'] = (self.now - timedelta(seconds=seconds - 1)).isoformat()
                self.assertFalse(self.flags()[condition])
                self.snapshot['sources'][name]['at'] = (self.now - timedelta(seconds=seconds)).isoformat()
                self.assertTrue(self.flags()[condition])
                self.snapshot['sources'][name]['at'] = self.now.isoformat()

    def test_missing_invalid_and_future_source_dates_are_not_fresh(self):
        for value in (None, 'bad', '2026-09-23T00:00:00', (self.now + timedelta(minutes=3)).isoformat()):
            self.snapshot['sources']['slack']['at'] = value
            self.assertTrue(self.flags()['source_slack'])

    def test_partial_source_progress_does_not_hide_an_old_complete_collection(self):
        self.status['slack']['last_success_at'] = (self.now - timedelta(hours=2)).isoformat()
        self.status['slack']['ok'] = False
        self.assertTrue(self.flags()['source_slack'])
        self.assertFalse(self.flags()['source_modules'])
        self.status = None
        self.assertTrue(self.flags()['source_slack'])
        self.assertTrue(self.flags()['source_modules'])

    def test_failed_restore_does_not_invent_other_failures(self):
        self.env['RESTORE_OUTCOME'] = 'failure'
        self.assertEqual([k for k, v in self.flags().items() if v], ['restore'])
        result=refresh_report.report(self.env,self.snapshot,collector_status=self.status,now=self.now)
        self.assertEqual([k for k,v in result['observed'].items() if v],['restore'])

    def test_skipped_decryption_is_unknown_and_cannot_reset_integrity_incident(self):
        self.env.update(SNAPSHOT_READY='false', VERIFY_OUTCOME='skipped', COLLECT_OUTCOME='failure')
        result=refresh_report.report(self.env,self.snapshot,collector_status=self.status,now=self.now)
        self.assertFalse(result['observed']['integrity'])
        self.assertTrue(result['observed']['publication'])

    def test_publication_checkpoint_and_integrity_are_separate(self):
        for environment, condition in [('SAVE_OUTCOME', 'checkpoint'), ('SNAPSHOT_PUBLISHED', 'publication'),
                                       ('VERIFY_OUTCOME', 'integrity')]:
            original = self.env[environment]
            self.env[environment] = 'failure'
            self.assertEqual([k for k, v in self.flags().items() if v], [condition])
            self.env[environment] = original

    def test_unreadable_restored_snapshot_is_integrity_failure(self):
        for value in (None, [], {}, {'sources': []}):
            self.snapshot = value
            self.assertTrue(self.flags()['integrity'])

    def test_probe_verification_and_gated_candidates_do_not_change_incidents(self):
        for env in ({'PROBE_ONLY': 'true'}, {'VERIFY_ONLY': 'true'}, {'PUBLISH_ENABLED': 'false'}):
            result = refresh_report.report({**self.env, **env}, None, now=self.now)
            self.assertNotEqual(result['mode'], 'refresh')
            self.assertFalse(any(result['conditions'].values()))


if __name__ == '__main__':
    unittest.main()
