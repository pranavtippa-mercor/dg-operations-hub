"""Start one detached local updater, or inspect/stop that exact process."""
import argparse
import fcntl
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT/'.private'


def active_pid():
    path = PRIVATE/'publisher.pid'
    if not path.exists():
        return None
    try:
        pid = int(path.read_text())
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'args='], capture_output=True, text=True)
        # Never print command lines: other local sessions can contain credentials.
        if result.returncode == 0 and str(ROOT/'scripts/publish.py') in result.stdout and '--watch' in result.stdout:
            return pid
    except (ValueError, OSError):
        pass
    return None


def start():
    cloud = PRIVATE/'hosted-active.json'
    if cloud.exists():
        import json
        if json.loads(cloud.read_text()).get('active') is True:
            print('GitHub hosted refresh is active. A local updater is not needed.')
            return
    pid = active_pid()
    if pid:
        print(f'Operations updater is already running (PID {pid}).')
        return
    PRIVATE.mkdir(exist_ok=True)
    PRIVATE.chmod(0o700)
    with (PRIVATE/'publisher.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('An updater already owns the process lock.')
    log = PRIVATE/'publisher.log'
    with log.open('ab', buffering=0) as output:
        log.chmod(0o600)
        process = subprocess.Popen([sys.executable, '-u', str(ROOT/'scripts/publish.py'), '--watch', '--push'], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
    for _ in range(40):
        if active_pid() == process.pid:
            print(f'Operations updater started (PID {process.pid}). The terminal may be closed.')
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError('Updater did not acquire its lock; inspect the private publisher log.')


def publisher_stopped():
    """Verify both process identity and the publisher lock before cutover."""
    if active_pid() is not None:
        return False
    if not PRIVATE.exists():
        return True
    with (PRIVATE/'publisher.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        # A replacement publisher could have appeared before the lock check.
        return active_pid() is None


def stop(wait=False, timeout=300):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError('Stop timeout must be a finite nonnegative number.')
    pid = active_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # The tracked updater finished between inspection and signal.
        print(f'Stop requested for operations updater PID {pid}.')
    elif not wait:
        print('Operations updater is not running.')
    if not wait:
        return
    deadline = time.monotonic() + timeout
    while True:
        if publisher_stopped():
            print('Operations updater stopped; its process is absent and publisher lock is free.')
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Updater shutdown was not verified before the timeout; no process was force-killed.')
        time.sleep(min(1, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'status', 'stop'))
    parser.add_argument('--wait', action='store_true', help='Wait for process exit and a free publisher lock.')
    parser.add_argument('--timeout', type=float, default=300, help='Maximum shutdown wait in seconds (default: 300).')
    args = parser.parse_args(argv)
    if args.wait and args.action != 'stop':
        parser.error('--wait is available only for stop')
    try:
        if args.action == 'start':
            start()
        elif args.action == 'stop':
            stop(wait=args.wait, timeout=args.timeout)
        else:
            pid = active_pid()
            print(f'Operations updater running (PID {pid}).' if pid else 'Operations updater is not running.')
    except (OSError, ValueError, RuntimeError) as error:
        # Do not include process command lines or arbitrary file contents.
        print(str(error) if isinstance(error, RuntimeError) else 'Unable to manage the local updater safely.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
