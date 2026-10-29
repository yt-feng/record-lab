"""Small local Git fixtures: parallel content checks equal the serial verdict."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import publication_guard as guard


def failed_worker(*_):
    raise RuntimeError('Synthetic content worker failure')


def terminated_worker(*_):
    os._exit(3)


class ParallelPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Automation')
        self.git('config', 'user.email', 'automation@users.noreply.github.com')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, name, value, message='Update public fixture'):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(value if isinstance(value, bytes) else value.encode())
        self.git('add', '-f', name)
        self.git('commit', '-m', message)

    def compare(self, cached=frozenset()):
        snapshots = []
        for workers in (1, 2):
            stats, verified = {}, set()
            findings = guard.scan_history(self.root, stats, cached_blobs=cached,
                                          verified_blobs=verified, workers=workers)
            snapshots.append((findings, stats, verified))
        self.assertEqual(snapshots[0][0], snapshots[1][0])
        self.assertEqual(snapshots[0][2], snapshots[1][2])
        for field in ('commits', 'tree_entries', 'unique_blobs', 'cached_blobs', 'fresh_blobs', 'bytes'):
            self.assertEqual(snapshots[0][1][field], snapshots[1][1][field], field)
        self.assertLessEqual(snapshots[1][1]['peak_pending_blobs'], 2)
        self.assertLessEqual(snapshots[1][1]['peak_pending_bytes'], guard.MAX_PROGRESS_BYTES)
        return snapshots[1]

    def test_clean_history_and_deduplicated_blobs_have_identical_stats_and_proof(self):
        self.commit('README.md', 'Public fixture')
        self.commit('web/data/latest.json', '基金公开数据' * 100)
        self.commit('web/data/other.json', '基金公开数据' * 100)
        self.commit('README.md', 'New public fixture')
        findings, stats, proof = self.compare()
        self.assertEqual(findings, [])
        self.assertEqual(stats['unique_blobs'], 3)
        self.assertEqual(len(proof), 3)
        self.assertEqual(stats['peak_pending_blobs'], 2)

    def test_all_refs_deleted_content_metadata_modes_and_paths_remain_checked(self):
        self.commit('README.md', 'Public fixture')
        self.git('checkout', '-b', 'other')
        self.commit('README.md', 'gh' + 'p_' + 'z' * 32)
        self.git('checkout', 'main')
        self.commit('web/data/latest.json', b'bad\x00content')
        self.commit('web/data/latest.json', b'bad\xffencoding')
        self.commit('README.md', 'Public fixture again', 'Message ' + ('sk-' + 'x' * 32))
        self.commit('raw/payload.json', 'Public fixture')
        (self.root / 'web/link.txt').symlink_to('Public fixture')
        self.git('add', 'web/link.txt')
        self.git('commit', '-m', 'Link fixture')
        findings, _, proof = self.compare()
        for finding in ('credential', 'unexpected_history_file', 'unsupported_content', 'unsupported_encoding'):
            self.assertIn(finding, findings)
        self.assertEqual(proof, set())

    def test_cached_blobs_skip_only_content_and_no_pool_is_started_for_all_cached(self):
        self.commit('README.md', 'Public fixture')
        cached = frozenset({self.git('rev-parse', 'HEAD:README.md').encode()})
        with mock.patch.object(guard, 'ProcessPoolExecutor', side_effect=AssertionError('No fresh blobs')):
            findings, stats, proof = self.compare(cached)
        self.assertEqual((findings, stats['bytes'], stats['fresh_blobs'], proof), ([], 0, 0, set(cached)))
        self.commit('raw/data.json', 'Public fixture')
        findings, _, proof = self.compare(cached)
        self.assertIn('unexpected_history_file', findings)
        self.assertEqual(proof, set())

    def test_size_allowance_is_checked_per_path_even_for_cached_blob(self):
        self.commit('web/data/progress.json', 'x' * 300)
        cached = frozenset({self.git('rev-parse', 'HEAD:web/data/progress.json').encode()})
        with mock.patch.object(guard, 'MAX_CONTENT_BYTES', 200), mock.patch.object(guard, 'MAX_PROGRESS_BYTES', 400):
            self.assertEqual(self.compare()[0], [])
            self.commit('web/data/latest.json', 'x' * 300)
            findings, _, proof = self.compare(cached)
        self.assertIn('unsupported_content', findings)
        self.assertEqual(proof, set())

    def test_detectors_and_finding_order_are_exactly_equal(self):
        values = [
            ('gh' + 'p_' + 'x' * 32).encode(),
            ('/Us' + 'ers/person/project/').encode(),
            ('person' + '@' + 'private-domain.test').encode(),
            ('https://example.com?to' + 'ken=' + 'z' * 30).encode(),
            ("APİ_KEY = '" + 'z' * 20).encode(), b'\xff', b'abc\x00def',
            '基金报告 https://example.com/report.pdf'.encode(),
        ]
        expected = []
        actual = []
        with guard.HistoryContentChecks(actual, workers=2) as checks:
            for index, value in enumerate(values):
                expected.extend(guard.inspect_content(value, guard.MAX_PROGRESS_BYTES))
                checks.submit(value)
                if index == 2:
                    expected.append('unexpected_history_file')
                    checks.add_findings(['unexpected_history_file'])
        self.assertEqual(actual, expected)

    def test_bounded_queue_reserves_space_before_reading_next_blob(self):
        findings = []
        with guard.HistoryContentChecks(findings, workers=2, max_pending_bytes=100) as checks:
            checks.submit(b'x' * 60)
            checks.reserve(50)
            self.assertEqual(checks.pending_bytes, 0)
            checks.submit(b'y' * 50)
            checks.submit(b'z' * 50)
            self.assertEqual((checks.pending_bytes, len(checks.pending)), (100, 2))
            checks.reserve(1)
            self.assertLessEqual(len(checks.pending), 1)
            self.assertEqual(checks.peak_pending_bytes, 100)
            with self.assertRaises(RuntimeError):
                checks.submit(b'x' * 101)
        self.assertEqual(findings, [])

    def test_actual_worker_error_or_exit_never_yields_partial_proof(self):
        self.commit('README.md', 'Public fixture')
        self.commit('web/data/latest.json', 'More public fixture')
        real_pool = guard.ProcessPoolExecutor
        for worker in (failed_worker, terminated_worker):
            def pool_factory(**kwargs):
                pool = real_pool(**kwargs)
                original_submit = pool.submit
                calls = 0
                def submit(function, *args):
                    nonlocal calls
                    calls += 1
                    return original_submit(worker if calls == 2 else function, *args)
                pool.submit = submit
                return pool
            verified, stats = set(), {}
            with self.subTest(worker=worker.__name__), mock.patch.object(guard, 'ProcessPoolExecutor', side_effect=pool_factory):
                with self.assertRaises(Exception):
                    guard.scan_history(self.root, stats, verified_blobs=verified, workers=2)
            self.assertEqual((verified, stats), (set(), {}))

    def test_real_cli_spawn_path_finishes_and_reports_two_worker_bound(self):
        self.commit('README.md', 'Public fixture')
        self.commit('web/data/latest.json', 'More public fixture')
        result = subprocess.run([sys.executable, str(Path(guard.__file__)), '--root', str(self.root), '--history'],
                                capture_output=True, text=True, timeout=20,
                                env={**os.environ, 'GITHUB_ACTIONS': 'false'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('content_workers=2', result.stdout)
        self.assertIn('publication_check_passed:', result.stdout)

    def test_main_uses_two_workers_and_never_writes_proof_after_scan_failure(self):
        with mock.patch.object(sys, 'argv', ['publication_guard.py', '--root', str(self.root), '--history']), \
             mock.patch('publication_cache.load_verified', return_value=frozenset()), \
             mock.patch('publication_cache.write_proof') as write, \
             mock.patch.object(guard, 'scan', side_effect=RuntimeError('Worker failed')) as scan:
            self.assertEqual(guard.main(), 1)
            self.assertEqual(scan.call_args.kwargs['history_workers'], 2)
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
