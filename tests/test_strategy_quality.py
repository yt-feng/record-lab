"""Strategy proof migration preserves unrelated budgets and old useful text."""
import copy
import unittest
import test_traversal as support
from test_traversal import traversal, CONFIG, PHASE, NOW, catalog, event


def previous(quality):
    return {"schemaVersion": 1, "phase": PHASE, "collectorVersion": 4, "funds": [{"code": "000001", "companyCode": "co1",
        "status": "complete", "attempts": 1, "lastAttemptAt": "2026-09-29T10:00:00Z",
        "quality": {"managerTableCoverageVersion": traversal.MANAGER_TABLE_COVERAGE_VERSION, "priceCoverageVersion": 2, **quality}}]}


class StrategyQualityTests(unittest.TestCase):
    def test_single_legacy_excerpt_is_partial_until_full_section_proof(self):
        fund = support.TraversalTests().retained_fund()
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "complete")
        proof = fund["selectedReport"].pop("strategySectionCoverage")
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "partial")
        self.assertEqual(fund["narrative"]["text"], "合成季度原文摘录")
        proof["sections"][0]["sectionBounded"] = False
        fund["selectedReport"]["strategySectionCoverage"] = proof
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "partial")

    def test_unknown_page_and_future_published_proof_never_complete(self):
        for change in [lambda proof: proof["sections"][0].update(pages=[None]), lambda proof: proof.update(publishedAt="2026-12-01")]:
            fund = support.TraversalTests().retained_fund()
            change(fund["selectedReport"]["strategySectionCoverage"])
            self.assertNotEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "complete")

    def test_strategy_observation_is_independent_but_cannot_predate_publication(self):
        fund = support.TraversalTests().retained_fund()
        fund["selectedReport"]["strategyEvidenceRetrievedAt"] = "2026-07-19T12:00:00Z"
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "partial")
        fund["selectedReport"]["strategyEvidenceRetrievedAt"] = "2026-07-21T12:00:00Z"
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "complete")
        fund["selectedReport"]["strategyEvidenceRetrievedAt"] = "2026-12-01T12:00:00Z"
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "partial")

    def test_old_fund_observation_needs_its_own_later_strategy_observation(self):
        fund = support.TraversalTests().retained_fund()
        fund["retrievedAt"] = "2026-07-01T12:00:00Z"
        fund["selectedReport"]["publishedAt"] = "2026-07-01"
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "partial")
        fund["selectedReport"]["strategyEvidenceRetrievedAt"] = "2026-07-21T12:00:00Z"
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "complete")

    def test_legacy_strategy_complete_gets_only_its_own_new_budget(self):
        fields = {key: "complete" for key in traversal.QUALITY_FIELDS}
        old = {"fields": fields, "attempts": 3, "totalAttempts": 8, "managerAttempts": 2, "managerTotalAttempts": 4,
               "managerTableCoverageVersion": traversal.MANAGER_TABLE_COVERAGE_VERSION, "priceCoverageVersion": 2, "strategyAttempts": 3, "strategyTotalAttempts": 3}
        value = traversal.normalize_quality(old, "complete", traversal.utc_now(NOW))
        self.assertEqual(value["missingFields"], ["strategy"])
        self.assertEqual(value["fields"]["strategy"], "partial")
        self.assertEqual((value["attempts"], value["managerAttempts"], value["totalAttempts"]), (3, 2, 8))
        self.assertEqual((value["strategyAttempts"], value["strategyTotalAttempts"]), (0, 3))
        self.assertEqual(traversal.normalize_quality(value, "complete", traversal.utc_now(NOW)), value)

    def test_exhausted_reports_receive_three_bounded_strategy_retries_without_resetting_others(self):
        fields = {key: "complete" for key in traversal.QUALITY_FIELDS}
        progress = previous({"fields": fields, "attempts": 3, "totalAttempts": 3, "managerAttempts": 2, "managerTotalAttempts": 2})
        config = {**CONFIG, "collectorVersion": 4, "requireReportQuality": True}
        for index in range(3):
            stamp = f"2026-09-29T1{index + 1}:00:00Z"
            plan = traversal.plan_batches(catalog(), progress, config, shards=1, batch_size=3, now=stamp)
            batch = plan["batches"]["batch-0.json"]
            self.assertIn("000001", batch["traversal"]["strategyCodes"])
            status = traversal.build_status(batch, None, stamp)
            self.assertTrue(traversal.valid_status(status, batch, stamp))
            progress = traversal.merge_progress(catalog(), progress, config, [status], stamp)
            quality = progress["funds"][0]["quality"]
            self.assertEqual((quality["attempts"], quality["managerAttempts"], quality["strategyAttempts"]), (3, 2, index + 1))
            replay = traversal.merge_progress(catalog(), progress, config, [status], stamp)
            self.assertEqual(replay["funds"][0]["quality"], quality)
        self.assertEqual(quality["status"], "exhausted")
        self.assertNotIn("000001", traversal.quality_eligible({item["code"]: item for item in progress["funds"]}))

    def test_old_inflight_complete_flags_neither_prove_section_nor_consume_new_budget(self):
        fields = {key: "complete" for key in traversal.QUALITY_FIELDS}
        config = {**CONFIG, "collectorVersion": 4, "requireReportQuality": True}
        attempt = event(at="2026-09-29T11:00:00Z")
        attempt["funds"][0].update(qualityAttempt=True, reportQuality={"fields": fields})
        progress = traversal.merge_progress(catalog(), previous({"fields": fields, "attempts": 3}), config, [attempt], NOW)
        quality = progress["funds"][0]["quality"]
        self.assertEqual(quality["fields"]["strategy"], "partial")
        self.assertEqual(quality["strategyAttempts"], 0)
        self.assertEqual(quality["fields"]["managers"], "complete")
        self.assertEqual(quality["fields"]["prices"], "complete")

    def test_legacy_observation_cannot_remove_a_new_verified_strategy_proof(self):
        current = traversal.report_quality(support.TraversalTests().retained_fund(), PHASE, traversal.utc_now(NOW))
        old = {"fields": {key: "complete" for key in traversal.SIX_QUALITY_FIELDS}}
        merged = traversal.merge_quality_observation(current, old)
        self.assertEqual(merged["fields"]["strategy"], "complete")
        self.assertEqual(merged["strategySectionCoverageVersion"], traversal.STRATEGY_SECTION_COVERAGE_VERSION)


if __name__ == "__main__":
    unittest.main()
