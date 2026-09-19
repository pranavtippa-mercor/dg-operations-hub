"""Start one detached local updater, or inspect/stop that exact process."""
import argparse
import fcntl
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'status', 'stop'))
    args = parser.parse_args()
    if args.action == 'start':
        start()
    else:
        pid = active_pid()
        if args.action == 'stop' and pid:
            os.kill(pid, signal.SIGTERM)
            print(f'Stop requested for operations updater PID {pid}.')
        else:
            print(f'Operations updater running (PID {pid}).' if pid else 'Operations updater is not running.')
