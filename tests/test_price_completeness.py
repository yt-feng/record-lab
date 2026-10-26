import datetime as dt
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from price_coverage import CALENDAR, expected_sessions, price_series, merge_price_series, valid_complete_price
from providers import PublicProvider, checked_price_rows
from traversal import price_coverage, normalize_enrichment, normalize_quality, valid_price_coverage, report_quality, enrichment_eligible
import test_traversal
from test_traversal import PHASE, NOW, complete_price

START, END = '2026-04-01', '2026-06-30'


def bars(market='CN'):
    return [{'date': date, 'low': 10, 'high': 20, 'close': 15} for date in expected_sessions(market, START, END)]


def make_series(rows, name='first'):
    return price_series('600001', 'CN', 'CNY', name, 'https://example.org/' + name,
                        checked_price_rows(rows, START, END), START, END)


class PriceCompletenessTests(unittest.TestCase):
    def test_calendar_counts_and_missing_or_conflicting_sources(self):
        self.assertEqual([len(expected_sessions(m, START, END)) for m in ('CN', 'HK', 'US')], [60, 59, 62])
        first, last = make_series(bars()[:31]), make_series(bars()[30:], 'second')
        self.assertEqual(first['coverage']['completeness'], 'partial')
        self.assertEqual(merge_price_series([first, last], START, END)['coverage']['completeness'], 'complete')
        last['rows'][0]['high'] = 99
        combined = merge_price_series([first, last], START, END)
        self.assertEqual(combined['coverage']['completeness'], 'partial')
        self.assertEqual(combined['coverage']['conflictingDateCount'], 1)
        wrong = make_series(bars()[30:], 'wrong'); wrong['currency'] = 'USD'
        self.assertEqual(merge_price_series([first, wrong], START, END)['coverage']['completeness'], 'partial')

    def test_independently_complete_source_replaces_partial_or_rejected_source(self):
        bad = make_series([*bars(), {'date': START, 'low': 80, 'high': 1, 'close': 2}])
        full = make_series(bars(), 'complete')
        partial = make_series([{**bars()[0], 'high': 99}], 'conflicting-partial')
        for prior in (bad, partial):
            result = merge_price_series([prior, full], START, END)
            self.assertEqual(result['coverage']['completeness'], 'complete')
            self.assertEqual(result['sourceUrls'], ['https://example.org/complete'])

    def test_provider_keeps_searching_after_first_partial_source(self):
        class Provider(PublicProvider):
            def __init__(self): self.calls = []
            def get(self, url, **_kwargs):
                self.calls.append(url)
                if 'eastmoney' in url:
                    return json.dumps({'data': {'code': '600001', 'klines': [START + ',15,15,20,10']}})
                days = expected_sessions('CN', START, END)
                timestamps = [int(dt.datetime.fromisoformat(day + 'T07:00:00+00:00').timestamp()) for day in days]
                return json.dumps({'chart': {'result': [{'meta': {'symbol': '600001.SS', 'currency': 'CNY', 'exchangeName': 'SHH'},
                    'timestamp': timestamps, 'events': {}, 'indicators': {'quote': [{'low': [10]*len(days), 'high': [20]*len(days), 'close': [15]*len(days)}]}}]}})
        source = Provider(); result = source.price_bars('600001', 'CN', START, END)
        self.assertEqual(len(source.calls), 2)
        self.assertEqual(result['coverage']['completeness'], 'complete')
        self.assertEqual(len(result['rows']), 60)

    def test_source_row_rejections_survive_parsing(self):
        values = checked_price_rows([*bars(), bars()[0], {'date': '2026-04-02', 'low': 30, 'high': 1, 'close': 3}], START, END)
        self.assertEqual(values.rejected_rows, 2)
        result = price_series('600001', 'CN', 'CNY', 'source', 'https://example.org/prices', values, START, END)
        self.assertEqual(result['coverage']['completeness'], 'partial')
        self.assertEqual(result['coverage']['rejectedRowCount'], 2)

    def test_old_availability_never_inherits_complete_or_exhausted_price_budget(self):
        old = {'status': 'complete', 'attempts': 3, 'holdingCount': 1, 'priceAvailable': 1,
               'missingStocks': [], 'holdingsIdentity': 'a'*64}
        self.assertFalse(valid_price_coverage(old))
        current = normalize_enrichment(old, 'complete', dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc))
        self.assertEqual(current['status'], 'pending'); self.assertEqual(current['attempts'], 0)
        self.assertEqual(current['totalAttempts'], 3)
        quality = normalize_quality({'fields': {'prices': 'complete'}}, 'no-equities', dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc))
        self.assertEqual(quality['fields']['prices'], 'missing')
        self.assertEqual(normalize_enrichment(None, 'no-equities', dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc))['status'], 'pending')

    def test_one_bar_or_wrong_window_cannot_complete_the_seventh_price_field(self):
        fund = test_traversal.TraversalTests().retained_fund()
        proof = complete_price()
        for mutation in ({'observedCount': 1, 'completeness': 'partial'}, {'rangeEnd': '2026-09-30'},
                         {'rejectedRowCount': 1}, {'expectedDateSetHash': 'b'*64}):
            fund['rows'][0]['priceRange'] = {**proof, **mutation}
            self.assertNotEqual(report_quality(fund, PHASE, dt.datetime.fromisoformat(NOW.replace('Z', '+00:00')))['fields']['prices'], 'complete')
        fund['rows'][0]['priceRange'] = proof
        self.assertTrue(valid_complete_price(proof, 'CN', '600001', START, END))
        self.assertEqual(price_coverage(fund)['priceComplete'], 1)

    def test_prior_only_holdings_under_current_no_equities_still_need_prices_and_baseline(self):
        fund = test_traversal.TraversalTests().retained_fund()
        fund['status'] = 'no-equities'; fund['selectedReport']['holdings'] = []
        fund['rows'][0].update(current=None, previous={'shares': 100})
        fund['rows'][0]['priceRange'] = {'status': 'missing'}
        coverage = price_coverage(fund)
        self.assertEqual(coverage['holdingCount'], 1)
        self.assertEqual(coverage['priceComplete'], 0)
        fields = report_quality(fund, PHASE, dt.datetime.fromisoformat(NOW.replace('Z', '+00:00')))['fields']
        self.assertEqual(fields['prices'], 'missing'); self.assertEqual(fields['baseline'], 'complete')
        extra = normalize_enrichment(coverage, 'no-equities', dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc))
        state = {'code': '000001', 'status': 'no-equities', 'attribution': 'direct', 'representativeCode': '000001', 'enrichment': extra}
        self.assertEqual(enrichment_eligible({'000001': state}), ['000001'])
        fund['previousReport'] = None; fund['rows'] = []
        fields = report_quality(fund, PHASE, dt.datetime.fromisoformat(NOW.replace('Z', '+00:00')))['fields']
        self.assertEqual(fields['prices'], 'not-applicable'); self.assertEqual(fields['baseline'], 'missing')


if __name__ == '__main__':
    unittest.main()
