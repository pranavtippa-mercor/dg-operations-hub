import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('source_client', Path(__file__).resolve().parents[1]/'scripts/source_client.py')
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


if __name__ == '__main__':
    unittest.main()
