"""Synthetic cell extraction through the actual build and retry checkpoint."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from collect import pdf_holdings_evidence
from foreign_holdings import parse_foreign_equity_grids
from providers import PublicProvider
from traversal import compact_enrichment, missing_stock_objects, price_coverage, report_quality, valid_price_coverage
from test_foreign_holdings import fixture


class ForeignPipelineTests(unittest.TestCase):
    def test_cells_build_and_missing_price_retry_roundtrip(self):
        parsed = parse_foreign_equity_grids(fixture())
        stats = {'pageCount': 40, 'totalPages': 40, 'processedPages': 40, 'truncated': False,
                 'pageNumbersVerified': True, 'charCount': 10000, 'nonEmptyPages': 40, 'textStatus': 'usable'}
        extraction = {'parseMethod': 'pypdf', 'sourceTextHash': 'a' * 64, 'pageStats': stats}
        document = {'fundCode': '000001', 'periodEnd': '2026-06-30', 'publishedAt': '2026-08-31',
                    'kind': 'semiannual', 'sourceUrl': 'https://example.org/interim.pdf'}
        details = {'pdfHoldingsMetadata': parsed['metadata'], 'pdfHoldingsExtraction': extraction,
                   'parseMethod': 'mineru', 'sourceTextHash': 'b' * 64, 'pageStats': {}}
        proof = pdf_holdings_evidence(details, document, parsed['holdings'])
        self.assertTrue(proof['complete'])
        self.assertEqual(proof['sourceTextHash'], 'a' * 64)
        self.assertEqual(proof['rankGroups'], 2)
        report = {**document, **extraction, 'holdings': parsed['holdings'], 'coverage': 'full', 'navYuan': 100000,
                  'holdingsSourceUrl': document['sourceUrl'], 'holdingsRetrieval': 'pdf-stock-table-cells',
                  'holdingsPossiblyTruncated': False, 'holdingsPdfEvidence': proof}
        payload = {'asOf': '2026-10-26', 'retrievedAt': '2026-10-26T10:00:00Z', 'errors': [],
                   'barsByStock': {'US:GOOG': {'market': 'US', 'stockCode': 'GOOG', 'currency': 'USD',
                                             'priceBasis': 'unadjusted', 'sourceUrl': 'https://example.org/prices',
                                             'rows': [{'date': '2026-04-01', 'low': 1, 'high': 2, 'close': 1.5}]}},
                   'funds': [{'code': '000001', 'name': '合成基金', 'managers': [], 'reports': [report]}]}
        script = ("const fs=require('node:fs');const {buildSnapshot}=require('./scripts/build-data');"
                  "const input=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(buildSnapshot(input,"
                  "{now:'2026-10-26T11:00:00Z',config:{funds:[{code:'000001'}],targetPeriodEnd:'2026-06-30',"
                  "baselinePeriodEnd:'2026-03-31'}}).funds[0]));")
        process = subprocess.run(['node', '-e', script], cwd=ROOT, input=json.dumps(payload), capture_output=True, text=True, check=True)
        fund = json.loads(process.stdout)
        quality = report_quality(fund, {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'},
                                 dt.datetime(2026, 10, 26, 11, tzinfo=dt.timezone.utc))
        self.assertEqual(quality['fields']['holdings'], 'complete')
        coverage = price_coverage(fund)
        self.assertEqual((coverage['holdingCount'], coverage['priceAvailable']), (3, 1))
        self.assertTrue(valid_price_coverage(coverage))
        checkpoint = compact_enrichment(coverage)
        self.assertEqual(coverage['priceComplete'], 0)
        self.assertEqual(checkpoint['missingStockKeys'], ['US:GOOG', 'US:GOOGL', 'US:NVDA'])
        self.assertEqual(missing_stock_objects(checkpoint), coverage['missingStocks'])

    def test_provider_attaches_original_pdf_geometry_and_dispatches_us_prices(self):
        parsed = parse_foreign_equity_grids(fixture())
        parsed['extraction'] = {'parseMethod': 'pypdf', 'sourceTextHash': 'a' * 64, 'pageStats': {}}
        provider = PublicProvider()
        provider.get = lambda *_args, **_kwargs: b'%PDF-synthetic'
        with patch('report_extract.extract_report', return_value={'pdfHoldings': [], 'pdfHoldingsMetadata': {'foreignTableCandidate': True}}), \
                patch('foreign_holdings.parse_foreign_equity_pdf', return_value=parsed) as parser:
            result = provider.report_details({'sourceUrl': 'https://example.org/report.pdf', 'periodEnd': '2026-06-30', 'kind': 'semiannual'})
        self.assertEqual(len(result['pdfHoldings']), 3)
        self.assertEqual(result['pdfHoldingsMetadata']['rankMode'], 'pdf-cells')
        parser.assert_called_once_with(b'%PDF-synthetic', max_pages=300)
        with patch('foreign_prices.us_price_bars', return_value={'currency': 'USD'}) as prices:
            self.assertEqual(provider.price_bars('BRK.B', 'US', '2026-04-01', '2026-06-30'), {'currency': 'USD'})
            prices.assert_called_once_with(provider, 'BRK.B', '2026-04-01', '2026-06-30')


if __name__ == '__main__':
    unittest.main()
