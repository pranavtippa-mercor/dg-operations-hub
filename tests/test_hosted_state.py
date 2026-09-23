import base64
import copy
import hashlib
import io
import importlib.util
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hosted_state', ROOT / 'scripts/hosted_state.py')
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)
TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')


def envelope(purpose=state.PURPOSE):
    return json.dumps({'format': 'dg-operations-state', 'version': 1, 'purpose': purpose,
                       'compression': 'gzip', 'iv': 'A' * 16,
                       'tag': base64.urlsafe_b64encode(bytes(16)).decode().rstrip('='),
                       'ciphertext': 'YWJj'})


class CryptoTests(unittest.TestCase):
    def invoke(self, operation, data, purpose=state.PURPOSE, key=TEST_KEY, destination='-'):
        return subprocess.run(['node', str(ROOT / 'scripts/state_crypto.mjs'), operation, '-', destination, purpose],
                              input=data, capture_output=True, env={**os.environ, 'DG_HUB_STATE_KEY': key})

    def test_roundtrip_compression_and_fresh_nonce(self):
        raw = json.dumps({'text': 'private example ' * 1000}).encode()
        encrypted = self.invoke('encrypt', raw)
        self.assertEqual(encrypted.returncode, 0)
        self.assertNotIn(b'private example', encrypted.stdout)
        self.assertLess(len(encrypted.stdout), len(raw))
        again = self.invoke('encrypt', raw)
        self.assertNotEqual(encrypted.stdout, again.stdout)
        decrypted = self.invoke('decrypt', encrypted.stdout)
        self.assertEqual(decrypted.returncode, 0)
        self.assertEqual(decrypted.stdout, raw)

    def test_wrong_key_purpose_and_tampering_fail_closed(self):
        encrypted = self.invoke('encrypt', b'private example').stdout
        wrong_key = base64.urlsafe_b64encode(bytes([9]) * 32).decode().rstrip('=')
        tampered = json.loads(encrypted)
        tampered['ciphertext'] = ('B' if tampered['ciphertext'][0] != 'B' else 'C') + tampered['ciphertext'][1:]
        cases = [self.invoke('decrypt', encrypted, key=wrong_key),
                 self.invoke('decrypt', encrypted, purpose='other-purpose'),
                 self.invoke('decrypt', json.dumps(tampered).encode()),
                 self.invoke('encrypt', b'private example', key='not-a-key')]
        for result in cases:
            with self.subTest(result=result.returncode):
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b'')
                self.assertEqual(result.stderr, b'Encrypted state operation failed.\n')

    def test_file_destination_private_and_cannot_follow_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'encrypted.json'
            result = self.invoke('encrypt', b'private example', destination=str(output))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            original = output.read_bytes()
            link = Path(directory) / 'link'
            link.symlink_to(output)
            rejected = self.invoke('encrypt', b'replace', destination=str(link))
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(output.read_bytes(), original)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(state, 'api', side_effect=AssertionError('Unmocked GitHub request'))
        guard.start()
        self.addCleanup(guard.stop)

    def fixture(self, root):
        root.mkdir(exist_ok=True)
        (root / 'config.json').write_text(json.dumps({'version': 2, 'studio': {'headers': {
            'X-Campaign-Id': 'campaign', 'X-Company-Id': 'company', 'X-Account-Id': 'account'}}}))
        (root / 'task-state.json').write_text('{"rows":[1]}')
        (root / 'slack/threads').mkdir(parents=True)
        (root / 'slack/threads/C123-123_45.json').write_text('{"messages":[]}')
        (root / 'modules/bootstrap').mkdir(parents=True)
        (root / 'modules/bootstrap/studio_audit_registry.tsv').write_text('id\n1\n')
        # These files must never be added to the state archive.
        (root / 'credentials.json').write_text('{"token":"example"}')
        (root / 'worker.pid').write_text('123')
        (root / 'worker.log').write_text('private example')

    def test_allowlist_roundtrip_permissions_and_unmanaged_files_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            self.fixture(source)
            manifest = state.build_manifest(source)
            self.assertEqual(len(manifest['files']), 4)
            target.mkdir()
            (target / 'credentials.json').write_text('preserve')
            (target / 'history.json').write_text('[]')
            self.assertEqual(state.restore_manifest(manifest, target), 4)
            self.assertEqual((target / 'credentials.json').read_text(), 'preserve')
            self.assertFalse((target / 'history.json').exists())
            for entry in manifest['files']:
                path = target / entry['path']
                self.assertEqual(path.read_bytes(), (source / entry['path']).read_bytes())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_manifest_rejects_traversal_and_duplicates_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            self.fixture(source)
            manifest = state.build_manifest(source)
            target.mkdir()
            (target / 'config.json').write_text('existing')
            for name in ('../outside.json', '/tmp/outside.json', 'slack/../history.json',
                         'slack\\threads\\x.json', 'slack//threads/x.json', '.private/config.json', 'credentials.json'):
                with self.subTest(name=name):
                    bad = copy.deepcopy(manifest)
                    bad['files'][-1]['path'] = name
                    with self.assertRaises(state.StateError):
                        state.restore_manifest(bad, target)
                    self.assertEqual((target / 'config.json').read_text(), 'existing')
            bad = copy.deepcopy(manifest)
            bad['files'].append(bad['files'][0])
            with self.assertRaises(state.StateError):
                state.validate_manifest(bad)

    def test_integrity_and_size_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            self.fixture(source)
            manifest = state.build_manifest(source)
            bad = copy.deepcopy(manifest)
            bad['files'][0]['sha256'] = '0' * 64
            with self.assertRaisesRegex(state.StateError, 'integrity'):
                state.validate_manifest(bad)
            with patch.object(state, 'MAX_FILES', 1):
                with self.assertRaises(state.StateError):
                    state.build_manifest(source)
                with self.assertRaises(state.StateError):
                    state.validate_manifest(manifest)
            with patch.object(state, 'MAX_TOTAL_BYTES', 1):
                with self.assertRaises(state.StateError):
                    state.validate_manifest(manifest)

    def test_allowed_symlink_file_and_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            private = base / 'private'
            private.mkdir()
            outside = base / 'outside.json'
            outside.write_text('{}')
            (private / 'task-state.json').symlink_to(outside)
            with self.assertRaises(state.StateError):
                state.build_manifest(private)
            (private / 'task-state.json').unlink()
            (private / 'slack').symlink_to(base, target_is_directory=True)
            with self.assertRaises(state.StateError):
                state.build_manifest(private)

    def test_restore_rejects_existing_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, target = base / 'source', base / 'target'
            self.fixture(source)
            manifest = state.build_manifest(source)
            outside = base / 'outside.json'
            outside.write_text('preserve')
            target.mkdir()
            (target / 'task-state.json').symlink_to(outside)
            with self.assertRaises(state.StateError):
                state.restore_manifest(manifest, target)
            self.assertEqual(outside.read_text(), 'preserve')
            self.assertFalse((target / 'config.json').exists())

    def test_config_scope_headers_allowed_credentials_rejected(self):
        state._validate_config(b'{"studio":{"headers":{"X-Account-Id":"scope"}}}')
        for value in ({'studio': {'headers': {'Authorization': 'example'}}},
                      {'studio': {'headers': {'X-Account-Id': 'bad\nvalue'}}},
                      {'source': {'refresh_token': 'example'}}, {'api_key': 'example'}):
            with self.subTest(value=value):
                with self.assertRaises(state.StateError):
                    state._validate_config(json.dumps(value).encode())

    def test_save_restore_through_crypto_without_remote_access(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'DG_HUB_STATE_KEY': TEST_KEY}):
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            self.fixture(source)
            with patch.object(state, '_branch', return_value=(None, {})), patch.object(state, '_commit_tree', return_value='b' * 40) as update:
                self.assertEqual(state.save(source), 4)
            content = {entry['path']: entry['content'].encode() for entry in update.call_args.args[1]}
            blobs = {name: blob_sha(raw) for name, raw in content.items()}
            with patch.object(state, '_branch', return_value=('b' * 40, blobs)), patch.object(state, '_download_archive', return_value=make_archive(content)) as download:
                self.assertEqual(state.restore(target), 4)
                download.assert_called_once_with('b' * 40)
            self.assertEqual((target / 'task-state.json').read_text(), '{"rows":[1]}')

    def test_unchanged_chunks_are_inherited_and_identical_save_is_noop(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'DG_HUB_STATE_KEY': TEST_KEY}):
            source = Path(directory) / 'source'
            self.fixture(source)
            with patch.object(state, '_branch', return_value=(None, {})), patch.object(state, '_commit_tree', return_value='b' * 40) as update:
                state.save(source)
            content = {entry['path']: entry['content'] for entry in update.call_args.args[1]}
            blobs = {name: blob_sha(raw.encode()) for name, raw in content.items()}
            original_manifest = content[state.STATE_FILE]
            with patch.object(state, '_branch', return_value=('b' * 40, blobs)), patch.object(state, '_read_blob', return_value=original_manifest), patch.object(state, '_commit_tree') as update:
                self.assertEqual(state.save(source), 4)
                update.assert_not_called()
            (source / 'task-state.json').write_text('{"rows":[2]}')
            with patch.object(state, '_branch', return_value=('b' * 40, blobs)), patch.object(state, '_read_blob', return_value=original_manifest), patch.object(state, '_commit_tree', return_value='c' * 40) as update:
                state.save(source)
            entries = {entry['path']: entry for entry in update.call_args.args[1]}
            changed = state._chunk_name('task-state.json')
            self.assertIn('content', entries[changed])
            self.assertNotEqual(entries[changed]['content'], content[changed])
            self.assertEqual(sum('content' in item for item in entries.values()), 2)
            for name in blobs.keys() - {state.STATE_FILE, changed}:
                self.assertNotIn(name, entries)


class BranchTests(unittest.TestCase):
    def setUp(self):
        self.head = 'a' * 40
        self.tree = 'b' * 40
        self.blob = 'c' * 40
        self.new_tree = 'd' * 40
        self.new_commit = 'e' * 40
        self.calls = []
        self.message = state.MARKER + ' Saved'
        self.parents = []
        self.entries = [{'path': state.AUTH_FILE, 'mode': '100644', 'type': 'blob',
                         'sha': self.blob, 'size': 512}]
        self.head_reads = 0
        self.race = False
        self.applied_blobs = None

    def api(self, path, method='GET', body=None, missing=False, before_retry=None):
        self.calls.append((path, method, body))
        if path == '/git/ref/heads/' + state.BRANCH:
            self.head_reads += 1
            head = 'f' * 40 if self.race and self.head_reads > 1 else self.head
            return {'object': {'sha': head, 'type': 'commit'}}
        if path == '/git/commits/' + self.head:
            return {'message': self.message, 'parents': self.parents, 'tree': {'sha': self.tree}}
        if path == '/git/trees/' + self.tree + '?recursive=1':
            return {'tree': self.entries, 'truncated': False}
        if path == '/git/trees' and method == 'POST':
            self.assertEqual(body.get('base_tree'), self.tree)
            self.applied_blobs = {entry['path']: entry['sha'] for entry in self.entries}
            for entry in body['tree']:
                if 'content' in entry:
                    self.applied_blobs[entry['path']] = blob_sha(entry['content'].encode())
                elif entry['sha'] is None:
                    del self.applied_blobs[entry['path']]
                else:
                    self.applied_blobs[entry['path']] = entry['sha']
            return {'sha': self.new_tree}
        if path == '/git/commits' and method == 'POST':
            return {'sha': self.new_commit}
        if path == '/git/refs/heads/' + state.BRANCH and method == 'PATCH':
            return {'object': {'sha': self.new_commit}}
        if path == '/git/blobs/' + self.blob:
            raw = envelope('source-auth').encode()
            return {'encoding': 'base64', 'size': len(raw), 'content': base64.b64encode(raw).decode()}
        raise AssertionError('Unexpected fake API route')

    def test_preserves_auth_blob_and_writes_parentless_commit(self):
        with patch.object(state, 'api', side_effect=self.api):
            self.assertEqual(state.update_files({state.STATE_FILE: envelope()}), self.new_commit)
        tree = next(body for path, method, body in self.calls if path == '/git/trees' and method == 'POST')
        by_name = {entry['path']: entry for entry in tree['tree']}
        self.assertEqual(tree['base_tree'], self.tree)
        self.assertEqual(set(by_name), {state.STATE_FILE})
        self.assertEqual(self.applied_blobs[state.AUTH_FILE], self.blob)
        self.assertEqual(self.applied_blobs[state.STATE_FILE], blob_sha(envelope().encode()))
        commit = next(body for path, method, body in self.calls if path == '/git/commits')
        self.assertEqual(commit['parents'], [])
        self.assertEqual(self.calls[-2][0], '/git/ref/heads/' + state.BRANCH)
        self.assertEqual(self.calls[-1][2], {'sha': self.new_commit, 'force': True})

    def test_delta_removes_retired_chunks_and_preserves_kept_chunks_and_auth(self):
        keep_chunk = 'chunks/' + '1' * 64 + '.enc.json'
        retire_chunk = 'chunks/' + '2' * 64 + '.enc.json'
        new_chunk = 'chunks/' + '3' * 64 + '.enc.json'
        self.entries += [{**self.entries[0], 'path': name} for name in
                         (state.STATE_FILE, keep_chunk, retire_chunk)]
        existing = {entry['path']: entry['sha'] for entry in self.entries}
        updates = {state.STATE_FILE: envelope(), new_chunk: envelope('new-chunk')}
        with patch.object(state, 'api', side_effect=self.api):
            state._publish(self.head, existing, updates,
                           keep={state.STATE_FILE, state.AUTH_FILE, keep_chunk, new_chunk})
        tree = next(body for path, method, body in self.calls if path == '/git/trees' and method == 'POST')
        by_name = {entry['path']: entry for entry in tree['tree']}
        self.assertEqual(set(by_name), {state.STATE_FILE, new_chunk, retire_chunk})
        self.assertIsNone(by_name[retire_chunk]['sha'])
        self.assertNotIn('content', by_name[retire_chunk])
        self.assertEqual(set(self.applied_blobs), {state.STATE_FILE, state.AUTH_FILE, keep_chunk, new_chunk})
        self.assertEqual(self.applied_blobs[keep_chunk], self.blob)
        self.assertEqual(self.applied_blobs[state.AUTH_FILE], self.blob)
        self.assertEqual(self.applied_blobs[new_chunk], blob_sha(updates[new_chunk].encode()))

    def test_auth_update_preserves_manifest_and_all_chunks_without_resending_them(self):
        chunks = ['chunks/' + str(i) * 64 + '.enc.json' for i in (1, 2)]
        self.entries += [{**self.entries[0], 'path': name} for name in [state.STATE_FILE] + chunks]
        before = {entry['path']: entry['sha'] for entry in self.entries}
        update = envelope('new-source-auth')
        with patch.object(state, 'api', side_effect=self.api):
            state.update_files({state.AUTH_FILE: update})
        tree = next(body for path, method, body in self.calls if path == '/git/trees' and method == 'POST')
        self.assertEqual([entry['path'] for entry in tree['tree']], [state.AUTH_FILE])
        self.assertEqual(self.applied_blobs, {**before, state.AUTH_FILE: blob_sha(update.encode())})

    def test_initial_state_sends_complete_tree_without_base_tree(self):
        calls = []
        def initial_api(path, method='GET', body=None, **kwargs):
            calls.append((path, method, body))
            if path == '/git/trees':
                self.assertNotIn('base_tree', body)
                self.assertEqual({entry['path'] for entry in body['tree']}, {state.STATE_FILE, state.AUTH_FILE})
                return {'sha': self.new_tree}
            if path == '/git/commits':
                self.assertEqual(body['parents'], [])
                return {'sha': self.new_commit}
            if path == '/git/refs':
                return {'object': {'sha': self.new_commit}}
            self.fail('Unexpected initial-state API request')
        with patch.object(state, 'api', side_effect=initial_api), patch.object(state, '_head', return_value=None):
            state._publish(None, {}, {state.STATE_FILE: envelope(), state.AUTH_FILE: envelope('source-auth')})
        self.assertEqual(len(calls), 3)

    def test_incremental_base_requires_valid_managed_immutable_commit(self):
        for change in ('sha', 'marker', 'parents', 'tree'):
            with self.subTest(change=change):
                self.setUp()
                if change == 'marker': self.message = 'Unmanaged user data'
                if change == 'parents': self.parents = [{'sha': 'f' * 40}]
                if change == 'tree': self.tree = 'bad'
                with patch.object(state, 'api', side_effect=self.api):
                    with self.assertRaises(state.StateError):
                        state._commit_tree('bad' if change == 'sha' else self.head, [])
                self.assertFalse(any(method != 'GET' for _, method, _ in self.calls))

    def test_fetches_ciphertext_and_allows_missing_sibling(self):
        with patch.object(state, 'api', side_effect=self.api):
            self.assertEqual(state.fetch_file(state.AUTH_FILE), envelope('source-auth'))
            self.assertIsNone(state.fetch_file(state.STATE_FILE))

    def test_unmanaged_branch_or_extra_file_is_never_overwritten(self):
        for change in ('marker', 'parents', 'extra', 'symlink'):
            with self.subTest(change=change):
                self.setUp()
                if change == 'marker':
                    self.message = 'User-authored content'
                if change == 'parents':
                    self.parents = [{'sha': 'f' * 40}]
                if change == 'extra':
                    self.entries.append({**self.entries[0], 'path': 'README.md'})
                if change == 'symlink':
                    self.entries[0]['mode'] = '120000'
                with patch.object(state, 'api', side_effect=self.api):
                    with self.assertRaises(state.StateError):
                        state.update_files({state.STATE_FILE: envelope()})
                self.assertTrue(all(method == 'GET' for _, method, _ in self.calls))

    def test_concurrent_head_change_does_not_update_branch(self):
        self.race = True
        with patch.object(state, 'api', side_effect=self.api):
            with self.assertRaisesRegex(state.StateError, 'concurrently'):
                state.update_files({state.STATE_FILE: envelope()})
        self.assertFalse(any(method == 'PATCH' for _, method, _ in self.calls))

    def test_plaintext_or_unknown_filename_rejected_before_any_api(self):
        with patch.object(state, 'api') as api:
            for update in ({state.STATE_FILE: '{"rows":[1]}'}, {'credentials.json': envelope()}):
                with self.assertRaises(state.StateError):
                    state.update_files(update)
            api.assert_not_called()


def blob_sha(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def make_archive(content, extra=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, raw in content.items():
            entry = tarfile.TarInfo('repo-commit/' + name)
            entry.size = len(raw)
            archive.addfile(entry, io.BytesIO(raw))
        if extra:
            archive.addfile(extra)
    return output.getvalue()


class ArchiveTests(unittest.TestCase):
    def test_traversal_symlink_hardlink_and_extra_files_rejected(self):
        content = {state.STATE_FILE: envelope().encode()}
        blobs = {name: blob_sha(raw) for name, raw in content.items()}
        self.assertEqual(state.read_archive(make_archive(content), blobs), content)
        for name, kind in (('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('repo-commit/link', tarfile.SYMTYPE), ('repo-commit/hard', tarfile.LNKTYPE),
                           ('repo-commit/unexpected', tarfile.REGTYPE)):
            with self.subTest(name=name):
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.linkname = 'collector-state.enc.json'
                with self.assertRaises(state.StateError):
                    state.read_archive(make_archive(content, entry), blobs)

    def test_archive_inventory_and_git_blob_integrity(self):
        content = {state.STATE_FILE: envelope().encode()}
        good = {name: blob_sha(raw) for name, raw in content.items()}
        with self.assertRaisesRegex(state.StateError, 'integrity'):
            state.read_archive(make_archive(content), {state.STATE_FILE: '0' * 40})
        with self.assertRaisesRegex(state.StateError, 'inventory'):
            state.read_archive(make_archive(content), {**good, state.AUTH_FILE: '0' * 40})

    def test_chunk_names_hide_paths_and_are_stable_and_key_scoped(self):
        with patch.dict(os.environ, {'DG_HUB_STATE_KEY': TEST_KEY}):
            first = state._chunk_name('slack/roots.json')
            self.assertEqual(first, state._chunk_name('slack/roots.json'))
            self.assertNotIn('slack', first)
            self.assertTrue(state.CHUNK_RE.fullmatch(first))
        with patch.dict(os.environ, {'DG_HUB_STATE_KEY': base64.urlsafe_b64encode(bytes([9]) * 32).decode().rstrip('=')}):
            self.assertNotEqual(first, state._chunk_name('slack/roots.json'))


if __name__ == '__main__':
    unittest.main()
