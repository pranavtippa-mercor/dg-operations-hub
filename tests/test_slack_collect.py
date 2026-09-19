import copy
import json
import tempfile
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import slack_collect as S


class EvidenceParsingTests(unittest.TestCase):
    def test_full_thread_requires_root_and_uncapped_chronology(self):
        text = '[Writer · 100.000001] hello\n\n[Other · 101.000001] reply'
        self.assertEqual(len(S.thread_messages(text, '100.000001')), 2)
        for root, limit in [('99.0', 1000), ('100.000001', 2)]:
            with self.assertRaises(ValueError):
                S.thread_messages(text, root, limit)
        with self.assertRaises(ValueError):
            S.thread_messages(text + '\n[truncated]', '100.000001')

    def test_search_requires_explicit_pagination_end(self):
        doc = {'results': '# Search\nNo results found.', 'pagination_info': 'End of results - No more pages available.'}
        self.assertEqual(S.parse_search(json.dumps(doc) + '\n' + json.dumps({'_mercor_rid': 'opaque'})), ([], None))
        doc['pagination_info'] = 'unknown'
        with self.assertRaises(ValueError):
            S.parse_search(doc)

    def test_cursor_pages_accumulate_and_repeating_cursor_fails(self):
        class Client:
            def __init__(self, repeat=False): self.calls = []; self.repeat = repeat
            def call(self, _, args):
                self.calls.append(args)
                more = len(self.calls) == 1 or self.repeat
                return {'results': 'No results found.', 'pagination_info': 'Use cursor `next`' if more else 'End of results - No more pages'}
        client = Client()
        self.assertEqual(S.search_all(client, 'test', 'DOP*'), ([], 2))
        self.assertEqual(client.calls[1]['cursor'], 'next')
        with self.assertRaises(ValueError): S.search_all(Client(True), 'test', 'DOP*')

    def test_absolute_deadlines_and_dst(self):
        self.assertEqual(S.explicit_deadline('Friday, September 18, 2026 at 11:59 PM PT'), '2026-09-19T06:59:00Z')
        self.assertEqual(S.explicit_deadline('December 18, 2026 at 11:59 PM Pacific'), '2026-12-19T07:59:00Z')
        self.assertEqual(S.explicit_deadline('Tuesday, September 22, 2026 — EOD Pacific.'), '2026-09-23T06:59:00Z')
        self.assertIsNone(S.explicit_deadline('Monday, September 22, 2026 at noon Pacific'))
        self.assertIsNone(S.explicit_deadline('September 18, 2026 at noon PST'))
        for phrase in ['tomorrow EOD', 'September 18 at noon PT', '2026-09-18', 'September 18, 2026 at noon', 'September 18, 2026']:
            self.assertIsNone(S.explicit_deadline(phrase))
        for phrase in ['September 22, 2026 between 5 PM and 7 PM PT',
                       'September 22, 2026 at 5–7 PM PT',
                       'September 22, 2026 at 5 PM or 7 PM PT',
                       'September 22, 2026 at 5 or 7 PM PT',
                       'September 22, 2026 at noon or 5 PM PT',
                       'September 22, 2026 at 5 PM PT or 2026-09-23',
                       '2026-09-22T17:00:00-07:00 or September 23, 2026 at 5 PM PT',
                       '2026-09-22T17:00:00-07:00 or 2026-09-23T17:00:00-07:00']:
            self.assertIsNone(S.explicit_deadline(phrase))

    def test_rfd_ask_does_not_extract_reply_cutoff(self):
        text = 'Hi @Writer — needs to be fully Ready for Delivery by Tuesday, September 22, 2026, at 11:59 PM PT, including audit. Please reply here by 10 PM tonight to confirm you can meet that deadline.'
        due, phrase = S.requested_deadline(text)
        self.assertEqual(due, '2026-09-23T06:59:00Z')
        self.assertNotIn('10 PM', phrase)

    def test_attribution_does_not_count_coordinator_or_name_prefix(self):
        root = {'channel': 'TEST', 'ts': '100.000000'}
        messages = [
            {'author': 'Coordinator', 'ts': '100.000000', 'text': 'Hi @Writer One — Ready for Delivery by Tuesday, September 22, 2026 at 11:59 PM PT. Please confirm you can meet that deadline.'},
            {'author': 'Coordinator', 'ts': '101.000000', 'text': 'Yes, I will finish it.'},
            {'author': 'Writer One Other', 'ts': '102.000000', 'text': 'Yes.'},
        ]
        rec = S.confirmation(messages, root, 'Coordinator', '2026-09-18T00:00:00Z')
        self.assertEqual(rec['state'], 'awaiting')
        messages.append({'author': 'Writer One', 'ts': '103.000000', 'text': 'Yes.'})
        rec = S.confirmation(messages, root, 'Coordinator', '2026-09-18T00:00:00Z')
        self.assertEqual(rec['state'], 'confirmed')
        messages.append({'author': 'Writer One', 'ts': '104.000000', 'text': 'I am blocked and need an extension.'})
        self.assertEqual(S.confirmation(messages, root, None, '2026-09-18T00:00:00Z')['state'], 'at_risk')

    def test_known_conflicting_identity_never_falls_back_to_display_name(self):
        message = {'author': 'Alex', 'author_id': 'UBBB', 'text': 'Yes', 'ts': '101.0'}
        self.assertFalse(S.is_person(message, 'Alex', 'UAAA', {'UAAA': ['Alex']}))
        self.assertTrue(S.is_person(message, 'Alex', 'UBBB'))
        root = {'channel': 'CTEST', 'ts': '100.0'}
        messages = [{'author': 'Coordinator', 'ts': '100.0', 'text': 'Hi <@UAAA|Alex> — Please confirm you can meet that deadline.'}, message]
        self.assertEqual(S.confirmation(messages, root, 'Alex', '2026-09-18T00:00:00Z')['state'], 'awaiting')

    def test_hedges_partial_stage_and_pay_schedule_are_not_commitments(self):
        for phrase in ['Yes, if the audit is done.', 'I will finish Stage 1.', 'I can finish, but I need an extension.']:
            self.assertNotEqual(S.classify_reply(phrase), 'confirmed')
        rec = S.confirmation([{'author': 'Bot', 'ts': '100.0', 'text': 'Please confirm by September 22. No confirmation reply is needed.'}], {'channel': 'TEST', 'ts': '100.0'}, None, '2026-09-18T00:00:00Z')
        self.assertEqual(rec['state'], 'no_request')

    def test_commitment_is_proven_and_history_preserved(self):
        conf = {'state': 'confirmed', 'by_addressee': True, 'reply_at': '1789760000.0', 'reply_by': 'Writer', 'reply_url': 'https://example.test/reply', 'reply_text': 'Yes.', 'deadline_at': '2026-09-23T06:59:00Z', 'observed_at': '2026-09-19T00:00:00Z'}
        original = {'assigned': {'due': '2026-09-19T06:59:00Z', 'source': 'studio'}, 'committed': {'due': '2026-09-20T06:59:00Z', 'url': 'https://example.test/older'}}
        rec = S.update_commitment(original, conf)
        self.assertEqual(rec['assigned'], original['assigned'])
        self.assertEqual(rec['committed']['due'], conf['deadline_at'])
        self.assertEqual(len(rec['moves']), 1)
        self.assertEqual(len(rec['commitment_history']), 1)
        self.assertEqual(S.update_commitment(rec, conf), rec)
        conf['reply_text'] = 'I will finish tomorrow.'
        self.assertEqual(S.update_commitment(original, conf)['committed'], original['committed'])

    def test_newer_activity_is_not_overwritten(self):
        activity = {'A': {'at': '2026-09-19T00:00:00Z', 'text': 'new'}}
        S.update_activity(activity, 'A', {'ts': '100.0', 'author': 'Writer', 'text': 'old'}, '2026-09-20T00:00:00Z', 'thread')
        self.assertEqual(activity['A']['text'], 'new')

    def test_spontaneous_commitment_requires_full_rfd_scope(self):
        message = {'author': 'Writer', 'ts': '100.0', 'text': 'I will finish and be Ready for Delivery by September 22, 2026 at 11:59 PM PT.'}
        root = {'channel': 'CTEST', 'ts': '99.0'}
        self.assertIsNotNone(S.explicit_owner_commitment([message], root, 'Writer', '2026-09-18T00:00:00Z'))
        self.assertIsNone(S.explicit_owner_commitment([message], root, 'Other Writer', '2026-09-18T00:00:00Z'))
        message['text'] = 'I will finish Stage 1 by September 22, 2026 at 11:59 PM PT.'
        self.assertIsNone(S.explicit_owner_commitment([message], root, 'Writer', '2026-09-18T00:00:00Z'))

    def test_confirmation_refresh_does_not_duplicate_history(self):
        old = {'state': 'confirmed', 'reply_at': '100.0', 'observed_at': '2026-09-18T00:00:00Z', 'messages': 5}
        newer = dict(old, observed_at='2026-09-19T00:00:00Z', messages=6)
        self.assertEqual(S.confirmation_fingerprint(old), S.confirmation_fingerprint(newer))

    def test_task_reference_alias_is_exact_and_unambiguous(self):
        tasks = {'DOP-TEST-001-EE-TEMPLATE': {'id': 'task_one'}}
        self.assertEqual(S.referenced_tasks({'text': '_DOP-TEST-001-EE_'}, tasks), list(tasks))
        self.assertEqual(S.referenced_tasks({'text': 'DOP-TEST-001-EE-OTHER'}, tasks), [])
        tasks['DOP-TEST-001-EE'] = {'id': 'task_two'}
        self.assertEqual(S.referenced_tasks({'text': 'DOP-TEST-001-EE'}, tasks), ['DOP-TEST-001-EE'])


class CollectionTests(unittest.TestCase):
    def setup_private(self, path):
        cache = path/'slack'
        cache.mkdir()
        state = {'rows': [
            {'task_id': 'task_one', 'task_name': 'DOP-TEST-001-EE', 'task_mode': 'Edit Eval', 'artifact': 'docx', 'status': 'writing', 'owned_by': 'user_one'},
            {'task_id': 'task_two', 'task_name': 'DOP-TEST-002-EE', 'task_mode': 'Edit Eval', 'artifact': 'docx', 'status': 'writing', 'owned_by': 'user_two'}],
                 'users': {'user_one': {'name': 'Writer One'}, 'user_two': {'name': 'Writer Two'}}, 'statuses': {'writing': 'Writing'}}
        S.atomic_json(path/'tasks.json', state)
        S.atomic_json(cache/'roots.json', {'roots': {'DOP-TEST-001-EE': {'channel': 'CTEST', 'ts': '100.000001'}, 'DOP-TEST-002-EE': {'channel': 'CTEST', 'ts': '100.000002'}}})
        S.atomic_json(cache/'state.json', {'collected_at': '2026-09-17T00:00:00Z', 'mention_through': '2026-09-17T00:00:00Z'})
        return {'task_state': str(path/'tasks.json'), 'slack': {'cache_dir': str(cache), 'workspace': 'test'}}

    def warm_threads(self, path):
        roots = S.load(path/'slack/roots.json')
        roots['_channels'] = {'CTEST': 'test-project-qc'}
        S.atomic_json(path/'slack/roots.json', roots)
        for name, root in roots['roots'].items():
            S.atomic_json(path/'slack/threads'/('CTEST-' + root['ts'].replace('.', '_') + '.json'),
                          {'observed_at': '2026-09-18T00:00:00Z', 'root': root, 'complete': True,
                           'messages': [{'author': name, 'ts': root['ts'], 'text': 'root'}]})

    def test_complete_channel_search_reuses_unchanged_thread_cache(self):
        class Client:
            def call(self, tool, args):
                if tool == 'slack_read_thread': raise AssertionError('Unchanged root should reuse verified cache')
                return {'results': 'No results found.', 'pagination_info': 'End of results - No more pages'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); config = self.setup_private(path); self.warm_threads(path)
            receipt = S.collect(config, client=Client(), now=datetime(2026, 9, 18, 0, 15, tzinfo=timezone.utc))
            self.assertTrue(receipt['ok'])
            self.assertEqual(receipt['threads_reused'], 2)
            self.assertEqual(S.load(path/'slack/confirmations.json')['harvested_at'], '2026-09-18T00:00:00Z')
            self.assertEqual(S.load(path/'slack/state.json')['collected_at'], '2026-09-18T00:15:00Z')

    def test_unmentioned_reply_invalidates_its_root_cache(self):
        class Client:
            def __init__(self, omit_parent=False): self.reads = []; self.omit_parent = omit_parent
            def call(self, tool, args):
                if tool == 'slack_read_thread':
                    self.reads.append(args['message_ts'])
                    return f"[Writer One · {args['message_ts']}] root\n\n[Writer One · 1789690020.000000] Working on it."
                if args['query'].startswith('in:'):
                    parent = '' if self.omit_parent else '?thread_ts=100.000001'
                    return {'results': f'### Result 1 of 1\nChannel: #test-project-qc (ID: CTEST)\nFrom: Writer One (ID: UTEST)\nMessage_ts: 1789690020.000000\nPermalink: [link](https://example.test/archives/CTEST/p1789690020000000{parent})\nText:\nWorking on it.\n---\n', 'pagination_info': 'End of results - No more pages'}
                return {'results': 'No results found.', 'pagination_info': 'End of results - No more pages'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); config = self.setup_private(path); self.warm_threads(path)
            client = Client()
            receipt = S.collect(config, client=client, now=datetime(2026, 9, 18, 0, 15, tzinfo=timezone.utc))
            self.assertTrue(receipt['ok'])
            self.assertEqual(client.reads, ['100.000001'])
            self.assertEqual(receipt['threads_reused'], 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); config = self.setup_private(path); self.warm_threads(path)
            client = Client(omit_parent=True)
            receipt = S.collect(config, client=client, now=datetime(2026, 9, 18, 0, 15, tzinfo=timezone.utc))
            self.assertTrue(receipt['ok'])
            self.assertEqual(client.reads, ['100.000001', '100.000002'])

    def test_missing_root_discovery_cache_expires_and_tracks_new_references_and_roster(self):
        class Client:
            def __init__(self): self.discoveries = 0; self.reference = None
            def call(self, tool, args):
                if tool == 'slack_read_thread':
                    return f"[Writer · {args['message_ts']}] unrelated root"
                if args['query'].startswith('"'): self.discoveries += 1
                if args['query'] == 'DOP*' and self.reference:
                    return {'results': self.reference, 'pagination_info': 'End of results - No more pages'}
                return {'results': 'No results found.', 'pagination_info': 'End of results - No more pages'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); config = self.setup_private(path); self.warm_threads(path)
            roots = S.load(path/'slack/roots.json')
            roots['roots'].pop('DOP-TEST-002-EE')
            S.atomic_json(path/'slack/roots.json', roots)
            client = Client()
            first = S.collect(config, client=client, now=datetime(2026, 9, 18, 0, 15, tzinfo=timezone.utc))
            self.assertTrue(first['ok'])
            self.assertEqual(first['missing_roots'], 1)
            self.assertEqual(client.discoveries, 1)
            second = S.collect(config, client=client, now=datetime(2026, 9, 18, 0, 30, tzinfo=timezone.utc))
            self.assertEqual(second['root_discoveries_reused'], 1)
            self.assertEqual(client.discoveries, 1)
            ts = str(int(datetime(2026, 9, 18, 0, 35, tzinfo=timezone.utc).timestamp())) + '.000000'
            client.reference = (f"### Result 1 of 1\nChannel: #test-project-qc (ID: CTEST)\nFrom: Writer Two (ID: UTEST)\n"
                                f"Message_ts: {ts}\nPermalink: [link](https://example.test/archives/CTEST/p{ts.replace('.', '')})\n"
                                "Text:\nDOP-TEST-002-EE needs a thread.\n---\n")
            third = S.collect(config, client=client, now=datetime(2026, 9, 18, 0, 45, tzinfo=timezone.utc))
            self.assertEqual(third['root_discoveries_reused'], 0)
            self.assertEqual(third['threads_reused'], 1)
            self.assertEqual(third['threads_reread'], 0)
            self.assertEqual(third['thread_rpc_calls'], 1)
            self.assertEqual(client.discoveries, 2)
            client.reference = None
            task_state = S.load(path/'tasks.json')
            task_state['rows'][1]['owned_by'] = 'user_one'
            S.atomic_json(path/'tasks.json', task_state)
            fourth = S.collect(config, client=client, now=datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc))
            self.assertEqual(fourth['root_discoveries_reused'], 0)
            self.assertEqual(client.discoveries, 3)
            fifth = S.collect(config, client=client, now=datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc))
            self.assertEqual(fifth['root_discoveries_reused'], 0)
            self.assertEqual(client.discoveries, 4)

    def test_daily_sweep_does_not_reuse_old_full_thread(self):
        class Client:
            def __init__(self): self.reads = 0
            def call(self, tool, args):
                if tool == 'slack_read_thread':
                    self.reads += 1
                    return f"[Writer · {args['message_ts']}] root"
                return {'results': 'No results found.', 'pagination_info': 'End of results - No more pages'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); config = self.setup_private(path); self.warm_threads(path)
            client = Client()
            receipt = S.collect(config, client=client, now=datetime(2026, 9, 19, 0, 15, tzinfo=timezone.utc))
            self.assertTrue(receipt['ok'])
            self.assertEqual(client.reads, 2)

    def test_incremental_window_includes_posts_during_long_baseline(self):
        class Client:
            def __init__(self): self.channel_after = None
            def call(self, tool, args):
                if args.get('query', '').startswith('in:'): self.channel_after = args['after']
                if tool == 'slack_read_thread': raise AssertionError('Verified cache should be usable')
                return {'results': 'No results found.', 'pagination_info': 'End of results - No more pages'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); config = self.setup_private(path); self.warm_threads(path)
            S.atomic_json(path/'slack/state.json', {'collected_at': '2026-09-18T00:12:00Z', 'mention_through': '2026-09-18T00:00:00Z'})
            client = Client()
            self.assertTrue(S.collect(config, client=client, now=datetime(2026, 9, 18, 0, 15, tzinfo=timezone.utc))['ok'])
            self.assertEqual(client.channel_after, str(int(datetime(2026, 9, 17, 23, 55, tzinfo=timezone.utc).timestamp())))

    def test_failed_thread_keeps_prior_evidence_and_global_freshness(self):
        class Client:
            def call(self, tool, args):
                if tool.startswith('slack_search'):
                    return {'results': 'No results found.', 'pagination_info': 'End of results - No more pages'}
                if args['message_ts'] == '100.000002': raise RuntimeError('unavailable')
                return '[Writer One · 100.000001] no request'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); config = self.setup_private(path)
            prior = {'state': 'confirmed', 'observed_at': '2026-09-17T00:00:00Z'}
            S.atomic_json(path/'slack/confirmations.json', {'tasks': {'DOP-TEST-002-EE': prior}, 'harvested_at': '2026-09-17T00:00:00Z'})
            receipt = S.collect(config, client=Client(), now=datetime(2026, 9, 18, tzinfo=timezone.utc))
            self.assertFalse(receipt['ok'])
            result = S.load(path/'slack/confirmations.json')
            self.assertEqual(result['tasks']['DOP-TEST-002-EE'], prior)
            self.assertEqual(result['harvested_at'], '2026-09-17T00:00:00Z')
            self.assertEqual(S.load(path/'slack/state.json')['collected_at'], '2026-09-17T00:00:00Z')

    def test_subset_never_claims_whole_scope_refresh(self):
        class Client:
            def call(self, tool, args):
                if tool.startswith('slack_search'):
                    return {'results': 'No results found.', 'pagination_info': 'End of results - No more pages'}
                return '[Writer One · 100.000001] no request'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); config = self.setup_private(path)
            receipt = S.collect(config, client=Client(), limit=1, now=datetime(2026, 9, 18, tzinfo=timezone.utc))
            self.assertTrue(receipt['partial_selection'])
            state = S.load(path/'slack/state.json')
            self.assertEqual(state['collected_at'], '2026-09-17T00:00:00Z')
            self.assertEqual(state['mention_through'], '2026-09-17T00:00:00Z')
            bundle = S.load(path/'slack/bundle.json')
            self.assertEqual(bundle['state'], state)
            self.assertEqual(bundle['confirmations'], S.load(path/'slack/confirmations.json'))


if __name__ == '__main__':
    unittest.main()
