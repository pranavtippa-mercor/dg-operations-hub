#!/usr/bin/env python3
"""Hosted-only, one-read Studio response-shape probe; never prints source values."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import traceback
from unittest.mock import patch

import source_client
from runtime_config import load_config

ROOT = Path(__file__).resolve().parents[1]
MAX_DEPTH = 4
MAX_NODES = 60
MAX_STRING_SCAN = 1024 * 1024
MAX_CAPTURE_BYTES = 64 * 1024
DIAGNOSTIC_PATH = ROOT / '.private/diagnostics/studio-probe.json'
SECRET_ENV_NAMES = ('MERCOR_API_KEY', 'DG_HUB_ACCESS_KEY', 'DG_HUB_STATE_KEY',
                    'GH_TOKEN', 'GITHUB_TOKEN')
KNOWN_KEYS = frozenset({
    'content', 'structuredContent', 'toolResult', 'result', 'data', 'response',
    'body', 'payload', 'rows', 'columns', 'text', 'json', 'isError', 'error',
    'errors', 'ok', 'success', 'status', 'status_code', 'metadata', '_meta',
    '_mercor_rid', 'pagination', 'pagination_info', 'next_cursor',
})


def capture_private_result(raw):
    """Write only a bounded, credential-redacted source result and UTC timestamp."""
    result = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, separators=(',', ':'))
    secrets = {os.environ[name] for name in SECRET_ENV_NAMES if os.environ.get(name)}
    # Object results contain JSON escapes; cover these as well as literal values.
    variants = secrets | {json.dumps(secret, ensure_ascii=ascii_only)[1:-1]
                          for secret in secrets for ascii_only in (False, True)}
    for secret in sorted(variants, key=len, reverse=True):
        result = result.replace(secret, '[REDACTED]')
    at = datetime.now(timezone.utc).isoformat()

    def serialize(text):
        return json.dumps({'result': text, 'datetime': at}, ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8')

    # Redact before truncation, so a boundary cannot retain part of a credential.
    prefix = result[:MAX_CAPTURE_BYTES]
    truncated = len(result) > len(prefix)
    data = serialize(prefix)
    if truncated or len(data) > MAX_CAPTURE_BYTES:
        lower, upper = 0, len(prefix)
        while lower < upper:
            middle = (lower + upper + 1) // 2
            if len(serialize(prefix[:middle] + '\n[TRUNCATED]')) <= MAX_CAPTURE_BYTES:
                lower = middle
            else:
                upper = middle - 1
        data = serialize(prefix[:lower] + '\n[TRUNCATED]')
    DIAGNOSTIC_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.studio-probe-', dir=DIAGNOSTIC_PATH.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
        os.replace(temporary, DIAGNOSTIC_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def value_type(value):
    for kind, name in ((dict, 'object'), (list, 'array'), (str, 'string'),
                       (bool, 'boolean'), (int, 'number'), (float, 'number')):
        if isinstance(value, kind):
            return name
    return 'null' if value is None else 'unsupported'


def shape(value, depth=0, budget=None):
    """Bounded shape only: keys are allowlisted and values are never emitted."""
    budget = budget if budget is not None else [MAX_NODES]
    kind = value_type(value)
    result = {'type': kind}
    if isinstance(value, (str, dict, list)):
        result['length'] = len(value)
    if depth >= MAX_DEPTH or budget[0] <= 0:
        result['depth_or_node_limit'] = True
        return result
    budget[0] -= 1
    if isinstance(value, dict):
        keys = sorted(KNOWN_KEYS.intersection(value))
        result['known_envelope_keys'] = keys
        result['other_key_count'] = len(value) - len(keys)
        result['fields'] = {key: shape(value[key], depth + 1, budget) for key in keys}
    elif isinstance(value, list):
        result['sample_shapes'] = [shape(item, depth + 1, budget) for item in value[:3]]
        result['sample_limited'] = len(value) > 3
    elif isinstance(value, str):
        scanned = value[:MAX_STRING_SCAN]
        stripped = scanned.lstrip()
        lowered = scanned.casefold()
        flags = {
            'starts_json_object': stripped.startswith('{'),
            'starts_json_array': stripped.startswith('['),
            'fenced_json': bool(re.search(r'```json\b', scanned, re.I)),
            'contains_metadata_tag': '<METADATA>' in scanned,
            'contains_tsv_tag': '<TSV_DATA>' in scanned,
            'contains_sse_data': bool(re.search(r'^data:', scanned, re.M)),
            'contains_unauthorized': 'unauthorized' in lowered,
            'contains_forbidden': 'forbidden' in lowered,
            'contains_permission': 'permission' in lowered,
            'contains_access_denied': 'access denied' in lowered,
            'contains_not_found': 'not found' in lowered,
            'contains_error': 'error' in lowered,
        }
        result['string_flags'] = flags
        result['scan_limited'] = len(value) > MAX_STRING_SCAN
        result['json_parse_attempted'] = not result['scan_limited']
        if result['json_parse_attempted']:
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                result['json_parse_success'] = False
            else:
                result['json_parse_success'] = True
                result['json_type'] = value_type(decoded)
                result['decoded_shape'] = shape(decoded, depth + 1, budget)
    return result


def safe_exception(error):
    return {'type': type(error).__name__, 'frames': [
        {'file': Path(frame.f_code.co_filename).name,
         'function': frame.f_code.co_name, 'line': line}
        for frame, line in traceback.walk_tb(error.__traceback__)
    ][-12:]}


def inventory_query(world):
    if not isinstance(world, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', world):
        raise ValueError('Invalid private source scope.')
    # Exact bounded read used by collect.py. No identifiers or SQL are printed.
    return ("SELECT t.task_id,t.task_name,t.task_status_id AS status,t.owned_by,t.updated_by,t.updated_at,t.transitioned_at,t.version,"
            "t.custom_fields->>'artifact_type' AS artifact,t.custom_fields->>'task_mode' AS task_mode,"
            "t.custom_fields->>'rfd_due' AS rfd_due,t.custom_fields->>'domain' AS domain,"
            "t.custom_fields->>'blocked' AS blocked,t.custom_fields->>'reviewer' AS reviewer,"
            "t.custom_fields->>'expert' AS expert,t.custom_fields->>'writer' AS writer FROM tasks t "
            "WHERE t.world_id='" + world + "' AND t.archived_at IS NULL ORDER BY t.task_name LIMIT 1000")


def probe(config, client_factory=None):
    report = {'probe': 'studio_task_inventory_shape', 'ok': False, 'source_calls': 0}
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        report['hosted_required'] = True
        return report
    decode = source_client.decode_rest_response

    def observe(raw):
        capture_private_result(raw)
        report['transport_value_shape'] = shape(raw)
        return decode(raw)

    try:
        query = inventory_query(config['studio']['world'])
        factory = client_factory or source_client.SourceClient
        with patch.object(source_client, 'decode_rest_response', observe):
            with factory(config, upstream_tools=['studio'], timeout=60) as client:
                report['source_calls'] = 1
                value = client.studio('POST', '/querier/unstructured', {'query': query})
        report['collector_value_shape'] = shape(value)
        report['ok'] = True
    except Exception as error:
        report['exception'] = safe_exception(error)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT/'.private/config.json'))
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        report = {'probe': 'studio_task_inventory_shape', 'ok': False, 'hosted_required': True, 'source_calls': 0}
    else:
        try:
            report = probe(load_config(args.config))
        except Exception as error:
            report = {'probe': 'studio_task_inventory_shape', 'ok': False, 'source_calls': 0,
                      'exception': safe_exception(error)}
    print(json.dumps(report, separators=(',', ':')))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
