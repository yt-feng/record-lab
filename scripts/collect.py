"""Run collection in Actions and write only normalized public facts."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import re
import time

from providers import DataError, PublicProvider, HOLDINGS_TOPLINE
from lifecycle import normalize_lifecycle_evidence, should_check_lifecycle
from report_managers import normalize_manager_table_coverage


def safe_error(code, stage, error):
    result = {"code": code, "stage": stage, "errorCode": error.code if isinstance(error, DataError) else "PARSE_FAILED"}
    if isinstance(error, DataError) and getattr(error, "provider_errors", None):
        result["providerErrors"] = error.provider_errors
    return result


def pdf_holdings_evidence(details, document, holdings):
    """Keep stock-table proof tied to its PDF, separately from strategy text."""
    meta = details.get("pdfHoldingsMetadata")
    if not isinstance(meta, dict) or not holdings:
        return None
    extraction = details.get("pdfHoldingsExtraction") or details
    stats = extraction.get("pageStats") or {}
    count = lambda value: isinstance(value, int) and not isinstance(value, bool) and value >= 0
    total, processed = stats.get("totalPages"), stats.get("processedPages")
    start, end = meta.get("startPage"), meta.get("endPage")
    section, following = meta.get("sectionNumber"), meta.get("nextSectionNumber")
    sections_valid = (isinstance(section, str) and isinstance(following, str)
                      and re.fullmatch(r"\d+(?:\.\d+){1,2}", section)
                      and re.fullmatch(r"\d+(?:\.\d+){1,2}", following)
                      and tuple(map(int, following.split('.'))) > tuple(map(int, section.split('.'))))
    rank_count = meta.get("rankGroups") if meta.get("rankMode") == "pdf-cells" else len(holdings)
    complete = bool(meta.get("complete") is True and meta.get("sectionBounded") is True
                    and sections_valid and meta.get("contiguousRanks") is True and meta.get("unitsVerified") is True
                    and meta.get("coverage") in {"full", "top10"}
                    and count(meta.get("rowsSeen")) and meta["rowsSeen"] == len(holdings)
                    and count(meta.get("parsedRows")) and meta["parsedRows"] == len(holdings)
                    and type(meta.get("excludedRows")) is int and meta["excludedRows"] == 0
                    and (meta["coverage"] != "top10" or count(rank_count) and 1 <= rank_count <= 10)
                    and count(total) and total > 0 and count(processed) and processed == total
                    and count(stats.get("pageCount")) and stats["pageCount"] == total
                    and stats.get("truncated") is False and stats.get("pageNumbersVerified") is True
                    and count(start) and count(end) and 1 <= start <= end <= total
                    and extraction.get("parseMethod") in {"pypdf", "pypdf-fallback"}
                    and re.fullmatch(r"[a-f0-9]{64}", str(extraction.get("sourceTextHash", ""))))
    return {**{key: meta.get(key) for key in ("coverage", "sectionNumber", "nextSectionNumber", "sectionBounded",
                                            "startPage", "endPage", "rowsSeen", "parsedRows", "excludedRows",
                                            "contiguousRanks", "unitsVerified")},
            **({key: meta.get(key) for key in ("rankMode", "rankGroups", "mergedSecurityRows", "continuedRows", "marketValueCurrency")}
               if meta.get("rankMode") == "pdf-cells" else {}),
            "complete": complete, "sourceUrl": document["sourceUrl"], "periodEnd": document["periodEnd"],
            "kind": document["kind"], "publishedAt": document["publishedAt"],
            "sourceTextHash": extraction.get("sourceTextHash"), "parseMethod": extraction.get("parseMethod"),
            "totalPages": total, "processedPages": processed,
            "documentTruncated": stats.get("truncated"), "pageNumbersVerified": stats.get("pageNumbersVerified")}


def collect_fund(item, as_of, history_years=2, max_reports=4, provider=None, target_period=None, baseline_period=None, deadline=None):
    code = item["code"]
    errors = []
    fund = {"code": code, "name": item.get("name", code), "company": item.get("companyName") or item.get("company") or "",
            "managers": [], "shareClassCodes": [code], "reports": []}
    if item.get("companyCode") or item.get("companyId"):
        fund["companyId"] = item.get("companyCode") or item.get("companyId")
    fund["metadataSourceUrl"] = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
    fund["managersAsOf"] = dt.datetime.now(dt.timezone.utc).date().isoformat()
    fund["managerScope"] = "current-profile"
    def expired():
        if deadline is not None and time.monotonic() >= deadline:
            errors.append({"code": code, "stage": "budget", "errorCode": "BATCH_DEADLINE_REACHED"})
            return True
        return False
    if expired():
        fund["collectionAttempt"] = {"attempted": False, "status": "deferred-budget", "errorCode": "BATCH_DEADLINE_REACHED"}
        return fund, errors
    # Only products that reach their first source request consume an attempt.
    # A later deadline cannot erase an actual source attempt or its failure.
    fund["collectionAttempt"] = {"attempted": True}
    source = provider or PublicProvider()
    def check_missing_target_lifecycle():
        if not should_check_lifecycle(target_period, as_of) or not callable(getattr(source, "lifecycle_evidence", None)) or expired():
            return False
        try:
            outcome = source.lifecycle_evidence(code, target_period, as_of, deadline=deadline)
            if not isinstance(outcome, dict):
                raise DataError("LIFECYCLE_PARSE_FAILED")
            for diagnostic in outcome.get("diagnostics", []):
                if isinstance(diagnostic, dict) and re.fullmatch(r"[A-Z_]+", str(diagnostic.get("errorCode", ""))):
                    errors.append({"code": code, "stage": "lifecycle", "errorCode": diagnostic["errorCode"]})
            evidence = normalize_lifecycle_evidence(outcome.get("notApplicableEvidence"), code, target_period, as_of)
            if evidence:
                fund["notApplicableEvidence"] = evidence
                return True
        except Exception as error:
            errors.append(safe_error(code, "lifecycle", error))
        return False
    overview = {}
    try:
        overview = source.overview(code)
        fund.update({k: overview[k] for k in ("name", "company", "managers")})
    except Exception as error:
        errors.append(safe_error(code, "metadata", error))
    foundation = overview.get("foundationDate")
    if isinstance(foundation, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", foundation) and foundation <= as_of:
        fund["inceptionDate"] = foundation
        fund["inceptionSourceUrl"] = fund["metadataSourceUrl"]
    if target_period and isinstance(foundation, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", foundation) and foundation > target_period and foundation <= as_of:
        fund["notApplicableEvidence"] = {"reason": "ESTABLISHED_AFTER_PERIOD", "date": foundation,
                                         "sourceUrl": fund["metadataSourceUrl"], "periodEnd": target_period,
                                         "fundCode": code}
        return fund, errors
    if expired():
        return fund, errors
    try:
        reports = source.announcements(code, as_of)
        if getattr(source, "announcement_error_code", None):
            errors.append({"code": code, "stage": "announcements", "errorCode": source.announcement_error_code})
        if not reports:
            raise DataError("NO_REPORTS")
    except Exception as error:
        if isinstance(error, DataError) and error.code == "NO_REPORTS" and check_missing_target_lifecycle():
            return fund, errors
        errors.append(safe_error(code, "announcements", error))
        return fund, errors
    allowed_periods = {p for p in (target_period, baseline_period) if p}
    if allowed_periods:
        reports = [r for r in reports if r["periodEnd"] in allowed_periods]
    if not any(not target_period or r["periodEnd"] == target_period for r in reports):
        if check_missing_target_lifecycle():
            return fund, errors
        errors.append({"code": code, "stage": "target-report", "errorCode": "TARGET_REPORT_UNAVAILABLE"})
        return fund, errors
    snapshots = {}
    year = int((target_period or as_of)[:4])
    for selected_year in range(year, year - min(max(1, history_years), 3), -1):
        if expired():
            return fund, errors
        try:
            snapshots.update(source.holdings(code, selected_year))
        except Exception as error:
            errors.append(safe_error(code, "holdings", error))
        # Two distinct visible periods are sufficient for the initial comparison.
        needed = {p for p in (target_period, baseline_period) if p}
        if (needed and needed.issubset(snapshots)) or (not needed and len([p for p in snapshots if p <= as_of]) >= 2):
            break
    extraction_keys = ("parseMethod", "parseErrorCode", "sourceTextHash", "pageStats",
                       "strategyExcerpts", "strategyThemes", "reportPdfSourceUrl",
                       "reportPdfPeriodEnd", "reportPdfKind")

    def identity_matches(details):
        main_code = details.get("portfolioCode")
        return not main_code or main_code == code or code in details.get("shareClassCodes", [])

    def attach_details(report, details, document):
        report.update({key: details.get(key) for key in ("navYuan", "narrative")})
        if isinstance(details.get("managerTableCoverage"), dict):
            report["managerTableCoverage"] = details["managerTableCoverage"]
        if details.get("reportManagers"):
            report["managers"] = details["reportManagers"]
            report["managerTableCoverage"] = details.get("managerTableCoverage")
            report["managerSourceUrl"] = document["sourceUrl"]
            report["managerEvidenceRetrievedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        for key in extraction_keys:
            if key in details:
                report[key] = details[key]
        if details.get("parseMethod"):
            report.update({"reportPdfSourceUrl": document["sourceUrl"],
                           "reportPdfPeriodEnd": document["periodEnd"], "reportPdfKind": document["kind"],
                           "evidenceRetrievedAt": dt.datetime.now(dt.timezone.utc).isoformat()})
        if details.get("navYuan") is not None:
            report["navRetrievedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        if details.get("portfolioCode"):
            fund.update({"portfolioCode": details["portfolioCode"], "sourcePortfolioCode": details["portfolioCode"],
                         "portfolioSourceUrl": document["sourceUrl"]})
        if details.get("shareClassCodes"):
            fund["shareClassCodes"] = details["shareClassCodes"]

    # A missing structured table must not prevent reading the product's PDF.
    selected_periods = sorted({r["periodEnd"] for r in reports}, reverse=True)[:max(2, min(max_reports, 4))]
    for period in selected_periods:
        if expired():
            return fund, errors
        alternatives = [r for r in reports if r["periodEnd"] == period]
        selected = max(alternatives, key=lambda r: r["publishedAt"])
        report = {k: selected[k] for k in ("title", "periodEnd", "kind", "publishedAt", "sourceUrl")}
        report["fundCode"] = code
        if selected.get("sourceFundCode"):
            report["sourceFundCode"] = selected["sourceFundCode"]
        holdings = snapshots.get(period, [])
        report.update({"coverage": "top10" if 0 < len(holdings) <= 10 else "unknown",
                       "holdings": holdings, "navYuan": None, "narrative": None,
                       "holdingsSourceUrl": f"https://fundf10.eastmoney.com/ccmx_{code}.html" if holdings else None,
                       "holdingsRetrieval": "eastmoney-fundarchives-year-table" if holdings else None,
                       "holdingsRequestedLimit": HOLDINGS_TOPLINE,
                       "holdingsPossiblyTruncated": len(holdings) >= HOLDINGS_TOPLINE,
                       "holdingsIncomplete": getattr(holdings, "excluded_rows", 0) > 0,
                       "holdingsExcludedRowCount": getattr(holdings, "excluded_rows", 0),
                       "holdingsIncompleteReason": "UNSUPPORTED_OR_INVALID_SECURITY_ROWS" if getattr(holdings, "excluded_rows", 0) else None})
        details = {}
        try:
            details = source.report_details(selected)
            if not identity_matches(details):
                errors.append({"code": code, "stage": "report", "errorCode": "REPORT_IDENTITY_MISMATCH"})
                continue
            attach_details(report, details, selected)
            pdf_holdings = details.get("pdfHoldings") or []
            stock_proof = pdf_holdings_evidence(details, selected, pdf_holdings)
            proven_complete = bool(stock_proof and stock_proof["complete"])
            # Prefer a larger disclosed PDF table when all old securities remain
            # represented; otherwise retain the structured table and its source.
            if pdf_holdings and (not holdings or {(h["market"], h["stockCode"]) for h in holdings}.issubset({(h["market"], h["stockCode"]) for h in pdf_holdings})
                                 and (len(pdf_holdings) > len(holdings) or proven_complete and
                                      (report["holdingsPossiblyTruncated"] or report["holdingsIncomplete"]
                                       or report["coverage"] != stock_proof["coverage"]))):
                holdings = pdf_holdings
                excluded = getattr(pdf_holdings, "excluded_rows", 0)
                excluded = 0 if proven_complete else max(excluded, report["holdingsExcludedRowCount"])
                report.update({"holdings": holdings, "coverage": stock_proof["coverage"] if proven_complete else "unknown",
                               "holdingsSourceUrl": selected["sourceUrl"],
                               "holdingsRetrieval": "pdf-stock-table-cells" if stock_proof and stock_proof.get("rankMode") == "pdf-cells" else "pdf-stock-table-text",
                               "holdingsPossiblyTruncated": not proven_complete,
                               "holdingsIncomplete": excluded > 0, "holdingsExcludedRowCount": excluded,
                               "holdingsIncompleteReason": "UNSUPPORTED_OR_INVALID_SECURITY_ROWS" if excluded else None})
                if stock_proof:
                    report["holdingsPdfEvidence"] = stock_proof
        except Exception as error:
            errors.append(safe_error(code, "report", error))
        evidence = details.get("noEquitiesEvidence")
        if evidence and (holdings or report["holdingsExcludedRowCount"]):
            errors.append({"code": code, "stage": "report", "errorCode": "CONTRADICTORY_NO_EQUITIES"})
            continue
        if not holdings:
            if evidence and isinstance(evidence.get("page"), int) and evidence["page"] > 0:
                report.update({"noEquitiesConfirmed": True, "noEquitiesEvidence": {**evidence, "fundCode": code},
                               "coverage": "full"})
                if not target_period or period == target_period:
                    fund["noEquitiesEvidence"] = report["noEquitiesEvidence"]
            elif details.get("sourceTextHash") and details.get("parseMethod"):
                report["reportOnly"] = True
                errors.append({"code": code, "stage": "holdings", "errorCode": "HOLDINGS_UNRESOLVED"})
            else:
                continue
        # Quarter-specific strategy uses its own PDF, never the interim period
        # merely because interim NAV and holdings refer to the same date.
        quarterly = next((r for r in alternatives if r["kind"] == "quarterly" and r != selected), None)
        if quarterly and not (deadline is not None and time.monotonic() >= deadline):
            try:
                quarter_details = source.report_details(quarterly)
                if not identity_matches(quarter_details):
                    raise DataError("REPORT_IDENTITY_MISMATCH")
                report["narrative"] = quarter_details.get("narrative")
                if quarter_details.get("reportManagers") and (not report.get("managers")
                        or not (report.get("managerTableCoverage") or {}).get("complete")
                        or (quarter_details.get("managerTableCoverage") or {}).get("complete")):
                    new_managers = quarter_details["reportManagers"]
                    if report.get("managers") and not (quarter_details.get("managerTableCoverage") or {}).get("complete"):
                        # Independent partial extracts can each contain a valid
                        # appointment. Keep both, without claiming either table
                        # established the whole combined roster.
                        key = lambda row: tuple(row.get(field) for field in ("name", "startDate", "endDate", "role"))
                        new_managers = list({key(row): row for row in [*report["managers"], *new_managers]}.values())
                    report["managers"] = new_managers
                    report["managerTableCoverage"] = normalize_manager_table_coverage(
                        quarter_details.get("managerTableCoverage"), new_managers, period)
                    report["managerSourceUrl"] = quarterly["sourceUrl"]
                    report["managerEvidenceRetrievedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
                if report["narrative"]:
                    report["narrative"]["reportKind"] = "quarterly"
                for key in extraction_keys:
                    if key in quarter_details:
                        report[key] = quarter_details[key]
                if quarter_details.get("parseMethod"):
                    report.update({"reportPdfSourceUrl": quarterly["sourceUrl"],
                                   "reportPdfPeriodEnd": period, "reportPdfKind": "quarterly",
                                   "evidenceRetrievedAt": dt.datetime.now(dt.timezone.utc).isoformat()})
                if report["navYuan"] is None:
                    report["navYuan"] = quarter_details.get("navYuan")
                    report["navSourceUrl"] = quarterly["sourceUrl"]
                    if report["navYuan"] is not None:
                        report["navRetrievedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
            except Exception as error:
                errors.append(safe_error(code, "quarterly-report", error))
        if report["navYuan"] is None:
            errors.append({"code": code, "stage": "nav", "errorCode": "NAV_NOT_DISCLOSED_OR_UNRESOLVED"})
        if report["narrative"] is None:
            errors.append({"code": code, "stage": "narrative", "errorCode": "NARRATIVE_UNRESOLVED"})
        fund["reports"].append(report)
    if not fund["reports"]:
        errors.append({"code": code, "stage": "join", "errorCode": "NO_DATED_HOLDINGS"})
    return fund, errors


def collect(config, as_of, checkpoint=None):
    items = config["funds"]
    workers = min(2, max(1, int(config.get("maxWorkers", 2))))
    funds, errors = [], []
    bars, requested_prices = {}, {}
    price_attempted = 0
    deadline = time.monotonic() + min(1800, max(1, float(config.get("maxBatchSeconds", 1500))))
    def snapshot(stage):
        comparable = sum(len(f["reports"]) >= 2 for f in funds)
        no_equities = sum(bool(f.get("noEquitiesEvidence", {}).get("confirmed")) for f in funds)
        prices_complete = sum(value.get("coverage", {}).get("completeness") == "complete" for value in bars.values())
        complete = stage == "finished" and comparable + no_equities == len(items) and not errors and prices_complete == len(requested_prices)
        payload = {"asOf": as_of, "targetPeriodEnd": config.get("targetPeriodEnd"), "baselinePeriodEnd": config.get("baselinePeriodEnd"),
                   "retrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(), "funds": funds, "barsByStock": bars, "errors": errors,
                   "coverage": {"requested": len(items), "completedFundRequests": len(funds),
                                "attemptedFundRequests": sum(f.get("collectionAttempt", {}).get("attempted") is not False for f in funds),
                                "deferredFundRequests": sum(f.get("collectionAttempt", {}).get("attempted") is False for f in funds),
                                "withHoldings": sum(bool(f["reports"]) for f in funds), "comparable": comparable,
                                "withNarrative": sum(any(r["narrative"] for r in f["reports"]) for f in funds),
                                "noEquities": no_equities, "priceSymbolsRequested": len(requested_prices),
                                "priceSymbolsAttempted": price_attempted, "priceSymbolsFetched": len(bars),
                                "priceSymbolsComplete": prices_complete,
                                "collectionStage": stage, "status": "complete" if complete else "partial"}}
        if checkpoint:
            checkpoint(payload)
        return payload
    snapshot("funds")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(collect_fund, x, as_of, config.get("historyYears", 2), config.get("maxReportsPerFund", 4),
                               target_period=config.get("targetPeriodEnd"), baseline_period=config.get("baselinePeriodEnd"), deadline=deadline) for x in items]
        for future in concurrent.futures.as_completed(futures):
            fund, issues = future.result()
            funds.append(fund)
            errors.extend(issues)
            snapshot("funds")
    order = {item["code"]: index for index, item in enumerate(items)}
    funds.sort(key=lambda fund: order[fund["code"]])
    item_by_code = {item["code"]: item for item in items}
    fund_price_keys = []
    for fund in funds:
        reports = sorted(fund["reports"], key=lambda r: r["periodEnd"], reverse=True)
        target_period = config.get("targetPeriodEnd")
        current = next((r for r in reports if not target_period or r["periodEnd"] == target_period), None)
        if current is None:
            continue
        baseline_period = config.get("baselinePeriodEnd")
        baseline = next((r for r in reports if r["periodEnd"] < current["periodEnd"] and (not baseline_period or r["periodEnd"] == baseline_period)), None)
        end = current["periodEnd"]
        if target_period or baseline is None:
            date = dt.date.fromisoformat(end)
            start = dt.date(date.year, ((date.month - 1) // 3) * 3 + 1, 1).isoformat()
        else:
            start = (dt.date.fromisoformat(baseline["periodEnd"]) + dt.timedelta(days=1)).isoformat()
        input_item = item_by_code[fund["code"]]
        targets = None
        if "priceTargets" in input_item:
            targets = {(str(x.get("market")), str(x.get("stockCode"))) for x in input_item["priceTargets"] if isinstance(x, dict)}
        # Current holdings lead; prior-only disclosures follow. Explicit enrichment
        # targets must still occur in one of the two verified report tables.
        candidates = current["holdings"] + (baseline["holdings"] if baseline else [])
        fund_keys = []
        for holding in candidates:
            if targets is not None and (holding["market"], holding["stockCode"]) not in targets:
                continue
            key = holding["market"] + ":" + holding["stockCode"]
            if key not in fund_keys:
                fund_keys.append(key)
            if key in requested_prices:
                old = requested_prices[key]
                start_item, end_item = min(start, old[2]), max(end, old[3])
            else:
                start_item, end_item = start, end
            requested_prices[key] = (holding["stockCode"], holding["market"], start_item, end_item)
        fund_price_keys.append(fund_keys)
    # Rank-round-robin keeps one large portfolio from consuming the price budget
    # before other funds' top holdings receive any coverage.
    price_order, seen = [], set()
    for rank in range(max((len(keys) for keys in fund_price_keys), default=0)):
        for keys in fund_price_keys:
            if rank < len(keys) and keys[rank] not in seen:
                seen.add(keys[rank])
                price_order.append(keys[rank])
    selected_prices = [(key, requested_prices[key]) for key in price_order[:min(2000, max(0, int(config.get("maxPriceSymbols", 150))))]]
    price_deadline = min(deadline, time.monotonic() + min(900, max(1, float(config.get("maxPriceSeconds", 600)))))
    snapshot("prices")
    def price(item):
        key, args = item
        if time.monotonic() >= price_deadline:
            return key, None, {"code": args[0], "stage": "prices", "errorCode": "PRICE_BUDGET_REACHED"}, False
        try:
            return key, PublicProvider().price_bars(*args), None, True
        except Exception as error:
            return key, None, safe_error(args[0], "prices", error), True
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(price, item) for item in selected_prices]
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            key, value, error, attempted = future.result()
            price_attempted += int(attempted)
            if value:
                bars[key] = value
            if error:
                errors.append(error)
            # Save every successful stock immediately; also checkpoint batches of
            # failures so a runner timeout never erases completed collection.
            if value or count % 5 == 0 or count == len(selected_prices):
                snapshot("prices")
    return snapshot("finished")


def write_checkpoint(path, payload):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/universe.json")
    parser.add_argument("--output", default="raw/input.json")
    parser.add_argument("--as-of", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--price-seconds", type=float)
    args = parser.parse_args()
    dt.date.fromisoformat(args.as_of)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.max_seconds is not None:
        config["maxBatchSeconds"] = args.max_seconds
    if args.price_seconds is not None:
        config["maxPriceSeconds"] = args.price_seconds
    if args.limit is not None:
        config["funds"] = config["funds"][:max(0, args.limit)]
    if not config["funds"] or any(not re.fullmatch(r"\d{6}", str(x.get("code", ""))) for x in config["funds"]):
        raise DataError("INVALID_CONFIG")
    if os.environ.get("GITHUB_ACTIONS") != "true" and len(config["funds"]) > 2:
        raise DataError("BATCH_REQUIRES_ACTIONS")
    payload = collect(config, args.as_of, checkpoint=lambda payload: write_checkpoint(args.output, payload))
    print(json.dumps({"status": payload["coverage"]["status"], "counts": payload["coverage"], "errorCount": len(payload["errors"])}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "errorCode": error.code if isinstance(error, DataError) else "COLLECTION_FAILED"}))
        raise SystemExit(1) from None
