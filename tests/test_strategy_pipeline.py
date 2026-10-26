"""Quarter strategy proof keeps its own original PDF beside interim holdings."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect import collect_fund
from providers import parse_pdf_pages
from report_extract import _details_for_pages


class Source:
    def overview(self, code):
        return {"name": "合成基金", "company": "合成公司", "managers": []}

    def announcements(self, code, as_of):
        return [{"title": title, "sourceUrl": f"https://example.org/{label}.pdf", "periodEnd": period, "kind": kind, "publishedAt": published}
                for label, title, period, kind, published in [
                    ("interim", "合成基金2026年中期报告", "2026-06-30", "semiannual", "2026-08-28"),
                    ("quarter", "合成基金2026年第2季度报告", "2026-06-30", "quarterly", "2026-07-20"),
                    ("baseline", "合成基金2026年第1季度报告", "2026-03-31", "quarterly", "2026-04-20")]]

    def holdings(self, code, year):
        return {end: [{"stockCode": "601318", "stockName": "中国平安", "market": "CN", "currency": "CNY",
                       "shares": 100, "weightPct": 5, "marketValueYuan": 500}] for end in ("2026-06-30", "2026-03-31")}

    def report_details(self, document):
        text = document["title"] + "\n4.4 报告期内基金的投资策略和运作分析\n本基金在报告期内基于估值变化增加中国平安配置。\n4.5 报告期内基金的业绩表现\n本期基金净值增长。"
        return _details_for_pages([text], document["sourceUrl"], document["periodEnd"], document["kind"],
            lambda *args: {**parse_pdf_pages(*args), "navYuan": 10000}, published_at=document["publishedAt"])


class StrategyPipelineTests(unittest.TestCase):
    def test_collect_uses_quarterly_strategy_source_without_relabelling_interim_holdings(self):
        fund, errors = collect_fund({"code": "000001"}, "2026-10-26", provider=Source(),
            target_period="2026-06-30", baseline_period="2026-03-31")
        current, previous = fund["reports"]
        self.assertEqual(current["sourceUrl"], "https://example.org/interim.pdf")
        self.assertEqual(current["publishedAt"], "2026-08-28")
        proof = current["strategySectionCoverage"]
        self.assertTrue(proof["complete"])
        self.assertEqual((proof["sourceUrl"], proof["publishedAt"], proof["periodStart"]),
                         ("https://example.org/quarter.pdf", "2026-07-20", "2026-04-01"))
        self.assertEqual(current["strategyAnalysis"]["analysisCoverage"], "selected-evidence")
        self.assertEqual(previous["strategySectionCoverage"]["periodEnd"], "2026-03-31")
        self.assertIn("strategyEvidenceRetrievedAt", current)

    def test_unpaginated_extraction_keeps_legacy_excerpt_but_has_no_complete_section(self):
        document = Source().announcements("000001", "2026-10-26")[1]
        text = document["title"] + "\n4.4 报告期内基金的投资策略和运作分析\n本基金在报告期内基于估值变化增加中国平安配置。\n4.5 报告期内基金的业绩表现"
        result = _details_for_pages([text], document["sourceUrl"], document["periodEnd"], document["kind"], parse_pdf_pages,
            page_numbers=[None], published_at=document["publishedAt"])
        self.assertFalse(result["strategySectionCoverage"]["complete"])
        self.assertTrue(result["strategyExcerpts"])
        self.assertFalse(result["strategyAnalysis"]["semanticAnalysisComplete"])


if __name__ == "__main__":
    unittest.main()
