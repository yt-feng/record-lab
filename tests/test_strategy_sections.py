"""Section completeness is stronger than one useful strategy excerpt."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from strategy_sections import parse_strategy_sections, normalize_strategy_section_coverage

REPORT = {"sourceUrl": "https://example.org/strategy.pdf", "kind": "quarterly", "periodEnd": "2026-06-30", "publishedAt": "2026-07-20"}
TITLE = "合成证券投资基金2026年第2季度报告\n"
START = "4.4 报告期内基金的投资策略和运作分析\n"
END = "\n4.5 报告期内基金的业绩表现\n本期净值增长99%。"
BODY = "本季度经济和市场回暖。我们增加中国平安的配置，因为估值较为合理。未来本基金将关注科技方向。"
SECURITIES = [{"stockCode": "601318", "market": "CN", "stockName": "中国平安"},
              {"stockCode": "600001", "market": "CN", "stockName": "平安"},
              {"stockCode": "META", "market": "US", "stockName": "Meta Platforms"}]


def extract(pages=None, **kwargs):
    return parse_strategy_sections(pages or [TITLE + START + BODY + END], REPORT, securities=SECURITIES, **kwargs)


class StrategySectionTests(unittest.TestCase):
    def test_complete_section_has_independent_page_and_original_report_proof(self):
        result = extract([TITLE + START + "本季度市场回暖。我们增加中国平安", "的配置，因为估值较为合理。未来本基金将关注科技方向。" + END], page_numbers=[7, 8])
        proof = result["strategySectionCoverage"]
        self.assertTrue(proof["complete"])
        self.assertTrue(normalize_strategy_section_coverage(proof, "2026-06-30")["complete"])
        self.assertEqual(proof["sections"][0]["pages"], [7, 8])
        operation = next(item for item in result["strategyAnalysis"]["evidence"] if "portfolio-operations" in item["categories"])
        self.assertEqual(operation["pages"], [7, 8])
        self.assertNotIn("增长99", result.sections[0]["text"])
        self.assertEqual(operation["securityMentions"][0]["stockCode"], "601318")

    def test_parent_combined_title_child_strategy_stops_before_performance(self):
        text = TITLE + "4.4 管理人对报告期内本基金的投资策略和业绩表现的说明\n4.4.1 报告期内基金投资策略和运作分析\n" + BODY
        text += "\n4.4.2 报告期内基金的业绩表现\n本基金净值增长88%。\n4.5 对未来市场展望\n未来回报未知。"
        result = extract([text])
        self.assertTrue(result["strategySectionCoverage"]["complete"])
        self.assertEqual(result["strategySectionCoverage"]["sections"][0]["endSection"], "4.4.2")
        self.assertNotIn("增长88", result.sections[0]["text"])
        self.assertNotIn("未来回报未知", result.sections[0]["text"])

    def test_nested_subheadings_are_included_until_forward_peer(self):
        result = extract([TITLE + START + "4.4.1 宏观回顾\n本季度经济增长。\n4.4.2 组合操作\n我们增配债券。" + END])
        self.assertTrue(result["strategySectionCoverage"]["complete"])
        self.assertIn("我们增配债券", result.sections[0]["text"])
        self.assertEqual(result["strategySectionCoverage"]["sections"][0]["endSection"], "4.5")

    def test_major_section_marker_bounds_strategy(self):
        result = extract([TITLE + START + BODY + "\n§5 投资组合报告\n5.1 资产组合情况"])
        self.assertTrue(result["strategySectionCoverage"]["complete"])
        self.assertEqual(result["strategySectionCoverage"]["sections"][0]["endSection"], "5")

    def test_wrapped_strategy_heading_is_joined_without_consuming_body(self):
        result = extract([TITLE + "4.4 报告期内基金的投资策略和\n运作分析\n" + BODY + END])
        self.assertTrue(result["strategySectionCoverage"]["complete"])
        self.assertIn("我们增加中国平安", result.sections[0]["text"])

    def test_unknown_noncontiguous_missing_boundary_and_caps_stay_partial(self):
        cases = [([TITLE + START + BODY, BODY + END], {"page_numbers": [7, None]}),
                 ([TITLE + START + BODY, BODY + END], {"page_numbers": [7, 9]}),
                 ([TITLE + START + BODY], {}),
                 ([TITLE + START + BODY] + [BODY] * 8 + [END], {}),
                 ([TITLE + START + BODY * 10 + END], {"max_chars": 100})]
        for pages, kwargs in cases:
            with self.subTest(kwargs=kwargs, pageCount=len(pages)):
                result = extract(pages, **kwargs)
                self.assertFalse(result["strategySectionCoverage"]["complete"])
                self.assertFalse(normalize_strategy_section_coverage(result["strategySectionCoverage"])["complete"])
                self.assertTrue(result["strategyAnalysis"]["evidence"])
                self.assertEqual(result["strategyAnalysis"]["status"], "partial-source")

    def test_document_title_period_and_kind_cannot_be_relabelled(self):
        for title in ["合成基金2026年第3季度报告\n", "合成基金2026年中期报告\n", ""]:
            result = extract([title + START + BODY + END])
            self.assertFalse(result["strategySectionCoverage"]["complete"])
            self.assertIn("MISSING_OR_MISMATCHED_DOCUMENT_PERIOD", result["strategySectionCoverage"]["reasonCodes"])
            self.assertEqual(bool(result["strategyAnalysis"]["evidence"]), not bool(title))
            self.assertTrue(result.sections[0]["text"])

    def test_toc_is_not_a_strategy_section(self):
        result = extract([TITLE + "目录\n4.4 报告期内基金的投资策略和运作分析........7\n4.5 报告期内基金的业绩表现.......8", START + BODY + END])
        self.assertEqual(result["strategySectionCoverage"]["sectionCount"], 1)
        self.assertTrue(result["strategySectionCoverage"]["complete"])

    def test_duplicate_actual_sections_block_whole_document_claim(self):
        result = extract([TITLE + START + BODY + END, START + BODY + END])
        self.assertFalse(result["strategySectionCoverage"]["complete"])
        self.assertIn("MULTIPLE_STRATEGY_SECTIONS", result["strategySectionCoverage"]["reasonCodes"])

    def test_full_text_private_and_short_public_evidence_is_not_semantic_completion(self):
        body = "".join(f"本季度市场第{index}阶段经济有所回升。" for index in range(20)) + BODY
        result = extract([TITLE + START + body + END])
        analysis = result["strategyAnalysis"]
        self.assertTrue(result["strategySectionCoverage"]["complete"])
        self.assertGreater(analysis["omittedSentenceCount"], 0)
        self.assertFalse(analysis["semanticAnalysisComplete"])
        self.assertEqual(analysis["analysisCoverage"], "selected-evidence")
        self.assertLessEqual(len(analysis["evidence"]), 8)
        self.assertTrue(all(len(item["text"]) <= 160 for item in analysis["evidence"]))
        self.assertNotIn(result.sections[0]["text"], json.dumps(result, ensure_ascii=False))

    def test_exact_security_names_only_and_mentions_do_not_assert_trades(self):
        body = "我们关注平安与银行行业。我们研究中国平安，但未持有该公司股票。市场meta叙事仍然存在。我们关注股票代码META。"
        result = extract([TITLE + START + body + END])
        mentions = [mention for item in result["strategyAnalysis"]["evidence"] for mention in item["securityMentions"]]
        self.assertEqual({item["stockCode"] for item in mentions}, {"601318", "META"})
        self.assertTrue(all(item["provesTrade"] is False and item["provesTradeReason"] is False for item in mentions))
        self.assertFalse(result["strategyAnalysis"]["explainsIndividualStockTrades"])

    def test_forged_flags_hash_counts_pages_and_source_cannot_complete(self):
        proof = extract()["strategySectionCoverage"]
        patches = [{"sourceUrl": "http://127.0.0.1/report.pdf"}, {"sourceTextHash": "short"}, {"sentenceCount": 99},
                   {"publishedAt": None}, {"documentPeriodEvidence": []}, {"periodStart": "2026-01-01"}]
        for patch in patches:
            modified = {**proof, **patch, "complete": True}
            self.assertFalse((normalize_strategy_section_coverage(modified) or {}).get("complete", False))
        for patch in [{"endSection": "4.3"}, {"endHeading": "4.3 运作情况"}, {"pages": [1, 3]}, {"textLimitReached": True}]:
            modified = copy.deepcopy(proof); modified["sections"][0].update(patch)
            self.assertFalse(normalize_strategy_section_coverage(modified)["complete"])


if __name__ == "__main__":
    unittest.main()
