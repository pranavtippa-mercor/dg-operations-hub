#!/usr/bin/env python3
"""Persist only allowlisted collector state as authenticated ciphertext.

GitHub Actions supplies GH_TOKEN and DG_HUB_STATE_KEY. Source authentication is
owned by the caller; fetch_file/update_files preserve its separate opaque blob.
The generated branch is intentionally parentless. Workflow concurrency must
serialize writers because GitHub's force-ref API has no compare-and-swap field.
"""
import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
REPO = os.environ.get('GITHUB_REPOSITORY', 'pranavtippa-mercor/dg-operations-hub')
BRANCH = 'collector-state'
MARKER = '[dg-operations generated collector state]'
STATE_FILE = 'collector-state.enc.json'
AUTH_FILE = 'source-auth.enc.json'
MANAGED_FILES = frozenset((STATE_FILE, AUTH_FILE))
PURPOSE = 'dg-operations-collector-state'
MIB = 1024 * 1024
MAX_FILES = 20000
MAX_FILE_BYTES = 16 * MIB
MAX_TOTAL_BYTES = 64 * MIB
MAX_MANIFEST_BYTES = 96 * MIB
MAX_ENCRYPTED_BYTES = 48 * MIB
MAX_ARCHIVE_BYTES = 128 * MIB
API_ATTEMPTS = 4
API_TIMEOUT = 60
API_WAIT_BUDGET = 120
DIAGNOSTIC_ERROR_LIMIT = 20
DIAGNOSTIC_MESSAGE_LIMIT = 4096
DIAGNOSTIC_RESPONSE_BYTES = 65536
DIAGNOSTIC_PATH = ROOT / '.private/diagnostics/github-state-error.enc.json'
DIAGNOSTIC_PURPOSE = 'dg-operations-github-state-diagnostic'
DIAGNOSTIC_CODES = frozenset(('missing', 'missing_field', 'invalid', 'already_exists',
                             'unprocessable', 'custom', 'too_large'))
DIAGNOSTIC_RESOURCES = frozenset(('Tree', 'Blob', 'Commit', 'Reference', 'Repository'))
DIAGNOSTIC_FIELDS = frozenset(('tree', 'base_tree', 'sha', 'path', 'mode', 'type',
                              'content', 'encoding', 'parents', 'ref', 'force'))
CHUNK_RE = re.compile(r'chunks/[0-9a-f]{64}\.enc\.json\Z')
FIXED_PATHS = frozenset((
    'config.json', 'task-state.json', 'history.json', 'snapshot.json', 'collector-status.json',
    'modules/registry.json', 'modules/data.json', 'modules/receipt.json',
    'modules/bootstrap/spec_map.json', 'modules/bootstrap/studio_audit_registry.tsv',
    'slack/roots.json', 'slack/state.json', 'slack/activity.json', 'slack/commitments.json',
    'slack/confirmations.json', 'slack/bundle.json',
    'diagnostics/studio-probe.json',
))
DYNAMIC_DIRS = frozenset(('modules/dimensions', 'slack/threads', 'slack/history'))
SCOPE_HEADERS = frozenset(('X-Campaign-Id', 'X-Company-Id', 'X-Account-Id'))
FORBIDDEN_CONFIG_KEYS = frozenset((
    'authorization', 'password', 'passwords', 'secret', 'secrets', 'token', 'tokens',
    'access_token', 'refresh_token', 'id_token', 'client_secret', 'api_key', 'apikey',
    'credential', 'credentials', 'cookie', 'cookies', 'access_key', 'state_key',
))


class StateError(RuntimeError):
    """Sanitized state failures safe to show in public Actions logs."""


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def _valid_name(name):
    if name not in MANAGED_FILES:
        raise StateError('Unexpected generated state filename.')


def _valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value) is not None


def _operation(path, method):
    # Only fixed labels reach public logs, never paths, request/response bodies or stderr.
    for route, verb, label in (
        (r'/git/ref/heads/collector-state', 'GET', 'read_ref'),
        (r'/git/commits/[0-9a-f]{40}', 'GET', 'read_commit'),
        (r'/git/trees/[0-9a-f]{40}\?recursive=1', 'GET', 'read_tree'),
        (r'/git/blobs/[0-9a-f]{40}', 'GET', 'read_blob'),
        (r'/git/trees', 'POST', 'create_tree'),
        (r'/git/commits', 'POST', 'create_commit'),
        (r'/git/refs', 'POST', 'create_ref'),
        (r'/git/refs/heads/collector-state', 'PATCH', 'update_ref'),
    ):
        if method == verb and re.fullmatch(route, path):
            return label
    return 'other_request'


def _response(stdout, stderr):
    status, headers, payload = None, {}, stdout
    parts = re.split(r'\r?\n\r?\n', stdout, maxsplit=1)
    match = re.match(r'HTTP/\S+ ([1-5][0-9]{2})\b', parts[0])
    if match and len(parts) == 2:
        status, payload = int(match[1]), parts[1]
        for line in parts[0].splitlines()[1:]:
            key, separator, value = line.partition(':')
            if separator:
                headers[key.lower().strip()] = value.strip()
    if status is None:
        match = re.search(r'\(HTTP ([1-5][0-9]{2})\)', stderr)
        status = int(match[1]) if match else None
    return status, headers, payload


def _retry_delay(status, headers, payload, stderr, attempt):
    rate_limited = status == 429 or (status == 403 and (
        headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in headers
        or re.search(r'(rate limit|secondary limit|abuse detection)', payload + stderr, re.I)))
    delay = 2 ** attempt
    if rate_limited:
        delay = 60 * 2 ** (attempt - 1)
    if status in (408, 429) or (status is not None and status >= 500) or rate_limited:
        retry_after = headers.get('retry-after', '')
        if re.fullmatch(r'[0-9]{1,10}', retry_after):
            delay = max(delay, int(retry_after))
        elif retry_after:
            try:
                delay = max(delay, int(parsedate_to_datetime(retry_after).timestamp() - time.time()) + 1)
            except (ValueError, TypeError, OverflowError):
                pass
        reset = headers.get('x-ratelimit-reset', '')
        if headers.get('x-ratelimit-remaining') == '0' and re.fullmatch(r'[0-9]{1,12}', reset):
            delay = max(delay, int(reset) - int(time.time()) + 1)
        return ('rate_limit' if rate_limited else 'server_error'), delay
    if status is None and re.search(
            r'timeout|timed out|connection (?:reset|refused|closed)|\bEOF\b|TLS handshake|'
            r'no such host|temporary failure|network is unreachable', stderr, re.I):
        return 'transport_error', delay
    return ({401: 'authentication', 403: 'permission', 404: 'not_found',
             409: 'conflict', 422: 'validation'}.get(status, 'request_error'), None)


def _message_category(message):
    """Classify provider text without ever returning any part of that text."""
    if not isinstance(message, str):
        return 'unavailable'
    message = message[:DIAGNOSTIC_MESSAGE_LIMIT].lower()
    for pattern, category in (
        (r'timed? out|took too long|timeout', 'processing_timeout'),
        (r'spam|abuse detection', 'spam_detection'),
        (r'rate limit|secondary limit', 'rate_limit'),
        (r'too many (?:tree )?(?:entries|objects|files)', 'entry_limit'),
        (r'(?:tree|blob|content|payload|request)[^\n]{0,80}(?:too large|exceeds|maximum size)', 'size_limit'),
        (r'(?:sha[^\n]{0,80}content|content[^\n]{0,80}sha)[^\n]{0,80}(?:both|same|only one)|(?:both|same)[^\n]{0,80}(?:sha[^\n]{0,80}content|content[^\n]{0,80}sha)', 'conflicting_content_and_sha'),
        (r'(?:object|blob|tree|sha)[^\n]{0,80}(?:not found|does not exist|missing)|(?:not found|does not exist)[^\n]{0,80}(?:object|blob|tree|sha)', 'missing_object'),
        (r'(?:invalid|not a valid)[^\n]{0,80}(?:object|blob|tree|sha)|(?:object|blob|tree|sha)[^\n]{0,80}(?:invalid|not valid)', 'invalid_object'),
        (r'is a directory|not a directory|duplicate path', 'path_conflict'),
        (r'validation failed', 'validation_failed'),
    ):
        if re.search(pattern, message):
            return category
    return 'other'


def _request_metrics(body, request_body):
    """Only bounded numeric shape information may leave the request boundary."""
    metrics = {'request_bytes': min(len((request_body or '').encode()), MAX_ARCHIVE_BYTES + 1)}
    entries = body.get('tree') if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return metrics
    contents = [entry['content'] for entry in entries[:MAX_FILES + 4]
                if isinstance(entry, dict) and isinstance(entry.get('content'), str)]
    sizes = [len(content.encode()) for content in contents]
    metrics.update(base_tree=_valid_sha(body.get('base_tree')),
                   tree_entries=min(len(entries), MAX_FILES + 4),
                   content_entries=len(contents),
                   sha_entries=sum(isinstance(entry, dict) and 'sha' in entry
                                   for entry in entries[:MAX_FILES + 4]),
                   deleted_entries=sum(isinstance(entry, dict) and 'sha' in entry and entry['sha'] is None
                                       for entry in entries[:MAX_FILES + 4]),
                   content_bytes=min(sum(sizes), MAX_ARCHIVE_BYTES + 1),
                   largest_content_bytes=min(max(sizes, default=0), MAX_ENCRYPTED_BYTES + 1))
    return metrics


def _failure_diagnostic(operation, status, headers, payload, request_metrics):
    """An allowlist, not redaction, protects public Actions logs from response data."""
    diagnostic = {'operation': operation, 'http': status, **request_metrics}
    request_id = headers.get('x-github-request-id', '')
    if isinstance(request_id, str) and re.fullmatch(r'[0-9A-Fa-f]{1,8}(?::[0-9A-Fa-f]{1,8}){4}', request_id):
        diagnostic['request_id'] = request_id.upper()
    try:
        value = json.loads(payload) if len(payload) <= DIAGNOSTIC_RESPONSE_BYTES else None
    except (ValueError, TypeError):
        value = None
    diagnostic['message_category'] = _message_category(value.get('message')) if isinstance(value, dict) else 'unavailable'
    errors = value.get('errors') if isinstance(value, dict) else None
    if isinstance(errors, list):
        diagnostic['error_count'] = min(len(errors), DIAGNOSTIC_ERROR_LIMIT + 1)
        safe_errors = []
        for error in errors[:DIAGNOSTIC_ERROR_LIMIT]:
            if isinstance(error, dict):
                safe = {key: error[key] if isinstance(error.get(key), str) and error[key] in allowed else 'other'
                        for key, allowed in (('resource', DIAGNOSTIC_RESOURCES),
                                             ('field', DIAGNOSTIC_FIELDS), ('code', DIAGNOSTIC_CODES))}
                safe['message_category'] = _message_category(error.get('message'))
            else:
                safe = {'message_category': _message_category(error)}
            if safe not in safe_errors:
                safe_errors.append(safe)
        diagnostic['errors'] = safe_errors
    return diagnostic


def _save_failure_diagnostic(diagnostic, payload):
    """Keep bounded provider detail as ciphertext, independently of the failing save."""
    if not os.environ.get('DG_HUB_STATE_KEY'):
        return
    pending = None
    try:
        # Never include request content, arguments, stderr, arbitrary headers or
        # environment data. Redact before truncating so a boundary cannot retain
        # only the first part of a credential that the provider might echo.
        secrets = set()
        for name, value in os.environ.items():
            if value and (name in ('GH_TOKEN', 'GITHUB_TOKEN', 'MERCOR_API_KEY', 'STUDIO_API_KEY')
                          or re.search(r'(?:^|_)(?:TOKEN|KEY|SECRET|PASSWORD)$', name)):
                secrets.add(value)
                secrets.add(json.dumps(value)[1:-1])
        redacted = payload
        for value in sorted(secrets, key=len, reverse=True):
            redacted = redacted.replace(value, '[redacted]')
        raw = redacted.encode()
        document = {'schema_version': 1, 'purpose': DIAGNOSTIC_PURPOSE,
                    'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'diagnostic': diagnostic,
                    'response': raw[:DIAGNOSTIC_RESPONSE_BYTES].decode('utf-8', errors='ignore'),
                    'response_truncated': len(raw) > DIAGNOSTIC_RESPONSE_BYTES}
        encrypted = crypt('encrypt', _json_bytes(document), DIAGNOSTIC_PURPOSE)
        private = DIAGNOSTIC_PATH.parent.parent
        _safe_path(private, 'diagnostics', directory=True)
        DIAGNOSTIC_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe_path(private, 'diagnostics/github-state-error.enc.json')
        descriptor, name = tempfile.mkstemp(prefix='.github-state-error-', dir=DIAGNOSTIC_PATH.parent)
        pending = Path(name)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(encrypted)
        pending.replace(DIAGNOSTIC_PATH)
        print('Encrypted GitHub state diagnostic retained.', file=sys.stderr)
    except Exception:
        # Recording a diagnostic must not replace or reveal the original error.
        print('Encrypted GitHub state diagnostic could not be retained.', file=sys.stderr)
    finally:
        if pending is not None:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass


def api(path, method='GET', body=None, missing=False, before_retry=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', REPO):
        raise StateError('Invalid state repository configuration.')
    args = ['gh', 'api', 'repos/' + REPO + path, '--method', method, '--include']
    if body is not None:
        args += ['--input', '-']
    operation = _operation(path, method)
    retry_safe = method == 'GET' or operation in ('create_tree', 'create_commit') or (
        operation in ('create_ref', 'update_ref') and before_retry is not None)
    attempts = API_ATTEMPTS if retry_safe else 1
    waited = 0
    request_body = json.dumps(body) if body is not None else None
    request_metrics = _request_metrics(body, request_body)
    for attempt in range(1, attempts + 1):
        headers, payload, diagnostic = {}, '', None
        if attempt > 1 and before_retry is not None:
            recovered = before_retry()
            if recovered is not None:
                print(f'GitHub state operation={operation} reconciled after an ambiguous response.', file=sys.stderr)
                return recovered
        try:
            result = subprocess.run(args, input=request_body, capture_output=True,
                                    text=True, cwd=ROOT, timeout=API_TIMEOUT)
            status, headers, payload = _response(result.stdout, result.stderr)
            if not result.returncode and (status is None or 200 <= status < 300):
                try:
                    value = json.loads(payload) if payload.strip() else None
                except ValueError:
                    raise StateError(f'GitHub returned invalid state metadata (operation={operation}).') from None
                if attempt > 1:
                    print(f'GitHub state operation={operation} recovered on attempt={attempt}.', file=sys.stderr)
                return value
            if missing and method == 'GET' and status == 404:
                return None
            if status is not None:
                diagnostic = _failure_diagnostic(operation, status, headers, payload, request_metrics)
                print('GitHub state diagnostic ' + json.dumps(diagnostic, sort_keys=True, separators=(',', ':')),
                      file=sys.stderr)
            category, delay = _retry_delay(status, headers, payload, result.stderr, attempt)
        except subprocess.TimeoutExpired:
            status, category, delay = None, 'timeout', 2 ** attempt
        except OSError:
            raise StateError(f'GitHub state request could not start (operation={operation}).') from None
        details = f'operation={operation} http={status or "unavailable"} category={category} attempt={attempt}/{attempts}'
        if delay is None or attempt == attempts:
            if diagnostic is not None:
                _save_failure_diagnostic(diagnostic, payload)
            raise StateError(f'GitHub encrypted state request failed ({details}).') from None
        if waited + delay > API_WAIT_BUDGET:
            if diagnostic is not None:
                _save_failure_diagnostic(diagnostic, payload)
            raise StateError(f'GitHub encrypted state request deferred ({details}; server_wait={delay}s exceeds retry budget).') from None
        print(f'GitHub state request retry ({details}; wait={delay}s).', file=sys.stderr)
        # Keep each sleep bounded while honoring the full server cooldown.
        remaining = delay
        while remaining:
            pause = min(remaining, 60)
            time.sleep(pause)
            remaining -= pause
        waited += delay


def _head():
    ref = api('/git/ref/heads/' + BRANCH, missing=True)
    if ref is None:
        return None
    value = ref.get('object', {}).get('sha') if isinstance(ref, dict) else None
    if not _valid_sha(value) or ref.get('object', {}).get('type') != 'commit':
        raise StateError('Invalid generated state reference.')
    return value


def _managed_tree(head):
    """Resolve only an immutable, validated generated commit to its base tree."""
    if not _valid_sha(head):
        raise StateError('Invalid generated state commit.')
    commit = api('/git/commits/' + head)
    tree = commit.get('tree') if isinstance(commit, dict) else None
    if (not isinstance(commit, dict) or not isinstance(commit.get('message'), str)
            or not commit['message'].startswith(MARKER)
            or commit.get('parents') != [] or not isinstance(tree, dict) or not _valid_sha(tree.get('sha'))):
        raise StateError('State branch is unmanaged; refusing to use or overwrite it.')
    return tree['sha']


def _branch():
    head = _head()
    if head is None:
        return None, {}
    tree = api('/git/trees/' + _managed_tree(head) + '?recursive=1')
    entries = tree.get('tree') if isinstance(tree, dict) else None
    if not isinstance(entries, list) or not entries or len(entries) > MAX_FILES + 3 or tree.get('truncated'):
        raise StateError('State branch has an invalid tree.')
    blobs = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get('path') == 'chunks' and entry.get('type') == 'tree' and entry.get('mode') == '040000' and _valid_sha(entry.get('sha')):
            continue
        if (not isinstance(entry, dict) or not isinstance(entry.get('path'), str)
                or not (entry['path'] in MANAGED_FILES or CHUNK_RE.fullmatch(entry['path']))
                or entry['path'] in blobs or entry.get('type') != 'blob'
                or entry.get('mode') != '100644' or not _valid_sha(entry.get('sha'))
                or type(entry.get('size')) is not int or not 0 < entry['size'] <= MAX_ENCRYPTED_BYTES):
            raise StateError('State branch has unrelated or invalid content.')
        blobs[entry['path']] = entry['sha']
    if not set(blobs) & MANAGED_FILES:
        raise StateError('State branch is missing its managed manifest.')
    return head, blobs


def _read_blob(sha):
    blob = api('/git/blobs/' + sha)
    if (not isinstance(blob, dict) or blob.get('encoding') != 'base64'
            or type(blob.get('size')) is not int or not 0 < blob['size'] <= MAX_ENCRYPTED_BYTES
            or not isinstance(blob.get('content'), str)
            or len(blob['content']) > MAX_ENCRYPTED_BYTES * 2):
        raise StateError('Invalid encrypted state blob.')
    try:
        raw = base64.b64decode(''.join(blob['content'].split()), validate=True)
        if len(raw) != blob['size']:
            raise ValueError()
        return raw.decode('utf-8')
    except (ValueError, UnicodeError):
        raise StateError('Invalid encrypted state blob.') from None


def fetch_file(name):
    """Read one managed ciphertext string, or None if not yet initialized."""
    _valid_name(name)
    _, blobs = _branch()
    return _read_blob(blobs[name]) if name in blobs else None


def _validate_ciphertexts(updates):
    for name, content in updates.items():
        if not isinstance(content, str) or not 0 < len(content.encode()) <= MAX_ENCRYPTED_BYTES:
            raise StateError('Encrypted state exceeds its size limit.')
        # A plaintext JSON document must not accidentally reach the public branch.
        try:
            envelope = json.loads(content)
            if (not isinstance(envelope, dict) or envelope.get('format') != 'dg-operations-state'
                    or envelope.get('version') != 1 or envelope.get('compression') != 'gzip'
                    or not all(isinstance(envelope.get(k), str) and envelope[k] for k in ('purpose', 'iv', 'tag', 'ciphertext'))
                    or set(envelope) != {'format', 'version', 'purpose', 'compression', 'iv', 'tag', 'ciphertext'}):
                raise ValueError()
            for field, size in (('iv', 12), ('tag', 16), ('ciphertext', None)):
                value = envelope[field]
                if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                    raise ValueError()
                raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
                if (size is not None and len(raw) != size) or base64.urlsafe_b64encode(raw).decode().rstrip('=') != value:
                    raise ValueError()
        except (ValueError, TypeError):
            raise StateError('Refusing to publish a non-ciphertext state document.') from None


def _commit_tree(old_head, entries):
    body = {'tree': sorted(entries, key=lambda e: e['path'])}
    if old_head is not None:
        body['base_tree'] = _managed_tree(old_head)
    print('GitHub state tree update ' + json.dumps(_request_metrics(body, json.dumps(body)),
                                                 sort_keys=True, separators=(',', ':')), file=sys.stderr)
    tree = api('/git/trees', 'POST', body)
    if not isinstance(tree, dict) or not _valid_sha(tree.get('sha')):
        raise StateError('GitHub returned an invalid state tree.')
    commit = api('/git/commits', 'POST', {'message': MARKER + ' Refresh encrypted collector state',
                                      'tree': tree['sha'], 'parents': []})
    if not isinstance(commit, dict) or not _valid_sha(commit.get('sha')):
        raise StateError('GitHub returned an invalid state commit.')
    if _head() != old_head:
        raise StateError('State branch changed concurrently; prior state retained.')

    def reconcile_ref():
        current = _head()
        if current == commit['sha']:
            return {'object': {'sha': current}}
        if current != old_head:
            raise StateError('State branch changed during retry; refusing to overwrite newer state.')
        return None

    if old_head is None:
        api('/git/refs', 'POST', {'ref': 'refs/heads/' + BRANCH, 'sha': commit['sha']},
            before_retry=reconcile_ref)
    else:
        api('/git/refs/heads/' + BRANCH, 'PATCH', {'sha': commit['sha'], 'force': True},
            before_retry=reconcile_ref)
    return commit['sha']


def _publish(old_head, existing, updates, keep=None):
    _validate_ciphertexts(updates)
    if old_head is None and existing:
        raise StateError('Existing state blobs require a generated base commit.')
    # The immutable base tree already owns unchanged blobs. Resubmitting all of
    # their nested paths made GitHub time out even on small updates. Send only
    # changed ciphertext and explicit removals; a parentless commit still means
    # this change does not retain previous state snapshots in branch history.
    entries = [{'path': name, 'mode': '100644', 'type': 'blob', 'sha': None}
               for name in existing if name not in updates and keep is not None and name not in keep]
    entries += [{'path': name, 'mode': '100644', 'type': 'blob', 'content': content}
                for name, content in updates.items()]
    return _commit_tree(old_head, entries)


def update_files(updates):
    """Update manifest/auth ciphertext, preserving the sibling and all chunks."""
    if not isinstance(updates, dict) or not updates:
        raise StateError('No encrypted state updates supplied.')
    for name in updates:
        _valid_name(name)
    _validate_ciphertexts(updates)
    old_head, existing = _branch()
    return _publish(old_head, existing, updates)


def allowed_path(name):
    if not isinstance(name, str) or len(name) > 256 or '\\' in name or '\x00' in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(part in ('', '.', '..') for part in name.split('/')):
        return False
    return name in FIXED_PATHS or (str(path.parent) in DYNAMIC_DIRS
                                 and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,190}\.json', path.name) is not None)


def _safe_path(private, name, directory=False):
    """Reject symlinks in every component below the private-state boundary."""
    private = Path(private)
    components = [private] + [private.joinpath(*PurePosixPath(name).parts[:i])
                              for i in range(1, len(PurePosixPath(name).parts) + 1)]
    for index, path in enumerate(components):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        final = index == len(components) - 1
        if stat.S_ISLNK(mode) or ((not final or directory) and not stat.S_ISDIR(mode)) or (final and not directory and not stat.S_ISREG(mode)):
            raise StateError('Unsafe filesystem entry in collector state.')
    return private / name


def _candidate_paths(private):
    private = Path(private)
    _safe_path(private, '', directory=True)
    result = []
    for name in sorted(FIXED_PATHS):
        path = _safe_path(private, name)
        if path.exists():
            result.append(name)
    for name in sorted(DYNAMIC_DIRS):
        directory = _safe_path(private, name, directory=True)
        if directory.exists():
            for entry in sorted(directory.iterdir()):
                relative = entry.relative_to(private).as_posix()
                if allowed_path(relative):
                    _safe_path(private, relative)
                    result.append(relative)
    if len(result) > MAX_FILES:
        raise StateError('Collector state exceeds its file count limit.')
    return sorted(result)


def _validate_config(raw):
    try:
        config = json.loads(raw)
    except (ValueError, UnicodeError):
        raise StateError('Invalid collector scope configuration.') from None
    if not isinstance(config, dict):
        raise StateError('Invalid collector scope configuration.')

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r'[-\s]', '_', key).lower()
                if normalized in FORBIDDEN_CONFIG_KEYS:
                    raise StateError('Credential fields are not allowed in collector configuration.')
                if normalized == 'headers':
                    if (not isinstance(child, dict) or not set(child) <= SCOPE_HEADERS
                            or any(not isinstance(v, str) or not v or len(v) > 256 or '\r' in v or '\n' in v for v in child.values())):
                        raise StateError('Only source scope headers may be persisted.')
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(config)


def build_manifest(private=None):
    private = Path(private or ROOT / '.private')
    files, total = [], 0
    for name in _candidate_paths(private):
        path = _safe_path(private, name)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise StateError('Collector state exceeds its file size limit.')
            data = handle.read(MAX_FILE_BYTES + 1)
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise StateError('Collector state exceeds its size limit.')
        if name == 'config.json':
            _validate_config(data)
        try:
            content = data.decode('utf-8')
        except UnicodeError:
            raise StateError('Collector state files must contain UTF-8 text.') from None
        # These JSON/TSV files are text. Encoding each one as base64 needlessly
        # inflates the encrypted archive and weakens cross-file compression.
        files.append({'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                      'encoding': 'utf8', 'data': content})
    if not files:
        raise StateError('No collector state is available to save.')
    manifest = {'schema_version': 1, 'purpose': PURPOSE,
                'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'files': files}
    if len(_json_bytes(manifest)) > MAX_MANIFEST_BYTES:
        raise StateError('Collector state manifest exceeds its size limit.')
    return manifest


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'purpose', 'created_at', 'files'}
            or manifest.get('schema_version') != 1 or manifest.get('purpose') != PURPOSE
            or not isinstance(manifest.get('created_at'), str) or not isinstance(manifest.get('files'), list)
            or not 0 < len(manifest['files']) <= MAX_FILES):
        raise StateError('Invalid collector state manifest.')
    files, seen, total = {}, set(), 0
    for item in manifest['files']:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256', 'encoding', 'data'}:
            raise StateError('Invalid collector state manifest entry.')
        name = item['path']
        if not allowed_path(name) or name.casefold() in seen:
            raise StateError('Unexpected or duplicate collector state path.')
        size = item['size']
        if (type(size) is not int or not 0 <= size <= MAX_FILE_BYTES or item['encoding'] != 'utf8'
                or not isinstance(item['data'], str) or len(item['data']) > MAX_FILE_BYTES):
            raise StateError('Invalid collector state file size.')
        total += size
        if total > MAX_TOTAL_BYTES:
            raise StateError('Collector state exceeds its size limit.')
        try:
            data = item['data'].encode('utf-8')
        except UnicodeError:
            raise StateError('Invalid collector state file encoding.') from None
        if len(data) != size or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise StateError('Collector state file integrity check failed.')
        if name == 'config.json':
            _validate_config(data)
        files[name] = data
        seen.add(name.casefold())
    return files


def restore_manifest(manifest, private=None):
    files = validate_manifest(manifest)
    private = Path(private or ROOT / '.private')
    # Validate all existing destinations before changing any of them.
    old = _candidate_paths(private)
    for name in files:
        _safe_path(private, name)
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    staging = Path(tempfile.mkdtemp(prefix='.restore-', dir=private))
    try:
        for name, data in files.items():
            destination = staging / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
        for name in files:
            destination = _safe_path(private, name)
            parent = private
            for part in PurePosixPath(name).parts[:-1]:
                parent = parent / part
                parent.mkdir(mode=0o700, exist_ok=True)
                os.chmod(parent, 0o700)
            os.replace(staging / name, destination)
            os.chmod(destination, 0o600)
        for name in set(old) - files.keys():
            _safe_path(private, name).unlink()
    finally:
        shutil.rmtree(staging)
    return len(files)


def crypt(operation, value, purpose=PURPOSE):
    raw = value.encode() if isinstance(value, str) else value
    if not isinstance(raw, bytes) or len(raw) > MAX_MANIFEST_BYTES:
        raise StateError('Encrypted state input exceeds its size limit.')
    result = subprocess.run(['node', str(ROOT / 'scripts/state_crypto.mjs'), operation, '-', '-', purpose],
                            input=raw, capture_output=True, cwd=ROOT)
    if result.returncode:
        raise StateError('Unable to authenticate encrypted collector state.')
    if len(result.stdout) > MAX_MANIFEST_BYTES:
        raise StateError('Encrypted state output exceeds its size limit.')
    return result.stdout


def _chunk_name(path):
    try:
        encoded = os.environ['DG_HUB_STATE_KEY']
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', encoded):
            raise ValueError()
        key = base64.urlsafe_b64decode(encoded + '=')
        if len(key) != 32 or base64.urlsafe_b64encode(key).decode().rstrip('=') != encoded:
            raise ValueError()
    except (KeyError, ValueError):
        raise StateError('A valid hosted state key is required.') from None
    naming_key = hmac.new(key, b'dg-operations-chunk-names-v1', hashlib.sha256).digest()
    return 'chunks/' + hmac.new(naming_key, path.encode(), hashlib.sha256).hexdigest() + '.enc.json'


def _chunk_purpose(name):
    if not CHUNK_RE.fullmatch(name):
        raise StateError('Invalid encrypted chunk name.')
    return 'dg-operations-chunk/' + name.split('/')[1].split('.')[0]


def crypt_many(operation, records):
    """Encrypt/decrypt a batch in one Node process; names reveal no source paths."""
    if not records:
        return {}
    request = [{'id': name, 'purpose': _chunk_purpose(name),
                'data': base64.urlsafe_b64encode(value).decode().rstrip('=')}
               for name, value in sorted(records.items())]
    try:
        result = json.loads(crypt(operation + '-batch', _json_bytes(request), 'batch'))
        if not isinstance(result, list) or len(result) != len(records):
            raise ValueError()
        output = {}
        for item in result:
            if (not isinstance(item, dict) or set(item) != {'id', 'data'} or item['id'] not in records
                    or item['id'] in output or not isinstance(item['data'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]*', item['data'])):
                raise ValueError()
            value = base64.urlsafe_b64decode(item['data'] + '=' * (-len(item['data']) % 4))
            if base64.urlsafe_b64encode(value).decode().rstrip('=') != item['data']:
                raise ValueError()
            output[item['id']] = value
        return output
    except (ValueError, TypeError, UnicodeError):
        raise StateError('Invalid encrypted chunk response.') from None


def _stored_manifest(encrypted):
    try:
        document = json.loads(crypt('decrypt', encrypted))
    except (ValueError, UnicodeError):
        raise StateError('Invalid encrypted collector manifest.') from None
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'purpose', 'created_at', 'files'}
            or document['schema_version'] != 2 or document['purpose'] != PURPOSE
            or not isinstance(document['created_at'], str) or not isinstance(document['files'], list)
            or not 0 < len(document['files']) <= MAX_FILES):
        raise StateError('Invalid encrypted collector manifest.')
    names, chunks, total = set(), set(), 0
    for item in document['files']:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256', 'encoding', 'chunk'}:
            raise StateError('Invalid encrypted collector manifest entry.')
        name = item['path']
        if (not allowed_path(name) or name.casefold() in names or item['encoding'] != 'utf8'
                or type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE_BYTES
                or not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                or item['chunk'] != _chunk_name(name) or item['chunk'] in chunks):
            raise StateError('Invalid encrypted collector manifest path or size.')
        total += item['size']
        if total > MAX_TOTAL_BYTES:
            raise StateError('Collector state exceeds its size limit.')
        names.add(name.casefold())
        chunks.add(item['chunk'])
    return document


def _download_archive(head):
    if not _valid_sha(head):
        raise StateError('Invalid generated state reference.')
    # One archive request retrieves all anonymous chunks, pinned to the commit
    # already checked by _branch. No per-file GitHub request fan-out is needed.
    result = subprocess.run(['gh', 'api', 'repos/' + REPO + '/tarball/' + head],
                            capture_output=True, cwd=ROOT, timeout=180)
    if result.returncode or len(result.stdout) > MAX_ARCHIVE_BYTES:
        raise StateError('Unable to download the encrypted state archive.')
    return result.stdout


def read_archive(raw, blobs):
    """Read ciphertext in memory; never extract archive paths to the filesystem."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ARCHIVE_BYTES:
        raise StateError('Encrypted state archive exceeds its size limit.')
    files, seen, root, total = {}, set(), None, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            for index, member in enumerate(archive):
                if index > MAX_FILES + 4:
                    raise StateError('Encrypted state archive has too many entries.')
                name = member.name.rstrip('/') if member.isdir() else member.name
                path = PurePosixPath(name)
                parts = path.parts
                if (not parts or path.is_absolute() or path.as_posix() != name or '\\' in name
                        or any(p in ('', '.', '..') for p in name.split('/')) or name in seen
                        or not re.fullmatch(r'[A-Za-z0-9_.-]+', parts[0])):
                    raise StateError('Unsafe path in encrypted state archive.')
                seen.add(name)
                root = root or parts[0]
                if parts[0] != root:
                    raise StateError('Encrypted state archive has multiple roots.')
                relative = '/'.join(parts[1:])
                if member.isdir():
                    if relative not in ('', 'chunks'):
                        raise StateError('Unexpected directory in encrypted state archive.')
                    continue
                if (not member.isfile() or member.issparse() or relative not in blobs
                        or not 0 < member.size <= MAX_ENCRYPTED_BYTES):
                    raise StateError('Unsafe entry in encrypted state archive.')
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise StateError('Encrypted state archive exceeds its size limit.')
                stream = archive.extractfile(member)
                if stream is None:
                    raise StateError('Missing encrypted state archive entry.')
                data = stream.read(member.size + 1)
                if (len(data) != member.size or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != blobs[relative]):
                    raise StateError('Encrypted state archive integrity check failed.')
                files[relative] = data
    except (tarfile.TarError, EOFError, OSError, ValueError):
        raise StateError('Invalid encrypted state archive.') from None
    if set(files) != set(blobs):
        raise StateError('Encrypted state archive inventory does not match its commit.')
    return files


def save(private=None):
    manifest = build_manifest(private)
    old_head, existing = _branch()
    previous = _stored_manifest(_read_blob(existing[STATE_FILE])) if STATE_FILE in existing else None
    previous_files = {item['path']: item for item in previous['files']} if previous else {}
    wanted, changed, files = set(), {}, []
    for item in manifest['files']:
        chunk = _chunk_name(item['path'])
        record = {k: v for k, v in item.items() if k != 'data'}
        record['chunk'] = chunk
        wanted.add(chunk)
        files.append(record)
        if previous_files.get(item['path']) != record or chunk not in existing:
            changed[chunk] = item['data'].encode('utf-8')
    existing_chunks = {name for name in existing if CHUNK_RE.fullmatch(name)}
    if previous and not changed and previous['files'] == files and existing_chunks == wanted:
        return len(files)
    stored = {**manifest, 'schema_version': 2, 'files': files}
    updates = {name: value.decode() for name, value in crypt_many('encrypt', changed).items()}
    updates[STATE_FILE] = crypt('encrypt', _json_bytes(stored)).decode()
    _publish(old_head, existing, updates, keep=wanted | {STATE_FILE, AUTH_FILE})
    return len(manifest['files'])


def restore(private=None):
    head, blobs = _branch()
    if STATE_FILE not in blobs:
        raise StateError('Hosted collector state has not been initialized.')
    archive = read_archive(_download_archive(head), blobs)
    stored = _stored_manifest(archive[STATE_FILE])
    wanted = {item['chunk'] for item in stored['files']}
    if wanted != {name for name in blobs if CHUNK_RE.fullmatch(name)}:
        raise StateError('Encrypted state chunk inventory does not match its manifest.')
    decoded = crypt_many('decrypt', {name: archive[name] for name in wanted})
    try:
        files = [{**{k: v for k, v in item.items() if k != 'chunk'}, 'data': decoded[item['chunk']].decode('utf-8')}
                 for item in stored['files']]
    except UnicodeError:
        raise StateError('Invalid decrypted collector state file encoding.') from None
    manifest = {**stored, 'schema_version': 1, 'files': files}
    return restore_manifest(manifest, private)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('restore', 'save'))
    args = parser.parse_args()
    try:
        count = restore() if args.operation == 'restore' else save()
    except Exception as error:
        print(str(error) if isinstance(error, StateError) else 'Hosted state operation failed.', file=sys.stderr)
        return 1
    print('Encrypted collector state ' + ('restored' if args.operation == 'restore' else 'saved') + f' ({count} files).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
