"""Small synthetic traversal fixtures; no network or real directory runs."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("traversal", Path(__file__).parents[1] / "scripts/traversal.py")
traversal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(traversal)

NOW = "2026-09-29T12:00:00Z"
CONFIG = {"targetPeriodEnd": "2026-06-30", "baselinePeriodEnd": "2026-03-31", "maxWorkers": 2}
PHASE = traversal.phase_for(CONFIG)


def catalog(incomplete=False):
    return {"schemaVersion": 1, "retrievedAt": "2026-09-29T09:00:00Z", "companies": [
        {"code": "co1", "name": "测试甲", "fundCount": 2, "directoryStatus": "complete"},
        {"code": "co2", "name": "测试乙", "fundCount": None if incomplete else 1,
         "directoryStatus": "pending" if incomplete else "complete"}],
        "funds": [{"code": "000001", "name": "合成甲A", "companyCode": "co1", "type": "股票型"},
                  {"code": "000002", "name": "合成甲C", "companyCode": "co1", "type": "股票型"},
                  {"code": "000003", "name": "合成乙", "companyCode": None if incomplete else "co2", "type": "债券型"}]}


def evidence():
    return {"sourceUrl": "https://example.org/report.pdf", "periodEnd": "2026-06-30",
            "publishedAt": "2026-07-20", "observedAt": "2026-09-29T10:00:00Z"}


def event(code="000001", status="complete", at="2026-09-29T11:00:00Z", company="co1"):
    row = {"code": code, "companyCode": company, "status": status, "errorCode": "FUND_FAILED" if status == "failed" else None}
    if status == "complete":
        row["reportEvidence"] = evidence()
    return {"schemaVersion": 1, "phase": PHASE, "attemptedAt": at, "funds": [row], "portfolioAliases": []}


class TraversalTests(unittest.TestCase):
    def coverage(self, available=0):
        stocks = [{"stockCode": "600001", "market": "CN"}, {"stockCode": "600002", "market": "CN"}]
        return {"holdingCount": 2, "priceAvailable": available, "missingStocks": stocks[available:],
                "holdingsIdentity": "a" * 64}

    def test_enrichment_prioritizes_missing_prices_without_replacing_report_state(self):
        first = event()
        first["funds"][0]["priceCoverage"] = self.coverage(1)
        progress = traversal.merge_progress(catalog(), None, CONFIG, [first], now=NOW)
        plan = traversal.plan_batches(catalog(), progress, CONFIG, shards=1, batch_size=1, now=NOW)
        batch = plan["batches"]["batch-0.json"]
        self.assertEqual(batch["traversal"]["enrichmentCodes"], ["000001"])
        self.assertEqual(batch["funds"][0]["priceTargets"], [{"stockCode": "600002", "market": "CN"}])
        self.assertEqual(progress["funds"][0]["status"], "complete")
        self.assertEqual(progress["enrichment"]["pendingFunds"], 1)

    def test_compact_progress_restores_price_targets_and_accepts_legacy_objects(self):
        first = event()
        first["funds"][0]["priceCoverage"] = self.coverage(1)
        progress = traversal.merge_progress(catalog(), None, CONFIG, [first], now=NOW)
        stored = progress["funds"][0]["enrichment"]
        self.assertEqual(stored["missingStockKeys"], ["CN:600002"])
        self.assertNotIn("missingStocks", stored)
        plan = traversal.plan_batches(catalog(), progress, CONFIG, shards=1, batch_size=1, now=NOW)
        self.assertEqual(plan["batches"]["batch-0.json"]["funds"][0]["priceTargets"], [{"stockCode": "600002", "market": "CN"}])
        legacy = json.loads(json.dumps(progress))
        legacy["funds"][0]["enrichment"].pop("missingStockKeys")
        legacy["funds"][0]["enrichment"]["missingStocks"] = [{"stockCode": "600002", "market": "CN"}]
        self.assertEqual(traversal.plan_batches(catalog(), legacy, CONFIG, shards=1, batch_size=1, now=NOW), plan)
        restored = traversal.merge_progress(catalog(), legacy, CONFIG, [], now=NOW)
        self.assertEqual(restored["funds"][0]["enrichment"], stored)

    def test_compact_coverage_validates_keys_and_substantially_reduces_checkpoint_size(self):
        stocks = [{"market": "CN", "stockCode": str(600000 + index)} for index in range(300)]
        full = {"holdingCount": 300, "priceAvailable": 0, "missingStocks": stocks, "holdingsIdentity": "a" * 64}
        compact = traversal.compact_enrichment(full)
        self.assertTrue(traversal.valid_price_coverage(compact))
        self.assertEqual(traversal.missing_stock_objects(compact), stocks)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)) * 0.4)
        invalid = {**compact, "missingStockKeys": ["CN:600001"] * 300}
        self.assertFalse(traversal.valid_price_coverage(invalid))
        invalid["missingStockKeys"][0] = "US:600001"
        self.assertFalse(traversal.valid_price_coverage(invalid))
        conflict = {**compact, "missingStocks": []}
        self.assertFalse(traversal.valid_price_coverage(conflict))

    def test_final_receipt_overrides_rejected_shrinking_coverage(self):
        first = event()
        first["funds"][0]["priceCoverage"] = self.coverage()
        progress = traversal.merge_progress(catalog(), None, CONFIG, [first], now=NOW)
        retry = event(at="2026-09-29T11:30:00Z")
        retry["funds"][0].update({"enrichmentAttempt": True, "priceCoverage": {
            "holdingCount": 1, "priceAvailable": 1, "missingStocks": [], "holdingsIdentity": "b" * 64}})
        receipt = {"schemaVersion": 1, "phase": PHASE, "generatedAt": NOW, "funds": [{
            "code": "000001", "companyCode": "co1", "representativeCode": "000001", "status": "complete",
            "reportEvidence": evidence(), "priceCoverage": self.coverage(), "shareClassCodes": ["000001"]}]}
        reconciled = traversal.reconcile_observations([retry], receipt, CONFIG, NOW)
        merged = traversal.merge_progress(catalog(), progress, CONFIG, reconciled, NOW)
        self.assertEqual(merged["funds"][0]["enrichment"]["holdingCount"], 2)
        self.assertEqual(merged["funds"][0]["enrichment"]["status"], "pending")
        self.assertEqual(merged["funds"][0]["enrichment"]["attempts"], 1)
        receipt["funds"] = []
        rejected = traversal.reconcile_observations([retry], receipt, CONFIG, NOW)
        self.assertEqual(rejected[0]["funds"][0]["status"], "failed")
        self.assertNotIn("priceCoverage", rejected[0]["funds"][0])
        with self.assertRaises(traversal.TraversalError):
            traversal.reconcile_observations([retry], None, CONFIG, NOW)

    def test_two_direct_share_classes_schedule_one_proven_portfolio(self):
        record = event()
        record["funds"][0]["priceCoverage"] = self.coverage()
        record["funds"].append({**record["funds"][0], "code": "000002"})
        record["portfolioAliases"] = [{"sourcePortfolioCode": "000001", "shareClassCodes": ["000001", "000002"],
                                      "sourceUrl": "https://example.org/classes.pdf", "status": "complete",
                                      "reportEvidence": evidence(), "priceCoverage": self.coverage()}]
        progress = traversal.merge_progress(catalog(), None, CONFIG, [record], now=NOW)
        self.assertEqual(progress["attemptedFunds"], 2)
        self.assertEqual(progress["enrichment"]["pendingFunds"], 1)
        self.assertEqual(traversal.enrichment_eligible({item["code"]: item for item in progress["funds"]}), ["000001"])
        replay = traversal.merge_progress(catalog(), progress, CONFIG, [], now=NOW)
        self.assertEqual(replay["funds"][1]["representativeCode"], "000001")

    def test_enrichment_failure_is_bounded_at_three_and_report_success_remains(self):
        first = event()
        first["funds"][0]["priceCoverage"] = self.coverage()
        progress = traversal.merge_progress(catalog(), None, CONFIG, [first], now=NOW)
        for minute in (10, 20, 30):
            retry = event(status="failed", at=f"2026-09-29T11:{minute:02}:00Z")
            retry["funds"][0]["enrichmentAttempt"] = True
            progress = traversal.merge_progress(catalog(), progress, CONFIG, [retry], now=NOW)
            replay = traversal.merge_progress(catalog(), progress, CONFIG, [retry], now=NOW)
            self.assertEqual(replay["funds"][0]["enrichment"]["attempts"], progress["funds"][0]["enrichment"]["attempts"])
        state = progress["funds"][0]
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["attempts"], 0)
        self.assertEqual(state["enrichment"]["attempts"], 3)
        self.assertEqual(state["enrichment"]["status"], "exhausted")
        self.assertEqual(progress["enrichment"]["exhaustedFunds"], 1)
        self.assertNotIn("000001", traversal.enrichment_eligible({item["code"]: item for item in progress["funds"]}))

    def test_partial_price_observations_accumulate_only_for_the_same_holdings_identity(self):
        first = event()
        first["funds"][0]["priceCoverage"] = self.coverage(1)
        progress = traversal.merge_progress(catalog(), None, CONFIG, [first], now=NOW)
        retry = event(at="2026-09-29T11:30:00Z")
        retry["funds"][0]["enrichmentAttempt"] = True
        retry["funds"][0]["priceCoverage"] = {**self.coverage(1), "missingStocks": [{"stockCode": "600001", "market": "CN"}]}
        merged = traversal.merge_progress(catalog(), progress, CONFIG, [retry], now=NOW)
        self.assertEqual(merged["funds"][0]["enrichment"]["priceAvailable"], 2)
        self.assertEqual(merged["funds"][0]["enrichment"]["status"], "complete")
        self.assertEqual(merged["enrichment"]["completeFunds"], 1)
        retry["funds"][0]["priceCoverage"]["holdingsIdentity"] = "b" * 64
        changed = traversal.merge_progress(catalog(), progress, CONFIG, [retry], now=NOW)
        self.assertEqual(changed["funds"][0]["enrichment"]["priceAvailable"], 1)

    def test_status_recovery_requires_the_matching_enrichment_flag(self):
        batch = {**CONFIG, "funds": [{"code": "000001", "companyCode": "co1"}],
                 "traversal": {"enrichmentCodes": ["000001"]}}
        status = traversal.build_status(batch, None, NOW)
        self.assertTrue(status["funds"][0]["enrichmentAttempt"])
        self.assertTrue(traversal.valid_status(status, batch, NOW))
        status["funds"][0]["enrichmentAttempt"] = False
        self.assertFalse(traversal.valid_status(status, batch, NOW))

    def test_plan_round_robin_mapped_only_and_bounded(self):
        plan = traversal.plan_batches(catalog(), None, CONFIG, shards=1, batch_size=3, now=NOW)
        self.assertEqual([x["code"] for x in plan["batches"]["batch-0.json"]["funds"]], ["000001", "000003", "000002"])
        self.assertEqual(plan["matrix"], [{"id": "0", "config": "batch-0.json"}])
        self.assertEqual(plan["batches"]["batch-0.json"]["maxWorkers"], 2)
        partial = traversal.plan_batches(catalog(True), None, CONFIG, shards=1, batch_size=3, now=NOW)
        self.assertEqual(partial["meta"]["selectedCount"], 2)
        self.assertTrue(partial["meta"]["continueRun"])

    def test_missing_snapshot_marks_every_actual_batch_item_failed(self):
        batch = {**CONFIG, "funds": [{"code": "000001", "companyCode": "co1"}]}
        result = traversal.build_status(batch, None, now=NOW)
        self.assertEqual(result["funds"][0]["status"], "failed")
        self.assertEqual(result["funds"][0]["errorCode"], "BATCH_FAILED")

    def test_status_requires_fresh_source_proof_not_just_claimed_success(self):
        batch = {**CONFIG, "funds": [{"code": "000001", "companyCode": "co1"}]}
        snapshot = {"schemaVersion": 1, "sourceMode": "actions", "targetPeriodEnd": "2026-06-30",
                    "batchResults": [{"code": "000001", "status": "complete", "errorCode": None}],
                    "funds": [{"code": "000001", "status": "ok", "dataStatus": "fresh", "retrievedAt": "2026-09-29T10:00:00Z",
                               "selectedReport": {**evidence(), "holdings": [{"stockCode": "600001"}]}, "rows": [{"stockCode": "600001"}]}]}
        self.assertEqual(traversal.build_status(batch, snapshot, now=NOW)["funds"][0]["status"], "complete")
        snapshot["funds"][0]["dataStatus"] = "cached"
        self.assertEqual(traversal.build_status(batch, snapshot, now=NOW)["funds"][0]["status"], "failed")
        snapshot["funds"][0]["dataStatus"] = "fresh"
        batch["traversal"] = {"plannedAt": "2026-09-29T11:00:00Z"}
        self.assertEqual(traversal.build_status(batch, snapshot, now=NOW)["funds"][0]["status"], "failed")

    def test_failure_replay_is_idempotent_and_second_failure_exhausts_retry(self):
        first = event(status="failed")
        one = traversal.merge_progress(catalog(), None, CONFIG, [first], now=NOW)
        replay = traversal.merge_progress(catalog(), one, CONFIG, [first], now=NOW)
        self.assertEqual(replay["funds"][0]["attempts"], 1)
        second = event(status="failed", at="2026-09-29T11:30:00Z")
        two = traversal.merge_progress(catalog(), replay, CONFIG, [second], now=NOW)
        self.assertEqual(two["funds"][0]["attempts"], 2)
        plan = traversal.plan_batches(catalog(), two, CONFIG, shards=1, batch_size=3, now=NOW)
        self.assertNotIn("000001", [x["code"] for x in plan["batches"]["batch-0.json"]["funds"]])

    def test_success_survives_later_failure_and_phase_change_resets(self):
        prior = traversal.merge_progress(catalog(), None, CONFIG, [event()], now=NOW)
        result = traversal.merge_progress(catalog(), prior, CONFIG, [event(status="failed", at="2026-09-29T11:30:00Z")], now=NOW)
        self.assertEqual(result["funds"][0]["status"], "complete")
        other = {"targetPeriodEnd": "2026-03-31", "baselinePeriodEnd": "2025-12-31"}
        reset = traversal.merge_progress(catalog(), result, other, [], now=NOW)
        self.assertEqual(reset["processedFunds"], 0)
        self.assertEqual(reset["funds"][0]["attempts"], 0)

    def test_source_verified_aliases_complete_same_company_without_inventing_attempts(self):
        record = event()
        record["portfolioAliases"] = [{"sourcePortfolioCode": "000001", "shareClassCodes": ["000001", "000002"],
                                        "sourceUrl": "https://example.org/classes.pdf", "status": "complete", "reportEvidence": evidence()}]
        result = traversal.merge_progress(catalog(), None, CONFIG, [record], now=NOW)
        self.assertEqual(result["processedFunds"], 2)
        self.assertEqual(result["attemptedFunds"], 1)
        self.assertEqual(result["completedCompanies"], 1)
        self.assertEqual(result["funds"][1]["attribution"], "portfolio-evidence")
        record["portfolioAliases"][0]["shareClassCodes"] = ["000001", "000003"]
        rejected = traversal.merge_progress(catalog(), None, CONFIG, [record], now=NOW)
        self.assertEqual(rejected["processedFunds"], 1)

    def test_directory_resume_counts_new_catalog_passes_and_stops_at_twelve(self):
        raw = catalog(True)
        result = traversal.merge_progress(raw, None, CONFIG, [], now=NOW)
        replay = traversal.merge_progress(raw, result, CONFIG, [], now=NOW)
        self.assertEqual(replay["directoryPasses"], 1)
        self.assertEqual(replay["companies"][1]["totalFunds"], None)
        self.assertEqual(replay["pendingFunds"], 3)
        # Exhaust mapped funds to isolate the bounded directory continuation.
        stopped = {**replay, "directoryPasses": 12}
        for item in stopped["funds"]:
            if item["companyCode"]:
                item.update({"status": "failed", "attempts": 2, "lastAttemptAt": "2026-09-29T11:00:00Z", "attempted": True})
        terminal = traversal.merge_progress(raw, stopped, CONFIG, [], now=NOW)
        self.assertFalse(terminal["continueRun"])
        self.assertEqual(terminal["pendingFunds"], 1)
        self.assertEqual(terminal["funds"][2]["status"], "pending")

    def test_future_wrong_phase_and_wrong_company_events_are_ignored(self):
        future = event(at="2026-10-01T00:00:00Z")
        wrong_company = event(company="co2")
        wrong_phase = {**event(), "phase": {"targetPeriodEnd": "2026-03-31", "baselinePeriodEnd": "2025-12-31"}}
        result = traversal.merge_progress(catalog(), None, CONFIG, [future, wrong_company, wrong_phase], now=NOW)
        self.assertEqual(result["attemptedFunds"], 0)
        self.assertEqual(result["processedFunds"], 0)

    def test_bond_type_does_not_establish_no_equities(self):
        record = event(code="000003", status="no-equities", company="co2")
        record["funds"][0]["reportEvidence"] = evidence()
        result = traversal.merge_progress(catalog(), None, CONFIG, [record], now=NOW)
        self.assertEqual(result["processedFunds"], 0)
        record["funds"][0]["reportEvidence"]["noEquities"] = {"confirmed": True, "sourceUrl": "https://example.org/report.pdf",
                                                               "page": 7, "text": "本基金本报告期末未持有股票"}
        accepted = traversal.merge_progress(catalog(), None, CONFIG, [record], now=NOW)
        self.assertEqual(accepted["processedFunds"], 1)
        self.assertEqual(accepted["companies"][1]["withoutEquities"], 1)


if __name__ == "__main__":
    unittest.main()
