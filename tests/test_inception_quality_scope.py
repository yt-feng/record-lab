"""Founding-date proof belongs to its product, not every portfolio alias."""
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import test_historical_inception as fixtures
from collect import collect_fund
from historical_inception import normalize_inception_conflict, normalize_profile_inception
from traversal import (base_states, hydrate_quality, normalize_quality, valid_baseline_exemption,
                       quality_for_code, report_quality)

NOW = fixtures.NOW
PHASE = {"targetPeriodEnd": "2026-06-30", "baselinePeriodEnd": "2026-03-31"}


def retained():
    return {"code": "000003", "companyId": "co1", "sourcePortfolioCode": "000003",
        "shareClassCodes": ["000003", "000004"], "portfolioSourceUrl": "https://example.org/report.pdf",
        "asOf": "2026-10-26", "retrievedAt": NOW.isoformat(), "status": "ok",
        "inceptionDate": "2026-04-10", "inceptionSourceUrl": fixtures.SOURCE_URL, "inceptionEvidence": fixtures.proof(),
        "rows": [], "selectedReport": {"periodEnd": "2026-06-30", "publishedAt": "2026-07-21", "kind": "quarterly",
            "sourceUrl": "https://example.org/report.pdf", "navYuan": 10000,
            "holdings": [{"stockCode": "600001", "market": "CN", "shares": 100, "weightPct": 1, "marketValueYuan": 100}]}}


def states(previous=None):
    catalog = {"companies": [{"code": "co1", "name": "合成机构"}],
               "funds": [{"code": code, "companyCode": "co1"} for code in ["000003", "000004"]]}
    result = base_states(catalog, previous, PHASE, NOW)[2]
    for row in result.values(): row["status"] = "complete"
    return result


class InceptionScopeTests(unittest.TestCase):
    def test_hydration_and_alias_projection_do_not_transfer_main_founding_exemption(self):
        rows = states()
        hydrate_quality(rows, [retained()], PHASE, NOW)
        self.assertEqual(rows["000003"]["quality"]["fields"]["baseline"], "not-applicable")
        self.assertEqual(rows["000004"]["quality"]["fields"]["baseline"], "missing")
        self.assertNotIn("baselineExemption", rows["000004"]["quality"])
        projected = quality_for_code(rows["000003"]["quality"], "000004")
        self.assertEqual(normalize_quality(projected, "complete", NOW, code="000004")["fields"]["baseline"], "missing")

    def test_old_alias_progress_is_revalidated_and_real_portfolio_baseline_still_shared(self):
        quality = report_quality(retained(), PHASE, NOW)
        previous = {"schemaVersion": 1, "phase": PHASE, "funds": [{
            "code": "000004", "status": "complete", "lastAttemptAt": NOW.isoformat(), "quality": quality}]}
        self.assertEqual(states(previous)["000004"]["quality"]["fields"]["baseline"], "missing")
        fund = retained()
        fund["previousReport"] = {**fund["selectedReport"], "periodEnd": "2026-03-31", "publishedAt": "2026-04-21"}
        rows = states(); hydrate_quality(rows, [fund], PHASE, NOW)
        self.assertEqual(rows["000003"]["quality"]["fields"]["baseline"], "complete")
        self.assertEqual(rows["000004"]["quality"]["fields"]["baseline"], "complete")
        prior = {"fields": {"baseline": "complete"}}
        self.assertEqual(quality_for_code(quality, "000004", prior)["fields"]["baseline"], "complete")

    def test_profile_exemption_requires_exact_official_host_and_code_path(self):
        evidence = {"reason": "ESTABLISHED_AFTER_BASELINE", "fundCode": "000003", "date": "2026-04-10",
                    "baselinePeriodEnd": "2026-03-31", "periodEnd": "2026-06-30"}
        for url in ["https://example.org/jbgk_000003.html", "https://fundf10.eastmoney.com/x/jbgk_000003.html",
                    "https://fundf10.eastmoney.com/jbgk_000004.html"]:
            self.assertFalse(valid_baseline_exemption({**evidence, "sourceUrl": url}, NOW))
        self.assertTrue(valid_baseline_exemption({**evidence, "sourceUrl": "https://fundf10.eastmoney.com/jbgk_000003.html"}, NOW))

    def test_collect_conflict_is_explicit_and_python_js_whitelists_agree(self):
        class Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None): return NOW
        with patch("collect.dt.datetime", Frozen):
            fund, _ = collect_fund({"code": "000003", "catalogInceptionEvidence": fixtures.proof()}, "2026-10-26",
                provider=fixtures.Stub("2026-07-10"), target_period="2026-06-30")
        value = fund["inceptionConflict"]
        self.assertIsNotNone(normalize_inception_conflict(value, "000003", now=NOW))
        self.assertNotIn("inceptionDate", fund)
        script = "const fs=require('fs'),v=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(require('./lib/historical-inception').normalizeInceptionConflict(v,'000003',new Date('2026-10-26T00:00:00Z'))));"
        result = subprocess.run(["node", "-e", script], input=json.dumps(value), text=True, capture_output=True,
            check=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(json.loads(result.stdout), value)
        invalid = copy.deepcopy(value); invalid["profileEvidence"]["sourceUrl"] = "https://example.org/jbgk_000003.html"
        self.assertIsNone(normalize_inception_conflict(invalid, "000003", now=NOW))
        self.assertIsNone(normalize_profile_inception(value["profileEvidence"], "000004", now=NOW))


if __name__ == "__main__": unittest.main()
