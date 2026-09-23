"""One coordinator, independently due read-only collectors, no overlapping runs."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.pending')
    tmp.write_text(json.dumps(value, separators=(',', ':')))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def iso():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def run_collector(name, config):
    from source_client import SourceClient
    with SourceClient(config) as client:
        if name == 'modules':
            from modules_collect import collect_modules
            data = collect_modules(client, config)
            return {'at': data['generated_at_utc'], 'modules': len(data['modules'])}
        if name == 'slack':
            from slack_collect import collect
            return collect(config, client=client)
        raise ValueError('Unknown collector')


class CollectorScheduler:
    def __init__(self, config, runner=run_collector, clock=time.time, status_path=None):
        from runtime_config import normalize_config
        self.config, self.runner, self.clock = normalize_config(config, root=ROOT), runner, clock
        self.status_path = Path(status_path or ROOT/'.private/collector-status.json')
        self.status = read_json(self.status_path, {})
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='source')
        self.futures = {}

    def _finish_ready(self):
        now = self.clock()
        changed = False
        for name, future in list(self.futures.items()):
            if not future.done():
                continue
            previous = self.status.get(name, {})
            receipt = None
            try:
                receipt = future.result()
                if not isinstance(receipt, dict):
                    raise ValueError('Collector returned no verifiable receipt.')
                if receipt.get('ok') is False or receipt.get('complete') is False:
                    raise RuntimeError('Source coverage is incomplete; retain the last successful watermark.')
                interval = self.config.get('schedules', {}).get(name+'_seconds', 3600 if name == 'modules' else 900)
                self.status[name] = {'ok': True, 'completed_at': iso(), 'last_success_at': iso(), 'next_at': now+interval, 'receipt': receipt}
                changed = True
                print(f'{name.capitalize()} source refreshed.', flush=True)
            except Exception as error:
                # Source error text may contain private records. Persist only its type.
                self.status[name] = {**previous, 'ok': False, 'attempted_at': iso(), 'error': type(error).__name__, 'next_at': now+300}
                if isinstance(receipt, dict):
                    self.status[name]['last_attempt_receipt'] = receipt
                print(f'{name.capitalize()} source failed ({type(error).__name__}); prior evidence retained.', flush=True)
            del self.futures[name]
            write_json(self.status_path, self.status)
        return changed

    def _launch_due(self, force=False):
        now = self.clock()
        for name in ('modules', 'slack'):
            if name not in self.futures and (force or now >= self.status.get(name, {}).get('next_at', 0)):
                self.futures[name] = self.pool.submit(self.runner, name, self.config)

    def tick(self, force=False):
        changed = self._finish_ready()
        self._launch_due(force=force)
        return changed

    def run_due(self, force=False):
        """Wait for this finite batch only; never relaunch a job while harvesting."""
        changed = self._finish_ready()
        self._launch_due(force=force)
        attempted = sorted(self.futures)
        for future in list(self.futures.values()):
            try:
                future.result()
            except Exception:
                pass
        changed = self._finish_ready() or changed
        failed = [name for name in ('modules', 'slack') if self.status.get(name, {}).get('ok') is False]
        return {'ok': not failed, 'attempted': attempted, 'failed': failed,
                'attempt_failed': [name for name in failed if name in attempted], 'changed': changed}

    def refresh_all(self):
        result = self.run_due(force=True)
        if not result['ok']:
            raise RuntimeError('At least one source collector failed; retirement is not safe.')
        return result

    def close(self):
        self.pool.shutdown(wait=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(ROOT/'.private/config.json'))
    args = parser.parse_args()
    from runtime_config import load_config
    schedule = CollectorScheduler(load_config(args.config))
    try:
        schedule.refresh_all()
    finally:
        schedule.close()
