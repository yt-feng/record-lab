"""Restoration uses synthetic archives; no source or GitHub requests."""
import copy
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import resume_traversal as resume


def identity():
    repo = {'id': 7, 'full_name': resume.REPOSITORY}
    run = {'id': 123, 'head_sha': 'a' * 40, 'repository': repo, 'head_repository': repo,
           'head_branch': 'main', 'path': '.github/workflows/traverse.yml', 'run_attempt': 1,
           'status': 'completed', 'conclusion': 'failure'}
    env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': resume.REPOSITORY,
           'GITHUB_WORKFLOW_REF': f'{resume.REPOSITORY}/{resume.WORKFLOW}@refs/heads/main',
           'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_SHA': 'b' * 40}
    return run, env


def archive(files, name='plan', number=1):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as file:
        for path, raw in files.items(): file.writestr(path, raw)
    raw = buffer.getvalue()
    meta = {'id': number, 'name': name, 'expired': False, 'size_in_bytes': len(raw),
            'digest': 'sha256:' + resume.digest(raw), 'workflow_run': {'id': 123, 'head_sha': 'a' * 40,
            'repository_id': 7, 'head_repository_id': 7, 'head_branch': 'main'}}
    return meta, raw


class API:
    def __init__(self):
        self.run, self.env = identity()
        self.calls, self.artifacts, self.archives = [], [], {}
        self.part = b'{ "retrievedAt":"2026-10-29T00:01:02.123456Z", "funds":[] }\n'
        matrix = [{'id': '0', 'config': 'batch-0.json', 'quarter': '2026Q2'}]
        self.add('plan', {'matrix.json': resume.encoded(matrix), 'meta.json': b'{}',
            'batch-0.json': resume.encoded({'quarter': '2026Q2', 'traversal': {'batchId': '0'}})})
        self.add('campaign', {'selection.json': resume.encoded({'quarters': ['2026Q2']}),
            '2026Q2.json': b'{}', 'catalogs/2026Q2.json': b'{}', 'locators/2026Q2.json': b'{}',
            'catalogs/2026Q2-identities/part-' + 'c' * 64 + '.json': b'{}'})
        self.add('directory', {'catalog.json': b'{"asOf":"2026-10-28","funds":[{"code":"000001"}]}'})
        self.add('part-0', {'part.json': self.part, 'status.json': b'{}'})
        self.add('request-accounting-123-1-0', {'receipts.json': b'{ "originalTimestamp": "2026-10-28" }'})

    def add(self, name, files):
        meta, raw = archive(files, name, len(self.artifacts) + 1)
        self.artifacts.append(meta); self.archives[meta['id']] = raw

    def get(self, path, binary=False):
        self.calls.append(path)
        if binary: return self.archives[int(path.split('/')[2])]
        if path == 'actions/runs/123': return self.run
        if path.endswith('/artifacts?per_page=100'):
            return {'total_count': len(self.artifacts), 'artifacts': self.artifacts}
        if path.endswith('/jobs?per_page=100'):
            return {'total_count': 1, 'jobs': [{'name': 'batch (0, batch-0.json, 2026Q2)', 'conclusion': 'success'}]}
        raise AssertionError(path)


class ResumeTests(unittest.TestCase):
    def test_run_binding_rejects_live_wrong_source_repo_or_repeated_producer(self):
        run, env = identity()
        resume.verify_run(run, '123', 'a' * 40, env)
        for field, value in [('status', 'in_progress'), ('run_attempt', 2), ('head_sha', 'f' * 40),
                             ('head_branch', 'other'), ('conclusion', 'success'), ('id', 124)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                resume.verify_run({**run, field: value}, '123', 'a' * 40, env)
        with self.assertRaises(ValueError):
            resume.verify_run(run, '123', 'a' * 40, {**env, 'GITHUB_ACTIONS': 'false'})

    def test_archive_digest_source_size_and_path_are_checked(self):
        run, _ = identity()
        meta, raw = archive({'matrix.json': b'[]'})
        self.assertEqual({'matrix.json': b'[]'}, resume.members(raw, meta, run, lambda p: p == 'matrix.json'))
        for field, value in [('digest', 'sha256:' + '0' * 64), ('size_in_bytes', len(raw) + 1), ('expired', True)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                resume.members(raw, {**meta, field: value}, run, lambda _: True)
        altered = copy.deepcopy(meta); altered['workflow_run']['id'] = 124
        with self.assertRaises(ValueError): resume.members(raw, altered, run, lambda _: True)
        for name in ('../escape.json', '/escape.json', 'folder\\escape.json'):
            meta, raw = archive({name: b'{}'})
            with self.subTest(name=name), self.assertRaises(ValueError):
                resume.members(raw, meta, run, lambda _: True)

    def test_restore_preserves_exact_bytes_identity_newer_directory_and_is_not_reapplied(self):
        api = API(); calls = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            current = b'{"asOf":"2026-10-29","funds":[{"code":"000001"},{"code":"000002"}]}\n'
            resume.stage(root / 'web/data', {'catalog.json': current})
            result = resume.resume('123', 'a' * 40, root=root, api=api, env=api.env,
                                   execute=lambda *args, **kwargs: calls.append((args, kwargs)))
            self.assertEqual(result['status'], 'merged')
            self.assertEqual((root / '.cache/parts/part-0/part.json').read_bytes(), api.part)
            self.assertEqual((root / 'web/data/catalog.json').read_bytes(), current)
            self.assertEqual(len(calls), 1)
            merge_env = calls[0][1]['env']
            self.assertEqual((merge_env['GITHUB_RUN_ID'], merge_env['GITHUB_RUN_ATTEMPT'], merge_env['GITHUB_SHA']),
                             ('123', '1', 'a' * 40))
            self.assertEqual(api.env['GITHUB_RUN_ID'], '999')
            api.calls.clear()
            repeated = resume.resume('123', 'a' * 40, root=root, api=api, env=api.env,
                                      execute=lambda *args, **kwargs: self.fail('replayed'))
            self.assertEqual(repeated['status'], 'already-published')
            self.assertEqual(api.calls, ['actions/runs/123'])
            self.assertEqual(result['sourceRequests'], 0)
            self.assertEqual(result['marketDataRequests'], 0)

    def test_missing_or_corrupt_receipt_aborts_before_any_merge_or_marker(self):
        for corrupt in (False, True):
            api = API()
            if corrupt: api.artifacts[-1]['digest'] = 'sha256:' + '0' * 64
            else: api.artifacts.pop()
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(ValueError):
                    resume.resume('123', 'a' * 40, root=Path(folder).resolve(), api=api, env=api.env,
                                  execute=lambda *a, **kw: self.fail('merge before verification'))
                self.assertFalse((Path(folder) / 'web/data/traversal-resumptions/123-1.json').exists())

    def test_failed_merge_never_creates_published_marker(self):
        api = API()
        def error(*args, **kwargs): raise RuntimeError('synthetic merge failure')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError):
                resume.resume('123', 'a' * 40, root=Path(folder).resolve(), api=api, env=api.env, execute=error)
            self.assertFalse((Path(folder) / 'web/data/traversal-resumptions/123-1.json').exists())

    def test_remote_readback_requires_identical_data_tree_and_exact_persisted_marker(self):
        marker = resume.encoded({'runId': '123', 'runAttempt': 1, 'sourceCommit': 'a' * 40, 'status': 'merged'})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            resume.stage(root / 'web/data/traversal-resumptions', {'123-1.json': marker})
            def run(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, marker if command[1] == 'show' else b'', b'')
            self.assertEqual(resume.verify_persisted('123', 'a' * 40, root=root, execute=run)['status'], 'remote-verified')
            def changed(command, **kwargs):
                return subprocess.CompletedProcess(command, 1 if command[1] == 'diff' else 0, b'', b'')
            with self.assertRaisesRegex(ValueError, 'REMOTE_READBACK_FAILED'):
                resume.verify_persisted('123', 'a' * 40, root=root, execute=changed)


if __name__ == '__main__': unittest.main()
