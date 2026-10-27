"""Record one conservative audit hold using GitHub metadata only.

Prepare uploads its receipt before apply may update main. There is exactly one
non-forced ref request; an ambiguous response is investigated with GET only.
"""
from __future__ import annotations
import argparse
import base64
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from publication_cache import read_bounded_command
from publication_guard import inspect_content
from recover_batches import ORIGINAL_RUN, ORIGINAL_SOURCE, MISSING, REPOSITORY, parse, encoded, digest

WORKFLOW = '.github/workflows/record-recovery-hold.yml'
AUDIT_PATH = 'web/data/periods/2026Q2/audit.json'
PROGRESS_PATH = 'web/data/periods/2026Q2/progress.json'
PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
MAX_BYTES = 2_000_000
SHA = re.compile(r'[0-9a-f]{40}')
FILES = {'intent.json', 'prepared.json', 'receipt.json', 'result.json', 'error.json'}
REQUIRED_STEPS = {'Merge source-linked results', 'Persist observations and checkpoint', 'Prepare compressed public site'}


class HoldError(ValueError):
    pass


def fail(code):
    raise HoldError(code)


def git_blob(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def write(root, name, value):
    if name not in FILES: fail('HOLD_OUTPUT_INVALID')
    raw = encoded(value)
    if len(raw) > MAX_BYTES or inspect_content(raw): fail('HOLD_PUBLICATION_REJECTED')
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    file = root / name; temporary = root / (name + '.tmp')
    temporary.write_bytes(raw); temporary.replace(file)


def read(root, name):
    file = Path(root) / name
    if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_BYTES: fail('HOLD_RECEIPT_INVALID')
    try: return parse(file.read_bytes())
    except Exception: fail('HOLD_RECEIPT_INVALID')


def validate_output(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir(): fail('HOLD_OUTPUT_INVALID')
    for file in root.iterdir():
        if (file.name not in FILES or file.is_symlink() or not file.is_file()
                or file.stat().st_size > MAX_BYTES or inspect_content(file.read_bytes())): fail('HOLD_PUBLICATION_REJECTED')
    return True


class ActionsAPI:
    def __init__(self, env):
        if env.get('GITHUB_ACTIONS') != 'true' or not env.get('GH_TOKEN'): fail('HOLD_REQUIRES_ACTIONS')
        self.deadline = time.monotonic() + 240

    def request(self, method, suffix, body=None):
        get = r'(?:actions/runs/\d+(?:/(?:jobs|artifacts)\?per_page=100)?|git/(?:ref/heads/main|(?:commits|trees|blobs)/[a-f0-9]{40}))'
        post = r'git/(?:blobs|trees|commits)'
        allowed = bool(re.fullmatch(get, suffix)) if method == 'GET' else bool(re.fullmatch(post, suffix)) if method == 'POST' else method == 'PATCH' and suffix == 'git/refs/heads/main'
        if not allowed or method == 'GET' and body is not None: fail('HOLD_ENDPOINT_INVALID')
        command = ['gh', 'api', '--hostname', 'github.com', '--method', method, f'repos/{REPOSITORY}/{suffix}']
        try:
            if body is None:
                raw = read_bounded_command(command, 3_000_000, min(self.deadline, time.monotonic() + 30))
            else:
                # The payload is public Git metadata or an already-guarded audit.
                # The credential stays in the process environment, never a file.
                with tempfile.NamedTemporaryFile() as handle:
                    handle.write(encoded(body)); handle.flush()
                    raw = read_bounded_command([*command, '--input', handle.name], 3_000_000,
                                               min(self.deadline, time.monotonic() + 30))
            return parse(raw)
        except HoldError: raise
        except Exception: fail('HOLD_API_UNAVAILABLE')


def environment(env):
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_WORKFLOW_REF') != f'{REPOSITORY}/{WORKFLOW}@refs/heads/main'
            or env.get('GITHUB_RUN_ATTEMPT') != '1' or not SHA.fullmatch(str(env.get('GITHUB_SHA')))):
        fail('HOLD_REQUIRES_ACTIONS')
    try: run_id = int(env['GITHUB_RUN_ID'])
    except (KeyError, ValueError): fail('HOLD_REQUIRES_ACTIONS')
    if run_id <= 0: fail('HOLD_REQUIRES_ACTIONS')
    return run_id


def metadata_evidence(api, env):
    run_id = environment(env)
    original = api.request('GET', f'actions/runs/{ORIGINAL_RUN}')
    current = api.request('GET', f'actions/runs/{run_id}')
    for row in (original, current):
        repo, head = row.get('repository', {}), row.get('head_repository', {})
        if (repo.get('full_name') != REPOSITORY or head.get('full_name') != REPOSITORY
                or type(repo.get('id')) is not int or repo['id'] != head.get('id')
                or row.get('head_branch') != 'main' or type(row.get('run_attempt')) is not int or row['run_attempt'] != 1):
            fail('HOLD_RUN_MISMATCH')
    if (original.get('id') != ORIGINAL_RUN or original.get('head_sha') != ORIGINAL_SOURCE
            or original.get('path') != '.github/workflows/traverse.yml'
            or original.get('status') != 'completed' or original.get('conclusion') not in {'success', 'failure'}):
        fail('HOLD_ORIGINAL_NOT_TERMINAL')
    if (current.get('id') != run_id or current.get('head_sha') != env['GITHUB_SHA'] or current.get('path') != WORKFLOW
            or current.get('event') != 'workflow_dispatch' or current.get('status') != 'in_progress'):
        fail('HOLD_RUN_MISMATCH')
    jobs = api.request('GET', f'actions/runs/{ORIGINAL_RUN}/jobs?per_page=100')
    rows = jobs.get('jobs')
    if not isinstance(rows, list) or len(rows) > 100 or type(jobs.get('total_count')) is not int or jobs['total_count'] != len(rows):
        fail('HOLD_JOBS_UNVERIFIED')
    batches, publishers = {}, []
    for row in rows:
        match = re.fullmatch(r'batch \((\d{1,2}), batch-\1\.json, 2026Q[23]\)', str(row.get('name')))
        if match:
            if match[1] in batches: fail('HOLD_JOBS_UNVERIFIED')
            batches[match[1]] = row
        if row.get('name') == 'publish': publishers.append(row)
    if set(batches) != {str(i) for i in range(32)} or len(publishers) != 1: fail('HOLD_JOBS_UNVERIFIED')
    for index, row in batches.items():
        if row.get('status') != 'completed' or row.get('conclusion') != ('failure' if index in MISSING else 'success'):
            fail('HOLD_BATCH_OUTCOME_CHANGED')
        if index in MISSING and [s.get('name') for s in row.get('steps', []) if s.get('conclusion') == 'failure'] != ['Validate batch publication']:
            fail('HOLD_BATCH_OUTCOME_CHANGED')
    publisher = publishers[0]; steps = publisher.get('steps', [])
    if (publisher.get('status') != 'completed' or publisher.get('conclusion') not in {'success', 'failure'}
            or any(sum(s.get('name') == name and s.get('conclusion') == 'success' for s in steps) != 1 for name in REQUIRED_STEPS)
            or any(s.get('conclusion') == 'failure' and s.get('name') != 'Continue unfinished traversal' for s in steps)):
        fail('HOLD_PERSISTENCE_UNVERIFIED')
    listing = api.request('GET', f'actions/runs/{ORIGINAL_RUN}/artifacts?per_page=100')
    rows = listing.get('artifacts')
    if not isinstance(rows, list) or len(rows) > 100 or type(listing.get('total_count')) is not int or listing['total_count'] != len(rows):
        fail('HOLD_ARTIFACTS_UNVERIFIED')
    names = [row.get('name') for row in rows]
    if len(set(names)) != len(names): fail('HOLD_ARTIFACTS_UNVERIFIED')
    expected = {f'part-{i}' for i in range(32) if str(i) not in MISSING}
    if {name for name in names if isinstance(name, str) and name.startswith('part-')} != expected:
        fail('HOLD_ARTIFACTS_UNVERIFIED')
    preserved = []
    for row in rows:
        if row.get('name') not in expected: continue
        identity = row.get('workflow_run', {})
        if (type(row.get('id')) is not int or row['id'] <= 0 or row.get('expired') is not False
                or type(row.get('size_in_bytes')) is not int or not 0 < row['size_in_bytes'] <= 32_000_000
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', str(row.get('digest')))
                or identity.get('id') != ORIGINAL_RUN or identity.get('head_sha') != ORIGINAL_SOURCE
                or identity.get('head_branch') != 'main' or identity.get('repository_id') != original['repository']['id']
                or identity.get('head_repository_id') != original['repository']['id']): fail('HOLD_ARTIFACTS_UNVERIFIED')
        preserved.append({'batchId': row['name'][5:], 'artifactId': row['id'], 'archiveSha256': row['digest'][7:], 'archiveBytes': row['size_in_bytes']})
    return {'originalRunId': ORIGINAL_RUN, 'originalRunAttempt': 1, 'originalSourceCommit': ORIGINAL_SOURCE,
            'persistenceStepVerified': True, 'publishJobId': publisher['id'],
            'missingBatchIds': list(MISSING), 'successfulArtifactMetadata': sorted(preserved, key=lambda row: int(row['batchId'])),
            'fundDataCompletenessVerified': False, 'externalSourceRequests': 0}


def head(api):
    row = api.request('GET', 'git/ref/heads/main')
    value = row.get('object', {})
    if row.get('ref') != 'refs/heads/main' or value.get('type') != 'commit' or not SHA.fullmatch(str(value.get('sha'))): fail('HOLD_REF_UNVERIFIED')
    return value['sha']


def commit(api, sha):
    row = api.request('GET', 'git/commits/' + sha)
    if row.get('sha') != sha or not SHA.fullmatch(str(row.get('tree', {}).get('sha'))): fail('HOLD_COMMIT_UNVERIFIED')
    return row


def tree(api, sha):
    value = api.request('GET', 'git/trees/' + sha)
    rows = value.get('tree')
    if value.get('sha') != sha or value.get('truncated') is not False or not isinstance(rows, list) or len(rows) > 5000: fail('HOLD_TREE_UNVERIFIED')
    result = {}
    for row in rows:
        name = row.get('path')
        if not isinstance(name, str) or '/' in name or name in {'', '.', '..'} or name in result or not SHA.fullmatch(str(row.get('sha'))): fail('HOLD_TREE_UNVERIFIED')
        result[name] = {key: row.get(key) for key in ('mode', 'type', 'sha')}
    return result


def entry(api, root, path):
    node = None
    for position, name in enumerate(path.split('/')):
        node = tree(api, root).get(name)
        if not node: fail('HOLD_PATH_UNVERIFIED')
        if position < len(path.split('/')) - 1 and node.get('type') != 'tree': fail('HOLD_PATH_UNVERIFIED')
        root = node['sha']
    if node.get('mode') != '100644' or node.get('type') != 'blob': fail('HOLD_PATH_UNVERIFIED')
    return node['sha']


def audit_blob(api, sha):
    row = api.request('GET', 'git/blobs/' + sha)
    if row.get('sha') != sha or row.get('encoding') != 'base64' or type(row.get('size')) is not int or not 0 < row['size'] <= MAX_BYTES: fail('HOLD_AUDIT_INVALID')
    try: raw = base64.b64decode(''.join(row['content'].split()), validate=True)
    except Exception: fail('HOLD_AUDIT_INVALID')
    if len(raw) != row['size'] or git_blob(raw) != sha or inspect_content(raw): fail('HOLD_AUDIT_INVALID')
    return raw


def transformed(raw):
    try: old = parse(raw)
    except Exception: fail('HOLD_AUDIT_INVALID')
    if (not isinstance(old, dict) or type(old.get('schemaVersion')) is not int or old['schemaVersion'] != 1
            or old.get('quarter') != '2026Q2' or old.get('phase') != PHASE): fail('HOLD_AUDIT_INVALID')
    hold = {'reason': 'BATCH_ARTIFACTS_UNVERIFIED', 'quarter': '2026Q2', 'sourceRunId': str(ORIGINAL_RUN),
            'batchIds': list(MISSING), 'requestAccounting': 'unverified'}
    errors = [{'batchId': index, 'quarter': '2026Q2', 'errorCode': 'BATCH_ARTIFACT_MISSING'} for index in MISSING]
    if old.get('continuationHold') not in (None, hold) or old.get('batchInputErrors') not in (None, [], errors): fail('HOLD_EXISTING_EVIDENCE_CONFLICT')
    if old.get('continuationHold') == hold and old.get('batchInputErrors') == errors and old.get('continueRun') is False:
        return raw, False
    new = copy.deepcopy(old); new.update(continuationHold=hold, batchInputErrors=errors, continueRun=False)
    protected = (set(old) | set(new)) - {'continuationHold', 'batchInputErrors', 'continueRun'}
    if any(old.get(key) != new.get(key) for key in protected): fail('HOLD_PROTECTED_AUDIT_CHANGED')
    result = encoded(new)
    if len(result) > MAX_BYTES or inspect_content(result): fail('HOLD_PUBLICATION_REJECTED')
    return result, True


def single_path_tree(api, old, new):
    for position, name in enumerate(AUDIT_PATH.split('/')):
        before, after = tree(api, old), tree(api, new)
        if set(before) != set(after) or name not in before or any(before[key] != after[key] for key in before if key != name): fail('HOLD_OTHER_BLOBS_CHANGED')
        if {k: before[name][k] for k in ('type', 'mode')} != {k: after[name][k] for k in ('type', 'mode')}: fail('HOLD_OTHER_BLOBS_CHANGED')
        old, new = before[name]['sha'], after[name]['sha']
        if position == len(AUDIT_PATH.split('/')) - 1: return old, new


def immutable_data(data, data_commit, old_audit, old_progress, audit_raw):
    def git(*args):
        value = subprocess.run(['git', '-C', str(data), *args], capture_output=True, text=True, timeout=30)
        if value.returncode: fail('HOLD_CHECKOUT_UNVERIFIED')
        return value.stdout.strip()
    if git('rev-parse', 'HEAD') != data_commit: fail('HOLD_CHECKOUT_UNVERIFIED')
    for path, expected in ((AUDIT_PATH, old_audit), (PROGRESS_PATH, old_progress)):
        file = Path(data) / path
        if file.is_symlink() or not file.is_file() or git('rev-parse', 'HEAD:' + path) != expected or git('hash-object', path) != expected:
            fail('HOLD_CHECKOUT_UNVERIFIED')
    if (Path(data) / AUDIT_PATH).read_bytes() != audit_raw: fail('HOLD_CHECKOUT_UNVERIFIED')


def prepare(data, data_commit, output, *, api=None, env=None):
    env = os.environ if env is None else env; run_id = environment(env); api = api or ActionsAPI(env)
    output = Path(output)
    if output.exists() or not SHA.fullmatch(str(data_commit)): fail('HOLD_PREPARE_INVALID')
    evidence = metadata_evidence(api, env)
    if head(api) != data_commit: fail('HOLD_MAIN_CHANGED')
    parent = commit(api, data_commit); old_tree = parent['tree']['sha']
    old_blob = entry(api, old_tree, AUDIT_PATH); progress_blob = entry(api, old_tree, PROGRESS_PATH)
    raw = audit_blob(api, old_blob); immutable_data(data, data_commit, old_blob, progress_blob, raw)
    candidate, changed = transformed(raw)
    intent = {'schemaVersion': 1, 'kind': 'recovery-hold-intent', 'workflowRunId': run_id, 'workflowRunAttempt': 1,
              'workflowSourceCommit': env['GITHUB_SHA'], 'parentCommit': data_commit, 'oldTree': old_tree,
              'path': AUDIT_PATH, 'oldBlob': old_blob, 'newBlob': git_blob(candidate), 'progressBlob': progress_blob,
              'oldAuditSha256': digest(raw), 'newAuditSha256': digest(candidate), 'evidence': evidence,
              'changedFields': ['batchInputErrors', 'continuationHold', 'continueRun'], 'externalSourceRequests': 0,
              'preparedAt': dt.datetime.now(dt.timezone.utc).isoformat()}
    write(output, 'intent.json', intent)
    if head(api) != data_commit: fail('HOLD_MAIN_CHANGED')
    if not changed:
        result = {'schemaVersion': 1, 'status': 'already-verified', 'commit': data_commit, 'auditBlob': old_blob,
                  'newRefRequests': 0, 'externalSourceRequests': 0, 'fundDataCompletenessVerified': False}
        write(output, 'result.json', result)
        return result
    blob = api.request('POST', 'git/blobs', {'encoding': 'base64', 'content': base64.b64encode(candidate).decode()})
    if blob.get('sha') != intent['newBlob']: fail('HOLD_BLOB_UNVERIFIED')
    new_tree = api.request('POST', 'git/trees', {'base_tree': old_tree,
        'tree': [{'path': AUDIT_PATH, 'mode': '100644', 'type': 'blob', 'sha': intent['newBlob']}]})
    tree_sha = new_tree.get('sha')
    if not SHA.fullmatch(str(tree_sha)) or single_path_tree(api, old_tree, tree_sha) != (old_blob, intent['newBlob']): fail('HOLD_TREE_UNVERIFIED')
    author = {'name': 'Records Automation', 'email': 'records-bot@users.noreply.github.com', 'date': intent['preparedAt']}
    prepared = api.request('POST', 'git/commits', {'message': 'Preserve pending request accounting', 'tree': tree_sha,
                    'parents': [data_commit], 'author': author, 'committer': author})
    sha = prepared.get('sha')
    if not SHA.fullmatch(str(sha)): fail('HOLD_COMMIT_UNVERIFIED')
    actual = commit(api, sha)
    if actual.get('tree', {}).get('sha') != tree_sha or [p.get('sha') for p in actual.get('parents', [])] != [data_commit]: fail('HOLD_COMMIT_UNVERIFIED')
    receipt = {**intent, 'kind': 'recovery-hold-prepared', 'newTree': tree_sha, 'preparedCommit': sha,
               'intentSha256': digest(encoded(intent)), 'refUpdateOutcome': 'unknown-until-final-readback'}
    write(output, 'prepared.json', receipt)
    return {'status': 'prepared', 'preparedCommit': sha}


def apply(output, *, api=None, env=None):
    env = os.environ if env is None else env; run_id = environment(env); api = api or ActionsAPI(env)
    output = Path(output)
    if (output / 'receipt.json').exists(): fail('HOLD_REF_REPLAY_FORBIDDEN')
    intent = read(output, 'intent.json')
    if (intent.get('workflowRunId') != run_id or intent.get('workflowRunAttempt') != 1
            or intent.get('workflowSourceCommit') != env['GITHUB_SHA'] or intent.get('path') != AUDIT_PATH): fail('HOLD_RECEIPT_INVALID')
    if (output / 'result.json').exists():
        result = read(output, 'result.json')
        if result.get('status') != 'already-verified' or head(api) != result.get('commit'): fail('HOLD_MAIN_CHANGED')
        raw = audit_blob(api, entry(api, commit(api, result['commit'])['tree']['sha'], AUDIT_PATH))
        if transformed(raw)[1] or git_blob(raw) != intent.get('oldBlob'): fail('HOLD_RECEIPT_INVALID')
        return result
    prepared = read(output, 'prepared.json')
    expected = {**intent, 'kind': 'recovery-hold-prepared', 'newTree': prepared.get('newTree'),
                'preparedCommit': prepared.get('preparedCommit'), 'intentSha256': digest(encoded(intent)),
                'refUpdateOutcome': 'unknown-until-final-readback'}
    if prepared != expected or any(not SHA.fullmatch(str(prepared.get(key))) for key in ('parentCommit', 'oldTree', 'newTree', 'preparedCommit', 'oldBlob', 'newBlob', 'progressBlob')):
        fail('HOLD_RECEIPT_INVALID')
    metadata_evidence(api, env)
    actual = commit(api, prepared['preparedCommit'])
    if (commit(api, prepared['parentCommit'])['tree']['sha'] != prepared['oldTree']
            or actual['tree']['sha'] != prepared['newTree'] or [p.get('sha') for p in actual.get('parents', [])] != [prepared['parentCommit']]
            or single_path_tree(api, prepared['oldTree'], prepared['newTree']) != (prepared['oldBlob'], prepared['newBlob'])
            or entry(api, prepared['newTree'], PROGRESS_PATH) != prepared['progressBlob']): fail('HOLD_RECEIPT_INVALID')
    old = audit_blob(api, prepared['oldBlob']); new = audit_blob(api, prepared['newBlob'])
    if transformed(old)[0] != new or digest(new) != prepared['newAuditSha256'] or digest(old) != prepared['oldAuditSha256']:
        fail('HOLD_RECEIPT_INVALID')
    if head(api) != prepared['parentCommit']: fail('HOLD_MAIN_CHANGED')
    receipt = {'schemaVersion': 1, 'status': 'unknown', 'workflowRunId': run_id, 'parentCommit': prepared['parentCommit'],
               'preparedCommit': prepared['preparedCommit'], 'oldBlob': prepared['oldBlob'], 'newBlob': prepared['newBlob'],
               'preparedSha256': digest(encoded(prepared)), 'refRequestCount': 1, 'force': False,
               'attemptedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'externalSourceRequests': 0}
    write(output, 'receipt.json', receipt)
    try: api.request('PATCH', 'git/refs/heads/main', {'sha': prepared['preparedCommit'], 'force': False})
    except Exception: pass  # The response cannot authorize a second ref request.
    try:
        observed = head(api)
        if observed == prepared['preparedCommit'] and entry(api, commit(api, observed)['tree']['sha'], AUDIT_PATH) == prepared['newBlob']:
            receipt.update(status='verified', observedHead=observed, verifiedAt=dt.datetime.now(dt.timezone.utc).isoformat())
        else:
            receipt.update(observedHead=observed, errorCode='HOLD_REF_OUTCOME_UNCONFIRMED')
    except Exception:
        receipt['errorCode'] = 'HOLD_REF_READBACK_UNAVAILABLE'
    write(output, 'receipt.json', receipt)
    result = {'schemaVersion': 1, 'status': receipt['status'], 'newRefRequests': 1, 'externalSourceRequests': 0,
              'fundDataCompletenessVerified': False, 'preparedCommit': prepared['preparedCommit']}
    write(output, 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument('mode', choices=['prepare', 'apply', 'validate'])
    parser.add_argument('--data'); parser.add_argument('--data-commit'); parser.add_argument('--output', default='.cache/recovery-hold')
    args = parser.parse_args(argv)
    try:
        if Path(args.output).resolve() != (Path.cwd() / '.cache/recovery-hold').resolve(): fail('HOLD_OUTPUT_INVALID')
        if args.mode == 'validate': validate_output(args.output); print('{"status":"validated"}'); return 0
        result = prepare(args.data, args.data_commit, args.output) if args.mode == 'prepare' else apply(args.output)
        validate_output(args.output)
        print(json.dumps({'status': result['status'], 'externalSourceRequests': 0}))
        return 0 if result['status'] in {'prepared', 'already-verified', 'verified'} else 1
    except Exception as error:
        code = str(error) if isinstance(error, HoldError) else 'HOLD_EXECUTION_FAILED'
        if not re.fullmatch(r'HOLD_[A-Z_]{1,64}', code): code = 'HOLD_EXECUTION_FAILED'
        if Path(args.output).resolve() == (Path.cwd() / '.cache/recovery-hold').resolve():
            try: write(args.output, 'error.json', {'schemaVersion': 1, 'status': 'failed', 'errorCode': code})
            except Exception: pass
        print(json.dumps({'status': 'failed', 'errorCode': code})); return 1


if __name__ == '__main__': raise SystemExit(main())
