"""Explain fixed stored numeric facts without claiming new response checks.

This is a directional IEEE-754 representation relation, not exact equality and
not a general tolerance. It never changes prices, clocks, raw witnesses or the
request ledger. The caller validates the census and restores the pinned proof.
"""
from __future__ import annotations
import copy
import hashlib

import price_consistency as consistency
import price_daily_evidence as daily
from price_coverage import expected_sessions

METHOD = 'exact-or-yahoo-equals-binary32-eastmoney'


def _same(left, right):
    return consistency.canonical_digest(left) == consistency.canonical_digest(right)


def _daily_compatible(proof, trusted_provenance):
    """Recompute the small original proof, never fetch a replacement response."""
    try:
        if proof['provenance'] != trusted_provenance:
            return False
        evidence = proof['dailyEvidence']
        original = copy.deepcopy(evidence); claimed_hash = original.pop('evidenceSha256')
        if (claimed_hash != trusted_provenance['evidenceSha256']
                or hashlib.sha256(daily.encoded(original)).hexdigest() != claimed_hash
                or evidence['key'] != proof['key'] or evidence['digest'] != proof['registryDigest']
                or len(evidence['sources']) != 2):
            return False
        facts = {fact['factId']: fact for fact in proof['facts']}
        expected = expected_sessions(evidence['market'], evidence['rangeStart'], evidence['rangeEnd'])
        sources = {}
        for source in evidence['sources']:
            provider = source['provider']
            if provider not in {'eastmoney', 'yahoo'} or provider in sources:
                return False
            series = daily.normalize_series(source['series'], provider, evidence)
            rows = series['rows']; fact = facts[source['factId']]
            if (source['outcome'] != 'observed' or source['storedRangeExactlyReproduced'] is not True
                    or source['coverage']['completeness'] != 'complete' or not expected
                    or [row['date'] for row in rows] != expected or series['errorCode'] is not None
                    or series['rejectedRowCount'] or series['duplicateDateCount']
                    or min(row['low'] for row in rows) != fact['price']['low']
                    or max(row['high'] for row in rows) != fact['price']['high']
                    or not any(obs['factId'] == fact['factId'] and obs['sourceUrl'] == source['sourceUrl']
                               and obs['fetchedAt'] == source['originalFetchedAt'] for obs in proof['coveredObservations'])):
                return False
            sources[provider] = rows
        counts = {'comparedCellCount': 0, 'exactCellCount': 0, 'float32OnlyCellCount': 0, 'nonFloat32CellCount': 0}
        for first, second in zip(sources['eastmoney'], sources['yahoo']):
            for field in daily.FIELDS:
                x, y = first[field], second[field]
                counts['comparedCellCount'] += 1
                category = 'exactCellCount' if x == y else 'float32OnlyCellCount' if daily.f32(x) == y else 'nonFloat32CellCount'
                counts[category] += 1
        comparison = evidence['comparison']
        return (not counts['nonFloat32CellCount'] and all(comparison[key] == value for key, value in counts.items())
                and comparison['bothCompleteCalendars'] is True and comparison['rule'] == METHOD
                and comparison['status'] in {'daily-identical', 'daily-float32-compatible'})
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def classify_stored_facts(census, proof, trusted_provenance):
    """Time-only observations share a fact, not a new original-response check."""
    compatible = _daily_compatible(proof, trusted_provenance)
    originals = {fact['factId']: fact for fact in proof['facts']}
    current = {fact['factId']: fact for fact in census['facts']}
    covered = proof['coveredObservations']
    direct_ids = {consistency.canonical_digest(obs) for obs in covered}
    observations = census['sourceObservations']
    direct = sorted(obs['observationId'] for obs in observations if obs['observationId'] in direct_ids)
    uncovered = sorted(obs['observationId'] for obs in observations if obs['observationId'] not in direct_ids)
    matching_facts = {identity for identity, fact in current.items() if identity in originals
        and _same(fact['price'], originals[identity]['price'])
        and _same(fact['sourceIdentity'], originals[identity]['sourceIdentity']) and fact['complete'] is True}
    mathematical = sorted(obs['observationId'] for obs in observations if compatible and obs['factId'] in matching_facts
        and any(obs['factId'] == old['factId'] and obs['sourceUrl'] == old['sourceUrl']
                and obs['sourceUrls'] == old['sourceUrls'] for old in covered))
    reasons = []
    if not compatible: reasons.append('DAILY_REPRESENTATION_NOT_VERIFIED')
    if not census['complete']: reasons.append('SOURCE_CENSUS_INCOMPLETE')
    if census['key'] != proof['key'] or census['rawRegistry']['digest'] != proof['registryDigest']:
        reasons.append('REGISTRY_SCOPE_CHANGED')
    if set(current) != set(originals) or matching_facts != set(originals): reasons.append('SOURCE_FACT_SET_CHANGED')
    if len(mathematical) != len(observations): reasons.append('SOURCE_OBSERVATION_NOT_FACT_MATCHED')
    eligible = not reasons
    result = {'schemaVersion': 1, 'kind': 'price-representation-classification',
        'key': census['key'], 'classification': 'stored-fact-representation-compatible' if eligible else 'unresolved',
        'eligible': eligible, 'method': METHOD, 'scope': 'stored-facts-only', 'reasonCodes': reasons,
        'rawRegistryDigest': census['rawRegistry']['digest'], 'sourceDataCommit': census['dataCommit'],
        'sourceUniverseDigest': census['sourceUniverseDigest'], 'proof': copy.deepcopy(trusted_provenance),
        'factCount': len(current), 'observationCount': len(observations),
        'mathematicallyClassifiedObservationCount': len(mathematical), 'mathematicallyClassifiedObservationIds': mathematical,
        'matchedObservationCount': len(direct), 'uncoveredObservationCount': len(uncovered),
        'uncoveredObservationIds': uncovered,
        'directlyBoundRecheckObservationCount': len(direct),
        'responseCoverage': 'complete' if not uncovered else 'partial',
        'responseCoverageMeaning': 'Original observations explicitly bound to the existing later recheck; other original responses were not rechecked.',
        'factCoverageMeaning': 'Identical stored price facts and exact source URLs; differing fetchedAt values are preserved.',
        'facts': copy.deepcopy(census['facts']),
        'directlyBoundObservations': copy.deepcopy(covered),
        'sourceObservations': [{key: obs[key] for key in ('observationId', 'factId', 'sourceUrl', 'sourceUrls', 'fetchedAt')}
                               for obs in observations],
        'dailyEvidence': copy.deepcopy(proof['dailyEvidence']),
        'marketDataRequests': 0, 'ledgerModified': False, 'rawConflictModified': False,
        'quotesModified': False, 'automaticResolution': False}
    result['classificationDigest'] = consistency.canonical_digest(result)
    return result
