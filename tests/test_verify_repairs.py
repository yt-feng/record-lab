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
    else:
        fund.update(managerAttribution="report", managerTableCoverage={"complete": True}, reportManagers=[{"name": name, "startDate": start, "endDate": end,
                    "evidence": {"page": page, "periodEnd": sample["periodEnd"], "sourceUrl": sample["sourceUrl"]}}
                    for name, start, end, page in sample["managerRows"]])
    return fund


def quality(*_):
    return {"fields": {"holdings": "complete", "managers": "complete"}}


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

    def test_exactly_five_fixed_sources_and_small_allowlisted_summary(self):
        provider = FakeProvider()
        result = verify.run_verification(provider, normalize=normalized, quality_fn=quality, now=NOW)
        self.assertEqual(len(provider.calls), 5)
        self.assertEqual(len(set(provider.calls)), 5)
        self.assertEqual(result["passedCount"], 5)
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
        sample = verify.SAMPLES[-1]
        fund = normalized(sample)
        fund["managerAttribution"] = "tenure-overlap"
        result = verify.evaluate(sample, {}, fund, {"fields": {"managers": "missing"}})
        self.assertFalse(result["checks"]["reportAttribution"])
        self.assertFalse(result["checks"]["normalizedQuality"])

    def test_download_failure_is_fixed_code_and_other_four_samples_continue(self):
        class Failed(FakeProvider):
            def report_details(self, document, max_pages):
                self.calls.append(document["sourceUrl"])
                if len(self.calls) == 1:
                    raise RuntimeError("private transient diagnostic must not be printed")
                return {}
        provider = Failed()
        result = verify.run_verification(provider, normalize=normalized, quality_fn=quality, now=NOW)
        self.assertEqual(len(provider.calls), 5)
        self.assertEqual(result["passedCount"], 4)
        self.assertEqual(result["results"][0]["errorCode"], "REPAIR_VERIFICATION_FAILED")
        self.assertNotIn("private transient", json.dumps(result))
        self.assertGreaterEqual(result["results"][0]["elapsedSeconds"], 0)

    def test_actual_node_builder_preserves_synthetic_manager_proof(self):
        from report_managers import parse_report_managers
        sample = verify.SAMPLES[3]
        table = "4.1 基金经理（或基金经理小组）简介\n姓名 职务 任本基金的基金经理期限 证券从业年限 说明 任职日期 离任日期\n刘洋 本基金基金经理 2017年10月28日 - 13年\n4.2 运作说明"
        managers = parse_report_managers([""] * 5 + [table], sample)
        details = {"parseMethod": "pypdf", "sourceTextHash": managers.coverage["sourceTextHash"], "navYuan": 10000,
                   "pageStats": {"pageCount": 6, "totalPages": 6, "processedPages": 6, "charCount": 200,
                                 "nonEmptyPages": 1, "truncated": False, "textStatus": "usable"},
                   "reportManagers": managers, "managerTableCoverage": managers.coverage}
        fund = verify.normalized_snapshot(sample, details, NOW)
        measured = verify.report_quality(fund, verify.phase_for(sample), NOW)
        self.assertEqual(verify.evaluate(sample, details, fund, measured)["status"], "passed")

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


if __name__ == "__main__":
    unittest.main()
