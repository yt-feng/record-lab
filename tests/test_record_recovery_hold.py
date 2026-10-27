"""Metadata and Git-object fakes only; never call GitHub or fund sources."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import record_recovery_hold as hold


def audit():
    return {'schemaVersion': 1, 'quarter': '2026Q2', 'phase': hold.PHASE,
            'updatedAt': '2026-10-27T00:15:31.123456+00:00', 'processedFunds': 22094, 'continueRun': True,
            'counts': {'sevenFieldsComplete': 482, 'missing': [None, 5]}, 'externalEvidence': {'retrievedAt': '2026-10-26T21:00:00Z'}}


class FakeAPI:
    def __init__(self, root, audit_value=None):
        self.root = Path(root); self.root.mkdir(); self.calls = []; self.patch_mode = 'success'; self.readback_fail = False
        self.before_patch = None; self.env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': hold.REPOSITORY,
            'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'c' * 40,
            'GITHUB_WORKFLOW_REF': f'{hold.REPOSITORY}/{hold.WORKFLOW}@refs/heads/main'}
        file = self.root / hold.AUDIT_PATH; file.parent.mkdir(parents=True)
        file.write_bytes(hold.encoded(audit() if audit_value is None else audit_value))
        (self.root / hold.PROGRESS_PATH).write_text('{"originalCounters":[1,2,3]}\n')
        (self.root / 'unchanged.txt').write_text('unchanged original blob\n')
        self.git('init', '-q'); self.git('add', '.')
        self.git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.test', 'commit', '-qm', 'fixture')
        self.parent = self.git('rev-parse', 'HEAD').decode().strip(); self.head = self.parent
        repo = {'id': 7, 'full_name': hold.REPOSITORY}
        common = {'repository': repo, 'head_repository': repo, 'head_branch': 'main', 'run_attempt': 1}
        self.original = {**common, 'id': hold.ORIGINAL_RUN, 'head_sha': hold.ORIGINAL_SOURCE,
            'path': '.github/workflows/traverse.yml', 'status': 'completed', 'conclusion': 'failure'}
        self.current = {**common, 'id': 999, 'head_sha': 'c' * 40, 'path': hold.WORKFLOW,
            'status': 'in_progress', 'event': 'workflow_dispatch'}
        jobs = []
        for index in range(32):
            failed = str(index) in hold.MISSING
            jobs.append({'name': f'batch ({index}, batch-{index}.json, 2026Q{2 if index < 16 else 3})',
                'status': 'completed', 'conclusion': 'failure' if failed else 'success',
                'steps': [{'name': 'Validate batch publication', 'conclusion': 'failure'}] if failed else []})
        jobs.append({'name': 'publish', 'id': 1234, 'status': 'completed', 'conclusion': 'failure',
                     'steps': [{'name': name, 'conclusion': 'success'} for name in sorted(hold.REQUIRED_STEPS)]
                     + [{'name': 'Continue unfinished traversal', 'conclusion': 'failure'}]})
        self.jobs = {'total_count': len(jobs), 'jobs': jobs}
        artifacts = []
        for index in range(32):
            if str(index) in hold.MISSING: continue
            artifacts.append({'id': index + 1, 'name': f'part-{index}', 'expired': False, 'size_in_bytes': 1000,
                'digest': 'sha256:' + 'a' * 64, 'workflow_run': {'id': hold.ORIGINAL_RUN, 'head_sha': hold.ORIGINAL_SOURCE,
                'head_branch': 'main', 'repository_id': 7, 'head_repository_id': 7}})
        self.artifacts = {'total_count': 30, 'artifacts': artifacts}

    def git(self, *args, body=None, env=None):
        return subprocess.run(['git', '-C', str(self.root), *args], input=body,
                              capture_output=True, env=env, check=True).stdout

    def request(self, method, suffix, body=None):
        self.calls.append((method, suffix, copy.deepcopy(body)))
        if method == 'GET':
            if suffix == f'actions/runs/{hold.ORIGINAL_RUN}': return copy.deepcopy(self.original)
            if suffix == 'actions/runs/999': return copy.deepcopy(self.current)
            if suffix.endswith('/jobs?per_page=100'): return copy.deepcopy(self.jobs)
            if suffix.endswith('/artifacts?per_page=100'): return copy.deepcopy(self.artifacts)
            if suffix == 'git/ref/heads/main':
                if self.readback_fail: raise hold.HoldError('HOLD_API_UNAVAILABLE')
                return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.head}}
            sha = suffix.rsplit('/', 1)[1]
            if suffix.startswith('git/commits/'):
                text = self.git('cat-file', '-p', sha).decode(); headers = text.split('\n\n', 1)[0].splitlines()
                return {'sha': sha, 'tree': {'sha': next(line[5:] for line in headers if line.startswith('tree '))},
                        'parents': [{'sha': line[7:]} for line in headers if line.startswith('parent ')]}
            if suffix.startswith('git/trees/'):
                rows = []
                for line in self.git('ls-tree', '-z', sha).split(b'\0'):
                    if not line: continue
                    metadata, path = line.split(b'\t'); mode, kind, item_sha = metadata.decode().split()
                    rows.append({'path': path.decode(), 'mode': mode, 'type': kind, 'sha': item_sha})
                return {'sha': sha, 'truncated': False, 'tree': rows}
            if suffix.startswith('git/blobs/'):
                raw = self.git('cat-file', 'blob', sha)
                return {'sha': sha, 'size': len(raw), 'encoding': 'base64', 'content': base64.b64encode(raw).decode()}
        if method == 'POST':
            if suffix == 'git/blobs':
                return {'sha': self.git('hash-object', '-w', '--stdin', body=base64.b64decode(body['content'])).decode().strip()}
            if suffix == 'git/trees':
                env = {**os.environ, 'GIT_INDEX_FILE': str(self.root / '.git' / 'synthetic-index')}
                self.git('read-tree', body['base_tree'], env=env)
                for row in body['tree']:
                    self.git('update-index', '--add', '--cacheinfo', f"{row['mode']},{row['sha']},{row['path']}", env=env)
                return {'sha': self.git('write-tree', env=env).decode().strip()}
            if suffix == 'git/commits':
                identity = body['author']; env = {**os.environ, 'GIT_AUTHOR_NAME': identity['name'],
                    'GIT_COMMITTER_NAME': identity['name'], 'GIT_AUTHOR_EMAIL': identity['email'], 'GIT_COMMITTER_EMAIL': identity['email'],
                    'GIT_AUTHOR_DATE': identity['date'], 'GIT_COMMITTER_DATE': identity['date']}
                return {'sha': self.git('commit-tree', body['tree'], '-p', body['parents'][0], body=body['message'].encode(), env=env).decode().strip()}
        if method == 'PATCH' and suffix == 'git/refs/heads/main':
            if self.before_patch: self.before_patch()
            if body.get('force') is not False: raise AssertionError('force')
            if self.patch_mode in {'success', 'timeout-after-write', 'unreadable-after-write'}: self.head = body['sha']
            if self.patch_mode == 'unreadable-after-write': self.readback_fail = True
            if self.patch_mode != 'success': raise hold.HoldError('HOLD_API_UNAVAILABLE')
            return {'object': {'type': 'commit', 'sha': self.head}}
        raise AssertionError((method, suffix))


class HoldTests(unittest.TestCase):
    def setup_api(self, directory, audit_value=None):
        api = FakeAPI(Path(directory) / 'data', audit_value)
        return api, Path(directory) / 'output'

    def test_only_three_json_fields_change_original_counts_and_clocks_survive(self):
        old = audit(); raw = hold.encoded(old); new_raw, changed = hold.transformed(raw); new = json.loads(new_raw)
        self.assertTrue(changed)
        self.assertEqual(set(new) - set(old), {'batchInputErrors', 'continuationHold'})
        for key in old:
            if key != 'continueRun': self.assertEqual(new[key], old[key])
        self.assertIs(new['continueRun'], False)
        self.assertEqual(hold.transformed(new_raw), (new_raw, False))
        for altered in [{**old, 'schemaVersion': True}, {**old, 'quarter': '2026Q3'}, {**old, 'phase': {}}, {**old, 'continuationHold': {'sourceRunId': 'another'}}]:
            with self.assertRaises(hold.HoldError): hold.transformed(hold.encoded(altered))

    def test_metadata_requires_persist_all_data_steps_and_all_thirty_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            api, _ = self.setup_api(directory)
            evidence = hold.metadata_evidence(api, api.env)
            self.assertTrue(evidence['persistenceStepVerified']); self.assertFalse(evidence['fundDataCompletenessVerified'])
            self.assertEqual(len(evidence['successfulArtifactMetadata']), 30)
            publisher = api.jobs['jobs'][-1]
            for name in hold.REQUIRED_STEPS:
                original = copy.deepcopy(publisher)
                next(row for row in publisher['steps'] if row['name'] == name)['conclusion'] = 'failure'
                with self.assertRaises(hold.HoldError): hold.metadata_evidence(api, api.env)
                publisher.clear(); publisher.update(original)
            api.artifacts['artifacts'][0]['name'] = 'part-10'
            with self.assertRaises(hold.HoldError): hold.metadata_evidence(api, api.env)

    def test_nonterminal_wrong_source_rerun_and_fork_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            api, output = self.setup_api(directory)
            original = copy.deepcopy(api.original)
            for change in [{'status': 'in_progress'}, {'head_sha': 'f' * 40}, {'run_attempt': 2}, {'run_attempt': True},
                           {'head_repository': {'id': 9, 'full_name': 'other/repo'}}]:
                api.original = {**original, **change}
                with self.assertRaises(hold.HoldError): hold.prepare(api.root, api.parent, output, api=api, env=api.env)
                self.assertFalse(output.exists()); self.assertTrue(all(method == 'GET' for method, _, _ in api.calls))

    def test_real_git_objects_only_audit_blob_changes_and_receipt_precedes_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            api, output = self.setup_api(directory); before = (api.root / hold.AUDIT_PATH).read_bytes()
            result = hold.prepare(api.root, api.parent, output, api=api, env=api.env)
            self.assertEqual(result['status'], 'prepared'); self.assertFalse(any(method == 'PATCH' for method, _, _ in api.calls))
            prepared = hold.read(output, 'prepared.json')
            changed = api.git('diff-tree', '--no-commit-id', '--name-only', '-r', api.parent, prepared['preparedCommit']).decode().splitlines()
            self.assertEqual(changed, [hold.AUDIT_PATH])
            self.assertEqual((api.root / hold.AUDIT_PATH).read_bytes(), before)
            def at_patch():
                receipt = hold.read(output, 'receipt.json')
                self.assertEqual(receipt['status'], 'unknown'); self.assertEqual(receipt['refRequestCount'], 1)
                self.assertEqual(receipt['preparedCommit'], prepared['preparedCommit'])
            api.before_patch = at_patch
            result = hold.apply(output, api=api, env=api.env)
            self.assertEqual(result['status'], 'verified'); self.assertFalse(result['fundDataCompletenessVerified'])
            self.assertEqual(sum(method == 'PATCH' for method, _, _ in api.calls), 1)
            self.assertTrue(hold.validate_output(output))
            with self.assertRaisesRegex(hold.HoldError, 'REPLAY'): hold.apply(output, api=api, env=api.env)
            self.assertEqual(sum(method == 'PATCH' for method, _, _ in api.calls), 1)

    def test_patch_timeout_is_resolved_by_get_only_or_remains_unknown(self):
        for mode, expected in [('timeout-after-write', 'verified'), ('timeout-before-write', 'unknown'), ('unreadable-after-write', 'unknown')]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                api, output = self.setup_api(directory); hold.prepare(api.root, api.parent, output, api=api, env=api.env)
                api.patch_mode = mode
                result = hold.apply(output, api=api, env=api.env)
                self.assertEqual(result['status'], expected)
                self.assertEqual(sum(method == 'PATCH' for method, _, _ in api.calls), 1)
                patch_index = next(i for i, row in enumerate(api.calls) if row[0] == 'PATCH')
                self.assertTrue(all(method == 'GET' for method, _, _ in api.calls[patch_index + 1:]))
                with self.assertRaises(hold.HoldError): hold.apply(output, api=api, env=api.env)

    def test_exact_existing_hold_is_read_only_and_different_hold_refuses(self):
        value = json.loads(hold.transformed(hold.encoded(audit()))[0])
        with tempfile.TemporaryDirectory() as directory:
            api, output = self.setup_api(directory, value)
            self.assertEqual(hold.prepare(api.root, api.parent, output, api=api, env=api.env)['status'], 'already-verified')
            self.assertEqual(hold.apply(output, api=api, env=api.env)['status'], 'already-verified')
            self.assertTrue(all(method == 'GET' for method, _, _ in api.calls))
        value['continuationHold']['sourceRunId'] = '123'
        with tempfile.TemporaryDirectory() as directory:
            api, output = self.setup_api(directory, value)
            with self.assertRaises(hold.HoldError): hold.prepare(api.root, api.parent, output, api=api, env=api.env)
            self.assertTrue(all(method == 'GET' for method, _, _ in api.calls))

    def test_changed_checkout_main_or_prepared_parent_cannot_update_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            api, output = self.setup_api(directory)
            (api.root / hold.PROGRESS_PATH).write_text('{}')
            with self.assertRaisesRegex(hold.HoldError, 'CHECKOUT'): hold.prepare(api.root, api.parent, output, api=api, env=api.env)
            self.assertFalse(any(method != 'GET' for method, _, _ in api.calls))
        with tempfile.TemporaryDirectory() as directory:
            api, output = self.setup_api(directory); hold.prepare(api.root, api.parent, output, api=api, env=api.env)
            api.head = 'a' * 40
            with self.assertRaisesRegex(hold.HoldError, 'MAIN_CHANGED'): hold.apply(output, api=api, env=api.env)
            self.assertFalse(any(method == 'PATCH' for method, _, _ in api.calls))

    def test_other_tree_blob_change_and_malformed_receipt_fail_before_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            api, output = self.setup_api(directory); hold.prepare(api.root, api.parent, output, api=api, env=api.env)
            prepared = hold.read(output, 'prepared.json'); prepared['preparedCommit'] = api.parent
            hold.write(output, 'prepared.json', prepared)
            with self.assertRaises(hold.HoldError): hold.apply(output, api=api, env=api.env)
            self.assertFalse(any(method == 'PATCH' for method, _, _ in api.calls))
            old = hold.commit(api, api.parent)['tree']['sha']
            blob = api.request('POST', 'git/blobs', {'encoding': 'base64', 'content': base64.b64encode(b'changed').decode()})['sha']
            new = api.request('POST', 'git/trees', {'base_tree': old, 'tree': [{'path': 'unchanged.txt', 'mode': '100644', 'type': 'blob', 'sha': blob}]})['sha']
            with self.assertRaisesRegex(hold.HoldError, 'OTHER_BLOBS'): hold.single_path_tree(api, old, new)

    def test_artifact_whitelist_and_workflow_durable_prepare_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); hold.write(root, 'result.json', {'status': 'unknown'})
            self.assertTrue(hold.validate_output(root))
            (root / 'raw-response.json').write_text('{}')
            with self.assertRaises(hold.HoldError): hold.validate_output(root)
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/record-recovery-hold.yml').read_text()
        self.assertEqual(text.count('include-hidden-files: true'), 2)
        self.assertEqual(text.count('if-no-files-found: error'), 2)
        self.assertNotIn('continue-on-error', text); self.assertNotIn('secrets.', text)
        self.assertNotIn('scripts/campaign.py', text); self.assertNotIn('persist-observations', text)
        self.assertLess(text.index('name: Preserve receipt before reference update'), text.index('name: Apply once and read back'))
        self.assertIn("if: always() && steps.publication.outcome == 'success'", text)


if __name__ == '__main__': unittest.main()
