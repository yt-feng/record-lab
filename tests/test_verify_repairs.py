"""Fixed-source verifier tests with synthetic findings; no network or PDFs."""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_repairs as verify

NOW = dt.datetime(2026, 10, 26, 12, tzinfo=dt.timezone.utc)


def normalized(sample, _details=None, _now=None):
    report = {"sourceUrl": sample["sourceUrl"], "periodEnd": sample["periodEnd"]}
    fund = {"code": sample["code"], "selectedReport": report, "status": "ok"}
    if sample["check"] == "foreign-holdings":
        rows = [{"market": market, "currency": {"US": "USD", "HK": "HKD"}[market], "marketValueCurrency": "CNY", "marketValueYuan": 0}
                for market, count in sample["marketCounts"].items() for _ in range(count)]
        rows[0]["marketValueYuan"] = sample["marketValueYuan"]
        report.update(holdings=rows, navYuan=sample["navYuan"], holdingsPdfEvidence={"complete": True, "rankMode": "pdf-cells", "startPage": 38, "endPage": 58})
    elif sample["check"] == "no-equities":
        fund["status"] = "no-equities"
        report["noEquitiesEvidence"] = {"evidenceType": sample["evidenceType"], "page": sample["page"]}
        if sample["evidenceType"] == "exhaustive-nonstock-assets":
            report["noEquitiesEvidence"] = nonstock_proof(sample)
    else:
        fund.update(managerAttribution="report", managerTableCoverage={"complete": True}, reportManagers=[{"name": name, "startDate": start, "endDate": end,
                    "evidence": {"page": page, "periodEnd": sample["periodEnd"], "sourceUrl": sample["sourceUrl"]}}
                    for name, start, end, page in sample["managerRows"]])
        report.update(strategy_details(sample))
    return fund


def quality(*_):
    return {"fields": {"holdings": "complete", "managers": "complete", "strategy": "complete"}}


def strategy_details(sample, with_outlook=False):
    from strategy_sections import parse_strategy_sections
    quarter = (int(sample["periodEnd"][5:7]) - 1) // 3 + 1
    pages = [f"合成基金2026年第{quarter}季度报告\n4.4 报告期内基金的投资策略和运作分析\n本季度经济和市场回暖。我们增加债券配置，因为估值较为合理。",
             ("未来本基金将关注科技方向。" if with_outlook else "") + "\n4.5 报告期内基金的业绩表现\n合成结束"]
    return parse_strategy_sections(pages, sample, page_numbers=sample["strategyPages"])


def nonstock_proof(sample):
    from nonstock_assets import parse_nonstock_assets
    fixture = json.loads((Path(__file__).parent / "fixtures/nonstock-assets.json").read_text())
    # Substitute only public numeric facts in the synthetic table layout.
    substitutions = {"4,000.00": "14,527,769,865.85", "40.00": "43.92", "3,000.00": "8,413,532,288.59",
                     "30.00": "25.44", "2,999.90": "10,135,724,619.88", "10,000.00": "33,077,230,295.61"}
    asset = fixture["pages"][0]
    for old, new in substitutions.items(): asset = asset.replace(old, new)
    asset = asset.replace("10,135,724,619.88 25.44", "10,135,724,619.88 30.64").replace("0.10", "203,521.29")
    fixture["pages"][0] = asset
    fixture["pages"][1] = fixture["pages"][1].replace("0.04", "44,971.77").replace("0.06", "158,549.52").replace("0.10", "203,521.29")
    return parse_nonstock_assets(fixture["pages"], sample, page_numbers=fixture["pageNumbers"])


class FakeProvider:
    def __init__(self):
        self.calls = []
    def report_details(self, document, max_pages):
        self.calls.append(document["sourceUrl"])
        return {"unpublishedDiagnostic": "must-not-appear", "text": "full extracted content must not be retained"}


class VerifyRepairTests(unittest.TestCase):
    def test_workflow_limits_cloud_job_and_retains_only_the_summary_file(self):
        workflow = (verify.ROOT / ".github/workflows/verify-repairs.yml").read_text()
        self.assertIn("timeout-minutes: 15", workflow)
        self.assertIn("sparse-checkout-cone-mode: false", workflow)
        for pattern in ("/scripts/", "/lib/", "/web/*.js", "/tests/", "/*", "!/*/"):
            self.assertIn(pattern, workflow)
        self.assertIn("path: .cache/repair-verification/result.json", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("contents: write", workflow)

    def test_local_main_refuses_before_provider_or_output(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(verify, "PublicProvider") as provider, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(verify.main(), 2)
        provider.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["errorCode"], "ACTIONS_REQUIRED")

    def test_exactly_six_fixed_sources_and_small_allowlisted_summary(self):
        provider = FakeProvider()
        result = verify.run_verification(provider, normalize=normalized, quality_fn=quality, now=NOW)
        self.assertEqual(len(provider.calls), 6)
        self.assertEqual(len(set(provider.calls)), 6)
        self.assertEqual(result["sampleCount"], 6)
        self.assertEqual(result["passedCount"], 6)
        self.assertFalse(result["provesFullCollection"])
        self.assertFalse(result["productionDataUpdated"])
        self.assertTrue(all(type(item["elapsedSeconds"]) in (int, float) and item["elapsedSeconds"] >= 0 for item in result["results"]))
        encoded = json.dumps(result)
        self.assertLess(len(encoded), 32000)
        self.assertNotIn("must-not-appear", encoded)
        self.assertNotIn("full extracted", encoded)
        self.assertTrue(all(url.startswith("https://pdf.dfcfw.com/pdf/H2_AN") for url in provider.calls))

    def test_foreign_amount_and_currency_proof_both_required(self):
        sample = verify.SAMPLES[0]
        for change in ("amount", "currency", "proof"):
            fund = normalized(sample)
            if change == "amount": fund["selectedReport"]["holdings"][0]["marketValueYuan"] -= 100
            if change == "currency": fund["selectedReport"]["holdings"][0]["marketValueCurrency"] = "USD"
            if change == "proof": fund["selectedReport"]["holdingsPdfEvidence"]["complete"] = False
            self.assertEqual(verify.evaluate(sample, {}, fund, quality())["status"], "failed")

    def test_no_equity_requires_strict_builder_acceptance_and_exact_source_page(self):
        sample = verify.SAMPLES[1]
        fund = normalized(sample)
        fund["status"] = "unavailable"
        self.assertEqual(verify.evaluate(sample, {"confirmed": True}, fund, quality())["status"], "failed")
        fund = normalized(sample)
        fund["selectedReport"]["noEquitiesEvidence"]["page"] = 42
        self.assertFalse(verify.evaluate(sample, {}, fund, quality())["checks"]["sourcePage"])

    def test_correct_manager_names_alone_do_not_pass_without_pdf_quality(self):
        sample = verify.SAMPLES[4]
        fund = normalized(sample)
        fund["managerAttribution"] = "tenure-overlap"
        result = verify.evaluate(sample, {}, fund, {"fields": {"managers": "missing"}})
        self.assertFalse(result["checks"]["reportAttribution"])
        self.assertFalse(result["checks"]["normalizedQuality"])

    def test_download_failure_is_fixed_code_and_other_five_samples_continue(self):
        class Failed(FakeProvider):
            def report_details(self, document, max_pages):
                self.calls.append(document["sourceUrl"])
                if len(self.calls) == 1:
                    raise RuntimeError("private transient diagnostic must not be printed")
                return {}
        provider = Failed()
        result = verify.run_verification(provider, normalize=normalized, quality_fn=quality, now=NOW)
        self.assertEqual(len(provider.calls), 6)
        self.assertEqual(result["passedCount"], 5)
        self.assertEqual(result["results"][0]["errorCode"], "REPAIR_VERIFICATION_FAILED")
        self.assertNotIn("private transient", json.dumps(result))
        self.assertGreaterEqual(result["results"][0]["elapsedSeconds"], 0)

    def test_actual_node_builder_preserves_synthetic_manager_proof(self):
        from report_extract import _details_for_pages
        for sample in verify.SAMPLES[3:5]:
            pages = [""] * 8
            pages[0] = sample["title"]
            for index, (name, start, end, page) in enumerate(sample["managerRows"]):
                header = "4.1 基金经理（或基金经理小组）简介\n姓名 职务 任本基金的基金经理期限 证券从业年限 说明 任职日期 离任日期\n" if index == 0 else ""
                pages[page - 1] = header + f"{name} 本基金基金经理 {start} {end or '-'} 13年\n"
            pages[sample["managerRows"][-1][3] - 1] += "4.2 运作说明"
            pages[6] = "4.4 报告期内基金的投资策略和运作分析\n本季度经济和市场回暖。我们增加债券配置，因为估值较为合理。"
            pages[7] = "4.5 报告期内基金的业绩表现\n合成结束"
            details = _details_for_pages(pages, sample["sourceUrl"], sample["periodEnd"], sample["kind"],
                                         lambda *_: {"navYuan": 10000}, total_pages=8, published_at=sample["publishedAt"])
            details["parseMethod"] = "pypdf"
            fund = verify.normalized_snapshot(sample, details, NOW)
            measured = verify.report_quality(fund, verify.phase_for(sample), NOW)
            result = verify.evaluate(sample, details, fund, measured)
            self.assertEqual(result["status"], "passed", result["checks"])
            self.assertEqual(fund["selectedReport"]["strategyEvidenceRetrievedAt"], NOW.isoformat())
            self.assertNotIn("sections", details["strategyAnalysis"])

    def test_both_manager_samples_require_complete_quarter_strategy_and_selected_evidence(self):
        for sample in verify.SAMPLES[3:5]:
            fund = normalized(sample)
            result = verify.evaluate(sample, {}, fund, quality())
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["observed"]["strategy"]["pages"], [7, 8])
            self.assertFalse(result["observed"]["strategy"]["semanticAnalysisComplete"])
            self.assertEqual(result["observed"]["strategy"]["sentenceCounts"]["outlook"], 0)
            self.assertNotIn("市场回暖", json.dumps(result, ensure_ascii=False))
            for field, value in [("complete", False), ("periodStart", "2026-01-01"),
                                 ("sourceUrl", "https://example.org/other.pdf"), ("publishedAt", "2026-10-25")]:
                broken = normalized(sample)
                broken["selectedReport"]["strategySectionCoverage"][field] = value
                self.assertEqual(verify.evaluate(sample, {}, broken, quality())["status"], "failed")
            for category in sample["strategyRequiredCategories"]:
                broken = normalized(sample)
                broken["selectedReport"]["strategyAnalysis"]["evidence"] = [item for item in broken["selectedReport"]["strategyAnalysis"]["evidence"] if category not in item["categories"]]
                self.assertFalse(verify.evaluate(sample, {}, broken, quality())["checks"]["strategyCategories"])
            broken = normalized(sample)
            broken["selectedReport"]["strategyAnalysis"]["semanticAnalysisComplete"] = True
            self.assertFalse(verify.evaluate(sample, {}, broken, quality())["checks"]["selectedEvidence"])
            measured = quality(); measured["fields"]["strategy"] = "partial"
            self.assertFalse(verify.evaluate(sample, {}, normalized(sample), measured)["checks"]["strategyQuality"])

    def test_no_outlook_is_valid_but_invented_outlook_or_unsupported_category_counts_fail(self):
        for sample in verify.SAMPLES[3:5]:
            self.assertTrue(verify.evaluate(sample, {}, normalized(sample), quality())["checks"]["strategyAbsentCategories"])
            invented = normalized(sample)
            invented["selectedReport"].update(strategy_details(sample, with_outlook=True))
            result = verify.evaluate(sample, {}, invented, quality())
            self.assertTrue(result["checks"]["strategyCategories"])
            self.assertFalse(result["checks"]["strategyAbsentCategories"])
            self.assertEqual(result["status"], "failed")
            for category, value in [("outlook", 1), ("market-review", 0), ("unclassified", 1)]:
                broken = normalized(sample)
                broken["selectedReport"]["strategyAnalysis"]["categorySentenceCounts"][category] = value
                self.assertFalse(verify.evaluate(sample, {}, broken, quality())["checks"]["strategyCategories"])
            # A different source that really contains an outlook can declare
            # that expectation; the generic check does not prohibit the class.
            with_outlook = {**sample, "strategyRequiredCategories": ["market-review", "portfolio-operations", "outlook"],
                            "strategyAbsentCategories": []}
            self.assertEqual(verify.evaluate(with_outlook, {}, invented, quality())["status"], "passed")

    def test_foreign_cells_cross_actual_proof_builder_and_normalization_bridge(self):
        from foreign_holdings import parse_foreign_equity_grids
        from test_foreign_holdings import fixture
        parsed = parse_foreign_equity_grids(fixture())
        extraction = {"parseMethod": "pypdf", "sourceTextHash": "a" * 64,
                      "pageStats": {"pageCount": 40, "totalPages": 40, "processedPages": 40, "truncated": False,
                                    "pageNumbersVerified": True, "charCount": 10000, "nonEmptyPages": 40, "textStatus": "usable"}}
        details = {**extraction, "pdfHoldings": parsed["holdings"], "pdfHoldingsMetadata": parsed["metadata"],
                   "pdfHoldingsExtraction": extraction, "navYuan": 100000}
        fund = verify.normalized_snapshot(verify.SAMPLES[0], details, NOW)
        report = fund["selectedReport"]
        self.assertEqual(len(report["holdings"]), 3)
        self.assertEqual(report["holdingsPdfEvidence"]["rankGroups"], 2)
        self.assertTrue(report["holdingsPdfEvidence"]["complete"])
        self.assertEqual(report["holdingsPdfEvidence"]["sourceTextHash"], "a" * 64)
        self.assertEqual(verify.report_quality(fund, verify.phase_for(verify.SAMPLES[0]), NOW)["fields"]["holdings"], "complete")

    def test_both_no_stock_evidence_types_cross_actual_builder_and_quality(self):
        balance = json.loads((Path(__file__).parent / "fixtures/zero-stock.json").read_text())
        explicit = "7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n本基金不投资于股票。\n7.4 报告期内股票投资组合的重大变动"
        for sample in verify.SAMPLES[1:3]:
            evidence = {**balance, **{key: sample[key] for key in ("sourceUrl", "periodEnd", "publishedAt", "kind", "page", "evidenceType")}}
            if sample["check"] == "no-equities" and sample["evidenceType"] == "explicit-no-stock-detail":
                evidence["text"] = explicit
            details = {"navYuan": 10000, "noEquitiesEvidence": evidence}
            fund = verify.normalized_snapshot(sample, details, NOW)
            measured = verify.report_quality(fund, verify.phase_for(sample), NOW)
            self.assertEqual(verify.evaluate(sample, details, fund, measured)["status"], "passed")

    def test_sixth_nonstock_proof_crosses_real_builder_quality_and_strict_evaluation(self):
        sample = verify.SAMPLES[5]
        details = {"navYuan": 10000, "noEquitiesEvidence": nonstock_proof(sample)}
        fund = verify.normalized_snapshot(sample, details, NOW)
        measured = verify.report_quality(fund, verify.phase_for(sample), NOW)
        result = verify.evaluate(sample, details, fund, measured)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["observed"]["pages"], [8, 12, 13])
        self.assertNotIn("text", json.dumps(result))
        fund["selectedReport"]["noEquitiesEvidence"]["otherAssetsTable"]["total"]["amount"] = "0.00"
        self.assertFalse(verify.evaluate(sample, details, fund, measured)["checks"]["closedTableProof"])


if __name__ == "__main__":
    unittest.main()
