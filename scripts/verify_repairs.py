"""Actions-only fixed public PDF repair checks; never update production data.

PDFs and extracted details exist only in memory. The sole written result is a
small allowlisted summary, not an observation for the collection campaign.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from collect import pdf_holdings_evidence
from providers import PublicProvider
from publication_guard import inspect_content
from traversal import report_quality


SAMPLES = (
    {"code": "000041", "kind": "semiannual", "periodEnd": "2026-06-30", "publishedAt": "2026-08-31",
     "title": "华夏全球精选股票型证券投资基金2026年中期报告", "sourceUrl": "https://pdf.dfcfw.com/pdf/H2_AN202608311828778333_1.pdf",
     "check": "foreign-holdings", "holdingCount": 181, "marketCounts": {"US": 156, "HK": 25},
     "marketValueYuan": 2698469414.02, "navYuan": 3027166978.18},
    {"code": "013543", "kind": "semiannual", "periodEnd": "2026-06-30", "publishedAt": "2026-08-31",
     "title": "2026年中期报告", "sourceUrl": "https://pdf.dfcfw.com/pdf/H2_AN202608311828784262_1.pdf",
     "check": "no-equities", "evidenceType": "explicit-no-stock-detail", "page": 43},
    {"code": "253050", "kind": "semiannual", "periodEnd": "2026-06-30", "publishedAt": "2026-08-31",
     "title": "2026年中期报告", "sourceUrl": "https://pdf.dfcfw.com/pdf/H2_AN202608311828774486_1.pdf",
     "check": "no-equities", "evidenceType": "zero-stock-balance-sheet", "page": 14},
    {"code": "000606", "kind": "quarterly", "periodEnd": "2026-06-30", "publishedAt": "2026-07-21",
     "title": "天弘优选债券型证券投资基金2026年第2季度报告", "sourceUrl": "https://pdf.dfcfw.com/pdf/H2_AN202607211827184178_1.pdf",
     "check": "managers", "managerRows": [("刘洋", "2017-10-28", None, 6)], "strategyPages": [7, 8]},
    {"code": "010898", "kind": "quarterly", "periodEnd": "2026-09-30", "publishedAt": "2026-10-24",
     "title": "银河产业动力混合型证券投资基金2026年第3季度报告", "sourceUrl": "https://pdf.dfcfw.com/pdf/H2_AN202610241830863000_1.pdf",
     "check": "managers", "managerRows": [("高鹏", "2025-10-24", None, 5), ("石磊", "2026-08-01", None, 6)], "strategyPages": [7, 8]},
    {"code": "253050", "kind": "quarterly", "periodEnd": "2026-06-30", "publishedAt": "2026-07-21",
     "title": "国联安货币市场证券投资基金2026年第2季度报告", "sourceUrl": "https://pdf.dfcfw.com/pdf/H2_AN202607211827190658_1.pdf",
     "check": "no-equities", "evidenceType": "exhaustive-nonstock-assets", "page": 8,
     "pages": [8, 12, 13], "totalAssets": "33077230295.61", "otherAssets": "203521.29"},
)
OUTPUT = Path(".cache/repair-verification/result.json")
ROOT = Path(__file__).resolve().parents[1]
KNOWN_ERRORS = {"HTTP_REJECTED", "HTTP_ERROR", "TLS_FAILED", "TIMEOUT", "NETWORK_ERROR", "INVALID_PDF",
                "EMPTY_RESPONSE", "RESPONSE_TOO_LARGE", "PDF_PARSE_FAILED", "PDF_TEXT_EMPTY", "SNAPSHOT_REJECTED",
                "SNAPSHOT_BRIDGE_FAILED", "REPAIR_VERIFICATION_FAILED"}
NODE_BRIDGE = """const fs=require('node:fs');
const {buildSnapshot}=require('./scripts/build-data');
try{const x=JSON.parse(fs.readFileSync(0,'utf8'));
const snapshot=buildSnapshot(x.payload,{now:x.now,config:x.config});
process.stdout.write(JSON.stringify({fund:snapshot.funds[0]||null,errorCount:snapshot.errors.length}));
}catch(_){process.stdout.write(JSON.stringify({fund:null,errorCount:1}));process.exitCode=1;}
"""


class VerificationError(Exception):
    def __init__(self, code):
        self.code = code if code in KNOWN_ERRORS else "REPAIR_VERIFICATION_FAILED"
        super().__init__(self.code)


def phase_for(sample):
    end = dt.date.fromisoformat(sample["periodEnd"])
    first = dt.date(end.year, ((end.month - 1) // 3) * 3 + 1, 1)
    return {"targetPeriodEnd": end.isoformat(), "baselinePeriodEnd": (first - dt.timedelta(days=1)).isoformat()}


def normalized_snapshot(sample, details, now):
    """Pass PDF findings through the same strict builder without writing files."""
    document = {key: sample[key] for key in ("title", "kind", "periodEnd", "publishedAt", "sourceUrl")}
    document["fundCode"] = sample["code"]
    fields = ("navYuan", "narrative", "parseMethod", "parseErrorCode", "sourceTextHash", "pageStats",
              "strategyExcerpts", "strategyThemes", "strategySectionCoverage", "strategyAnalysis",
              "reportPdfSourceUrl", "reportPdfPeriodEnd", "reportPdfKind")
    report = {**document, **{key: details[key] for key in fields if key in details},
              "holdings": [], "coverage": "unknown", "managers": details.get("reportManagers", []),
              "managerEvidenceRetrievedAt": now.isoformat(), "evidenceRetrievedAt": now.isoformat()}
    if "managerTableCoverage" in details:
        report["managerTableCoverage"] = details["managerTableCoverage"]
    if "strategySectionCoverage" in details:
        report["strategyEvidenceRetrievedAt"] = now.isoformat()
    if sample["check"] == "foreign-holdings":
        holdings = details.get("pdfHoldings") or []
        proof = pdf_holdings_evidence(details, document, holdings)
        report.update(holdings=holdings, holdingsPdfEvidence=proof,
                      holdingsRetrieval="pdf-stock-table-cells", holdingsSourceUrl=document["sourceUrl"],
                      coverage=(proof or {}).get("coverage", "unknown"),
                      holdingsPossiblyTruncated=(proof or {}).get("complete") is not True)
    elif sample["check"] == "no-equities":
        report.update(noEquitiesConfirmed=True, noEquitiesEvidence=details.get("noEquitiesEvidence"), coverage="full")
    else:
        # Manager checks deliberately do not assert that this report's equity
        # table was fully recovered. A report-only record supports attribution.
        report["reportOnly"] = True
    payload = {"asOf": now.date().isoformat(), "retrievedAt": now.isoformat(), "errors": [], "barsByStock": {},
               "funds": [{"code": sample["code"], "name": "公开报告验证", "reports": [report], "managers": []}]}
    config = {**phase_for(sample), "funds": [{"code": sample["code"]}]}
    try:
        process = subprocess.run(["node", "-e", NODE_BRIDGE], input=json.dumps({"payload": payload, "config": config,
                                 "now": now.isoformat()}, ensure_ascii=False), text=True, cwd=ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=False)
        if process.returncode or len(process.stdout) > 4_000_000:
            raise VerificationError("SNAPSHOT_BRIDGE_FAILED")
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or not isinstance(result.get("fund"), dict) or result.get("errorCount"):
            raise VerificationError("SNAPSHOT_REJECTED")
        return result["fund"]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise VerificationError("SNAPSHOT_BRIDGE_FAILED") from None


def _amount(rows):
    values = [row.get("marketValueYuan") for row in rows if isinstance(row, dict)]
    if len(values) != len(rows) or not values or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
        return None
    return round(math.fsum(values), 2)


def _near(value, expected):
    return type(value) in (int, float) and math.isfinite(value) and abs(value - expected) <= 0.02


def evaluate(sample, details, fund, quality):
    """Return only fixed keys, numeric facts and bounded public manager facts."""
    report = fund.get("selectedReport") or {}
    checks = {"selectedPeriod": report.get("periodEnd") == sample["periodEnd"],
              "sourcePreserved": report.get("sourceUrl") == sample["sourceUrl"]}
    observed = {"fundStatus": fund.get("status") if fund.get("status") in {"ok", "no-equities", "unavailable"} else "unknown"}
    fields = (quality or {}).get("fields", {})
    if sample["check"] == "foreign-holdings":
        rows = report.get("holdings") or []
        counts = dict(sorted(collections.Counter(row.get("market") for row in rows).items()))
        value = _amount(rows)
        proof = report.get("holdingsPdfEvidence") or {}
        checks.update(holdingCount=len(rows) == sample["holdingCount"], marketCounts=counts == sample["marketCounts"],
                      currencySafe=all(row.get("marketValueCurrency") == "CNY" and row.get("currency") == {"US": "USD", "HK": "HKD"}.get(row.get("market")) for row in rows),
                      marketValue=_near(value, sample["marketValueYuan"]), nav=_near(report.get("navYuan"), sample["navYuan"]),
                      tableProof=proof.get("complete") is True and proof.get("rankMode") == "pdf-cells",
                      normalizedQuality=fields.get("holdings") == "complete")
        observed.update(holdingCount=len(rows), marketCounts={key: counts.get(key, 0) for key in ("US", "HK")},
                        marketValueYuan=value, navYuan=report.get("navYuan"), tableComplete=proof.get("complete") is True,
                        startPage=proof.get("startPage"), endPage=proof.get("endPage"))
    elif sample["check"] == "no-equities":
        evidence = report.get("noEquitiesEvidence") or {}
        checks.update(explicitEvidence=evidence.get("evidenceType") == sample["evidenceType"],
                      sourcePage=evidence.get("page") == sample["page"], noEquityStatus=fund.get("status") == "no-equities",
                      normalizedQuality=fields.get("holdings") == "complete")
        observed.update(evidenceType=evidence.get("evidenceType") if evidence.get("evidenceType") in
                        {"explicit-no-stock-detail", "zero-stock-balance-sheet", "exhaustive-nonstock-assets"} else "unknown", page=evidence.get("page"))
        if sample["evidenceType"] == "exhaustive-nonstock-assets":
            from nonstock_assets import valid_nonstock_assets
            asset, other = evidence.get("assetTable") or {}, evidence.get("otherAssetsTable") or {}
            checks.update(closedTableProof=valid_nonstock_assets(evidence), physicalPages=evidence.get("pages") == sample["pages"],
                          totalAssets=(asset.get("total") or {}).get("amount") == sample["totalAssets"],
                          otherAssets=(other.get("total") or {}).get("amount") == sample["otherAssets"])
            # Only fixed numeric facts are retained; the two original table
            # excerpts remain in memory with all other extracted text.
            if checks["closedTableProof"]:
                observed.update(pages=evidence["pages"], stockBookValue=evidence["stockBookValue"],
                                totalAssets=asset["total"]["amount"], otherAssets=other["total"]["amount"])
    else:
        managers = fund.get("reportManagers") or []
        rows = [(item.get("name"), item.get("startDate"), item.get("endDate"), (item.get("evidence") or {}).get("page")) for item in managers]
        checks.update(managerRows=sorted(rows) == sorted(sample["managerRows"]), reportAttribution=fund.get("managerAttribution") == "report",
                      managerTableProof=(fund.get("managerTableCoverage") or {}).get("complete") is True,
                      sourceProof=bool(managers) and all((item.get("evidence") or {}).get("sourceUrl") == sample["sourceUrl"]
                        and (item.get("evidence") or {}).get("periodEnd") == sample["periodEnd"] for item in managers),
                      normalizedQuality=fields.get("managers") == "complete")
        observed["managers"] = [{"name": str(name)[:30], "startDate": start if isinstance(start, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) else None,
                                 "endDate": end if isinstance(end, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) else None,
                                 "page": page if type(page) is int and 1 <= page <= 10000 else None} for name, start, end, page in rows[:10]]
        observed["managerTableComplete"] = (fund.get("managerTableCoverage") or {}).get("complete") is True
        proof = report.get("strategySectionCoverage") or {}
        analysis = report.get("strategyAnalysis") or {}
        sections = proof.get("sections") or []
        evidence = analysis.get("evidence") or []
        categories = ("market-review", "portfolio-operations", "outlook")
        period_start = (dt.date.fromisoformat(phase_for(sample)["baselinePeriodEnd"]) + dt.timedelta(days=1)).isoformat()
        checks.update(strategySectionComplete=proof.get("complete") is True,
                      strategyPeriod=(proof.get("periodStart") == period_start and proof.get("periodEnd") == sample["periodEnd"]
                                      and proof.get("kind") == "quarterly"),
                      strategySource=(proof.get("sourceUrl") == sample["sourceUrl"] and proof.get("publishedAt") == sample["publishedAt"]),
                      strategyBounds=(len(sections) == 1 and sections[0].get("section") == "4.4"
                                      and sections[0].get("endSection") == "4.5" and sections[0].get("pages") == sample["strategyPages"]),
                      strategyQuality=fields.get("strategy") == "complete",
                      selectedEvidence=(analysis.get("scope") == "selected-evidence" and analysis.get("analysisCoverage") == "selected-evidence"
                                        and analysis.get("status") == "available"
                                        and analysis.get("semanticAnalysisComplete") is False and analysis.get("sourceSectionComplete") is True
                                        and analysis.get("explainsIndividualStockTrades") is False and analysis.get("securityMentionsProveTrades") is False),
                      strategyEvidenceProvenance=bool(evidence) and len(evidence) <= 8 and all(isinstance(item, dict)
                          and isinstance(item.get("text"), str) and 0 < len(item["text"]) <= 160
                          and item.get("sourceUrl") == sample["sourceUrl"] and item.get("periodStart") == period_start
                          and item.get("periodEnd") == sample["periodEnd"] and item.get("page") in sample["strategyPages"] for item in evidence),
                      strategyCategories=all(any(isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()
                                                 and category in (item.get("categories") or []) for item in evidence) for category in categories))
        counts = analysis.get("categorySentenceCounts") or {}
        observed["strategy"] = {"sectionComplete": proof.get("complete") is True,
                                "analysisCoverage": "selected-evidence" if analysis.get("analysisCoverage") == "selected-evidence" else "unknown",
                                "semanticAnalysisComplete": analysis.get("semanticAnalysisComplete") is True,
                                "sectionCount": len(sections), "selectedEvidenceCount": len(evidence),
                                "sentenceCounts": {key: counts[key] if type(counts.get(key)) is int and 0 <= counts[key] <= 100000 else None
                                                   for key in (*categories, "unclassified")}}
        if checks["strategyBounds"]:
            observed["strategy"].update(section="4.4", endSection="4.5", pages=sample["strategyPages"])
    return {"code": sample["code"], "periodEnd": sample["periodEnd"], "sourceUrl": sample["sourceUrl"], "check": sample["check"],
            "status": "passed" if all(checks.values()) else "failed", "checks": checks, "observed": observed}


def run_verification(provider, *, normalize=normalized_snapshot, quality_fn=report_quality, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    results = []
    for sample in SAMPLES:
        started = time.monotonic()
        document = {key: sample[key] for key in ("title", "kind", "periodEnd", "publishedAt", "sourceUrl")}
        try:
            details = provider.report_details(document, max_pages=300)
            fund = normalize(sample, details, now)
            quality = quality_fn(fund, phase_for(sample), now)
            result = evaluate(sample, details, fund, quality)
        except Exception as error:
            code = getattr(error, "code", None)
            result = {"code": sample["code"], "periodEnd": sample["periodEnd"], "sourceUrl": sample["sourceUrl"], "check": sample["check"],
                      "status": "failed", "errorCode": code if code in KNOWN_ERRORS else "REPAIR_VERIFICATION_FAILED"}
        result["elapsedSeconds"] = round(max(0.0, time.monotonic() - started), 3)
        results.append(result)
    return {"schemaVersion": 1, "scope": "fixed-public-report-repair-verification", "provesFullCollection": False,
            "productionDataUpdated": False, "observedAt": now.isoformat(), "sampleCount": len(SAMPLES),
            "passedCount": sum(item["status"] == "passed" for item in results),
            "status": "passed" if all(item["status"] == "passed" for item in results) else "failed", "results": results}


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print(json.dumps({"status": "failed", "errorCode": "ACTIONS_REQUIRED"}))
        return 2
    logging.disable(logging.CRITICAL)
    result = run_verification(PublicProvider(timeout=20, attempts=2))
    encoded = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
    if len(encoded) > 32_000 or inspect_content(encoded):
        print(json.dumps({"status": "failed", "errorCode": "VERIFICATION_OUTPUT_REJECTED"}))
        return 1
    destination = ROOT / OUTPUT
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)
    print(json.dumps({"status": result["status"], "sampleCount": result["sampleCount"], "passedCount": result["passedCount"]}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
