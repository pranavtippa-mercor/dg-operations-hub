#!/usr/bin/env python3
"""Decide when refresh failures need a native Actions notification.

Only sanitized booleans and Actions metadata are read. Fixed successful marker
steps retain incident continuity even when encrypted collector-state saves fail.
The caller records every output marker, then fails a separate notification step.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


CONDITIONS = ('checkpoint', 'restore', 'publication', 'source_tasks',
              'source_slack', 'source_modules', 'integrity')
IMMEDIATE = frozenset(('source_tasks', 'source_slack', 'source_modules', 'integrity'))
LABELS = {'checkpoint': 'Collector progress save', 'restore': 'Collector state restore',
          'publication': 'Dashboard publication', 'source_tasks': 'Task evidence freshness',
          'source_slack': 'Slack evidence freshness', 'source_modules': 'Module evidence freshness',
          'integrity': 'Dashboard decryption and integrity'}
EVALUATION_MARKER = 'Health v1: evaluation recorded'
NOTIFY_MARKER = 'Health v1: notify'
REMINDER = timedelta(hours=6)
PERSISTENCE = timedelta(minutes=30)
MAX_RUNS = 100
PAGE_SIZE = 50
MAX_API_BYTES = 4 * 1024 * 1024
LEGACY_RECORD = object()
CONCLUSIONS = frozenset(('success', 'failure', 'cancelled', 'timed_out', 'skipped',
                         'neutral', 'action_required', 'stale', 'startup_failure'))


class HealthError(RuntimeError):
    """An intentionally content-free monitor failure."""


def marker(key, kind):
    return f'Health v1: {key} {kind}'


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise HealthError()
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise HealthError() from None
    if result.tzinfo is None:
        raise HealthError()
    return result.astimezone(timezone.utc)


def positive_int(value):
    if type(value) is not int or value <= 0:
        raise HealthError()
    return value


def validate_report(report):
    if (not isinstance(report, dict) or set(report) != {'version', 'mode', 'conditions', 'observed'}
            or type(report['version']) is not int or report['version'] != 1
            or report['mode'] not in ('refresh', 'verification', 'probe')
            or not isinstance(report['conditions'], dict)
            or set(report['conditions']) != set(CONDITIONS)
            or any(type(value) is not bool for value in report['conditions'].values())
            or not isinstance(report['observed'], dict)
            or set(report['observed']) != set(CONDITIONS)
            or any(type(value) is not bool for value in report['observed'].values())
            or any(report['conditions'][key] and not report['observed'][key] for key in CONDITIONS)):
        raise HealthError()
    return report


def empty_decision(report=None):
    active = dict.fromkeys(CONDITIONS, False)
    observed = dict.fromkeys(CONDITIONS, False)
    eligible = report is not None and report['mode'] == 'refresh'
    if eligible:
        observed.update(report['observed'])
        active.update({key: report['conditions'][key] and observed[key] for key in CONDITIONS})
    return {'eligible': eligible, 'active': active, 'observed': observed,
            'alert': dict.fromkeys(CONDITIONS, False),
            'acknowledged': dict.fromkeys(CONDITIONS, False),
            'monitor_error': False, 'notify': False,
            'reasons': dict.fromkeys(CONDITIONS, 'healthy')}


def github_json(repo, route):
    # The route is built exclusively from validated identifiers and constants.
    result = subprocess.run(['gh', 'api', 'repos/' + repo + route],
                            capture_output=True, timeout=25, check=False)
    if result.returncode or len(result.stdout) > MAX_API_BYTES:
        raise HealthError()
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise HealthError() from None


class ActionsHistory:
    """Bounded metadata reads; job details are fetched only when needed."""

    def __init__(self, env=None, api=None):
        env = os.environ if env is None else env
        self.repo = env.get('GITHUB_REPOSITORY', '')
        raw_id, raw_attempt = env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_RUN_ATTEMPT', '')
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repo)
                or not re.fullmatch(r'[1-9][0-9]{0,19}', raw_id)
                or not re.fullmatch(r'[1-9][0-9]{0,7}', raw_attempt)
                or env.get('GITHUB_REF') != 'refs/heads/main'
                or env.get('GITHUB_WORKFLOW_REF') != self.repo + '/.github/workflows/refresh.yml@refs/heads/main'):
            raise HealthError()
        self.current_id, self.current_attempt = int(raw_id), int(raw_attempt)
        self.api = api or (lambda route: github_json(self.repo, route))
        current = self.api('/actions/runs/' + str(self.current_id))
        self._validate_run(current, current=True)
        self.workflow_id = current['workflow_id']
        self.started_at = timestamp(current['run_started_at'])
        self.created_at = timestamp(current['created_at'])
        self.runs = []
        self.records = {}
        self.complete_window = False
        self._load_runs()

    def _validate_run(self, run, current=False):
        if not isinstance(run, dict):
            raise HealthError()
        for field in ('id', 'workflow_id', 'run_attempt'):
            positive_int(run.get(field))
        if (run.get('path') != '.github/workflows/refresh.yml'
                or run.get('head_branch') != 'main'
                or run.get('event') not in ('schedule', 'workflow_dispatch')):
            raise HealthError()
        created, started = timestamp(run.get('created_at')), timestamp(run.get('run_started_at'))
        if started < created:
            raise HealthError()
        if current:
            if run['id'] != self.current_id or run['run_attempt'] != self.current_attempt:
                raise HealthError()
        elif run.get('status') != 'completed' or run.get('conclusion') not in CONCLUSIONS:
            raise HealthError()

    def _load_runs(self):
        seen = set()
        cutoff = self.started_at - REMINDER
        for page in range(1, MAX_RUNS // PAGE_SIZE + 1):
            route = (f'/actions/workflows/{self.workflow_id}/runs?branch=main&status=completed'
                     f'&per_page={PAGE_SIZE}&page={page}')
            payload = self.api(route)
            if (not isinstance(payload, dict) or not isinstance(payload.get('workflow_runs'), list)
                    or len(payload['workflow_runs']) > PAGE_SIZE):
                raise HealthError()
            batch = payload['workflow_runs']
            for run in batch:
                if not isinstance(run, dict):
                    raise HealthError()
                # Do not use another workflow, branch, current run, or a newer run
                # as evidence that an incident recovered or was acknowledged.
                run_id = positive_int(run.get('id'))
                if (run.get('workflow_id') != self.workflow_id or run.get('head_branch') != 'main'
                        or run.get('path') != '.github/workflows/refresh.yml'
                        or run.get('event') not in ('schedule', 'workflow_dispatch')
                        or run_id >= self.current_id):
                    continue
                self._validate_run(run)
                if (timestamp(run['created_at']) > self.created_at
                        or timestamp(run['run_started_at']) >= self.started_at):
                    continue
                if run_id in seen:
                    raise HealthError()
                seen.add(run_id)
                self.runs.append(run)
            # Results are ordered by creation, but an older run can be rerun.
            # Keep both bounded pages unless the API has exhausted its history.
            if len(batch) < PAGE_SIZE:
                self.complete_window = True
                break
        self.runs.sort(key=lambda run: timestamp(run['run_started_at']), reverse=True)
        if self.runs and min(timestamp(run['created_at']) for run in self.runs) <= cutoff:
            self.complete_window = True

    def _record(self, run):
        if run['id'] in self.records:
            return self.records[run['id']]
        route = f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100&page=1"
        payload = self.api(route)
        if (not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list)
                or type(payload.get('total_count')) is not int
                or payload['total_count'] != len(payload['jobs']) or len(payload['jobs']) > 100
                or any(not isinstance(job, dict) for job in payload['jobs'])):
            raise HealthError()
        jobs = [job for job in payload['jobs'] if isinstance(job, dict) and job.get('name') == 'refresh']
        if not jobs:
            self.records[run['id']] = None
            return None
        if len(jobs) != 1:
            raise HealthError()
        job = jobs[0]
        if (type(job.get('run_id')) is not int or type(job.get('run_attempt')) is not int
                or job['run_id'] != run['id'] or job['run_attempt'] != run['run_attempt']
                or job.get('head_branch') != 'main' or job.get('status') != 'completed'
                or job.get('conclusion') not in CONCLUSIONS or not isinstance(job.get('steps'), list)
                or len(job['steps']) > 100):
            raise HealthError()
        by_name = {}
        for step in job['steps']:
            if not isinstance(step, dict) or not isinstance(step.get('name'), str):
                raise HealthError()
            name = step['name']
            if not name.startswith('Health v1:'):
                continue
            if name in by_name or step.get('conclusion') not in CONCLUSIONS:
                raise HealthError()
            by_name[name] = step
        evaluation = by_name.get(EVALUATION_MARKER)
        if evaluation is None:
            if by_name:
                raise HealthError()
            # Before this policy existed, refresh jobs had no health markers.
            # This is an unknown-history boundary, never a healthy reset. It
            # avoids downloading a hundred legacy jobs at the first impairment.
            self.records[run['id']] = LEGACY_RECORD
            return LEGACY_RECORD
        if evaluation['conclusion'] != 'success':
            self.records[run['id']] = None
            return None
        active, alerted, acknowledged, observed = {}, {}, {}, {}
        for key in CONDITIONS:
            for kind, target in (('impaired', active), ('alerted', alerted),
                                 ('acknowledged', acknowledged), ('observed', observed)):
                step = by_name.get(marker(key, kind))
                if step is None or step['conclusion'] not in ('success', 'skipped'):
                    raise HealthError()
                target[key] = step['conclusion'] == 'success'
            if ((acknowledged[key] and not active[key]) or (alerted[key] and not acknowledged[key])
                    or (not observed[key] and (active[key] or acknowledged[key] or alerted[key]))):
                raise HealthError()
        notification = by_name.get(NOTIFY_MARKER)
        if notification is None:
            raise HealthError()
        sent = notification['conclusion'] == 'failure' and run['conclusion'] == 'failure'
        notified_at = None
        if sent:
            notified_at = timestamp(notification.get('completed_at'))
            if notified_at < timestamp(run['run_started_at']) or notified_at >= self.started_at:
                raise HealthError()
        else:
            # Cancellation between markers and the failing notification step must
            # never count as delivery. The next run can raise the alert again.
            for key in CONDITIONS:
                if alerted[key]:
                    acknowledged[key] = False
                    alerted[key] = False
        record = {'active': active, 'alerted': alerted, 'acknowledged': acknowledged, 'observed': observed,
                  'started_at': timestamp(run['run_started_at']), 'notified_at': notified_at}
        self.records[run['id']] = record
        return record

    def recent(self):
        for run in self.runs:
            record = self._record(run)
            if record is LEGACY_RECORD:
                break
            if record is not None:
                yield record

    def last_alert(self, key):
        cutoff = self.started_at - REMINDER
        for run in self.runs:
            if run['conclusion'] != 'failure':
                continue
            # A run can start before the reminder window and notify inside it.
            # The refresh workflow has a 25-minute job timeout.
            if timestamp(run['run_started_at']) < cutoff - timedelta(minutes=30):
                continue
            record = self._record(run)
            if record is LEGACY_RECORD:
                # Acknowledgement cannot be proved by a pre-policy run. A
                # visible monitor alert is safer than suppressing an incident.
                if timestamp(run['run_started_at']) >= cutoff:
                    raise HealthError()
                break
            if record is not None and record['alerted'][key] and record['notified_at'] is not None:
                return record['notified_at']
        if not self.complete_window:
            raise HealthError()
        return None


def decide(report, history_factory=ActionsHistory):
    report = validate_report(report)
    decision = empty_decision(report)
    if not decision['eligible'] or not any(decision['active'].values()):
        return decision
    history = history_factory()
    records = iter(history.recent())
    cached, exhausted = [], False

    def observations(key):
        """Unknown runs neither recover an incident nor count as failed attempts."""
        nonlocal exhausted
        index, unknown_gap = 0, False
        while True:
            if index >= len(cached):
                if exhausted:
                    return
                prior = next(records, None)
                if prior is None:
                    exhausted = True
                    return
                cached.append(prior)
            prior = cached[index]
            index += 1
            if not prior['observed'][key]:
                unknown_gap = True
                continue
            yield prior, unknown_gap
            unknown_gap = False

    for key in CONDITIONS:
        if not decision['active'][key]:
            continue
        prior_observations = observations(key)
        first = next(prior_observations, None)
        latest = first[0] if first is not None else None
        if latest is not None and latest['active'][key] and latest['acknowledged'][key]:
            decision['acknowledged'][key] = True
            prior_alert = history.last_alert(key)
            if prior_alert is not None and history.started_at - prior_alert < REMINDER:
                decision['reasons'][key] = 'ongoing incident; notification already sent'
                continue
            decision['alert'][key] = True
            decision['reasons'][key] = 'six-hour reminder for an ongoing incident'
            continue
        count, oldest, continuous = 1, history.started_at, True
        if key not in IMMEDIATE:
            item = first
            while item is not None and count < 3:
                prior, unknown_gap = item
                if not prior['active'][key]:
                    break
                continuous = continuous and not unknown_gap
                count += 1
                oldest = prior['started_at']
                if count >= 3 or (continuous and history.started_at - oldest >= PERSISTENCE):
                    break
                item = next(prior_observations, None)
        alert = key in IMMEDIATE or count >= 3 or (continuous and history.started_at - oldest >= PERSISTENCE)
        decision['alert'][key] = alert
        decision['acknowledged'][key] = alert
        decision['reasons'][key] = ('freshness or integrity threshold reached' if key in IMMEDIATE else
                                   'persistent failure threshold reached' if alert else
                                   'temporary failure; prior evidence retained')
    decision['notify'] = any(decision['alert'].values())
    return decision


def safe_decision(report, history_factory=ActionsHistory):
    try:
        return decide(report, history_factory)
    except Exception:
        # API response bodies and report/parser exceptions may contain private
        # data. Never include them in the Actions log, output, or summary.
        try:
            decision = empty_decision(validate_report(report))
        except Exception:
            decision = empty_decision()
        decision['monitor_error'] = True
        decision['notify'] = True
        return decision


def render_summary(decision):
    lines = ['### Refresh health', '']
    if decision['monitor_error']:
        lines += ['The health monitor could not verify incident history. A visible alert is required; '
                  'the failure has not been silently suppressed.', '']
    elif not decision['eligible']:
        lines += ['This verification or probe run does not change refresh incident history.', '']
    elif not any(decision['active'].values()) and all(decision['observed'].values()):
        lines += ['All monitored refresh conditions are healthy. Prior incidents are reset.', '']
    elif not any(decision['active'].values()):
        lines += ['The conditions checked in this run are healthy. Unchecked conditions remain unknown '
                  'and do not reset prior incidents.', '']
    if decision['eligible'] and any(decision['active'].values()):
        for key in CONDITIONS:
            if decision['active'][key]:
                action = 'Alert' if decision['alert'][key] else 'Warning'
                reason = decision['reasons'][key]
                if decision['monitor_error']:
                    reason = 'incident history could not be verified'
                lines.append(f'- **{LABELS[key]} — {action}:** {reason}.')
        lines.append('')
    if decision['eligible'] and not all(decision['observed'].values()):
        unknown = ', '.join(LABELS[key] for key in CONDITIONS if not decision['observed'][key])
        lines += ['Not observed in this run: ' + unknown + '. Prior incident history is preserved.', '']
    if decision['notify']:
        lines += ['The final notification step will fail so existing GitHub Actions notifications can report this incident.', '']
    return '\n'.join(lines)


def output_values(decision):
    values = {key: decision[key] for key in ('eligible', 'notify', 'monitor_error')}
    for key in CONDITIONS:
        values['observed_' + key] = decision['observed'][key]
        values['active_' + key] = decision['active'][key]
        values['alert_' + key] = decision['alert'][key]
        values['acknowledged_' + key] = decision['acknowledged'][key]
    return {key: 'true' if value else 'false' for key, value in values.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    args = parser.parse_args(argv)
    report = None
    try:
        with Path(args.report).open('rb') as handle:
            raw = handle.read(8193)
        if len(raw) > 8192:
            raise HealthError()
        report = json.loads(raw)
    except Exception:
        pass
    decision = safe_decision(report)
    rendered = render_summary(decision)
    output = ''.join(f'{key}={value}\n' for key, value in output_values(decision).items())
    try:
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
                handle.write(output)
        else:
            print(output, end='')
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
                handle.write(rendered)
        print('Refresh health evaluated; notification ' + ('required.' if decision['notify'] else 'not required.'))
        if decision['monitor_error']:
            print('::warning::Refresh health history is unavailable; the final notification step must remain enabled.')
        elif any(decision['active'].values()):
            print('::warning::Refresh is degraded; see the health summary for retained evidence and notification status.')
        return 0
    except Exception:
        # Only output/summary write failure prevents the workflow recording its
        # markers. Fail visibly rather than returning an unobservable success.
        print('Unable to record refresh health outputs.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
