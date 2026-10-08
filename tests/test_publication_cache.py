import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import sys
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('publication_cache', ROOT/'scripts/publication_cache.py')
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)
spec = importlib.util.spec_from_file_location('publication_guard', ROOT/'scripts/publication_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PublicationCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'repo'
        self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Automation')
        self.git('config', 'user.email', 'automation@users.noreply.github.com')
        for name in cache.SOURCES:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT/name).read_bytes())
        (self.root/'README.md').write_text('Public data')
        self.commit()
        self.head = self.git('rev-parse', 'HEAD')
        self.digest = cache.fingerprint(self.root)
        self.oids = sorted({entry.split()[2] for entry in self.git('ls-tree', '-r', 'HEAD').splitlines()})
        self.proof = dict(schema=1, repository=cache.REPOSITORY, run_id=123, run_attempt=1,
                          workflow_path=cache.WORKFLOWS[0], run_head_sha=self.head,
                          checkout_sha=self.head, fingerprint=self.digest, blob_oids=self.oids)
        self.run = dict(id=123, run_attempt=1, path=cache.WORKFLOWS[0], status='completed',
                        conclusion='success', event='push', head_branch='main', head_sha=self.head,
                        repository=dict(id=10, full_name=cache.REPOSITORY),
                        head_repository=dict(id=10, full_name=cache.REPOSITORY))
        self.archive = self.pack(self.proof)
        self.artifact = dict(id=456, name=cache.artifact_name(self.digest, 123, 1), expired=False,
                             size_in_bytes=len(self.archive), digest='sha256:'+hashlib.sha256(self.archive).hexdigest(),
                             workflow_run=dict(id=123, repository_id=10, head_repository_id=10,
                                               head_branch='main', head_sha=self.head))
        self.env = dict(GITHUB_ACTIONS='true', GITHUB_REPOSITORY=cache.REPOSITORY, GH_TOKEN='fixture',
                        GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='push', GITHUB_SHA=self.head,
                        GITHUB_WORKFLOW_REF=cache.REPOSITORY+'/'+cache.WORKFLOWS[0]+'@refs/heads/main',
                        GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1',
                        PUBLICATION_CACHE_OUTPUT=str(self.root.parent/'publication-history.json'),
                        GITHUB_OUTPUT=str(self.root.parent/'step-output'))
        self.api = mock.Mock()
        self.api.get.side_effect = lambda suffix, binary=False: {
            'actions/artifacts?per_page=100': {'artifacts': [self.artifact]},
            'actions/runs/123': self.run,
            'actions/artifacts/456/zip': self.archive,
        }[suffix]

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-m', 'Update public fixture')

    def pack(self, proof):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr('publication-history.json', json.dumps(proof))
        return output.getvalue()

    def replace_proof(self, proof):
        self.archive = self.pack(proof)
        self.artifact['size_in_bytes'] = len(self.archive)
        self.artifact['digest'] = 'sha256:'+hashlib.sha256(self.archive).hexdigest()

    def load(self):
        return cache.load_verified(self.root, self.env, self.api)

    def test_valid_api_proof_reuses_only_old_contents(self):
        (self.root/'README.md').write_text('New public data')
        self.commit()
        verified = self.load()
        self.assertEqual(verified, frozenset(oid.encode() for oid in self.oids))
        stats, inventory = {}, set()
        self.assertEqual(guard.scan_history(self.root, stats, cached_blobs=verified, verified_blobs=inventory), [])
        self.assertEqual(stats['cached_blobs'], len(self.oids))
        self.assertEqual(stats['fresh_blobs'], 1)
        self.assertEqual(stats['bytes'], len(b'New public data'))
        self.assertEqual(len(inventory), len(self.oids)+1)

    def test_wrong_repo_pr_failed_incomplete_and_other_workflow_are_misses(self):
        mutations = [
            {'repository': dict(id=11, full_name='other/records')},
            {'head_repository': dict(id=11, full_name='other/records')},
            {'event': 'pull_request'}, {'head_branch': 'feature'},
            {'conclusion': 'failure'}, {'status': 'in_progress'},
            {'path': '.github/workflows/untrusted.yml'}, {'run_attempt': 2},
        ]
        original = copy.deepcopy(self.run)
        for change in mutations:
            with self.subTest(change=change):
                self.run = dict(original, **change)
                self.assertEqual(self.load(), frozenset())
        self.run = original
        self.env['GITHUB_REPOSITORY'] = 'other/records'
        self.api.reset_mock()
        self.assertEqual(self.load(), frozenset())
        self.api.get.assert_not_called()

    def test_wrong_artifact_binding_missing_digest_and_modified_oid_are_misses(self):
        original = copy.deepcopy(self.artifact)
        for change in [{'digest': None}, {'digest': 'sha256:'+'0'*64}, {'expired': True},
                       {'name': 'caller-supplied-proof'}, {'workflow_run': dict(id=999)}]:
            self.artifact = dict(original, **change)
            self.assertEqual(self.load(), frozenset())
        self.artifact = original
        # Tampering with even a well-formed OID invalidates the authenticated ZIP digest.
        self.proof['blob_oids'][0] = '0'*40
        self.archive = self.pack(self.proof)
        self.assertEqual(self.load(), frozenset())

    def test_self_asserted_fingerprint_and_dirty_scanner_are_misses(self):
        proof = dict(self.proof, fingerprint='0'*64)
        self.replace_proof(proof)
        self.assertEqual(self.load(), frozenset())
        self.replace_proof(self.proof)
        (self.root/cache.SOURCES[0]).write_text('different scanner')
        self.assertEqual(self.load(), frozenset())
        self.git('add', '.'); self.git('commit', '-m', 'Change scanner')
        self.assertEqual(self.load(), frozenset())

    def test_historical_source_cannot_claim_current_fingerprint(self):
        (self.root/cache.SOURCES[0]).write_text('different scanner')
        self.commit()
        self.digest = cache.fingerprint(self.root)
        self.proof['fingerprint'] = self.digest
        self.artifact['name'] = cache.artifact_name(self.digest, 123, 1)
        self.replace_proof(self.proof)
        self.assertEqual(self.load(), frozenset())

    def test_unrelated_checkout_cannot_supply_proof(self):
        self.git('checkout', '--orphan', 'unrelated')
        self.git('commit', '-m', 'Separate public history')
        self.proof['checkout_sha'] = self.git('rev-parse', 'HEAD')
        self.replace_proof(self.proof)
        self.git('checkout', 'main')
        self.assertEqual(self.load(), frozenset())

    def test_run_head_and_later_actual_checkout_are_both_bound(self):
        (self.root/'README.md').write_text('Later public data')
        self.commit()
        self.proof['checkout_sha'] = self.git('rev-parse', 'HEAD')
        self.replace_proof(self.proof)
        self.assertTrue(self.load())
        self.proof['run_head_sha'] = '0'*40
        self.replace_proof(self.proof)
        self.assertEqual(self.load(), frozenset())

    def test_invalid_oid_inventory_zip_and_api_error_are_misses(self):
        for oids in [['not-an-oid'], [self.oids[0]]*2, list(reversed(self.oids))]:
            self.replace_proof(dict(self.proof, blob_oids=oids))
            self.assertEqual(self.load(), frozenset())
        self.archive = b'not a zip'
        self.artifact['digest'] = 'sha256:'+hashlib.sha256(self.archive).hexdigest()
        self.assertEqual(self.load(), frozenset())
        self.api.get.side_effect = subprocess.CalledProcessError(1, ['gh'])
        self.assertEqual(self.load(), frozenset())
        self.assertEqual(self.api.get.call_count > 0, True)

    def test_cached_content_does_not_skip_working_files_metadata_paths_or_modes(self):
        verified = self.load()
        (self.root/'README.md').write_text('gh'+'p_'+'x'*32)
        self.assertIn('credential', guard.scan(self.root, True, cached_blobs=verified)[1])
        (self.root/'README.md').write_text('Public data')
        (self.root/'raw').mkdir()
        (self.root/'raw/private.json').write_text('Public data')
        (self.root/'web').mkdir()
        (self.root/'web/link.txt').symlink_to('Public data')
        self.git('add', '-f', 'raw/private.json', 'web/link.txt')
        self.git('config', 'user.email', 'person'+'@'+'private-domain.test')
        self.git('commit', '-m', 'Update metadata')
        stats = {}
        findings = guard.scan_history(self.root, stats, cached_blobs=verified)
        self.assertIn('personal_email', findings)
        self.assertEqual(findings.count('unexpected_history_file'), 2)
        self.assertEqual(stats['cached_blobs'], len(self.oids))

    def test_writer_binds_actual_checkout_and_declines_pr_or_changed_workflow(self):
        (self.root/'README.md').write_text('Later public data')
        self.commit()
        self.assertTrue(cache.write_proof(self.root, {oid.encode() for oid in self.oids}, self.env))
        emitted = json.loads(Path(self.env['PUBLICATION_CACHE_OUTPUT']).read_text())
        self.assertEqual(emitted['run_head_sha'], self.head)
        self.assertEqual(emitted['checkout_sha'], self.git('rev-parse', 'HEAD'))
        self.env['GITHUB_EVENT_NAME'] = 'pull_request'
        self.assertFalse(cache.write_proof(self.root, set(), self.env))
        self.env['GITHUB_EVENT_NAME'] = 'push'
        (self.root/cache.WORKFLOWS[0]).write_text('different workflow')
        self.commit()
        self.assertFalse(cache.write_proof(self.root, set(), self.env))

    def test_cli_never_writes_proof_after_any_failed_check(self):
        # Exercise production ordering; a clean history alone is not enough.
        import sys
        with mock.patch.dict(sys.modules, {'publication_cache': cache}):
            with mock.patch.object(cache, 'load_verified', return_value=frozenset()), \
                 mock.patch.object(cache, 'write_proof') as write, \
                 mock.patch.object(guard, 'scan', return_value=(1, ['credential'])), \
                 mock.patch.object(sys, 'argv', ['guard', '--root', str(self.root), '--history']):
                self.assertEqual(guard.main(), 1)
                write.assert_not_called()


class BoundedProofReaderTests(unittest.TestCase):
    def test_real_oversized_child_is_killed_and_reaped(self):
        spawn = subprocess.Popen
        children = []
        def record(*args, **kwargs):
            process = spawn(*args, **kwargs)
            children.append(process)
            return process
        with mock.patch.object(cache.subprocess, 'Popen', side_effect=record):
            with self.assertRaises(ValueError):
                cache.read_bounded_command([sys.executable, '-c',
                                            'import sys,time;sys.stdout.write("x"*100000);sys.stdout.flush();time.sleep(30)'],
                                           128, time.monotonic()+2)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_real_hanging_child_is_killed_and_reaped(self):
        spawn = subprocess.Popen
        children = []
        def record(*args, **kwargs):
            process = spawn(*args, **kwargs)
            children.append(process)
            return process
        started = time.monotonic()
        with mock.patch.object(cache.subprocess, 'Popen', side_effect=record):
            with self.assertRaises(TimeoutError):
                cache.read_bounded_command([sys.executable, '-c', 'import time;time.sleep(30)'],
                                           128, time.monotonic()+0.1)
        self.assertLess(time.monotonic()-started, 2)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_api_calls_share_one_total_deadline(self):
        api = cache.GitHubAPI()
        with mock.patch.object(cache, 'read_bounded_command', return_value=b'{}') as read:
            api.get('actions/artifacts?per_page=100')
            api.get('actions/runs/123')
            self.assertEqual(read.call_args_list[0].args[2], read.call_args_list[1].args[2])
            api.deadline = time.monotonic()-1
            with self.assertRaises(TimeoutError):
                api.get('actions/runs/456')
            self.assertEqual(read.call_count, 2)


if __name__ == '__main__':
    unittest.main()
