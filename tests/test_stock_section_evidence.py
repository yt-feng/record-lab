import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from zero_stock import parse_explicit_no_stock, valid_explicit_no_stock

TEXT = ('7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n'
        '本基金不投资于股票。\n7.4 报告期内股票投资组合的重大变动')
REPORT = {'sourceUrl': 'https://example.org/interim.pdf', 'periodEnd': '2026-06-30',
          'publishedAt': '2026-08-31', 'kind': 'semiannual', 'title': '合成基金中期报告'}


class StockSectionEvidenceTests(unittest.TestCase):
    def test_explicit_full_stock_section_and_immediate_next_heading(self):
        evidence = parse_explicit_no_stock(['其他页', TEXT + '\n7.4.1 累计买入股票明细'], REPORT)
        self.assertTrue(valid_explicit_no_stock(evidence))
        self.assertEqual(evidence['page'], 2)
        self.assertEqual(evidence['text'], TEXT)
        self.assertEqual(evidence['evidenceType'], 'explicit-no-stock-detail')
        wrapped = TEXT.replace('股票投资明细', '股票投\n资明细')
        self.assertIsNotNone(parse_explicit_no_stock([wrapped], REPORT))

    def test_mandate_top_ten_restricted_options_and_extra_statement_are_not_proof(self):
        cases = [TEXT.replace('所有股票投资明细', '前十名股票投资明细'),
                 TEXT.replace('所有股票投资明细', '所有流通受限股票投资明细'),
                 TEXT.replace('所有股票投资明细', '所有股票期权投资明细'),
                 TEXT.replace('期末按公允价值占基金资产净值比例大小排序的所有股票投资明细', '基金投资范围'),
                 TEXT.replace('本基金不投资于股票。', '本基金不投资于股票。另持有股票。'),
                 TEXT.replace('本基金不投资于股票。', '本基金并非不投资于股票。'),
                 TEXT.replace('本基金不投资于股票。', '本基金不投资于股票期权。'),
                 TEXT.replace('7.4 报告期', '7.5 报告期'),
                 TEXT.split('\n7.4')[0]]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(parse_explicit_no_stock([text], REPORT))
                self.assertFalse(valid_explicit_no_stock({**REPORT, 'text': text, 'evidenceType': 'explicit-no-stock-detail'}))
        self.assertIsNone(parse_explicit_no_stock(TEXT.split('\n', 1), REPORT))

    def test_target_period_and_report_kind_are_checked(self):
        for changes in ({'periodEnd': '2026-07-01'}, {'periodEnd': '2026-09-30'}, {'kind': 'annual'}, {'kind': 'unknown'}):
            self.assertIsNone(parse_explicit_no_stock([TEXT], {**REPORT, **changes}))


if __name__ == '__main__':
    unittest.main()
