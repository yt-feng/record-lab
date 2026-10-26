"""Synthetic records under the pinned schema; no source network requests."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import subprocess
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import collect_fund
from historical_inception import normalize_inception, catalog_inception
from historical_universe import SOURCE_ID, SOURCE_URL, SOURCE_AS_OF, SOURCE_HASH, SHEET
from providers import DataError
from traversal import catalog_records, plan_batches, valid_baseline_exemption, report_quality, normalize_quality

NOW = dt.datetime(2026, 10, 26, 0, tzinfo=dt.timezone.utc)


def proof():
    return {'schemaVersion': 1, 'fundCode': '000003', 'periodEnd': SOURCE_AS_OF, 'date': '2026-04-10',
            'sourceId': SOURCE_ID, 'sourceUrl': SOURCE_URL, 'contentHash': SOURCE_HASH,
            'sheet': SHEET, 'row': 3, 'retrievedAt': NOW.isoformat()}


def catalog():
    p = proof()
    return {'schemaVersion': 1, 'targetPeriodEnd': SOURCE_AS_OF, 'companies': [],
            'quarterUniverse': {'targetPeriodEnd': SOURCE_AS_OF, 'sources': [{
                'id': SOURCE_ID, 'sourceUrl': SOURCE_URL, 'effectiveDate': SOURCE_AS_OF,
                'contentHash': SOURCE_HASH, 'retrievedAt': NOW.isoformat(), 'format': 'xlsx', 'recordUnit': 'fund-product-code'}]},
            'funds': [{'code': '000003', 'name': '合成基金', 'historicalInceptionDate': p['date'],
                       'historicalInceptionSourceUrl': SOURCE_URL,
                       'historicalPresenceEvidence': [{'sourceId': SOURCE_ID, 'sheet': SHEET, 'row': 3}]}]}


class Stub:
    def __init__(self, foundation=None): self.foundation = foundation
    def overview(self, code):
        if self.foundation is None: raise DataError('NETWORK_ERROR')
        return {'name': '合成基金', 'company': '合成机构', 'managers': [], 'foundationDate': self.foundation}
    def announcements(self, code, as_of): return []


class HistoricalInceptionTests(unittest.TestCase):
    def collect(self, p=None, foundation=None, code='000003', target=SOURCE_AS_OF):
        return collect_fund({'code': code, 'catalogInceptionEvidence': p if p is not None else proof()},
                            '2026-10-26', provider=Stub(foundation), target_period=target, baseline_period='2026-03-31')

    def test_catalog_proof_reaches_real_batch_and_baseline_exemption(self):
        c = catalog()
        self.assertEqual(catalog_records(c)[1]['000003']['catalogInceptionEvidence'], proof())
        planned = plan_batches(c, None, {'targetPeriodEnd': SOURCE_AS_OF, 'baselinePeriodEnd': '2026-03-31'},
                               shards=1, batch_size=1, now=NOW)
        # Inspect the real collector input, not a mirrored construction.
        batch = next(iter(planned['batches'].values()))
        fund, errors = collect_fund(batch['funds'][0], '2026-10-26', provider=Stub(), target_period=SOURCE_AS_OF)
        self.assertEqual(fund['inceptionDate'], '2026-04-10')
        self.assertEqual(fund['inceptionSourceUrl'], SOURCE_URL)
        self.assertTrue(valid_baseline_exemption({'reason': 'ESTABLISHED_AFTER_BASELINE', 'date': fund['inceptionDate'],
            'sourceUrl': fund['inceptionSourceUrl'], 'fundCode': fund['code'], 'baselinePeriodEnd': '2026-03-31',
            'periodEnd': SOURCE_AS_OF, 'inceptionEvidence': fund['inceptionEvidence']}, NOW))
        self.assertNotIn('notApplicableEvidence', fund)
        self.assertTrue(any(error['stage'] == 'metadata' for error in errors))

    def test_alias_wrong_quarter_and_invalid_proofs_cannot_transfer_dates(self):
        for change in ({'fundCode': '000004'}, {'periodEnd': '2026-09-30'}, {'contentHash': 'b'*64},
                       {'sourceUrl': 'https://example.org/other.xlsx'}, {'row': True}, {'row': 2},
                       {'date': '2026-02-30'}, {'date': '2026-07-01'}, {'retrievedAt': '2026-10-27T00:00:00Z'},
                       {'retrievedAt': '2026-06-29T00:00:00Z'}, {'retrievedAt': '2026-10-26T00:00:00'}):
            value = {**proof(), **change}
            with self.subTest(change=change): self.assertIsNone(normalize_inception(value, '000003', SOURCE_AS_OF, now=NOW))
        fund, _ = self.collect(code='000004')
        self.assertNotIn('inceptionDate', fund)
        fund, _ = self.collect(target='2026-09-30')
        self.assertNotIn('inceptionDate', fund)

    def test_unverified_candidate_and_unbound_source_are_ignored(self):
        c = catalog(); c['funds'][0]['historicalCandidate'] = True
        self.assertIsNone(catalog_inception(c['funds'][0], c, now=NOW))
        c = catalog(); c['quarterUniverse']['sources'][0]['effectiveDate'] = '2026-09-30'
        self.assertIsNone(catalog_inception(c['funds'][0], c, now=NOW))

    def test_conflicting_profile_has_no_exemption_and_invalid_profile_uses_official_date(self):
        fund, errors = self.collect(foundation='2026-07-10')
        self.assertNotIn('inceptionDate', fund)
        self.assertNotIn('notApplicableEvidence', fund)
        self.assertTrue(any(error['errorCode'] == 'INCEPTION_DATE_CONFLICT' for error in errors))
        fund, _ = self.collect(foundation='2026-02-30')
        self.assertEqual(fund['inceptionDate'], '2026-04-10')
        fund, _ = self.collect(p={}, foundation='2025-01-01')
        self.assertEqual(fund['inceptionDate'], '2025-01-01')
        self.assertIn('eastmoney.com', fund['inceptionSourceUrl'])

    def test_proof_survives_javascript_build_merge_and_python_quality_roundtrip(self):
        fund, _ = self.collect()
        fund['reports'] = [{'periodEnd': SOURCE_AS_OF, 'publishedAt': '2026-07-21', 'kind': 'quarterly',
            'sourceUrl': 'https://example.org/synthetic.pdf', 'coverage': 'top10', 'navYuan': 10000,
            'holdings': [{'stockCode': '600001', 'stockName': '合成证券', 'market': 'CN', 'currency': 'CNY',
                          'shares': 100, 'weightPct': 1, 'marketValueYuan': 100}]}]
        script = """
const fs = require('node:fs');
const { buildSnapshot } = require('./scripts/build-data');
const { mergeSnapshots } = require('./scripts/merge-parts');
const fund = JSON.parse(fs.readFileSync(0, 'utf8'));
const config = {targetPeriodEnd:'2026-06-30',baselinePeriodEnd:'2026-03-31',funds:[{code:fund.code}]};
const now = '2026-10-26T12:00:00Z';
const snapshot = buildSnapshot({asOf:'2026-10-26',retrievedAt:'2026-10-26T00:00:00Z',funds:[fund],barsByStock:{},errors:[]},{now,config});
const merged = mergeSnapshots({snapshots:[snapshot],catalog:{companies:[],funds:[{code:fund.code}]},config,now});
process.stdout.write(JSON.stringify(Object.values(merged.companySnapshots).flatMap(part=>part.funds)[0]));
"""
        result = subprocess.run(['node', '-e', script], input=json.dumps(fund), text=True, capture_output=True,
                                check=True, cwd=Path(__file__).resolve().parents[1])
        retained = json.loads(result.stdout)
        self.assertEqual(retained['inceptionEvidence'], proof())
        quality = report_quality(retained, {'targetPeriodEnd': SOURCE_AS_OF, 'baselinePeriodEnd': '2026-03-31'}, NOW)
        self.assertEqual(quality['fields']['baseline'], 'not-applicable')
        normalized = normalize_quality(quality, 'complete', NOW)
        self.assertEqual(normalized['baselineExemption']['inceptionEvidence'], proof())
        del retained['inceptionEvidence']
        self.assertNotEqual(report_quality(retained, {'targetPeriodEnd': SOURCE_AS_OF,
            'baselinePeriodEnd': '2026-03-31'}, NOW)['fields']['baseline'], 'not-applicable')


if __name__ == '__main__': unittest.main()
