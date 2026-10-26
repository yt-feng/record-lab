"""Bounded directory traversal, source-checked statuses, and resumable progress."""
from __future__ import annotations

import argparse
from collections import deque
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse, parse_qsl

FUND_CODE = re.compile(r"\d{6}\Z")
COMPANY_CODE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
SUCCESS = {"complete", "no-equities"}
TERMINAL = SUCCESS | {"not-applicable"}
QUALITY_FIELDS = ("holdings", "pdf", "strategy", "nav", "baseline", "prices")
QUALITY_VALUES = {"complete", "missing", "partial", "truncated", "wrong-period", "not-applicable"}
QUALITY_ACCEPTED = {"complete", "not-applicable"}
QUALITY_MAX_ATTEMPTS = 3
ERROR_CODES = {
    "BATCH_FAILED", "FUND_FAILED", "NO_VALID_REPORTS", "TARGET_REPORT_MISSING", "FUND_NOT_COLLECTED",
    "NO_VALID_FRESH_FUNDS", "NO_REPORTS", "NO_DATED_HOLDINGS", "EMPTY_HOLDINGS", "OLDER_REPORT",
    "HOLDING_COVERAGE_REGRESSION", "REPORT_COVERAGE_REGRESSION", "CONTRADICTORY_NO_EQUITIES",
    "INVALID_REPORT", "INVALID_REPORT_SOURCE", "REPORT_IDENTITY_MISMATCH", "INVALID_NO_EQUITIES_EVIDENCE",
    "BATCH_DEADLINE_REACHED", "PRICE_BUDGET_REACHED",
    "TIMEOUT", "NETWORK_ERROR", "HTTP_ERROR", "TLS_FAILED", "TARGET_REPORT_UNAVAILABLE",
    "PDF_PARSE_FAILED", "PDF_TEXT_EMPTY", "INVALID_PDF", "UNSUPPORTED_MARKET", "HOLDINGS_UNRESOLVED", "INVALID_FORMAT", "PARSE_FAILED",
}
PARSER_ERRORS = {"PDF_PARSE_FAILED", "PDF_TEXT_EMPTY", "INVALID_PDF", "UNSUPPORTED_MARKET", "HOLDINGS_UNRESOLVED", "INVALID_FORMAT", "PARSE_FAILED"}
RECOVERABLE_ERRORS = {"NO_VALID_REPORTS", "TARGET_REPORT_MISSING", "NO_REPORTS", "TARGET_REPORT_UNAVAILABLE",
                      "NO_DATED_HOLDINGS", "TIMEOUT", "NETWORK_ERROR", "HTTP_ERROR", "TLS_FAILED",
                      "BATCH_FAILED", "FUND_NOT_COLLECTED", "BATCH_DEADLINE_REACHED"}
COMMAND_ERRORS = {"INVALID_CONFIG", "INVALID_CATALOG", "INVALID_PROGRESS", "INVALID_ARGUMENTS",
                  "READ_FAILED", "WRITE_FAILED", "TRAVERSAL_FAILED"}


class TraversalError(Exception):
    def __init__(self, code):
        self.code = code if code in COMMAND_ERRORS else "TRAVERSAL_FAILED"
        super().__init__(self.code)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise TraversalError("INVALID_ARGUMENTS")


def date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def instant(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def utc_now(now=None):
    value = instant(now) if isinstance(now, str) else now
    if now is not None and value is None:
        raise TraversalError("INVALID_ARGUMENTS")
    value = value or dt.datetime.now(dt.timezone.utc)
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise TraversalError("INVALID_ARGUMENTS")
    return value.astimezone(dt.timezone.utc)


def is_code(value):
    return isinstance(value, str) and FUND_CODE.fullmatch(value) is not None


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.fragment or "." not in host:
            return False
        if host.endswith((".localhost", ".local")) or re.match(r"^(?:0|10|127|169\.254|192\.168)\.", host) or re.match(r"^172\.(?:1[6-9]|2\d|3[01])\.", host):
            return False
        return not any(key.lower() in {"token", "key", "secret", "password", "signature", "authorization", "access_token", "api_key"}
                       for key, _ in parse_qsl(parsed.query))
    except ValueError:
        return False


def phase_for(config):
    if not isinstance(config, dict):
        raise TraversalError("INVALID_CONFIG")
    target, baseline = config.get("targetPeriodEnd"), config.get("baselinePeriodEnd")
    if not date(target) or not date(baseline) or baseline >= target:
        raise TraversalError("INVALID_CONFIG")
    return {"targetPeriodEnd": target, "baselinePeriodEnd": baseline}


def catalog_records(catalog):
    if not isinstance(catalog, dict) or catalog.get("schemaVersion", 1) != 1 or not isinstance(catalog.get("funds"), list):
        raise TraversalError("INVALID_CATALOG")
    companies = {}
    for item in catalog.get("companies", []):
        code = item.get("code") or item.get("id") or item.get("companyCode")
        if not isinstance(code, str) or not COMPANY_CODE.fullmatch(code):
            continue
        count = item.get("fundCount")
        companies[code] = {"code": code, "name": str(item.get("name") or item.get("companyName") or code)[:200],
                           "fundCount": count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None,
                           "directoryStatus": item.get("directoryStatus") if item.get("directoryStatus") in {"complete", "failed", "pending"} else "pending"}
    funds = {}
    for item in catalog["funds"]:
        if not isinstance(item, dict) or not is_code(item.get("code")):
            continue
        code = item["code"]
        company = item.get("companyCode") or item.get("companyId")
        company = company if company in companies else None
        record = {"code": code, "name": str(item.get("name") or code)[:200],
                  "companyCode": company, "companyName": companies[company]["name"] if company else None,
                  "type": str(item.get("type") or "未分类")[:100]}
        if code in funds and funds[code]["companyCode"] != company:
            record["companyCode"] = None
            record["companyName"] = None
        funds[code] = record
    return companies, funds


def collector_version(config):
    value = config.get("collectorVersion", 1) if isinstance(config, dict) else 1
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1000 else 1


def quality_enabled(config):
    return isinstance(config, dict) and config.get("requireReportQuality") is True


def counter(value, default=0):
    return min(1_000_000, max(0, value)) if isinstance(value, int) and not isinstance(value, bool) else default


def retry_state(previous, last, now):
    previous = previous if isinstance(previous, dict) else {}
    cycle = date(previous.get("cycleDate"))
    last_date = instant(last)
    if cycle is None or cycle > now.date():
        cycle = last_date.date() if last_date and last_date <= now else now.date()
    return {"cycleDate": cycle.isoformat(), "scheduledCycles": min(4, counter(previous.get("scheduledCycles"))),
            "nextRetryAt": None}


def midnight(value):
    return dt.datetime.combine(value, dt.time(), tzinfo=dt.timezone.utc).isoformat()


def retry_lane(lane, limit, recoverable, config, now, daily=True):
    """Open a bounded later cycle; future eligibility never means work now."""
    retry = lane["retry"]
    retry["nextRetryAt"] = None
    policy = config.get("retryPolicy") if isinstance(config, dict) else None
    if not isinstance(policy, dict) or not recoverable or lane["attempts"] < limit:
        return
    cycle = date(retry["cycleDate"])
    last = instant(lane.get("lastAttemptAt"))
    anchor = max(cycle, last.date()) if last else cycle
    until = date(policy.get("dailyUntil")) if daily else None
    next_day = anchor + dt.timedelta(days=1)
    daily_cycle = bool(until and next_day <= until and now.date() <= until)
    if daily_cycle:
        due = next_day
    else:
        maximum = min(4, counter(policy.get("maxScheduledCycles"), 4))
        if retry["scheduledCycles"] >= maximum:
            return
        interval = min(30, max(7, counter(policy.get("intervalDays"), 7)))
        due = anchor + dt.timedelta(days=interval)
    if due <= now.date():
        lane["attempts"] = 0
        retry["cycleDate"] = now.date().isoformat()
        if not daily_cycle:
            retry["scheduledCycles"] += 1
        if lane.get("status") == "exhausted":
            lane["status"] = "pending"
    else:
        retry["nextRetryAt"] = midnight(due)


def apply_retry_policy(states, config, now):
    for state in states.values():
        retry_lane(state, 2, state["status"] == "failed" and state.get("errorCode") in RECOVERABLE_ERRORS, config, now)
        quality = state["quality"]
        fields = quality["fields"]
        missing_report = fields["pdf"] == "missing" and quality.get("pdfRetryable", True)
        retry_lane(quality, QUALITY_MAX_ATTEMPTS, state["status"] in SUCCESS and missing_report, config, now)
        retry_lane(state["enrichment"], 3, state["status"] == "complete" and state["enrichment"]["status"] == "exhausted",
                   config, now, daily=False)
        times = [state.get("deferredUntil"), state["retry"]["nextRetryAt"],
                 quality["retry"]["nextRetryAt"], state["enrichment"]["retry"]["nextRetryAt"]]
        state["nextRetryAt"] = min((value for value in times if instant(value) and instant(value) > now), default=None)


def base_states(catalog, previous, phase, now, config=None):
    companies, records = catalog_records(catalog)
    old = {}
    if isinstance(previous, dict) and previous.get("schemaVersion") == 1 and previous.get("phase") == phase:
        for item in previous.get("funds", []):
            if isinstance(item, dict) and is_code(item.get("code")):
                old[item["code"]] = item
    states = {}
    version_changed = collector_version(config) > collector_version(previous)
    for code, record in records.items():
        prior = old.get(code, {})
        attempted_at = prior.get("lastAttemptAt")
        parsed = instant(attempted_at)
        valid_attempt = parsed is not None and parsed <= now + dt.timedelta(minutes=5)
        status = prior.get("status") if prior.get("status") in TERMINAL | {"failed", "pending"} else "pending"
        if status != "pending" and not valid_attempt:
            status = "pending"
        exemption = prior.get("notApplicableEvidence")
        if status == "not-applicable" and not valid_not_applicable(exemption, code, phase, now):
            status = "pending"
        failures = prior.get("attempts", 0)
        failures = max(0, min(2, failures)) if isinstance(failures, int) and not isinstance(failures, bool) else 0
        if version_changed:
            failures = 0
        states[code] = {"code": code, "companyCode": record["companyCode"], "status": status,
                        "attempts": failures, "lastAttemptAt": attempted_at if valid_attempt else None,
                        "totalAttempts": counter(prior.get("totalAttempts"), counter(prior.get("attempts"))),
                        "retry": retry_state(prior.get("retry"), attempted_at if valid_attempt else None, now),
                        "deferredUntil": prior.get("deferredUntil") if instant(prior.get("deferredUntil")) and instant(prior["deferredUntil"]) > now else None,
                        "lastDeferredAt": prior.get("lastDeferredAt") if instant(prior.get("lastDeferredAt")) and instant(prior["lastDeferredAt"]) <= now else None,
                        "errorCode": prior.get("errorCode") if status == "failed" and prior.get("errorCode") in ERROR_CODES else None,
                        "attempted": bool(prior.get("attempted", valid_attempt and prior.get("attribution") != "portfolio-evidence")),
                        "representativeCode": prior.get("representativeCode") if is_code(prior.get("representativeCode"))
                        and prior.get("representativeCode") in records and records[prior["representativeCode"]]["companyCode"] == record["companyCode"] else code,
                        "attribution": "portfolio-evidence" if prior.get("attribution") == "portfolio-evidence" else "direct"}
        prior_enrichment = prior.get("enrichment")
        prior_quality = prior.get("quality")
        if isinstance(prior_quality, dict) and isinstance(prior_quality.get("baselineExemption"), dict):
            exemption_phase = prior_quality["baselineExemption"]
            if (exemption_phase.get("periodEnd") != phase["targetPeriodEnd"]
                    or exemption_phase.get("baselinePeriodEnd") != phase["baselinePeriodEnd"]):
                prior_quality = {key: value for key, value in prior_quality.items() if key != "baselineExemption"}
        if version_changed:
            states[code]["retry"] = retry_state(None, None, now)
            prior_enrichment = prior_enrichment if isinstance(prior_enrichment, dict) else {}
            prior_quality = prior_quality if isinstance(prior_quality, dict) else {}
            prior_enrichment = {**prior_enrichment, "attempts": 0,
                                "totalAttempts": counter(prior_enrichment.get("totalAttempts"), counter(prior_enrichment.get("attempts"))),
                                "retry": retry_state(None, None, now)}
            prior_quality = {**prior_quality, "attempts": 0,
                             "totalAttempts": counter(prior_quality.get("totalAttempts"), counter(prior_quality.get("attempts"))),
                             "retry": retry_state(None, None, now)}
        states[code]["enrichment"] = normalize_enrichment(prior_enrichment, status, now)
        states[code]["quality"] = normalize_quality(prior_quality, status, now)
        if status == "not-applicable":
            states[code]["notApplicableEvidence"] = {key: exemption[key] for key in ("reason", "date", "sourceUrl", "periodEnd", "fundCode")}
    apply_retry_policy(states, config, now)
    return companies, records, states


def directory_incomplete(companies, records):
    return any(item["directoryStatus"] != "complete" for item in companies.values()) or any(not item["companyCode"] for item in records.values())


def eligible(states):
    # Institution mapping is an independent catalogue task. A valid fund code
    # can be collected directly even while its owner remains unresolved.
    return [code for code, state in states.items()
            if state["status"] in {"pending", "failed"} and state["attempts"] < 2 and not state.get("deferredUntil")]


def stock_key(item):
    if not isinstance(item, dict) or item.get("market") not in {"CN", "HK"}:
        return None
    code = item.get("stockCode")
    return f"{item['market']}:{code}" if isinstance(code, str) and re.fullmatch(r"\d{5,6}", code) else None


def missing_stock_objects(value):
    """Decode compact checkpoints; status and collector interfaces keep objects."""
    if not isinstance(value, dict):
        return None
    if "missingStockKeys" in value:
        keys = value["missingStockKeys"]
        if not isinstance(keys, list) or any(not isinstance(key, str) or not re.fullmatch(r"(?:CN|HK):\d{5,6}", key) for key in keys):
            return None
        items = [{"market": key.split(":", 1)[0], "stockCode": key.split(":", 1)[1]} for key in keys]
        if "missingStocks" in value and value["missingStocks"] != items:
            return None
        return items
    items = value.get("missingStocks")
    return items if isinstance(items, list) and all(stock_key(item) for item in items) else None


def compact_enrichment(value):
    items = missing_stock_objects(value)
    result = {key: item for key, item in value.items() if key not in {"missingStocks", "missingStockKeys"}}
    result["missingStockKeys"] = [stock_key(item) for item in items] if items is not None else []
    return result


def valid_price_coverage(value):
    if not isinstance(value, dict):
        return False
    count, available = value.get("holdingCount"), value.get("priceAvailable")
    missing = missing_stock_objects(value)
    fingerprint = value.get("holdingsIdentity")
    return (isinstance(count, int) and not isinstance(count, bool) and count >= 0
            and isinstance(available, int) and not isinstance(available, bool) and 0 <= available <= count
            and isinstance(missing, list) and len(missing) == count - available
            and all(stock_key(item) for item in missing)
            and len({stock_key(item) for item in missing}) == len(missing)
            and isinstance(fingerprint, str) and bool(re.fullmatch(r"[a-f0-9]{64}", fingerprint)))


def price_coverage(fund):
    holdings = fund.get("selectedReport", {}).get("holdings", [])
    if not isinstance(holdings, list) or any(not stock_key(item) for item in holdings):
        return None
    stocks = {stock_key(item): {"stockCode": item["stockCode"], "market": item["market"]} for item in holdings}
    observed = set()
    for row in fund.get("rows", []):
        key = stock_key(row)
        price = row.get("priceRange", {}) if isinstance(row, dict) else {}
        low, high = price.get("low"), price.get("high")
        if (key in stocks and price.get("status") == "observed" and price.get("priceBasis") == "unadjusted"
                and public_url(price.get("sourceUrl")) and isinstance(low, (int, float)) and not isinstance(low, bool)
                and isinstance(high, (int, float)) and not isinstance(high, bool) and 0 < low <= high
                and price.get("currency") == ("CNY" if stocks[key]["market"] == "CN" else "HKD")):
            observed.add(key)
    missing = [stocks[key] for key in sorted(set(stocks) - observed)]
    return {"holdingCount": len(stocks), "priceAvailable": len(observed), "missingStocks": missing,
            "holdingsIdentity": hashlib.sha256("|".join(sorted(stocks)).encode()).hexdigest()}


def normalize_enrichment(previous, status, now):
    previous = previous if isinstance(previous, dict) else {}
    attempts = previous.get("attempts", 0)
    attempts = max(0, min(3, attempts)) if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
    last = previous.get("lastAttemptAt")
    if not instant(last) or instant(last) > now + dt.timedelta(minutes=5):
        last = None
    result = {"status": "not-applicable", "attempts": attempts, "lastAttemptAt": last,
              "totalAttempts": counter(previous.get("totalAttempts"), attempts), "retry": retry_state(previous.get("retry"), last, now),
              "holdingCount": None, "priceAvailable": None, "missingStocks": [], "holdingsIdentity": None,
              "errorCode": None}
    if status == "no-equities":
        result.update({"holdingCount": 0, "priceAvailable": 0})
    elif status == "complete":
        if valid_price_coverage(previous):
            result.update({key: previous[key] for key in ("holdingCount", "priceAvailable", "holdingsIdentity")})
            result["missingStocks"] = missing_stock_objects(previous)
        full = result["holdingCount"] is not None and result["priceAvailable"] == result["holdingCount"]
        result["status"] = "complete" if full else "exhausted" if attempts >= 3 else "pending"
        result["errorCode"] = None if full else "PRICE_COVERAGE_INCOMPLETE"
    return result


def enrichment_eligible(states):
    return [code for code, state in states.items() if state["status"] == "complete"
            and not state.get("deferredUntil")
            and state["attribution"] != "portfolio-evidence" and state.get("representativeCode", code) == code
            and state["enrichment"]["status"] == "pending"
            and state["enrichment"]["attempts"] < 3]


def apply_coverage(state, coverage):
    current = state["enrichment"]
    if not valid_price_coverage(coverage):
        return
    missing = missing_stock_objects(coverage)
    if valid_price_coverage(current) and current["holdingsIdentity"] == coverage["holdingsIdentity"]:
        still_missing = {stock_key(item) for item in missing_stock_objects(current)}
        missing = [item for item in missing if stock_key(item) in still_missing]
    current.update({key: coverage[key] for key in ("holdingCount", "holdingsIdentity")})
    current.update({"missingStocks": missing, "priceAvailable": coverage["holdingCount"] - len(missing)})
    current["status"] = "complete" if not missing else "exhausted" if current["attempts"] >= 3 else "pending"
    current["errorCode"] = None if not missing else "PRICE_COVERAGE_INCOMPLETE"


def normalize_quality(previous, status, now):
    previous = previous if isinstance(previous, dict) else {}
    raw_fields = previous.get("fields") if isinstance(previous.get("fields"), dict) else {}
    if status == "not-applicable":
        return {"fields": {key: "not-applicable" for key in QUALITY_FIELDS}, "missingFields": [],
                "attempts": 0, "totalAttempts": counter(previous.get("totalAttempts")),
                "retry": retry_state(previous.get("retry"), previous.get("lastAttemptAt"), now),
                "lastAttemptAt": None, "status": "not-applicable"}
    fields = {key: raw_fields.get(key) if raw_fields.get(key) in QUALITY_VALUES else "missing"
              for key in QUALITY_FIELDS}
    if status == "no-equities":
        fields.update({"holdings": "complete", "baseline": "not-applicable", "prices": "not-applicable"})
    baseline_exemption = previous.get("baselineExemption")
    baseline_exempt = valid_baseline_exemption(baseline_exemption, now)
    for key in QUALITY_FIELDS:
        if fields[key] == "not-applicable" and not ((status == "no-equities" and key in {"baseline", "prices"})
                                                    or key == "baseline" and baseline_exempt):
            fields[key] = "missing"
    attempts = previous.get("attempts", 0)
    attempts = max(0, min(QUALITY_MAX_ATTEMPTS, attempts)) if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
    last = previous.get("lastAttemptAt")
    if not instant(last) or instant(last) > now + dt.timedelta(minutes=5):
        last = None
    missing = [key for key in QUALITY_FIELDS if fields[key] not in QUALITY_ACCEPTED]
    result = {"fields": fields, "missingFields": missing, "attempts": attempts, "lastAttemptAt": last,
              "totalAttempts": counter(previous.get("totalAttempts"), attempts), "retry": retry_state(previous.get("retry"), last, now),
              "pdfRetryable": previous.get("pdfRetryable") is not False,
              "status": "complete" if not missing else "exhausted" if attempts >= QUALITY_MAX_ATTEMPTS else "pending"}
    if baseline_exempt and fields["baseline"] == "not-applicable":
        result["baselineExemption"] = {key: baseline_exemption[key] for key in ("reason", "date", "sourceUrl", "fundCode", "baselinePeriodEnd", "periodEnd")}
    return result


def valid_baseline_exemption(evidence, now):
    if not isinstance(evidence, dict) or evidence.get("reason") != "ESTABLISHED_AFTER_BASELINE" or not is_code(evidence.get("fundCode")):
        return False
    established, baseline, target = (date(evidence.get(key)) for key in ("date", "baselinePeriodEnd", "periodEnd"))
    return (bool(established and baseline and target) and baseline < established <= target <= now.date()
            and public_url(evidence.get("sourceUrl"))
            and urlparse(evidence["sourceUrl"]).path.endswith(f"jbgk_{evidence['fundCode']}.html"))


def quarter_start(period_end):
    value = date(period_end)
    return dt.date(value.year, ((value.month - 1) // 3) * 3 + 1, 1).isoformat() if value else None


def report_quality(fund, phase, now):
    """Measure retained source evidence, not a collector's success flag."""
    if not isinstance(fund, dict) or not is_code(fund.get("code")):
        return None
    report = fund.get("selectedReport")
    observed = instant(fund.get("retrievedAt"))
    if (not isinstance(report, dict) or report.get("periodEnd") != phase["targetPeriodEnd"]
            or not public_url(report.get("sourceUrl")) or not observed or observed > now + dt.timedelta(minutes=5)):
        return None
    published = date(report.get("publishedAt"))
    if not published or published < date(report["periodEnd"]) or published > observed.date():
        return None
    if report.get("fundCode") not in (None, fund["code"]) or report.get("sourceFundCode") not in (None, fund["code"]):
        return None
    fields = {key: "missing" for key in QUALITY_FIELDS}
    if fund.get("status") == "no-equities":
        fields["holdings"] = "complete"
    elif fund.get("status") == "ok" and isinstance(report.get("holdings"), list) and report["holdings"]:
        fields["holdings"] = ("truncated" if report.get("holdingsPossiblyTruncated") is True else
                              "partial" if report.get("holdingsIncomplete") is True else "complete")
    stats = report.get("pageStats") if isinstance(report.get("pageStats"), dict) else {}
    fingerprint = report.get("sourceTextHash")
    if (report.get("parseMethod") in {"pypdf", "pypdf-fallback", "mineru", "ocr"}
            and isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint)
            and isinstance(stats.get("charCount"), int) and stats["charCount"] > 0
            and isinstance(stats.get("nonEmptyPages"), int) and stats["nonEmptyPages"] > 0):
        if stats.get("truncated") is True:
            fields["pdf"] = "truncated"
        elif stats.get("truncated") is False and stats.get("textStatus") == "usable":
            fields["pdf"] = "complete"
        else:
            fields["pdf"] = "partial"
    narrative = fund.get("narrative") or report.get("narrative") or {}
    if isinstance(narrative, dict) and isinstance(narrative.get("text"), str) and narrative["text"].strip() and public_url(narrative.get("sourceUrl")):
        fields["strategy"] = "complete" if (narrative.get("periodStart") == quarter_start(phase["targetPeriodEnd"])
                                                and narrative.get("periodEnd") == phase["targetPeriodEnd"]) else "wrong-period"
    if fields["strategy"] != "complete":
        excerpts = report.get("strategyExcerpts")
        if isinstance(excerpts, list) and any(isinstance(item, dict) and isinstance(item.get("text"), str)
                and item["text"].strip() and public_url(item.get("sourceUrl"))
                and item.get("periodStart") == quarter_start(phase["targetPeriodEnd"])
                and item.get("periodEnd") == phase["targetPeriodEnd"] for item in excerpts):
            fields["strategy"] = "complete"
    nav = report.get("navYuan")
    if isinstance(nav, (float, int)) and not isinstance(nav, bool) and 0 < nav < float("inf"):
        fields["nav"] = "complete"
    baseline_exemption = {"reason": "ESTABLISHED_AFTER_BASELINE", "date": fund.get("inceptionDate"),
                          "sourceUrl": fund.get("inceptionSourceUrl"), "fundCode": fund["code"],
                          "baselinePeriodEnd": phase["baselinePeriodEnd"], "periodEnd": phase["targetPeriodEnd"]}
    if fund.get("status") == "no-equities":
        fields.update({"baseline": "not-applicable", "prices": "not-applicable"})
    else:
        baseline = fund.get("previousReport")
        if (isinstance(baseline, dict) and baseline.get("periodEnd") == phase["baselinePeriodEnd"]
                and public_url(baseline.get("sourceUrl")) and date(baseline.get("publishedAt"))
                and date(baseline["publishedAt"]) >= date(baseline["periodEnd"])
                and date(baseline["publishedAt"]) <= observed.date()
                and (baseline.get("holdings") or baseline.get("noEquitiesConfirmed") is True)):
            fields["baseline"] = "complete"
        elif valid_baseline_exemption(baseline_exemption, now):
            fields["baseline"] = "not-applicable"
        coverage = price_coverage(fund)
        if valid_price_coverage(coverage) and coverage["holdingCount"] > 0:
            fields["prices"] = "complete" if coverage["priceAvailable"] == coverage["holdingCount"] else "partial" if coverage["priceAvailable"] else "missing"
    result = {"fields": fields,
              "pdfRetryable": not report.get("parseMethod") and report.get("parseErrorCode") in (None, *RECOVERABLE_ERRORS)}
    if fields["baseline"] == "not-applicable" and valid_baseline_exemption(baseline_exemption, now):
        result["baselineExemption"] = baseline_exemption
    return result


def quality_eligible(states):
    return [code for code, state in states.items() if state["status"] in SUCCESS
            and not state.get("deferredUntil")
            and state["attribution"] != "portfolio-evidence" and state.get("representativeCode", code) == code
            and state["quality"]["status"] == "pending" and state["quality"]["attempts"] < QUALITY_MAX_ATTEMPTS]


def valid_quality(value, phase=None):
    valid = (isinstance(value, dict) and isinstance(value.get("fields"), dict)
            and set(value["fields"]) == set(QUALITY_FIELDS)
            and all(item in QUALITY_VALUES for item in value["fields"].values()))
    if valid and value.get("baselineExemption") is not None:
        exemption = value["baselineExemption"]
        valid = isinstance(exemption, dict) and (phase is None or (
            exemption.get("periodEnd") == phase["targetPeriodEnd"]
            and exemption.get("baselinePeriodEnd") == phase["baselinePeriodEnd"]))
    return valid


def hydrate_quality(states, retained, phase, now):
    """Recompute quality from the merger's effective observations on every pass."""
    for fund in retained or []:
        quality = report_quality(fund, phase, now)
        if quality is None:
            continue
        coverage = price_coverage(fund)
        codes = [fund["code"]]
        classes = fund.get("shareClassCodes")
        canonical = fund.get("sourcePortfolioCode")
        if (isinstance(classes, list) and canonical in classes and fund["code"] in classes
                and is_code(canonical) and public_url(fund.get("portfolioSourceUrl"))):
            codes = [code for code in classes if is_code(code)]
        for code in codes:
            state = states.get(code)
            if not state:
                continue
            # Direct records can remain unmapped. Alias expansion still needs
            # one proven owner and may not bridge unknown catalogue mappings.
            if code != fund["code"] and (not state["companyCode"] or state["companyCode"] != fund.get("companyId")):
                continue
            state["quality"] = normalize_quality({**state["quality"], **quality}, state["status"], now)
            if state["status"] == "complete" and valid_price_coverage(coverage):
                apply_coverage(state, coverage)


def load_retained_funds(data_root):
    """Read only manifest-referenced company files within this quarter root."""
    if not data_root:
        return
    root = Path(data_root)
    if root.is_symlink():
        raise TraversalError("READ_FAILED")
    if (root / "companies").is_symlink():
        raise TraversalError("READ_FAILED")
    manifest = read_json(root / "latest.json", optional=True)
    if manifest is None:
        return
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise TraversalError("INVALID_PROGRESS")
    funds = manifest.get("funds") or []
    files = manifest.get("companyFiles") or []
    if not isinstance(files, list) or not isinstance(funds, list) or len(files) > 10000 or len(funds) > 150000:
        raise TraversalError("INVALID_PROGRESS")
    count = len(funds)
    yield from funds
    seen = set()
    for item in files:
        value = item.get("path") if isinstance(item, dict) else None
        match = re.fullmatch(r"\./data/companies/([A-Za-z0-9_-]{1,64})\.json", value or "")
        if not match or match[1] in seen:
            raise TraversalError("INVALID_PROGRESS")
        seen.add(match[1])
        document = read_json(root / "companies" / (match[1] + ".json"))
        if not isinstance(document, dict) or document.get("schemaVersion") != 1 or not isinstance(document.get("funds"), list):
            raise TraversalError("INVALID_PROGRESS")
        count += len(document["funds"])
        if count > 150000:
            raise TraversalError("INVALID_PROGRESS")
        yield from document["funds"]


def no_equities_text(text):
    if not isinstance(text, str):
        return False
    excerpt = re.sub(r"\s+", "", text[:600])
    return not re.search(r"并非未|不是未|不能确认|尚未确认|无法确认", excerpt) and bool(
        re.search(r"(?:未持有|未投资于?)股票(?:资产|投资|组合)?(?:[。；，,.;：:]|$)", excerpt))


def no_equities_content(evidence):
    """Require the quoted stock section and both same-page zero asset rows."""
    if not isinstance(evidence, dict):
        return False
    kind = evidence.get("evidenceType")
    if kind is None:
        return no_equities_text(evidence.get("text"))
    if kind != "empty-stock-detail-section":
        return False
    text, assets = evidence.get("text"), evidence.get("assetAllocationText")
    if not isinstance(text, str) or not isinstance(assets, str) or len(text) > 600 or len(assets) > 600:
        return False
    section = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"(?:[0-9]+\.){1,2}[0-9]+期末按公允价值占基金资产净值比例大小排序的所有股票投资明细(?:注[:：])?无[。.]?", section):
        return False
    lines = [line.strip() for line in assets.splitlines() if line.strip()]
    if len(lines) != 2:
        return False
    zero = r"(?:-|0(?:\.0+)?)"
    equity = re.fullmatch(rf"(?:[1-9][0-9]{{0,2}}\s+)?权益投资\s+{zero}\s+{zero}", lines[0])
    stocks = re.fullmatch(rf"其中\s*[:：]?\s*股票\s+{zero}\s+{zero}", lines[1])
    return bool(equity and stocks)


def evidence_page(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def valid_not_applicable(evidence, code, phase, now):
    if not isinstance(evidence, dict) or evidence.get("reason") != "ESTABLISHED_AFTER_PERIOD":
        return False
    established = date(evidence.get("date"))
    return (evidence.get("fundCode") == code and evidence.get("periodEnd") == phase["targetPeriodEnd"]
            and established is not None and date(phase["targetPeriodEnd"]) < established <= now.date()
            and public_url(evidence.get("sourceUrl"))
            and urlparse(evidence["sourceUrl"]).path.endswith(f"jbgk_{code}.html"))


def plan_batches(catalog, previous, config, shards=4, batch_size=25, now=None, retained=()):
    current = utc_now(now)
    phase = phase_for(config)
    if not isinstance(shards, int) or not isinstance(batch_size, int) or not 1 <= shards <= 32 or not 1 <= batch_size <= 100:
        raise TraversalError("INVALID_ARGUMENTS")
    companies, records, states = base_states(catalog, previous, phase, current, config)
    hydrate_quality(states, retained, phase, current)
    apply_retry_policy(states, config, current)
    selected = []
    enrich_codes = set(enrichment_eligible(states))
    quality_codes = set(quality_eligible(states)) if quality_enabled(config) else set()
    pools = (eligible(states), quality_codes, enrich_codes) if quality_enabled(config) else (enrich_codes, eligible(states))
    for pool in pools:
        queues = {}
        for code in sorted(pool):
            if code not in selected:
                queues.setdefault(states[code]["companyCode"] or "~unmapped", deque()).append(code)
        order = deque(sorted(queues))
        while order and len(selected) < shards * batch_size:
            company = order.popleft()
            selected.append(queues[company].popleft())
            if queues[company]:
                order.append(company)
    batches, matrix = {}, []
    for index, start in enumerate(range(0, len(selected), batch_size)):
        batch = {key: value for key, value in config.items() if key not in {"funds", "traversal"}}
        batch_codes = selected[start:start + batch_size]
        batch["funds"] = [{**records[code], **({"priceTargets": states[code]["enrichment"]["missingStocks"]}
                          if code in enrich_codes and code not in quality_codes and states[code]["enrichment"]["missingStocks"] else {})} for code in batch_codes]
        batch["traversal"] = {"phase": phase, "batchId": str(index), "plannedAt": current.isoformat(),
                              "enrichmentCodes": [code for code in batch_codes if code in enrich_codes and code not in quality_codes],
                              "qualityCodes": [code for code in batch_codes if code in quality_codes]}
        filename = f"batch-{index}.json"
        batches[filename] = batch
        matrix.append({"id": str(index), "config": filename})
    passes = previous.get("directoryPasses", 0) if isinstance(previous, dict) and previous.get("phase") == phase else 0
    passes = passes if isinstance(passes, int) else 0
    continuing = bool(selected)
    return {"batches": batches, "matrix": matrix,
            "meta": {"hasWork": bool(selected), "continueRun": continuing, "selectedCount": len(selected),
                     "enrichmentCount": sum(code in enrich_codes and code not in quality_codes for code in selected),
                     "qualityCount": sum(code in quality_codes for code in selected), "phase": phase}}


def report_evidence(fund, phase, now, planned_at=None):
    if not isinstance(fund, dict) or fund.get("dataStatus") != "fresh" or not is_code(fund.get("code")):
        return None
    observed = instant(fund.get("retrievedAt"))
    report = fund.get("selectedReport")
    if not observed or observed > now + dt.timedelta(minutes=5) or planned_at and observed < planned_at:
        return None
    if not isinstance(report, dict) or report.get("periodEnd") != phase["targetPeriodEnd"] or not public_url(report.get("sourceUrl")):
        return None
    if report.get("fundCode") not in (None, fund["code"]) or report.get("sourceFundCode") not in (None, fund["code"]):
        return None
    published = date(report.get("publishedAt"))
    if not published or published < date(report["periodEnd"]) or published > now.date() or published > observed.date():
        return None
    result = {"sourceUrl": report["sourceUrl"], "periodEnd": report["periodEnd"],
              "publishedAt": report["publishedAt"], "observedAt": fund["retrievedAt"]}
    if fund.get("status") == "no-equities":
        evidence = fund.get("noEquitiesEvidence") or report.get("noEquitiesEvidence") or {}
        text = evidence.get("text", "") if isinstance(evidence, dict) else ""
        if (not isinstance(evidence, dict) or evidence.get("confirmed") is not True or evidence.get("sourceUrl") != report["sourceUrl"]
                or evidence.get("periodEnd") != report["periodEnd"] or evidence.get("publishedAt") != report["publishedAt"]
                or not evidence_page(evidence.get("page")) or not no_equities_content(evidence)):
            return None
        result["noEquities"] = {"confirmed": True, "sourceUrl": evidence["sourceUrl"], "page": evidence["page"], "text": text[:600]}
        if evidence.get("evidenceType") == "empty-stock-detail-section":
            result["noEquities"].update({key: evidence[key] for key in ("evidenceType", "assetAllocationText", "periodEnd", "publishedAt")})
        return "no-equities", result
    if fund.get("status") == "ok" and isinstance(report.get("holdings"), list) and report["holdings"] and fund.get("rows"):
        return "complete", result
    return None


def build_status(config, snapshot=None, now=None):
    current = utc_now(now)
    phase = phase_for(config)
    planned = instant(config.get("traversal", {}).get("plannedAt"))
    wanted = {item["code"]: item for item in config.get("funds", []) if isinstance(item, dict) and is_code(item.get("code"))}
    if not wanted:
        raise TraversalError("INVALID_CONFIG")
    observed, aliases, results, qualities, exemptions = {}, [], {}, {}, {}
    enrich_codes = set(config.get("traversal", {}).get("enrichmentCodes", []))
    quality_codes = set(config.get("traversal", {}).get("qualityCodes", []))
    snapshot_ok = isinstance(snapshot, dict) and snapshot.get("schemaVersion") == 1 and snapshot.get("sourceMode") == "actions"
    if snapshot_ok and snapshot.get("targetPeriodEnd") not in (None, phase["targetPeriodEnd"]):
        snapshot_ok = False
    if snapshot_ok:
        for item in snapshot.get("batchResults", []):
            if isinstance(item, dict) and item.get("code") in wanted:
                results[item["code"]] = item
        for fund in snapshot.get("funds", []):
            if (isinstance(fund, dict) and fund.get("dataStatus") == "fresh"
                    and instant(fund.get("retrievedAt")) and (not planned or instant(fund["retrievedAt"]) >= planned)
                    and instant(fund["retrievedAt"]) <= current + dt.timedelta(minutes=5)
                    and valid_not_applicable(fund.get("notApplicableEvidence"), fund.get("code"), phase, current)):
                exemptions[fund["code"]] = fund["notApplicableEvidence"]
            quality = report_quality(fund, phase, current)
            if quality is not None:
                qualities[fund["code"]] = quality
            proof = report_evidence(fund, phase, current, planned)
            if not proof:
                continue
            status, evidence = proof
            coverage = price_coverage(fund)
            observed[fund["code"]] = (status, evidence, coverage)
            classes = fund.get("shareClassCodes", [])
            canonical = fund.get("sourcePortfolioCode")
            source = fund.get("portfolioSourceUrl")
            if is_code(canonical) and isinstance(classes, list) and canonical in classes and fund["code"] in classes and public_url(source):
                codes = sorted({code for code in classes if is_code(code)})
                aliases.append({"sourcePortfolioCode": canonical, "shareClassCodes": codes, "sourceUrl": source,
                                "status": status, "reportEvidence": evidence, "priceCoverage": coverage})
                for code in codes:
                    observed.setdefault(code, (status, evidence, coverage))
                    if quality is not None:
                        qualities.setdefault(code, quality)
    rows = []
    for code, item in wanted.items():
        declared = results.get(code, {})
        proof = observed.get(code)
        if valid_deferred(declared):
            row = {"code": code, "companyCode": item.get("companyCode") or item.get("companyId"),
                   "status": "deferred-budget", "attempted": False, "errorCode": "BATCH_DEADLINE_REACHED"}
        elif code in exemptions and declared.get("status") == "not-applicable":
            row = {"code": code, "companyCode": item.get("companyCode") or item.get("companyId"),
                   "status": "not-applicable", "errorCode": None,
                   "notApplicableEvidence": {key: exemptions[code][key] for key in ("reason", "date", "sourceUrl", "periodEnd", "fundCode")}}
        elif proof and declared.get("status") in SUCCESS:
            status, evidence, coverage = proof
            row = {"code": code, "companyCode": item.get("companyCode") or item.get("companyId"),
                   "status": status, "errorCode": None, "reportEvidence": evidence, "priceCoverage": coverage}
        else:
            error = declared.get("errorCode")
            if snapshot_ok and error in {"NO_VALID_REPORTS", "FUND_FAILED"}:
                diagnostics = [entry.get("errorCode") for entry in snapshot.get("errors", [])
                               if isinstance(entry, dict) and entry.get("code") == code
                               and entry.get("stage") in {"report", "quarterly-report", "no-equities-report", "holdings", "build"}
                               and entry.get("errorCode") in PARSER_ERRORS]
                error = diagnostics[0] if diagnostics else error
            row = {"code": code, "companyCode": item.get("companyCode") or item.get("companyId"), "status": "failed",
                   "errorCode": error if error in ERROR_CODES else "FUND_FAILED" if snapshot_ok else "BATCH_FAILED"}
        row["enrichmentAttempt"] = code in enrich_codes
        row["qualityAttempt"] = code in quality_codes
        if code in qualities:
            row["reportQuality"] = qualities[code]
        rows.append(row)
    return {"schemaVersion": 1, "phase": phase, "attemptedAt": current.isoformat(), "funds": rows, "portfolioAliases": aliases}


def valid_deferred(item):
    return (isinstance(item, dict) and item.get("status") == "deferred-budget" and item.get("attempted") is False
            and item.get("errorCode") == "BATCH_DEADLINE_REACHED")


def valid_event_evidence(evidence, status, phase, attempted_at):
    if not isinstance(evidence, dict) or not public_url(evidence.get("sourceUrl")) or evidence.get("periodEnd") != phase["targetPeriodEnd"]:
        return False
    published, observed = date(evidence.get("publishedAt")), instant(evidence.get("observedAt"))
    if not published or published < date(phase["targetPeriodEnd"]) or not observed or published > observed.date() or observed > attempted_at:
        return False
    if status == "no-equities":
        noeq = evidence.get("noEquities") or {}
        if not isinstance(noeq, dict) or noeq.get("confirmed") is not True or noeq.get("sourceUrl") != evidence["sourceUrl"] or not evidence_page(noeq.get("page")):
            return False
        if noeq.get("evidenceType") == "empty-stock-detail-section" and any(noeq.get(key) != evidence[key] for key in ("periodEnd", "publishedAt")):
            return False
        return no_equities_content(noeq)
    return status == "complete"


def valid_status(document, config, now=None):
    """Validate a saved attempt before reusing its event time during recovery."""
    current = utc_now(now)
    phase = phase_for(config)
    if not isinstance(document, dict) or document.get("schemaVersion") != 1 or document.get("phase") != phase:
        return False
    attempted = instant(document.get("attemptedAt"))
    planned = instant(config.get("traversal", {}).get("plannedAt"))
    if not attempted or attempted > current + dt.timedelta(minutes=5) or planned and attempted < planned:
        return False
    wanted = {item["code"]: item.get("companyCode") or item.get("companyId") for item in config.get("funds", [])
              if isinstance(item, dict) and is_code(item.get("code"))}
    rows = document.get("funds")
    if not wanted or not isinstance(rows, list) or len(rows) != len(wanted):
        return False
    seen = set()
    enrich_codes = set(config.get("traversal", {}).get("enrichmentCodes", []))
    quality_codes = set(config.get("traversal", {}).get("qualityCodes", []))
    for item in rows:
        if not isinstance(item, dict) or item.get("code") not in wanted or item["code"] in seen:
            return False
        seen.add(item["code"])
        if item.get("companyCode") != wanted[item["code"]] or item.get("status") not in TERMINAL | {"failed", "deferred-budget"}:
            return False
        if bool(item.get("enrichmentAttempt")) != (item["code"] in enrich_codes):
            return False
        if bool(item.get("qualityAttempt")) != (item["code"] in quality_codes):
            return False
        if item.get("reportQuality") is not None and not valid_quality(item["reportQuality"], phase):
            return False
        if item.get("priceCoverage") is not None and not valid_price_coverage(item["priceCoverage"]):
            return False
        if item["status"] == "deferred-budget":
            if not valid_deferred(item):
                return False
        elif item.get("attempted") is False:
            return False
        elif item["status"] == "not-applicable":
            if not valid_not_applicable(item.get("notApplicableEvidence"), item["code"], phase, attempted):
                return False
        elif item["status"] == "failed":
            if item.get("errorCode") not in ERROR_CODES:
                return False
        elif not valid_event_evidence(item.get("reportEvidence"), item["status"], phase, attempted):
            return False
    proofs = document.get("portfolioAliases", [])
    if not isinstance(proofs, list):
        return False
    for proof in proofs:
        if not isinstance(proof, dict) or not is_code(proof.get("sourcePortfolioCode")) or not public_url(proof.get("sourceUrl")):
            return False
        codes = proof.get("shareClassCodes")
        if (not isinstance(codes, list) or not all(is_code(code) for code in codes)
                or proof["sourcePortfolioCode"] not in codes or not seen.intersection(codes)
                or not valid_event_evidence(proof.get("reportEvidence"), proof.get("status"), phase, attempted)):
            return False
    return True


def reconcile_observations(statuses, receipt, config, now=None):
    """Use only the merger's retained public observations when advancing coverage."""
    current = utc_now(now)
    generated = instant(receipt.get("generatedAt")) if isinstance(receipt, dict) else None
    if (not isinstance(receipt, dict) or receipt.get("schemaVersion") != 1 or receipt.get("phase") != phase_for(config)
            or not generated or generated > current + dt.timedelta(minutes=5) or not isinstance(receipt.get("funds"), list)):
        raise TraversalError("INVALID_PROGRESS")
    observations = {}
    for item in receipt["funds"]:
        if valid_deferred(item) and is_code(item.get("code")) and item.get("representativeCode") in (None, item["code"]):
            observations[item["code"]] = item
            continue
        if (isinstance(item, dict) and is_code(item.get("code")) and item.get("status") == "not-applicable"
                and item.get("representativeCode") == item["code"]
                and instant(item.get("observedAt")) and instant(item["observedAt"]) <= generated
                and valid_not_applicable(item.get("notApplicableEvidence"), item["code"], phase_for(config), generated)):
            observations[item["code"]] = item
            continue
        if (not isinstance(item, dict) or not is_code(item.get("code")) or not is_code(item.get("representativeCode"))
                or not valid_event_evidence(item.get("reportEvidence"), item.get("status"), phase_for(config), generated)
                or not valid_price_coverage(item.get("priceCoverage"))):
            continue
        observations[item["code"]] = item
    reconciled = []
    for document in statuses:
        if not isinstance(document, dict):
            continue
        result = {**document, "funds": [], "portfolioAliases": []}
        proofs = {}
        for original in document.get("funds", []):
            if not isinstance(original, dict):
                continue
            row = dict(original)
            observation = observations.get(row.get("code"))
            if row.get("status") == "deferred-budget":
                owner = observation.get("companyCode") if observation else None
                if owner == "unknown":
                    owner = None
                if not valid_deferred(row) or not valid_deferred(observation) or owner != row.get("companyCode"):
                    row.update({"status": "failed", "attempted": True, "errorCode": "BATCH_FAILED"})
            elif row.get("status") in TERMINAL:
                observed_company = observation.get("companyCode") if observation else None
                if observed_company == "unknown":
                    observed_company = None
                exempt = row.get("status") == "not-applicable"
                if (not observation or observed_company != row.get("companyCode")
                        or exempt != (observation.get("status") == "not-applicable")):
                    row.update({"status": "failed", "errorCode": "FUND_FAILED"})
                    row.pop("priceCoverage", None)
                    row.pop("reportEvidence", None)
                    row.pop("reportQuality", None)
                    row.pop("notApplicableEvidence", None)
                elif exempt:
                    row["notApplicableEvidence"] = observation["notApplicableEvidence"]
                    row["errorCode"] = None
                else:
                    row.update({key: observation[key] for key in ("status", "reportEvidence", "priceCoverage", "representativeCode")})
                    row["errorCode"] = None
                    aliases = observation.get("shareClassCodes", [])
                    canonical = observation.get("sourcePortfolioCode")
                    source = observation.get("portfolioSourceUrl")
                    if (is_code(canonical) and isinstance(aliases, list) and all(is_code(code) for code in aliases)
                            and canonical in aliases and row["code"] in aliases and public_url(source)):
                        proofs[canonical] = {"sourcePortfolioCode": canonical, "shareClassCodes": aliases,
                                             "sourceUrl": source, "status": observation["status"],
                                             "reportEvidence": observation["reportEvidence"],
                                             "priceCoverage": observation["priceCoverage"],
                                             "representativeCode": observation["representativeCode"]}
            result["funds"].append(row)
        result["portfolioAliases"] = list(proofs.values())
        reconciled.append(result)
    return reconciled


def merge_progress(catalog, previous, config, statuses=(), now=None, retained=()):
    current = utc_now(now)
    phase = phase_for(config)
    companies, records, states = base_states(catalog, previous, phase, current, config)
    matching_previous = isinstance(previous, dict) and previous.get("schemaVersion") == 1 and previous.get("phase") == phase
    events = []
    for document in statuses:
        if not isinstance(document, dict) or document.get("schemaVersion") != 1 or document.get("phase") != phase:
            continue
        attempted = instant(document.get("attemptedAt"))
        if not attempted or attempted > current + dt.timedelta(minutes=5):
            continue
        events.append((attempted, document))
    for attempted, document in sorted(events, key=lambda event: event[0]):
        accepted_success = set()
        for item in document.get("funds", []):
            if not isinstance(item, dict) or item.get("code") not in states:
                continue
            state = states[item["code"]]
            if item.get("companyCode") != state["companyCode"]:
                continue
            status = item.get("status")
            if status == "deferred-budget":
                prior_deferred = instant(state.get("lastDeferredAt"))
                prior_attempt = instant(state.get("lastAttemptAt"))
                if (valid_deferred(item) and (not prior_deferred or attempted > prior_deferred)
                        and (not prior_attempt or attempted > prior_attempt)):
                    state["lastDeferredAt"] = document["attemptedAt"]
                    until = midnight(attempted.date() + dt.timedelta(days=1))
                    state["deferredUntil"] = until if instant(until) > current else None
                continue
            if status not in TERMINAL | {"failed"}:
                continue
            if item.get("attempted") is False:
                continue
            if status == "not-applicable":
                if not valid_not_applicable(item.get("notApplicableEvidence"), item["code"], phase, attempted):
                    continue
                prior_attempt = instant(state["lastAttemptAt"])
                if state["status"] in SUCCESS or prior_attempt and attempted <= prior_attempt:
                    continue
                state.update({"status": status, "lastAttemptAt": document["attemptedAt"], "attempted": True,
                              "totalAttempts": state["totalAttempts"] + 1, "deferredUntil": None,
                              "attribution": "direct", "errorCode": None,
                              "notApplicableEvidence": {key: item["notApplicableEvidence"][key] for key in ("reason", "date", "sourceUrl", "periodEnd", "fundCode")}})
                state["quality"] = normalize_quality(None, status, current)
                state["enrichment"] = normalize_enrichment(None, status, current)
                continue
            if state["status"] == "not-applicable":
                continue
            if status in SUCCESS and not valid_event_evidence(item.get("reportEvidence"), status, phase, attempted):
                continue
            representative = item.get("representativeCode")
            if representative in states and states[representative]["companyCode"] == state["companyCode"]:
                state["representativeCode"] = representative
            prior_attempt = instant(state["lastAttemptAt"])
            if state["status"] in SUCCESS and item.get("qualityAttempt") is True:
                quality = state["quality"]
                prior_quality = instant(quality["lastAttemptAt"]) or prior_attempt
                if prior_quality and attempted <= prior_quality:
                    continue
                quality["attempts"] = min(QUALITY_MAX_ATTEMPTS, quality["attempts"] + 1)
                quality["totalAttempts"] += 1
                state["totalAttempts"] += 1
                state["deferredUntil"] = None
                quality["lastAttemptAt"] = document["attemptedAt"]
                state["lastAttemptAt"] = document["attemptedAt"]
                state["attempted"] = True
                if valid_quality(item.get("reportQuality"), phase):
                    quality.update(item["reportQuality"])
                state["quality"] = normalize_quality(quality, state["status"], current)
                if status in SUCCESS:
                    if status == "complete":
                        apply_coverage(state, item.get("priceCoverage"))
                    accepted_success.add(item["code"])
                # Failed PDF enrichment must never erase a valid holding report.
                continue
            if state["status"] == "complete" and item.get("enrichmentAttempt") is True:
                extra = state["enrichment"]
                prior_extra = instant(extra["lastAttemptAt"]) or prior_attempt
                if extra["status"] == "complete" or prior_extra and attempted <= prior_extra:
                    continue
                extra["attempts"] = min(3, extra["attempts"] + 1)
                extra["totalAttempts"] += 1
                state["totalAttempts"] += 1
                state["deferredUntil"] = None
                extra["lastAttemptAt"] = document["attemptedAt"]
                state["lastAttemptAt"] = document["attemptedAt"]
                state["attempted"] = True
                extra["status"] = "exhausted" if extra["attempts"] >= 3 else "pending"
                extra["errorCode"] = "ENRICHMENT_FAILED"
                if status == "complete":
                    apply_coverage(state, item.get("priceCoverage"))
                    accepted_success.add(item["code"])
                # Report availability is never downgraded by a price retry.
                continue
            if state["status"] in SUCCESS or prior_attempt and attempted <= prior_attempt:
                if state["status"] in SUCCESS and status in SUCCESS:
                    accepted_success.add(item["code"])
                continue
            state.update({"lastAttemptAt": document["attemptedAt"], "attempted": True, "attribution": "direct",
                          "totalAttempts": state["totalAttempts"] + 1, "deferredUntil": None})
            if valid_quality(item.get("reportQuality"), phase):
                state["quality"] = normalize_quality({**state["quality"], **item["reportQuality"]}, status, current)
            if status == "failed":
                state.update({"status": "failed", "attempts": min(2, state["attempts"] + 1),
                              "errorCode": item.get("errorCode") if item.get("errorCode") in ERROR_CODES else "FUND_FAILED"})
            else:
                state.update({"status": status, "errorCode": None})
                state["enrichment"] = normalize_enrichment(state["enrichment"], status, current)
                state["quality"] = normalize_quality(state["quality"], status, current)
                if status == "complete":
                    apply_coverage(state, item.get("priceCoverage"))
                accepted_success.add(item["code"])
        for proof in document.get("portfolioAliases", []):
            if not isinstance(proof, dict) or not is_code(proof.get("sourcePortfolioCode")) or not public_url(proof.get("sourceUrl")):
                continue
            codes = proof.get("shareClassCodes")
            status = proof.get("status")
            if not isinstance(codes, list) or proof["sourcePortfolioCode"] not in codes or not accepted_success.intersection(codes):
                continue
            if not valid_event_evidence(proof.get("reportEvidence"), status, phase, attempted):
                continue
            owners = {states[code]["companyCode"] for code in codes if code in states}
            if len(owners) != 1 or None in owners:
                continue
            representative = proof.get("representativeCode")
            if representative not in codes or representative not in states:
                representative = proof["sourcePortfolioCode"] if proof["sourcePortfolioCode"] in accepted_success else min(accepted_success.intersection(codes))
            portfolio_attempts = max((states[code]["enrichment"]["attempts"] for code in codes if code in states), default=0)
            quality_attempts = max((states[code]["quality"]["attempts"] for code in codes if code in states), default=0)
            representative_quality = states[representative]["quality"]
            for code in codes:
                if code in states and states[code]["status"] == "not-applicable":
                    continue
                if code in states:
                    states[code]["representativeCode"] = representative
                    states[code]["enrichment"]["attempts"] = portfolio_attempts
                    states[code]["quality"] = normalize_quality({**representative_quality, "attempts": quality_attempts}, status, current)
                if code in states and states[code]["status"] not in SUCCESS:
                    states[code].update({"status": status, "lastAttemptAt": document["attemptedAt"], "errorCode": None,
                                         "attribution": "portfolio-evidence"})
                    states[code]["enrichment"] = normalize_enrichment(states[code]["enrichment"], status, current)
                if code in states and states[code]["status"] == "complete" and status == "complete":
                    apply_coverage(states[code], proof.get("priceCoverage"))
    hydrate_quality(states, retained, phase, current)
    apply_retry_policy(states, config, current)
    passes = previous.get("directoryPasses", 0) if matching_previous else 0
    passes = min(12, max(0, passes)) if isinstance(passes, int) else 0
    last_directory = previous.get("lastDirectoryObservedAt") if matching_previous else None
    directory_observed = catalog.get("retrievedAt")
    if not instant(directory_observed) or instant(directory_observed) > current + dt.timedelta(minutes=5):
        directory_observed = current.isoformat()
    if directory_incomplete(companies, records) and directory_observed != last_directory:
        passes = min(12, passes + 1)
    company_rows = []
    for code, company in sorted(companies.items()):
        members = [state for state in states.values() if state["companyCode"] == code]
        complete = sum(item["status"] == "complete" for item in members)
        noeq = sum(item["status"] == "no-equities" for item in members)
        failed = sum(item["status"] == "failed" for item in members)
        not_applicable = sum(item["status"] == "not-applicable" for item in members)
        total = company["fundCount"] if company["directoryStatus"] == "complete" else None
        if total is not None:
            total = max(total, len(members))
        pending = max(0, total - complete - noeq - failed - not_applicable) if total is not None else sum(item["status"] == "pending" for item in members)
        last = max((item["lastAttemptAt"] for item in members if item["lastAttemptAt"]), default=None)
        company_rows.append({"code": code, "name": company["name"], "totalFunds": total,
                             "processedFunds": complete + noeq, "withHoldings": complete, "withoutEquities": noeq,
                             "failed": failed, "pending": pending, "notApplicable": not_applicable, "lastAttemptAt": last,
                             "deferredFunds": sum(bool(item.get("deferredUntil")) for item in members),
                             "enrichmentPending": sum(item["attribution"] != "portfolio-evidence" and item["representativeCode"] == item["code"] and item["enrichment"]["status"] == "pending" for item in members),
                             "enrichmentExhausted": sum(item["attribution"] != "portfolio-evidence" and item["representativeCode"] == item["code"] and item["enrichment"]["status"] == "exhausted" for item in members),
                             "researchComplete": sum(item["status"] in SUCCESS and item["quality"]["status"] == "complete" for item in members),
                             "directoryStatus": company["directoryStatus"]})
    all_states = sorted(states.values(), key=lambda state: state["code"])
    processed = sum(state["status"] in SUCCESS for state in all_states)
    failed = sum(state["status"] == "failed" for state in all_states)
    not_applicable = sum(state["status"] == "not-applicable" for state in all_states)
    continuing = bool(eligible(states) or enrichment_eligible(states)
                      or quality_enabled(config) and quality_eligible(states))
    enrichment_states = [state["enrichment"] for state in all_states if state["status"] == "complete" and state["attribution"] != "portfolio-evidence" and state["representativeCode"] == state["code"]]
    research_complete = sum(state["status"] in SUCCESS and state["quality"]["status"] == "complete" for state in all_states)
    research_pending = sum(state["status"] in SUCCESS and state["quality"]["status"] == "pending" for state in all_states)
    research_exhausted = sum(state["status"] in SUCCESS and state["quality"]["status"] == "exhausted" for state in all_states)
    applicable = len(records) - not_applicable
    completed = (research_complete == applicable if quality_enabled(config) else processed == applicable) and not directory_incomplete(companies, records)
    directory_retry = midnight(current.date() + dt.timedelta(days=1)) if directory_incomplete(companies, records) and passes < 12 else None
    next_retry = min((value for value in [directory_retry, *(state.get("nextRetryAt") for state in all_states)] if value), default=None)
    return {"schemaVersion": 1, "phase": phase, "updatedAt": current.isoformat(), "collectorVersion": collector_version(config),
            "totalCompanies": len(companies), "totalFunds": len(records), "processedFunds": processed,
            "attemptedFunds": sum(state["attempted"] for state in all_states),
            "completedCompanies": sum(company["directoryStatus"] == "complete" and company["totalFunds"] == company["processedFunds"] + company["notApplicable"] for company in company_rows),
            "pendingFunds": len(records) - processed - failed - not_applicable, "failedFunds": failed,
            "notApplicableFunds": not_applicable,
            "deferredFunds": sum(bool(state.get("deferredUntil")) for state in all_states),
            "nextRetryAt": next_retry, "directoryNextRetryAt": directory_retry,
            "retryableFailedFunds": sum(state["status"] == "failed" and state["attempts"] < 2 for state in all_states),
            "unmappedFunds": sum(state["companyCode"] is None for state in all_states),
            "completionStatus": "complete" if completed else "incomplete-running" if continuing else "incomplete-exhausted",
            "research": {"enabled": quality_enabled(config), "applicableFunds": applicable, "completeFunds": research_complete,
                         "incompleteFunds": applicable - research_complete, "pendingFunds": research_pending,
                         "exhaustedFunds": research_exhausted, "maxAttempts": QUALITY_MAX_ATTEMPTS,
                         "fieldCoverage": {key: sum(state["status"] != "not-applicable" and state["quality"]["fields"][key] in QUALITY_ACCEPTED for state in all_states)
                                           for key in QUALITY_FIELDS}},
            "enrichment": {"pendingFunds": sum(item["status"] == "pending" for item in enrichment_states),
                           "completeFunds": sum(item["status"] == "complete" for item in enrichment_states),
                           "exhaustedFunds": sum(item["status"] == "exhausted" for item in enrichment_states),
                           "attemptedFunds": sum(item["attempts"] > 0 for item in enrichment_states),
                           "attempts": sum(item["attempts"] for item in enrichment_states), "maxAttempts": 3},
            "companies": company_rows,
            "funds": [{**state, "enrichment": compact_enrichment(state["enrichment"])} for state in all_states],
            "directoryPasses": passes,
            "lastDirectoryObservedAt": directory_observed, "continueRun": continuing}


def read_json(path, optional=False):
    path = Path(path)
    if optional and not path.exists():
        return None
    try:
        # Coverage contains one small record per directory code; bounded company
        # snapshots retain their smaller limit instead of lifting every read.
        maximum = 96_000_000 if path.name == "progress.json" else 25_000_000
        if path.is_symlink() or path.stat().st_size > maximum:
            raise ValueError("bounded-read")
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise TraversalError("READ_FAILED") from None


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise TraversalError("WRITE_FAILED") from None


def main(argv=None):
    parser = SafeArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    for name, default in (("catalog", "web/data/catalog.json"), ("progress", "web/data/progress.json"),
                          ("config", "config/universe.json"), ("output", ".cache/plan")):
        plan.add_argument("--" + name, default=default)
    plan.add_argument("--shards", type=int, default=4)
    plan.add_argument("--batch-size", type=int, default=25)
    plan.add_argument("--data-root", help="Quarter directory containing the retained published snapshot.")
    status = commands.add_parser("status")
    status.add_argument("--config", required=True)
    status.add_argument("--snapshot", default="web/data/part.json")
    status.add_argument("--output", default="web/data/status.json")
    merge = commands.add_parser("merge")
    for name, default in (("parts", ".cache/parts"), ("catalog", "web/data/catalog.json"),
                          ("progress", "web/data/progress.json"), ("config", "config/universe.json")):
        merge.add_argument("--" + name, default=default)
    merge.add_argument("--data-root", help="Quarter directory containing the merger's effective snapshot.")
    args = parser.parse_args(argv)
    config = read_json(args.config)
    if args.command == "plan":
        result = plan_batches(read_json(args.catalog), read_json(args.progress, optional=True), config, args.shards, args.batch_size,
                              retained=load_retained_funds(args.data_root))
        output = Path(args.output)
        for filename, batch in result["batches"].items():
            atomic_json(output / filename, batch)
        atomic_json(output / "matrix.json", result["matrix"])
        atomic_json(output / "meta.json", result["meta"])
        print(json.dumps({"status": "planned", "batchCount": len(result["matrix"]), "selectedCount": result["meta"]["selectedCount"], "continueRun": result["meta"]["continueRun"]}))
    elif args.command == "status":
        try:
            snapshot = read_json(args.snapshot, optional=True)
        except TraversalError:
            snapshot = None
        result = build_status(config, snapshot)
        atomic_json(args.output, result)
        print(json.dumps({"status": "recorded", "fundCount": len(result["funds"]), "failedCount": sum(item["status"] == "failed" for item in result["funds"])}))
    else:
        statuses = []
        parts = Path(args.parts)
        for file in sorted(parts.glob("**/status.json")) if parts.exists() else []:
            if file.is_symlink() or len(file.relative_to(parts).parts) > 3:
                continue
            try:
                statuses.append(read_json(file))
            except TraversalError:
                continue
        statuses = reconcile_observations(statuses, read_json(parts / "merge-receipt.json"), config)
        result = merge_progress(read_json(args.catalog), read_json(args.progress, optional=True), config, statuses,
                                retained=load_retained_funds(args.data_root))
        atomic_json(args.progress, result)
        print(json.dumps({"status": "merged", "processedFunds": result["processedFunds"], "failedFunds": result["failedFunds"], "pendingFunds": result["pendingFunds"], "continueRun": result["continueRun"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "errorCode": error.code if isinstance(error, TraversalError) else "TRAVERSAL_FAILED"}))
        raise SystemExit(1) from None
