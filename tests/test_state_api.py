"""Hosted GitHub retries must remain bounded, private, and safe for branch writers."""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('state_api_under_test', ROOT / 'scripts/hosted_state.py')
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)
SECRET = 'private-example-token-and-source-content'
REF_PATH = '/git/ref/heads/collector-state'


def response(status=200, value=None, *, headers=None, stderr='', raw=None):
    lines = [f'HTTP/2.0 {status} Example']
    lines += [f'{name}: {value}' for name, value in (headers or {}).items()]
    payload = json.dumps(value) if raw is None else raw
    return subprocess.CompletedProcess(
        ['gh'], 0 if 200 <= status < 300 else 1,
        '\r\n'.join(lines) + '\r\n\r\n' + payload, stderr)


def head_response(sha):
    return response(200, {'object': {'sha': sha, 'type': 'commit'}}) if sha else response(404)


class PatchedTestCase(unittest.TestCase):
    def enterContext(self, manager):
        # unittest.TestCase.enterContext is only available starting with Python 3.11.
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value


class StateApiTests(PatchedTestCase):
    def setUp(self):
        self.request = self.enterContext(patch.object(state.subprocess, 'run'))
        self.request.side_effect = AssertionError('Unmocked GitHub request')
        self.sleep = self.enterContext(patch.object(state.time, 'sleep'))
        self.enterContext(patch.object(state.time, 'time', return_value=1000))
        self.logs = io.StringIO()
        self.enterContext(patch.object(state.sys, 'stderr', self.logs))
        self.save_diagnostic = self.enterContext(patch.object(state, '_save_failure_diagnostic'))

    def test_server_error_recovers_and_request_body_is_reused(self):
        self.request.side_effect = [response(503, {'message': SECRET}, stderr=SECRET),
                                response(201, {'sha': 'a' * 40})]
        value = state.api('/git/trees', 'POST', {'tree': [{'content': SECRET}]})
        self.assertEqual(value, {'sha': 'a' * 40})
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(self.request.call_args_list[0], self.request.call_args_list[1])
        self.assertEqual(self.request.call_args.kwargs['timeout'], state.API_TIMEOUT)
        self.assertIn('--include', self.request.call_args.args[0])
        self.sleep.assert_called_once_with(2)
        self.assertIn('operation=create_tree http=503', self.logs.getvalue())
        self.assertIn('recovered on attempt=2', self.logs.getvalue())
        self.assertNotIn(SECRET, self.logs.getvalue())

    def test_server_error_exhaustion_has_bounded_attempts_and_no_private_output(self):
        self.request.side_effect = [response(503, {'message': SECRET}, stderr=SECRET)] * state.API_ATTEMPTS
        with self.assertRaises(state.StateError) as raised:
            state.api(REF_PATH)
        self.assertEqual(self.request.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(4), call(8)])
        diagnostic = str(raised.exception) + self.logs.getvalue()
        self.assertIn('operation=read_ref http=503 category=server_error attempt=4/4', diagnostic)
        self.assertNotIn(SECRET, diagnostic)

    def test_timeout_is_retried_without_exposing_exception_output(self):
        self.request.side_effect = [subprocess.TimeoutExpired(
            ['gh', SECRET], 60, output=SECRET, stderr=SECRET), response(200, {'ok': True})]
        self.assertEqual(state.api(REF_PATH), {'ok': True})
        self.sleep.assert_called_once_with(2)
        self.assertIn('category=timeout', self.logs.getvalue())
        self.assertNotIn(SECRET, self.logs.getvalue())

    def test_recognized_network_error_is_retried(self):
        self.request.side_effect = [subprocess.CompletedProcess(
            ['gh'], 1, '', 'connection reset by peer ' + SECRET), response(200, {'ok': True})]
        self.assertEqual(state.api(REF_PATH), {'ok': True})
        self.sleep.assert_called_once_with(2)
        self.assertIn('category=transport_error', self.logs.getvalue())
        self.assertNotIn(SECRET, self.logs.getvalue())

    def test_start_failure_is_sanitized_and_not_retried(self):
        self.request.side_effect = OSError(SECRET)
        with self.assertRaisesRegex(state.StateError, 'could not start') as raised:
            state.api(REF_PATH)
        self.assertNotIn(SECRET, str(raised.exception) + self.logs.getvalue())
        self.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_permanent_failures_are_not_retried(self):
        for status, category in ((401, 'authentication'), (403, 'permission'),
                                 (404, 'not_found'), (409, 'conflict'), (422, 'validation')):
            with self.subTest(status=status):
                self.request.reset_mock()
                self.request.side_effect = [response(status, {'message': SECRET}, stderr=SECRET)]
                with self.assertRaises(state.StateError) as raised:
                    state.api(REF_PATH)
                self.request.assert_called_once()
                self.sleep.assert_not_called()
                self.assertIn(f'http={status} category={category} attempt=1/4', str(raised.exception))
                self.assertNotIn(SECRET, str(raised.exception))

    def test_expected_missing_read_returns_none_without_retry(self):
        self.request.side_effect = [response(404, {'message': SECRET})]
        self.assertIsNone(state.api(REF_PATH, missing=True))
        self.request.assert_called_once()
        self.sleep.assert_not_called()
        self.assertEqual(self.logs.getvalue(), '')

    def test_missing_does_not_suppress_write_failure(self):
        self.request.side_effect = [response(404)]
        with self.assertRaisesRegex(state.StateError, 'http=404'):
            state.api('/git/refs', 'POST', {'ref': 'example'}, missing=True)
        self.request.assert_called_once()

    def test_status_is_available_when_gh_only_reports_it_in_stderr(self):
        self.request.side_effect = [subprocess.CompletedProcess(
            ['gh'], 1, SECRET, 'gh: forbidden ' + SECRET + ' (HTTP 403)')]
        with self.assertRaisesRegex(state.StateError, 'http=403 category=permission') as raised:
            state.api(REF_PATH)
        self.assertNotIn(SECRET, str(raised.exception))
        self.sleep.assert_not_called()

    def test_unclassified_cli_error_is_not_retried(self):
        self.request.side_effect = [subprocess.CompletedProcess(['gh'], 1, SECRET, SECRET)]
        with self.assertRaisesRegex(state.StateError, 'category=request_error') as raised:
            state.api(REF_PATH)
        self.assertNotIn(SECRET, str(raised.exception))
        self.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_retry_after_is_honored_and_each_sleep_is_at_most_one_minute(self):
        self.request.side_effect = [response(429, headers={'Retry-After': '90'}), response(200, {})]
        self.assertEqual(state.api(REF_PATH), {})
        self.assertEqual(self.sleep.call_args_list, [call(60), call(30)])
        self.assertIn('category=rate_limit', self.logs.getvalue())

    def test_primary_rate_limit_waits_through_reset(self):
        self.request.side_effect = [response(403, headers={
            'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1090'}), response(200, {})]
        self.assertEqual(state.api(REF_PATH), {})
        self.assertEqual(self.sleep.call_args_list, [call(60), call(31)])

    def test_secondary_rate_limit_defaults_to_one_minute(self):
        self.request.side_effect = [response(403, {'message': 'secondary rate limit ' + SECRET}),
                                response(200, {})]
        self.assertEqual(state.api(REF_PATH), {})
        self.sleep.assert_called_once_with(60)
        self.assertNotIn(SECRET, self.logs.getvalue())

    def test_retry_after_http_date_is_honored(self):
        self.request.side_effect = [response(503, headers={
            'Retry-After': 'Thu, 01 Jan 1970 00:18:00 GMT'}), response(200, {})]
        self.assertEqual(state.api(REF_PATH), {})
        self.assertEqual(self.sleep.call_args_list, [call(60), call(21)])

    def test_server_cooldown_larger_than_budget_is_not_shortened(self):
        self.request.side_effect = [response(429, headers={'Retry-After': '121'})]
        with self.assertRaisesRegex(state.StateError, 'server_wait=121s exceeds retry budget'):
            state.api(REF_PATH)
        self.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_rate_limit_wait_budget_is_cumulative(self):
        self.request.side_effect = [response(429), response(429)]
        with self.assertRaisesRegex(state.StateError, 'server_wait=120s exceeds retry budget'):
            state.api(REF_PATH)
        self.assertEqual(self.request.call_count, 2)
        self.sleep.assert_called_once_with(60)

    def test_untrusted_headers_and_path_are_never_logged(self):
        self.request.side_effect = [response(429, {'message': SECRET}, headers={
            'Retry-After': SECRET, 'X-RateLimit-Reset': SECRET, 'Authorization': SECRET},
            stderr=SECRET), response(200, {})]
        self.assertEqual(state.api('/unknown/' + SECRET), {})
        self.sleep.assert_called_once_with(60)
        self.assertIn('operation=other_request', self.logs.getvalue())
        self.assertNotIn(SECRET, self.logs.getvalue())

    def test_invalid_success_json_is_sanitized_and_not_retried(self):
        self.request.side_effect = [response(200, raw=SECRET)]
        with self.assertRaisesRegex(state.StateError, 'invalid state metadata') as raised:
            state.api(REF_PATH)
        self.assertNotIn(SECRET, str(raised.exception))
        self.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_reference_write_without_reconciliation_is_not_retried(self):
        for path, method in (('/git/refs', 'POST'), ('/git/refs/heads/collector-state', 'PATCH')):
            with self.subTest(method=method):
                self.request.reset_mock()
                self.request.side_effect = [response(503)]
                with self.assertRaisesRegex(state.StateError, 'attempt=1/1'):
                    state.api(path, method, {'sha': 'a' * 40})
                self.request.assert_called_once()
                self.sleep.assert_not_called()


    def test_validation_diagnostic_exposes_shape_and_allowlisted_provider_categories_only(self):
        provider = {'message': 'Validation Failed ' + SECRET, 'errors': [
            {'resource': 'Tree', 'field': 'tree', 'code': 'custom',
             'message': 'This request took too long to process: ' + SECRET,
             'value': SECRET},
            {'resource': SECRET, 'field': SECRET, 'code': SECRET, 'message': SECRET},
        ]}
        self.request.side_effect = [response(422, provider, headers={
            'X-GitHub-Request-Id': 'abcd:12:3456:7890:abcdef12', 'Authorization': SECRET})]
        body = {'tree': [{'path': SECRET, 'content': SECRET}, {'path': SECRET, 'sha': 'a' * 40}]}
        with self.assertRaisesRegex(state.StateError, 'http=422 category=validation') as raised:
            state.api('/git/trees', 'POST', body)
        line = next(line for line in self.logs.getvalue().splitlines() if line.startswith('GitHub state diagnostic '))
        diagnostic = json.loads(line.removeprefix('GitHub state diagnostic '))
        self.assertEqual(diagnostic['request_id'], 'ABCD:12:3456:7890:ABCDEF12')
        self.assertEqual(diagnostic['request_bytes'], len(json.dumps(body).encode()))
        self.assertEqual(diagnostic['tree_entries'], 2)
        self.assertEqual(diagnostic['content_entries'], 1)
        self.assertEqual(diagnostic['sha_entries'], 1)
        self.assertEqual(diagnostic['content_bytes'], len(SECRET))
        self.assertEqual(diagnostic['message_category'], 'validation_failed')
        self.assertEqual(diagnostic['errors'][0], {
            'resource': 'Tree', 'field': 'tree', 'code': 'custom', 'message_category': 'processing_timeout'})
        self.assertEqual(diagnostic['errors'][1], {
            'resource': 'other', 'field': 'other', 'code': 'other', 'message_category': 'other'})
        self.assertNotIn(SECRET, self.logs.getvalue() + str(raised.exception))
        self.save_diagnostic.assert_called_once_with(diagnostic, json.dumps(provider))
        self.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_untrusted_diagnostic_fields_are_bounded_and_cannot_escape_to_logs(self):
        provider = {'message': SECRET, 'errors': [
            {'resource': [SECRET], 'field': {'private': SECRET}, 'code': SECRET, 'message': SECRET},
            SECRET, {'message': SECRET},
        ] * 100}
        self.request.side_effect = [response(422, provider, headers={'X-GitHub-Request-Id': SECRET})]
        with self.assertRaises(state.StateError):
            state.api('/git/trees', 'POST', {'tree': [{'path': SECRET, 'content': SECRET}]})
        diagnostic = self.save_diagnostic.call_args.args[0]
        self.assertNotIn('request_id', diagnostic)
        self.assertEqual(diagnostic['error_count'], state.DIAGNOSTIC_ERROR_LIMIT + 1)
        self.assertLessEqual(len(diagnostic['errors']), state.DIAGNOSTIC_ERROR_LIMIT)
        self.assertNotIn(SECRET, self.logs.getvalue())
        self.assertLess(len(self.logs.getvalue()), 2000)

    def test_invalid_or_oversized_provider_json_stays_private(self):
        for payload in (SECRET, SECRET * 10000):
            with self.subTest(size=len(payload)):
                diagnostic = state._failure_diagnostic('create_tree', 422, {}, payload, {'request_bytes': 0})
                self.assertEqual(diagnostic['message_category'], 'unavailable')
                self.assertNotIn(SECRET, json.dumps(diagnostic))

    def test_message_classifications_never_echo_provider_text(self):
        for message, category in (
            ('This request took too long to process: ' + SECRET, 'processing_timeout'),
            ('The endpoint has been spammed: ' + SECRET, 'spam_detection'),
            ('API rate limit exceeded: ' + SECRET, 'rate_limit'),
            ('Too many tree entries: ' + SECRET, 'entry_limit'),
            ('Tree is too large: ' + SECRET, 'size_limit'),
            ('Both sha and content were supplied: ' + SECRET, 'conflicting_content_and_sha'),
            ('Blob does not exist: ' + SECRET, 'missing_object'),
            ('Invalid object sha: ' + SECRET, 'invalid_object'),
            ('Duplicate path: ' + SECRET, 'path_conflict'),
        ):
            with self.subTest(category=category):
                self.assertEqual(state._message_category(message), category)

    def test_timeout_does_not_retain_an_earlier_responses_detail(self):
        self.request.side_effect = [response(502, {'message': SECRET})] + [
            subprocess.TimeoutExpired(['gh'], 60)] * (state.API_ATTEMPTS - 1)
        with self.assertRaisesRegex(state.StateError, 'category=timeout'):
            state.api('/git/trees', 'POST', {'tree': []})
        self.save_diagnostic.assert_not_called()

    def test_recovered_requests_do_not_leave_failure_artifacts(self):
        self.request.side_effect = [response(502, {'message': SECRET}), response(201, {'sha': 'a' * 40})]
        self.assertEqual(state.api('/git/trees', 'POST', {'tree': []}), {'sha': 'a' * 40})
        self.save_diagnostic.assert_not_called()


class EncryptedDiagnosticTests(PatchedTestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / 'diagnostics/github-state-error.enc.json'
        self.enterContext(patch.object(state, 'DIAGNOSTIC_PATH', self.path))
        self.enterContext(patch.dict(os.environ, {
            'DG_HUB_STATE_KEY': 'A' * 43, 'GH_TOKEN': SECRET,
            'STUDIO_API_KEY': 'studio-private-example-key',
        }))
        self.logs = io.StringIO()
        self.enterContext(patch.object(state.sys, 'stderr', self.logs))

    def test_artifact_round_trip_contains_redacted_response_and_no_credentials(self):
        payload = json.dumps({'message': 'Unknown provider reason ' + SECRET,
                              'errors': [{'message': os.environ['STUDIO_API_KEY']}],
                              'other': os.environ['DG_HUB_STATE_KEY']})
        state._save_failure_diagnostic({'operation': 'create_tree', 'http': 422}, payload)
        encrypted = self.path.read_bytes()
        self.assertNotIn(SECRET.encode(), encrypted)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        decoded = state.crypt('decrypt', encrypted, state.DIAGNOSTIC_PURPOSE)
        document = json.loads(decoded)
        self.assertIn('Unknown provider reason [redacted]', document['response'])
        for credential in (SECRET, os.environ['STUDIO_API_KEY'], os.environ['DG_HUB_STATE_KEY']):
            self.assertNotIn(credential, decoded.decode() + self.logs.getvalue())
        self.assertEqual(document['diagnostic'], {'operation': 'create_tree', 'http': 422})
        self.assertFalse(document['response_truncated'])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_redaction_happens_before_response_truncation(self):
        payload = 'x' * (state.DIAGNOSTIC_RESPONSE_BYTES - 4) + SECRET + 'y' * 200
        state._save_failure_diagnostic({'http': 422}, payload)
        document = json.loads(state.crypt('decrypt', self.path.read_bytes(), state.DIAGNOSTIC_PURPOSE))
        self.assertTrue(document['response_truncated'])
        self.assertLessEqual(len(document['response'].encode()), state.DIAGNOSTIC_RESPONSE_BYTES)
        self.assertNotIn(SECRET[:4], document['response'])

    def test_encryption_failure_emits_only_fixed_warning_and_no_plaintext_file(self):
        with patch.object(state, 'crypt', side_effect=RuntimeError(SECRET)):
            state._save_failure_diagnostic({'http': 422}, SECRET)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.logs.getvalue(), 'Encrypted GitHub state diagnostic could not be retained.\n')

    def test_existing_symlink_is_not_overwritten(self):
        self.path.parent.mkdir()
        outside = Path(self.directory) / 'outside'
        outside.write_text('keep')
        self.path.symlink_to(outside)
        state._save_failure_diagnostic({'http': 422}, SECRET)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(outside.read_text(), 'keep')
        self.assertIn('could not be retained', self.logs.getvalue())


class ReferenceRetryTests(PatchedTestCase):
    def setUp(self):
        self.request = self.enterContext(patch.object(state.subprocess, 'run'))
        self.sleep = self.enterContext(patch.object(state.time, 'sleep'))
        self.logs = io.StringIO()
        self.enterContext(patch.object(state.sys, 'stderr', self.logs))
        self.old = 'a' * 40
        self.new = 'b' * 40
        self.tree = 'c' * 40
        self.other = 'd' * 40

    def configure(self, old, retry_head, *, repeat=False):
        self.request.reset_mock()
        self.sleep.reset_mock()
        self.events = []
        prefix = 'repos/' + state.REPO
        write = ('POST', '/git/refs') if old is None else ('PATCH', '/git/refs/heads/collector-state')
        self.steps = [
            ('POST', '/git/trees', response(201, {'sha': self.tree})),
            ('POST', '/git/commits', response(201, {'sha': self.new})),
            ('GET', REF_PATH, head_response(old)),
            (*write, subprocess.TimeoutExpired(['gh', SECRET], 60, stderr=SECRET)),
            ('GET', REF_PATH, head_response(retry_head)),
        ]
        if repeat:
            self.steps.append((*write, response(200 if old else 201, {'object': {'sha': self.new}})))

        def invoke(args, **kwargs):
            method, path, result = self.steps.pop(0)
            self.assertEqual(args[2], prefix + path)
            self.assertEqual(args[args.index('--method') + 1], method)
            self.events.append((method, path))
            if isinstance(result, BaseException):
                raise result
            return result

        self.request.side_effect = invoke
        self.sleep.side_effect = lambda duration: self.events.append(('sleep', duration))
        return write

    def test_lost_successful_reference_response_is_reconciled_without_another_write(self):
        for old in (self.old, None):
            with self.subTest(existing_branch=old is not None):
                write = self.configure(old, self.new)
                self.assertEqual(state._commit_tree(old, []), self.new)
                self.assertFalse(self.steps)
                self.assertEqual(self.events.count(write), 1)
                self.assertEqual(self.events[-2:], [('sleep', 2), ('GET', REF_PATH)])
                self.assertIn('reconciled after an ambiguous response', self.logs.getvalue())
                self.assertNotIn(SECRET, self.logs.getvalue())

    def test_head_changed_during_backoff_aborts_before_another_write(self):
        for old in (self.old, None):
            with self.subTest(existing_branch=old is not None):
                write = self.configure(old, self.other)
                with self.assertRaisesRegex(state.StateError, 'changed during retry'):
                    state._commit_tree(old, [])
                self.assertFalse(self.steps)
                self.assertEqual(self.events.count(write), 1)
                self.assertEqual(self.events[-2:], [('sleep', 2), ('GET', REF_PATH)])

    def test_unchanged_head_allows_the_reference_write_to_retry(self):
        for old in (self.old, None):
            with self.subTest(existing_branch=old is not None):
                write = self.configure(old, old, repeat=True)
                self.assertEqual(state._commit_tree(old, []), self.new)
                self.assertFalse(self.steps)
                self.assertEqual(self.events.count(write), 2)
                self.assertEqual(self.events[-3:], [('sleep', 2), ('GET', REF_PATH), write])


if __name__ == '__main__':
    unittest.main()
