"""Manager source stays attached to its own PDF across extraction and collect."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect import collect_fund
from report_extract import _details_for_pages


TABLE = "4.1 基金经理（或基金经理小组）简介\n姓名 职务 任本基金的基金经理期限 证券从业年限 说明 任职日期 离任日期\n张明 本基金基金经理 2025年1月1日 - 20年\n4.2 运作说明"


class ManagerPipelineTests(unittest.TestCase):
    def test_extraction_maps_verified_pages_but_does_not_invent_mineru_page_numbers(self):
        parser = lambda *args: {"navYuan": 100, "narrative": None}
        result = _details_for_pages([TABLE], "https://example.org/quarter.pdf", "2026-06-30", "quarterly", parser,
                                    page_numbers=[5], published_at="2026-07-20")
        self.assertEqual(result["reportManagers"][0]["evidence"]["page"], 5)
        self.assertTrue(result["managerTableCoverage"]["complete"])
        missing = _details_for_pages([TABLE], "https://example.org/quarter.pdf", "2026-06-30", "quarterly", parser,
                                     page_numbers=[None])
        self.assertEqual(missing["reportManagers"], [])

    def test_collect_keeps_quarter_manager_source_separate_from_interim_and_baseline(self):
        class Source:
            def overview(self, code):
                return {"name": "合成基金", "company": "合成公司", "managers": [{"name": "当前经理"}]}
            def announcements(self, code, as_of):
                return [{"title": "合成报告", "sourceUrl": "https://example.org/" + label + ".pdf", "periodEnd": period, "kind": kind,
                         "publishedAt": published} for label, period, kind, published in
                        [("interim", "2026-06-30", "semiannual", "2026-08-28"),
                         ("quarter", "2026-06-30", "quarterly", "2026-07-20"),
                         ("baseline", "2026-03-31", "quarterly", "2026-04-20")]]
            def holdings(self, code, year):
                return {end: [{"stockCode": "600001", "stockName": "合成股票", "market": "CN", "currency": "CNY",
                               "shares": 100, "weightPct": 5, "marketValueYuan": 500}] for end in ("2026-06-30", "2026-03-31")}
            def report_details(self, doc):
                label = doc["sourceUrl"].rsplit("/", 1)[1].split(".")[0]
                names = {"interim": "中报经理", "quarter": "季度经理", "baseline": "基准经理"}
                return _details_for_pages([TABLE.replace("张明", names[label])], doc["sourceUrl"], doc["periodEnd"], doc["kind"],
                    lambda *args: {"navYuan": 10000, "narrative": None}, published_at=doc["publishedAt"])
        fund, _ = collect_fund({"code": "000001"}, "2026-10-26", provider=Source(), target_period="2026-06-30", baseline_period="2026-03-31")
        current, previous = fund["reports"]
        self.assertEqual(current["sourceUrl"], "https://example.org/interim.pdf")
        self.assertEqual(current["managers"][0]["name"], "季度经理")
        self.assertEqual(current["managerSourceUrl"], "https://example.org/quarter.pdf")
        self.assertTrue(current["managerTableCoverage"]["complete"])
        self.assertEqual(current["managerTableCoverage"]["kind"], "quarterly")
        self.assertEqual(current["managerTableCoverage"]["periodStart"], "2026-04-01")
        self.assertEqual(current["managerTableCoverage"]["publishedAt"], "2026-07-20")
        self.assertEqual(current["publishedAt"], "2026-08-28")
        self.assertEqual(previous["managers"][0]["name"], "基准经理")
        self.assertEqual(fund["managers"][0]["name"], "当前经理")


if __name__ == "__main__":
    unittest.main()
