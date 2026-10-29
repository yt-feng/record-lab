"""Bounded public statement layout plus synthetic negative and pipeline checks."""
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from collect import collect_fund
from nonstock_assets import parse_nonstock_assets, normalize_nonstock_assets, STATEMENT_METHOD
from providers import parse_pdf_pages
from report_extract import _details_for_pages
import traversal

FIXTURE = json.loads((ROOT / 'tests/fixtures/nonstock-zero-other-assets.json').read_text())
NOW = dt.datetime(2026, 10, 29, 1, tzinfo=dt.timezone.utc)
PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
QUARTER = {**FIXTURE['report'], 'title': '合成基金2026年第2季度报告'}
INTERIM = {'sourceUrl': 'https://example.org/interim.pdf', 'kind': 'semiannual', 'periodEnd': '2026-06-30',
           'publishedAt': '2026-08-31', 'title': '合成基金2026年中期报告'}


def parse(fixture=FIXTURE):
    return parse_nonstock_assets(fixture['pages'], fixture['report'], page_numbers=fixture['pageNumbers'])


def quarter_details():
    return _details_for_pages(FIXTURE['pages'], QUARTER['sourceUrl'], QUARTER['periodEnd'], QUARTER['kind'],
        parse_pdf_pages, page_numbers=FIXTURE['pageNumbers'], total_pages=14, published_at=QUARTER['publishedAt']) | {
        'parseMethod': 'pypdf', 'reportPdfSourceUrl': QUARTER['sourceUrl'], 'reportPdfKind': 'quarterly',
        'reportPdfPeriodEnd': '2026-06-30'}


class Source:
    def __init__(self, proof=None, stock=False):
        self.calls = []
        self.details = quarter_details()
        if proof is not None: self.details['noEquitiesEvidence'] = proof
        self.stock = stock
    def overview(self, _code): return {'name': '合成基金', 'company': '合成机构', 'managers': []}
    def announcements(self, *_): return [INTERIM, QUARTER]
    def holdings(self, *_):
        return {'2026-06-30': [{'stockCode': '600001', 'stockName': '合成证券', 'market': 'CN', 'currency': 'CNY',
                              'shares': 1, 'marketValueYuan': 10, 'weightPct': 1}]} if self.stock else {}
    def report_details(self, doc):
        self.calls.append(doc['sourceUrl'])
        if doc['sourceUrl'] == QUARTER['sourceUrl']: return copy.deepcopy(self.details)
        return {'parseMethod': 'pypdf', 'sourceTextHash': 'b' * 64,
                'pageStats': {'charCount': 100, 'totalPages': 1, 'processedPages': 1, 'pageCount': 1}}


def collect(source):
    return collect_fund({'code': '003589'}, NOW.date().isoformat(), provider=source,
                        target_period=PHASE['targetPeriodEnd'], baseline_period=PHASE['baselinePeriodEnd'])


def build(item, previous=None):
    raw = {'asOf': NOW.date().isoformat(), 'retrievedAt': NOW.isoformat(), 'funds': [item], 'barsByStock': {}, 'errors': []}
    source = "const fs=require('node:fs');const{buildSnapshot}=require('./scripts/build-data');const x=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(buildSnapshot(x.raw,{now:x.now,config:x.config,previous:x.previous})))"
    proc = subprocess.run(['node', '-e', source], cwd=ROOT, input=json.dumps({'raw': raw, 'now': NOW.isoformat(),
        'config': {**PHASE, 'funds': [{'code': '003589'}]}, 'previous': previous}), text=True, capture_output=True, timeout=10)
    if proc.returncode: raise AssertionError('SYNTHETIC_BUILD_FAILED')
    return json.loads(proc.stdout)


class NonstockStatementTests(unittest.TestCase):
    def test_actual_bounded_statement_is_not_fabricated_as_an_asset_table(self):
        proof = parse()
        self.assertIsNotNone(proof)
        self.assertEqual(proof['method'], STATEMENT_METHOD)
        self.assertEqual(proof['pages'], [9, 12])
        self.assertEqual(proof['assetTable']['rows'][-1]['amount'], '0.00')
        self.assertNotIn('otherAssetsTable', proof)
        statement = proof['otherAssetsStatement']
        self.assertEqual(statement['statementType'], 'explicit-no-period-end-other-assets')
        self.assertEqual(statement['section'], '5.9.3')
        self.assertEqual(statement['endSection'], '5.9.4')
        self.assertNotIn('rows', statement)
        self.assertNotIn('amountUnit', statement)
        self.assertEqual(normalize_nonstock_assets(proof), proof)

    def test_negation_other_period_open_section_and_missing_asset_row_reject(self):
        cases = [(1, '本报告期末', '上报告期末'), (1, '未持有', '无法确认未持有'),
                 (1, '未持有其他资产。', '未持有其他资产，但存在其他投资。'),
                 (1, '5.9.4 投资组合报告附注的其他文字描述部分', ''),
                 (1, '5.9.4', '5.9.5'), (1, '5.9.3', '6.9.3'),
                 (0, '4  其他资产  -  -\n', ''), (0, '固定收益投资', '权益投资'),
                 (0, '12,039,770,572.14', '12,039,770,572.15')]
        for index, before, after in cases:
            with self.subTest(before=before, after=after):
                fixture = copy.deepcopy(FIXTURE)
                self.assertIn(before, fixture['pages'][index])
                fixture['pages'][index] = fixture['pages'][index].replace(before, after)
                self.assertIsNone(parse(fixture))

    def test_rounded_zero_other_weight_with_nonzero_amount_cannot_use_statement(self):
        fixture = copy.deepcopy(FIXTURE)
        fixture['pages'][0] = fixture['pages'][0].replace('4  其他资产  -  -', '4  其他资产  0.01  0.00')
        fixture['pages'][0] = fixture['pages'][0].replace('1,901,396,038.79', '1,901,396,038.78')
        self.assertIsNone(parse(fixture))

    def test_false_page_mapping_duplicate_statement_and_conflicting_forms_reject(self):
        fixture = copy.deepcopy(FIXTURE); fixture['pageNumbers'] = [9, None]
        self.assertIsNone(parse(fixture))
        fixture = copy.deepcopy(FIXTURE); fixture['pages'] += [fixture['pages'][1]]; fixture['pageNumbers'] += [13]
        self.assertIsNone(parse(fixture))
        for edit in [lambda p: p.update(method='two-closed-nonstock-asset-tables'),
                     lambda p: p.update(otherAssetsTable={}),
                     lambda p: p['otherAssetsStatement'].update(sectionBounded=False),
                     lambda p: p['otherAssetsStatement'].update(statementType='generic-empty')]:
            proof = parse(); edit(proof); self.assertIsNone(normalize_nonstock_assets(proof))

    def test_collector_reuses_quarter_fetch_preserves_source_and_builder_recovers_unknown(self):
        provider = Source(); item, errors = collect(provider)
        self.assertEqual(provider.calls, [INTERIM['sourceUrl'], QUARTER['sourceUrl']])
        self.assertEqual(len(item['reports']), 2)
        self.assertNotIn('noEquitiesEvidence', item['reports'][0])
        self.assertEqual(item['reports'][1]['sourceUrl'], QUARTER['sourceUrl'])
        result = build(item)
        fund = result['funds'][0]
        self.assertEqual(fund['status'], 'no-equities')
        self.assertEqual(fund['selectedReport']['sourceUrl'], QUARTER['sourceUrl'])
        self.assertEqual(fund['selectedReport']['kind'], 'quarterly')
        self.assertEqual(fund['noEquitiesEvidence']['method'], STATEMENT_METHOD)
        self.assertEqual(traversal.report_quality(fund, PHASE, NOW)['fields']['holdings'], 'complete')
        self.assertEqual(traversal.report_quality(fund, PHASE, NOW)['fields']['prices'], 'not-applicable')
        old_item = copy.deepcopy(item); old_item['reports'] = [old_item['reports'][0]]; old_item.pop('noEquitiesEvidence')
        old_item['reports'][0]['evidenceRetrievedAt'] = '2026-10-20T01:02:03.123456Z'
        old = build(old_item)
        self.assertEqual(old['funds'][0]['status'], 'unavailable')
        before = copy.deepcopy(old)
        recovered = build(item, old)
        self.assertEqual(recovered['funds'][0]['status'], 'no-equities')
        self.assertNotIn('CONTRADICTORY_NO_EQUITIES', [e['errorCode'] for e in recovered['errors']])
        self.assertEqual(old, before)

    def test_collector_does_not_append_invalid_proof_or_skip_holdings_contradiction(self):
        for edit in [lambda p: p.update(sourceUrl=INTERIM['sourceUrl']), lambda p: p.update(kind='semiannual'),
                     lambda p: p.update(periodEnd='2026-09-30'),
                     lambda p: p['otherAssetsStatement'].update(sectionBounded=False)]:
            proof = parse(); edit(proof); provider = Source(proof)
            item, _ = collect(provider)
            self.assertEqual(len(item['reports']), 1)
            self.assertNotIn('noEquitiesEvidence', item)
        provider = Source(stock=True); item, errors = collect(provider)
        self.assertEqual(len(item['reports']), 1)
        self.assertNotIn('noEquitiesEvidence', item)
        self.assertIn('CONTRADICTORY_NO_EQUITIES', [e['errorCode'] for e in errors])
        self.assertEqual(provider.calls.count(QUARTER['sourceUrl']), 1)


if __name__ == '__main__': unittest.main()
