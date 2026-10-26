"""Restore one explicitly named, API-bound conflict artifact, never latest.

Only Actions may fetch GitHub. A bounded workflow metadata check rejects an
already consumed parent; the artifact cannot reset any digest's attempt budget.
The sole legacy bootstrap is the independently audited first ledger run.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import zipfile

from publication_cache import read_bounded_command
from price_conflicts import (ConflictError, PHASES, MAX_BYTES, LEDGER_BOOTSTRAP, normalize_registry, normalize_ledger,
                             normalize_restored_state, union_ledgers, encoded, validate_plan, merge_receipts, write_json)

REPOSITORY = 'yt-feng/record-lab'
WORKFLOW = '.github/workflows/recheck-price-conflicts.yml'
ARTIFACT_NAME = 'price-conflict-ledger-verification'
MAX_ARCHIVE, MAX_TOTAL, MAX_METADATA = 32_000_000, 100_000_000, 2_000_000
BOOTSTRAP = LEDGER_BOOTSTRAP
SHA = re.compile(r'[0-9a-f]{40}')
CONCLUSIONS = {'success', 'failure', 'cancelled', 'timed_out'}


def fail(code):
    raise ConflictError(code)


def positive(value):
    return type(value) is int and value > 0


def parse_json(raw):
    try:
        def constant(_): raise ValueError()
        return json.loads(raw, parse_constant=constant)
    except (ValueError, UnicodeError, TypeError): fail('CONFLICT_RESTORE_JSON_INVALID')


class ActionsAPI:
    def __init__(self):
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
                or not os.environ.get('GH_TOKEN')): fail('CONFLICT_RESTORE_REQUIRES_ACTIONS')
        self.deadline = time.monotonic() + 120

    def get(self, suffix, binary=False):
        if not re.fullmatch(r'actions/(?:runs/\d+(?:/artifacts\?per_page=100)?|artifacts/\d+/zip|workflows/\d+/runs\?per_page=100)', suffix):
            fail('CONFLICT_RESTORE_ENDPOINT_INVALID')
        try:
            raw = read_bounded_command(['gh', 'api', '--hostname', 'github.com', f'repos/{REPOSITORY}/{suffix}'],
                                       MAX_ARCHIVE if binary else MAX_METADATA, self.deadline)
            return raw if binary else parse_json(raw)
        except ConflictError: raise
        except Exception: fail('CONFLICT_RESTORE_API_UNAVAILABLE')


def run_identity(run, *, run_id, source_commit, current=False):
    if (not isinstance(run, dict) or run.get('id') != run_id or not positive(run_id)
            or not isinstance(source_commit, str) or not SHA.fullmatch(source_commit) or run.get('head_sha') != source_commit
            or run.get('path') != WORKFLOW or run.get('head_branch') != 'main' or run.get('event') != 'workflow_dispatch'
            or not positive(run.get('workflow_id')) or not positive(run.get('run_number')) or not positive(run.get('run_attempt'))):
        fail('CONFLICT_RESTORE_RUN_MISMATCH')
    repository, head_repository = run.get('repository'), run.get('head_repository')
    if (not isinstance(repository, dict) or not isinstance(head_repository, dict)
            or repository.get('full_name') != REPOSITORY or head_repository.get('full_name') != REPOSITORY
            or not positive(repository.get('id')) or repository['id'] != head_repository.get('id')):
        fail('CONFLICT_RESTORE_REPOSITORY_MISMATCH')
    if current:
        if run.get('status') != 'in_progress' or run['run_attempt'] != 1: fail('CONFLICT_RESTORE_CURRENT_ATTEMPT_INVALID')
    elif run.get('status') != 'completed' or run.get('conclusion') not in CONCLUSIONS:
        fail('CONFLICT_RESTORE_PRIOR_NOT_COMPLETED')
    return run


def validate_lineage(prior, current, listing):
    """A metadata guard only: never auto-select or recover a newer artifact."""
    if prior['workflow_id'] != current['workflow_id'] or prior['run_number'] >= current['run_number']:
        fail('CONFLICT_RESTORE_LINEAGE_INVALID')
    rows = listing.get('workflow_runs') if isinstance(listing, dict) else None
    total = listing.get('total_count') if isinstance(listing, dict) else None
    if not isinstance(rows, list) or len(rows) > 100 or type(total) is not int or total < len(rows): fail('CONFLICT_RESTORE_LINEAGE_UNVERIFIED')
    by_id = {}
    for row in rows:
        if (not isinstance(row, dict) or not positive(row.get('id')) or row['id'] in by_id or not positive(row.get('run_number'))
                or row.get('workflow_id') != current['workflow_id']): fail('CONFLICT_RESTORE_LINEAGE_UNVERIFIED')
        by_id[row['id']] = row
    if prior['id'] not in by_id or current['id'] not in by_id: fail('CONFLICT_RESTORE_LINEAGE_UNVERIFIED')
    for full in (prior, current):
        if any(by_id[full['id']].get(key) != full.get(key) for key in ('run_number', 'run_attempt', 'head_sha', 'status', 'conclusion')):
            fail('CONFLICT_RESTORE_LINEAGE_UNVERIFIED')
    between = [row for row in rows if prior['run_number'] < row['run_number'] < current['run_number']]
    if between: fail('CONFLICT_RESTORE_STALE_PRIOR')
    # Workflow run_number is monotonic. Require the adjacent run, rather than
    # treating a missing/deleted/hidden execution as proof that nothing ran.
    if current['run_number'] != prior['run_number'] + 1: fail('CONFLICT_RESTORE_LINEAGE_UNVERIFIED')


def artifact_identity(artifact, prior):
    if not isinstance(artifact, dict): fail('CONFLICT_RESTORE_ARTIFACT_INVALID')
    identity = artifact.get('workflow_run')
    expected_name = f"{ARTIFACT_NAME}-{prior['id']}-{prior['run_attempt']}"
    legacy = prior['id'] == BOOTSTRAP['runId'] and prior['run_attempt'] == BOOTSTRAP['runAttempt'] and artifact.get('name') == ARTIFACT_NAME
    if (not positive(artifact.get('id')) or artifact.get('expired') is not False or not isinstance(identity, dict)
            or artifact.get('name') != expected_name and not legacy
            or identity.get('id') != prior['id'] or identity.get('repository_id') != prior['repository']['id']
            or identity.get('head_repository_id') != prior['repository']['id'] or identity.get('head_branch') != 'main'
            or identity.get('head_sha') != prior['head_sha'] or not positive(artifact.get('size_in_bytes'))
            or artifact['size_in_bytes'] > MAX_ARCHIVE or not re.fullmatch(r'sha256:[a-f0-9]{64}', str(artifact.get('digest')))):
        fail('CONFLICT_RESTORE_ARTIFACT_INVALID')
    if legacy and (artifact['id'] != BOOTSTRAP['artifactId'] or artifact['digest'] != 'sha256:' + BOOTSTRAP['archiveSha256']
                   or prior['head_sha'] != BOOTSTRAP['sourceCommit']): fail('CONFLICT_RESTORE_BOOTSTRAP_MISMATCH')
    return legacy


def archive_files(raw, artifact):
    if not isinstance(raw, bytes) or len(raw) != artifact['size_in_bytes'] or 'sha256:' + hashlib.sha256(raw).hexdigest() != artifact['digest']:
        fail('CONFLICT_RESTORE_ARCHIVE_MISMATCH')
    allowed = {'provenance.json', 'result.json'} | {f'{quarter}/{name}.json' for quarter in PHASES
                                                   for name in ('registry', 'plan', 'ledger', 'input-ledger', 'receipts', 'execution')}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > len(allowed) or len({item.filename for item in entries}) != len(entries): fail('CONFLICT_RESTORE_ARCHIVE_INVALID')
            if sum(item.file_size for item in entries) > MAX_TOTAL: fail('CONFLICT_RESTORE_ARCHIVE_INVALID')
            result = {}
            for item in entries:
                if (item.filename not in allowed or item.is_dir() or item.file_size > MAX_BYTES
                        or item.external_attr >> 16 & 0o170000 == 0o120000 or item.flag_bits & 1
                        or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    fail('CONFLICT_RESTORE_ARCHIVE_INVALID')
                result[item.filename] = parse_json(archive.read(item))
            return result
    except ConflictError: raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError): fail('CONFLICT_RESTORE_ARCHIVE_INVALID')


def restore_archive(artifact, prior, raw, *, prior_data_commit, current, data_commit, now=None):
    legacy = artifact_identity(artifact, prior)
    if not SHA.fullmatch(str(prior_data_commit)) or not SHA.fullmatch(str(data_commit)): fail('CONFLICT_RESTORE_DATA_MISMATCH')
    if legacy and prior_data_commit != BOOTSTRAP['dataCommit']: fail('CONFLICT_RESTORE_BOOTSTRAP_MISMATCH')
    files = archive_files(raw, artifact)
    provenance = files.get('provenance.json')
    if legacy:
        if provenance is not None or prior.get('conclusion') != 'success' or files.get('result.json', {}).get('status') != 'complete':
            fail('CONFLICT_RESTORE_BOOTSTRAP_MISMATCH')
    else:
        if (not isinstance(provenance, dict) or set(provenance) != {'schemaVersion', 'kind', 'repository', 'workflowPath', 'runId',
                                                                 'runAttempt', 'sourceCommit', 'dataCommit', 'restoredFrom', 'inputLedgerHashes', 'bootstrap'}
                or provenance.get('schemaVersion') != 1 or provenance.get('kind') != 'price-conflict-provenance'
                or provenance.get('repository') != REPOSITORY or provenance.get('workflowPath') != WORKFLOW
                or provenance.get('runId') != prior['id'] or provenance.get('runAttempt') != prior['run_attempt']
                or provenance.get('sourceCommit') != prior['head_sha'] or provenance.get('dataCommit') != prior_data_commit
                or provenance.get('bootstrap') != BOOTSTRAP
                or not isinstance(provenance.get('inputLedgerHashes'), dict) or set(provenance['inputLedgerHashes']) != set(PHASES)
                or any(not re.fullmatch(r'[a-f0-9]{64}', str(value)) for value in provenance['inputLedgerHashes'].values())):
            fail('CONFLICT_RESTORE_PROVENANCE_MISMATCH')
        # Reuse the strict reference shape validator for the prior run's parent.
        normalize_restored_state({'schemaVersion': 1, 'kind': 'price-conflict-restored-state', 'repository': REPOSITORY,
            'workflowPath': WORKFLOW, 'currentRun': {'runId': prior['id'], 'runAttempt': prior['run_attempt'], 'sourceCommit': prior['head_sha']},
            'currentDataCommit': prior_data_commit, 'restoredFrom': provenance['restoredFrom'],
            'bootstrap': BOOTSTRAP, 'ledgers': {quarter: normalize_ledger(None, quarter) for quarter in PHASES}}, now)
    ledgers = {}
    for quarter in PHASES:
        if not isinstance(files.get(f'{quarter}/ledger.json'), dict): fail('CONFLICT_RESTORE_LEDGER_MISSING')
        ledger = normalize_ledger(files[f'{quarter}/ledger.json'], quarter, now)
        inherited = None
        if not legacy:
            if not isinstance(files.get(f'{quarter}/input-ledger.json'), dict): fail('CONFLICT_RESTORE_INPUT_LEDGER_MISSING')
            inherited = normalize_ledger(files[f'{quarter}/input-ledger.json'], quarter, now)
            if hashlib.sha256(encoded(inherited)).hexdigest() != provenance['inputLedgerHashes'][quarter]:
                fail('CONFLICT_RESTORE_INPUT_LEDGER_MISMATCH')
        registry, plan, receipts = (files.get(f'{quarter}/{name}.json') for name in ('registry', 'plan', 'receipts'))
        if registry is not None:
            registry = normalize_registry(registry, now)
            if registry['quarter'] != quarter or registry['dataCommit'] != prior_data_commit: fail('CONFLICT_RESTORE_DATA_MISMATCH')
        if plan is not None:
            if registry is None: fail('CONFLICT_RESTORE_CHECKPOINT_INVALID')
            validate_plan(plan, registry, now)
        if receipts is not None:
            if plan is None: fail('CONFLICT_RESTORE_CHECKPOINT_INVALID')
            ledger = merge_receipts(registry, ledger, plan, receipts, now=now)
        if inherited is not None and union_ledgers(inherited, ledger, quarter, now) != ledger:
            fail('CONFLICT_RESTORE_HISTORY_LOST')
        if legacy and any(value is None for value in (registry, plan, receipts)): fail('CONFLICT_RESTORE_BOOTSTRAP_MISMATCH')
        ledgers[quarter] = ledger
    state = {'schemaVersion': 1, 'kind': 'price-conflict-restored-state', 'repository': REPOSITORY, 'workflowPath': WORKFLOW,
             'currentRun': {'runId': current['id'], 'runAttempt': current['run_attempt'], 'sourceCommit': current['head_sha']},
             'currentDataCommit': data_commit, 'restoredFrom': {'runId': prior['id'], 'runAttempt': prior['run_attempt'],
             'sourceCommit': prior['head_sha'], 'dataCommit': prior_data_commit, 'artifactId': artifact['id'],
             'archiveSha256': artifact['digest'].split(':')[1], 'conclusion': prior['conclusion']}, 'bootstrap': BOOTSTRAP, 'ledgers': ledgers}
    return normalize_restored_state(state, now)


def restore(*, prior_run_id, prior_source_commit, prior_data_commit, data_commit, env=None, api=None, now=None):
    env = os.environ if env is None else env
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_WORKFLOW_REF') != f'{REPOSITORY}/{WORKFLOW}@refs/heads/main'):
        fail('CONFLICT_RESTORE_REQUIRES_ACTIONS')
    try: current_id, attempt = int(env.get('GITHUB_RUN_ID', '')), int(env.get('GITHUB_RUN_ATTEMPT', ''))
    except ValueError: fail('CONFLICT_RESTORE_CURRENT_ATTEMPT_INVALID')
    if attempt != 1 or not positive(prior_run_id) or current_id == prior_run_id: fail('CONFLICT_RESTORE_CURRENT_ATTEMPT_INVALID')
    if any(not SHA.fullmatch(str(value)) for value in (prior_source_commit, prior_data_commit, data_commit, env.get('GITHUB_SHA'))):
        fail('CONFLICT_RESTORE_IDENTITY_REQUIRED')
    api = api or ActionsAPI()
    current = run_identity(api.get(f'actions/runs/{current_id}'), run_id=current_id, source_commit=env['GITHUB_SHA'], current=True)
    prior = run_identity(api.get(f'actions/runs/{prior_run_id}'), run_id=prior_run_id, source_commit=prior_source_commit)
    validate_lineage(prior, current, api.get(f"actions/workflows/{current['workflow_id']}/runs?per_page=100"))
    listing = api.get(f'actions/runs/{prior_run_id}/artifacts?per_page=100')
    artifacts = listing.get('artifacts') if isinstance(listing, dict) else None
    if (not isinstance(artifacts, list) or len(artifacts) > 100 or listing.get('total_count') != len(artifacts)):
        fail('CONFLICT_RESTORE_ARTIFACT_LIST_INVALID')
    candidates = [value for value in artifacts if isinstance(value, dict) and value.get('name') in
                  (ARTIFACT_NAME if prior_run_id == BOOTSTRAP['runId'] else '', f'{ARTIFACT_NAME}-{prior_run_id}-{prior["run_attempt"]}')]
    if len(candidates) != 1: fail('CONFLICT_RESTORE_ARTIFACT_MISSING_OR_AMBIGUOUS')
    artifact = candidates[0]; artifact_identity(artifact, prior)
    raw = api.get(f"actions/artifacts/{artifact['id']}/zip", binary=True)
    return restore_archive(artifact, prior, raw, prior_data_commit=prior_data_commit, current=current, data_commit=data_commit, now=now)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--prior-run-id', required=True, type=int)
    parser.add_argument('--prior-source-commit', required=True)
    parser.add_argument('--prior-data-commit', required=True)
    parser.add_argument('--data-commit', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        path = Path(args.output)
        if path.exists(): fail('CONFLICT_RESTORE_OUTPUT_EXISTS')
        state = restore(prior_run_id=args.prior_run_id, prior_source_commit=args.prior_source_commit,
                        prior_data_commit=args.prior_data_commit, data_commit=args.data_commit)
        write_json(state, path)
        print(json.dumps({'status': 'restored', 'priorRunId': args.prior_run_id,
                          'roundCount': sum(len(history['rounds']) for ledger in state['ledgers'].values() for entry in ledger['entries'] for history in entry['digests']),
                          'productionDataModified': False}))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ConflictError) and re.fullmatch(r'CONFLICT_[A-Z_]+', str(error)) else 'CONFLICT_RESTORE_FAILED'
        print(json.dumps({'status': 'failed', 'errorCode': code, 'productionDataModified': False}))
        return 1


if __name__ == '__main__': raise SystemExit(main())
