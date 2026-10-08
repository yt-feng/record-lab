import importlib.util
import unittest
import tempfile
import subprocess
from unittest import mock
from pathlib import Path
spec = importlib.util.spec_from_file_location('guard', Path(__file__).parents[1] / 'scripts/publication_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

class PublicationTests(unittest.TestCase):
    def test_explicit_path_allowlist(self):
        self.assertTrue(guard.allowed_path('web/data/latest.json'))
        for item in ['.env', 'web/.env.json', 'raw/data.json', 'web/../../keys.txt', 'docs/private.key', 'web/file.bin']:
            self.assertFalse(guard.allowed_path(item), item)

    def test_identity_and_tokens(self):
        self.assertIn('credential', guard.inspect_content(('gh' + 'p_' + 'z' * 32).encode()))
        self.assertIn('machine_path', guard.inspect_content(('/Us' + 'ers/person/project/').encode()))
        self.assertIn('personal_email', guard.inspect_content(('person' + '@' + 'private-domain.test').encode()))
        self.assertEqual(guard.inspect_content(b'Automation <automation@users.noreply.github.com>'), [])

    def test_sensitive_url_and_binary(self):
        self.assertIn('url_credential', guard.inspect_content(('https://example.com?to' + 'ken=' + 'z' * 30).encode()))
        self.assertEqual(guard.inspect_content(b'abc\0def'), ['unsupported_content'])
        self.assertEqual(guard.inspect_content('基金报告：https://example.com/report.pdf'.encode()), [])

    def test_public_plan_artifact_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'matrix.json').write_text('[]')
            (root / 'meta.json').write_text('{}')
            (root / 'batch-0.json').write_text('{}')
            self.assertEqual(guard.scan_plan(root), (3, []))
            (root / 'batch-0.json').write_text('gh' + 'p_' + 'z' * 32)
            self.assertIn('credential', guard.scan_plan(root)[1])
            (root / 'private.txt').write_text('not allowed')
            self.assertIn('unexpected_plan_file', guard.scan_plan(root)[1])


class HistoryPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Automation')
        self.git('config', 'user.email', 'automation@users.noreply.github.com')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit_file(self, name, value, message='Update public fixture'):
        path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value)
        self.git('add', name); self.git('commit', '-m', message)

    def test_unchanged_blob_is_scanned_once_across_many_commits(self):
        content = 'Public historical data ' * 1000
        self.commit_file('web/data/latest.json', content)
        for index in range(12):
            self.commit_file('README.md', 'Public change '+str(index))
        stats = {}
        with mock.patch.object(guard, 'inspect_content', wraps=guard.inspect_content) as inspect:
            self.assertEqual(guard.scan_history(self.root, stats), [])
        self.assertEqual(sum(call.args[0] == content.encode() for call in inspect.call_args_list), 1)
        self.assertEqual((stats['commits'], stats['tree_entries'], stats['unique_blobs']), (13, 25, 13))
        self.assertEqual(stats['bytes'], len(content.encode()) + sum(len(('Public change '+str(i)).encode()) for i in range(12)))
        self.assertGreaterEqual(stats['elapsed_seconds'], 0)

    def test_removed_credentials_and_commit_metadata_still_fail(self):
        token = 'gh'+'p_'+'z'*32
        self.commit_file('README.md', token)
        self.commit_file('README.md', 'Public fixture', 'Message '+('sk-'+'x'*32))
        findings = guard.scan_history(self.root)
        self.assertGreaterEqual(findings.count('credential'), 2)

    def test_identity_in_historical_author_still_fails(self):
        self.git('config', 'user.email', 'person'+'@'+'private-domain.test')
        self.commit_file('README.md', 'Public fixture')
        self.assertIn('personal_email', guard.scan_history(self.root))

    def test_same_blob_at_disallowed_path_or_symlink_is_not_skipped(self):
        self.commit_file('README.md', 'public-target')
        self.commit_file('raw/payload.json', 'public-target')
        (self.root/'web').mkdir()
        (self.root/'web'/'link.txt').symlink_to('public-target')
        self.git('add', 'web/link.txt'); self.git('commit', '-m', 'Link fixture')
        self.assertEqual(guard.scan_history(self.root).count('unexpected_history_file'), 3)

    def test_non_main_reachable_history_is_checked(self):
        self.commit_file('README.md', 'Public fixture')
        self.git('checkout', '-b', 'other')
        self.commit_file('README.md', 'gh'+'p_'+'x'*32)
        self.git('checkout', 'main')
        self.assertIn('credential', guard.scan_history(self.root))

    def test_oversized_blob_is_rejected_and_next_response_is_intact(self):
        self.commit_file('README.md', 'x'*2048)
        large = self.git('rev-parse', 'HEAD:README.md').encode()
        self.commit_file('README.md', 'Public fixture')
        small = self.git('rev-parse', 'HEAD:README.md').encode()
        with mock.patch.object(guard, 'MAX_CONTENT_BYTES', 1024):
            with guard.history_blobs(self.root) as read:
                self.assertIsNone(read(large))
                self.assertEqual(read(small), b'Public fixture')
            self.assertIn('unsupported_content', guard.scan_history(self.root))

    def test_missing_object_fails_closed(self):
        with guard.history_blobs(self.root) as read:
            with self.assertRaisesRegex(RuntimeError, 'Invalid history blob response'):
                read(b'0'*40)

if __name__ == '__main__':
    unittest.main()
