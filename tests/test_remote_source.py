import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote_source import BASE_URL, NoRedirects, RemoteSource, SourceHTTPError, SourceReportedError
from source_client import SourceClient, decode_rest_response


class Response(io.BytesIO):
    def __init__(self, body, content_type='application/json', status=200):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = {'Content-Type': content_type}
        self.status = status


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.options = []

    def open(self, request, **kwargs):
        self.requests.append(request)
        self.options.append(kwargs)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class RemoteSourceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'MERCOR_API_KEY': 'test-source-key-not-a-real-credential'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_fixed_endpoint_bearer_and_raw_tool_arguments(self):
        args = {'method': 'GET', 'path': '/tasks', 'params': {'limit': 3}}
        native = {'result': {'rows': [{'id': 'example'}]}, 'next_cursor': None}
        opener = Opener([Response(native)])
        client = RemoteSource(timeout=12, opener=opener)
        self.assertEqual(client.call_tool('studio', args), native)
        request = opener.requests[0]
        self.assertEqual(request.full_url, 'https://coil.mercor.com/tools/studio')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(json.loads(request.data), args)
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-source-key-not-a-real-credential')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(opener.options[0]['timeout'], 12)
        self.assertIsNone(request.get_header('Mcp-session-id'))

    def test_native_json_lists_strings_and_plain_text(self):
        values = [{'result': {'content': 'native data'}}, [{'row': 1}], 'JSON string']
        opener = Opener([Response(x) for x in values] + [Response('[Writer · 100.0] Hello'.encode(), 'text/plain')])
        client = RemoteSource(opener=opener)
        for expected in values:
            self.assertEqual(client.call_tool('slack_read_thread', {}), expected)
        self.assertEqual(client.call_tool('slack_read_thread', {}), '[Writer · 100.0] Hello')

    def test_native_result_and_content_fields_are_not_guessed_wrappers(self):
        for value in [{'result': {'rows': []}}, {'toolResult': {'rows': []}},
                      {'result': {'content': [{'type': 'text', 'text': 'native'}]}},
                      {'content': [{'type': 'text', 'text': 'native'}], 'page': 2}]:
            self.assertEqual(decode_rest_response(value), value)

    def test_standard_mcp_tool_results_use_the_existing_decoder(self):
        self.assertEqual(decode_rest_response({'content': [{'type': 'text', 'text': '{"rows": [1]}'}]}), {'rows': [1]})
        self.assertEqual(decode_rest_response({'content': [{'type': 'text', 'text': 'summary'}],
                                              'structuredContent': {'result': '{"rows": [2]}'}}), {'rows': [2]})
        multi = {'content': [{'type': 'text', 'text': 'first'}, {'type': 'text', 'text': 'second'}]}
        self.assertEqual(decode_rest_response(multi), 'first\nsecond')
        with self.assertRaises(SourceReportedError):
            decode_rest_response({'content': [{'type': 'text', 'text': '{"error":"private detail"}'}]})

    def test_hosted_client_preserves_plain_payload_and_decodes_mcp(self):
        native = {'result': {'rows': [1]}}
        opener = Opener([Response(native), Response({'content': [{'type': 'text', 'text': '{"rows":[2]}'}]})])
        with tempfile.TemporaryDirectory() as directory, patch('remote_source.urllib.request.build_opener', return_value=opener):
            config = {'source_transport': 'https', 'source_lock': str(Path(directory)/'source.lock')}
            with SourceClient(config) as client:
                self.assertEqual(client.call('studio', {'method': 'GET', 'path': '/tasks'}), native)
                self.assertEqual(client.call('studio', {'method': 'GET', 'path': '/tasks'}), {'rows': [2]})
        self.assertEqual(len(opener.requests), 2)

    def test_github_runner_never_falls_back_to_local_codex(self):
        opener = Opener([Response({'rows': []})])
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch('remote_source.urllib.request.build_opener', return_value=opener):
            with SourceClient({'source_lock': str(Path(directory)/'source.lock'), 'bridge_dir': '/does-not-exist'}) as client:
                self.assertEqual(client.call('studio', {'method': 'GET', 'path': '/tasks'}), {'rows': []})

    def test_http_errors_and_connection_failures_are_sanitized(self):
        for status in (401, 403, 429, 500):
            error = urllib.error.HTTPError(BASE_URL, status, 'private server detail', {}, io.BytesIO(b'private body'))
            with self.assertRaises(SourceHTTPError) as raised:
                RemoteSource(opener=Opener([error])).call_tool('studio', {})
            self.assertEqual(raised.exception.status, status)
            self.assertNotIn('private', str(raised.exception))
            self.assertNotIn(os.environ['MERCOR_API_KEY'], str(raised.exception))
        with self.assertRaises(RuntimeError) as raised:
            RemoteSource(opener=Opener([urllib.error.URLError('private network detail')])).call_tool('studio', {})
        self.assertNotIn('private network detail', str(raised.exception))

    def test_reported_errors_are_distinct_and_sanitized(self):
        for value in [{'error': {'message': 'private detail'}}, {'isError': True, 'content': []},
                      {'ok': False, 'reason': 'private detail'}, {'success': False, 'message': 'private detail'}]:
            with self.assertRaises(SourceReportedError) as raised:
                RemoteSource(opener=Opener([Response(value)])).call_tool('studio', {})
            self.assertNotIn('private detail', str(raised.exception))
        self.assertEqual(RemoteSource(opener=Opener([Response({'error': None, 'rows': []})])).call_tool('studio', {}), {'error': None, 'rows': []})

    def test_redirects_and_manipulated_tool_paths_are_refused(self):
        for status in (301, 302, 303, 307, 308):
            with self.assertRaisesRegex(RuntimeError, 'redirect refused'):
                NoRedirects().redirect_request(None, None, status, '', {}, 'https://untrusted.example')
        opener = Opener([])
        for name in ('../studio', 'studio?redirect=x', 'https://untrusted.example', 'studio/other'):
            with self.assertRaises(ValueError):
                RemoteSource(opener=opener).call_tool(name, {})
        self.assertEqual(opener.requests, [])

    def test_missing_or_invalid_key_fails_without_local_login(self):
        for key in ('', 'short', 'mercor-sk-invalid\r\nvalue', 'mercor-sk-with space'):
            with patch.dict(os.environ, {'MERCOR_API_KEY': key}):
                with self.assertRaises(RuntimeError):
                    RemoteSource()
        with patch.dict(os.environ, {'MERCOR_API_KEY': ''}):
            with SourceClient({'source_transport': 'https', 'MERCOR_API_KEY': 'config-value-is-not-used'}) as client:
                with self.assertRaises(RuntimeError):
                    client._call('studio', {'method': 'GET'})
                self.assertIsNone(client.bridge)

    def test_mutations_are_refused_before_transport_or_lock_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'source.lock'
            with SourceClient({'source_transport': 'https', 'source_lock': str(path)}) as client:
                for tool, args in [('slack_send_message', {}), ('studio', {'method': 'DELETE', 'path': '/tasks/example'}),
                                   ('studio', {'method': 'POST', 'path': '/querier/unstructured', 'params': {'query': 'DELETE FROM tasks'}})]:
                    with self.assertRaises(ValueError):
                        client.call(tool, args)
                self.assertIsNone(client.bridge)
                self.assertFalse(path.exists())

    def test_response_size_and_total_deadline_are_bounded(self):
        with patch('remote_source.MAX_RESPONSE_BYTES', 16):
            with self.assertRaisesRegex(SourceReportedError, 'exceeds'):
                RemoteSource(opener=Opener([Response(b'x'*17, 'text/plain')])).call_tool('studio', {})
        with patch('remote_source.time.monotonic', side_effect=[0, 0, 6]):
            with self.assertRaisesRegex(RuntimeError, 'connection failed'):
                RemoteSource(timeout=5, opener=Opener([Response({'rows': []})])).call_tool('studio', {})

    def test_invalid_or_unsupported_response_is_not_accepted_as_evidence(self):
        for body, kind in [(b'private broken JSON', 'application/json'), (b'', 'application/json'),
                           (b'<html>private login form</html>', 'text/html'), (b'data: private', 'text/event-stream')]:
            with self.assertRaises(SourceReportedError) as raised:
                RemoteSource(opener=Opener([Response(body, kind)])).call_tool('studio', {})
            self.assertNotIn('private', str(raised.exception))

    def test_close_removes_credentials_and_prevents_calls(self):
        opener = Opener([])
        client = RemoteSource(opener=opener)
        client.close()
        self.assertEqual(client.token, '')
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            client.call_tool('studio', {})
        self.assertEqual(opener.requests, [])


if __name__ == '__main__':
    unittest.main()
