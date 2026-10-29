"""Quarter-isolated, resumable collection; all bulk work runs on Actions."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from functools import partial

from traversal import atomic_json, read_json, STATUS_SNAPSHOT_ERRORS, source_repair_mode, SOURCE_REPAIR_LANE
from publication_guard import inspect_content, content_limit
from quarter_catalog import (compose_quarter_catalog, freeze_catalog, validate_catalog_lock,
                             normalize_overlay, recover_historical_overlay, combine_historical_overlays,
                             historical_candidate_codes, report_candidate_codes, copy_frozen_catalog)
from report_identity_catalog import supplement_catalog
from report_locators import (refresh_registries, fetch_results as fetch_locator_results, registry_locators, freeze_registry, validate_registry_lock,
                             normalize_locator, MAX_BYTES as LOCATOR_MAX_BYTES)
from source_dates import china_today
from campaign_accounting import collect_batch_accounting, persist_accounting

PERIODS = {
    '2026Q2': ('2026-06-30', '2026-03-31'),
    '2026Q3': ('2026-09-30', '2026-06-30'),
}
# Leave time inside the 40-minute Actions job for checkout/tests, report
# hydration, freezing, validation and uploads. These cap refresh time, not scope.
PLAN_REFRESH_SECONDS = 900
DIRECTORY_REFRESH_SECONDS = 600
LOCATOR_REFRESH_SECONDS = 240
HISTORICAL_REFRESH_SECONDS = 60  # The official downloader enforces this bound.


def plan_stage(stage, started, **values):
    print(json.dumps({'stage': stage, 'elapsedSeconds': round(time.monotonic() - started, 3), **values}), flush=True)


def selected_periods(value):
    if value == 'both': return list(PERIODS)
    if value in PERIODS: return [value]
    raise ValueError('INVALID_PERIOD')


def quarter_config(base, quarter, as_of):
    target, baseline = PERIODS[quarter]
    if dt.date.fromisoformat(as_of).isoformat() != as_of or as_of < target:
        raise ValueError('INVALID_AS_OF')
    claims_quota = base.get('strategyClaimsQuota', 20)
    if type(claims_quota) is not int or not 0 <= claims_quota <= 20:
        raise ValueError('INVALID_STRATEGY_CLAIMS_QUOTA')
    return {**base, 'funds': [], 'quarter': quarter, 'asOf': as_of,
            'targetPeriodEnd': target, 'baselinePeriodEnd': baseline,
            'collectorVersion': 5, 'requireReportQuality': True, 'requireHistoricalUniverse': True,
            'reportLocatorEnabled': base.get('reportLocatorEnabled') is True,
            'strategyClaimsQuota': claims_quota,
            'retryPolicy': {'dailyUntil': '2026-11-30' if quarter == '2026Q3' else None,
                            'intervalDays': 7, 'maxScheduledCycles': 4},
            'scope': f'{quarter} 全目录产品与报告逐项补齐'}


def seed_quarter(data_root, quarter):
    """Copy the legacy Q2 snapshot exactly once, retaining source timestamps."""
    root = Path(data_root)
    target = root / 'periods' / quarter
    target.mkdir(parents=True, exist_ok=True)
    legacy = root / 'latest.json'
    if quarter == '2026Q2' and not (target / 'latest.json').exists() and legacy.exists():
        old = read_json(legacy)
        period = old.get('targetPeriodEnd') or old.get('coverage', {}).get('targetPeriodEnd')
        if period != PERIODS[quarter][0]: raise ValueError('LEGACY_PERIOD_MISMATCH')
        for name in ('latest.json', 'progress.json'):
            if (root / name).is_file(): shutil.copy2(root / name, target / name)
        if (root / 'companies').is_dir(): shutil.copytree(root / 'companies', target / 'companies', dirs_exist_ok=True)
    return target


def directory_refresh_checkpoint(root, current, original, as_of, error_code):
    """Recover only an atomic discovery checkpoint; never invent row attempts."""
    file = Path(root) / 'catalog.json'
    try:
        fresh = read_json(file)
        if fresh == current:
            raise ValueError('NO_NEW_CHECKPOINT')
        from discover import retain_known_catalog, COMPANIES_URL, FUNDS_URL, DIRECTORY_URL
        if (not isinstance(fresh, dict) or fresh.get('schemaVersion') != 1 or fresh.get('asOf') != as_of
                or fresh.get('sourceUrls') != {'companies': COMPANIES_URL, 'funds': FUNDS_URL, 'companyIndex': DIRECTORY_URL}
                or fresh.get('coverage', {}).get('scope') != 'all-directory-companies'
                or not isinstance(fresh.get('companies'), list) or not isinstance(fresh.get('funds'), list)
                or fresh.get('companyCount') != len(fresh['companies']) or fresh.get('fundCount') != len(fresh['funds'])
                or not fresh['companies'] or not fresh['funds']):
            raise ValueError('INVALID_DIRECTORY_CHECKPOINT')
        # Discovery's existing retention gate keeps historical codes and dated
        # ownership. Reapply it before accepting an interrupted refresh.
        fresh = retain_known_catalog(fresh, current)
        old_codes = {row['code'] for row in (current or {}).get('funds', [])}
        if not old_codes.issubset({row['code'] for row in fresh['funds']}):
            raise ValueError('DIRECTORY_CHECKPOINT_REGRESSION')
        atomic_json(file, fresh)
        return {'status': 'refreshed' if fresh['coverage'].get('directoryComplete') else 'partial',
                'asOf': fresh['asOf'], 'errorCode': error_code, 'checkpointRetained': True,
                'staleCompanyCount': fresh['coverage'].get('staleCompanyCount', 0)}
    except Exception:
        # Keep exact old bytes, including all original observation timestamps.
        # A partial prior snapshot is also useful and must remain resumable.
        if current and original is not None:
            file.write_bytes(original)
            return {'status': 'cached', 'asOf': current.get('asOf'), 'errorCode': error_code,
                    'checkpointRetained': False}
        raise ValueError('DIRECTORY_REFRESH_NO_CHECKPOINT') from None


def refresh_directory(root, as_of, *, max_seconds=DIRECTORY_REFRESH_SECONDS):
    if not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool) or not 0 < max_seconds <= DIRECTORY_REFRESH_SECONDS:
        raise ValueError('INVALID_DIRECTORY_REFRESH_BUDGET')
    current = read_json(root / 'catalog.json', optional=True)
    original = (root / 'catalog.json').read_bytes() if current else None
    if current and current.get('asOf') == as_of and current.get('coverage', {}).get('directoryComplete'):
        return {'status': 'current', 'asOf': current['asOf']}
    # The first new date refreshes all institution pages. Subsequent waves reuse
    # that verified directory, while unowned codes remain direct collection tasks.
    try:
        subprocess.run([sys.executable, 'scripts/discover.py', '--output', str(root / 'catalog.json'),
                        '--state', 'raw/catalog-state.json', '--as-of', as_of,
                        '--allow-partial', '--max-companies', '250'], check=True, timeout=max_seconds)
        refreshed = read_json(root / 'catalog.json')
        return {'status': 'refreshed' if refreshed.get('coverage', {}).get('directoryComplete') else 'partial',
                'asOf': refreshed.get('asOf'),
                'staleCompanyCount': refreshed.get('coverage', {}).get('staleCompanyCount', 0)}
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for the discovery child before this
        # branch reads its last atomic checkpoint; queued work stays unattempted.
        return directory_refresh_checkpoint(root, current, original, as_of, 'DIRECTORY_REFRESH_TIME_LIMIT')
    except subprocess.CalledProcessError:
        return directory_refresh_checkpoint(root, current, original, as_of, 'DIRECTORY_REFRESH_FAILED')


def fetch_historical_overlay():
    # The adapter also checks Actions; importing it never downloads the source.
    from historical_universe import fetch_official_overlay
    return fetch_official_overlay()


def historical_error(error):
    code = getattr(error, 'code', None)
    return {'NETWORK_ERROR': 'HISTORICAL_NETWORK_ERROR', 'TIMEOUT': 'HISTORICAL_TIMEOUT',
            'HTTP_REJECTED': 'HISTORICAL_HTTP_REJECTED', 'SOURCE_HASH_MISMATCH': 'HISTORICAL_SOURCE_HASH_MISMATCH',
            'PUBLICATION_REJECTED': 'HISTORICAL_PUBLICATION_REJECTED'}.get(code, 'HISTORICAL_PARSE_FAILED')


def retained_report_candidates(root, quarter):
    """A damaged directory must not remove otherwise retained report records."""
    directory = Path(root) / 'periods' / quarter
    try:
        manifest = read_json(directory / 'latest.json', optional=True)
        if (not isinstance(manifest, dict) or manifest.get('schemaVersion') != 1 or manifest.get('sourceMode') != 'actions'
                or manifest.get('targetPeriodEnd') != PERIODS[quarter][0]): return []
        codes = set(report_candidate_codes(manifest, quarter))
        files = manifest.get('companyFiles', [])
        if not isinstance(files, list) or len(files) > 10000: return sorted(codes)
        for file in files:
            name = file.get('path') if isinstance(file, dict) else None
            if not isinstance(name, str) or not re.fullmatch(r'\./data/companies/[A-Za-z0-9_-]{1,64}\.json', name): continue
            path = directory / name[len('./data/'):]
            if path.parent.is_symlink(): continue
            try: codes.update(report_candidate_codes(read_json(path), quarter))
            except Exception: continue
        return sorted(codes)
    except Exception:
        return []


def quarter_history(root, quarter, *, explicit=None, now=None, allow_fetch=True):
    """Use only validated source rows; a failed refresh never redates cached evidence."""
    if explicit is not None:
        value = explicit.get(quarter)
        if value is not None: normalize_overlay(value, PERIODS[quarter][0], now=now)
        return value, {'status': 'provided' if value is not None else 'not-configured'}
    cached, cache_error, candidates = None, None, set()
    file = Path(root) / 'periods' / quarter / 'catalog.json'
    if file.exists() or file.is_symlink():
        try:
            if file.is_symlink() or file.parent.is_symlink(): raise ValueError('INVALID_HISTORICAL_CACHE')
            raw = read_json(file)
            candidates.update(historical_candidate_codes(raw, quarter))
            cached = recover_historical_overlay(raw, quarter, now=now)
        except Exception:
            cache_error = 'HISTORICAL_CACHE_INVALID'
    if cache_error or not file.exists():
        candidates.update(retained_report_candidates(root, quarter))
    if cached: candidates.difference_update(row['code'] for row in cached['records'])
    def result(value, state):
        if candidates: state['candidateCodes'] = sorted(candidates)
        return value, state
    if quarter != '2026Q2' or os.environ.get('GITHUB_ACTIONS') != 'true' or not allow_fetch:
        state = {'status': 'cached' if cached else 'not-configured' if quarter != '2026Q2' else 'local-only'}
        if quarter == '2026Q2' and not allow_fetch: state.update(status='cached' if cached else 'deferred', errorCode='HISTORICAL_REFRESH_TIME_BUDGET')
        if cache_error: state['cacheErrorCode'] = cache_error
        return result(cached, state)
    try:
        fresh = fetch_historical_overlay()
        clean = normalize_overlay(fresh, PERIODS[quarter][0], now=now)
        if not clean['records']: raise ValueError('EMPTY_HISTORICAL_OVERLAY')
        overlay = combine_historical_overlays(fresh, cached, PERIODS[quarter][0], now=now)
        state = {'status': 'refreshed' if clean['parseStatus'] == 'complete' else 'partial'}
        if cache_error: state['cacheErrorCode'] = cache_error
        return result(overlay, state)
    except Exception as error:
        state = {'status': 'cached' if cached else 'unavailable', 'errorCode': historical_error(error)}
        if cache_error: state['cacheErrorCode'] = cache_error
        return result(cached, state)


def interleave_matrix(matrix, quarters):
    """Give each quarter early runner slots without changing batch identities."""
    groups = {quarter: [] for quarter in quarters}
    for item in matrix: groups[item['quarter']].append(item)
    return [groups[quarter][index] for index in range(max((len(group) for group in groups.values()), default=0))
            for quarter in quarters if index < len(groups[quarter])]


def deployed_continuation_state(root, quarter):
    audit = read_json(Path(root) / 'periods' / quarter / 'audit.json', optional=True)
    if audit is None: return {'batchInputErrors': [], 'continuationHold': None}
    if not isinstance(audit, dict): raise ValueError('INVALID_CONTINUATION_HOLD')
    hold = audit.get('continuationHold')
    if hold is None: return {'batchInputErrors': [], 'continuationHold': None}
    target, baseline = PERIODS[quarter]
    if (audit.get('schemaVersion') != 1 or audit.get('quarter') != quarter
            or audit.get('phase') != {'targetPeriodEnd': target, 'baselinePeriodEnd': baseline}
            or not isinstance(hold, dict) or set(hold) != {'reason', 'quarter', 'sourceRunId', 'batchIds', 'requestAccounting'}
            or hold['reason'] != 'BATCH_ARTIFACTS_UNVERIFIED' or hold['requestAccounting'] != 'unverified'
            or hold['quarter'] != quarter
            or hold['sourceRunId'] is not None and (not isinstance(hold['sourceRunId'], str) or not re.fullmatch(r'[0-9]{1,20}', hold['sourceRunId']))
            or not isinstance(hold['batchIds'], list) or not 1 <= len(hold['batchIds']) <= 32
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,2}', value) or int(value) > 31 for value in hold['batchIds'])
            or len(set(hold['batchIds'])) != len(hold['batchIds'])):
        raise ValueError('INVALID_CONTINUATION_HOLD')
    errors = audit.get('batchInputErrors')
    if (not isinstance(errors, list) or len(errors) != len(hold['batchIds'])
            or any(not isinstance(row, dict) or set(row) != {'batchId', 'quarter', 'errorCode'}
                   or row['quarter'] != quarter or row['batchId'] not in hold['batchIds']
                   or not isinstance(row['errorCode'], str)
                   or row['errorCode'] not in STATUS_SNAPSHOT_ERRORS | {'BATCH_ARTIFACT_MISSING'} for row in errors)
            or len({row['batchId'] for row in errors}) != len(errors)):
        raise ValueError('INVALID_CONTINUATION_HOLD')
    return {'batchInputErrors': errors, 'continuationHold': hold}


def plan(quarter, as_of, data='web/data', shards=16, batch_size=75, refresh=True, historical_overlays=None, source_repair=False):
    started = time.monotonic()
    refresh_deadline = started + PLAN_REFRESH_SECONDS
    if type(source_repair) is not bool: raise ValueError('INVALID_SOURCE_REPAIR_LANE')
    selected = selected_periods(quarter)
    # Planning, Actions output and missing-part reconciliation share one bound.
    # Reject unsupported configurations before fetching or changing any files.
    if (type(shards) is not int or not 1 <= shards <= 32
            or shards * len(selected) > 32
            or type(batch_size) is not int or not 1 <= batch_size <= 100):
        raise ValueError('INVALID_BATCH_CAPACITY')
    overlays = historical_overlays if historical_overlays is not None else {}
    if not isinstance(overlays, dict) or any(name not in PERIODS for name in overlays):
        raise ValueError('INVALID_HISTORICAL_OVERLAYS')
    root = Path(data)
    requested = selected
    holds = {name: state['continuationHold'] for name in requested
             if (state := deployed_continuation_state(root, name))['continuationHold']}
    selected = [name for name in requested if source_repair or name not in holds]
    if not selected:
        directory_status = {'status': 'held'}
        atomic_json('.cache/plan/matrix.json', [])
        atomic_json('.cache/plan/meta.json', {'quarters': [], 'asOf': as_of, 'hasWork': False,
                    'directoryRefresh': directory_status, 'heldQuarters': list(holds), 'continuationHolds': holds})
        atomic_json('.cache/campaign/selection.json', {'quarters': [], 'requestedQuarters': requested, 'asOf': as_of,
                    'selection': quarter, 'marketAccountingVersion': 1,
                    'directoryRefresh': directory_status, 'heldQuarters': list(holds), 'continuationHolds': holds})
        scan_campaign_files()
        return {'status': 'held', 'batchCount': 0, 'quarters': [], 'heldQuarters': list(holds),
                'continuationHolds': holds, 'directoryRefresh': directory_status}
    plan_stage('DIRECTORY_REFRESH_START', started, maxSeconds=DIRECTORY_REFRESH_SECONDS)
    directory_status = refresh_directory(root, as_of, max_seconds=min(DIRECTORY_REFRESH_SECONDS,
        max(0.001, refresh_deadline - time.monotonic()))) if refresh else {'status': 'provided'}
    plan_stage('DIRECTORY_REFRESH_END', started, status=directory_status['status'], errorCode=directory_status.get('errorCode'))
    catalog = read_json(root / 'catalog.json')
    base = read_json('config/universe.json')
    configs = {name: quarter_config(base, name, as_of) for name in selected}
    if source_repair:
        for config in configs.values():
            config.update(collectionLane=SOURCE_REPAIR_LANE, maxPriceSymbols=0, maxPriceSeconds=0)
    locator_seconds = max(0, min(LOCATOR_REFRESH_SECONDS, int(refresh_deadline - time.monotonic())))
    plan_stage('LOCATOR_REFRESH_START', started, maxSeconds=locator_seconds)
    locator_registries = refresh_registries(root, selected, as_of, allow_fetch=refresh and locator_seconds > 0,
        fetcher=partial(fetch_locator_results, max_seconds=locator_seconds) if locator_seconds else None)
    plan_stage('LOCATOR_REFRESH_END', started, status='deferred' if refresh and not locator_seconds else 'observed',
        quarters={name: locator_registries[name]['refreshStatus'] for name in selected})
    scoped_catalogs, identity_parts = {}, {}
    for name in selected:
        allow_history = refresh_deadline - time.monotonic() >= HISTORICAL_REFRESH_SECONDS
        plan_stage('HISTORICAL_REFRESH_START', started, quarter=name, allowed=allow_history)
        overlay, status = quarter_history(root, name, explicit=historical_overlays, allow_fetch=allow_history)
        plan_stage('HISTORICAL_REFRESH_END', started, quarter=name, status=status['status'], errorCode=status.get('errorCode'))
        candidates = status.pop('candidateCodes', [])
        scoped_catalogs[name] = compose_quarter_catalog(catalog, name, overlay, candidates=candidates)
        scoped_catalogs[name]['quarterUniverse']['historicalRefresh'] = status
        scoped_catalogs[name], identity_parts[name] = supplement_catalog(
            scoped_catalogs[name], root / 'periods' / name, name)
    matrix, catalogs, locator_locks = [], {}, {}
    for name in selected:
        output = seed_quarter(root, name)
        config = configs[name]
        scoped_catalog = scoped_catalogs[name]
        locator_locks[name] = freeze_registry(locator_registries[name], name)
        config['reportLocatorRegistry'] = locator_locks[name]
        known_codes = {row['code'] for row in scoped_catalog['funds']}
        config['reportLocatorUniverseCodes'] = sorted({code for value in locator_registries[name]['records'] for code in value['codes']} & known_codes)
        binding = freeze_catalog(scoped_catalog, name, identity_parts=identity_parts[name])
        catalogs[name] = binding
        config.update(catalogLockVersion=1, catalogDigest=binding['sha256'], catalogPath=binding['path'])
        atomic_json(Path('.cache/campaign') / f'{name}.json', config)
        frozen = Path('.cache/campaign') / binding['path']
        copy_frozen_catalog(frozen, output, name)
        # CLI hydration reads existing reports without serializing their raw text
        # into the public batch plan.
        private_plan = Path('.cache') / ('plan-' + name)
        plan_stage('QUARTER_PLAN_START', started, quarter=name)
        subprocess.run([sys.executable, 'scripts/traversal.py', 'plan', '--catalog', str(frozen),
                        '--progress', str(output / 'progress.json'), '--config', f'.cache/campaign/{name}.json',
                        '--data-root', str(output), '--output', str(private_plan),
                        '--shards', str(shards), '--batch-size', str(batch_size)], check=True)
        plan_stage('QUARTER_PLAN_END', started, quarter=name)
        for item in read_json(private_plan / 'matrix.json'):
            batch = read_json(private_plan / item['config'])
            index = str(len(matrix))
            batch['traversal']['batchId'] = index
            filename = f'batch-{index}.json'
            atomic_json(Path('.cache/plan') / filename, batch)
            matrix.append({'id': index, 'config': filename, 'quarter': name})
    matrix = interleave_matrix(matrix, selected)
    atomic_json('.cache/plan/matrix.json', matrix)
    atomic_json('.cache/plan/meta.json', {'quarters': selected, 'asOf': as_of, 'hasWork': bool(matrix), 'directoryRefresh': directory_status,
                'heldQuarters': list(holds), 'continuationHolds': holds})
    atomic_json('.cache/campaign/selection.json', {'quarters': selected, 'asOf': as_of, 'selection': quarter,
                'marketAccountingVersion': 1,
                **({'sourceRepair': True} if source_repair else {}),
                'directoryRefresh': directory_status, 'catalogLockVersion': 1, 'catalogs': catalogs,
                'requestedQuarters': requested, 'heldQuarters': list(holds), 'continuationHolds': holds,
                'strategyClaimsQuotas': {name: configs[name]['strategyClaimsQuota'] for name in selected},
                'reportLocators': locator_locks})
    scan_campaign_files()
    plan_stage('PLAN_COMPLETE', started, batchCount=len(matrix), collectionComplete=False)
    return {'batchCount': len(matrix), 'quarters': selected, 'directoryRefresh': directory_status,
            'heldQuarters': list(holds), 'continuationHolds': holds}


def scan_campaign_files():
    """The recursive artifact includes frozen catalogues, not just root JSON."""
    root = Path('.cache/campaign')
    registry_sizes = {}
    for file in root.rglob('*'):
        if file.is_symlink(): raise ValueError('PLAN_PUBLICATION_REJECTED')
        if file.is_dir(): continue
        relative = file.relative_to(root).as_posix()
        match = re.fullmatch(r'catalogs/(2026Q[23])\.json', relative)
        identity = re.fullmatch(r'catalogs/(2026Q[23])-identities/part-[a-f0-9]{64}\.json', relative)
        locator = re.fullmatch(r'locators/(2026Q[23])\.json', relative)
        if file.suffix != '.json' or file.parent.is_symlink() or '/' in relative and not (match or identity or locator):
            raise ValueError('PLAN_PUBLICATION_REJECTED')
        policy_name = f'web/data/periods/{match[1]}/catalog.json' if match else f'config/{file.name}'
        maximum = LOCATOR_MAX_BYTES if locator else 5_000_000 if identity else content_limit(policy_name)
        if file.stat().st_size > maximum or inspect_content(file.read_bytes(), maximum):
            raise ValueError('PLAN_PUBLICATION_REJECTED')
        if identity:
            count, total = registry_sizes.get(identity[1], (0, 0))
            registry_sizes[identity[1]] = (count + 1, total + file.stat().st_size)
            if count + 1 > 128 or total + file.stat().st_size > 256_000_000:
                raise ValueError('PLAN_PUBLICATION_REJECTED')


def frozen_catalogs_for_publish(selection, matrix, root):
    """Validate every selected lock before changing any published quarter.

    Legacy plans have no lock at all. Recover them once from the supplied
    directory artifact and explicitly retain an unverified historical scope.
    A partial or damaged modern lock never takes the legacy recovery path.
    """
    quarters = selection.get('quarters')
    if not isinstance(quarters, list) or not quarters or len(set(quarters)) != len(quarters) or any(q not in PERIODS for q in quarters):
        raise ValueError('INVALID_PERIOD')
    configs = {q: read_json(Path('.cache/campaign') / f'{q}.json') for q in quarters}
    lock_keys = {'catalogLockVersion', 'catalogDigest', 'catalogPath'}
    legacy = ('catalogLockVersion' not in selection and 'catalogs' not in selection
              and all(not lock_keys.intersection(config) for config in configs.values()))
    bindings = selection.get('catalogs')
    if not legacy and (selection.get('catalogLockVersion') != 1 or not isinstance(bindings, dict) or set(bindings) != set(quarters)):
        raise ValueError('INVALID_CATALOG_LOCK')
    paths, pending, batches = {}, {}, []
    for quarter, config in configs.items():
        if config.get('targetPeriodEnd') != PERIODS[quarter][0] or config.get('baselinePeriodEnd') != PERIODS[quarter][1]:
            raise ValueError('CATALOG_PERIOD_MISMATCH')
        if legacy:
            pending[quarter] = compose_quarter_catalog(read_json(root / 'catalog.json'), quarter, legacy=True)
        else:
            paths[quarter] = validate_catalog_lock(quarter, config, bindings[quarter])
    if not isinstance(matrix, list): raise ValueError('INVALID_PLAN')
    for item in matrix:
        if (not isinstance(item, dict) or item.get('quarter') not in configs or not isinstance(item.get('config'), str)
                or not re.fullmatch(r'batch-\d+\.json', item['config'])):
            raise ValueError('INVALID_PLAN')
        file = Path('.cache/plan') / item['config']
        batch = read_json(file)
        quarter = item['quarter']
        if (batch.get('targetPeriodEnd') != PERIODS[quarter][0] or batch.get('baselinePeriodEnd') != PERIODS[quarter][1]
                or (legacy and lock_keys.intersection(batch))):
            raise ValueError('INVALID_CATALOG_LOCK')
        if not legacy and (batch.get('catalogLockVersion') != 1 or batch.get('catalogDigest') != bindings[quarter]['sha256']
                or batch.get('catalogPath') != bindings[quarter]['path'] or batch.get('requireHistoricalUniverse') is not True):
            raise ValueError('INVALID_CATALOG_LOCK')
        batches.append((file, quarter, batch))
    if legacy:
        bindings = {q: freeze_catalog(catalog, q) for q, catalog in pending.items()}
        for quarter, config in configs.items():
            config.update(requireHistoricalUniverse=True, catalogLockVersion=1, catalogDigest=bindings[quarter]['sha256'],
                          catalogPath=bindings[quarter]['path'], legacyPlanRecovered=True)
            atomic_json(Path('.cache/campaign') / f'{quarter}.json', config)
            paths[quarter] = validate_catalog_lock(quarter, config, bindings[quarter])
        for file, quarter, batch in batches:
            batch.update(requireHistoricalUniverse=True, catalogLockVersion=1, catalogDigest=bindings[quarter]['sha256'],
                         catalogPath=bindings[quarter]['path'], legacyPlanRecovered=True)
            atomic_json(file, batch)
        selection.update(catalogLockVersion=1, catalogs=bindings, legacyPlanRecovered=True)
        atomic_json('.cache/campaign/selection.json', selection)
    scan_campaign_files()
    return paths


def frozen_locators_for_publish(selection, matrix):
    bindings = selection.get('reportLocators')
    if bindings is None:
        # Previously queued campaigns have no supplemental source registry.
        if any('reportLocatorRegistry' in read_json(Path('.cache/campaign') / (q + '.json')) for q in selection.get('quarters', [])):
            raise ValueError('INVALID_LOCATOR_LOCK')
        return {}
    if not isinstance(bindings, dict) or set(bindings) != set(selection.get('quarters', [])): raise ValueError('INVALID_LOCATOR_LOCK')
    if not isinstance(selection.get('catalogs'), dict) or set(selection['catalogs']) != set(bindings): raise ValueError('INVALID_CATALOG_LOCK')
    paths, locators = {}, {}
    for quarter, binding in bindings.items():
        file, registry = validate_registry_lock(binding, quarter)
        config = read_json(Path('.cache/campaign') / (quarter + '.json'))
        if config.get('reportLocatorRegistry') != binding: raise ValueError('INVALID_LOCATOR_LOCK')
        codes = config.get('reportLocatorUniverseCodes')
        if not isinstance(codes, list) or 'reportLocatorCandidates' in config: raise ValueError('INVALID_LOCATOR_LOCK')
        catalog_path = validate_catalog_lock(quarter, config, selection['catalogs'][quarter])
        catalog = read_json(catalog_path)
        known_codes = {row['code'] for row in catalog['funds']}
        expected_codes = sorted({code for value in registry['records'] for code in value['codes']} & known_codes)
        if codes != expected_codes: raise ValueError('INVALID_LOCATOR_LOCK')
        expected = registry_locators(registry, codes, as_of=config.get('asOf')) if config.get('reportLocatorEnabled') is True else {}
        paths[quarter], locators[quarter] = file, expected
    for item in matrix:
        batch = read_json(Path('.cache/plan') / item['config']); quarter = item['quarter']
        if ('reportLocatorRegistry' in batch or 'reportLocatorCandidates' in batch
                or 'reportLocatorUniverseCodes' in batch): raise ValueError('INVALID_LOCATOR_LOCK')
        for fund in batch.get('funds', []):
            value = fund.get('reportLocator')
            if value is not None and (normalize_locator(value, fund.get('code'), PERIODS[quarter][0]) != value
                                      or value != locators[quarter].get(fund.get('code'))): raise ValueError('INVALID_LOCATOR_LOCK')
    return paths


def frozen_claims_quotas_for_publish(selection, matrix):
    quotas = selection.get('strategyClaimsQuotas')
    if quotas is None:
        if any('strategyClaimsOnly' in fund for item in matrix
               for fund in read_json(Path('.cache/plan') / item['config']).get('funds', [])):
            raise ValueError('INVALID_STRATEGY_CLAIMS_QUOTA')
        return
    if not isinstance(quotas, dict) or set(quotas) != set(selection.get('quarters', [])):
        raise ValueError('INVALID_STRATEGY_CLAIMS_QUOTA')
    counts = {quarter: 0 for quarter in quotas}
    for quarter, quota in quotas.items():
        if (type(quota) is not int or not 0 <= quota <= 20 or quarter not in PERIODS
                or read_json(Path('.cache/campaign') / (quarter + '.json')).get('strategyClaimsQuota') != quota):
            raise ValueError('INVALID_STRATEGY_CLAIMS_QUOTA')
    for item in matrix:
        quarter = item.get('quarter')
        batch = read_json(Path('.cache/plan') / item['config'])
        codes = [fund['code'] for fund in batch.get('funds', []) if 'strategyClaimsOnly' in fund]
        if (quarter not in quotas or batch.get('strategyClaimsQuota') != quotas[quarter]
                or batch.get('traversal', {}).get('strategyClaimsCodes', []) != codes):
            raise ValueError('INVALID_STRATEGY_CLAIMS_QUOTA')
        counts[quarter] += len(codes)
    if any(counts[quarter] > quotas[quarter] for quarter in quotas):
        raise ValueError('INVALID_STRATEGY_CLAIMS_QUOTA')


def batch_input_state(reconciliation, matrix):
    """Missing artifacts leave request accounting unknown, not a free retry."""
    quarters = {item['id']: item.get('quarter') for item in matrix}
    rows = []
    for item in reconciliation.get('inputErrors', []):
        if item['batchId'] not in quarters: raise ValueError('INVALID_RECONCILIATION')
        rows.append({'batchId': item['batchId'], 'quarter': quarters[item['batchId']], 'errorCode': item['errorCode']})
    for identifier in reconciliation.get('missingPartIds', []):
        if identifier not in quarters: raise ValueError('INVALID_RECONCILIATION')
        rows.append({'batchId': identifier, 'quarter': quarters[identifier], 'errorCode': 'BATCH_ARTIFACT_MISSING'})
    rows.sort(key=lambda row: int(row['batchId']))
    run_id = os.environ.get('GITHUB_RUN_ID')
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9]{1,20}', run_id): run_id = None
    hold = {'reason': 'BATCH_ARTIFACTS_UNVERIFIED', 'quarter': None, 'sourceRunId': run_id,
            'batchIds': [row['batchId'] for row in rows],
            'requestAccounting': 'unverified'} if rows else None
    return {'batchInputErrors': rows, 'continuationHold': hold,
            'missingPartCount': reconciliation.get('missingPartCount', 0), 'missingPartIds': reconciliation.get('missingPartIds', []),
            'inputErrorCount': reconciliation.get('inputErrorCount', 0)}


def source_repair_for_publish(selection, matrix):
    enabled = selection.get('sourceRepair', False)
    if type(enabled) is not bool: raise ValueError('INVALID_SOURCE_REPAIR_LANE')
    for quarter in selection['quarters']:
        if source_repair_mode(read_json(f'.cache/campaign/{quarter}.json')) != enabled:
            raise ValueError('INVALID_SOURCE_REPAIR_LANE')
    for item in matrix:
        if source_repair_mode(read_json(Path('.cache/plan') / item['config'])) != enabled:
            raise ValueError('INVALID_SOURCE_REPAIR_LANE')
    return enabled


def publish(data='web/data', parts='.cache/parts'):
    root, part_root = Path(data), Path(parts)
    selection = read_json('.cache/campaign/selection.json')
    matrix = read_json('.cache/plan/matrix.json')
    if selection.get('quarters') == []:
        requested = selected_periods(selection.get('selection'))
        holds = {name: deployed_continuation_state(root, name)['continuationHold'] for name in requested}
        if matrix or not all(holds.values()) or selection.get('continuationHolds') != holds:
            raise ValueError('INVALID_CONTINUATION_HOLD')
        result = {'status': 'held', 'quarters': [], 'continueRun': False, 'selection': selection['selection'],
                  'heldQuarters': requested, 'continuationHolds': holds}
        atomic_json('.cache/campaign/result.json', result)
        return result
    prior_input_states = {name: deployed_continuation_state(root, name) for name in selection['quarters']}
    source_repair = source_repair_for_publish(selection, matrix)
    frozen_claims_quotas_for_publish(selection, matrix)
    locator_paths = frozen_locators_for_publish(selection, matrix)
    catalogs = frozen_catalogs_for_publish(selection, matrix, root)
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location('complete_status', 'scripts/complete-status.py')
    module = module_from_spec(spec); spec.loader.exec_module(module)
    try:
        reconciliation = module.reconcile('.cache/plan', str(part_root))
    except module.BatchStatusInputError as error:
        # No production merge can admit an existing invalid batch artifact.
        # Still retain a fixed, public diagnostic for the workflow/recovery UI.
        atomic_json('.cache/campaign/result.json', {
            'status': 'batch-input-error', 'errorCode': 'BATCH_STATUS_INPUT_ERROR', 'quarters': [],
            'continueRun': False, 'selection': selection['selection'],
            **batch_input_state(error.summary, matrix)})
        raise
    input_state = batch_input_state(reconciliation, matrix)
    accounting_required = (source_repair or selection.get('marketAccountingVersion') == 1
                           or os.environ.get('CAMPAIGN_MARKET_ACCOUNTING') == 'required')
    accounting = collect_batch_accounting(matrix) if accounting_required else None
    for name, prior in prior_input_states.items():
        new_errors = [item for item in input_state['batchInputErrors'] if item['quarter'] == name]
        if not source_repair and prior['continuationHold'] and new_errors and new_errors != prior['batchInputErrors']:
            raise ValueError('CONTINUATION_HOLD_REQUIRES_RECOVERY')
    summaries = []
    for quarter in selection['quarters']:
        if quarter not in PERIODS: raise ValueError('INVALID_PERIOD')
        output = seed_quarter(root, quarter)
        if quarter in locator_paths:
            shutil.copy2(locator_paths[quarter], output / 'report-locators.json')
        copy_frozen_catalog(catalogs[quarter], output, quarter)
        scoped_parts = Path('.cache') / ('parts-' + quarter)
        scoped_parts.mkdir(parents=True, exist_ok=True)
        for item in matrix:
            if item['quarter'] == quarter:
                src = part_root / ('part-' + item['id'])
                if src.is_dir(): shutil.copytree(src, scoped_parts / src.name, dirs_exist_ok=True)
        config = f'.cache/campaign/{quarter}.json'
        subprocess.run(['node', 'scripts/merge-parts.js', '--parts', str(scoped_parts), '--data', str(output),
                        '--catalog', str(catalogs[quarter]), '--config', config,
                        *(['--reuseMarketPrices', 'false'] if source_repair else [])], check=True)
        subprocess.run([sys.executable, 'scripts/traversal.py', 'merge', '--parts', str(scoped_parts),
                        '--catalog', str(catalogs[quarter]), '--progress', str(output / 'progress.json'),
                        '--config', config, '--data-root', str(output)], check=True)
        progress = read_json(output / 'progress.json')
        quarter_input_errors = [item for item in input_state['batchInputErrors'] if item['quarter'] == quarter]
        quarter_hold = {**input_state['continuationHold'], 'quarter': quarter,
                        'batchIds': [item['batchId'] for item in quarter_input_errors]} if quarter_input_errors else None
        prior = prior_input_states[quarter]
        if prior['continuationHold']:
            # A new ordinary publish is not an authorization to discard unknown
            # requests from an earlier run. Verified recovery admission is separate.
            quarter_input_errors = prior['batchInputErrors']
            quarter_hold = prior['continuationHold']
        market_accounting = persist_accounting(output, quarter, accounting.get(quarter, [])) if accounting is not None else None
        accounting_allows_continuation = market_accounting is None or market_accounting['continuationAllowed']
        source_lane = None
        if source_repair:
            entries = accounting.get(quarter, [])
            verified_zero = (len(entries) == sum(item['quarter'] == quarter for item in matrix)
                             and all(row['status'] == 'complete' and row['counts'] is not None
                                     and all(value == 0 for value in row['counts'].values()) for row, _ in entries))
            current_errors = [item for item in input_state['batchInputErrors'] if item['quarter'] == quarter]
            source_lane = {'lane': SOURCE_REPAIR_LANE, 'currentMarketAccountingVerifiedZero': verified_zero,
                           'batchInputErrors': current_errors,
                           'continueRun': progress.get('sourceRepairContinueRun') is True and verified_zero and not current_errors}
        atomic_json(output / 'audit.json', {
            'schemaVersion': 1, 'quarter': quarter, 'phase': progress.get('phase'),
            'updatedAt': progress.get('updatedAt'), 'completionStatus': progress.get('completionStatus'),
            'totalFunds': progress.get('totalFunds'), 'processedFunds': progress.get('processedFunds'),
            'failedFunds': progress.get('failedFunds'), 'pendingFunds': progress.get('pendingFunds'),
            'notApplicableFunds': progress.get('notApplicableFunds'), 'research': progress.get('research'),
            'continueRun': bool(progress.get('continueRun')) and not quarter_hold and accounting_allows_continuation,
            'nextRetryAt': progress.get('nextRetryAt'),
            'completionAudit': progress.get('completionAudit'),
            'strategyClaims': progress.get('strategyClaims'),
            'batchInputErrors': quarter_input_errors, 'continuationHold': quarter_hold,
            **({'marketAccounting': market_accounting} if market_accounting is not None else {}),
            **({'sourceRepair': source_lane} if source_lane is not None else {}),
        })
        summary = {'quarter': quarter, **{k: progress.get(k) for k in (
            'totalFunds', 'processedFunds', 'failedFunds', 'pendingFunds', 'research', 'strategyClaims', 'completionStatus', 'continueRun')},
            'batchInputErrors': quarter_input_errors, 'continuationHold': quarter_hold,
            **({'marketAccounting': market_accounting} if market_accounting is not None else {})}
        if source_lane is not None: summary['sourceRepair'] = source_lane
        if quarter_hold or not accounting_allows_continuation: summary['continueRun'] = False
        # The analyzer reads its own compact progress view. Do not retain the
        # whole per-product checkpoint in this parent process during analysis
        # or while merging the next quarter.
        del progress
        subprocess.run(['node', 'scripts/analyze-data.js', '--data', str(output),
                        '--output', str(output / 'analysis.json')], check=True)
        summaries.append(summary)
    periods = []
    for quarter, (target, baseline) in PERIODS.items():
        directory = root / 'periods' / quarter
        if (directory / 'latest.json').exists() or (directory / 'progress.json').exists():
            periods.append({'id': quarter, 'label': quarter, 'path': f'./data/periods/{quarter}/',
                            'targetPeriodEnd': target, 'baselinePeriodEnd': baseline})
    default = '2026Q3' if any(p['id'] == '2026Q3' for p in periods) else '2026Q2'
    atomic_json(root / 'periods.json', {'schemaVersion': 1, 'defaultPeriod': default, 'periods': periods})
    selected_holds = {item['quarter']: item['continuationHold'] for item in summaries if item['continuationHold']}
    if selected_holds and not input_state['continuationHold']:
        input_state['batchInputErrors'] = [row for item in summaries for row in item['batchInputErrors']]
        input_state['continuationHold'] = next(iter(selected_holds.values()))
    result = {'quarters': summaries, 'continueRun': not any(s['continuationHold'] for s in summaries) and any(s['continueRun'] for s in summaries),
              'directoryRefresh': selection.get('directoryRefresh'), 'selection': selection['selection'], **input_state,
              'heldQuarters': list(dict.fromkeys([*selection.get('heldQuarters', []), *selected_holds])),
              'continuationHolds': {**selection.get('continuationHolds', {}), **selected_holds}}
    # A mixed-quarter continuation would select the unresolved scope again.
    # Persist the verified source results, but require explicit review before
    # any automatic next run while an accounting batch remains unresolved.
    if any(not s.get('marketAccounting', {}).get('continuationAllowed', True) for s in summaries):
        result['continueRun'] = False
    if source_repair:
        result['sourceRepair'] = True
        result['continueRun'] = (all(s['sourceRepair']['currentMarketAccountingVerifiedZero']
                                    and not s['sourceRepair']['batchInputErrors'] for s in summaries)
                                 and any(s['sourceRepair']['continueRun'] for s in summaries))
    atomic_json('.cache/campaign/result.json', result)
    summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_file:
        with open(summary_file, 'a') as handle:
            handle.write('## Collection coverage\n\n')
            if input_state['continuationHold']:
                handle.write(f"Automatic continuation held: {len(input_state['batchInputErrors'])} batch artifacts missing; request accounting is unverified.\n\n")
            unresolved = sum(s.get('marketAccounting', {}).get('unresolvedBatchCount', 0) for s in summaries)
            if unresolved:
                handle.write(f"Automatic continuation held: {unresolved} market-accounting batches remain unresolved; source observations are retained.\n\n")
            for s in summaries:
                research = s.get('research') or {}
                handle.write(f"- {s['quarter']}: {s['totalFunds']} codes; {s['processedFunds']} with holdings/no-equity evidence; "
                             f"{research.get('completeFunds', 0)} fully evidenced; {s['failedFunds']} failed; "
                             f"{s['pendingFunds']} pending. Status: {s.get('completionStatus', 'incomplete')}.\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['plan', 'publish'])
    parser.add_argument('--quarter', choices=['both', *PERIODS], default='both')
    parser.add_argument('--as-of', default=china_today())
    parser.add_argument('--shards', type=int, default=16)
    parser.add_argument('--batch-size', type=int, default=75)
    parser.add_argument('--source-repair', action='store_true')
    parser.add_argument('--historical-overlays', help='JSON object mapping quarter IDs to normalized historical overlays')
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true': raise ValueError('BATCH_REQUIRES_ACTIONS')
    overlays = read_json(args.historical_overlays) if args.historical_overlays else None
    result = plan(args.quarter, args.as_of, shards=args.shards, batch_size=args.batch_size, historical_overlays=overlays,
                  source_repair=args.source_repair) if args.command == 'plan' else publish()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try: main()
    except Exception:
        print(json.dumps({'status': 'failed', 'errorCode': 'CAMPAIGN_FAILED'}))
        raise SystemExit(1) from None
