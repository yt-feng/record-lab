"""Synthetic parser fixtures; never market-data seeds."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from providers import DataError, js_literal, parse_announcements, parse_holdings_html, parse_pdf_pages, report_period
from collect import collect_fund


class ParserTests(unittest.TestCase):
    def test_report_dates_require_real_publication_and_reject_future(self):
        data = {"Data": [
            {"TITLE": "示例基金2026年第2季度报告", "PUBLISHDATEDESC": "2026-07-21", "ART_CODE": "AN202607211234567890"},
            {"TITLE": "示例基金2026年第3季度报告", "PUBLISHDATEDESC": "2026-10-21", "ART_CODE": "AN202610211234567890"},
            {"TITLE": "示例基金2026年中期报告", "ART_CODE": "AN202608311234567890"},
        ]}
        result = parse_announcements(data, "2026-09-29")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["periodEnd"], "2026-06-30")
        self.assertEqual(report_period("示例基金2026年第 一 季度报告"), ("2026-03-31", "quarterly"))
        self.assertIsNone(report_period("示例基金2026年中期报告提示公告"))

    def test_holdings_share_and_money_units_and_hk_identity(self):
        html = '''<div class="box"><h4 class="t">示例基金 2026年2季度股票投资明细</h4>
        <p>截止至：2026-06-30</p><table><thead><tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>相关资讯</th><th>占净值 比例</th><th>持股数 （万股）</th><th>持仓市值 （万元）</th></tr></thead>
        <tbody><tr><td>1</td><td>00700</td><td>示例港股</td><td>资讯</td><td>5.20%</td><td>12.50</td><td>5,000.25</td></tr></tbody></table></div>'''
        payload = "var apidata={content:" + json.dumps(html) + ",arryear:[2026]};"
        row = parse_holdings_html(payload)["2026-06-30"][0]
        self.assertEqual(row["shares"], 125000)
        self.assertEqual(row["marketValueYuan"], 50002500)
        self.assertEqual(row["weightPct"], 5.2)
        self.assertEqual(row["stockCode"], "00700")
        self.assertEqual(row["currency"], "HKD")

    def test_js_not_executed_and_truncation_is_error(self):
        self.assertEqual(js_literal('var apidata={content:"safe\\ntext"}; malicious()', "content"), "safe\ntext")
        with self.assertRaises(DataError):
            js_literal('var apidata={content:"missing end', "content")

    def test_multiple_share_classes_do_not_use_one_nav_cell(self):
        pages = ["基金主代码 000001\n下属分级基金的基金简称 示例A 示例C\n4.期末基金资产净值 10,000,000.00\n20,000,000.00"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "quarterly")
        self.assertIsNone(result["navYuan"])
        self.assertEqual(result["portfolioCode"], "000001")

    def test_interim_balance_uses_current_portfolio_nav_not_previous_year(self):
        pages = ["基金主代码 000001\n下属分级基金的基金简称 示例A 示例C",
                 "6.1 资产负债表\n本期末 2026 年 6 月 30 日 上年度末2025年12月31日\n净资产合计 30,000,000.00 50,000,000.00\n6.2 利润表"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "semiannual")
        self.assertEqual(result["navYuan"], 30000000)

    def test_nav_and_narrative_carry_period_page_and_bounded_text(self):
        pages = ["基金主代码 000001\n4.期末基金资产净值 10,000,000.00\n5.期末基金份额净值 1.20",
                 "4.4 报告期内基金投资策略和运作分析\n本基金本季度调整了行业配置，增加若干行业的配置，并根据公司经营情况、估值水平以及行业竞争格局进行了组合调整。\n4.5 报告期内基金的业绩表现\n业绩说明"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "quarterly")
        self.assertEqual(result["navYuan"], 10000000)
        self.assertEqual(result["narrative"]["page"], 2)
        self.assertEqual(result["narrative"]["periodStart"], "2026-04-01")
        self.assertNotIn("业绩说明", result["narrative"]["text"])

    def test_operation_excerpt_skips_macro_and_cites_actual_page(self):
        pages = ["4.4 报告期内基金投资策略和运作分析\n全球经济与证券市场受到各类事件影响。" + "指数变动与宏观背景说明。" * 30,
                 "本基金本季度增加了部分行业的配置，并依据企业经营与估值对组合进行了调整，降低了另一些行业的持仓比例。\n4.5 报告期内基金的业绩表现\n业绩说明"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "quarterly")
        excerpt = result["narrative"]
        self.assertEqual(excerpt["page"], 2)
        self.assertEqual(excerpt["excerptSelection"], "operation-sentences")
        self.assertIn("增加了", excerpt["text"])
        self.assertNotIn("全球经济", excerpt["text"])
        self.assertLessEqual(len(excerpt["text"]), 260)

    def test_report_identity_mismatch_is_excluded(self):
        data = {"Data": [{"FCODE": "000002", "TITLE": "示例基金2026年第2季度报告", "PUBLISHDATEDESC": "2026-07-21", "ART_CODE": "AN202607211234567890"}]}
        self.assertEqual(parse_announcements(data, "2026-09-29", "000001"), [])

    def test_failed_source_preserves_missing_and_sanitized_errors(self):
        class FailedProvider:
            def overview(self, code):
                raise RuntimeError("secret-and-local-path")
            def announcements(self, code, as_of):
                raise DataError("TIMEOUT")
        fund, errors = collect_fund({"code": "000001"}, "2026-09-29", provider=FailedProvider())
        self.assertEqual(fund["reports"], [])
        self.assertEqual(errors[0]["errorCode"], "PARSE_FAILED")
        self.assertNotIn("secret", json.dumps(errors))


if __name__ == "__main__":
    unittest.main()
