"""Read-only collectors' transport to the documented Mercor REST tool gateway.

Tool arguments are the JSON body. The dedicated API key is read only from the
process environment, and credentials are sent only to the fixed HTTPS gateway.
"""
import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request

BASE_URL = 'https://coil.mercor.com/tools/'
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class SourceHTTPError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__('Source request failed (HTTP %s); prior evidence retained.' % status)


class SourceReportedError(RuntimeError):
    """The source reported failure; its private error text is intentionally omitted."""


class SourceAuthenticationError(SourceReportedError):
    """The upstream requires a different or renewed authenticated connection."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Source redirect refused; credentials were not forwarded.')


def reject_reported_error(value):
    if isinstance(value, str) and value.lstrip().lower().startswith('needs_auth:'):
        raise SourceAuthenticationError('Source authentication is required; prior evidence retained.')
    if isinstance(value, dict) and (value.get('isError') is True
            or value.get('ok') is False or value.get('success') is False
            or value.get('error') not in (None, False, '', {}, [])):
        raise SourceReportedError('Source reported a read failure; prior evidence retained.')
    return value


def response_value(response, deadline):
    """Bound body size and elapsed time, and return the native JSON or text value."""
    chunks, total = [], 0
    # HTTPResponse.read1 does at most one underlying read. Together with a
    # shrinking socket timeout, a slowly streaming body cannot reset the budget.
    read = getattr(response, 'read1', response.read)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = read(min(65536, MAX_RESPONSE_BYTES + 1 - total))
        if time.monotonic() > deadline:
            raise TimeoutError
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise SourceReportedError('Source response exceeds the configured limit.')
        if not chunk:
            break
        chunks.append(chunk)
    body = b''.join(chunks).decode('utf-8')
    kind = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    if not body.strip() or kind in ('text/html', 'text/event-stream'):
        raise SourceReportedError('Source returned an unsupported or empty response.')
    try:
        value = json.loads(body)
    except ValueError:
        if kind == 'application/json' or kind.endswith('+json'):
            raise SourceReportedError('Source returned invalid JSON evidence.') from None
        value = body
    return reject_reported_error(value)


class RemoteSource:
    def __init__(self, timeout=180, opener=None):
        self.token = os.environ.get('MERCOR_API_KEY', '')
        if len(self.token) < 16 or not all(0x21 <= ord(c) <= 0x7e for c in self.token):
            raise RuntimeError('The hosted Mercor API key is missing or invalid.')
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('The source timeout must be a positive finite number.')
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(NoRedirects())

    def call_tool(self, name, arguments):
        # SourceClient enforces the read-only allowlist before reaching here.
        # This guard also prevents URL-path manipulation if called independently.
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', name):
            raise ValueError('Invalid source tool name.')
        if not self.token:
            raise RuntimeError('The source connection is closed.')
        try:
            body = json.dumps(arguments, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError):
            raise ValueError('Source arguments must be valid JSON.') from None
        request = urllib.request.Request(BASE_URL + name, data=body, headers={
            'Authorization': 'Bearer ' + self.token,
            'Accept': 'application/json, text/plain',
            'Content-Type': 'application/json',
            'User-Agent': 'DG-Operations-Hub/1.0',
        }, method='POST')
        deadline = time.monotonic() + self.timeout
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise SourceHTTPError(response.status)
                return response_value(response, deadline)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise SourceHTTPError(status) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
            raise RuntimeError('Source connection failed; prior evidence retained.') from None

    def close(self):
        self.token = ''
