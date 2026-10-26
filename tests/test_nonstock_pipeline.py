"""Source-bounded table proof across extraction, collection and status replay."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from collect import collect_fund
from providers import PublicProvider, parse_no_equities, parse_pdf_pages
from report_extract import _details_for_pages
import traversal

FIXTURE = json.loads((ROOT / "tests/fixtures/nonstock-assets.json").read_text())
REPORT = {**FIXTURE["report"], "title": "合成基金2026年第2季度报告"}
NOW = "2026-10-26T12:00:00Z"
PHASE = {"targetPeriodEnd": "2026-06-30", "baselinePeriodEnd": "2026-03-31"}
CONFIG = {**PHASE, "funds": [{"code": "000001", "companyCode": "co1"}]}


def physical_pages():
    pages = [""] * 14
    for number, page in zip(FIXTURE["pageNumbers"], FIXTURE["pages"]):
        pages[number - 1] = page
    return pages


def extracted():
    provider = PublicProvider()
    with patch.object(provider, "get", return_value=b"%PDF-synthetic"), patch("report_extract._pypdf_pages", return_value=physical_pages()), patch.dict(os.environ, {}, clear=True):
        return provider.report_details(REPORT)


def snapshot(item):
    payload = {"asOf": NOW[:10], "retrievedAt": NOW, "errors": [], "barsByStock": {}, "funds": [item]}
    source = "const fs=require('node:fs');const{buildSnapshot}=require('./scripts/build-data');const x=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(buildSnapshot(x.payload,{config:x.config,now:x.now})));"
    result = subprocess.run(["node", "-e", source], input=json.dumps({"payload": payload, "config": CONFIG, "now": NOW}),
                            cwd=ROOT, text=True, capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


class NonstockPipelineTests(unittest.TestCase):
    def test_provider_extracts_and_maps_every_nested_physical_page(self):
        details = extracted()
        proof = details["noEquitiesEvidence"]
        self.assertEqual(proof["pages"], [8, 12, 13])
        self.assertEqual(proof["otherAssetsTable"]["pageNumbers"], [12, 13])
        self.assertEqual(proof, parse_no_equities(physical_pages(), REPORT))
        self.assertEqual(details["parseMethod"], "pypdf")
        self.assertNotIn("text", proof)

    def test_common_extractor_never_uses_mineru_block_ordinals_as_physical_pages(self):
        for numbers, accepted in [([8, 12, 13], True), ([8, None, 13], False), ([8, 12, 14], False), ([None] * 3, False)]:
            details = _details_for_pages(FIXTURE["pages"], REPORT["sourceUrl"], REPORT["periodEnd"], REPORT["kind"],
                                         parse_pdf_pages, page_numbers=numbers, published_at=REPORT["publishedAt"])
            self.assertEqual(details["noEquitiesEvidence"] is not None, accepted)
            if accepted:
                self.assertEqual(details["noEquitiesEvidence"]["assetTable"]["startPage"], 8)
                self.assertEqual(details["noEquitiesEvidence"]["otherAssetsTable"]["endPage"], 13)

    def test_collect_build_quality_and_replayed_status_keep_full_small_proof(self):
        details = extracted()
        class Source:
            def overview(self, code): return {"name": "合成基金", "company": "合成机构", "managers": []}
            def announcements(self, code, as_of): return [REPORT]
            def holdings(self, code, year): return {}
            def report_details(self, document, **kwargs): return copy.deepcopy(details)
        item, _ = collect_fund({"code": "000001"}, NOW[:10], provider=Source(), target_period=PHASE["targetPeriodEnd"], baseline_period=PHASE["baselinePeriodEnd"])
        result = snapshot(item)
        self.assertEqual(result["errors"], [])
        fund = result["funds"][0]
        self.assertEqual(fund["status"], "no-equities")
        self.assertEqual(fund["noEquitiesEvidence"], details["noEquitiesEvidence"])
        quality = traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))
        self.assertEqual(quality["fields"]["holdings"], "complete")
        self.assertEqual(quality["fields"]["baseline"], "missing")
        self.assertEqual(quality["fields"]["prices"], "not-applicable")
        status = traversal.build_status(CONFIG, result, NOW)
        self.assertTrue(traversal.valid_status(status, CONFIG, NOW))
        saved = status["funds"][0]["reportEvidence"]["noEquities"]
        self.assertEqual(saved, details["noEquitiesEvidence"])
        self.assertNotIn("text", saved)
        receipt = {"schemaVersion": 1, "phase": PHASE, "generatedAt": NOW, "funds": [{"code": "000001",
                   "representativeCode": "000001", "companyCode": "co1", "status": "no-equities",
                   "reportEvidence": status["funds"][0]["reportEvidence"], "priceCoverage": traversal.price_coverage(fund),
                   "shareClassCodes": ["000001"]}]}
        events = traversal.reconcile_observations([status], receipt, CONFIG, NOW)
        directory = {"schemaVersion": 1, "retrievedAt": NOW, "companies": [{"code": "co1", "name": "合成机构", "fundCount": 1, "directoryStatus": "complete"}],
                     "funds": [{"code": "000001", "name": "合成基金", "companyCode": "co1"}]}
        progress = traversal.merge_progress(directory, None, CONFIG, events, NOW)
        self.assertEqual(progress["funds"][0]["status"], "no-equities")
        self.assertEqual(events[0]["funds"][0]["reportEvidence"]["noEquities"], saved)
        broken = copy.deepcopy(status)
        broken["funds"][0]["reportEvidence"]["noEquities"]["otherAssetsTable"]["total"]["amount"] = "0.00"
        self.assertFalse(traversal.valid_status(broken, CONFIG, NOW))
        changed = copy.deepcopy(fund)
        changed["noEquitiesEvidence"] = broken["funds"][0]["reportEvidence"]["noEquities"]
        self.assertEqual(traversal.report_quality(changed, PHASE, traversal.utc_now(NOW))["fields"]["holdings"], "missing")

    def test_collector_rejects_structured_stock_holdings_contradiction(self):
        details = extracted()
        class Source:
            def overview(self, code): return {"name": "合成基金", "company": "合成机构", "managers": []}
            def announcements(self, code, as_of): return [REPORT]
            def holdings(self, code, year): return {PHASE["targetPeriodEnd"]: [{"stockCode": "600001", "market": "CN", "shares": 10}]}
            def report_details(self, document, **kwargs): return copy.deepcopy(details)
        item, errors = collect_fund({"code": "000001"}, NOW[:10], provider=Source(), target_period=PHASE["targetPeriodEnd"])
        self.assertIn("CONTRADICTORY_NO_EQUITIES", [error["errorCode"] for error in errors])
        self.assertNotIn("noEquitiesEvidence", item)
        self.assertFalse(item["reports"])

    def test_baseline_zero_flag_without_its_own_valid_evidence_does_not_complete_comparison(self):
        proof = extracted()["noEquitiesEvidence"]
        fund = snapshot({"code": "000001", "reports": [], "noEquitiesEvidence": proof})["funds"][0]
        fund["previousReport"] = {"periodEnd": "2026-03-31", "publishedAt": "2026-04-20", "sourceUrl": "https://example.org/prior.pdf", "holdings": [], "noEquitiesConfirmed": True}
        self.assertEqual(traversal.report_quality(fund, PHASE, traversal.utc_now(NOW))["fields"]["baseline"], "missing")


if __name__ == "__main__": unittest.main()
