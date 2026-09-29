"""Run collection in Actions and write only normalized public facts."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import re

from providers import DataError, PublicProvider


def safe_error(code, stage, error):
    return {"code": code, "stage": stage, "errorCode": error.code if isinstance(error, DataError) else "PARSE_FAILED"}


def collect_fund(item, as_of, history_years=2, max_reports=4, provider=None, target_period=None, baseline_period=None):
    code = item["code"]
    source = provider or PublicProvider()
    errors = []
    fund = {"code": code, "name": code, "company": "", "managers": [], "shareClassCodes": [code], "reports": []}
    fund["metadataSourceUrl"] = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
    fund["managersAsOf"] = dt.datetime.now(dt.timezone.utc).date().isoformat()
    fund["managerScope"] = "current-profile"
    overview = {}
    try:
        overview = source.overview(code)
        fund.update({k: overview[k] for k in ("name", "company", "managers")})
    except Exception as error:
        errors.append(safe_error(code, "metadata", error))
    try:
        reports = source.announcements(code, as_of)
        if not reports:
            raise DataError("NO_REPORTS")
    except Exception as error:
        errors.append(safe_error(code, "announcements", error))
        return fund, errors
    snapshots = {}
    year = int(as_of[:4])
    for selected_year in range(year, year - min(max(1, history_years), 3), -1):
        try:
            snapshots.update(source.holdings(code, selected_year))
        except Exception as error:
            errors.append(safe_error(code, "holdings", error))
        # Two distinct visible periods are sufficient for the initial comparison.
        if len([p for p in snapshots if p <= as_of]) >= 2:
            break
    allowed_periods = {p for p in (target_period, baseline_period) if p}
    if allowed_periods:
        reports = [r for r in reports if r["periodEnd"] in allowed_periods]
    target_reports = [r for r in reports if not target_period or r["periodEnd"] == target_period]
    if not target_reports:
        errors.append({"code": code, "stage": "target-report", "errorCode": "TARGET_REPORT_UNAVAILABLE"})
        return fund, errors
    if not snapshots or (target_period and target_period not in snapshots):
        # Absence of a holdings table does not prove absence of equity positions.
        # Only an explicit report statement can resolve a no-equities record.
        candidate = next((r for r in target_reports if r["kind"] == "quarterly"), target_reports[0])
        try:
            details = source.report_details(candidate, max_pages=80)
            evidence = details.get("noEquitiesEvidence")
            main_code = details.get("portfolioCode")
            identity_matches = not main_code or main_code == code or code in details.get("shareClassCodes", [])
            if evidence and identity_matches:
                fund["noEquitiesEvidence"] = {**evidence, "fundCode": code}
                return fund, [e for e in errors if e["stage"] != "holdings"]
        except Exception as error:
            errors.append(safe_error(code, "no-equities-report", error))
    selected_periods = sorted({r["periodEnd"] for r in reports if r["periodEnd"] in snapshots}, reverse=True)[:max(2, min(max_reports, 4))]
    for period in selected_periods:
        alternatives = [r for r in reports if r["periodEnd"] == period]
        # The newest report provides the latest narrative; the raw holdings subset
        # is conservatively classified and may be incomplete even for an interim report.
        selected = max(alternatives, key=lambda r: r["publishedAt"])
        report = {k: selected[k] for k in ("title", "periodEnd", "kind", "publishedAt", "sourceUrl")}
        report["fundCode"] = code
        report["holdingsSourceUrl"] = f"https://fundf10.eastmoney.com/ccmx_{code}.html"
        report["holdingsRetrieval"] = "eastmoney-fundarchives-year-table"
        if selected.get("sourceFundCode"):
            report["sourceFundCode"] = selected["sourceFundCode"]
        report.update({"coverage": "top10" if len(snapshots[period]) <= 10 else "unknown",
                       "holdings": snapshots[period], "navYuan": None, "narrative": None})
        # Overview scale is rounded and may describe a share class, so it is never
        # silently used as a portfolio-wide NAV. Exact report NAV is preferred.
        try:
            details = source.report_details(selected)
            report.update({k: details[k] for k in ("navYuan", "narrative")})
            if details.get("portfolioCode"):
                if details["portfolioCode"] != code and code not in details.get("shareClassCodes", []):
                    errors.append({"code": code, "stage": "report", "errorCode": "REPORT_IDENTITY_MISMATCH"})
                    continue
                fund["portfolioCode"] = details["portfolioCode"]
                fund["sourcePortfolioCode"] = details["portfolioCode"]
                fund["portfolioSourceUrl"] = selected["sourceUrl"]
            if details.get("shareClassCodes"):
                fund["shareClassCodes"] = details["shareClassCodes"]
        except Exception as error:
            errors.append(safe_error(code, "report", error))
        # Quarter-specific operations use the quarter report even when the newer
        # interim report supplies same-date NAV and the latest holdings table.
        quarterly = next((r for r in alternatives if r["kind"] == "quarterly" and r != selected), None)
        if quarterly:
            try:
                quarterly_details = source.report_details(quarterly)
                if quarterly_details.get("narrative"):
                    report["narrative"] = quarterly_details["narrative"]
                    report["narrative"]["reportKind"] = "quarterly"
                if report["navYuan"] is None:
                    report["navYuan"] = quarterly_details["navYuan"]
                    report["navSourceUrl"] = quarterly["sourceUrl"]
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


def collect(config, as_of):
    items = config["funds"]
    workers = min(2, max(1, int(config.get("maxWorkers", 2))))
    funds, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(collect_fund, x, as_of, config.get("historyYears", 2), config.get("maxReportsPerFund", 4),
                               target_period=config.get("targetPeriodEnd"), baseline_period=config.get("baselinePeriodEnd")) for x in items]
        for future in futures:
            fund, issues = future.result()
            funds.append(fund)
            errors.extend(issues)
    requested_prices = {}
    for fund in funds:
        reports = sorted(fund["reports"], key=lambda r: r["periodEnd"], reverse=True)
        if len(reports) < 2:
            continue
        start = (dt.date.fromisoformat(reports[1]["periodEnd"]) + dt.timedelta(days=1)).isoformat()
        end = reports[0]["periodEnd"]
        for holding in reports[0]["holdings"]:
            key = holding["market"] + ":" + holding["stockCode"]
            if key in requested_prices:
                old = requested_prices[key]
                start_item, end_item = min(start, old[2]), max(end, old[3])
            else:
                start_item, end_item = start, end
            requested_prices[key] = (holding["stockCode"], holding["market"], start_item, end_item)
    bars = {}
    selected_prices = list(requested_prices.items())[:min(300, max(0, int(config.get("maxPriceSymbols", 150))))]
    def price(item):
        key, args = item
        try:
            return key, PublicProvider().price_bars(*args), None
        except Exception as error:
            return key, None, safe_error(args[0], "prices", error)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for key, value, error in pool.map(price, selected_prices):
            if value:
                bars[key] = value
            if error:
                errors.append(error)
    observed = sum(bool(f["reports"]) for f in funds)
    comparable = sum(len(f["reports"]) >= 2 for f in funds)
    return {"asOf": as_of, "retrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(), "funds": funds,
            "barsByStock": bars, "errors": errors,
            "coverage": {"requested": len(items), "withHoldings": observed, "comparable": comparable,
                         "withNarrative": sum(any(r["narrative"] for r in f["reports"]) for f in funds),
                         "priceSymbolsRequested": len(requested_prices), "priceSymbolsAttempted": len(selected_prices),
                         "priceSymbolsFetched": len(bars), "status": "complete" if comparable == len(items) and not errors else "partial"}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/universe.json")
    parser.add_argument("--output", default="raw/input.json")
    parser.add_argument("--as-of", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    dt.date.fromisoformat(args.as_of)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.limit is not None:
        config["funds"] = config["funds"][:max(0, args.limit)]
    if not config["funds"] or any(not re.fullmatch(r"\d{6}", str(x.get("code", ""))) for x in config["funds"]):
        raise DataError("INVALID_CONFIG")
    if os.environ.get("GITHUB_ACTIONS") != "true" and len(config["funds"]) > 2:
        raise DataError("BATCH_REQUIRES_ACTIONS")
    payload = collect(config, args.as_of)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"status": payload["coverage"]["status"], "counts": payload["coverage"], "errorCount": len(payload["errors"])}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "errorCode": error.code if isinstance(error, DataError) else "COLLECTION_FAILED"}))
        raise SystemExit(1) from None
