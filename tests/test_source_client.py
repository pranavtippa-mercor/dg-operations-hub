import importlib.util
import json
import sys
from pathlib import Path
import unittest

scripts = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('source_client', scripts/'source_client.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class SourceClientTests(unittest.TestCase):
    def test_studio_mutations_and_multi_statement_queries_refused(self):
        for arguments in [
            {'method': 'PATCH', 'path': '/tasks/example'},
            {'method': 'POST', 'path': '/querier/unstructured', 'params': {'query': 'SELECT 1; DELETE FROM tasks'}},
            {'method': 'POST', 'path': '/querier/unstructured', 'params': {'query': 'UPDATE tasks SET name=1'}},
        ]:
            with self.assertRaises(ValueError):
                client.validate_call('studio', arguments)

    def test_read_query_and_get_are_allowed(self):
        client.validate_call('studio', {'method': 'GET', 'path': '/worlds/example'})
        client.validate_call('studio', {'method': 'POST', 'path': '/querier/unstructured', 'params': {'query': 'SELECT task_id FROM tasks LIMIT 100'}})

    def test_slack_message_write_refused(self):
        with self.assertRaises(ValueError):
            client.validate_call('slack_send_message', {})

    def test_errors_not_treated_as_empty_data(self):
        with self.assertRaises(RuntimeError):
            client.decode_response({'toolResult': {'isError': True, 'content': []}})

    def test_tsv_and_transcript_payload_preserved(self):
        text = '<TSV_DATA>count\n5</TSV_DATA>'
        self.assertIn(text, client.decode_response({'content': [{'type': 'text', 'text': text}, {'type': 'text', 'text': '{"_mercor_rid":"example"}'}]}))
        self.assertEqual(client.decode_response({'structuredContent': {'result': '{"rows":[1]}'}}), {'rows': [1]})


    def test_rest_serialized_objects_and_lists_are_native_collector_values(self):
        rows = {'rows': [{'task_id': 'example'}]}
        users = [{'user_id': 'example'}]
        self.assertEqual(client.decode_rest_response(json.dumps(rows)).get('rows'), rows['rows'])
        self.assertEqual(client.decode_rest_response(json.dumps(users)), users)
        self.assertEqual(client.decode_rest_response('[]'), [])

    def test_rest_text_and_json_scalars_are_preserved_exactly(self):
        values = ['ordinary text', '[Writer · 100.0] Hello',
                  '<METADATA>coverage</METADATA><TSV_DATA>count\n5</TSV_DATA>',
                  '123', ' true ', 'false', 'null', '"quoted string"',
                  json.dumps(json.dumps({'rows': []}))]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(client.decode_rest_response(value), value)

    def test_rest_generic_result_dictionaries_keep_their_structure(self):
        for value in [{'result': {'rows': [1]}}, {'result': '[1,2]'},
                      {'toolResult': {'rows': []}},
                      {'result': {'content': [{'type': 'text', 'text': 'native'}]}}]:
            with self.subTest(value=value):
                self.assertEqual(client.decode_rest_response(value), value)
                self.assertEqual(client.decode_rest_response(json.dumps(value)), value)

    def test_rest_serialized_reported_errors_are_rejected_without_details(self):
        from remote_source import SourceReportedError
        for value in [{'error': 'private source detail'}, {'isError': True},
                      {'ok': False, 'message': 'private source detail'},
                      {'success': False, 'message': 'private source detail'}]:
            with self.subTest(value=value):
                with self.assertRaises(SourceReportedError) as raised:
                    client.decode_rest_response(json.dumps(value))
                self.assertNotIn('private source detail', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
