"""Publish immutable completed traversal artifacts without collecting again."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
import zipfile

from publication_cache import read_bounded_command
from publication_guard import inspect_content
from recover_batches import parse, encoded, RecoveryError

REPOSITORY = 'yt-feng/record-lab'
WORKFLOW = '.github/workflows/resume-traversal.yml'
QUARTERS = {'2026Q2', '2026Q3'}
MAX_ARCHIVE = 64_000_000
MAX_EXPANDED = 128_000_000


def fail(code):
    raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class ActionsAPI:
    def __init__(self):
        self.deadline = time.monotonic() + 900

    def get(self, suffix, binary=False):
        if not re.fullmatch(r'actions/(?:runs/\d+(?:/(?:artifacts|jobs)\?per_page=100)?|artifacts/\d+/zip)', suffix):
            fail('RESUME_ENDPOINT_INVALID')
        try:
            raw = read_bounded_command(['gh', 'api', '--hostname', 'github.com',
                f'repos/{REPOSITORY}/{suffix}'], MAX_ARCHIVE if binary else 2_000_000,
                min(self.deadline, time.monotonic() + 90))
            return raw if binary else parse(raw)
        except Exception:
            fail('RESUME_API_UNAVAILABLE')


def verify_run(run, run_id, source_commit, env):
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_WORKFLOW_REF') != f'{REPOSITORY}/{WORKFLOW}@refs/heads/main'
            or not re.fullmatch(r'[1-9][0-9]{0,19}', str(run_id))
            or not re.fullmatch(r'[a-f0-9]{40}', source_commit)):
        fail('RESUME_IDENTITY_INVALID')
    repo, head = run.get('repository', {}), run.get('head_repository', {})
    if (repo.get('full_name') != REPOSITORY or head.get('full_name') != REPOSITORY
            or type(repo.get('id')) is not int or repo['id'] != head.get('id')
            or run.get('id') != int(run_id) or run.get('head_sha') != source_commit
            or run.get('head_branch') != 'main' or run.get('path') != '.github/workflows/traverse.yml'
            or run.get('run_attempt') != 1 or run.get('status') != 'completed'
            or run.get('conclusion') not in {'failure', 'cancelled', 'timed_out'}):
        fail('RESUME_ORIGINAL_RUN_INVALID')


def members(raw, metadata, run, allowed):
    binding = metadata.get('workflow_run', {})
    if (metadata.get('expired') is not False or type(metadata.get('id')) is not int
            or binding.get('id') != run['id'] or binding.get('head_sha') != run['head_sha']
            or binding.get('repository_id') != run['repository']['id']
            or binding.get('head_repository_id') != run['repository']['id']
            or binding.get('head_branch') != 'main' or not 0 < len(raw) <= MAX_ARCHIVE
            or metadata.get('size_in_bytes') != len(raw) or metadata.get('digest') != 'sha256:' + digest(raw)):
        fail('RESUME_ARCHIVE_IDENTITY_INVALID')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (len(entries) > 1000 or len({x.filename for x in entries}) != len(entries)
                    or sum(x.file_size for x in entries) > MAX_EXPANDED):
                fail('RESUME_ARCHIVE_INVALID')
            result = {}
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if (path.is_absolute() or '..' in path.parts or '\\' in name
                        or entry.flag_bits & 1 or entry.external_attr >> 16 & 0o170000 == 0o120000):
                    fail('RESUME_ARCHIVE_INVALID')
                if entry.is_dir(): continue
                if (not allowed(name) or entry.file_size > 25_000_000
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    fail('RESUME_ARCHIVE_INVALID')
                content = archive.read(entry)
                if inspect_content(content): fail('RESUME_PUBLICATION_REJECTED')
                parse(content)
                result[name] = content
            return result
    except (ValueError, RecoveryError): raise
    except Exception: fail('RESUME_ARCHIVE_INVALID')


def verify_matrix(plan, campaign):
    matrix = parse(plan.get('matrix.json', b'null'))
    selection = parse(campaign.get('selection.json', b'null'))
    if (not isinstance(matrix, list) or not 1 <= len(matrix) <= 32 or not isinstance(selection, dict)
            or not isinstance(selection.get('quarters'), list) or not selection['quarters']
            or len(set(selection['quarters'])) != len(selection['quarters'])
            or not set(selection['quarters']).issubset(QUARTERS)):
        fail('RESUME_PLAN_INVALID')
    seen = set()
    for row in matrix:
        index = row.get('id') if isinstance(row, dict) else None
        if (not isinstance(index, str) or not re.fullmatch(r'\d{1,2}', index) or index in seen
                or row.get('config') != f'batch-{index}.json' or row.get('quarter') not in selection['quarters']):
            fail('RESUME_PLAN_INVALID')
        config = parse(plan.get(row['config'], b'null'))
        if (not isinstance(config, dict) or config.get('quarter') != row['quarter']
                or config.get('traversal', {}).get('batchId') != index):
            fail('RESUME_PLAN_INVALID')
        seen.add(index)
    if set(plan) != {'matrix.json', 'meta.json', *(row['config'] for row in matrix)}:
        fail('RESUME_PLAN_INVALID')
    return matrix


def stage(root, files):
    root = Path(root)
    for name, raw in files.items():
        target = root / name
        if target.is_symlink() or any(p.is_symlink() for p in target.parents): fail('RESUME_PATH_INVALID')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def source_environment(env, run):
    # Only the merge subprocess uses the producer identity. The current
    # workflow, publication scanner and persistence keep their real identity.
    return {**env, 'GITHUB_RUN_ID': str(run['id']), 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_SHA': run['head_sha'], 'CAMPAIGN_MARKET_ACCOUNTING': 'required'}


def verify_persisted(run_id, source_commit, *, root='.', execute=None):
    if (not re.fullmatch(r'[1-9][0-9]{0,19}', str(run_id))
            or not re.fullmatch(r'[a-f0-9]{40}', source_commit)):
        fail('RESUME_IDENTITY_INVALID')
    runner = execute or subprocess.run
    def git(*args):
        result = runner(['git', *args], cwd=root, capture_output=True, timeout=120)
        if result.returncode: fail('RESUME_REMOTE_READBACK_FAILED')
        return result.stdout
    git('fetch', '--no-tags', 'origin', 'refs/heads/main')
    git('merge-base', '--is-ancestor', 'HEAD', 'FETCH_HEAD')
    git('diff', '--exit-code', 'HEAD', 'FETCH_HEAD', '--', 'web/data')
    path = f'web/data/traversal-resumptions/{run_id}-1.json'
    remote = git('show', 'FETCH_HEAD:' + path)
    if remote != (Path(root) / path).read_bytes(): fail('RESUME_REMOTE_READBACK_FAILED')
    marker = parse(remote)
    if (marker.get('runId') != str(run_id) or marker.get('sourceCommit') != source_commit
            or marker.get('runAttempt') != 1 or marker.get('status') != 'merged'):
        fail('RESUME_REMOTE_READBACK_FAILED')
    return {'status': 'remote-verified', 'runId': str(run_id)}


def resume(run_id, source_commit, *, root='.', api=None, env=None, execute=None):
    root = Path(root)
    env = dict(os.environ if env is None else env)
    api = api or ActionsAPI()
    run = api.get(f'actions/runs/{run_id}')
    verify_run(run, run_id, source_commit, env)
    marker = root / 'web/data/traversal-resumptions' / f'{run_id}-1.json'
    identity = {'schemaVersion': 1, 'runId': str(run_id), 'runAttempt': 1, 'sourceCommit': source_commit}
    if marker.exists():
        previous = parse(marker.read_bytes())
        if (not isinstance(previous, dict) or any(previous.get(k) != v for k, v in identity.items())
                or previous.get('status') != 'merged' or not isinstance(previous.get('artifacts'), list)):
            fail('RESUME_MARKER_INVALID')
        return {**identity, 'status': 'already-published', 'sourceRequests': 0, 'marketDataRequests': 0}
    listing = api.get(f'actions/runs/{run_id}/artifacts?per_page=100')
    artifacts = listing.get('artifacts')
    if (not isinstance(artifacts, list) or len(artifacts) > 100
            or listing.get('total_count') != len(artifacts)
            or len({x.get('name') for x in artifacts}) != len(artifacts)):
        fail('RESUME_ARTIFACT_LIST_INVALID')
    by_name = {x['name']: x for x in artifacts}
    proofs = []

    def download(name, allowed):
        metadata = by_name.get(name)
        if metadata is None: fail('RESUME_ARTIFACT_MISSING')
        raw = api.get(f"actions/artifacts/{metadata['id']}/zip", binary=True)
        result = members(raw, metadata, run, allowed)
        proofs.append({'id': metadata['id'], 'name': name, 'sha256': digest(raw),
                       'files': {key: digest(value) for key, value in sorted(result.items())}})
        return result

    plan = download('plan', lambda p: bool(re.fullmatch(r'(?:matrix|meta|batch-\d{1,2})\.json', p)))
    campaign = download('campaign', lambda p: bool(re.fullmatch(
        r'(?:(?:selection|2026Q[23]|(?:catalogs|locators)/2026Q[23])\.json|'
        r'catalogs/2026Q[23]-identities/part-[a-f0-9]{64}\.json)', p)))
    matrix = verify_matrix(plan, campaign)
    jobs = api.get(f'actions/runs/{run_id}/jobs?per_page=100')
    job_rows = jobs.get('jobs')
    if not isinstance(job_rows, list) or jobs.get('total_count') != len(job_rows): fail('RESUME_JOBS_INVALID')
    for row in matrix:
        name = f"batch ({row['id']}, {row['config']}, {row['quarter']})"
        matching = [j for j in job_rows if j.get('name') == name]
        if len(matching) != 1 or matching[0].get('conclusion') != 'success': fail('RESUME_BATCH_NOT_VERIFIED')
    directory = download('directory', lambda p: p == 'catalog.json')
    if set(directory) != {'catalog.json'}: fail('RESUME_DIRECTORY_INVALID')
    # The frozen campaign contains the exact quarter denominators. Avoid
    # regressing the global directory if another publisher has newer evidence.
    current_path = root / 'web/data/catalog.json'
    if current_path.exists():
        current, restored = parse(current_path.read_bytes()), parse(directory['catalog.json'])
        old_codes = {row.get('code') for row in current.get('funds', [])}
        new_codes = {row.get('code') for row in restored.get('funds', [])}
        if str(current.get('asOf', '')) > str(restored.get('asOf', '')) or not old_codes.issubset(new_codes):
            directory = {}
    stage(root / '.cache/plan', plan)
    stage(root / '.cache/campaign', campaign)
    for row in matrix:
        name = f"part-{row['id']}"
        part = download(name, lambda p: p in {'part.json', 'status.json'})
        if set(part) != {'part.json', 'status.json'}: fail('RESUME_PART_INVALID')
        stage(root / '.cache/parts' / name, part)
        name = f"request-accounting-{run_id}-1-{row['id']}"
        receipt = download(name, lambda p: p == 'receipts.json')
        if set(receipt) != {'receipts.json'}: fail('RESUME_RECEIPT_INVALID')
        stage(root / '.cache/request-accounting' / name, receipt)
    stage(root / 'web/data', directory)
    print(json.dumps({'stage': 'RESTORED_VERIFIED_ARTIFACTS', 'batchCount': len(matrix),
                      'artifactCount': len(proofs), 'sourceRequests': 0, 'marketDataRequests': 0}), flush=True)
    # Existing campaign validators recheck the catalog locks, status/snapshot
    # binding and every receipt before admitting these original bytes.
    runner = execute or subprocess.run
    try:
        runner([sys.executable, 'scripts/campaign.py', 'publish'], cwd=root,
               env=source_environment(env, run), check=True)
    except subprocess.CalledProcessError:
        fail('RESUME_MERGE_FAILED')
    result = {**identity, 'status': 'merged', 'artifacts': proofs,
              'sourceRequests': 0, 'marketDataRequests': 0, 'modelRequests': 0}
    raw = encoded(result)
    if inspect_content(raw): fail('RESUME_PUBLICATION_REJECTED')
    stage(marker.parent, {marker.name: raw})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--verify-persisted', action='store_true')
    args = parser.parse_args()
    if args.verify_persisted:
        print(json.dumps(verify_persisted(args.run_id, args.source_commit)))
        return
    result = resume(args.run_id, args.source_commit)
    stage('.cache/resume-traversal', {'result.json': encoded(result)})
    print(json.dumps({key: result[key] for key in ('runId', 'status', 'sourceRequests', 'marketDataRequests')}))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        code = str(error)
        if not re.fullmatch(r'RESUME_[A-Z_]+', code): code = 'RESUME_FAILED'
        print(json.dumps({'status': 'failed', 'errorCode': code}))
        raise SystemExit(1) from None
