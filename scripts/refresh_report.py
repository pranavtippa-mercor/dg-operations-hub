"""Reduce private snapshot freshness to fixed public health flags."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_LIMITS = {'source_tasks': (('tasks', 'activity'), 1800),
                 'source_slack': (('slack',), 3600),
                 'source_modules': (('modules',), 10800)}
CONDITIONS = ('checkpoint', 'restore', 'publication', 'source_tasks',
              'source_slack', 'source_modules', 'integrity')


def stale(value, now, limit):
    if not isinstance(value, str) or len(value) > 40:
        return True
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if at.tzinfo is None:
            return True
        age = (now - at).total_seconds()
        return age < -120 or age >= limit
    except (ValueError, OverflowError):
        return True


def report(env, snapshot, *, collector_status=None, now=None):
    now = now or datetime.now(timezone.utc)
    mode = ('probe' if env.get('PROBE_ONLY') == 'true' else
            'verification' if env.get('VERIFY_ONLY') == 'true' or env.get('PUBLISH_ENABLED') != 'true'
            else 'refresh')
    flags = dict.fromkeys(CONDITIONS, False)
    observed = dict.fromkeys(CONDITIONS, False)
    if mode != 'refresh':
        return {'version': 1, 'mode': mode, 'conditions': flags, 'observed': observed}
    restored = env.get('RESTORE_OUTCOME') == 'success'
    ready = env.get('SNAPSHOT_READY') == 'true'
    flags['restore'] = not restored
    observed['restore'] = True
    if restored:
        observed['checkpoint'] = env.get('SAVE_OUTCOME') in ('success', 'failure')
        observed['publication'] = env.get('COLLECT_OUTCOME') in ('success', 'failure')
        observed['integrity'] = ready and env.get('VERIFY_OUTCOME') in ('success', 'failure')
        flags['checkpoint'] = observed['checkpoint'] and env.get('SAVE_OUTCOME') != 'success'
        flags['publication'] = observed['publication'] and env.get('SNAPSHOT_PUBLISHED') != 'true'
        flags['integrity'] = observed['integrity'] and env.get('VERIFY_OUTCOME') != 'success'
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('sources'), dict):
            flags['integrity'] = True
            observed['integrity'] = True
        else:
            sources = snapshot['sources']
            for condition, (names, limit) in SOURCE_LIMITS.items():
                observed[condition] = True
                flags[condition] = any(stale(sources.get(name, {}).get('at')
                                             if isinstance(sources.get(name), dict) else None,
                                             now, limit) for name in names)
            # A partial collector can update a snapshot timestamp without
            # advancing its last complete successful collection. Check both.
            for name, limit in (('slack', 3600), ('modules', 10800)):
                status = collector_status.get(name, {}) if isinstance(collector_status, dict) else {}
                last_success = status.get('last_success_at') if isinstance(status, dict) else None
                flags['source_' + name] |= stale(last_success, now, limit)
    return {'version': 1, 'mode': mode, 'conditions': flags, 'observed': observed}


def main():
    snapshot = None
    collector_status = None
    path = ROOT / '.private/snapshot.json'
    try:
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 16 * 1024 * 1024:
            snapshot = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        status_path = ROOT / '.private/collector-status.json'
        if status_path.is_file() and not status_path.is_symlink() and status_path.stat().st_size <= 1024 * 1024:
            collector_status = json.loads(status_path.read_text())
    except (OSError, ValueError, UnicodeError):
        pass
    result = report(os.environ, snapshot, collector_status=collector_status)
    destination = ROOT / '.private/refresh-health-report.json'
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(json.dumps(result))
    destination.chmod(0o600)
    # Output contains only the fixed flags above, never source dates or records.
    print(json.dumps(result, sort_keys=True))
    if os.environ.get('COLLECT_OUTCOME') == 'failure':
        print('::warning::Collection reported a problem; usable evidence was retained. Source age determines escalation.')


if __name__ == '__main__':
    main()
