"""Synthetic opening-title and incidental-history discrimination; no PDFs."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from report_period import document_periods, period_token


class ReportPeriodTests(unittest.TestCase):
    def test_real_estate_wrapped_cover_is_not_poisoned_by_late_history_titles(self):
        cover = '第1页 共69页\n合成高速公路封闭式基础设施\n证券投资基金\n2026 年中期报告'
        history = ('14.5 其他重大事件的信息披露\n合成基金2025年第4季度报告\n'
                   '合成基金2025年年度报告\n合成基金2026年第1季度报告')
        pages = [cover, '重要提示'] + ['正文'] * 64 + [history]
        self.assertEqual(document_periods(pages), [
            {'periodEnd': '2026-06-30', 'kind': 'semiannual', 'page': 1, 'text': '2026年中期报告'}])
        self.assertEqual(document_periods(pages, preserve_spacing=True)[0]['text'], '2026 年中期报告')

    def test_incorrect_cover_cannot_be_overridden_by_expected_period_in_history(self):
        result = document_periods(['合成基金2025年年度报告', '重要提示',
            '历史公告列表\n合成基金2026年中期报告'])
        self.assertEqual([(x['kind'], x['periodEnd']) for x in result], [('annual', '2025-12-31')])

    def test_reference_tables_and_narrative_mentions_are_not_opening_titles(self):
        cases = ['历史公告列表\n合成基金2026年中期报告',
                 '14.5 信息披露\n合成基金2026年中期报告',
                 '详见合成基金2026年中期报告',
                 '合成基金2026年中期报告 于本月披露',
                 '1 | 合成基金2026年中期报告 | 2026-08-28',
                 '目录\n合成基金2026年中期报告........4']
        for value in cases:
            with self.subTest(value=value): self.assertEqual(document_periods([value]), [])

    def test_conflicting_opening_titles_remain_conflicting_and_unknown_pages_stay_unknown(self):
        for pages in [['合成基金2026年中期报告', '合成基金2025年年度报告'],
                      ['合成基金2026年中期报告\n2025年年度报告']]:
            self.assertEqual(len(document_periods(pages)), 2)
        self.assertIsNone(document_periods(['合成基金2026年中期报告'], [None])[0]['page'])

    def test_strict_running_header_fallback_preserves_physical_pages(self):
        result = document_periods(['封面', '4.1 基金经理简介', '合成基金2026年第3季度报告\n正文'], [4, 5, 6])
        self.assertEqual(result[0]['page'], 6)
        self.assertEqual(result[0]['periodEnd'], '2026-09-30')
        self.assertEqual(document_periods(['封面', '4.1 基金经理简介', '2025年年度报告\n历史表']), [])

    def test_late_different_report_cover_is_conflict_not_an_ignored_reference(self):
        for late in ['合成基金2025年年度报告\n4.4 报告期内基金投资策略和运作分析',
                     '合成证券投资\n基金\n2025年年度报告']:
            values = document_periods(['合成基金2026年中期报告', '正文', late])
            self.assertEqual({r['periodEnd'] for r in values}, {'2026-06-30', '2025-12-31'})

    def test_bare_period_validation_and_kind_never_borrow_comparison_dates(self):
        self.assertEqual(period_token('2026年中期报告'), ('semiannual', '2026-06-30'))
        self.assertEqual(period_token('2026年第2季度报告'), ('quarterly', '2026-06-30'))
        self.assertIsNone(period_token('2026年6月30日'))
        self.assertIsNone(period_token('详见2026年中期报告'))

    def test_wrapped_tokens_and_internal_spaces_preserve_kind_and_physical_page(self):
        for token, kind, end in [('2026年第2季度\n报告', 'quarterly', '2026-06-30'),
                                 ('2026年第3季 度报 告', 'quarterly', '2026-09-30'),
                                 ('2026年中期报\n告', 'semiannual', '2026-06-30'),
                                 ('2 0 2 6 年半 年 度报 告', 'semiannual', '2026-06-30'),
                                 ('2026\n年\n第2季度\n报\n告', 'quarterly', '2026-06-30')]:
            with self.subTest(token=token):
                values = document_periods(['合成证券投资基金\n' + token], [7], preserve_spacing=True)
                self.assertEqual([(v['kind'], v['periodEnd'], v['page']) for v in values], [(kind, end, 7)])
                self.assertEqual(period_token(values[0]['text']), (kind, end))

    def test_partial_title_cannot_join_across_history_sections_prose_or_pages(self):
        for boundary in ['历史公告列表', '14.5 信息披露', '重要提示', '本基金正文内容']:
            self.assertEqual(document_periods(['合成基金\n2026年第2季度\n' + boundary + '\n报告']), [])
        self.assertEqual(document_periods(['合成基金\n2026年第2季度', '报告']), [])
        values = document_periods(['合成基金\n2026年中期报\n告', '正文',
                                  '14.5 信息披露\n合成基金2025年第4季 度报 告'])
        self.assertEqual([(v['kind'], v['periodEnd']) for v in values], [('semiannual', '2026-06-30')])


if __name__ == '__main__': unittest.main()
