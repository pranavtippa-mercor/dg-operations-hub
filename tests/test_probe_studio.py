import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import probe_studio as P
import source_client


class ProbeStudioTests(unittest.TestCase):
    def test_shapes_never_emit_source_values_or_unknown_keys(self):
        private = 'private-provider-record-marker'
        value = {'response': json.dumps({'rows': [{'task_id': private, private: 'secret'}]}), private: private}
        rendered = json.dumps(P.shape(value))
        self.assertNotIn(private, rendered)
        self.assertNotIn('secret', rendered)
        self.assertNotIn('task_id', rendered)
        response = P.shape(value)['fields']['response']
        self.assertTrue(response['json_parse_success'])
        self.assertEqual(response['json_type'], 'object')
        self.assertEqual(response['decoded_shape']['fields']['rows']['type'], 'array')
        deeply_nested = 'private-provider-record-marker'
        for _ in range(20):
            deeply_nested = {'result': deeply_nested}
        self.assertIn('depth_or_node_limit', json.dumps(P.shape(deeply_nested)))

    def test_fixed_string_classifiers_do_not_print_text(self):
        text = '```json\nprivate error: unauthorized forbidden permission access denied not found\n```\n<METADATA><TSV_DATA>\ndata: hidden'
        summary = P.shape(text)
        flags = summary['string_flags']
        for flag in ('fenced_json', 'contains_metadata_tag', 'contains_tsv_tag', 'contains_sse_data',
                     'contains_error', 'contains_unauthorized', 'contains_forbidden', 'contains_permission',
                     'contains_access_denied', 'contains_not_found'):
            self.assertTrue(flags[flag])
        self.assertFalse(summary['json_parse_success'])
        self.assertNotIn('hidden', json.dumps(summary))
        self.assertNotIn('private', json.dumps(summary))

    def test_hosted_probe_observes_raw_and_decoded_with_one_read(self):
        calls = []
        class Client:
            def __init__(self, config, **kwargs):
                self.kwargs = kwargs
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def studio(self, method, path, params):
                calls.append((method, path, params, self.kwargs))
                return source_client.decode_rest_response(json.dumps({'rows': [{'task_id': 'private-task'}]}))
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
            report = P.probe({'studio': {'world': 'world_private'}}, client_factory=Client)
        self.assertTrue(report['ok'])
        self.assertEqual(report['source_calls'], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ('POST', '/querier/unstructured'))
        self.assertTrue(calls[0][2]['query'].endswith('LIMIT 1000'))
        source_client.validate_call('studio', {'method': calls[0][0], 'path': calls[0][1], 'params': calls[0][2]})
        self.assertEqual(report['transport_value_shape']['type'], 'string')
        self.assertEqual(report['collector_value_shape']['type'], 'object')
        self.assertNotIn('world_private', json.dumps(report))
        self.assertNotIn('private-task', json.dumps(report))

    def test_nonhosted_probe_does_not_construct_client(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': ''}):
            report = P.probe({}, client_factory=lambda *a, **k: self.fail('No local source call allowed'))
        self.assertTrue(report['hosted_required'])
        self.assertEqual(report['source_calls'], 0)

    def test_errors_emit_frames_but_not_messages_or_source_lines(self):
        class Client:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def studio(self, *args): raise RuntimeError('private-key-and-provider-message')
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
            report = P.probe({'studio': {'world': 'world_example'}}, client_factory=Client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['exception']['type'], 'RuntimeError')
        self.assertTrue(report['exception']['frames'])
        self.assertTrue(all(set(frame) == {'file', 'function', 'line'} for frame in report['exception']['frames']))
        self.assertNotIn('private-key-and-provider-message', json.dumps(report))
        self.assertNotIn('raise RuntimeError', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
