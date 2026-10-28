"""Admit two verified source-recovery parts; never replay the original batch set.

GitHub metadata/artifact reads are the only network operations in this module.
This adapter neither clears the quarter hold nor invokes a data provider.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

from publication_cache import read_bounded_command
from publication_guard import inspect_content
import recover_batches as recovery
from traversal import (atomic_json, read_json, read_status_snapshot, valid_status,
                       reconcile_observations, merge_progress, load_retained_funds, catalog_records)
from campaign import deployed_continuation_state

WORKFLOW = '.github/workflows/admit-batch-recovery.yml'
QUARTER = '2026Q2'
PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
MAX_ARCHIVE = 1_000_000_000
MAX_FILE = 25_000_000
LEDGER = 'recovery-admissions.json'
ROOT = Path(__file__).resolve().parents[1]
EXECUTION_STAGE = 'START'


def stage(name):
    """Keep only a fixed, non-sensitive phase label for failure receipts."""
    global EXECUTION_STAGE
    if re.fullmatch(r'[A-Z0-9_]{1,48}', name):
        EXECUTION_STAGE = name


class AdmissionError(ValueError):
    pass


def fail(code):
    raise AdmissionError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''): hasher.update(block)
    return hasher.hexdigest()


def command(args, *, cwd=ROOT, timeout=180):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
        if result.returncode: fail('ADMISSION_COMMAND_FAILED')
        return result.stdout
    except AdmissionError: raise
    except Exception: fail('ADMISSION_COMMAND_FAILED')


def git(repo, *args):
    return command(['git', '-C', str(repo), *args], timeout=60).decode().strip()


def binding_for(base, run, source, artifact, archive):
    if (not recovery.SHA.fullmatch(str(base)) or not recovery.SHA.fullmatch(str(source))
            or type(run) is not int or run <= 0 or type(artifact) is not int or artifact <= 0
            or not recovery.HEX.fullmatch(str(archive))): fail('ADMISSION_BINDING_INVALID')
    return {'originalRunId': recovery.ORIGINAL_RUN, 'baseDataCommit': base, 'recoveryRunId': run,
            'recoveryRunAttempt': 1, 'recoverySourceCommit': source, 'artifactId': artifact,
            'archiveSha256': archive, 'quarter': QUARTER, 'batchIds': list(recovery.MISSING)}


class ActionsAPI:
    def __init__(self):
        if os.environ.get('GITHUB_ACTIONS') != 'true' or not os.environ.get('GH_TOKEN'):
            fail('ADMISSION_REQUIRES_ACTIONS')
        self.deadline = time.monotonic() + 600

    def get(self, endpoint):
        if not re.fullmatch(r'(?:commits/main|actions/runs/\d+(?:/(?:artifacts|jobs)\?per_page=100)?)', endpoint):
            fail('ADMISSION_ENDPOINT_INVALID')
        try:
            raw = read_bounded_command(['gh', 'api', '--hostname', 'github.com',
                f'repos/{recovery.REPOSITORY}/{endpoint}'], 2_000_000, min(self.deadline, time.monotonic() + 90))
            return recovery.parse(raw)
        except Exception: fail('ADMISSION_API_UNAVAILABLE')

    def download(self, artifact_id, output):
        if type(artifact_id) is not int or artifact_id <= 0: fail('ADMISSION_ENDPOINT_INVALID')
        # Stream to private disk; no archive or provider body is logged.
        process = subprocess.Popen(['gh', 'api', '--hostname', 'github.com',
            f'repos/{recovery.REPOSITORY}/actions/artifacts/{artifact_id}/zip'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        import threading
        timer = threading.Timer(max(0.01, min(240, self.deadline - time.monotonic())), process.kill)
        timer.start(); size = 0
        try:
            with Path(output).open('xb') as handle:
                while block := process.stdout.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_ARCHIVE: fail('ADMISSION_ARCHIVE_TOO_LARGE')
                    handle.write(block)
            if process.wait() != 0: fail('ADMISSION_API_UNAVAILABLE')
        finally:
            timer.cancel()
            if process.poll() is None: process.kill()
            process.wait()
            process.stdout.close()


def verified_metadata(api, binding):
    original = api.get(f'actions/runs/{recovery.ORIGINAL_RUN}')
    if (original.get('id') != recovery.ORIGINAL_RUN or original.get('head_sha') != recovery.ORIGINAL_SOURCE or original.get('status') != 'completed'
            or original.get('run_attempt') != 1 or original.get('path') != '.github/workflows/traverse.yml'):
        fail('ADMISSION_ORIGINAL_NOT_PERSISTED')
    jobs = api.get(f'actions/runs/{recovery.ORIGINAL_RUN}/jobs?per_page=100')
    rows = jobs.get('jobs')
    if not isinstance(rows, list) or len(rows) > 100 or jobs.get('total_count') != len(rows): fail('ADMISSION_ORIGINAL_NOT_PERSISTED')
    publishers = [row for row in rows if row.get('name') == 'publish']
    if len(publishers) != 1 or publishers[0].get('status') != 'completed': fail('ADMISSION_ORIGINAL_NOT_PERSISTED')
    publisher = publishers[0]; steps = publisher.get('steps')
    if not isinstance(steps, list) or not steps or len(steps) > 100: fail('ADMISSION_ORIGINAL_NOT_PERSISTED')
    for name in ('Merge source-linked results', 'Persist observations and checkpoint', 'Prepare compressed public site'):
        matching = [step for step in steps if step.get('name') == name]
        if len(matching) != 1 or matching[0].get('conclusion') != 'success': fail('ADMISSION_ORIGINAL_NOT_PERSISTED')
    # A disabled continuation workflow can fail after data was safely persisted.
    # This admits that exact terminal step only, never another failed data step.
    failed = [step for step in steps if step.get('conclusion') not in {'success', 'skipped'}]
    if publisher.get('conclusion') == 'success':
        if failed: fail('ADMISSION_ORIGINAL_NOT_PERSISTED')
    elif (publisher.get('conclusion') != 'failure' or len(failed) != 1
            or failed[0].get('name') != 'Continue unfinished traversal' or failed[0].get('conclusion') != 'failure'):
        fail('ADMISSION_ORIGINAL_NOT_PERSISTED')
    run = api.get(f"actions/runs/{binding['recoveryRunId']}")
    for value in (original, run):
        repo, head = value.get('repository', {}), value.get('head_repository', {})
        if (repo.get('full_name') != recovery.REPOSITORY or head.get('full_name') != recovery.REPOSITORY
                or type(repo.get('id')) is not int or repo['id'] != head.get('id') or value.get('head_branch') != 'main'):
            fail('ADMISSION_RUN_IDENTITY_INVALID')
    if (run.get('id') != binding['recoveryRunId'] or run.get('head_sha') != binding['recoverySourceCommit']
            or run.get('path') != recovery.WORKFLOW or run.get('event') != 'workflow_dispatch'
            or run.get('run_attempt') != 1 or run.get('run_number') != 1
            or run.get('status') != 'completed' or run.get('conclusion') != 'success'):
        fail('ADMISSION_RECOVERY_NOT_COMPLETE')
    listing = api.get(f"actions/runs/{binding['recoveryRunId']}/artifacts?per_page=100")
    artifacts = listing.get('artifacts')
    if not isinstance(artifacts, list) or len(artifacts) > 100 or listing.get('total_count') != len(artifacts): fail('ADMISSION_ARTIFACT_INVALID')
    selected = [item for item in artifacts if item.get('id') == binding['artifactId']]
    if len(selected) != 1: fail('ADMISSION_ARTIFACT_INVALID')
    artifact = selected[0]; owner = artifact.get('workflow_run', {})
    if (artifact.get('name') != f"batch-recovery-{binding['recoveryRunId']}-1" or artifact.get('expired') is not False
            or artifact.get('digest') != 'sha256:' + binding['archiveSha256']
            or type(artifact.get('size_in_bytes')) is not int or not 0 < artifact['size_in_bytes'] <= MAX_ARCHIVE
            or owner.get('id') != run['id'] or owner.get('head_sha') != binding['recoverySourceCommit']
            or owner.get('repository_id') != run['repository']['id'] or owner.get('head_repository_id') != run['repository']['id']
            or owner.get('head_branch') != 'main'): fail('ADMISSION_ARTIFACT_INVALID')
    return artifact


def extract_archive(archive, output, metadata, binding):
    if Path(archive).stat().st_size != metadata['size_in_bytes'] or file_sha(archive) != binding['archiveSha256']:
        fail('ADMISSION_ARCHIVE_DIGEST_MISMATCH')
    allowed = re.compile(r'(?:result|execution-error)\.json|original-parts/part-\d{1,2}/(?:part|status)\.json|'
                         r'original-plan/(?:matrix|meta|batch-\d{1,2})\.json|'
                         r'original-campaign/(?:selection|2026Q[23]|(?:catalogs|locators)/2026Q[23]|catalogs/2026Q[23]-identities/part-[a-f0-9]{64})\.json|'
                         r'recovered-(?:10|13)/(?:config|part|status|build-diagnostic|source-attempts)\.json')
    with zipfile.ZipFile(archive) as zipped:
        files = [row for row in zipped.infolist() if not row.is_dir()]
        if len(files) > 200 or len({row.filename for row in files}) != len(files) or sum(row.file_size for row in files) > MAX_ARCHIVE:
            fail('ADMISSION_ARCHIVE_INVALID')
        for row in files:
            if (not allowed.fullmatch(row.filename) or row.file_size > MAX_FILE or row.flag_bits & 1
                    or row.external_attr >> 16 & 0o170000 == 0o120000
                    or row.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                fail('ADMISSION_ARCHIVE_INVALID')
            if row.filename.startswith('original-parts/'): continue
            raw = zipped.read(row)
            if inspect_content(raw): fail('ADMISSION_PUBLICATION_REJECTED')
            recovery.parse(raw)
            path = Path(output) / row.filename; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    if (Path(output) / 'execution-error.json').exists(): fail('ADMISSION_RECOVERY_NOT_COMPLETE')


def load_ledger(quarter):
    path = Path(quarter) / LEDGER
    if path.is_symlink(): fail('ADMISSION_LEDGER_INVALID')
    if not path.exists(): return {'schemaVersion': 1, 'quarter': QUARTER, 'entries': []}
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE: fail('ADMISSION_LEDGER_INVALID')
        value = recovery.parse(path.read_bytes())
    except Exception: fail('ADMISSION_LEDGER_INVALID')
    if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'quarter', 'entries'}
            or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
            or value['quarter'] != QUARTER or not isinstance(value['entries'], list) or len(value['entries']) > 16):
        fail('ADMISSION_LEDGER_INVALID')
    seen = set()
    for row in value['entries']:
        if (not isinstance(row, dict) or set(row) != {'admissionDigest', 'binding', 'admittedAt', 'files',
                'requestedCodes', 'startedSourceCodes', 'originalMarketRequests', 'recoveryMarketRequests', 'holdPreserved'}):
            fail('ADMISSION_LEDGER_INVALID')
        binding = row['binding']
        if not isinstance(binding, dict): fail('ADMISSION_LEDGER_INVALID')
        try:
            expected = binding_for(binding.get('baseDataCommit'), binding.get('recoveryRunId'),
                                   binding.get('recoverySourceCommit'), binding.get('artifactId'), binding.get('archiveSha256'))
        except AdmissionError: fail('ADMISSION_LEDGER_INVALID')
        stamp = recovery.instant(row['admittedAt'])
        if (binding != expected or row['admissionDigest'] != digest(binding) or binding['recoveryRunId'] in seen
                or not stamp or stamp > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
                or row['originalMarketRequests'] != 'unknown' or type(row['recoveryMarketRequests']) is not int
                or row['recoveryMarketRequests'] != 0 or row['holdPreserved'] is not True): fail('ADMISSION_LEDGER_INVALID')
        seen.add(binding['recoveryRunId'])
        for field in ('requestedCodes', 'startedSourceCodes'):
            codes = row[field]
            if (not isinstance(codes, list) or len(codes) > 150 or any(not isinstance(code, str)
                    or not re.fullmatch(r'[0-9]{6}', code) for code in codes) or codes != sorted(set(codes))):
                fail('ADMISSION_LEDGER_INVALID')
        if not set(row['startedSourceCodes']) <= set(row['requestedCodes']): fail('ADMISSION_LEDGER_INVALID')
        files = row['files']; names = set()
        if not isinstance(files, list) or len(files) != 8: fail('ADMISSION_LEDGER_INVALID')
        for item in files:
            if (not isinstance(item, dict) or set(item) != {'batchId', 'file', 'sha256'}
                    or item['batchId'] not in recovery.MISSING
                    or item['file'] not in {'config.json', 'part.json', 'status.json', 'source-attempts.json'}
                    or not recovery.HEX.fullmatch(str(item['sha256']))): fail('ADMISSION_LEDGER_INVALID')
            names.add((item['batchId'], item['file']))
        if len(names) != 8: fail('ADMISSION_LEDGER_INVALID')
    return value


def check_runtime(repo, env):
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != recovery.REPOSITORY
            or env.get('GITHUB_WORKFLOW_REF') != f'{recovery.REPOSITORY}/{WORKFLOW}@refs/heads/main'):
        fail('ADMISSION_REQUIRES_ACTIONS')
    if git(repo, 'rev-parse', 'HEAD') != env.get('GITHUB_SHA'): fail('ADMISSION_CHECKOUT_INVALID')
    if git(repo, 'status', '--porcelain', '--untracked-files=all', '--', 'web/data'): fail('ADMISSION_DIRTY_DATA')


def check_base(repo, binding, env):
    check_runtime(repo, env)
    # Equal trees are required in addition to ancestry; source-only advancement is safe.
    command(['git', '-C', str(repo), 'merge-base', '--is-ancestor', binding['baseDataCommit'], 'HEAD'])
    if git(repo, 'rev-parse', binding['baseDataCommit'] + ':web/data') != git(repo, 'rev-parse', 'HEAD:web/data'):
        fail('ADMISSION_DATA_CHANGED')
    state = deployed_continuation_state(Path(repo) / 'web/data', QUARTER)
    hold = state['continuationHold']
    if (not hold or hold['sourceRunId'] != str(recovery.ORIGINAL_RUN) or set(hold['batchIds']) != set(recovery.MISSING)):
        fail('ADMISSION_HOLD_INVALID')
    return state


def verify_bundle(bundle, binding, repo):
    bundle = Path(bundle); manifest = read_json(bundle / 'result.json')
    if (manifest.get('originalRunId') != recovery.ORIGINAL_RUN or manifest.get('originalSourceCommit') != recovery.ORIGINAL_SOURCE
            or manifest.get('originalRunAttempt') != 1 or manifest.get('recoveryRunId') != binding['recoveryRunId']
            or manifest.get('sourceCommit') != binding['recoverySourceCommit'] or manifest.get('productionWrite') is not False
            or manifest.get('marketRequests') != 0 or manifest.get('originalMarketReceipts') != 'unknown'
            or manifest.get('status') not in {'recovered', 'partial'}): fail('ADMISSION_MANIFEST_INVALID')
    preserved = manifest.get('preserved')
    expected_preserved = {str(index) for index in range(32)} - set(recovery.MISSING)
    if (not isinstance(preserved, list) or len(preserved) != 30
            or any(not isinstance(row, dict) for row in preserved)
            or {row.get('batchId') for row in preserved} != expected_preserved): fail('ADMISSION_MANIFEST_INVALID')
    for row in preserved:
        if (set(row) != {'batchId', 'artifactId', 'archiveSha256', 'files'}
                or type(row['artifactId']) is not int or row['artifactId'] <= 0
                or not recovery.HEX.fullmatch(str(row['archiveSha256'])) or not isinstance(row['files'], list)
                or len(row['files']) != 2 or any(not isinstance(item, dict) for item in row['files'])
                or {item.get('name') for item in row['files']} != {'part.json', 'status.json'}): fail('ADMISSION_MANIFEST_INVALID')
        for item in row['files']:
            if (set(item) != {'name', 'bytes', 'sha256'} or type(item['bytes']) is not int
                    or not 0 < item['bytes'] <= MAX_FILE or not recovery.HEX.fullmatch(str(item['sha256']))):
                fail('ADMISSION_MANIFEST_INVALID')
    source_data = manifest.get('dataCommit')
    if not recovery.SHA.fullmatch(str(source_data)): fail('ADMISSION_DATA_CHANGED')
    command(['git', '-C', str(repo), 'merge-base', '--is-ancestor', source_data, 'HEAD'])
    if git(repo, 'rev-parse', source_data + ':web/data') != git(repo, 'rev-parse', binding['baseDataCommit'] + ':web/data'):
        fail('ADMISSION_DATA_CHANGED')
    frozen = manifest.get('frozenFiles')
    if not isinstance(frozen, list) or len(frozen) > 100: fail('ADMISSION_FROZEN_PLAN_INVALID')
    seen = set()
    for row in frozen:
        name = row.get('name')
        if (not isinstance(name, str) or not re.fullmatch(r'original-(?:plan|campaign)/[A-Za-z0-9_./-]+\.json', name)
                or '..' in name or name in seen): fail('ADMISSION_FROZEN_PLAN_INVALID')
        seen.add(name); file = bundle / name
        if not file.is_file() or file.stat().st_size != row.get('bytes') or file_sha(file) != row.get('sha256'):
            fail('ADMISSION_FROZEN_PLAN_INVALID')
    plans = {p.relative_to(bundle / 'original-plan').as_posix(): p.read_bytes() for p in (bundle / 'original-plan').rglob('*.json')}
    campaign = {p.relative_to(bundle / 'original-campaign').as_posix(): p.read_bytes() for p in (bundle / 'original-campaign').rglob('*.json')}
    if seen != {'original-plan/' + p for p in plans} | {'original-campaign/' + p for p in campaign}:
        fail('ADMISSION_FROZEN_PLAN_INVALID')
    original_configs = recovery.validate_plan(plans, campaign, frozen_root=bundle / 'original-campaign')
    progress = read_json(Path(repo) / 'web/data/periods/2026Q2/progress.json')
    catalog = read_json(Path(repo) / 'web/data/periods/2026Q2/catalog.json')
    if catalog.get('targetPeriodEnd') != PHASE['targetPeriodEnd']: fail('ADMISSION_CATALOG_MISMATCH')
    records = catalog_records(catalog)[1]
    rows = manifest.get('batches')
    if not isinstance(rows, list) or len(rows) != 2 or {row.get('batchId') for row in rows} != set(recovery.MISSING):
        fail('ADMISSION_MANIFEST_INVALID')
    prepared = recovery.instant(manifest.get('preparedAt'))
    if not prepared or prepared > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5): fail('ADMISSION_TIME_INVALID')
    validated = []; requested = set(); started = set(); files = []
    for batch in rows:
        index = batch['batchId']; folder = bundle / ('recovered-' + index)
        if batch.get('status') != 'recovered' or batch.get('marketRequests') != 0: fail('ADMISSION_RECOVERY_NOT_COMPLETE')
        config = read_json(folder / 'config.json')
        expected, skipped = recovery.recovery_config(original_configs[index], progress, prepared)
        if config != expected or batch.get('deferredCodes') != skipped or batch.get('requested') != len(config['funds']):
            fail('ADMISSION_CONFIG_MISMATCH')
        codes = {item['code'] for item in config['funds']}
        if requested & codes: fail('ADMISSION_DUPLICATE_CODE')
        requested.update(codes)
        if any(code not in records or next(item for item in config['funds'] if item['code'] == code).get('companyCode')
               != records[code]['companyCode'] for code in codes): fail('ADMISSION_CATALOG_MISMATCH')
        part = read_status_snapshot(folder / 'part.json', config)
        status = read_json(folder / 'status.json'); receipt = read_json(folder / 'source-attempts.json')
        if not part or not valid_status(status, config): fail('ADMISSION_STATUS_INVALID')
        if (set(row['code'] for row in status['funds']) != codes or set(row['code'] for row in part.get('batchResults', [])) != codes
                or any(row.get('code') not in codes for row in part['funds'])
                or len({row.get('code') for row in part['funds']}) != len(part['funds'])
                or part.get('pricePatches') or part.get('reportLocatorReceipts') or part.get('strategyClaimsReceipts')
                or part.get('strategyClaimsPatches') or part.get('strategyClaimsTasks')): fail('ADMISSION_STATUS_INVALID')
        observed = recovery.instant(part.get('retrievedAt')); status_at = recovery.instant(status.get('attemptedAt'))
        if not observed or not status_at or not prepared <= observed <= status_at: fail('ADMISSION_TIME_INVALID')
        for fund in part['funds']:
            if fund.get('priceUpdatedAt') is not None: fail('ADMISSION_UNEXPECTED_PRICE_DATA')
            for row in fund.get('rows', []):
                price = row.get('priceRange') or {}
                if price.get('status') == 'observed' or any(price.get(key) is not None
                        for key in ('low', 'high', 'fetchedAt', 'sourceUrl', 'globalPriceConflict')):
                    fail('ADMISSION_UNEXPECTED_PRICE_DATA')
        if (not isinstance(receipt, dict) or set(receipt) != {'schemaVersion', 'marketRequests', 'originalMarketReceipts', 'sourceAttempts'}
                or receipt['schemaVersion'] != 1 or receipt['marketRequests'] != 0 or receipt['originalMarketReceipts'] != 'unknown'
                or not isinstance(receipt['sourceAttempts'], list)): fail('ADMISSION_SOURCE_RECEIPT_INVALID')
        attempts = {}
        for row in receipt['sourceAttempts']:
            stamp = recovery.instant(row.get('attemptedAt')); observed = recovery.instant(part.get('retrievedAt'))
            if (set(row) != {'code', 'attemptedAt', 'outcome'} or row.get('code') not in codes or row['code'] in attempts
                    or row.get('outcome') != 'returned' or not stamp or not observed or not prepared <= stamp <= observed):
                fail('ADMISSION_SOURCE_RECEIPT_INVALID')
            attempts[row['code']] = row
        for row in status['funds']:
            unstarted = row.get('status') == 'deferred-budget' and row.get('attempted') is False
            if (row['code'] in attempts) == unstarted: fail('ADMISSION_SOURCE_RECEIPT_INVALID')
            if any(row.get(key) for key in ('priceOnly', 'enrichmentAttempt', 'reportLocatorOnly', 'strategyClaimsOnly')):
                fail('ADMISSION_UNEXPECTED_LANE')
        started.update(attempts)
        diagnostic = read_json(folder / 'build-diagnostic.json')
        part_file = folder / 'part.json'
        if (diagnostic.get('status') != 'ok' or diagnostic.get('serialization') != 'compact-json'
                or diagnostic.get('maxOutputBytes') != MAX_FILE or diagnostic.get('outputBytes') != part_file.stat().st_size
                or diagnostic.get('outputSha256') != file_sha(part_file) or batch.get('partSha256') != file_sha(part_file)):
            fail('ADMISSION_PART_DIGEST_MISMATCH')
        for name in ('config.json', 'part.json', 'status.json', 'source-attempts.json'):
            files.append({'batchId': index, 'file': name, 'sha256': file_sha(folder / name)})
        validated.append({'batchId': index, 'config': config, 'status': status, 'folder': folder})
    return {'batches': validated, 'requested': requested, 'started': started, 'files': files,
            'catalog': catalog, 'progress': progress, 'manifest': manifest}


def protected_files(data, *, all_files=False):
    result = {}
    for file in Path(data).rglob('*'):
        if file.is_symlink(): fail('ADMISSION_DATA_SYMLINK')
        if file.is_file() and (all_files or not file.relative_to(data).as_posix().startswith('periods/2026Q2/')):
            result[file.relative_to(data).as_posix()] = file_sha(file)
    return result


def fund_protection(quarter):
    manifest = read_json(Path(quarter) / 'latest.json'); result = {}
    files = manifest.get('companyFiles', [])
    if not isinstance(files, list) or len(files) > 10000: fail('ADMISSION_MANIFEST_INVALID')
    for item in files:
        name = item.get('path', '')
        if not re.fullmatch(r'\./data/companies/[A-Za-z0-9_-]{1,100}\.json', name): fail('ADMISSION_MANIFEST_INVALID')
        envelope = read_json(Path(quarter) / 'companies' / Path(name).name)
        for fund in envelope.get('funds', []):
            code = fund.get('code')
            if not isinstance(code, str) or code in result: fail('ADMISSION_FUND_IDENTITY_INVALID')
            prices = {}
            for row in fund.get('rows', []):
                price = row.get('priceRange') or {}
                if price.get('status') != 'observed' and not price.get('globalPriceConflict'): continue
                key = '|'.join(str(row.get(field)) for field in ('market', 'stockCode', 'currency'))
                prices[key] = digest({key: value for key, value in price.items() if key not in {'dataStatus', 'label'}})
            source = copy.deepcopy(fund)
            for key in ('dataStatus', 'cacheReason', 'summary', 'priceUpdatedAt',
                        'company', 'companyId',
                        'status', 'collectionAttempt', 'collectionErrors',
                        'portfolioDeduplicated', 'portfolioRepresentativeCode',
                        'aliases', 'companyMapping', 'selectedReport', 'previousReport'):
                source.pop(key, None)
            for row in source.get('rows', []): row.pop('priceRange', None)
            result[code] = {'sourceHash': digest(source),
                            'sourceFields': {key: digest(value) for key, value in source.items()},
                            'prices': prices, 'priceUpdatedAt': fund.get('priceUpdatedAt')}
    return result


def protect_market_budgets(before, after, started):
    previous = {row['code']: row for row in before['funds']}; current = {row['code']: row for row in after['funds']}
    if set(previous) != set(current): fail('ADMISSION_PROGRESS_SCOPE_CHANGED')
    quality_groups = {}
    for code, row in current.items():
        representative = row.get('representativeCode') if isinstance(row.get('representativeCode'), str) else code
        quality_groups.setdefault(representative, []).append(code)

    def alias_quality_sync(code, key, old_value, new_value):
        """Accept quality counters copied from a source-started representative.

        A single quarterly report can be shared by several share classes.  The
        merger projects the representative's quality counters onto those
        aliases, even when the alias itself was not part of the source request.
        The representative is still required to be source-started and may move
        by at most one real attempt; the alias must exactly match its resulting
        counter.  This does not permit top-level market or enrichment counters
        to move.
        """
        if key not in {'attempts', 'totalAttempts', 'managerAttempts', 'managerTotalAttempts',
                       'strategyAttempts', 'strategyTotalAttempts'}:
            return False
        representative = current[code].get('representativeCode')
        if not isinstance(representative, str) or representative not in started:
            return False
        old_rep = previous.get(representative, {}).get('quality', {})
        new_rep = current.get(representative, {}).get('quality', {})
        rep_before, rep_after = old_rep.get(key), new_rep.get(key)
        if any(type(value) is not int for value in (old_value, new_value, rep_before, rep_after)):
            return False
        started_members = sum(member in started for member in quality_groups.get(representative, ()))
        return (started_members > 0 and rep_before <= rep_after <= rep_before + started_members
                and new_value == rep_after and new_value >= old_value)

    for code, old in previous.items():
        new = current[code]
        for key in ('reportLocatorAttempts', 'strategyClaimsAttempts', 'strategyClaimsHistoryBlocked', 'reportLocatorHistoryFull'):
            if old.get(key) != new.get(key): fail('ADMISSION_UNRELATED_LEDGER_CHANGED')
        for key in ('attempts', 'totalAttempts', 'lastAttemptAt', 'retry'):
            before_value = old.get('enrichment', {}).get(key)
            after_value = new.get('enrichment', {}).get(key)
            if key == 'retry' and new.get('status') == 'not-applicable':
                # Reclassifying a product that was established after the
                # quarter rehydrates the empty enrichment lane and may stamp
                # its cycle date.  Its counters and future schedule must stay
                # unchanged; the date spelling is not a market attempt.
                before_retry = before_value if isinstance(before_value, dict) else {}
                after_retry = after_value if isinstance(after_value, dict) else {}
                if (before_retry.get('scheduledCycles') == after_retry.get('scheduledCycles')
                        and before_retry.get('nextRetryAt') == after_retry.get('nextRetryAt')
                        and isinstance(after_retry.get('cycleDate'), str)):
                    continue
            if before_value != after_value: fail('ADMISSION_MARKET_LEDGER_CHANGED')
        for container in (None, 'quality'):
            left = old if container is None else old.get(container, {})
            right = new if container is None else new.get(container, {})
            for key in ('attempts', 'totalAttempts', 'managerAttempts', 'managerTotalAttempts', 'strategyAttempts', 'strategyTotalAttempts'):
                a, b = left.get(key), right.get(key)
                # Older persisted quality rows predate the independent manager/
                # strategy counters.  merge_progress materializes those fields
                # as zero (or derives a total from its paired attempt counter).
                # Treat that schema backfill as the historical baseline; only a
                # real increment for a source-started code may pass this gate.
                if a is None:
                    paired = {
                        'totalAttempts': 'attempts',
                        'managerTotalAttempts': 'managerAttempts',
                        'strategyTotalAttempts': 'strategyAttempts',
                    }.get(key)
                    a = left.get(paired) if paired else 0
                    if a is None: a = 0
                if b is None and a == 0: continue
                if container == 'quality' and alias_quality_sync(code, key, a, b): continue
                if type(a) is not int or type(b) is not int or not 0 <= b - a <= (1 if code in started else 0):
                    # Keep the public failure bounded while identifying the
                    # counter family that needs a schema/merge fix.  Never
                    # include a fund code or raw values in the receipt.
                    fail('ADMISSION_SOURCE_BUDGET_CHANGED_' + key.upper())


def _same_requested_portfolio(old, new, requested, code=None):
    """Allow only alias metadata normalization within a requested portfolio.

    merge-parts rebuilds a deduplicated portfolio from all of its share-class
    observations.  That can rewrite a non-requested class while preserving
    the same explicitly evidenced portfolio identity.  A source hash change
    outside that identity must remain a hard admission failure.
    """
    related = set()
    old_code = (old.get('code') if isinstance(old, dict) else None) or code
    old_portfolio = old.get('sourcePortfolioCode') if isinstance(old, dict) else None
    new_portfolio = new.get('sourcePortfolioCode') if isinstance(new, dict) else None
    new_aliases = set(new.get('shareClassCodes') or []) if isinstance(new, dict) else set()
    for row in (old, new):
        if not isinstance(row, dict):
            continue
        representative = row.get('sourcePortfolioCode')
        if isinstance(representative, str):
            related.add(representative)
        aliases = row.get('shareClassCodes')
        if isinstance(aliases, list):
            related.update(code for code in aliases if isinstance(code, str))
    requested = set(requested)
    if not related & requested:
        return False
    if isinstance(old_portfolio, str) and old_portfolio == new_portfolio:
        return True
    # A source refresh may fill a previously absent representative identity on
    # a non-requested share class. Allow that one-way normalization only when
    # the new representative is requested and the old code is in that alias group.
    return (isinstance(new_portfolio, str) and new_portfolio in requested
            and isinstance(old_code, str) and old_code in new_aliases)


def _same_requested_portfolio_url(old, new, requested, code=None):
    """Recognize URL-only normalization inside an evidenced alias group."""
    requested = set(requested)
    old_aliases = set(old.get('shareClassCodes') or []) if isinstance(old, dict) else set()
    new_aliases = set(new.get('shareClassCodes') or []) if isinstance(new, dict) else set()
    for row, target in ((old, old_aliases), (new, new_aliases)):
        representative = row.get('sourcePortfolioCode') if isinstance(row, dict) else None
        if isinstance(representative, str):
            target.add(representative)
    members = old_aliases | new_aliases
    if not (requested & new_aliases) or not isinstance(code, str) or code not in new_aliases:
        return False
    old_rep = old.get('sourcePortfolioCode') if isinstance(old, dict) else None
    new_rep = new.get('sourcePortfolioCode') if isinstance(new, dict) else None
    if (isinstance(old_rep, str) and isinstance(new_rep, str) and old_rep != new_rep
            and old_rep not in new_aliases and new_rep not in old_aliases):
        return False
    return bool(members)


def protect_funds(before, after, requested):
    requested = set(requested)
    for code, old in before.items():
        if code not in after: fail('ADMISSION_EXISTING_FUND_REMOVED')
        new = after[code]
        if code not in requested and old['sourceHash'] != new['sourceHash']:
            before_fields, after_fields = old.get('sourceFields', {}), new.get('sourceFields', {})
            changed = sorted(key for key in set(before_fields) | set(after_fields)
                             if before_fields.get(key) != after_fields.get(key))
            same_portfolio = _same_requested_portfolio(old, new, requested, code)
            url_only_alias_normalization = (changed == ['portfolioSourceUrl']
                                            and _same_requested_portfolio_url(old, new, requested, code))
            if same_portfolio or url_only_alias_normalization:
                continue
            # Keep the receipt bounded and category-only; the first changed
            # field is sufficient to route the next repair without exposing
            # values or allowing the public error-code grammar to overflow.
            suffix = re.sub(r'[^A-Z_]', '_', changed[0].upper())[:32] if changed else 'UNKNOWN_FIELD'
            fail('ADMISSION_UNRELATED_SOURCE_CHANGED_' + suffix)
        for key, value in old['prices'].items():
            if new['prices'].get(key) != value: fail('ADMISSION_PRICE_FACT_CHANGED')
        if old['priceUpdatedAt'] is not None and old['priceUpdatedAt'] != new['priceUpdatedAt']:
            fail('ADMISSION_PRICE_CLOCK_CHANGED')


def admit(repo, bundle, binding, *, env, now=None, runner=command):
    stage('ADMIT_LOAD_LEDGER')
    repo = Path(repo); quarter = repo / 'web/data/periods/2026Q2'
    ledger = load_ledger(quarter); identifier = digest(binding)
    existing = [row for row in ledger['entries'] if row.get('binding', {}).get('recoveryRunId') == binding['recoveryRunId']]
    if existing:
        if len(existing) != 1 or existing[0]['admissionDigest'] != identifier: fail('ADMISSION_REPLAY_CONFLICT')
        return {'status': 'already-admitted', 'admissionDigest': identifier, 'sourceRequests': 0, 'marketRequests': 0, 'continueRun': False}
    if len(ledger['entries']) >= 16: fail('ADMISSION_LEDGER_FULL')
    stage('ADMIT_CHECK_BASE')
    hold = check_base(repo, binding, env)
    stage('ADMIT_VERIFY_BUNDLE')
    bundle_info = verify_bundle(bundle, binding, repo)
    stage('ADMIT_PROTECT_BEFORE')
    protected = protected_files(repo / 'web/data'); before = fund_protection(quarter)
    audit = read_json(quarter / 'audit.json'); progress_before = bundle_info['progress']
    current = now or dt.datetime.now(dt.timezone.utc)
    with tempfile.TemporaryDirectory(prefix='admit-source-recovery-') as temporary:
        parts = Path(temporary) / 'parts'; parts.mkdir()
        for batch in bundle_info['batches']:
            target = parts / ('part-' + batch['batchId']); target.mkdir()
            for name in ('part.json', 'status.json'): shutil.copyfile(batch['folder'] / name, target / name)
        # Explicitly bounded to two new parts; no complete-status reconciliation.
        config = read_json(Path(bundle) / 'original-campaign/2026Q2.json')
        config['funds'] = [item for batch in bundle_info['batches'] for item in batch['config']['funds']]
        config['asOf'] = max(batch['config']['asOf'] for batch in bundle_info['batches'])
        config_file = Path(temporary) / 'config.json'; atomic_json(config_file, config)
        stage('ADMIT_MERGE_PARTS')
        runner(['node', str(ROOT / 'scripts/merge-parts.js'), '--parts', str(parts), '--data', str(quarter),
                '--catalog', str(quarter / 'catalog.json'), '--config', str(config_file),
                '--reuseMarketPrices', 'false'], cwd=ROOT, timeout=2400)
        stage('ADMIT_RECONCILE')
        receipt = read_json(parts / 'merge-receipt.json')
        statuses = reconcile_observations([row['status'] for row in bundle_info['batches']], receipt, config, current)
        updated = merge_progress(bundle_info['catalog'], progress_before, config, statuses, current,
                                 retained=load_retained_funds(quarter))
        stage('ADMIT_PROTECT_BUDGETS')
        protect_market_budgets(progress_before, updated, bundle_info['started'])
        stage('ADMIT_PROTECT_FUNDS')
        protect_funds(before, fund_protection(quarter), bundle_info['requested'])
        if protected_files(repo / 'web/data') != protected: fail('ADMISSION_NON_TARGET_DATA_CHANGED')
        stage('ADMIT_PERSIST')
        atomic_json(quarter / 'progress.json', updated)
        # Coverage may advance, but a source-only recovery cannot settle unknown markets.
        for key in ('updatedAt', 'completionStatus', 'totalFunds', 'processedFunds', 'failedFunds', 'pendingFunds',
                    'notApplicableFunds', 'research', 'completionAudit', 'strategyClaims'):
            if key in updated: audit[key] = updated[key]
        audit.update(**hold, continueRun=False)
        atomic_json(quarter / 'audit.json', audit)
        stage('ADMIT_ANALYZE')
        runner(['node', str(ROOT / 'scripts/analyze-data.js'), '--data', str(quarter), '--output', str(quarter / 'analysis.json')],
               cwd=ROOT, timeout=2400)
        stage('ADMIT_PROTECT_AFTER')
        if protected_files(repo / 'web/data') != protected or deployed_continuation_state(repo / 'web/data', QUARTER) != hold:
            fail('ADMISSION_PROTECTION_FAILED')
    entry = {'admissionDigest': identifier, 'binding': binding, 'admittedAt': current.isoformat(),
             'files': bundle_info['files'], 'requestedCodes': sorted(bundle_info['requested']),
             'startedSourceCodes': sorted(bundle_info['started']), 'originalMarketRequests': 'unknown',
             'recoveryMarketRequests': 0, 'holdPreserved': True}
    ledger['entries'].append(entry); recovery.public_write(quarter / LEDGER, ledger)
    return {'status': 'staged', 'persistence': 'pending', 'admissionDigest': identifier, 'requestedFundCount': len(bundle_info['requested']),
            'startedSourceFundCount': len(bundle_info['started']), 'sourceRequests': 0, 'marketRequests': 0,
            'originalMarketRequests': 'unknown', 'holdPreserved': True, 'continueRun': False}


def run(repo, binding, *, api=None, env=None):
    env = os.environ if env is None else env; repo = Path(repo)
    stage('RUN_CHECK_RUNTIME')
    check_runtime(repo, env)
    # Idempotent already-persisted admissions require no artifact download.
    entries = load_ledger(repo / 'web/data/periods/2026Q2')['entries']
    matching = [row for row in entries if row.get('binding', {}).get('recoveryRunId') == binding['recoveryRunId']]
    if matching:
        if len(matching) != 1 or matching[0]['admissionDigest'] != digest(binding): fail('ADMISSION_REPLAY_CONFLICT')
        return {'status': 'already-admitted', 'admissionDigest': digest(binding), 'sourceRequests': 0, 'marketRequests': 0, 'continueRun': False}
    stage('RUN_CHECK_BASE')
    check_base(repo, binding, env)
    api = api or ActionsAPI()
    stage('RUN_CHECK_MAIN')
    if api.get('commits/main').get('sha') != env.get('GITHUB_SHA'): fail('ADMISSION_MAIN_MOVED')
    stage('RUN_VERIFY_METADATA')
    metadata = verified_metadata(api, binding)
    with tempfile.TemporaryDirectory(prefix='verified-recovery-artifact-') as temporary:
        archive = Path(temporary) / 'recovery.zip'; bundle = Path(temporary) / 'bundle'; bundle.mkdir()
        stage('RUN_DOWNLOAD')
        api.download(binding['artifactId'], archive)
        stage('RUN_EXTRACT')
        extract_archive(archive, bundle, metadata, binding)
        stage('RUN_ADMIT')
        return admit(repo, bundle, binding, env=env)


class SafeParser(argparse.ArgumentParser):
    def error(self, message): fail('ADMISSION_ARGUMENTS_INVALID')


def main(argv=None):
    try:
        parser = SafeParser()
        parser.add_argument('--verify-protected-output', action='store_true')
        parser.add_argument('--base-data-commit')
        parser.add_argument('--recovery-run-id', type=int)
        parser.add_argument('--recovery-source-commit')
        parser.add_argument('--artifact-id', type=int)
        parser.add_argument('--archive-sha256')
        args = parser.parse_args(argv)
        proof_path = ROOT / '.cache/recovery-admission-protection.json'
        if args.verify_protected_output:
            if os.environ.get('GITHUB_ACTIONS') != 'true': fail('ADMISSION_REQUIRES_ACTIONS')
            proof = read_json(proof_path)
            if proof != protected_files(ROOT / 'web/data', all_files=True): fail('ADMISSION_POST_PERSIST_DATA_CHANGED')
            print(json.dumps({'status': 'protected-output-verified'})); return 0
        binding = binding_for(args.base_data_commit, args.recovery_run_id, args.recovery_source_commit, args.artifact_id, args.archive_sha256)
        result = run(ROOT, binding)
        atomic_json(proof_path, protected_files(ROOT / 'web/data', all_files=True))
        recovery.public_write(ROOT / '.cache/recovery-admission/result.json', result)
        print(json.dumps(result)); return 0
    except Exception as error:
        if isinstance(error, AdmissionError) and re.fullmatch(r'ADMISSION_[A-Z_]{1,64}', str(error)):
            code = str(error)
        else:
            error_type = re.sub(r'[^A-Z0-9_]', '_', type(error).__name__.upper())[:24] or 'UNKNOWN'
            code = f'ADMISSION_EXECUTION_FAILED_{EXECUTION_STAGE}_{error_type}'
        result = {'status': 'failed', 'errorCode': code, 'continueRun': False, 'sourceRequests': 0, 'marketRequests': 0}
        try: recovery.public_write(ROOT / '.cache/recovery-admission/result.json', result)
        except Exception: pass
        print(json.dumps(result)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
