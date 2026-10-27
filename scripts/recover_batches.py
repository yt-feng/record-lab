"""One explicit artifact recovery; source reports only, never market requests.

This isolated workflow does not publish, mutate progress, or continue a chain.
The two lost batches have unknown quote receipts. Their budgets are not zero.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from urllib.parse import urlsplit

from publication_cache import read_bounded_command
from publication_guard import inspect_content
from source_dates import china_today

REPOSITORY = 'yt-feng/record-lab'
WORKFLOW = '.github/workflows/recover-batches.yml'
ORIGINAL_RUN = 40412646517
ORIGINAL_SOURCE = '383e7996dcbe4b6a8ff4535c8f3dc8292abb3bda'
MISSING = ('10', '13')
MAX_BYTES = 25_000_000
MAX_ARCHIVE = 32_000_000
SHA = re.compile(r'[0-9a-f]{40}')
HEX = re.compile(r'[0-9a-f]{64}')


class RecoveryError(ValueError):
    pass


def fail(code):
    raise RecoveryError(code)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def instant(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(dt.timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def parse(raw):
    try:
        def pairs(rows):
            result = {}
            for key, value in rows:
                if key in result: raise ValueError()
                result[key] = value
            return result
        def constant(_): raise ValueError()
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError):
        fail('RECOVERY_JSON_INVALID')


def public_write(path, value):
    raw = encoded(value)
    if inspect_content(raw): fail('RECOVERY_PUBLICATION_REJECTED')
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(raw); temporary.replace(path)


class ActionsAPI:
    def __init__(self):
        if os.environ.get('GITHUB_ACTIONS') != 'true' or not os.environ.get('GH_TOKEN'):
            fail('RECOVERY_REQUIRES_ACTIONS')
        self.deadline = time.monotonic() + 600

    def get(self, suffix, binary=False):
        if not re.fullmatch(r'(?:commits/main|actions/(?:runs/\d+(?:/(?:artifacts|jobs)\?per_page=100)?|artifacts/\d+/zip|workflows/\d+/runs\?(?:status=(?:queued|in_progress)&)?per_page=100))', suffix):
            fail('RECOVERY_ENDPOINT_INVALID')
        try:
            raw = read_bounded_command(['gh', 'api', '--hostname', 'github.com', f'repos/{REPOSITORY}/{suffix}'],
                                       MAX_ARCHIVE if binary else 2_000_000, min(self.deadline, time.monotonic() + 90))
            return raw if binary else parse(raw)
        except RecoveryError: raise
        except Exception: fail('RECOVERY_API_UNAVAILABLE')


def verify_runs(original, current, history, active, *, data_commit, latest_commit, env):
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_WORKFLOW_REF') != f'{REPOSITORY}/{WORKFLOW}@refs/heads/main'
            or env.get('GITHUB_RUN_ATTEMPT') != '1' or not SHA.fullmatch(str(data_commit))
            or latest_commit != data_commit): fail('RECOVERY_IDENTITY_INVALID')
    for row in (original, current):
        repo, head = row.get('repository', {}), row.get('head_repository', {})
        if (repo.get('full_name') != REPOSITORY or head.get('full_name') != REPOSITORY
                or type(repo.get('id')) is not int or repo['id'] != head.get('id')
                or row.get('head_branch') != 'main' or type(row.get('run_attempt')) is not int or row['run_attempt'] != 1):
            fail('RECOVERY_RUN_MISMATCH')
    if (original.get('id') != ORIGINAL_RUN or original.get('head_sha') != ORIGINAL_SOURCE
            or original.get('path') != '.github/workflows/traverse.yml'
            or original.get('status') != 'completed'
            or original.get('conclusion') not in {'success', 'failure'}): fail('RECOVERY_ORIGINAL_NOT_TERMINAL')
    if (str(current.get('id')) != env.get('GITHUB_RUN_ID') or current.get('head_sha') != env.get('GITHUB_SHA')
            or current.get('path') != WORKFLOW or current.get('event') != 'workflow_dispatch'
            or current.get('status') != 'in_progress' or type(current.get('run_number')) is not int or current['run_number'] != 1):
        fail('RECOVERY_REPLAY_NOT_PERMITTED')
    rows = history.get('workflow_runs')
    if (type(history.get('total_count')) is not int or history['total_count'] != 1 or not isinstance(rows, list) or len(rows) != 1
            or rows[0].get('id') != current['id'] or type(rows[0].get('run_attempt')) is not int or rows[0]['run_attempt'] != 1):
        fail('RECOVERY_REPLAY_NOT_PERMITTED')
    rows = active.get('workflow_runs')
    if not isinstance(rows, list) or len(rows) > 200 or type(active.get('total_count')) is not int or active['total_count'] != len(rows): fail('RECOVERY_ACTIVE_RUNS_UNVERIFIED')
    if any(row.get('status') != 'completed' for row in rows): fail('RECOVERY_OTHER_TRAVERSAL_ACTIVE')


def archive_members(raw, metadata, original, allowed):
    identity = metadata.get('workflow_run', {})
    if (metadata.get('expired') is not False or type(metadata.get('id')) is not int
            or identity.get('id') != ORIGINAL_RUN or identity.get('head_sha') != ORIGINAL_SOURCE
            or identity.get('repository_id') != original['repository']['id']
            or identity.get('head_repository_id') != original['repository']['id']
            or identity.get('head_branch') != 'main' or type(metadata.get('size_in_bytes')) is not int
            or not 0 < len(raw) <= MAX_ARCHIVE or metadata['size_in_bytes'] != len(raw)
            or metadata.get('digest') != 'sha256:' + digest(raw)):
        fail('RECOVERY_ARCHIVE_IDENTITY_INVALID')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (len(entries) > 100 or len({x.filename for x in entries}) != len(entries)
                    or sum(x.file_size for x in entries) > 100_000_000): fail('RECOVERY_ARCHIVE_INVALID')
            result = {}
            for entry in entries:
                if entry.is_dir(): continue
                if (not allowed(entry.filename) or entry.file_size > MAX_BYTES or entry.flag_bits & 1
                        or entry.external_attr >> 16 & 0o170000 == 0o120000
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    fail('RECOVERY_ARCHIVE_INVALID')
                content = archive.read(entry)
                if inspect_content(content): fail('RECOVERY_PUBLICATION_REJECTED')
                parse(content)
                result[entry.filename] = content
            return result
    except RecoveryError: raise
    except Exception: fail('RECOVERY_ARCHIVE_INVALID')


def validate_plan(plan, campaign, frozen_root=None):
    matrix = parse(plan.get('matrix.json', b'null'))
    selection = parse(campaign.get('selection.json', b'null'))
    if (not isinstance(matrix, list) or len(matrix) != 32 or not isinstance(selection, dict)
            or selection.get('quarters') != ['2026Q2', '2026Q3']): fail('RECOVERY_PLAN_INVALID')
    catalogs = {}
    from traversal import catalog_records
    for quarter in selection['quarters']:
        binding = selection.get('catalogs', {}).get(quarter, {})
        name = f'catalogs/{quarter}.json'; content = campaign.get(name, b'')
        target = '2026-06-30' if quarter == '2026Q2' else '2026-09-30'
        if not content or binding.get('path') != name or binding.get('targetPeriodEnd') != target or digest(content) != binding.get('sha256'):
            fail('RECOVERY_CATALOG_LOCK_INVALID')
        catalog = parse(content)
        if catalog.get('targetPeriodEnd') != target or catalog.get('quarterUniverse', {}).get('targetPeriodEnd') != target:
            fail('RECOVERY_CATALOG_LOCK_INVALID')
        if frozen_root is not None:
            from quarter_catalog import validate_catalog_lock
            quarter_config = parse(campaign.get(quarter + '.json', b'null'))
            try: validate_catalog_lock(quarter, quarter_config, binding, campaign_root=frozen_root)
            except Exception: fail('RECOVERY_CATALOG_LOCK_INVALID')
        catalogs[quarter] = catalog_records(catalog)[1]
    configs = {}
    for item in matrix:
        index = item.get('id'); quarter = '2026Q2' if str(index).isdigit() and int(index) < 16 else '2026Q3'
        if (not isinstance(index, str) or index in configs or index not in {str(x) for x in range(32)}
                or item.get('config') != f'batch-{index}.json' or item.get('quarter') != quarter): fail('RECOVERY_PLAN_INVALID')
        config = parse(plan.get(item['config'], b'null'))
        binding = selection.get('catalogs', {}).get(quarter, {})
        catalog_path = binding.get('path')
        catalog = campaign.get(catalog_path, b'') if isinstance(catalog_path, str) else b''
        phase = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'} if quarter == '2026Q2' else {
            'targetPeriodEnd': '2026-09-30', 'baselinePeriodEnd': '2026-06-30'}
        if (not isinstance(config, dict) or config.get('quarter') != quarter or not catalog
                or config.get('catalogDigest') != binding.get('sha256')
                or config.get('catalogPath') != catalog_path or config.get('catalogLockVersion') != 1
                or config.get('traversal', {}).get('phase') != phase
                or config.get('traversal', {}).get('batchId') != index
                or any(config.get(k) != v for k, v in phase.items())
                or not instant(config.get('traversal', {}).get('plannedAt'))): fail('RECOVERY_CATALOG_LOCK_INVALID')
        funds = config.get('funds')
        if (not isinstance(funds, list) or len(funds) != 75
                or len({x.get('code') for x in funds}) != len(funds)
                or any(not re.fullmatch(r'\d{6}', str(x.get('code'))) for x in funds)): fail('RECOVERY_PLAN_INVALID')
        if any(item['code'] not in catalogs[quarter]
               or item.get('companyCode') != catalogs[quarter][item['code']]['companyCode'] for item in funds):
            fail('RECOVERY_CATALOG_MEMBERSHIP_INVALID')
        configs[index] = config
    return configs


def remaining_budget(state, config, code, now):
    """Read counters verbatim. No parser migration, cooldown reset or retry cycle."""
    planned = instant(config['traversal']['plannedAt'])
    if not isinstance(state, dict) or state.get('code') != code: return False
    if state.get('deferredUntil') or state.get('nextRetryAt'): return False
    if state.get('attribution') == 'portfolio-evidence' or state.get('representativeCode', code) != code: return False
    quality = state.get('quality', {})
    def available(row, field, clock, limit):
        count = row.get(field); last = instant(row.get(clock))
        if last and last > now: return False
        # The lost batch definitely attempted every product. Reserve that slot
        # when the latest published lane has not already accounted for it.
        reserve = 0 if last and last >= planned else 1
        return type(count) is int and 0 <= count + reserve < limit
    if code not in config['traversal'].get('qualityCodes', []):
        return state.get('status') in {'pending', 'failed'} and available(state, 'attempts', 'lastAttemptAt', 2)
    missing = quality.get('missingFields', [])
    if not isinstance(missing, list) or state.get('status') not in {'complete', 'no-equities'}: return False
    if code in config['traversal'].get('managerCodes', []):
        return 'managers' in missing and available(quality, 'managerAttempts', 'managerLastAttemptAt', 3)
    return any(field not in {'prices', 'managers', 'strategy'} for field in missing) and available(quality, 'attempts', 'lastAttemptAt', 3)


def recovery_config(original, progress, now):
    phase = original['traversal']['phase']
    if (not isinstance(progress, dict) or progress.get('schemaVersion') != 1 or progress.get('phase') != phase
            or type(progress.get('collectorVersion')) is not int or progress['collectorVersion'] != original.get('collectorVersion', 1)
            or not isinstance(progress.get('funds'), list)): fail('RECOVERY_PROGRESS_INVALID')
    rows = progress['funds']; states = {row.get('code'): row for row in rows if isinstance(row, dict)}
    if len(states) != len(rows): fail('RECOVERY_PROGRESS_INVALID')
    funds, skipped = [], []
    for item in original['funds']:
        code = item['code']
        if (any(key in item for key in ('priceOnly', 'strategyClaimsOnly', 'reportLocatorOnly'))
                or not remaining_budget(states.get(code), original, code, now)):
            skipped.append(code); continue
        funds.append({k: v for k, v in item.items() if k not in {'reportLocator', 'priceTargets'}})
    kept = {item['code'] for item in funds}
    traversal = {**original['traversal'], 'plannedAt': now.isoformat()}
    for key, value in list(traversal.items()):
        if key.endswith('Codes') and isinstance(value, list): traversal[key] = [code for code in value if code in kept]
    traversal.update(enrichmentCodes=[], strategyClaimsCodes=[], reportLocatorCodes=[])
    config = {**original, 'funds': funds, 'traversal': traversal, 'asOf': china_today(now),
              'maxPriceSymbols': 0, 'maxPriceSeconds': 0, 'reportLocatorEnabled': False,
              'recovery': {'schemaVersion': 1, 'originalRunId': ORIGINAL_RUN, 'originalBatchId': traversal['batchId'],
                           'originalPlannedAt': original['traversal']['plannedAt'], 'originalPlanCanonicalSha256': digest(encoded(original)),
                           'lostMarketAttempts': 'unknown', 'marketRequestsPermitted': False}}
    return config, skipped


def source_url_allowed(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443): return False
        return bool((parsed.hostname == 'fundf10.eastmoney.com' and (re.fullmatch(r'/jbgk_\d{6}\.html', parsed.path)
                     or parsed.path == '/FundArchivesDatas.aspx'))
                    or parsed.hostname == 'api.fund.eastmoney.com' and parsed.path == '/f10/JJGG'
                    or parsed.hostname == 'pdf.dfcfw.com' and re.fullmatch(r'/pdf/H2_AN\d{8,32}_1\.pdf', parsed.path))
    except (TypeError, ValueError): return False


def provider_class():
    import requests
    from providers import PublicProvider, DataError
    class ReportSession(requests.Session):
        def request(self, method, url, **kwargs):
            if method.upper() != 'GET' or not source_url_allowed(url): raise DataError('RECOVERY_NETWORK_DENIED')
            kwargs['allow_redirects'] = False
            return super().request(method, url, **kwargs)
    class ReportProvider(PublicProvider):
        def __init__(self):
            super().__init__()
            headers = dict(self.session.headers); self.session.close()
            self.session = ReportSession(); self.session.headers.update(headers)
        def price_bars(self, *args, **kwargs):
            raise DataError('RECOVERY_MARKET_REQUEST_FORBIDDEN')
        def get(self, url, **kwargs):
            if not source_url_allowed(url): raise DataError('RECOVERY_NETWORK_DENIED')
            return super().get(url, **kwargs)
    return ReportProvider


def collect_reports(config, raw_path, receipt_path, *, collector=None, provider_factory=None):
    from collect import collect_fund
    collector = collector or collect_fund; provider_factory = provider_factory or provider_class()
    deadline = time.monotonic() + min(1500, config.get('maxBatchSeconds', 1500))
    funds, errors, receipts, lock = [], [], {}, threading.RLock()
    def checkpoint():
        value = {'asOf': config['asOf'], 'targetPeriodEnd': config['targetPeriodEnd'], 'baselinePeriodEnd': config['baselinePeriodEnd'],
                 'retrievedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'funds': list(funds), 'errors': list(errors),
                 'barsByStock': {}, 'pricePatches': [], 'coverage': {'status': 'partial', 'priceSymbolsAttempted': 0}}
        path = Path(raw_path); path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp'); temporary.write_bytes(encoded(value)); temporary.replace(path)
        public_write(receipt_path, {'schemaVersion': 1, 'marketRequests': 0, 'originalMarketReceipts': 'unknown',
                                   'sourceAttempts': list(receipts.values())})
        return value
    def one(item):
        if time.monotonic() >= deadline:
            return {'code': item['code'], 'reports': [], 'collectionAttempt': {'attempted': False,
                    'status': 'deferred-budget', 'errorCode': 'BATCH_DEADLINE_REACHED'}}, []
        with lock:
            receipts[item['code']] = {'code': item['code'], 'attemptedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'outcome': 'unknown'}
            checkpoint()
        result = collector(item, config['asOf'], config.get('historyYears', 2), config.get('maxReportsPerFund', 2),
                           provider=provider_factory(), target_period=config['targetPeriodEnd'],
                           baseline_period=config['baselinePeriodEnd'], deadline=deadline)
        with lock: receipts[item['code']]['outcome'] = 'returned'
        return result
    checkpoint()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(one, item) for item in config['funds']]
        for future in concurrent.futures.as_completed(futures):
            fund, issues = future.result()
            with lock: funds.append(fund); errors.extend(issues); checkpoint()
    return checkpoint()


def build_part(raw_path, config_path, output, *, runner=subprocess.run):
    output = Path(output)
    if output.exists(): fail('RECOVERY_OUTPUT_ALREADY_EXISTS')
    result = runner(['node', 'scripts/build-data.js', '--input', str(raw_path), '--config', str(config_path), '--output', str(output)],
                    capture_output=True, timeout=180)
    try: diagnostic = parse(result.stdout)
    except RecoveryError: diagnostic = {'status': 'failed', 'errorCode': 'RECOVERY_BUILD_FAILED'}
    allowed = {'status', 'errorCode', 'fundCount', 'failedFundCount', 'holdingCount', 'errorCount',
               'serialization', 'outputBytes', 'maxOutputBytes', 'outputSha256'}
    errors = {'OUTPUT_TOO_LARGE', 'OUTPUT_WRITE_FAILED', 'INPUT_READ_FAILED', 'INVALID_CONFIG',
              'INVALID_PREVIOUS_SNAPSHOT', 'INVALID_CATALOG', 'INVALID_ARGUMENTS', 'NO_VALID_FRESH_FUNDS', 'RECOVERY_BUILD_FAILED'}
    if (not isinstance(diagnostic, dict) or set(diagnostic) - allowed or diagnostic.get('status') not in {'ok', 'failed'}
            or any(type(value) is not int or value < 0 for key, value in diagnostic.items() if key.endswith('Count') or key in {'outputBytes', 'maxOutputBytes'})
            or diagnostic.get('errorCode') is not None and diagnostic['errorCode'] not in errors
            or diagnostic.get('serialization') is not None and diagnostic['serialization'] != 'compact-json'
            or diagnostic.get('outputSha256') is not None and not HEX.fullmatch(str(diagnostic['outputSha256']))):
        diagnostic = {'status': 'failed', 'errorCode': 'RECOVERY_BUILD_DIAGNOSTIC_INVALID'}
    public_write(output.parent / 'build-diagnostic.json', {'schemaVersion': 1, 'stage': 'build', **diagnostic})
    if result.returncode != 0 or diagnostic.get('status') != 'ok':
        if output.is_file(): output.unlink()
        return False
    if (not output.is_file() or output.stat().st_size > MAX_BYTES
            or output.stat().st_size != diagnostic.get('outputBytes')
            or digest(output.read_bytes()) != diagnostic.get('outputSha256')): fail('RECOVERY_BUILD_OUTPUT_INVALID')
    return True


def validate_output(output):
    """Upload only guarded normalized records and fixed diagnostics, never raw."""
    root = Path(output)
    if not root.is_dir() or root.is_symlink(): fail('RECOVERY_OUTPUT_INVALID')
    files = list(root.rglob('*'))
    if len(files) > 250: fail('RECOVERY_OUTPUT_INVALID')
    for file in files:
        if file.is_symlink(): fail('RECOVERY_OUTPUT_INVALID')
        if file.is_dir(): continue
        name = file.relative_to(root).as_posix()
        if (name not in {'result.json', 'execution-error.json'} and not re.fullmatch(r'original-parts/part-\d{1,2}/(?:part|status)\.json', name)
                and not re.fullmatch(r'original-plan/(?:matrix|meta|batch-\d{1,2})\.json', name)
                and not re.fullmatch(r'original-campaign/(?:selection|2026Q[23]|(?:catalogs|locators)/2026Q[23]|catalogs/2026Q[23]-identities/part-[a-f0-9]{64})\.json', name)
                and not re.fullmatch(r'recovered-(?:10|13)/(?:config|part|status|build-diagnostic|source-attempts)\.json', name)):
            fail('RECOVERY_OUTPUT_INVALID')
        if not file.is_file() or file.stat().st_size > MAX_BYTES or inspect_content(file.read_bytes()):
            fail('RECOVERY_PUBLICATION_REJECTED')
    return True


def run(data, output, data_commit, *, api=None, env=None):
    env = os.environ if env is None else env
    if env.get('GITHUB_ACTIONS') != 'true': fail('RECOVERY_REQUIRES_ACTIONS')
    if any(env.get(key) for key in ('MINER_U_KEYS', 'MINER_U', 'MINERU_API_KEY')): fail('RECOVERY_NATIVE_PDF_ONLY')
    api = api or ActionsAPI(); output = Path(output)
    if output.exists(): fail('RECOVERY_OUTPUT_ALREADY_EXISTS')
    try: current_id = int(env['GITHUB_RUN_ID'])
    except (KeyError, ValueError): fail('RECOVERY_IDENTITY_INVALID')
    original = api.get(f'actions/runs/{ORIGINAL_RUN}'); current = api.get(f'actions/runs/{current_id}')
    active = {'workflow_runs': [], 'total_count': 0}
    for status in ('queued', 'in_progress'):
        listing = api.get(f"actions/workflows/{original['workflow_id']}/runs?status={status}&per_page=100")
        rows = listing.get('workflow_runs')
        if not isinstance(rows, list) or len(rows) > 100 or type(listing.get('total_count')) is not int or listing['total_count'] != len(rows): fail('RECOVERY_ACTIVE_RUNS_UNVERIFIED')
        active['workflow_runs'].extend(rows); active['total_count'] += len(rows)
    verify_runs(original, current, api.get(f"actions/workflows/{current['workflow_id']}/runs?per_page=100"),
                active, data_commit=data_commit,
                latest_commit=api.get('commits/main').get('sha'), env=env)
    checkout = subprocess.run(['git', '-C', str(data), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    if checkout != data_commit: fail('RECOVERY_DATA_COMMIT_MISMATCH')
    for name in ('progress.json', 'audit.json'):
        relative = 'web/data/periods/2026Q2/' + name
        file = Path(data) / relative
        if file.is_symlink() or not file.is_file(): fail('RECOVERY_DATA_COMMIT_MISMATCH')
        expected = subprocess.run(['git', '-C', str(data), 'rev-parse', 'HEAD:' + relative], capture_output=True, text=True, check=True).stdout.strip()
        observed = subprocess.run(['git', '-C', str(data), 'hash-object', relative], capture_output=True, text=True, check=True).stdout.strip()
        if observed != expected: fail('RECOVERY_DATA_COMMIT_MISMATCH')
    from campaign import deployed_continuation_state
    held = deployed_continuation_state(Path(data) / 'web/data', '2026Q2')
    hold = held['continuationHold']
    if not hold or hold['sourceRunId'] != str(ORIGINAL_RUN) or set(hold['batchIds']) != set(MISSING):
        fail('RECOVERY_PUBLISHED_HOLD_UNVERIFIED')
    jobs = api.get(f'actions/runs/{ORIGINAL_RUN}/jobs?per_page=100')
    if not isinstance(jobs.get('jobs'), list) or jobs.get('total_count') != len(jobs['jobs']): fail('RECOVERY_JOBS_INVALID')
    batch_jobs = {}
    for job in jobs['jobs']:
        match = re.fullmatch(r'batch \((\d{1,2}), batch-\1\.json, 2026Q[23]\)', str(job.get('name')))
        if match:
            if match[1] in batch_jobs: fail('RECOVERY_JOBS_INVALID')
            batch_jobs[match[1]] = job
    if set(batch_jobs) != {str(x) for x in range(32)}: fail('RECOVERY_JOBS_INVALID')
    for index, job in batch_jobs.items():
        if job.get('status') != 'completed' or job.get('conclusion') != ('failure' if index in MISSING else 'success'):
            fail('RECOVERY_JOBS_INVALID')
        if index in MISSING and [s.get('name') for s in job.get('steps', []) if s.get('conclusion') == 'failure'] != ['Validate batch publication']:
            fail('RECOVERY_FAILURE_CLASS_CHANGED')
    listing = api.get(f'actions/runs/{ORIGINAL_RUN}/artifacts?per_page=100'); artifacts = listing.get('artifacts')
    if not isinstance(artifacts, list) or listing.get('total_count') != len(artifacts) or len(artifacts) > 100: fail('RECOVERY_ARTIFACT_LIST_INVALID')
    by_name = {row.get('name'): row for row in artifacts}
    if len(by_name) != len(artifacts): fail('RECOVERY_ARTIFACT_LIST_INVALID')
    expected = {f'part-{x}' for x in range(32) if str(x) not in MISSING}
    if {name for name in by_name if name.startswith('part-')} != expected: fail('RECOVERY_MISSING_PARTS_CHANGED')
    def fetch(name, allowed):
        meta = by_name.get(name)
        if not meta: fail('RECOVERY_ARTIFACT_MISSING')
        return archive_members(api.get(f"actions/artifacts/{meta['id']}/zip", binary=True), meta, original, allowed)
    plan = fetch('plan', lambda p: p in {'matrix.json', 'meta.json'} or bool(re.fullmatch(r'batch-\d{1,2}\.json', p)))
    campaign = fetch('campaign', lambda p: p in {'selection.json', '2026Q2.json', '2026Q3.json'} or bool(re.fullmatch(r'(?:(?:catalogs|locators)/2026Q[23]|catalogs/2026Q[23]-identities/part-[a-f0-9]{64})\.json', p)))
    frozen = []
    for label, members in (('plan', plan), ('campaign', campaign)):
        for name, content in members.items():
            path = output / ('original-' + label) / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
            frozen.append({'name': ('original-' + label) + '/' + name, 'bytes': len(content), 'sha256': digest(content)})
    configs = validate_plan(plan, campaign, output / 'original-campaign')
    from traversal import valid_status, build_status
    preserved = []
    for index in range(32):
        if str(index) in MISSING: continue
        name = f'part-{index}'; members = fetch(name, lambda p: p in {'part.json', 'status.json'})
        if set(members) != {'part.json', 'status.json'} or not valid_status(parse(members['status.json']), configs[str(index)]): fail('RECOVERY_PART_INVALID')
        record = {'batchId': str(index), 'artifactId': by_name[name]['id'], 'archiveSha256': by_name[name]['digest'][7:], 'files': []}
        for filename, content in members.items():
            target = output / 'original-parts' / name / filename; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(content)
            record['files'].append({'name': filename, 'bytes': len(content), 'sha256': digest(content)})
        preserved.append(record)
    progress_path = Path(data) / 'web/data/periods/2026Q2/progress.json'
    if not progress_path.is_file() or progress_path.stat().st_size > 96_000_000: fail('RECOVERY_PROGRESS_INVALID')
    progress = parse(progress_path.read_bytes()); now = dt.datetime.now(dt.timezone.utc)
    manifest = {'schemaVersion': 1, 'status': 'prepared', 'originalRunId': ORIGINAL_RUN, 'originalRunAttempt': 1,
                'originalSourceCommit': ORIGINAL_SOURCE, 'dataCommit': data_commit, 'recoveryRunId': current_id,
                'sourceCommit': env['GITHUB_SHA'], 'preparedAt': now.isoformat(), 'productionWrite': False,
                'marketRequests': 0, 'originalMarketReceipts': 'unknown', 'preserved': preserved, 'frozenFiles': frozen,
                'planArchiveSha256': by_name['plan']['digest'][7:], 'campaignArchiveSha256': by_name['campaign']['digest'][7:], 'batches': []}
    public_write(output / 'result.json', manifest)
    with tempfile.TemporaryDirectory(prefix='report-recovery-') as private:
        for index in MISSING:
            config, skipped = recovery_config(configs[index], progress, now)
            root = output / ('recovered-' + index); config_path = root / 'config.json'
            public_write(config_path, config)
            row = {'batchId': index, 'requested': len(config['funds']), 'deferredCodes': skipped, 'status': 'deferred', 'marketRequests': 0}
            manifest['batches'].append(row); public_write(output / 'result.json', manifest)
            if not config['funds']: continue
            raw_path = Path(private) / (index + '.json')
            collect_reports(config, raw_path, root / 'source-attempts.json')
            if not build_part(raw_path, config_path, root / 'part.json'):
                row['status'] = 'build-failed'; public_write(output / 'result.json', manifest); continue
            part = parse((root / 'part.json').read_bytes())
            status = build_status(config, part)
            if not valid_status(status, config): fail('RECOVERY_STATUS_INVALID')
            public_write(root / 'status.json', status)
            row['status'] = 'recovered'; row['partSha256'] = digest((root / 'part.json').read_bytes())
            public_write(output / 'result.json', manifest)
    for record in preserved:
        for file in record['files']:
            if digest((output / 'original-parts' / ('part-' + record['batchId']) / file['name']).read_bytes()) != file['sha256']:
                fail('RECOVERY_PRESERVATION_FAILED')
    manifest['status'] = 'partial' if any(row['status'] != 'recovered' or row['deferredCodes'] for row in manifest['batches']) else 'recovered'
    public_write(output / 'result.json', manifest)
    validate_output(output)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data'); parser.add_argument('--data-commit')
    parser.add_argument('--output', default='.cache/batch-recovery')
    parser.add_argument('--validate-output', action='store_true')
    args = parser.parse_args(argv)
    try:
        if Path(args.output).resolve() != (Path.cwd() / '.cache/batch-recovery').resolve(): fail('RECOVERY_OUTPUT_INVALID')
        if args.validate_output:
            validate_output(args.output)
            print(json.dumps({'status': 'validated'})); return 0
        if not args.data or not args.data_commit: fail('RECOVERY_IDENTITY_INVALID')
        result = run(args.data, args.output, args.data_commit)
        print(json.dumps({'status': result['status'], 'preservedBatches': len(result['preserved']), 'marketRequests': 0}))
        return 0 if all(row['status'] != 'build-failed' for row in result['batches']) else 1
    except Exception as error:
        code = str(error) if isinstance(error, RecoveryError) else 'RECOVERY_EXECUTION_FAILED'
        if not re.fullmatch(r'RECOVERY_[A-Z_]{1,64}', code): code = 'RECOVERY_EXECUTION_FAILED'
        if Path(args.output).resolve() == (Path.cwd() / '.cache/batch-recovery').resolve():
            try: public_write(Path(args.output) / 'execution-error.json', {'schemaVersion': 1, 'status': 'failed', 'errorCode': code})
            except Exception: pass
        print(json.dumps({'status': 'failed', 'errorCode': code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
