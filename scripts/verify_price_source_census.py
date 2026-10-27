"""Fixed, read-only price-source census and restoration of existing proof.

This workflow is not a recheck-ledger successor. It never requests market data,
changes quotes, or writes an attempt ledger. Old proof and current data have
separate immutable scopes; matching extremes do not cover a new observation.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import zipfile

import price_conflicts as conflicts
import price_daily_evidence as daily
import price_consistency as consistency
import restore_price_conflict_ledger as restore
from publication_cache import read_bounded_command
from publication_guard import inspect_content

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = '.github/workflows/verify-price-source-census.yml'
DATA_COMMIT = '383e7996dcbe4b6a8ff4535c8f3dc8292abb3bda'
QUARTER = '2026Q3'
KEY = 'CN|000636|CNY|unadjusted|2026-07-01|2026-09-30|exchange-sessions-2026-q2-q3-v1'
PROOF = {'runId': 40406248667, 'runAttempt': 1, 'sourceCommit': 'a3eea5ba72e28354c5a89c603980284ab28634f2',
         'dataCommit': '63ab7e38ce388f246d80588146081991a88ff623', 'artifactId': 12691448452,
         'archiveSha256': '5534fb649d9087cc915f9c9d17607d74b5caf7360d62375f289e0278b9e100f7',
         'evidenceSha256': 'f9291f0e01d0b91bf16d41e43740415974cc741695d50aabfd6b7d85b772a017'}
MAX_CENSUS_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 512_000
ERRORS = {'CENSUS_REQUIRES_ACTIONS', 'CENSUS_PROOF_IDENTITY_INVALID', 'CENSUS_PROOF_ARCHIVE_INVALID',
          'CENSUS_PROVENANCE_INVALID', 'CENSUS_PROOF_LEDGER_INVALID', 'CENSUS_PROOF_INVALID',
          'CENSUS_DATA_INVALID', 'CENSUS_SCAN_FAILED', 'CENSUS_SCAN_TIMEOUT', 'CENSUS_INPUT_INVALID',
          'CENSUS_OUTPUT_INVALID', 'CENSUS_API_UNAVAILABLE'}


class CensusError(ValueError):
    pass


def fail(code):
    raise CensusError(code if code in ERRORS else 'CENSUS_INPUT_INVALID')


def encoded(value):
    return conflicts.encoded(value)


def parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: fail('CENSUS_INPUT_INVALID')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: fail('CENSUS_INPUT_INVALID'))
    except (ValueError, TypeError, UnicodeError):
        fail('CENSUS_INPUT_INVALID')


def archive_members(raw, artifact):
    """Validate the immutable archive; parse members individually, not all at once."""
    if (not isinstance(raw, bytes) or len(raw) != artifact.get('size_in_bytes') or len(raw) > restore.MAX_ARCHIVE
            or hashlib.sha256(raw).hexdigest() != PROOF['archiveSha256']
            or artifact.get('digest') != 'sha256:' + PROOF['archiveSha256']): fail('CENSUS_PROOF_ARCHIVE_INVALID')
    allowed = {'provenance.json', 'result.json'} | {f'{quarter}/{name}.json' for quarter in conflicts.PHASES
        for name in ('registry', 'plan', 'ledger', 'input-ledger', 'receipts', 'execution')}
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        entries = archive.infolist()
        if (len(entries) > len(allowed) or len({row.filename for row in entries}) != len(entries)
                or sum(row.file_size for row in entries) > restore.MAX_TOTAL): fail('CENSUS_PROOF_ARCHIVE_INVALID')
        for row in entries:
            if (row.filename not in allowed or row.is_dir() or row.file_size > conflicts.MAX_BYTES
                    or row.external_attr >> 16 & 0o170000 == 0o120000 or row.flag_bits & 1
                    or row.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                fail('CENSUS_PROOF_ARCHIVE_INVALID')
        return archive
    except (OSError, ValueError, zipfile.BadZipFile): fail('CENSUS_PROOF_ARCHIVE_INVALID')


def validate_proof(artifact, prior, raw, now=None):
    """Validate only this proof's original scope; no new ledger lineage is invented."""
    current = conflicts.clock(now)
    try:
        restore.run_identity(prior, run_id=PROOF['runId'], source_commit=PROOF['sourceCommit'])
        restore.artifact_identity(artifact, prior)
        if (prior['run_attempt'] != PROOF['runAttempt'] or prior['conclusion'] != 'success'
                or artifact['id'] != PROOF['artifactId']): fail('CENSUS_PROOF_IDENTITY_INVALID')
        archive = archive_members(raw, artifact)
        with archive:
            def member(name):
                try: return parse_json(archive.read(name))
                except (KeyError, OSError, ValueError, zipfile.BadZipFile): fail('CENSUS_PROOF_ARCHIVE_INVALID')
            provenance = member('provenance.json')
            fields = {'schemaVersion', 'kind', 'repository', 'workflowPath', 'runId', 'runAttempt', 'sourceCommit',
                      'dataCommit', 'restoredFrom', 'inputLedgerHashes', 'bootstrap'}
            if (not isinstance(provenance, dict) or set(provenance) != fields or provenance.get('schemaVersion') != 1
                    or provenance.get('kind') != 'price-conflict-provenance' or provenance.get('repository') != restore.REPOSITORY
                    or provenance.get('workflowPath') != restore.WORKFLOW
                    or any(provenance.get(key) != PROOF[key] for key in ('runId', 'runAttempt', 'sourceCommit', 'dataCommit'))
                    or provenance.get('bootstrap') != conflicts.LEDGER_BOOTSTRAP
                    or not isinstance(provenance.get('inputLedgerHashes'), dict)
                    or set(provenance['inputLedgerHashes']) != set(conflicts.PHASES)):
                fail('CENSUS_PROVENANCE_INVALID')
            # This reconstructs the original run's restoration metadata only.
            # The current census run is deliberately absent from ledger state.
            conflicts.normalize_restored_state({'schemaVersion': 1, 'kind': 'price-conflict-restored-state',
                'repository': restore.REPOSITORY, 'workflowPath': restore.WORKFLOW,
                'currentRun': {key: PROOF[key] for key in ('runId', 'runAttempt', 'sourceCommit')},
                'currentDataCommit': PROOF['dataCommit'], 'restoredFrom': provenance['restoredFrom'],
                'bootstrap': conflicts.LEDGER_BOOTSTRAP,
                'ledgers': {quarter: conflicts.empty_ledger(quarter) for quarter in conflicts.PHASES}}, current)
            ledgers, inherited, ledger_hashes = {}, {}, {}
            for quarter in conflicts.PHASES:
                inherited[quarter] = conflicts.normalize_ledger(member(f'{quarter}/input-ledger.json'), quarter, current)
                ledgers[quarter] = conflicts.normalize_ledger(member(f'{quarter}/ledger.json'), quarter, current)
                if hashlib.sha256(encoded(inherited[quarter])).hexdigest() != provenance['inputLedgerHashes'][quarter]:
                    fail('CENSUS_PROOF_LEDGER_INVALID')
                if conflicts.union_ledgers(inherited[quarter], ledgers[quarter], quarter, current) != ledgers[quarter]:
                    fail('CENSUS_PROOF_LEDGER_INVALID')
                ledger_hashes[quarter] = hashlib.sha256(encoded(ledgers[quarter])).hexdigest()
            original = member(f'{QUARTER}/registry.json')
            rows = [row for row in original.get('records', []) if isinstance(row, dict) and row.get('key') == KEY]
            if (original.get('kind') != 'price-conflict-registry' or original.get('quarter') != QUARTER
                    or original.get('dataCommit') != PROOF['dataCommit'] or len(rows) != 1): fail('CENSUS_PROOF_INVALID')
            # Target-only reconstruction validates exact original facts and their
            # digest without claiming that unrelated registry keys were audited.
            subset = conflicts.registry_from_observations({**original, **original['scanSummary'],
                'kind': 'price-conflict-observations', 'records': [{'key': KEY, 'groups': rows[0]['facts']}]}, current)
            if subset['records'] != rows: fail('CENSUS_PROOF_INVALID')
            del original
            plan = member(f'{QUARTER}/plan.json'); conflicts.validate_plan(plan, subset, current)
            receipts = member(f'{QUARTER}/receipts.json')
            if (len(plan['items']) != 1 or plan['items'][0]['key'] != KEY or plan['items'][0]['round'] != 2
                    or plan.get('target') != {'quarter': QUARTER, 'key': KEY, 'digest': rows[0]['digest']}
                    or not isinstance(receipts, list) or len(receipts) != 1): fail('CENSUS_PROOF_INVALID')
            merged = conflicts.merge_receipts(subset, inherited[QUARTER], plan, receipts, now=current)
            if merged != ledgers[QUARTER]: fail('CENSUS_PROOF_LEDGER_INVALID')
            history = next(history for entry in merged['entries'] if entry['key'] == KEY
                           for history in entry['digests'] if history['digest'] == rows[0]['digest'])
            if len(history['rounds']) != 2 or receipts[0] not in history['rounds']: fail('CENSUS_PROOF_LEDGER_INVALID')
            # The non-target quarter inherited its exact original ledger and did
            # not make a request in this fixed targeted run.
            other = '2026Q2'; other_plan = member(f'{other}/plan.json')
            if (other_plan.get('quarter') != other or other_plan.get('items') != []
                    or member(f'{other}/receipts.json') != [] or ledgers[other] != inherited[other]):
                fail('CENSUS_PROOF_LEDGER_INVALID')
            execution = member(f'{QUARTER}/execution.json')
            if (execution.get('schemaVersion') != 1 or execution.get('kind') != 'price-conflict-execution'
                    or execution.get('quarter') != QUARTER or execution.get('receipts') != receipts or not isinstance(execution.get('dailyEvidence'), list)
                    or len(execution['dailyEvidence']) != 1 or execution.get('productionDataModified') is not False
                    or execution.get('automaticResolution') is not False): fail('CENSUS_PROOF_INVALID')
            evidence = daily.normalize_evidence(execution['dailyEvidence'][0], plan['items'][0], receipts[0])
            if (evidence['evidenceSha256'] != PROOF['evidenceSha256'] or evidence['key'] != KEY
                    or evidence['comparison']['status'] != 'daily-float32-compatible'
                    or evidence['comparison']['bothCompleteCalendars'] is not True
                    or evidence['comparison']['nonFloat32CellCount'] != 0
                    or {source['provider'] for source in evidence['sources']} != {'eastmoney', 'yahoo'}):
                fail('CENSUS_PROOF_INVALID')
            summary = member('result.json')
            if (summary.get('kind') != 'price-conflict-run' or summary.get('status') != 'complete'
                    or summary.get('target') != plan['target'] or summary.get('productionDataModified') is not False
                    or summary.get('automaticResolution') is not False): fail('CENSUS_PROOF_INVALID')
            facts = {fact['factId']: fact for fact in rows[0]['facts']}
            covered = []
            for request in receipts[0]['requests']:
                fact = facts.get(request['factId'])
                matches = [obs for obs in (fact or {}).get('observations', [])
                           if obs['sourceUrl'] == request['sourceUrl'] and obs['fetchedAt'] == request['originalFetchedAt']]
                if len(matches) != 1 or request['outcome'] != 'observed': fail('CENSUS_PROOF_INVALID')
                covered.append({'factId': fact['factId'], **copy.deepcopy(matches[0])})
            return {'schemaVersion': 1, 'kind': 'restored-price-census-proof', 'scope': 'original-target-key-and-inherited-ledgers',
                'provenance': dict(PROOF), 'key': KEY, 'registryDigest': rows[0]['digest'],
                'manifestContentHash': subset['manifestContentHash'], 'facts': list(facts.values()),
                'coveredObservations': covered, 'dailyEvidence': evidence, 'ledgerHashes': ledger_hashes,
                'attemptRounds': 2, 'remainingAttempts': 0, 'ledgerModified': False,
                'marketDataRequests': 0, 'automaticResolution': False, 'resolution': 'unresolved'}
    except CensusError: raise
    except Exception: fail('CENSUS_PROOF_INVALID')


def restore_proof(api=None, now=None):
    api = api or restore.ActionsAPI()
    try:
        prior = api.get(f"actions/runs/{PROOF['runId']}")
        restore.run_identity(prior, run_id=PROOF['runId'], source_commit=PROOF['sourceCommit'])
        listing = api.get(f"actions/runs/{PROOF['runId']}/artifacts?per_page=100")
        artifacts = listing.get('artifacts') if isinstance(listing, dict) else None
        if (not isinstance(artifacts, list) or len(artifacts) > 100 or listing.get('total_count') != len(artifacts)):
            fail('CENSUS_PROOF_IDENTITY_INVALID')
        selected = [artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get('id') == PROOF['artifactId']]
        if len(selected) != 1: fail('CENSUS_PROOF_IDENTITY_INVALID')
        restore.artifact_identity(selected[0], prior)
        if selected[0].get('digest') != 'sha256:' + PROOF['archiveSha256']: fail('CENSUS_PROOF_IDENTITY_INVALID')
        raw = api.get(f"actions/artifacts/{PROOF['artifactId']}/zip", binary=True)
        return validate_proof(selected[0], prior, raw, now)
    except CensusError: raise
    except Exception: fail('CENSUS_API_UNAVAILABLE')


COUNTS = set('declaredCompanyPages scannedCompanyPages scannedFunds scannedRows targetRows unobservedTargetRows ordinaryOccurrences witnessOccurrences validOccurrences retainedOccurrences invalidTargetOccurrences omittedOccurrences invalidDocuments scannedInputBytes factCount sourceObservationCount locationCount unscannedCompanyPages'.split())
LIMITS = {'maxOutputBytes': 8 * 1024 * 1024, 'maxInputBytes': 2 * 1024 * 1024 * 1024,
    'maxPartBytes': 25 * 1024 * 1024, 'maxManifestBytes': 4 * 1024 * 1024, 'maxPages': 10000,
    'maxFunds': 100000, 'maxFundsPerPart': 1000, 'maxRows': 5000000, 'maxRowsPerFund': 30000,
    'maxFacts': 128, 'maxObservations': 4096, 'maxLocations': 20000, 'maxSeconds': 600}
REASONS = set('CENSUS_TIME_LIMIT CENSUS_INPUT_LIMIT CENSUS_BLOB_INVALID CENSUS_PATH_INVALID CENSUS_DOCUMENT_INVALID CENSUS_TARGET_FACT_INVALID CENSUS_FACT_COLLISION CENSUS_EVIDENCE_LIMIT CENSUS_MANIFEST_INVALID CENSUS_PAGE_LIMIT CENSUS_COMPANY_PATH_INVALID CENSUS_FUND_LIMIT CENSUS_FUND_INVALID CENSUS_ROW_LIMIT CENSUS_ROW_INVALID CENSUS_TARGET_BINDING_INVALID CENSUS_TARGET_TIME_INVALID CENSUS_TARGET_WITNESS_INVALID CENSUS_CHECKOUT_CHANGED CENSUS_INCOMPLETE_PAGES CENSUS_NO_TARGET_FACTS CENSUS_OUTPUT_LIMIT'.split())
SCOPE = 'All target-key observations in this commit referenced company parts, including retained witnesses; no claim about discarded unretained sources.'


def normalize_census(value, now=None):
    """Recompute bounded numerical/observation identities, not just their labels."""
    current = conflicts.clock(now)
    fields = {'schemaVersion', 'kind', 'quarter', 'key', 'targetPeriodEnd', 'baselinePeriodEnd', 'generatedAt', 'dataCommit',
        'productionDataModified', 'automaticResolution', 'quoteRequests', 'resolution', 'scope', 'limits', 'manifest', 'parts',
        'counts', 'facts', 'sourceObservations', 'groups', 'rawRegistry', 'sourceUniverseDigest', 'complete', 'status', 'reasonCodes'}
    try:
        if (not isinstance(value, dict) or set(value) != fields or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
                or value['kind'] != 'price-source-census' or value['quarter'] != QUARTER or value['key'] != KEY
                or value['dataCommit'] != DATA_COMMIT or value['targetPeriodEnd'] != '2026-09-30' or value['baselinePeriodEnd'] != '2026-06-30'
                or value['scope'] != SCOPE or value['productionDataModified'] is not False or value['automaticResolution'] is not False
                or type(value['quoteRequests']) is not int or value['quoteRequests'] != 0 or value['resolution'] != 'unresolved'
                or not consistency._instant(value['generatedAt']) or consistency._instant(value['generatedAt']) > current + dt.timedelta(minutes=5)
                or type(value['complete']) is not bool or value['status'] != ('complete' if value['complete'] else 'partial')
                or not isinstance(value['reasonCodes'], list) or any(not isinstance(reason, str) or reason not in REASONS for reason in value['reasonCodes'])
                or value['reasonCodes'] != sorted(set(value['reasonCodes'])) or value['complete'] != (not value['reasonCodes'])):
            fail('CENSUS_INPUT_INVALID')
        observed_now = consistency._instant(value['generatedAt'])
        limits = value['limits']; counts = value['counts']
        if (not isinstance(limits, dict) or set(limits) != set(LIMITS)
                or any(type(n) is not int or not 1 <= n <= LIMITS[key] for key, n in limits.items())
                or not isinstance(counts, dict) or set(counts) != COUNTS
                or any(type(n) is not int or not 0 <= n <= LIMITS['maxInputBytes'] for n in counts.values())):
            fail('CENSUS_INPUT_INVALID')
        parts = value['parts']
        if not isinstance(parts, list) or len(parts) > limits['maxPages']: fail('CENSUS_INPUT_INVALID')
        paths = set()
        for item in ([value['manifest']] if value['manifest'] is not None else []) + parts:
            if (not isinstance(item, dict) or set(item) != {'path', 'gitBlobSha', 'sha256', 'bytes'}
                    or not isinstance(item['path'], str) or not re.fullmatch(r'web/data/periods/2026Q3/(?:latest|companies/[A-Za-z0-9_-]{1,64})\.json', item['path'])
                    or item['path'] in paths or not re.fullmatch(r'[a-f0-9]{40}', str(item['gitBlobSha']))
                    or not re.fullmatch(r'[a-f0-9]{64}', str(item['sha256'])) or type(item['bytes']) is not int
                    or not 0 <= item['bytes'] <= limits['maxPartBytes']): fail('CENSUS_INPUT_INVALID')
            paths.add(item['path'])
        if value['manifest'] is not None and value['manifest']['path'] != 'web/data/periods/2026Q3/latest.json': fail('CENSUS_INPUT_INVALID')
        facts, observations = value['facts'], value['sourceObservations']
        if (not isinstance(facts, list) or len(facts) > limits['maxFacts'] or not isinstance(observations, list)
                or len(observations) > limits['maxObservations']): fail('CENSUS_INPUT_INVALID')
        row = conflicts.identity(KEY, QUARTER); identity = {key: row[key] for key in ('market', 'stockCode', 'currency', 'rangeStart', 'rangeEnd', 'calendarId')}
        by_id = {}
        for fact in facts:
            if (not isinstance(fact, dict) or set(fact) != {'factId', 'price', 'sourceIdentity', 'complete'}
                    or not re.fullmatch(r'[a-f0-9]{64}', str(fact['factId'])) or fact['factId'] in by_id
                    or type(fact['complete']) is not bool or not isinstance(fact['price'], dict)
                    or set(fact['price']) != set(consistency.PRICE_FIELDS) or consistency._price(fact['price'], identity) != fact['price']
                    or not isinstance(fact['sourceIdentity'], list) or not 1 <= len(fact['sourceIdentity']) <= 9
                    or fact['sourceIdentity'] != sorted(set(conflicts.source_identity(url) for url in fact['sourceIdentity']))
                    or fact['factId'] != conflicts.digest({'sourceIdentity': fact['sourceIdentity'], 'price': fact['price']})
                    or fact['complete'] != conflicts.valid_complete_price(fact['price'], 'CN', '000636', '2026-07-01', '2026-09-30')):
                fail('CENSUS_INPUT_INVALID')
            by_id[fact['factId']] = fact
        if [fact['factId'] for fact in facts] != sorted(by_id): fail('CENSUS_INPUT_INVALID')
        seen, grouped, locations, ordinary, witness = set(), {fact_id: [] for fact_id in by_id}, 0, 0, 0
        for obs in observations:
            if (not isinstance(obs, dict) or set(obs) != {'observationId', 'factId', 'sourceUrl', 'sourceUrls', 'fetchedAt', 'occurrenceCount', 'locations'}
                    or obs.get('factId') not in by_id or obs.get('observationId') in seen or not isinstance(obs.get('locations'), list)
                    or type(obs.get('occurrenceCount')) is not int or obs['occurrenceCount'] != len(obs['locations'])
                    or not 1 <= obs['occurrenceCount'] <= limits['maxLocations']): fail('CENSUS_INPUT_INVALID')
            base = {key: obs[key] for key in ('factId', 'sourceUrl', 'sourceUrls', 'fetchedAt')}
            if (conflicts.digest(base) != obs['observationId'] or consistency._fact({'price': by_id[obs['factId']]['price'], **base}, identity, observed_now) is None
                    or sorted(set(conflicts.source_identity(url) for url in [obs['sourceUrl'], *obs['sourceUrls']])) != by_id[obs['factId']]['sourceIdentity']):
                fail('CENSUS_INPUT_INVALID')
            seen.add(obs['observationId']); grouped[obs['factId']].append(obs)
            for location in obs['locations']:
                if (not isinstance(location, dict) or set(location) != {'partPath', 'fundCode', 'fundIndex', 'rowIndex', 'origin', 'witnessDigest', 'reportRetrievedAt', 'priceUpdatedAt'}
                        or location['partPath'] not in paths or '/companies/' not in location['partPath']
                        or not re.fullmatch(r'[0-9]{6}', str(location['fundCode'])) or location['fundCode'] == '000000'
                        or any(type(location[key]) is not int or not 0 <= location[key] < limit for key, limit in
                               (('fundIndex', limits['maxFundsPerPart']), ('rowIndex', limits['maxRowsPerFund'])))
                        or location['origin'] not in ('ordinary', 'retained-witness')
                        or not consistency._instant(location['reportRetrievedAt'])
                        or consistency._instant(location['reportRetrievedAt']) > observed_now + dt.timedelta(minutes=5)
                        or location['priceUpdatedAt'] is not None and (not consistency._instant(location['priceUpdatedAt'])
                            or consistency._instant(location['priceUpdatedAt']) > observed_now + dt.timedelta(minutes=5))): fail('CENSUS_INPUT_INVALID')
                if location['origin'] == 'ordinary':
                    bound = max(consistency._instant(stamp) for stamp in (location['reportRetrievedAt'], location['priceUpdatedAt']) if stamp)
                    if location['witnessDigest'] is not None or consistency._instant(obs['fetchedAt']) > bound: fail('CENSUS_INPUT_INVALID')
                    ordinary += 1
                else:
                    if not re.fullmatch(r'[a-f0-9]{64}', str(location['witnessDigest'])): fail('CENSUS_INPUT_INVALID')
                    witness += 1
                locations += 1
        if ([obs['observationId'] for obs in observations] != sorted(seen) or locations > limits['maxLocations']
                or counts['factCount'] != len(facts) or counts['sourceObservationCount'] != len(observations)
                or counts['locationCount'] != locations or counts['retainedOccurrences'] != locations): fail('CENSUS_INPUT_INVALID')
        groups = []
        for fact in facts:
            items = sorted(grouped[fact['factId']], key=lambda obs: (consistency._instant(obs['fetchedAt']), obs['observationId']))
            if not items: fail('CENSUS_INPUT_INVALID')
            groups.append({'price': fact['price'], 'observations': [{key: obs[key] for key in ('sourceUrl', 'sourceUrls', 'fetchedAt')} for obs in items],
                'occurrenceCount': sum(obs['occurrenceCount'] for obs in items), 'omittedObservationCount': 0,
                'firstFetchedAt': items[0]['fetchedAt'], 'lastFetchedAt': items[-1]['fetchedAt']})
        if groups != value['groups']: fail('CENSUS_INPUT_INVALID')
        complete = [fact for fact in facts if fact['complete']]
        extremes = [fact for fact in facts if not fact['complete'] and complete and
                    (fact['price']['low'] < min(row['price']['low'] for row in complete) or fact['price']['high'] > max(row['price']['high'] for row in complete))]
        types = (['complete-source-disagreement'] if len({(fact['price']['low'], fact['price']['high']) for fact in complete}) > 1 else []) + (['partial-extreme'] if extremes else [])
        ids = sorted(fact['factId'] for fact in complete + extremes)
        registry = {'key': KEY, 'types': types, 'factIds': ids, 'digest': conflicts.digest({'key': KEY, 'types': types, 'factIds': ids}) if types else None}
        universe = conflicts.digest({'key': KEY, 'factIds': sorted(by_id), 'observationIds': sorted(seen)})
        if (value['rawRegistry'] != registry or value['sourceUniverseDigest'] != universe
                and not (value['sourceUniverseDigest'] is None and not facts and 'CENSUS_OUTPUT_LIMIT' in value['reasonCodes'])):
            fail('CENSUS_INPUT_INVALID')
        if value['complete'] and (not value['manifest'] or not facts or counts['scannedCompanyPages'] != counts['declaredCompanyPages']
                or counts['scannedCompanyPages'] != len(parts) or counts['unscannedCompanyPages'] != 0
                or any(counts[key] for key in ('invalidTargetOccurrences', 'omittedOccurrences', 'invalidDocuments'))
                or counts['validOccurrences'] != locations or counts['ordinaryOccurrences'] != ordinary or counts['witnessOccurrences'] != witness):
            fail('CENSUS_INPUT_INVALID')
        if len(encoded(value)) > MAX_CENSUS_BYTES: fail('CENSUS_INPUT_INVALID')
        return value
    except CensusError: raise
    except Exception: fail('CENSUS_INPUT_INVALID')


def certificate(census, proof, now=None):
    census = normalize_census(census, now)
    covered_ids = {conflicts.digest(obs) for obs in proof['coveredObservations']}
    current_ids = {obs['observationId'] for obs in census['sourceObservations']}
    matched = sorted(current_ids & covered_ids); uncovered = sorted(current_ids - covered_ids)
    proof_fact_ids = {fact['factId'] for fact in proof['facts']}
    new_facts = sorted(fact['factId'] for fact in census['facts'] if fact['factId'] not in proof_fact_ids)
    eligible = (census['complete'] and len(matched) == len(covered_ids) == 2 and not uncovered and not new_facts
                and census['rawRegistry']['digest'] == proof['registryDigest'])
    parts_digest = conflicts.digest(census['parts'])
    candidate_digest = conflicts.digest({'schemaVersion': 1, 'key': KEY, 'proof': proof['provenance'],
        'dataCommit': census['dataCommit'], 'manifest': census['manifest'], 'partsDigest': parts_digest,
        'sourceUniverseDigest': census['sourceUniverseDigest'], 'rawRegistry': census['rawRegistry']})
    data_status = 'complete' if eligible else 'partial'
    return {'schemaVersion': 1, 'kind': 'price-source-census-verification', 'status': data_status,
        # A complete census can correctly find observations that the old proof
        # does not cover. That is a successful diagnosis, not data completion.
        'executionSuccess': census['complete'], 'dataStatus': data_status,
        'key': KEY, 'proofScope': {**proof['provenance'], 'registryDigest': proof['registryDigest'], 'attemptRounds': 2, 'remainingAttempts': 0},
        'censusScope': {'dataCommit': census['dataCommit'], 'manifest': census['manifest'], 'partsDigest': parts_digest,
                        'sourceUniverseDigest': census['sourceUniverseDigest'], 'rawRegistry': census['rawRegistry'],
                        'complete': census['complete'], 'reasonCodes': census['reasonCodes'], 'counts': census['counts']},
        'diagnosticCandidate': {'eligible': eligible, 'digest': candidate_digest, 'admission': 'not-implemented',
            'classification': 'exact-or-yahoo-equals-binary32-eastmoney', 'matchedObservationCount': len(matched),
            'uncoveredObservationCount': len(uncovered), 'uncoveredObservationIds': uncovered,
            'uncoveredFactIds': new_facts, 'comparison': proof['dailyEvidence']['comparison']},
        'proofLedgerHashes': proof['ledgerHashes'], 'priceLedgerParentRunId': PROOF['runId'],
        'marketDataRequests': 0, 'ledgerModified': False, 'productionDataModified': False,
        'automaticResolution': False, 'resolution': 'unresolved'}


def write_guarded(path, value, maximum):
    raw = encoded(value) + b'\n'
    if len(raw) > maximum or inspect_content(raw, max_bytes=maximum): fail('CENSUS_OUTPUT_INVALID')
    path = Path(path)
    if path.exists(): fail('CENSUS_OUTPUT_INVALID')
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)


def data_identity(root):
    root = Path(root).resolve()
    try:
        def git(*args):
            return subprocess.run(['git', '--no-optional-locks', '-C', str(root), *args], capture_output=True,
                check=True, timeout=30).stdout.decode().strip()
        if git('rev-parse', 'HEAD') != DATA_COMMIT or git('status', '--porcelain=v1', '--untracked-files=all'):
            fail('CENSUS_DATA_INVALID')
    except CensusError: raise
    except Exception: fail('CENSUS_DATA_INVALID')
    return root


def run(data_repo, output, api=None, now=None):
    env = os.environ
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != restore.REPOSITORY
            or env.get('GITHUB_WORKFLOW_REF') != f'{restore.REPOSITORY}/{WORKFLOW}@refs/heads/main'):
        fail('CENSUS_REQUIRES_ACTIONS')
    source = data_identity(data_repo); destination = Path(output).resolve()
    if destination.is_relative_to(source) or destination.exists(): fail('CENSUS_OUTPUT_INVALID')
    proof = restore_proof(api, now)
    # Scanner is a distinct local-only child. It has no quote provider or
    # credential input; GitHub credentials are removed from its environment.
    child_env = {key: value for key, value in env.items() if key not in ('GH_TOKEN', 'GITHUB_TOKEN')}
    # An unvalidated child output never enters the always-upload artifact path.
    with tempfile.TemporaryDirectory(prefix='price-census-validation-') as temporary:
        child_output = Path(temporary).resolve() / 'census.json'
        command = ['node', 'scripts/price-source-census.js', '--data', str(source / 'web/data/periods'),
                   '--data-commit', DATA_COMMIT, '--output', str(child_output)]
        if now is not None:
            command += ['--now', now.isoformat() if hasattr(now, 'isoformat') else str(now)]
        try:
            child = subprocess.run(command, cwd=ROOT, env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=660)
        except subprocess.TimeoutExpired: fail('CENSUS_SCAN_TIMEOUT')
        except OSError: fail('CENSUS_SCAN_FAILED')
        if (child.returncode != 0 or not child_output.is_file() or child_output.is_symlink()
                or child_output.stat().st_size > MAX_CENSUS_BYTES): fail('CENSUS_SCAN_FAILED')
        value = parse_json(child_output.read_bytes()); normalize_census(value, now)
        if inspect_content(child_output.read_bytes(), max_bytes=MAX_CENSUS_BYTES): fail('CENSUS_OUTPUT_INVALID')
    data_identity(source)
    result = certificate(value, proof, now)
    write_guarded(destination / 'census.json', value, MAX_CENSUS_BYTES)
    write_guarded(destination / 'proof.json', proof, MAX_RESULT_BYTES)
    write_guarded(destination / 'result.json', result, MAX_RESULT_BYTES)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument('--data-repo', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.data_repo, args.output)
        print(json.dumps({'status': result['status'], 'dataStatus': result['dataStatus'],
                          'executionSuccess': result['executionSuccess'], 'marketDataRequests': 0, 'ledgerModified': False,
                          'eligible': result['diagnosticCandidate']['eligible']}))
        return 0 if result['executionSuccess'] is True else 2
    except Exception as error:
        code = str(error) if isinstance(error, CensusError) and str(error) in ERRORS else 'CENSUS_INPUT_INVALID'
        result = {'schemaVersion': 1, 'kind': 'price-source-census-verification', 'status': 'partial', 'errorCode': code,
                  'executionSuccess': False, 'dataStatus': 'partial',
                  'key': KEY, 'marketDataRequests': 0, 'ledgerModified': False, 'productionDataModified': False,
                  'automaticResolution': False, 'resolution': 'unresolved'}
        try:
            destination = Path(args.output).resolve()
            if not destination.is_relative_to(Path(args.data_repo).resolve()):
                write_guarded(destination / 'result.json', result, MAX_RESULT_BYTES)
        except Exception: pass
        print(json.dumps({'status': 'partial', 'dataStatus': 'partial', 'executionSuccess': False,
                          'errorCode': code, 'marketDataRequests': 0})); return 2


if __name__ == '__main__': raise SystemExit(main())
