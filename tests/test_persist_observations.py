"""Small local bare repositories exercise push races without network access."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('persist_observations', ROOT / 'scripts/persist-observations.py')
persist = importlib.util.module_from_spec(spec)
spec.loader.exec_module(persist)


class PersistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote, self.repo, self.other = [root / name for name in ('remote.git', 'publisher', 'source')]
        self.env = {**os.environ, 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_COMMITTER_NAME': 'Fixture',
                    'GIT_AUTHOR_EMAIL': 'fixture@example.test', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
        self.git(root, 'init', '--bare', '--initial-branch=main', str(self.remote))
        self.git(root, 'init', '--initial-branch=main', str(self.repo))
        (self.repo / 'web/data').mkdir(parents=True)
        (self.repo / 'web/data/observed.json').write_text('{"observed":1}\n')
        (self.repo / 'source.js').write_text('source one\n')
        self.git(self.repo, 'add', '.')
        self.git(self.repo, 'commit', '-m', 'Seed fixture')
        self.git(self.repo, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.repo, 'push', '-u', 'origin', 'main')
        self.git(root, 'clone', str(self.remote), str(self.other))
        self.commands, self.advances = [], 0

    def git(self, repo, *args):
        result = subprocess.run(['git', '-c', 'commit.gpgsign=false', *args], cwd=repo, env=self.env,
                                capture_output=True, check=True)
        return result.stdout.decode().strip()

    def change_data(self):
        (self.repo / 'web/data/observed.json').write_text('{"observed":2}\n')

    def advance(self, data=False):
        self.git(self.other, 'pull', '--ff-only', 'origin', 'main')
        self.advances += 1
        relative = 'web/data/observed.json' if data else 'source.js'
        (self.other / relative).write_text('{"observed":99}\n' if data else f'source {self.advances + 1}\n')
        self.git(self.other, 'add', '--', relative)
        self.git(self.other, 'commit', '-m', 'Concurrent fixture update')
        self.git(self.other, 'push', 'origin', 'HEAD:main')

    def runner(self, before_push=None, after_push=None, fail_fetch=False):
        def run(args, **kwargs):
            self.commands.append(args)
            action = args[3]
            if action == 'fetch' and fail_fetch:
                return subprocess.CompletedProcess(args, 128, b'', b'fixture unavailable')
            if action == 'push' and before_push:
                before_push()
            result = subprocess.run(args, **kwargs)
            if action == 'push' and after_push:
                return after_push(args, result)
            return result
        return run

    def execute(self, **kwargs):
        return persist.persist(self.repo, sleep=lambda _: None, **kwargs)

    def assert_neutral_data_commit(self, commit):
        self.assertEqual(self.git(self.repo, 'show', '-s', '--format=%an|%ae|%cn|%ce', commit),
                         'Automation|automation@users.noreply.github.com|Automation|automation@users.noreply.github.com')
        self.assertEqual(self.git(self.repo, 'diff', '--name-only', commit + '^', commit), 'web/data/observed.json')
        self.assertFalse(any('--force' in command or '-f' in command for command in self.commands))

    def test_normal_push_is_read_back_and_only_data_is_committed(self):
        self.change_data()
        (self.repo / 'local-only.txt').write_text('not included\n')
        result = self.execute(run=self.runner())
        self.assertEqual(result['pushAttempts'], 1)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'main'), result['commit'])
        self.assertEqual(self.git(self.remote, 'show', 'main:web/data/observed.json'), '{"observed":2}')
        self.assertEqual((self.repo / 'local-only.txt').read_text(), 'not included\n')
        self.assert_neutral_data_commit(result['commit'])
        self.assertEqual(sum(command[3] == 'fetch' for command in self.commands), 2)

    def test_concurrent_source_push_rebases_data_and_preserves_new_source(self):
        self.change_data()
        result = self.execute(run=self.runner(before_push=lambda: self.advance() if self.advances == 0 else None))
        self.assertEqual(result['pushAttempts'], 2)
        self.assertEqual(self.git(self.remote, 'show', 'main:source.js'), 'source 2')
        self.assertEqual((self.repo / 'source.js').read_text(), 'source 2\n')
        self.assertEqual(self.git(self.remote, 'show', 'main:web/data/observed.json'), '{"observed":2}')
        self.assert_neutral_data_commit(result['commit'])

    def test_source_change_before_initial_fetch_is_preserved(self):
        self.advance(); self.change_data()
        result = self.execute(run=self.runner())
        self.assertEqual(result['pushAttempts'], 1)
        self.assertEqual((self.repo / 'source.js').read_text(), 'source 2\n')
        self.assert_neutral_data_commit(result['commit'])

    def test_concurrent_data_change_fails_without_overwriting_either_observation(self):
        self.change_data()
        with self.assertRaisesRegex(persist.PersistError, '^CONCURRENT_DATA_CHANGE$'):
            self.execute(run=self.runner(before_push=lambda: self.advance(data=True)))
        self.assertEqual(sum(command[3] == 'push' for command in self.commands), 1)
        self.assertEqual(self.git(self.remote, 'show', 'main:web/data/observed.json'), '{"observed":99}')
        self.assertEqual((self.repo / 'web/data/observed.json').read_text(), '{"observed":2}\n')
        self.assertFalse(any(command[3] == 'rebase' for command in self.commands))

    def test_unknown_success_is_detected_before_any_second_push(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                (self.repo / 'web/data/observed.json').write_text('{"observed":3}\n' if timeout else '{"observed":2}\n')
                def unknown(args, result):
                    self.assertEqual(result.returncode, 0)
                    if timeout: raise subprocess.TimeoutExpired(args, 45)
                    return subprocess.CompletedProcess(args, 128, b'', b'fixture lost acknowledgement')
                result = self.execute(run=self.runner(after_push=unknown))
                self.assertEqual(result['status'], 'persisted')
                self.assertEqual(result['pushAttempts'], 1)

    def test_unknown_success_followed_by_source_commit_updates_checkout_without_retry(self):
        self.change_data()
        def unknown(args, result):
            self.assertEqual(result.returncode, 0); self.advance()
            raise subprocess.TimeoutExpired(args, 45)
        result = self.execute(run=self.runner(after_push=unknown))
        self.assertEqual(result['pushAttempts'], 1)
        self.assertEqual((self.repo / 'source.js').read_text(), 'source 2\n')
        self.assertEqual(sum(command[3] == 'push' for command in self.commands), 1)
        self.git(self.repo, 'merge-base', '--is-ancestor', result['commit'], 'HEAD')

    def test_three_source_races_stop_without_force_push(self):
        self.change_data()
        with self.assertRaisesRegex(persist.PersistError, '^PUSH_RETRY_EXHAUSTED$'):
            self.execute(run=self.runner(before_push=self.advance))
        self.assertEqual(sum(command[3] == 'push' for command in self.commands), 3)
        self.assertEqual(self.git(self.remote, 'show', 'main:web/data/observed.json'), '{"observed":1}')
        self.assertEqual((self.repo / 'web/data/observed.json').read_text(), '{"observed":2}\n')
        self.assert_neutral_data_commit(self.git(self.repo, 'rev-parse', 'HEAD'))

    def test_unverified_remote_outcome_does_not_retry_a_push(self):
        self.change_data()
        push_seen = False
        real = self.runner()
        def run(args, **kwargs):
            nonlocal push_seen
            if args[3] == 'fetch' and push_seen:
                self.commands.append(args)
                return subprocess.CompletedProcess(args, 128, b'', b'fixture unavailable')
            result = real(args, **kwargs)
            if args[3] == 'push': push_seen = True
            return result
        with self.assertRaisesRegex(persist.PersistError, '^REMOTE_OUTCOME_UNVERIFIED$'):
            self.execute(run=run)
        self.assertEqual(sum(command[3] == 'push' for command in self.commands), 1)
        self.assertEqual(sum(command[3] == 'fetch' for command in self.commands), 4)
        self.assertEqual(self.git(self.remote, 'show', 'main:web/data/observed.json'), '{"observed":2}')

    def test_non_data_staged_or_unstaged_changes_are_rejected(self):
        self.change_data(); (self.repo / 'source.js').write_text('local source edit\n')
        for staged in (False, True):
            if staged: self.git(self.repo, 'add', 'source.js')
            with self.assertRaisesRegex(persist.PersistError, '^NON_DATA_CHANGE_PRESENT$'):
                self.execute(run=self.runner())
        self.assertFalse(any(command[3] == 'push' for command in self.commands))

    def test_no_data_change_fast_forwards_source_and_never_pushes(self):
        self.advance()
        result = self.execute(run=self.runner())
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(result['pushAttempts'], 0)
        self.assertEqual((self.repo / 'source.js').read_text(), 'source 2\n')
        self.assertFalse(any(command[3] == 'push' for command in self.commands))

    def test_rename_into_data_cannot_hide_a_source_deletion(self):
        self.git(self.repo, 'mv', 'source.js', 'web/data/moved-source.js')
        with self.assertRaisesRegex(persist.PersistError, '^NON_DATA_CHANGE_PRESENT$'):
            self.execute(run=self.runner())
        self.assertFalse(any(command[3] == 'push' for command in self.commands))
        self.assertEqual(self.git(self.remote, 'show', 'main:source.js'), 'source one')

    def test_workflow_has_one_shared_data_persistence_entry(self):
        traverse = (ROOT / '.github/workflows/traverse.yml').read_text()
        update = (ROOT / '.github/workflows/update.yml').read_text()
        self.assertIn('run: python scripts/persist-observations.py', traverse)
        self.assertNotIn('git push', traverse)
        self.assertIn('gh workflow run traverse.yml --ref main', update)
        self.assertNotIn('git push', update)

    def test_data_transfer_commands_use_bounded_timeouts_for_observed_sizes(self):
        received = {}
        def run(args, **kwargs):
            received[args[3]] = kwargs['timeout']
            return subprocess.CompletedProcess(args, 0, b'', b'')
        git = persist.Git(self.repo, run=run)
        for command in ('add', 'commit', 'rebase', 'push', 'fetch', 'rev-parse', 'ls-tree', 'diff', 'merge-base'):
            git.call(command)
        self.assertEqual(received, {'add': 180, 'commit': 180, 'rebase': 180, 'push': 300,
                                   'fetch': 120, 'rev-parse': 45, 'ls-tree': 45, 'diff': 45, 'merge-base': 45})


if __name__ == '__main__':
    unittest.main()
