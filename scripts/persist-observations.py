"""Persist one data-only commit without overwriting concurrent observations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time

COMMAND_TIMEOUTS = {'add': 180, 'commit': 180, 'rebase': 180, 'push': 300, 'fetch': 120}


class PersistError(Exception):
    pass


class Git:
    def __init__(self, repo, run=None, sleep=None):
        self.repo = Path(repo)
        self.run = run or subprocess.run
        self.sleep = sleep or time.sleep
        self.env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0',
                    'GIT_AUTHOR_NAME': 'Automation', 'GIT_COMMITTER_NAME': 'Automation',
                    'GIT_AUTHOR_EMAIL': 'automation@users.noreply.github.com',
                    'GIT_COMMITTER_EMAIL': 'automation@users.noreply.github.com'}

    def call(self, *args):
        command = ['git', '-c', 'commit.gpgsign=false', *args]
        try:
            return self.run(command, cwd=self.repo, env=self.env, capture_output=True,
                            timeout=COMMAND_TIMEOUTS.get(args[0], 45))
        except subprocess.TimeoutExpired:
            # A timed-out push has an unknown outcome. Its caller must fetch
            # before any further mutation; never expose transport diagnostics.
            return subprocess.CompletedProcess(command, 124, b'', b'')
        except OSError:
            return subprocess.CompletedProcess(command, 125, b'', b'')

    def checked(self, *args, error='GIT_OPERATION_FAILED'):
        result = self.call(*args)
        if result.returncode:
            raise PersistError(error)
        return result.stdout

    def oid(self, revision):
        value = self.checked('rev-parse', '--verify', revision).strip().decode('ascii')
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
            raise PersistError('INVALID_COMMIT_ID')
        return value

    def ancestor(self, first, second):
        result = self.call('merge-base', '--is-ancestor', first, second)
        if result.returncode not in (0, 1):
            raise PersistError('ANCESTOR_CHECK_FAILED')
        return result.returncode == 0

    def data_tree(self, revision):
        value = self.checked('ls-tree', '-z', revision, '--', 'web/data')
        if not value:
            return None
        match = re.fullmatch(rb'040000 tree ([0-9a-f]{40}|[0-9a-f]{64})\tweb/data\x00', value)
        if not match:
            raise PersistError('INVALID_DATA_TREE')
        return match[1].decode('ascii')

    def fetch(self):
        for attempt in range(3):
            result = self.call('fetch', '--no-tags', 'origin', 'refs/heads/main')
            if result.returncode == 0:
                return self.oid('FETCH_HEAD^{commit}')
            if attempt < 2:
                self.sleep(attempt + 1)
        raise PersistError('REMOTE_OUTCOME_UNVERIFIED')


def data_paths_only(raw):
    return all(name == b'web/data' or name.startswith(b'web/data/')
               for name in raw.split(b'\x00') if name)


def persist(repo='.', *, run=None, sleep=None):
    git = Git(repo, run=run, sleep=sleep)
    base = git.oid('HEAD^{commit}')
    # Reject an unrelated staged or tracked change before staging anything.
    if not data_paths_only(git.checked('diff', '--no-renames', '--name-only', '-z', 'HEAD', '--')):
        raise PersistError('NON_DATA_CHANGE_PRESENT')
    git.checked('add', '--all', '--', 'web/data')
    if not data_paths_only(git.checked('diff', '--cached', '--no-renames', '--name-only', '-z', '--')):
        raise PersistError('NON_DATA_CHANGE_PRESENT')
    changed = git.call('diff', '--cached', '--quiet')
    if changed.returncode not in (0, 1):
        raise PersistError('INDEX_CHECK_FAILED')
    if changed.returncode == 0:
        remote = git.fetch()
        if not git.ancestor(base, remote):
            raise PersistError('REMOTE_HISTORY_DIVERGED')
        git.checked('merge', '--ff-only', remote, error='SOURCE_FAST_FORWARD_FAILED')
        return {'status': 'unchanged', 'pushAttempts': 0, 'commit': remote}

    original_data = git.data_tree(base)
    git.checked('commit', '--no-gpg-sign', '-m', 'Update public observations and coverage', error='DATA_COMMIT_FAILED')
    commit = git.oid('HEAD^{commit}')
    expected_data = git.data_tree(commit)

    def finish(remote, attempts):
        git.checked('merge', '--ff-only', remote, error='SOURCE_FAST_FORWARD_FAILED')
        return {'status': 'persisted', 'pushAttempts': attempts, 'commit': commit}

    def synchronize(remote):
        nonlocal base, commit
        if git.ancestor(commit, remote):
            return True
        # Only source-only movement since this data commit's parent can be
        # replayed. Equal final data trees do not authorize divergent history.
        if not git.ancestor(base, remote):
            raise PersistError('REMOTE_HISTORY_DIVERGED')
        if git.data_tree(remote) != original_data:
            raise PersistError('CONCURRENT_DATA_CHANGE')
        if remote != base:
            result = git.call('rebase', '--no-autostash', '--no-autosquash', '--onto', remote, base)
            if result.returncode:
                git.call('rebase', '--abort')
                raise PersistError('DATA_REPLAY_FAILED')
            commit = git.oid('HEAD^{commit}')
            if git.oid('HEAD^') != remote or git.data_tree(commit) != expected_data:
                raise PersistError('DATA_REPLAY_VERIFICATION_FAILED')
            if not data_paths_only(git.checked('diff', '--no-renames', '--name-only', '-z', remote, commit, '--')):
                raise PersistError('DATA_REPLAY_VERIFICATION_FAILED')
            base = remote
        return False

    remote = git.fetch()
    for attempt in range(1, 4):
        if synchronize(remote):
            return finish(remote, attempt - 1)
        git.call('push', 'origin', 'HEAD:refs/heads/main')
        # Check even a successful transport response, and always check a
        # rejection/timeout before retrying. The commit may already be remote.
        remote = git.fetch()
        if git.ancestor(commit, remote):
            return finish(remote, attempt)
        if not git.ancestor(base, remote):
            raise PersistError('REMOTE_HISTORY_DIVERGED')
        if git.data_tree(remote) != original_data:
            raise PersistError('CONCURRENT_DATA_CHANGE')
        if attempt < 3:
            git.sleep(attempt)
    raise PersistError('PUSH_RETRY_EXHAUSTED')


def main():
    try:
        if os.environ.get('GITHUB_ACTIONS') != 'true':
            raise PersistError('PERSIST_REQUIRES_ACTIONS')
        result = persist()
        print(json.dumps(result))
        return 0
    except PersistError as error:
        print(json.dumps({'status': 'failed', 'errorCode': str(error)}))
        return 1
    except Exception:
        print(json.dumps({'status': 'failed', 'errorCode': 'PERSIST_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
