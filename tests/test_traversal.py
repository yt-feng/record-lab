"""Small synthetic traversal fixtures; no network or real directory runs."""
import importlib.util
import json
from pathlib import Path
import tempfile
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

    def retained_fund(self):
        return {"code": "000001", "companyId": "co1", "status": "ok", "dataStatus": "fresh",
                "retrievedAt": "2026-09-29T10:00:00Z", "shareClassCodes": ["000001"],
                "selectedReport": {**evidence(), "fundCode": "000001", "navYuan": 1000,
                    "parseMethod": "pypdf", "sourceTextHash": "a" * 64,
                    "pageStats": {"pageCount": 10, "totalPages": 10, "processedPages": 10,
                                  "nonEmptyPages": 10, "charCount": 12000, "textStatus": "usable", "truncated": False},
                    "holdings": [{"stockCode": "600001", "market": "CN"}]},
                "previousReport": {"periodEnd": "2026-03-31", "publishedAt": "2026-04-20",
                                   "sourceUrl": "https://example.org/baseline.pdf", "holdings": [{"stockCode": "600001"}]},
                "narrative": {"text": "合成季度原文摘录", "sourceUrl": "https://example.org/report.pdf",
                              "periodStart": "2026-04-01", "periodEnd": "2026-06-30", "page": None},
                "rows": [{"stockCode": "600001", "market": "CN", "priceRange": {"status": "observed",
                    "priceBasis": "unadjusted", "sourceUrl": "https://example.org/prices", "currency": "CNY", "low": 1, "high": 2}}]}

    def test_quality_requires_full_text_quarter_strategy_nav_exact_baseline_and_prices(self):
        fund = self.retained_fund()
        fields = traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]
        self.assertTrue(all(value == "complete" for value in fields.values()))
        fund["selectedReport"]["pageStats"]["truncated"] = True
        fund["narrative"]["periodStart"] = "2026-01-01"
        fund["selectedReport"]["navYuan"] = None
        fund["previousReport"]["periodEnd"] = "2025-12-31"
        fund["rows"][0]["priceRange"]["priceBasis"] = "adjusted"
        fields = traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]
        self.assertEqual(fields, {"holdings": "complete", "pdf": "truncated", "strategy": "wrong-period", "nav": "missing",
                                  "baseline": "missing", "prices": "missing"})

    def test_legacy_pdf_stats_do_not_prove_complete_extraction(self):
        fund = self.retained_fund()
        del fund["selectedReport"]["pageStats"]["truncated"]
        del fund["selectedReport"]["pageStats"]["textStatus"]
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["pdf"], "partial")
        fund["selectedReport"]["sourceFundCode"] = "000099"
        self.assertIsNone(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW)))

    def test_truncated_or_excluded_holdings_never_count_as_full_research(self):
        for flag, expected in (("holdingsPossiblyTruncated", "truncated"), ("holdingsIncomplete", "partial")):
            fund = self.retained_fund()
            fund["selectedReport"][flag] = True
            quality = traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))
            self.assertEqual(quality["fields"]["holdings"], expected)
            state = traversal.normalize_quality(quality, "complete", traversal.utc_now(NOW))
            self.assertEqual(state["missingFields"], ["holdings"])
            self.assertEqual(state["status"], "pending")

    def test_retained_snapshot_hydrates_and_requeues_missing_report_fields(self):
        config = {**CONFIG, "collectorVersion": 2, "requireReportQuality": True}
        first = event()
        first["funds"][0]["priceCoverage"] = self.coverage(2)
        progress = traversal.merge_progress(catalog(), None, config, [first], now=NOW)
        complete_fund = self.retained_fund()
        complete_plan = traversal.plan_batches(catalog(), progress, config, shards=1, batch_size=3, now=NOW, retained=[complete_fund])
        self.assertEqual(complete_plan["meta"]["qualityCount"], 0)
        self.assertNotIn("000001", [item["code"] for item in complete_plan["batches"]["batch-0.json"]["funds"]])
        del complete_fund["narrative"]
        missing_plan = traversal.plan_batches(catalog(), progress, config, shards=1, batch_size=3, now=NOW, retained=[complete_fund])
        self.assertEqual(missing_plan["batches"]["batch-0.json"]["traversal"]["qualityCodes"], ["000001"])
        self.assertNotIn("000001", missing_plan["batches"]["batch-0.json"]["traversal"]["enrichmentCodes"])
        merged = traversal.merge_progress(catalog(), progress, config, [], now=NOW, retained=[complete_fund])
        self.assertEqual(merged["research"]["completeFunds"], 0)
        self.assertEqual(merged["funds"][0]["quality"]["missingFields"], ["strategy"])

    def test_quality_retries_are_idempotent_and_never_downgrade_holdings(self):
        config = {**CONFIG, "collectorVersion": 2, "requireReportQuality": True}
        progress = traversal.merge_progress(catalog(), None, config, [event()], now=NOW)
        for minute in (10, 20, 30):
            retry = event(status="failed", at=f"2026-09-29T11:{minute:02}:00Z")
            retry["funds"][0]["qualityAttempt"] = True
            progress = traversal.merge_progress(catalog(), progress, config, [retry], now=NOW)
            replay = traversal.merge_progress(catalog(), progress, config, [retry], now=NOW)
            self.assertEqual(progress["funds"][0]["quality"]["attempts"], replay["funds"][0]["quality"]["attempts"])
        self.assertEqual(progress["funds"][0]["status"], "complete")
        self.assertEqual(progress["funds"][0]["quality"]["status"], "exhausted")
        self.assertEqual(progress["research"]["exhaustedFunds"], 1)
        self.assertEqual(progress["research"]["completeFunds"], 0)
        self.assertNotIn("000001", traversal.quality_eligible({item["code"]: item for item in progress["funds"]}))

    def test_collector_version_resets_exhausted_attempts_exactly_once(self):
        prior = traversal.merge_progress(catalog(), None, CONFIG, [event(status="failed"),
            event(status="failed", at="2026-09-29T11:10:00Z")], now=NOW)
        self.assertEqual(prior["funds"][0]["attempts"], 2)
        config = {**CONFIG, "collectorVersion": 2, "requireReportQuality": True}
        resumed = traversal.merge_progress(catalog(), prior, config, [], now=NOW)
        self.assertEqual(resumed["collectorVersion"], 2)
        self.assertEqual(resumed["funds"][0]["attempts"], 0)
        next_event = event(status="failed", at="2026-09-29T11:20:00Z")
        failed = traversal.merge_progress(catalog(), resumed, config, [next_event], now=NOW)
        self.assertEqual(failed["funds"][0]["attempts"], 1)
        self.assertEqual(traversal.merge_progress(catalog(), failed, config, [], now=NOW)["funds"][0]["attempts"], 1)

    def test_unmapped_fund_can_complete_directly_but_not_infer_alias_ownership(self):
        raw = catalog(True)
        direct = event(code="000003", company=None)
        result = traversal.merge_progress(raw, None, CONFIG, [direct], now=NOW)
        self.assertEqual(result["funds"][2]["status"], "complete")
        self.assertIsNone(result["funds"][2]["companyCode"])
        receipt = {"schemaVersion": 1, "phase": PHASE, "generatedAt": NOW, "funds": [{
            "code": "000003", "representativeCode": "000003", "companyCode": "unknown", "status": "complete",
            "reportEvidence": evidence(), "priceCoverage": self.coverage(2), "shareClassCodes": ["000003"]}]}
        reconciled = traversal.reconcile_observations([direct], receipt, CONFIG, NOW)
        self.assertEqual(reconciled[0]["funds"][0]["status"], "complete")
        self.assertIsNone(reconciled[0]["funds"][0]["companyCode"])

    def test_no_equities_still_requires_pdf_strategy_and_nav(self):
        fund = self.retained_fund()
        fund.update({"status": "no-equities", "previousReport": None, "rows": []})
        fund["selectedReport"]["holdings"] = []
        quality = traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))
        self.assertEqual(quality["fields"]["baseline"], "not-applicable")
        self.assertEqual(quality["fields"]["prices"], "not-applicable")
        fund["narrative"] = None
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["strategy"], "missing")

    def test_manifest_loader_uses_only_referenced_quarter_files(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            traversal.atomic_json(root / "latest.json", {"schemaVersion": 1, "funds": [],
                "companyFiles": [{"path": "./data/companies/co1.json"}]})
            traversal.atomic_json(root / "companies/co1.json", {"schemaVersion": 1, "funds": [self.retained_fund()]})
            traversal.atomic_json(root / "companies/stale.json", {"schemaVersion": 1, "funds": [{"code": "000009"}]})
            self.assertEqual([fund["code"] for fund in traversal.load_retained_funds(root)], ["000001"])
            traversal.atomic_json(root / "latest.json", {"schemaVersion": 1, "companyFiles": [{"path": "../../private.json"}]})
            with self.assertRaises(traversal.TraversalError):
                list(traversal.load_retained_funds(root))

    def test_quality_flag_is_required_for_status_recovery(self):
        batch = {**CONFIG, "funds": [{"code": "000001", "companyCode": "co1"}], "traversal": {"qualityCodes": ["000001"]}}
        status = traversal.build_status(batch, None, NOW)
        self.assertTrue(status["funds"][0]["qualityAttempt"])
        self.assertTrue(traversal.valid_status(status, batch, NOW))
        status["funds"][0]["qualityAttempt"] = False
        self.assertFalse(traversal.valid_status(status, batch, NOW))

    def test_post_period_inception_is_a_sourced_exemption_not_holdings_success(self):
        config = {**CONFIG, "collectorVersion": 2, "requireReportQuality": True,
                  "funds": [{"code": "000003", "companyCode": "co2"}]}
        proof = {"reason": "ESTABLISHED_AFTER_PERIOD", "date": "2026-07-02", "fundCode": "000003",
                 "periodEnd": "2026-06-30", "sourceUrl": "https://fundf10.eastmoney.com/jbgk_000003.html"}
        snapshot = {"schemaVersion": 1, "sourceMode": "actions", "batchResults": [{"code": "000003", "status": "not-applicable"}],
                    "funds": [{"code": "000003", "status": "unavailable", "dataStatus": "fresh", "retrievedAt": "2026-09-29T10:00:00Z",
                               "selectedReport": None, "rows": [], "notApplicableEvidence": proof}]}
        status = traversal.build_status(config, snapshot, NOW)
        self.assertTrue(traversal.valid_status(status, config, NOW))
        self.assertEqual(status["funds"][0]["status"], "not-applicable")
        progress = traversal.merge_progress(catalog(), None, config, [status], NOW)
        self.assertEqual(progress["notApplicableFunds"], 1)
        self.assertEqual(progress["processedFunds"], 0)
        self.assertEqual(progress["pendingFunds"], 2)
        self.assertEqual(progress["research"]["applicableFunds"], 2)
        self.assertEqual(progress["research"]["incompleteFunds"], 2)
        self.assertEqual(progress["companies"][1]["notApplicable"], 1)
        replay = traversal.merge_progress(catalog(), progress, config, [], NOW)
        self.assertEqual(replay["funds"][2]["notApplicableEvidence"], proof)
        self.assertNotIn("000003", traversal.eligible({item["code"]: item for item in replay["funds"]}))
        receipt = {"schemaVersion": 1, "phase": PHASE, "generatedAt": NOW, "funds": [{
            "code": "000003", "representativeCode": "000003", "companyCode": "co2", "status": "not-applicable",
            "observedAt": "2026-09-29T10:00:00Z", "notApplicableEvidence": proof, "shareClassCodes": ["000003"]}]}
        reconciled = traversal.reconcile_observations([status], receipt, config, NOW)
        self.assertEqual(reconciled[0]["funds"][0]["status"], "not-applicable")
        bad = json.loads(json.dumps(snapshot))
        bad["funds"][0]["notApplicableEvidence"]["date"] = "2026-05-01"
        self.assertEqual(traversal.build_status(config, bad, NOW)["funds"][0]["status"], "failed")

    def test_within_quarter_inception_exempts_only_the_missing_baseline(self):
        fund = self.retained_fund()
        fund.update({"previousReport": None, "inceptionDate": "2026-05-02",
                     "inceptionSourceUrl": "https://fundf10.eastmoney.com/jbgk_000001.html"})
        quality = traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))
        self.assertEqual(quality["fields"]["baseline"], "not-applicable")
        normalized = traversal.normalize_quality(quality, "complete", traversal.utc_now(NOW))
        self.assertEqual(normalized["status"], "complete")
        self.assertEqual(normalized["baselineExemption"]["date"], "2026-05-02")
        fund["inceptionDate"] = "2026-03-02"
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["baseline"], "missing")
        forged = {"fields": {key: "not-applicable" for key in traversal.QUALITY_FIELDS}}
        self.assertEqual(traversal.normalize_quality(forged, "complete", traversal.utc_now(NOW))["missingFields"], list(traversal.QUALITY_FIELDS))

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

    def test_plan_round_robin_includes_unmapped_codes_and_is_bounded(self):
        plan = traversal.plan_batches(catalog(), None, CONFIG, shards=1, batch_size=3, now=NOW)
        self.assertEqual([x["code"] for x in plan["batches"]["batch-0.json"]["funds"]], ["000001", "000003", "000002"])
        self.assertEqual(plan["matrix"], [{"id": "0", "config": "batch-0.json"}])
        self.assertEqual(plan["batches"]["batch-0.json"]["maxWorkers"], 2)
        partial = traversal.plan_batches(catalog(True), None, CONFIG, shards=1, batch_size=3, now=NOW)
        self.assertEqual(partial["meta"]["selectedCount"], 3)
        self.assertIn("000003", [item["code"] for item in partial["batches"]["batch-0.json"]["funds"]])
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
        remaining = traversal.merge_progress(raw, stopped, CONFIG, [], now=NOW)
        self.assertTrue(remaining["continueRun"])
        self.assertEqual(remaining["pendingFunds"], 1)
        # Directory exhaustion cannot prevent direct collection by fund code.
        stopped["funds"][2].update({"status": "failed", "attempts": 2,
                                   "lastAttemptAt": "2026-09-29T11:00:00Z", "attempted": True})
        terminal = traversal.merge_progress(raw, stopped, CONFIG, [], now=NOW)
        self.assertFalse(terminal["continueRun"])
        self.assertEqual(terminal["pendingFunds"], 0)
        self.assertEqual(terminal["funds"][2]["status"], "failed")
        self.assertEqual(terminal["completionStatus"], "incomplete-exhausted")

    def test_future_wrong_phase_and_wrong_company_events_are_ignored(self):
        future = event(at="2026-10-01T00:00:00Z")
        wrong_company = event(company="co2")
        wrong_phase = {**event(), "phase": {"targetPeriodEnd": "2026-03-31", "baselinePeriodEnd": "2025-12-31"}}
        result = traversal.merge_progress(catalog(), None, CONFIG, [future, wrong_company, wrong_phase], now=NOW)
        self.assertEqual(result["attemptedFunds"], 0)
        self.assertEqual(result["processedFunds"], 0)

    def retry_fixture(self, error="NO_VALID_REPORTS", unmapped=False):
        config = {"targetPeriodEnd": "2026-09-30", "baselinePeriodEnd": "2026-06-30",
                  "collectorVersion": 2, "requireReportQuality": True,
                  "retryPolicy": {"dailyUntil": "2026-11-30", "intervalDays": 7, "maxScheduledCycles": 4}}
        owner = None if unmapped else "co1"
        directory = {"schemaVersion": 1, "retrievedAt": "2026-10-26T06:00:00Z",
                     "companies": [{"code": "co1", "name": "合成", "fundCount": 0 if unmapped else 1, "directoryStatus": "complete"}],
                     "funds": [{"code": "000001", "name": "合成", "companyCode": owner}]}
        previous = {"schemaVersion": 1, "phase": traversal.phase_for(config), "collectorVersion": 2,
                    "directoryPasses": 1, "lastDirectoryObservedAt": directory["retrievedAt"],
                    "funds": [{"code": "000001", "companyCode": owner, "status": "failed", "attempts": 2,
                               "lastAttemptAt": "2026-10-26T06:00:00Z", "errorCode": error, "attempted": True}]}
        return directory, previous, config

    def test_missing_report_rechecks_next_day_without_empty_chain_or_lost_totals(self):
        directory, previous, config = self.retry_fixture()
        today = traversal.merge_progress(directory, previous, config, now="2026-10-26T07:00:00Z")
        self.assertFalse(today["continueRun"])
        self.assertEqual(today["nextRetryAt"], "2026-10-27T00:00:00+00:00")
        self.assertEqual(today["funds"][0]["totalAttempts"], 2)
        tomorrow = traversal.merge_progress(directory, today, config, now="2026-10-27T07:00:00Z")
        self.assertTrue(tomorrow["continueRun"])
        self.assertEqual(tomorrow["funds"][0]["attempts"], 0)
        self.assertEqual(tomorrow["funds"][0]["totalAttempts"], 2)
        self.assertEqual(tomorrow["funds"][0]["retry"]["cycleDate"], "2026-10-27")
        failed = {"schemaVersion": 1, "phase": traversal.phase_for(config), "attemptedAt": "2026-10-27T08:00:00Z",
                  "funds": [{"code": "000001", "companyCode": "co1", "status": "failed", "errorCode": "NO_VALID_REPORTS"}]}
        result = traversal.merge_progress(directory, tomorrow, config, [failed], now="2026-10-27T09:00:00Z")
        replay = traversal.merge_progress(directory, result, config, [failed], now="2026-10-27T09:00:00Z")
        self.assertEqual((replay["funds"][0]["attempts"], replay["funds"][0]["totalAttempts"]), (1, 3))
        failed["attemptedAt"] = "2026-10-27T10:00:00Z"
        exhausted = traversal.merge_progress(directory, replay, config, [failed], now="2026-10-27T11:00:00Z")
        self.assertFalse(exhausted["continueRun"])
        self.assertEqual((exhausted["funds"][0]["attempts"], exhausted["funds"][0]["totalAttempts"]), (2, 4))
        self.assertEqual(exhausted["nextRetryAt"], "2026-10-28T00:00:00+00:00")

    def test_historical_quarter_uses_weekly_instead_of_daily_recheck(self):
        directory, previous, config = self.retry_fixture(error="NETWORK_ERROR")
        config.update({"targetPeriodEnd": "2026-06-30", "baselinePeriodEnd": "2026-03-31"})
        config["retryPolicy"]["dailyUntil"] = None
        previous["phase"] = traversal.phase_for(config)
        waiting = traversal.merge_progress(directory, previous, config, now="2026-10-27T07:00:00Z")
        self.assertFalse(waiting["continueRun"])
        self.assertEqual(waiting["nextRetryAt"], "2026-11-02T00:00:00+00:00")

    def test_weekly_recheck_has_four_cycles_and_daily_window_does_not_extend_forever(self):
        directory, previous, config = self.retry_fixture()
        previous["funds"][0]["lastAttemptAt"] = "2026-11-30T06:00:00Z"
        after_window = traversal.merge_progress(directory, previous, config, now="2026-12-01T07:00:00Z")
        self.assertFalse(after_window["continueRun"])
        self.assertEqual(after_window["nextRetryAt"], "2026-12-07T00:00:00+00:00")
        for day in (7, 14, 21, 28):
            stamp = f"2026-12-{day:02d}T07:00:00Z"
            opened = traversal.merge_progress(directory, after_window, config, now=stamp)
            self.assertEqual(opened["funds"][0]["attempts"], 0)
            self.assertTrue(opened["continueRun"])
            opened["funds"][0].update({"attempts": 2, "lastAttemptAt": stamp})
            after_window = traversal.merge_progress(directory, opened, config, now=stamp)
        self.assertEqual(after_window["funds"][0]["retry"]["scheduledCycles"], 4)
        self.assertIsNone(after_window["nextRetryAt"])
        self.assertFalse(traversal.merge_progress(directory, after_window, config, now="2027-01-05T07:00:00Z")["continueRun"])

    def test_unsupported_parser_does_not_reopen_on_schedule(self):
        directory, previous, config = self.retry_fixture(error="PDF_PARSE_FAILED")
        result = traversal.merge_progress(directory, previous, config, now="2026-10-27T07:00:00Z")
        self.assertFalse(result["continueRun"])
        self.assertIsNone(result["nextRetryAt"])
        self.assertEqual(result["funds"][0]["attempts"], 2)

    def test_unmapped_exhausted_directory_does_not_create_empty_chain(self):
        directory, previous, config = self.retry_fixture(error="PDF_PARSE_FAILED", unmapped=True)
        result = traversal.merge_progress(directory, previous, config, now="2026-10-26T07:00:00Z")
        plan = traversal.plan_batches(directory, result, config, now="2026-10-26T07:00:00Z")
        self.assertEqual(plan["matrix"], [])
        self.assertFalse(plan["meta"]["continueRun"])
        self.assertFalse(result["continueRun"])
        self.assertEqual(result["completionStatus"], "incomplete-exhausted")
        self.assertEqual(result["directoryNextRetryAt"], "2026-10-27T00:00:00+00:00")
        self.assertEqual(result["directoryPasses"], 1)

    def test_missing_pdf_quality_reopens_but_truncated_parser_does_not(self):
        directory, previous, config = self.retry_fixture()
        fund = previous["funds"][0]
        fund.update({"status": "complete", "quality": {"attempts": 3, "lastAttemptAt": fund["lastAttemptAt"],
                     "fields": {key: "missing" if key == "pdf" else "complete" for key in traversal.QUALITY_FIELDS}},
                     "enrichment": self.coverage(2)})
        opened = traversal.merge_progress(directory, previous, config, now="2026-10-27T07:00:00Z")
        self.assertEqual(opened["funds"][0]["status"], "complete")
        self.assertEqual(opened["funds"][0]["quality"]["attempts"], 0)
        self.assertEqual(opened["funds"][0]["quality"]["totalAttempts"], 3)
        fund["quality"]["fields"]["pdf"] = "truncated"
        closed = traversal.merge_progress(directory, previous, config, now="2026-10-27T07:00:00Z")
        self.assertEqual(closed["funds"][0]["quality"]["attempts"], 3)
        self.assertFalse(closed["continueRun"])
        self.assertIsNone(closed["nextRetryAt"])

    def test_deferred_budget_receipt_preserves_unattempted_state_and_rechecks_tomorrow(self):
        directory, _, config = self.retry_fixture()
        batch = {**config, "funds": [{"code": "000001", "companyCode": "co1"}]}
        raw = {"schemaVersion": 1, "sourceMode": "actions", "targetPeriodEnd": "2026-09-30", "funds": [],
               "batchResults": [{"code": "000001", "status": "deferred-budget", "attempted": False, "errorCode": "BATCH_DEADLINE_REACHED"}]}
        status = traversal.build_status(batch, raw, "2026-10-26T07:00:00Z")
        self.assertTrue(traversal.valid_status(status, batch, "2026-10-26T07:00:00Z"))
        receipt = {"schemaVersion": 1, "phase": traversal.phase_for(config), "generatedAt": "2026-10-26T07:00:00Z",
                   "funds": [{**raw["batchResults"][0], "companyCode": "co1"}]}
        reconciled = traversal.reconcile_observations([status], receipt, config, "2026-10-26T07:00:00Z")
        today = traversal.merge_progress(directory, None, config, reconciled, now="2026-10-26T07:00:00Z")
        self.assertEqual(today["funds"][0]["status"], "pending")
        self.assertEqual(today["funds"][0]["attempts"], 0)
        self.assertEqual(today["funds"][0]["totalAttempts"], 0)
        self.assertEqual(today["attemptedFunds"], 0)
        self.assertEqual(today["deferredFunds"], 1)
        self.assertFalse(today["continueRun"])
        tomorrow = traversal.merge_progress(directory, today, config, now="2026-10-27T07:00:00Z")
        self.assertTrue(tomorrow["continueRun"])
        self.assertEqual(tomorrow["deferredFunds"], 0)
        bad = json.loads(json.dumps(status)); bad["funds"][0]["attempted"] = True
        self.assertFalse(traversal.valid_status(bad, batch, "2026-10-26T07:00:00Z"))

    def test_deferred_quality_keeps_available_holdings_and_both_attempt_counts(self):
        directory, previous, config = self.retry_fixture()
        prior = previous["funds"][0]
        prior.update({"status": "complete", "totalAttempts": 6, "quality": {"attempts": 2, "totalAttempts": 5,
                      "fields": {key: "missing" for key in traversal.QUALITY_FIELDS}, "lastAttemptAt": prior["lastAttemptAt"]}})
        event = {"schemaVersion": 1, "phase": traversal.phase_for(config), "attemptedAt": "2026-10-26T07:00:00Z",
                 "funds": [{"code": "000001", "companyCode": "co1", "status": "deferred-budget", "attempted": False,
                            "errorCode": "BATCH_DEADLINE_REACHED", "qualityAttempt": True}]}
        result = traversal.merge_progress(directory, previous, config, [event], now="2026-10-26T07:00:00Z")
        fund = result["funds"][0]
        self.assertEqual((fund["status"], fund["totalAttempts"]), ("complete", 6))
        self.assertEqual((fund["quality"]["attempts"], fund["quality"]["totalAttempts"]), (2, 5))
        self.assertEqual(result["deferredFunds"], 1)
        self.assertFalse(result["continueRun"])

    def test_status_preserves_definite_parser_failure_over_missing_report_wrapper(self):
        _, _, config = self.retry_fixture()
        batch = {**config, "funds": [{"code": "000001", "companyCode": "co1"}]}
        snapshot = {"schemaVersion": 1, "sourceMode": "actions", "funds": [],
                    "batchResults": [{"code": "000001", "status": "failed", "errorCode": "NO_VALID_REPORTS"}],
                    "errors": [{"code": "000001", "stage": "report", "errorCode": "PDF_PARSE_FAILED"}]}
        status = traversal.build_status(batch, snapshot, "2026-10-26T07:00:00Z")
        self.assertEqual(status["funds"][0]["errorCode"], "PDF_PARSE_FAILED")

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

    def empty_stock_evidence(self):
        return {"confirmed": True, "kind": "semiannual", "evidenceType": "empty-stock-detail-section",
                "sourceUrl": "https://example.org/report.pdf", "periodEnd": "2026-06-30", "publishedAt": "2026-07-20",
                "page": 38, "text": "7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n无。",
                "assetAllocationText": "1 权益投资 - -\n其中：股票 - -"}

    def test_empty_stock_section_requires_both_original_zero_asset_rows(self):
        proof = self.empty_stock_evidence()
        self.assertTrue(traversal.no_equities_content(proof))
        for rows in ("1 权益投资 0 0.00\n其中：股票 0.00 0", "权益投资 - 0\n其中 股票 0.00 -"):
            self.assertTrue(traversal.no_equities_content({**proof, "assetAllocationText": rows}))
        for rows in ("1 权益投资 100 0.01\n其中：股票 - -", "1 权益投资 - -\n其中：股票 1 0",
                     "1 权益投资 - -", "其中：股票 - -", "1 权益投资 - -\n其中：债券 - -",
                     "1 权益投资 - -\n其中：股票 - -\n2 债券 100 100", "1 权益投资 - - 1\n其中：股票 - -"):
            with self.subTest(rows=rows):
                self.assertFalse(traversal.no_equities_content({**proof, "assetAllocationText": rows}))
        for text in ("无。", "7.4 期末基金投资组合说明\n无。", "7.3 股票投资明细\n无。", proof["text"] + "另有股票1股"):
            self.assertFalse(traversal.no_equities_content({**proof, "text": text}))
        self.assertFalse(traversal.no_equities_content({**proof, "evidenceType": "invented-empty"}))

    def test_empty_stock_section_preserves_evidence_and_checks_source_dates_and_page(self):
        fund = self.retained_fund()
        proof = self.empty_stock_evidence()
        fund.update({"status": "no-equities", "rows": [], "noEquitiesEvidence": proof})
        fund["selectedReport"]["holdings"] = []
        result = traversal.report_evidence(fund, PHASE, traversal.utc_now(NOW))
        self.assertEqual(result[0], "no-equities")
        saved = result[1]["noEquities"]
        for key in ("evidenceType", "assetAllocationText", "periodEnd", "publishedAt"):
            self.assertEqual(saved[key], proof[key])
        self.assertTrue(traversal.valid_event_evidence(result[1], "no-equities", PHASE, traversal.utc_now(NOW)))
        for key, bad in (("sourceUrl", "https://example.org/other.pdf"), ("periodEnd", "2026-03-31"),
                         ("publishedAt", "2026-07-21"), ("page", 0), ("page", True), ("page", None)):
            with self.subTest(key=key, bad=bad):
                fund["noEquitiesEvidence"] = {**proof, key: bad}
                self.assertIsNone(traversal.report_evidence(fund, PHASE, traversal.utc_now(NOW)))
                broken = {**result[1], "noEquities": {**saved, key: bad}}
                self.assertFalse(traversal.valid_event_evidence(broken, "no-equities", PHASE, traversal.utc_now(NOW)))
        for key in ("assetAllocationText", "periodEnd", "publishedAt"):
            stripped = {key2: value for key2, value in saved.items() if key2 != key}
            self.assertFalse(traversal.valid_event_evidence({**result[1], "noEquities": stripped}, "no-equities", PHASE, traversal.utc_now(NOW)))

    def test_empty_stock_section_status_survives_reconciliation_without_invented_statement(self):
        config = {**CONFIG, "funds": [{"code": "000001", "companyCode": "co1"}]}
        fund = self.retained_fund()
        fund.update({"status": "no-equities", "rows": [], "noEquitiesEvidence": self.empty_stock_evidence()})
        fund["selectedReport"]["holdings"] = []
        snapshot = {"schemaVersion": 1, "sourceMode": "actions", "funds": [fund],
                    "batchResults": [{"code": "000001", "status": "no-equities"}]}
        status = traversal.build_status(config, snapshot, NOW)
        self.assertTrue(traversal.valid_status(status, config, NOW))
        proof = status["funds"][0]["reportEvidence"]
        receipt = {"schemaVersion": 1, "phase": PHASE, "generatedAt": NOW, "funds": [{"code": "000001",
                   "representativeCode": "000001", "companyCode": "co1", "status": "no-equities",
                   "reportEvidence": proof, "priceCoverage": traversal.price_coverage(fund), "shareClassCodes": ["000001"]}]}
        events = traversal.reconcile_observations([status], receipt, config, NOW)
        progress = traversal.merge_progress(catalog(), None, config, events, NOW)
        self.assertEqual(progress["funds"][0]["status"], "no-equities")
        self.assertEqual(events[0]["funds"][0]["reportEvidence"]["noEquities"]["text"], self.empty_stock_evidence()["text"])


if __name__ == "__main__":
    unittest.main()
