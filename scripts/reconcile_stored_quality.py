"""Recompute stored quality without creating a collection or retry event."""
import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path

import traversal
from publication_guard import inspect_content

PERIODS = {'2026Q2': ('2026-06-30', '2026-03-31'), '2026Q3': ('2026-09-30', '2026-06-30')}
QUALITY = {'fields', 'missingFields', 'status', 'priceConsistencyVersion', 'priceConflictOnly',
           'priceUniverseEmpty', 'priceCoverageVersion', 'managerTableCoverageVersion',
           'strategySectionCoverageVersion', 'pdfRetryable'}
ENRICHMENT = {'status', 'coverageVersion', 'calendarId', 'holdingCount', 'priceAvailable', 'priceComplete',
              'missingStocks', 'missingStockKeys', 'holdingsIdentity', 'consistencyVersion',
              'priceConflicted', 'conflictingStocks', 'referenceResolutions', 'errorCode'}
SOURCE_IDENTITY = ('code', 'companyCode', 'status', 'attribution', 'representativeCode')


def fail(code):
    raise ValueError(code)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()


def fund_map(value):
    if not isinstance(value, list): fail('QUALITY_FUNDS_INVALID')
    result = {}
    for row in value:
        if not isinstance(row, dict) or not traversal.is_code(row.get('code')) or row['code'] in result:
            fail('QUALITY_FUNDS_INVALID')
        result[row['code']] = row
    return result


def protected_fund(value):
    # Exclude derived proofs before copying: stored price references can be
    # large, and none of those discarded values participates in this check.
    result = dict(value)
    for lane, fields in (('quality', QUALITY), ('enrichment', ENRICHMENT)):
        if isinstance(result.get(lane), dict):
            result[lane] = {key: item for key, item in result[lane].items() if key not in fields}
    return copy.deepcopy(result)


def audit_states(rows, runtime_descriptors=None):
    """Restore transient task inputs only in the read-only runtime audit view."""
    states = fund_map(rows)
    if runtime_descriptors is not None and (not isinstance(runtime_descriptors, dict)
            or set(runtime_descriptors) != set(states)):
        fail('QUALITY_AUDIT_DESCRIPTORS_INVALID')
    for code, row in states.items():
        missing = traversal.missing_stock_objects(row.get('enrichment'))
        if missing is None:
            fail('QUALITY_COMPACT_STOCKS_INVALID')
        descriptors = runtime_descriptors[code] if runtime_descriptors is not None else {}
        if not isinstance(descriptors, dict) or set(descriptors) - {'reportLocator', 'strategyClaimsTask'}:
            fail('QUALITY_AUDIT_DESCRIPTORS_INVALID')
        states[code] = {**row, 'enrichment': {**row['enrichment'], 'missingStocks': missing},
                       'reportLocator': descriptors.get('reportLocator'),
                       'strategyClaimsTask': descriptors.get('strategyClaimsTask')}
    return states


def update_existing(target, generated):
    """Known derived keys update; unknown extension fields remain exact."""
    if not isinstance(target, dict): target = {}
    result = copy.deepcopy(target)
    for key, value in generated.items():
        result[key] = update_existing(result.get(key), value) if isinstance(value, dict) else copy.deepcopy(value)
    return result


def reconcile(catalog, previous, config, retained, audit, *, now=None):
    """Pure transformation. Retained must be the current manifest's records."""
    if (not isinstance(previous, dict) or previous.get('schemaVersion') != 1
            or previous.get('phase') != traversal.phase_for(config)
            or not isinstance(audit, dict) or audit.get('phase') != previous['phase']):
        fail('QUALITY_PHASE_INVALID')
    checkpoint = traversal.instant(previous.get('updatedAt'))
    if checkpoint is None: fail('QUALITY_CLOCK_INVALID')
    current = checkpoint if now is None else traversal.utc_now(now)
    if current < checkpoint: fail('QUALITY_VALIDATION_CLOCK_BEFORE_CHECKPOINT')
    old = fund_map(previous.get('funds'))
    _, catalog_funds = traversal.catalog_records(catalog)
    if set(old) != set(catalog_funds): fail('QUALITY_DIRECTORY_CHANGED')
    # Collector upgrades are separate collection events, never a side effect of
    # a stored-data reclassification. All retry clocks are pinned below too.
    scoped = {**config, 'collectorVersion': traversal.collector_version(previous)}
    descriptors = {}
    generated = traversal.merge_progress(catalog, copy.deepcopy(previous), scoped, statuses=(), now=current,
                                         retained=retained, runtime_descriptors=descriptors)
    fresh = fund_map(generated.get('funds'))
    if set(old) != set(fresh): fail('QUALITY_DIRECTORY_CHANGED')
    result = copy.deepcopy(previous)
    changed = preserved = 0
    for row in result['funds']:
        before, candidate = old[row['code']], fresh[row['code']]
        if any(before.get(key) != candidate.get(key) for key in SOURCE_IDENTITY):
            fail('QUALITY_SOURCE_STATE_CHANGED')
        protected_before = encoded(protected_fund(before))
        if protected_before != encoded(protected_fund(candidate)):
            preserved += 1
        for lane, fields in (('quality', QUALITY), ('enrichment', ENRICHMENT)):
            if not isinstance(before.get(lane), dict) or not isinstance(candidate.get(lane), dict):
                fail('QUALITY_LANE_INVALID')
            for key in fields:
                original = row[lane].pop(key, None)
                if key in candidate[lane]:
                    value = candidate[lane][key]
                    row[lane][key] = update_existing(original, value) if isinstance(value, dict) else copy.deepcopy(value)
        # A version migration can calculate "pending" after internally resetting
        # attempts. Derive status again from the preserved original budgets and
        # the newly verified field versions; never persist that implicit reset.
        normalized_quality = traversal.normalize_quality(row['quality'], row['status'], current, code=row['code'])
        for key in ('status', 'missingFields', 'priceConflictOnly'):
            if key in normalized_quality: row['quality'][key] = normalized_quality[key]
            else: row['quality'].pop(key, None)
        normalized_enrichment = traversal.normalize_enrichment(row['enrichment'], row['status'], current)
        for key in ('status', 'errorCode'):
            row['enrichment'][key] = normalized_enrichment[key]
        if encoded(protected_fund(row)) != protected_before:
            fail('QUALITY_HISTORY_CHANGED')
        changed += row != before

    # Aggregates use the final projected rows, including untouched original
    # attempts, rather than the normalizer's clamped or migrated histories.
    rows = result['funds']
    research = copy.deepcopy(generated['research'])
    for key in traversal.QUALITY_FIELDS:
        research['fieldCoverage'][key] = sum(row['status'] != 'not-applicable'
            and row['quality']['fields'][key] in traversal.QUALITY_ACCEPTED for row in rows)
    research['completeFunds'] = sum(row['status'] in traversal.SUCCESS and row['quality']['status'] == 'complete' for row in rows)
    research['incompleteFunds'] = research['applicableFunds'] - research['completeFunds']
    for status in ('pending', 'exhausted'):
        research[status + 'Funds'] = sum(row['status'] in traversal.SUCCESS and row['quality']['status'] == status for row in rows)
    research['priceConflictFunds'] = sum(bool(row['enrichment'].get('priceConflicted')) for row in rows)
    result['research'] = update_existing(previous.get('research'), research)
    enrichment_rows = [row['enrichment'] for row in rows if row['status'] in traversal.SUCCESS
        and row['attribution'] != 'portfolio-evidence' and row['representativeCode'] == row['code']]
    enrichment = copy.deepcopy(generated['enrichment'])
    for status in ('pending', 'complete', 'exhausted'):
        enrichment[status + 'Funds'] = sum(row['status'] == status for row in enrichment_rows)
    enrichment['conflictFunds'] = sum(bool(row.get('priceConflicted')) for row in enrichment_rows)
    enrichment['conflictingStocks'] = sum(row.get('priceConflicted', 0) for row in enrichment_rows)
    for key, value in {'attemptedFunds': sum(row.get('attempts', 0) > 0 for row in enrichment_rows),
                      'attempts': sum(row.get('attempts', 0) for row in enrichment_rows)}.items(): enrichment[key] = value
    result['enrichment'] = update_existing(previous.get('enrichment'), enrichment)
    if not isinstance(previous.get('companies'), list): fail('QUALITY_COMPANIES_INVALID')
    for company in result['companies']:
        members = [row for row in rows if row['companyCode'] == company['code']]
        company['researchComplete'] = sum(row['status'] in traversal.SUCCESS and row['quality']['status'] == 'complete' for row in members)
        for status in ('pending', 'exhausted'):
            company['enrichment' + status.title()] = sum(row['attribution'] != 'portfolio-evidence'
                and row['representativeCode'] == row['code'] and row['enrichment']['status'] == status for row in members)
    # Work counters describe these final rows; they do not change any retry
    # state or continuation flag. In particular, a removed price-only gap
    # must not remain in the compact audit as an executable price task.
    completion = copy.deepcopy(previous.get('completionAudit', {}))
    if not isinstance(completion, dict): fail('QUALITY_AUDIT_INVALID')
    rebuilt_audit = traversal.completion_audit(audit_states(rows, descriptors), scoped, current,
        completion.get('directoryPending', generated['completionAudit']['directoryPending']),
        previous.get('quarterUniverse') or generated['quarterUniverse'])
    original_as_of = completion.get('asOf')
    completion = update_existing(completion, rebuilt_audit)
    if original_as_of is None: completion.pop('asOf', None)
    else: completion['asOf'] = original_as_of
    held = bool(previous.get('continuationHold') or audit.get('continuationHold')
        or previous.get('marketAccounting', {}).get('continuationAllowed') is False
        or audit.get('marketAccounting', {}).get('continuationAllowed') is False)
    if held: completion['complete'] = False
    result['completionAudit'] = completion
    result['completionStatus'] = 'complete' if completion['complete'] else (
        previous['completionStatus'] if previous.get('completionStatus') != 'complete'
        else 'incomplete-running' if previous.get('continueRun') else 'incomplete-exhausted')
    updated_audit = copy.deepcopy(audit)
    updated_audit['research'] = update_existing(audit.get('research'), result['research'])
    audit_completion = copy.deepcopy(audit.get('completionAudit', {}))
    if not isinstance(audit_completion, dict): fail('QUALITY_AUDIT_INVALID')
    audit_as_of = audit_completion.get('asOf')
    audit_completion = update_existing(audit_completion, completion)
    if audit_as_of is None: audit_completion.pop('asOf', None)
    else: audit_completion['asOf'] = audit_as_of
    updated_audit['completionAudit'] = audit_completion
    updated_audit['completionStatus'] = result['completionStatus']
    # No event time, hold, control flag, source observation or unknown ledger is
    # copied from merge_progress. The whitelist above is the entire mutation.
    if result['updatedAt'] != previous['updatedAt'] or updated_audit.get('updatedAt') != audit.get('updatedAt'):
        fail('QUALITY_CLOCK_CHANGED')
    return result, updated_audit, {'status': 'reconciled', 'changedQualityFunds': changed,
        'validatedAt': current.isoformat(),
        'protectedNormalizationsNotApplied': preserved, 'fundCount': len(rows), 'marketDataRequests': 0,
        'sourceRequests': 0, 'modelRequests': 0, 'observationClocksPreserved': True,
        'beforePriceCompleteFunds': previous.get('research', {}).get('fieldCoverage', {}).get('prices', 0),
        'afterPriceCompleteFunds': result['research']['fieldCoverage']['prices'],
        'progressChanged': result != previous, 'auditChanged': updated_audit != audit}


def prepare_quarter(root, quarter, *, now=None):
    folder = Path(root) / 'web/data/periods' / quarter
    previous = traversal.read_json(folder / 'progress.json')
    audit = traversal.read_json(folder / 'audit.json')
    catalog = traversal.read_json(folder / 'catalog.json')
    manifest = traversal.read_json(folder / 'latest.json')
    target, baseline = PERIODS[quarter]
    if manifest.get('targetPeriodEnd') != target or manifest.get('baselinePeriodEnd') != baseline:
        fail('QUALITY_MANIFEST_PHASE_INVALID')
    base = traversal.read_json(Path(root) / 'config/universe.json')
    config = {**base, 'funds': [], 'targetPeriodEnd': target, 'baselinePeriodEnd': baseline,
        'requireReportQuality': previous.get('research', {}).get('enabled') is True,
        'requireHistoricalUniverse': previous.get('quarterUniverse', {}).get('required') is True}
    if config.get('reportLocatorEnabled') is True:
        from report_locators import read_registry, registry_locators
        locator_file = folder / 'report-locators.json'
        if not locator_file.exists() and previous.get('reportLocators', {}).get('readyFunds', 0):
            fail('QUALITY_LOCATOR_REGISTRY_MISSING')
        clock = traversal.instant(previous.get('updatedAt')) if now is None else traversal.utc_now(now)
        registry = read_registry(locator_file, quarter, now=clock)
        _, catalog_funds = traversal.catalog_records(catalog)
        config['reportLocatorCandidates'] = registry_locators(registry, catalog_funds, now=clock)
    result, updated_audit, summary = reconcile(catalog, previous, config,
        traversal.load_retained_funds(folder), audit, now=now)
    outputs = []
    for name, value, original, limit in [('progress.json', result, previous, 96000000),
                                       ('audit.json', updated_audit, audit, 25000000)]:
        data = encoded(value) + b'\n'
        if inspect_content(data, limit): fail('QUALITY_PUBLICATION_REJECTED')
        if value != original: outputs.append((folder / name, data))
    return outputs, {'quarter': quarter, **summary}


def validation_now():
    return dt.datetime.now(dt.timezone.utc)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    parser.add_argument('--quarter', choices=['both', *PERIODS], default='both')
    args = parser.parse_args(argv)
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        fail('QUALITY_RECONCILE_REQUIRES_ACTIONS_MAIN')
    quarters = list(PERIODS) if args.quarter == 'both' else [args.quarter]
    staged, summaries = [], []
    # This is a verification clock, not a newly observed PDF/quote/model event.
    current = validation_now()
    for quarter in quarters:
        outputs, summary = prepare_quarter(args.root, quarter, now=current)
        staged.extend(outputs); summaries.append(summary)
    # Any error propagates to Actions. No caller may persist a partially staged
    # result if the second write fails.
    for file, data in staged:
        temporary = file.with_name(file.name + '.tmp')
        temporary.write_bytes(data); temporary.replace(file)
    output = Path(args.root) / '.cache/stored-price-reference/quality-result.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded({'quarters': summaries, 'marketDataRequests': 0,
        'generatedAt': dt.datetime.now(dt.timezone.utc).isoformat()}) + b'\n')
    print(json.dumps({'status': 'reconciled', 'quarters': summaries}))


if __name__ == '__main__':
    main()
