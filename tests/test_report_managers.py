"""Synthetic table fixtures; no PDF downloads, services, or private paths."""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from report_managers import parse_report_managers


HEADER = "4.1 基金经理（或基金经理小组）简介\n姓名 职务 任本基金的基金经理期限 证券从业年限 说明 任职日期 离任日期\n"
REPORT = {"sourceUrl": "https://example.org/report.pdf", "periodEnd": "2026-09-30", "kind": "quarterly"}


def extract(body, **kwargs):
    return parse_report_managers([HEADER + body], REPORT, **kwargs)


class ReportManagerTests(unittest.TestCase):
    def test_wrapped_appointment_and_bounded_source_proof(self):
        row = "王明 本基金基金经理\n2017\n年10\n月28\n日\n- 13年\n女，经济学硕士。履历文字。\n4.2 管理人对报告期内基金运作的说明"
        result = extract(row)[0]
        self.assertEqual((result["name"], result["startDate"], result["endDate"]), ("王明", "2017-10-28", None))
        self.assertEqual((result["periodStart"], result["periodEnd"], result["activeAtPeriodEnd"]), ("2026-07-01", "2026-09-30", True))
        self.assertEqual(result["evidence"]["sourceUrl"], REPORT["sourceUrl"])
        self.assertEqual(result["evidence"]["page"], 1)
        self.assertEqual(result["evidence"]["method"], "explicit-report-manager-table")
        self.assertNotIn("履历", result["evidence"]["text"])

    def test_comanagers_continue_on_next_page_and_ignore_resume_and_other_product_table(self):
        pages = ["封面", HEADER + "张明 本基金的\n基金经理\n2025 年10 月\n24 日 - 13 年\n曾就职于证券公",
                 "某基金2026年第3季度报告\n第6页 共13页\n司。2024年12月起担任其他证券投资基金基金经理。\n李华 本基金的\n基金经理\n2026 年8 月1\n日 - 26 年\n"
                 "2019年4月起担任其他混合型证券投资基金基金经理。\n"
                 "4.1.1 期末兼任私募资产管理计划投资经理的基金经理同时管理的产品情况\n"
                 "赵亮 本基金基金经理 2019年4月22日 - 10年\n4.2 运作说明"]
        result = parse_report_managers(pages, REPORT)
        self.assertEqual([(r["name"], r["startDate"], r["evidence"]["page"]) for r in result],
                         [("张明", "2025-10-24", 2), ("李华", "2026-08-01", 3)])

    def test_departed_and_incoming_managers_both_belong_to_quarter(self):
        result = extract("张明 本基金基金经理 2020-01-01 2026-08-02 15年\n"
                         "李华 基金经理 2026-08-02 - 12年\n"
                         "赵亮 本基金基金经理 2026-10-01 - 10年\n"
                         "王明 本基金基金经理 2020-01-01 2026-06-30 10年")
        self.assertEqual([(r["name"], r["activeAtPeriodEnd"]) for r in result], [("张明", False), ("李华", True)])

    def test_director_only_assistant_and_biography_mentions_are_not_managers(self):
        rows = ["张明 董事长 2020-01-01 - 20年", "李华 本基金基金经理助理 2020-01-01 - 12年",
                "王明 曾担任本基金基金经理 2020-01-01 - 10年",
                "赵亮 公司总经理 2020-01-01 - 20年", "陈明 某证券投资基金经理 2020-01-01 - 15年"]
        self.assertEqual(extract("\n".join(rows)), [])
        valid = extract("张明 权益投资部总经理、\n本基金基金经理 2020-01-01 - 20年")
        self.assertEqual([r["name"] for r in valid], ["张明"])

    def test_requires_section_and_explicit_tenure_headers(self):
        row = "张明 本基金基金经理 2020-01-01 - 20年"
        for header in ["", HEADER.replace("任职日期", ""), HEADER.replace("离任日期", ""),
                       HEADER.replace("任本基金的基金经理期限", "其他基金任职情况"), HEADER.replace("4.1", "4.2")]:
            self.assertEqual(parse_report_managers([header + row], REPORT), [])

    def test_invalid_dates_missing_date_columns_and_unrelated_resume_dates_rejected(self):
        for row in ["张明 本基金基金经理 2026-02-30 - 20年", "张明 本基金基金经理 2026-08-01 2026-07-01 20年",
                    "张明 本基金基金经理\n20年\n曾于2020年1月1日入职，2021年3月1日担任其他基金经理。",
                    "张明 本基金基金经理 2020年1月1日 20年"]:
            self.assertEqual(extract(row), [])

    def test_explicit_pdf_page_mapping_or_unknown_mapping(self):
        pages = [HEADER + "张明 本基金基金经理 2020-01-01 - 20年"]
        result = parse_report_managers(pages, REPORT, page_numbers=[7])
        self.assertEqual(result[0]["evidence"]["page"], 7)
        for numbers in [[None], [False], [0], [], [7, 8]]:
            self.assertEqual(parse_report_managers(pages, REPORT, page_numbers=numbers), [])

    def test_duplicate_table_does_not_repeat_manager(self):
        row = "张明 本基金基金经理 2020-01-01 - 20年\n"
        result = parse_report_managers([HEADER + row, HEADER + row], REPORT)
        self.assertEqual(len(result), 1)

    def test_missing_first_row_date_cannot_borrow_next_managers_columns(self):
        result = extract("张明 本基金基金经理\n李华 本基金基金经理 2026年8月1日 - 20年")
        self.assertEqual([(r["name"], r["startDate"]) for r in result], [("李华", "2026-08-01")])

    def test_missing_intermediate_page_cannot_establish_table_continuation(self):
        pages = [HEADER, "李华 本基金基金经理 2026年8月1日 - 20年"]
        for mapping in [[None, 6], [4, 6]]:
            self.assertEqual(parse_report_managers(pages, REPORT, page_numbers=mapping), [])
        result = parse_report_managers(pages, REPORT, page_numbers=[5, 6])
        self.assertEqual((result[0]["evidence"]["tablePage"], result[0]["evidence"]["page"]), (5, 6))

    def test_notes_end_the_explicit_table(self):
        result = extract("张明 本基金基金经理 2020-01-01 - 20年\n注：相关任职日期以公告为准。\n李华 本基金基金经理 2026-08-01 - 10年")
        self.assertEqual([r["name"] for r in result], ["张明"])

    def test_document_report_period_cannot_be_relabelled(self):
        row = "张明 本基金基金经理 2020-01-01 - 20年"
        self.assertEqual(parse_report_managers(["某基金2026年第2季度报告\n" + HEADER + row], REPORT), [])
        result = parse_report_managers(["某基金2026 年第3 季度报告\n" + HEADER + row], REPORT)
        self.assertEqual(len(result), 1)

    def test_semiannual_title_anchor_ignores_history_list_but_not_wrong_cover_or_kind(self):
        report = {**REPORT, 'kind': 'semiannual', 'periodEnd': '2026-06-30', 'publishedAt': '2026-08-28'}
        body = HEADER + '张明 本基金基金经理 2020-01-01 - 20年\n4.2 管理人报告'
        history = '14.5 信息披露\n合成基金2025年第4季度报告\n合成基金2025年年度报告\n合成基金2026年第1季度报告'
        result = parse_report_managers(['合成基金2026年中期报告', body, history], report)
        self.assertEqual([r['name'] for r in result], ['张明'])
        self.assertTrue(result.coverage['complete'])
        for first in ['合成基金2025年年度报告', '合成基金2026年第2季度报告']:
            self.assertEqual(parse_report_managers([first, body, history + '\n合成基金2026年中期报告'], report), [])
        self.assertEqual(parse_report_managers(['历史公告列表\n合成基金2026年中期报告', body], report), [])

    def test_wrapped_and_spaced_titles_cannot_bypass_quarter_or_kind_binding(self):
        report = {**REPORT, 'periodEnd': '2026-06-30', 'publishedAt': '2026-07-21'}
        body = HEADER + '张明 本基金基金经理 2020-01-01 - 20年\n4.2 管理人报告'
        for token in ['2026年第2季度\n报告', '2026年第2季 度报 告', '2026\n年\n第2季度\n报\n告']:
            rows = parse_report_managers(['合成证券投资基金\n' + token, body], report)
            self.assertEqual([row['name'] for row in rows], ['张明'])
            self.assertTrue(rows.coverage['complete'])
        for token in ['2026年第3季 度报告', '2026年第3季度\n报告',
                      '2026年中期报 告', '2026年中期报\n告', '2 0 2 6 年半 年 度报 告']:
            with self.subTest(token=token):
                rows = parse_report_managers(['合成证券投资基金\n' + token, body], report)
                self.assertEqual(rows, [])
                self.assertFalse(rows.coverage['complete'])

    def test_invalid_report_context_does_not_create_metadata(self):
        for patch in [{"periodEnd": "2026-09-29"}, {"periodEnd": "2026-02-30"}, {"kind": "termination"},
                      {"sourceUrl": "file:///report.pdf"}, {"sourceUrl": "https://" + "private:secret" + "@" + "example.test/report.pdf"}]:
            self.assertEqual(parse_report_managers([HEADER + "张明 本基金基金经理 2020-01-01 - 20年"], {**REPORT, **patch}), [])

    def test_interim_intro_subsection_uses_half_year_interval(self):
        header = HEADER.replace("4.1 ", "4.1.2 ").replace("简介", "及基金经理助理简介")
        report = {**REPORT, "periodEnd": "2026-06-30", "kind": "interim"}
        result = parse_report_managers([header + "张明 本基金基金经理 2020-01-01 2026-02-01 20年\n4.1.3 其他人员"], report)
        self.assertEqual(result[0]["periodStart"], "2026-01-01")
        self.assertFalse(result[0]["activeAtPeriodEnd"])
        self.assertEqual(parse_report_managers([header + "张明 本基金基金经理 2020-01-01 2026-02-01 20年"],
                         {**report, "kind": "semiannual"})[0]["periodStart"], "2026-01-01")

    def test_markdown_and_html_rows_keep_same_contract(self):
        markdown = "## 4.1 基金经理（或基金经理小组）简介\n|姓名|职务|任本基金的基金经理期限|证券从业年限|说明|\n|任职日期|离任日期|\n|张明|本基金基金经理|2020年1月1日|-|20年|合成履历|"
        html_table = "<h3>4.1 基金经理（或基金经理小组）简介</h3>\n<table><tr><th>姓名</th><th>职务</th><th>任本基金的基金经理期限</th><th>证券从业年限</th></tr><tr><th>任职日期</th><th>离任日期</th></tr><tr><td>张明</td><td>本基金基金经理</td><td>2020年1月1日</td><td>-</td><td>20年</td></tr></table>"
        for text in [markdown, html_table]:
            result = parse_report_managers([text], REPORT, page_numbers=[4])
            self.assertEqual([(r["name"], r["startDate"], r["evidence"]["page"]) for r in result], [("张明", "2020-01-01", 4)])


if __name__ == "__main__":
    unittest.main()
