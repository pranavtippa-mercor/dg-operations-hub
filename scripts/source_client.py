"""Read-only source calls through the user's existing authenticated Codex connection."""
import json
import fcntl
import re
import sys
from pathlib import Path

READ_TOOLS = frozenset({
    'studio', 'slack_read_thread', 'slack_search_public_and_private',
    'slack_search_public', 'slack_list_channel_history',
    'datadog_list_datadog_skills', 'datadog_load_datadog_skill',
    'datadog_analyze_datadog_logs', 'datadog_search_datadog_logs',
})


def decode_response(raw):
    for key in ('toolResult', 'result'):
        if isinstance(raw, dict) and isinstance(raw.get(key), dict):
            raw = raw[key]
    if not isinstance(raw, dict) or raw.get('isError'):
        raise RuntimeError('Source read failed; previous evidence retained.')
    structured = raw.get('structuredContent')
    if isinstance(structured, dict):
        value = structured.get('result', structured)
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value
    texts = [p['text'] for p in raw.get('content', []) if p.get('type') == 'text']
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            return texts[0]
    if texts:
        return '\n'.join(texts)
    raise RuntimeError('Source returned no readable evidence.')


def validate_call(tool, args):
    if tool not in READ_TOOLS:
        raise ValueError('Source tool is not on the read-only allowlist.')
    if tool == 'studio':
        method = args.get('method', '').upper()
        if method == 'GET':
            return
        query = (args.get('params') or {}).get('query', '')
        if (method != 'POST' or args.get('path') != '/querier/unstructured'
                or not re.match(r'^\s*SELECT\b', query, re.I)
                or any(token in query for token in (';', '--', '/*'))
                or re.search(r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|GRANT|CREATE|TRUNCATE|EXECUTE|CALL)\b', query, re.I)):
            raise ValueError('Only Studio GETs and a single SELECT query are allowed.')


class SourceClient:
    def __init__(self, config, upstream_tools=None, timeout=180):
        self.config = config
        self.tools = list(upstream_tools or READ_TOOLS)
        if not set(self.tools) <= READ_TOOLS:
            raise ValueError('Unexpected source tool.')
        self.timeout = timeout
        self.bridge = None

    def call(self, tool_name, arguments):
        validate_call(tool_name, arguments)
        if tool_name not in self.tools:
            raise ValueError('Source tool was not enabled for this client.')
        # The connector can stall under concurrent calls. Serialize RPCs across
        # both collector threads and the separate task snapshot subprocess.
        path = Path(self.config.get('source_lock', str(Path(__file__).resolve().parents[1]/'.private/source.lock')))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return self._call(tool_name, arguments)

    def _call(self, tool_name, arguments):
        if self.bridge is None:
            adapter = Path(self.config.get('bridge_dir', str(Path.home() / 'Documents/Ryu/Tools')))
            sys.path.insert(0, str(adapter))
            from google_read_mcp import CodexBridge
            self.bridge = CodexBridge(upstream_tools=self.tools, timeout=self.timeout)
        return decode_response(self.bridge.rpc('mcpServer/tool/call', {
            'threadId': self.bridge.thread, 'server': 'mercor-mcp',
            'tool': tool_name, 'arguments': arguments,
        }))

    def studio(self, method, path, params=None):
        return self.call('studio', {
            'method': method, 'path': path, 'params': params,
            'headers': self.config['studio']['headers'],
            'evidence': {'rationale': 'Read-only source collection for the unified DG operations app, authorized by the user. No Studio mutations.', 'variables': {}},
        })

    def close(self):
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
