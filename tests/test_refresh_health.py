import copy
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('refresh_health', ROOT / 'scripts/refresh_health.py')
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def report(*active, mode='refresh', unobserved=()):
    return {'version': 1, 'mode': mode,
            'conditions': {key: key in active for key in H.CONDITIONS},
            'observed': {key: key not in unobserved for key in H.CONDITIONS}}


def record(*active, acknowledged=(), alerted=(), minutes=5, notified_at=None, unobserved=()):
    return {'active': {key: key in active for key in H.CONDITIONS},
            'observed': {key: key not in unobserved for key in H.CONDITIONS},
            'acknowledged': {key: key in acknowledged for key in H.CONDITIONS},
            'alerted': {key: key in alerted for key in H.CONDITIONS},
            'started_at': NOW - timedelta(minutes=minutes), 'notified_at': notified_at}


class History:
    started_at = NOW

    def __init__(self, records=(), alerts=None):
        self.records = list(records)
        self.alerts = alerts or {}
        self.read_count = 0

    def recent(self):
        for item in self.records:
            self.read_count += 1
            yield item

    def last_alert(self, key):
        return self.alerts.get(key)


def run_metadata(run_id, minutes=5, conclusion='success', attempt=1):
    started = NOW - timedelta(minutes=minutes)
    return {'id': run_id, 'workflow_id': 42, 'run_attempt': attempt,
            'path': '.github/workflows/refresh.yml', 'head_branch': 'main',
            'event': 'schedule', 'status': 'completed', 'conclusion': conclusion,
            'created_at': iso(started), 'run_started_at': iso(started)}


def job_metadata(run, state, eligible=True, notification='skipped'):
    steps = [{'name': H.EVALUATION_MARKER,
              'conclusion': 'success' if eligible else 'skipped'}]
    for key in H.CONDITIONS:
        for kind, field in (('impaired', 'active'), ('acknowledged', 'acknowledged'),
                            ('alerted', 'alerted'), ('observed', 'observed')):
            steps.append({'name': H.marker(key, kind),
                          'conclusion': 'success' if state[field][key] else 'skipped'})
    steps.append({'name': H.NOTIFY_MARKER, 'conclusion': notification,
                  'completed_at': iso(state['notified_at'] or state['started_at'] + timedelta(minutes=1))})
    return {'name': 'refresh', 'run_id': run['id'], 'run_attempt': run['run_attempt'],
            'head_branch': 'main', 'status': 'completed', 'conclusion': run['conclusion'], 'steps': steps}


class API:
    env = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '1000',
           'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REF': 'refs/heads/main',
           'GITHUB_WORKFLOW_REF': 'owner/repo/.github/workflows/refresh.yml@refs/heads/main'}

    def __init__(self, runs=(), jobs=None):
        self.runs = list(runs)
        self.jobs = jobs or {}
        self.calls = []

    def __call__(self, route):
        self.calls.append(route)
        if route == '/actions/runs/1000':
            return run_metadata(1000, minutes=0)
        if route.startswith('/actions/workflows/42/runs?'):
            page = int(route.rsplit('page=', 1)[1])
            start = (page - 1) * H.PAGE_SIZE
            return {'workflow_runs': self.runs[start:start + H.PAGE_SIZE]}
        for run in self.runs:
            if route == f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100&page=1":
                jobs = self.jobs.get(run['id'], [])
                return {'total_count': len(jobs), 'jobs': jobs}
        raise AssertionError('Unexpected API route')

    def history(self):
        return H.ActionsHistory(self.env, self)


class DecisionTests(unittest.TestCase):
    def evaluate(self, current, records=(), alerts=None):
        history = History(records, alerts)
        return H.decide(current, lambda: history)

    def test_first_two_checkpoint_failures_warn_third_alerts(self):
        current = report('checkpoint')
        first = self.evaluate(current)
        second = self.evaluate(current, [record('checkpoint')])
        third = self.evaluate(current, [record('checkpoint'), record('checkpoint', minutes=10)])
        self.assertFalse(first['notify'])
        self.assertFalse(second['notify'])
        self.assertTrue(third['notify'])
        self.assertTrue(third['acknowledged']['checkpoint'])

    def test_thirty_minutes_continuous_failure_alerts_even_with_two_runs(self):
        decision = self.evaluate(report('restore'), [record('restore', minutes=31)])
        self.assertTrue(decision['alert']['restore'])

    def test_actual_recovery_resets_incident_even_if_old_alert_was_recent(self):
        decision = self.evaluate(report('checkpoint'), [record(), record('checkpoint', acknowledged=('checkpoint',), minutes=10)],
                                 {'checkpoint': NOW - timedelta(minutes=10)})
        self.assertFalse(decision['notify'])
        self.assertFalse(decision['acknowledged']['checkpoint'])

    def test_recent_acknowledged_incident_suppresses_repeat(self):
        history = History([record('checkpoint', acknowledged=('checkpoint',))],
                          {'checkpoint': NOW - timedelta(hours=1)})
        decision = H.decide(report('checkpoint'), lambda: history)
        self.assertFalse(decision['notify'])
        self.assertTrue(decision['acknowledged']['checkpoint'])
        self.assertEqual(history.read_count, 1)

    def test_restore_failure_with_unknown_sources_preserves_stale_incident_acknowledgment(self):
        unknown_sources = tuple(key for key in H.CONDITIONS if key != 'restore')
        prior = [record('restore', unobserved=unknown_sources),
                 record('source_tasks', acknowledged=('source_tasks',), minutes=20)]
        decision = self.evaluate(report('source_tasks'), prior,
                                 {'source_tasks': NOW - timedelta(minutes=19)})
        self.assertFalse(decision['notify'])
        self.assertTrue(decision['acknowledged']['source_tasks'])

    def test_skipped_decryption_after_encryption_failure_does_not_reset_integrity_incident(self):
        prior = [record('publication', unobserved=('integrity',)),
                 record('integrity', acknowledged=('integrity',), minutes=20)]
        decision = self.evaluate(report('integrity'), prior,
                                 {'integrity': NOW - timedelta(minutes=19)})
        self.assertFalse(decision['notify'])
        self.assertTrue(decision['acknowledged']['integrity'])

    def test_unknown_run_is_not_a_failed_attempt(self):
        prior = [record('restore', unobserved=('checkpoint',)), record('checkpoint', minutes=10)]
        decision = self.evaluate(report('checkpoint'), prior)
        self.assertFalse(decision['notify'])
        self.assertFalse(decision['acknowledged']['checkpoint'])

    def test_unknown_gap_disables_continuous_time_shortcut_but_not_three_observed_failures(self):
        prior = [record('restore', unobserved=('checkpoint',)), record('checkpoint', minutes=40)]
        second_observation = self.evaluate(report('checkpoint'), prior)
        self.assertFalse(second_observation['notify'])
        third_observation = self.evaluate(report('checkpoint'), prior + [record('checkpoint', minutes=50)])
        self.assertTrue(third_observation['notify'])

    def test_real_recovery_before_unknown_runs_still_resets_incident(self):
        prior = [record('restore', unobserved=('checkpoint',)), record(minutes=10),
                 record('checkpoint', acknowledged=('checkpoint',), minutes=20)]
        decision = self.evaluate(report('checkpoint'), prior,
                                 {'checkpoint': NOW - timedelta(minutes=19)})
        self.assertFalse(decision['notify'])
        self.assertFalse(decision['acknowledged']['checkpoint'])

    def test_current_unknown_conditions_neither_alert_nor_claim_recovery(self):
        def forbidden():
            raise AssertionError('Unexpected history query')
        decision = H.decide(report(unobserved=('integrity',)), forbidden)
        self.assertFalse(decision['active']['integrity'])
        self.assertFalse(decision['observed']['integrity'])
        self.assertFalse(decision['notify'])
        self.assertIn('remain unknown', H.render_summary(decision))
        self.assertNotIn('All monitored refresh conditions are healthy', H.render_summary(decision))
        self.assertEqual(H.output_values(decision)['observed_integrity'], 'false')

    def test_unknown_history_window_does_not_invent_an_acknowledged_incident(self):
        history = History([record(unobserved=('checkpoint', 'integrity'), minutes=5 * (i + 1))
                           for i in range(H.MAX_RUNS)])
        decision = H.decide(report('checkpoint', 'integrity'), lambda: history)
        self.assertFalse(decision['alert']['checkpoint'])
        self.assertTrue(decision['alert']['integrity'])
        self.assertFalse(decision['acknowledged']['checkpoint'])
        self.assertEqual(history.read_count, H.MAX_RUNS)

    def test_six_hour_reminder_and_missing_old_alert_both_notify(self):
        prior = [record('publication', acknowledged=('publication',))]
        at_boundary = self.evaluate(report('publication'), prior, {'publication': NOW - timedelta(hours=6)})
        outside_window = self.evaluate(report('publication'), prior)
        self.assertTrue(at_boundary['alert']['publication'])
        self.assertTrue(outside_window['alert']['publication'])

    def test_new_distinct_condition_is_not_hidden_by_an_acknowledged_one(self):
        decision = self.evaluate(report('checkpoint', 'source_tasks'),
                                 [record('checkpoint', acknowledged=('checkpoint',))],
                                 {'checkpoint': NOW - timedelta(minutes=20)})
        self.assertFalse(decision['alert']['checkpoint'])
        self.assertTrue(decision['alert']['source_tasks'])

    def test_source_age_and_integrity_conditions_alert_immediately(self):
        for key in H.IMMEDIATE:
            with self.subTest(key=key):
                self.assertTrue(self.evaluate(report(key))['alert'][key])

    def test_healthy_and_verification_runs_do_not_query_history(self):
        def forbidden():
            raise AssertionError('Unexpected history query')
        healthy = H.decide(report(), forbidden)
        self.assertTrue(healthy['eligible'])
        for mode in ('verification', 'probe'):
            decision = H.decide(report('integrity', mode=mode), forbidden)
            self.assertFalse(decision['eligible'])
            self.assertFalse(decision['notify'])

    def test_history_failure_is_visible_and_never_prints_exception_content(self):
        def broken():
            raise RuntimeError('SECRET-private-provider-response')
        decision = H.safe_decision(report('checkpoint'), broken)
        self.assertTrue(decision['monitor_error'])
        self.assertTrue(decision['notify'])
        self.assertNotIn('SECRET', H.render_summary(decision))
        self.assertNotIn('SECRET', json.dumps(H.output_values(decision)))

    def test_report_schema_rejects_private_extras_strings_and_missing_conditions(self):
        samples = [None, {'version': 1, 'mode': 'refresh', 'conditions': {}}, report()]
        samples[-1]['conditions']['checkpoint'] = 'SECRET-token'
        extra = report()
        extra['private_body'] = 'SECRET-body'
        samples.append(extra)
        samples.append(report('integrity', unobserved=('integrity',)))
        for sample in samples:
            with self.subTest(sample=sample is None):
                decision = H.safe_decision(sample)
                self.assertTrue(decision['monitor_error'])
                self.assertTrue(decision['notify'])
                self.assertNotIn('SECRET', H.render_summary(decision))


class ActionsHistoryTests(unittest.TestCase):
    def test_latest_run_attempt_is_pinned_and_unrelated_or_newer_runs_are_excluded(self):
        trusted = run_metadata(999, attempt=2)
        wrong_workflow = {**run_metadata(998), 'workflow_id': 43}
        wrong_branch = {**run_metadata(997), 'head_branch': 'other'}
        newer = run_metadata(1001, minutes=1)
        api = API([newer, trusted, wrong_workflow, wrong_branch],
                  {999: [job_metadata(trusted, record('checkpoint'))]})
        result = list(api.history().recent())
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['active']['checkpoint'])
        self.assertIn('/actions/runs/999/attempts/2/jobs?per_page=100&page=1', api.calls)
        self.assertFalse(any('/runs/998/' in call or '/runs/997/' in call or '/runs/1001/' in call for call in api.calls))

    def test_verification_run_does_not_reset_health(self):
        verification, previous = run_metadata(999), run_metadata(998, minutes=10)
        api = API([verification, previous], {
            999: [job_metadata(verification, record(), eligible=False)],
            998: [job_metadata(previous, record('restore', minutes=10))]})
        records = list(api.history().recent())
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['active']['restore'])

    def test_observed_markers_preserve_acknowledgment_through_real_api_history(self):
        unknown_run = run_metadata(999)
        alert_run = run_metadata(998, minutes=20, conclusion='failure')
        unknown = record('restore', unobserved=('source_tasks',))
        old_alert = record('source_tasks', acknowledged=('source_tasks',), alerted=('source_tasks',),
                           minutes=20, notified_at=NOW - timedelta(minutes=19))
        api = API([unknown_run, alert_run], {
            999: [job_metadata(unknown_run, unknown)],
            998: [job_metadata(alert_run, old_alert, notification='failure')]})
        decision = H.decide(report('source_tasks'), api.history)
        self.assertTrue(decision['acknowledged']['source_tasks'])
        self.assertFalse(decision['notify'])

    def test_legacy_policy_boundary_stops_after_one_job_without_inventing_recovery(self):
        runs = [run_metadata(999 - i, minutes=5 * (i + 1)) for i in range(100)]
        legacy = job_metadata(runs[0], record())
        legacy['steps'] = [{'name': 'Save encrypted collector progress', 'conclusion': 'failure'}]
        api = API(runs, {999: [legacy]})
        decision = H.decide(report('checkpoint'), api.history)
        self.assertFalse(decision['notify'])
        self.assertFalse(decision['acknowledged']['checkpoint'])
        self.assertEqual(sum('/jobs?' in call for call in api.calls), 1)

    def test_prepolicy_run_cannot_prove_acknowledgment_or_suppress_an_alert(self):
        latest, legacy_run = run_metadata(999), run_metadata(998, minutes=10, conclusion='failure')
        acknowledged = record('checkpoint', acknowledged=('checkpoint',))
        legacy = job_metadata(legacy_run, record())
        legacy['steps'] = [{'name': 'Save encrypted collector progress', 'conclusion': 'failure'}]
        api = API([latest, legacy_run], {999: [job_metadata(latest, acknowledged)], 998: [legacy]})
        decision = H.safe_decision(report('checkpoint'), api.history)
        self.assertTrue(decision['monitor_error'])
        self.assertTrue(decision['notify'])

    def test_alert_marker_without_failed_final_notification_is_not_acknowledged(self):
        prior = run_metadata(999, conclusion='cancelled')
        state = record('checkpoint', acknowledged=('checkpoint',), alerted=('checkpoint',))
        api = API([prior], {999: [job_metadata(prior, state, notification='cancelled')]})
        saved = list(api.history().recent())[0]
        self.assertFalse(saved['acknowledged']['checkpoint'])
        self.assertFalse(saved['alerted']['checkpoint'])

    def test_actual_notification_timestamp_drives_reminder(self):
        prior = run_metadata(999, conclusion='failure')
        state = record('integrity', acknowledged=('integrity',), alerted=('integrity',),
                       notified_at=NOW - timedelta(minutes=4))
        api = API([prior], {999: [job_metadata(prior, state, notification='failure')]})
        history = api.history()
        self.assertEqual(history.last_alert('integrity'), NOW - timedelta(minutes=4))

    def test_missing_or_duplicate_marker_metadata_fails_safe(self):
        prior = run_metadata(999)
        for mutation in ('missing', 'duplicate', 'wrong_attempt'):
            job = job_metadata(prior, record('checkpoint'))
            if mutation == 'missing':
                job['steps'].pop(1)
            elif mutation == 'duplicate':
                job['steps'].append(copy.deepcopy(job['steps'][0]))
            else:
                job['run_attempt'] = 99
            api = API([prior], {999: [job]})
            decision = H.safe_decision(report('checkpoint'), api.history)
            self.assertTrue(decision['monitor_error'])
            self.assertTrue(decision['notify'])

    def test_missing_observed_marker_or_active_unobserved_condition_fails_safe(self):
        prior = run_metadata(999)
        for invalid in ('missing', 'contradiction'):
            state = record('checkpoint', unobserved=('checkpoint',) if invalid == 'contradiction' else ())
            job = job_metadata(prior, state)
            if invalid == 'missing':
                job['steps'] = [step for step in job['steps'] if step['name'] != H.marker('checkpoint', 'observed')]
            api = API([prior], {999: [job]})
            decision = H.safe_decision(report('checkpoint'), api.history)
            self.assertTrue(decision['monitor_error'])
            self.assertTrue(decision['notify'])

    def test_future_notification_timestamp_fails_safe(self):
        prior = run_metadata(999, conclusion='failure')
        state = record('integrity', acknowledged=('integrity',), alerted=('integrity',),
                       notified_at=NOW + timedelta(minutes=1))
        api = API([prior], {999: [job_metadata(prior, state, notification='failure')]})
        decision = H.safe_decision(report('integrity'), api.history)
        self.assertTrue(decision['monitor_error'])
        self.assertTrue(decision['notify'])

    def test_reminder_reads_failure_jobs_only_and_reuses_cached_records(self):
        runs = [run_metadata(999 - i, minutes=5 * (i + 1)) for i in range(20)]
        runs[-1]['conclusion'] = 'failure'
        current_state = record('checkpoint', acknowledged=('checkpoint',))
        alert_state = record('checkpoint', acknowledged=('checkpoint',), alerted=('checkpoint',),
                             minutes=100, notified_at=NOW - timedelta(minutes=99))
        api = API(runs, {999: [job_metadata(runs[0], current_state)],
                         980: [job_metadata(runs[-1], alert_state, notification='failure')]})
        decision = H.decide(report('checkpoint'), api.history)
        self.assertFalse(decision['notify'])
        job_reads = [call for call in api.calls if '/jobs?' in call]
        self.assertEqual(len(job_reads), 2)

    def test_history_pagination_is_bounded_and_does_not_download_all_jobs(self):
        runs = [run_metadata(999 - i, minutes=5 * (i + 1)) for i in range(120)]
        api = API(runs)
        history = api.history()
        self.assertEqual(len(history.runs), H.MAX_RUNS)
        self.assertTrue(history.complete_window)
        self.assertEqual(sum('/actions/workflows/' in call for call in api.calls), 2)
        self.assertFalse(any('/jobs?' in call for call in api.calls))

    def test_unknown_history_horizon_never_silently_suppresses_acknowledged_incident(self):
        runs = [run_metadata(999 - i, minutes=(i + 1) / 10) for i in range(100)]
        state = record('checkpoint', acknowledged=('checkpoint',), minutes=.1)
        api = API(runs, {999: [job_metadata(runs[0], state)]})
        decision = H.safe_decision(report('checkpoint'), api.history)
        self.assertTrue(decision['monitor_error'])
        self.assertTrue(decision['notify'])

    def test_invalid_environment_and_malicious_metadata_are_never_used_as_routes(self):
        for field, value in (('GITHUB_REPOSITORY', 'owner/repo?token=SECRET'),
                             ('GITHUB_RUN_ID', '../SECRET'),
                             ('GITHUB_WORKFLOW_REF', 'owner/repo/.github/workflows/evil.yml@refs/heads/main')):
            env = {**API.env, field: value}
            api = API()
            with self.assertRaises(H.HealthError):
                H.ActionsHistory(env, api)
            self.assertEqual(api.calls, [])
        api = API([run_metadata(999)])
        api.runs[0]['id'] = '999/SECRET'
        with self.assertRaises(H.HealthError):
            api.history()

    def test_cli_writes_fixed_boolean_outputs_and_summary_without_failing_early(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / 'report.json'
            output = Path(directory) / 'output'
            summary = Path(directory) / 'summary'
            report_path.write_text(json.dumps(report('checkpoint')))
            decision = H.safe_decision(report('checkpoint'), lambda: (_ for _ in ()).throw(RuntimeError('SECRET')))
            with patch.object(H, 'safe_decision', return_value=decision), patch.dict(os.environ, {
                    'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)}):
                self.assertEqual(H.main(['--report', str(report_path)]), 0)
            self.assertIn('notify=true\n', output.read_text())
            self.assertIn('monitor_error=true\n', output.read_text())
            self.assertNotIn('SECRET', output.read_text() + summary.read_text())


if __name__ == '__main__':
    unittest.main()
