"""Native Studio reads with an environment-only, campaign-scoped API key."""
import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from remote_source import NoRedirects, SourceHTTPError, SourceReportedError, response_value

BASE_URL = 'https://api.studio.mercor.com'
SCOPE_HEADERS = frozenset(('X-Campaign-Id', 'X-Company-Id', 'X-Account-Id'))
GET_PATH = re.compile(r'/(?:worlds/[A-Za-z0-9_-]+|users/campaign/[A-Za-z0-9_-]+|'
                      r'qc-audits/(?:[A-Za-z0-9_-]+)?|qc-specs/summary)\Z')
ARGUMENTS = frozenset(('method', 'path', 'params', 'headers', 'evidence'))


class RemoteStudio:
    def __init__(self, config, timeout=180, opener=None):
        self.token = os.environ.get('STUDIO_API_KEY', '')
        if (not self.token.startswith('rls-sk-') or len(self.token) < 16
                or not all(0x21 <= ord(c) <= 0x7e for c in self.token)):
            raise RuntimeError('The native Studio API key is missing or invalid.')
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('The source timeout must be a positive finite number.')
        studio = config.get('studio') if isinstance(config, dict) else None
        headers = studio.get('headers') if isinstance(studio, dict) else None
        if (not isinstance(headers, dict) or not set(headers) <= SCOPE_HEADERS
                or 'X-Campaign-Id' not in headers
                or any(not isinstance(value, str) or not 0 < len(value) <= 256
                       or not all(0x21 <= ord(c) <= 0x7e for c in value)
                       for value in headers.values())):
            raise ValueError('Studio requires valid configured source scope headers only.')
        self.headers = dict(headers)
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirects())

    def call_tool(self, name, arguments):
        # Validate again at this boundary, even when called without SourceClient.
        from source_client import validate_call
        if name != 'studio' or not isinstance(arguments, dict) or not set(arguments) <= ARGUMENTS:
            raise ValueError('Only native Studio read arguments are accepted.')
        method, path = arguments.get('method'), arguments.get('path')
        params = arguments.get('params')
        if not isinstance(method, str) or not isinstance(path, str) or not (params is None or isinstance(params, dict)):
            raise ValueError('Invalid native Studio request shape.')
        method = method.upper()
        if method == 'POST' and not isinstance((params or {}).get('query'), str):
            raise ValueError('Studio SELECT query must be a string.')
        validate_call('studio', arguments)
        if not ((method == 'GET' and GET_PATH.fullmatch(path))
                or (method == 'POST' and path == '/querier/unstructured')):
            raise ValueError('Studio endpoint is outside the collector read scope.')
        if 'headers' in arguments and arguments['headers'] != self.headers:
            raise ValueError('Studio request headers cannot override configured scope.')
        if not self.token:
            raise RuntimeError('The Studio source connection is closed.')
        try:
            encoded = json.dumps(params or {}, allow_nan=False).encode('utf-8')
            query = urllib.parse.urlencode(params or {}, doseq=True) if method == 'GET' else ''
        except (TypeError, ValueError):
            raise ValueError('Studio parameters must be valid JSON and query values.') from None
        # Studio's published client uses this UA for its Cloudflare front end.
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json',
                   'User-Agent': 'curl/8.0', **self.headers}
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(BASE_URL + path + ('?' + query if query else ''),
                                         data=encoded if method == 'POST' else None,
                                         headers=headers, method=method)
        deadline = time.monotonic() + self.timeout
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise SourceHTTPError(response.status)
                kind = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                if kind != 'application/json' and not kind.endswith('+json'):
                    raise SourceReportedError('Studio returned an unexpected response format.')
                value = response_value(response, deadline)
                if not isinstance(value, (dict, list)):
                    raise SourceReportedError('Studio returned no native JSON object or list.')
                return value
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise SourceHTTPError(status) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
            raise RuntimeError('Studio source connection failed; prior evidence retained.') from None

    def close(self):
        self.token = ''
