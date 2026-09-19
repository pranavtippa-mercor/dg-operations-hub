"""One-time import of private seed evidence; the running app never reads legacy paths."""
import argparse
import importlib.util
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(path.read_text())


def save_missing(path, value):
    if not path.exists():
        path.write_text(json.dumps(value, separators=(',', ':')))
        path.chmod(0o600)


def copy_missing(source, destination):
    if not destination.exists():
        shutil.copy2(source, destination)
        destination.chmod(0o600)


def migrate(completion, staleness, modules, output):
    private = ROOT/'.private'
    private.mkdir(exist_ok=True)
    private.chmod(0o700)
    board, stale, module = Path(completion), Path(staleness), Path(modules)
    match = re.search(r'<script type="application/json" id="seed">(.*?)</script>', (board/'board.html').read_text(), re.S)
    if not match:
        raise ValueError('Missing completion seed')
    seed = json.loads(match.group(1))
    state = {
        'seed': seed, 'rows': load(stale/'rows.json'),
        'statuses': load(stale/'statuses.json')['statuses'], 'users': load(stale/'users.json'),
        'annotations': load(stale/'annotations.json'),
        'history': [json.loads(x) for x in (stale/'history.jsonl').read_text().splitlines() if x.strip()][-600:],
        'fixed_html': load(board/'html_roster.json')['roster'],
        'roots': load(board/'ee_thread_roots.json')['roots'],
    }
    save_missing(private/'task-state.json', state)
    slack = private/'slack'
    bootstrap = private/'modules/bootstrap'
    slack.mkdir(exist_ok=True)
    bootstrap.mkdir(parents=True, exist_ok=True)
    for source, target in [(board/'confirmations.json', 'confirmations.json'), (stale/'deadlines.json', 'commitments.json'), (stale/'slack_activity.json', 'activity.json'), (board/'ee_thread_roots.json', 'roots.json')]:
        copy_missing(source, slack/target)
    for name in ('spec_map.json', 'studio_audit_registry.tsv'):
        copy_missing(module/name, bootstrap/name)
    copy_missing(module/'data.json', bootstrap.parent/'data.json')
    # Read the already-authorized adapter's scope only during explicit migration.
    spec = importlib.util.spec_from_file_location('legacy_adapter', board/'refresh_board.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    config = {
        'version': 2,
        'studio': {'campaign': seed['campaign'], 'world': seed['world'], 'headers': adapter.HEADERS},
        'task_state': str(private/'task-state.json'),
        'modules': {'cache_dir': str(bootstrap.parent), 'data': str(bootstrap.parent/'data.json')},
        'slack': {'cache_dir': str(slack), 'workspace': 'mercor'},
        'schedules': {'tasks_seconds': 300, 'slack_seconds': 900, 'modules_seconds': 3600},
    }
    save_missing(Path(output), config)
    print('Private seed imported. No legacy jobs stopped and no source records changed.')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--completion', required=True)
    p.add_argument('--staleness', required=True)
    p.add_argument('--modules', required=True, help='Legacy module dashboard directory')
    p.add_argument('--output-config', default=str(ROOT/'.private/config-v2.json'))
    a = p.parse_args()
    migrate(a.completion, a.staleness, a.modules, a.output_config)
