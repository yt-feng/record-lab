"""Synthetic extraction-to-publication-to-quality proof boundaries."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from collect import pdf_holdings_evidence
from providers import parse_pdf_holdings
from traversal import report_quality


class PDFPipelineTests(unittest.TestCase):
    def test_only_proven_full_table_reaches_complete_holdings_quality(self):
        page = ('7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n'
                '序号 股票代码 股票名称 数量（股） 公允价值（元） 占基金资产净值比例（%）\n'
                '1 600001 合成证券 100 500.00 5.00\n'
                '7.4 报告期内股票投资组合的重大变动')
        rows = parse_pdf_holdings([page])
        document = {'fundCode': '000001', 'periodEnd': '2026-06-30', 'publishedAt': '2026-08-31',
                    'kind': 'semiannual', 'sourceUrl': 'https://example.org/interim.pdf'}
        stats = {'pageCount': 1, 'totalPages': 1, 'processedPages': 1, 'truncated': False,
                 'pageNumbersVerified': True, 'charCount': len(page), 'nonEmptyPages': 1, 'textStatus': 'usable'}
        details = {'pdfHoldingsMetadata': rows.metadata, 'parseMethod': 'pypdf',
                   'sourceTextHash': 'a' * 64, 'pageStats': stats}
        proof = pdf_holdings_evidence(details, document, rows)
        self.assertTrue(proof['complete'])
        report = {**document, 'holdings': rows, 'coverage': 'full', 'navYuan': 10000,
                  'holdingsSourceUrl': document['sourceUrl'], 'holdingsRetrieval': 'pdf-stock-table-text',
                  'holdingsPossiblyTruncated': False, 'holdingsPdfEvidence': proof,
                  'parseMethod': 'pypdf', 'sourceTextHash': 'a' * 64, 'pageStats': stats}
        script = ("const fs=require('node:fs');const {buildSnapshot}=require('./scripts/build-data');"
                  "const input=JSON.parse(fs.readFileSync(0,'utf8'));"
                  "process.stdout.write(JSON.stringify(buildSnapshot(input,{now:'2026-10-26T11:00:00Z',"
                  "config:{funds:[{code:'000001'}],targetPeriodEnd:'2026-06-30',baselinePeriodEnd:'2026-03-31'}}).funds[0]));")
        phase = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
        for valid in (True, False):
            selected = json.loads(json.dumps(report))
            if not valid:
                selected['holdingsPdfEvidence']['excludedRows'] = 1
            payload = {'asOf': '2026-10-26', 'retrievedAt': '2026-10-26T10:00:00Z', 'errors': [], 'barsByStock': {},
                       'funds': [{'code': '000001', 'name': '合成基金', 'managers': [], 'reports': [selected]}]}
            result = subprocess.run(['node', '-e', script], input=json.dumps(payload), cwd=ROOT,
                                    capture_output=True, text=True, check=True)
            published = json.loads(result.stdout)
            quality = report_quality(published, phase, dt.datetime(2026, 10, 26, 11, tzinfo=dt.timezone.utc))
            self.assertEqual(quality['fields']['holdings'], 'complete' if valid else 'truncated')
            self.assertEqual(published['selectedReport']['holdingsPdfEvidence']['complete'], valid)


if __name__ == '__main__':
    unittest.main()
