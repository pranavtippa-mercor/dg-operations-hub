"""Hosted GitHub retries must remain bounded, private, and safe for branch writers."""
import importlib.util
import io
import json
import subprocess
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
