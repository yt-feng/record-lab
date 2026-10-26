"""Small normalized overlays and exact-byte catalogue locks; no downloads."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from quarter_catalog import compose_quarter_catalog, freeze_catalog, validate_catalog_lock
NOW = '2026-10-26T02:00:00Z'


def current():
    return {'schemaVersion': 1, 'asOf': '2026-10-26', 'retrievedAt': '2026-10-26T00:00:00Z',
            'companyCount': 1, 'fundCount': 1, 'coverage': {'directoryComplete': True, 'fundCount': 1, 'companyCount': 1},
            'companies': [{'code': 'co1', 'name': '合成机构', 'fundCount': 1, 'directoryStatus': 'complete'}],
            'funds': [{'code': '000001', 'name': '当前产品', 'type': '混合型', 'companyCode': 'co1', 'companyName': '合成机构'}]}


def overlay():
    return {'schemaVersion': 1, 'targetPeriodEnd': '2026-06-30', 'parseStatus': 'complete',
            'historicalUniverseVerified': True,
            'evidence': [{'id': 'synthetic-june', 'sourceUrl': 'https://example.org/june.xlsx', 'format': 'xlsx',
                          'effectiveDate': '2026-06-30', 'publishedAt': '2026-07-15', 'retrievedAt': '2026-10-26T01:00:00Z',
                          'recordUnit': 'fund-product-code', 'contentHash': 'a' * 64, 'parsedRows': 2}],
            'records': [{'code': code, 'name': '历史产品' + code, 'companyCode': 'co1', 'sourceCompanyName': '官方来源中的公司文字',
                         'evidence': [{'sourceId': 'synthetic-june', 'sheet': '产品索引', 'row': row}]}
                        for row, code in enumerate(('000001', '000003'), 2)]}


class QuarterCatalogTests(unittest.TestCase):
    def test_union_retains_current_metadata_and_unmapped_codes_without_asserting_historical_completeness(self):
        source, additions = current(), overlay()
        before = copy.deepcopy(source)
        result = compose_quarter_catalog(source, '2026Q2', additions, now=NOW)
        self.assertEqual(source, before)
        self.assertEqual(result['fundCount'], 2)
        self.assertEqual(result['funds'][0]['name'], '当前产品')
        self.assertEqual(result['funds'][0]['companyCode'], 'co1')
        self.assertIsNone(result['funds'][1]['companyCode'])
        self.assertEqual(result['funds'][1]['historicalSourceCompanyName'], '官方来源中的公司文字')
        self.assertEqual(result['funds'][1]['historicalPresenceEvidence'][0]['row'], 3)
        proof = result['quarterUniverse']
        self.assertEqual(proof['historicalAddedFundCount'], 1)
        self.assertTrue(proof['currentDirectoryComplete'])
        self.assertEqual(proof['historicalParseStatus'], 'complete')
        self.assertFalse(proof['historicalUniverseVerified'])
        self.assertEqual(proof['recordUnit'], 'fund-share-code')
        self.assertEqual(proof['sources'][0]['recordUnit'], 'fund-product-code')
        self.assertEqual(proof['sources'][0]['effectiveDate'], '2026-06-30')
        self.assertEqual(proof['sources'][0]['retrievedAt'], additions['evidence'][0]['retrievedAt'])
        self.assertEqual(result['retrievedAt'], source['retrievedAt'])
        self.assertEqual(result['coverage']['unmappedCount'], 1)

    def test_current_only_and_legacy_views_are_never_a_historical_certificate(self):
        for legacy in (False, True):
            result = compose_quarter_catalog(current(), '2026Q3', legacy=legacy, now=NOW)
            self.assertEqual(result['quarterUniverse']['status'], 'current-directory-only')
            self.assertEqual(result['quarterUniverse']['historicalParseStatus'], 'unchecked')
            self.assertFalse(result['quarterUniverse']['historicalUniverseVerified'])
            self.assertEqual(result['quarterUniverse'].get('legacyPlanRecovered', False), legacy)
        with self.assertRaisesRegex(ValueError, 'INVALID_HISTORICAL_OVERLAY'):
            compose_quarter_catalog(current(), '2026Q3', overlay(), now=NOW)

    def test_invalid_source_rows_duplicates_and_unparsed_records_are_rejected(self):
        changes = [lambda x: x.update(parseStatus='unchecked'), lambda x: x.update(schemaVersion=True),
                   lambda x: x['evidence'][0].update(effectiveDate='2026-09-30'),
                   lambda x: x['evidence'][0].update(sourceUrl='https://localhost/source.xlsx'),
                   lambda x: x['evidence'][0].update(contentHash='bad'),
                   lambda x: x['records'][0]['evidence'][0].update(sourceId='unknown'),
                   lambda x: x['records'][0]['evidence'][0].update(row=True),
                   lambda x: x['records'].append(copy.deepcopy(x['records'][0]))]
        for change in changes:
            raw = overlay(); change(raw)
            with self.assertRaises(ValueError): compose_quarter_catalog(current(), '2026Q2', raw, now=NOW)

    def test_source_fetch_time_uses_run_clock_not_the_older_current_directory_fetch(self):
        raw = overlay()
        raw['evidence'][0]['retrievedAt'] = '2026-10-26T02:04:00Z'
        self.assertEqual(compose_quarter_catalog(current(), '2026Q2', raw, now=NOW)['quarterUniverse']['historicalAddedFundCount'], 1)
        raw['evidence'][0]['retrievedAt'] = '2026-10-26T02:06:00Z'
        with self.assertRaisesRegex(ValueError, 'FUTURE_HISTORICAL_SOURCE'):
            compose_quarter_catalog(current(), '2026Q2', raw, now=NOW)

    def test_frozen_lock_is_exact_bytes_and_rejects_missing_file_or_changed_source(self):
        with tempfile.TemporaryDirectory() as name:
            result = compose_quarter_catalog(current(), '2026Q2', overlay(), now=NOW)
            lock = freeze_catalog(result, '2026Q2', name)
            config = {'targetPeriodEnd': '2026-06-30', 'catalogLockVersion': 1, 'catalogPath': lock['path'],
                      'catalogDigest': lock['sha256'], 'requireHistoricalUniverse': True}
            file = validate_catalog_lock('2026Q2', config, lock, name)
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), lock['sha256'])
            file.write_bytes(file.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'CATALOG_DIGEST_MISMATCH'): validate_catalog_lock('2026Q2', config, lock, name)
            file.unlink()
            with self.assertRaisesRegex(ValueError, 'MISSING_CATALOG_LOCK'): validate_catalog_lock('2026Q2', config, lock, name)

    def test_catalog_size_scan_uses_its_public_destination_policy(self):
        with tempfile.TemporaryDirectory() as name, patch('quarter_catalog.content_limit', return_value=100) as policy:
            with self.assertRaisesRegex(ValueError, 'CATALOG_PUBLICATION_REJECTED'):
                freeze_catalog(compose_quarter_catalog(current(), '2026Q2', now=NOW), '2026Q2', name)
            policy.assert_called_with('web/data/periods/2026Q2/catalog.json')
            self.assertFalse((Path(name) / 'catalogs/2026Q2.json').exists())


if __name__ == '__main__': unittest.main()
