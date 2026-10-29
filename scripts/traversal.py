"""Bounded directory traversal, source-checked statuses, and resumable progress."""
from __future__ import annotations

import argparse
from collections import deque
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import urlparse, parse_qsl
from lifecycle import normalize_lifecycle_evidence
from report_managers import normalize_manager_table_coverage, manager_key
from strategy_sections import normalize_strategy_section_coverage
from price_coverage import CALENDAR, VERSION as PRICE_COVERAGE_VERSION, valid_usable_complete_price
from price_consistency import normalize_price_conflict
from price_range_reference import effective_price_range, unresolved_conflict, resolution_for, valid_resolution
from price_patches import valid_binding as valid_price_binding, valid_target as valid_price_target, status_for as price_patch_status
from source_dates import china_date

FUND_CODE = re.compile(r"\d{6}\Z")
COMPANY_CODE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
SUCCESS = {"complete", "no-equities"}
TERMINAL = SUCCESS | {"not-applicable"}
SIX_QUALITY_FIELDS = ("holdings", "pdf", "strategy", "nav", "baseline", "prices")
QUALITY_FIELDS = (*SIX_QUALITY_FIELDS, "managers")
QUALITY_VALUES = {"complete", "missing", "partial", "truncated", "wrong-period", "not-applicable"}
QUALITY_ACCEPTED = {"complete", "not-applicable"}
QUALITY_MAX_ATTEMPTS = 3
MANAGER_MAX_ATTEMPTS = 3
MANAGER_TABLE_COVERAGE_VERSION = 2
STRATEGY_SECTION_COVERAGE_VERSION = 1
STRATEGY_MAX_ATTEMPTS = 3
REPORT_LOCATOR_MAX_ATTEMPTS = 2
REPORT_LOCATOR_MAX_DIGESTS = 16
STRATEGY_CLAIMS_MAX_ATTEMPTS = 2
STRATEGY_CLAIMS_MAX_DIGESTS = 16
STRATEGY_CLAIMS_MAX_QUOTA = 20
MAX_STATUS_SNAPSHOT_BYTES = 25_000_000
ERROR_CODES = {
    "BATCH_FAILED", "FUND_FAILED", "NO_VALID_REPORTS", "TARGET_REPORT_MISSING", "FUND_NOT_COLLECTED",
    "NO_VALID_FRESH_FUNDS", "NO_REPORTS", "NO_DATED_HOLDINGS", "EMPTY_HOLDINGS", "OLDER_REPORT",
    "HOLDING_COVERAGE_REGRESSION", "REPORT_COVERAGE_REGRESSION", "CONTRADICTORY_NO_EQUITIES",
    "INVALID_REPORT", "INVALID_REPORT_SOURCE", "REPORT_IDENTITY_MISMATCH", "INVALID_NO_EQUITIES_EVIDENCE",
    "BATCH_DEADLINE_REACHED", "PRICE_BUDGET_REACHED",
    "PRICE_BINDING_INVALID", "ENRICHMENT_FAILED",
    "REPORT_LOCATOR_NOT_ATTEMPTED",
    "TIMEOUT", "NETWORK_ERROR", "HTTP_ERROR", "TLS_FAILED", "TARGET_REPORT_UNAVAILABLE",
    "PDF_PARSE_FAILED", "PDF_TEXT_EMPTY", "INVALID_PDF", "UNSUPPORTED_MARKET", "HOLDINGS_UNRESOLVED", "INVALID_FORMAT", "PARSE_FAILED",
}
PARSER_ERRORS = {"PDF_PARSE_FAILED", "PDF_TEXT_EMPTY", "INVALID_PDF", "UNSUPPORTED_MARKET", "HOLDINGS_UNRESOLVED", "INVALID_FORMAT", "PARSE_FAILED"}
RECOVERABLE_ERRORS = {"NO_VALID_REPORTS", "TARGET_REPORT_MISSING", "NO_REPORTS", "TARGET_REPORT_UNAVAILABLE",
                      "NO_DATED_HOLDINGS", "TIMEOUT", "NETWORK_ERROR", "HTTP_ERROR", "TLS_FAILED",
                      "BATCH_FAILED", "FUND_NOT_COLLECTED", "BATCH_DEADLINE_REACHED"}
STATUS_SNAPSHOT_ERRORS = {"SNAPSHOT_TOO_LARGE", "SNAPSHOT_INVALID_JSON", "SNAPSHOT_INVALID_SCHEMA",
                          "SNAPSHOT_PHASE_MISMATCH", "SNAPSHOT_READ_FAILED"}
COMMAND_ERRORS = {"INVALID_CONFIG", "INVALID_CATALOG", "INVALID_PROGRESS", "INVALID_ARGUMENTS",
                  "READ_FAILED", "WRITE_FAILED", "TRAVERSAL_FAILED", "INVALID_SOURCE_REPAIR_LANE"} | STATUS_SNAPSHOT_ERRORS


class TraversalError(Exception):
    def __init__(self, code):
        self.code = code if code in COMMAND_ERRORS else "TRAVERSAL_FAILED"
        super().__init__(self.code)


class JsonReadError(TraversalError):
    """Fixed input categories only; paths and exception messages stay private."""
    def __init__(self, path, reason, byte_length, maximum):
        super().__init__("READ_FAILED")
        name = Path(path).name
        kinds = {"progress.json": "progress", "merge-receipt.json": "merge-receipt",
                 "status.json": "batch-status", "latest.json": "manifest", "catalog.json": "catalog"}
        kind = kinds.get(name, "company-page" if Path(path).parent.name == "companies" else "other")
        self.diagnostic = {"inputKind": kind, "reason": reason,
                           "byteLength": byte_length, "maxBytes": maximum}


class StatusSnapshotError(TraversalError):
    """A present batch artifact could not be inspected; not a product outcome."""
    def __init__(self, code, byte_length=None):
        super().__init__(code)
        self.byte_length = byte_length if type(byte_length) is int and byte_length >= 0 else None


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
    from historical_inception import catalog_inception
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
        inception = catalog_inception(item, catalog)
        if inception:
            record['catalogInceptionEvidence'] = inception
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


def retained_deferral_until(previous, now):
    """Migrate only the old next-day lock for an explicitly unstarted task."""
    due = instant(previous.get("deferredUntil"))
    if not due or due <= now:
        return None
    deferred = instant(previous.get("lastDeferredAt"))
    attempted = instant(previous.get("lastAttemptAt"))
    if (deferred and deferred <= now and (not attempted or deferred > attempted)
            and due == instant(midnight(deferred.date() + dt.timedelta(days=1)))):
        return None
    return previous["deferredUntil"]


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
        retry_lane(state["enrichment"], 3, state["status"] in SUCCESS and state["enrichment"]["status"] == "exhausted",
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
                        "deferredUntil": retained_deferral_until(prior, now),
                        "lastDeferredAt": prior.get("lastDeferredAt") if instant(prior.get("lastDeferredAt")) and instant(prior["lastDeferredAt"]) <= now else None,
                        "errorCode": prior.get("errorCode") if status == "failed" and prior.get("errorCode") in ERROR_CODES else None,
                        "attempted": bool(prior.get("attempted", valid_attempt and prior.get("attribution") != "portfolio-evidence")),
                        "representativeCode": prior.get("representativeCode") if is_code(prior.get("representativeCode"))
                        and prior.get("representativeCode") in records and records[prior["representativeCode"]]["companyCode"] == record["companyCode"] else code,
                        "attribution": "portfolio-evidence" if prior.get("attribution") == "portfolio-evidence" else "direct"}
        states[code]['priceBindingNeedsReport'] = prior.get('priceBindingNeedsReport') is True
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
                             "managerAttempts": 0,
                             "managerTotalAttempts": counter(prior_quality.get("managerTotalAttempts"), counter(prior_quality.get("managerAttempts"))),
                             "retry": retry_state(None, None, now)}
        states[code]["enrichment"] = normalize_enrichment(prior_enrichment, status, now)
        states[code]["quality"] = normalize_quality(prior_quality, status, now, code=code)
        candidates = (config or {}).get("reportLocatorCandidates", {})
        states[code]["reportLocator"] = normalized_locator(candidates.get(code) if isinstance(candidates, dict) else None,
                                                          code, phase, now)
        ledger, full = locator_history(prior.get("reportLocatorAttempts"), now)
        states[code]["reportLocatorAttempts"] = ledger
        states[code]["reportLocatorHistoryFull"] = full or prior.get("reportLocatorHistoryFull") is True
        history, blocked = claims_history(prior.get('strategyClaimsAttempts'), code, phase, now)
        states[code]['strategyClaimsAttempts'] = history
        states[code]['strategyClaimsHistoryBlocked'] = blocked or prior.get('strategyClaimsHistoryBlocked') is True
        states[code]['strategyClaimsTask'] = None
        states[code]['strategyClaimsStatus'] = 'unavailable'
        states[code]['strategyClaimsErrorCode'] = 'STRATEGY_CLAIMS_SOURCE_UNAVAILABLE'
        states[code]['strategyClaimsRepresentativeCode'] = code
        states[code]['strategyClaimsAttribution'] = 'unavailable'
        states[code]['strategyClaimsGroupAttempts'] = 0
        states[code]['strategyClaimsGroupDigestCount'] = 0
        deferred = instant(prior.get('strategyClaimsLastDeferredAt'))
        states[code]['strategyClaimsLastDeferredAt'] = prior['strategyClaimsLastDeferredAt'] if deferred and deferred <= now else None
        if status == "not-applicable":
            states[code]["notApplicableEvidence"] = clean_not_applicable(exemption, code, phase, now)
    apply_retry_policy(states, config, now)
    return companies, records, states


def directory_incomplete(companies, records):
    return any(item["directoryStatus"] != "complete" for item in companies.values()) or any(not item["companyCode"] for item in records.values())


def eligible(states):
    # Institution mapping is an independent catalogue task. A valid fund code
    # can be collected directly even while its owner remains unresolved.
    return [code for code, state in states.items()
            if state["status"] in {"pending", "failed"} and state["attempts"] < 2 and not state.get("deferredUntil")]


def normalized_locator(value, code, phase, now):
    if not isinstance(value, dict):
        return None
    from report_locators import normalize_locator
    return normalize_locator(value, code, phase["targetPeriodEnd"], now=now)


def locator_history(previous, now):
    """Keep a bounded, version-independent ledger; old digests never reset."""
    previous = previous if isinstance(previous, dict) else {}
    rows = {}
    for digest, item in previous.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(item, dict):
            continue
        count, observed = item.get("attempts"), instant(item.get("lastAttemptAt"))
        if (not isinstance(count, int) or isinstance(count, bool) or count < 1
                or not observed or observed > now + dt.timedelta(minutes=5)):
            continue
        rows[digest] = {"attempts": min(count, REPORT_LOCATOR_MAX_ATTEMPTS), "lastAttemptAt": item["lastAttemptAt"],
                        "errorCode": item.get("errorCode") if item.get("errorCode") in ERROR_CODES else None}
    return dict(list(rows.items())[:REPORT_LOCATOR_MAX_DIGESTS]), len(rows) >= REPORT_LOCATOR_MAX_DIGESTS


def locator_eligible(states):
    result = []
    for code, state in states.items():
        locator = state.get("reportLocator")
        if state["status"] not in {"pending", "failed"} or not locator:
            continue
        ledger = state.get("reportLocatorAttempts", {})
        digest = locator["digest"]
        if digest not in ledger and state.get("reportLocatorHistoryFull"):
            continue
        if ledger.get(digest, {}).get("attempts", 0) < REPORT_LOCATOR_MAX_ATTEMPTS:
            result.append(code)
    return result


def locator_receipt(value, locator, now):
    if not locator or not isinstance(value, dict):
        return None
    from report_locators import normalize_receipt
    return normalize_receipt(value, locator, now=now)


def claims_quota(config):
    value = config.get('strategyClaimsQuota', STRATEGY_CLAIMS_MAX_QUOTA)
    if type(value) is not int or not 0 <= value <= STRATEGY_CLAIMS_MAX_QUOTA:
        raise TraversalError('INVALID_CONFIG')
    return value


def claims_history(previous, code, phase, now):
    """Corrupt/oversized history blocks new requests instead of resetting it."""
    if previous is None:
        return {}, False
    if not isinstance(previous, dict):
        return {}, True
    if len(previous) > STRATEGY_CLAIMS_MAX_DIGESTS:
        # Never trim an oversized old ledger and publish a shorter history.
        raise TraversalError('INVALID_PROGRESS')
    result, blocked = {}, False
    for digest, entry in previous.items():
        if (not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                or not isinstance(entry, dict) or not isinstance(entry.get('receipts'), list)
                or not 1 <= len(entry['receipts']) <= STRATEGY_CLAIMS_MAX_ATTEMPTS):
            blocked = True; continue
        receipts, seen = [], set()
        for receipt in entry['receipts']:
            started = instant(receipt.get('attemptedAt')) if isinstance(receipt, dict) else None
            from strategy_claim_patches import ERRORS, NO_REQUEST_ERRORS, RECEIPT_FIELDS
            outcome, error = (receipt.get('outcome'), receipt.get('errorCode')) if isinstance(receipt, dict) else (None, None)
            outcome_ok = isinstance(outcome, str) and (error is None or isinstance(error, str)) and (
                          outcome in {'started', 'parsed'} and error is None
                          or outcome == 'unknown' and error == 'STRATEGY_CLAIMS_INTERRUPTED'
                          or outcome == 'failed' and error in ERRORS - NO_REQUEST_ERRORS)
            if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS or type(receipt.get('schemaVersion')) is not int or receipt['schemaVersion'] != 1
                    or receipt.get('code') != code or receipt.get('targetPeriodEnd') != phase['targetPeriodEnd']
                    or receipt.get('taskDigest') != digest or not re.fullmatch(r'[a-f0-9]{64}', str(receipt.get('analysisDigest', '')))
                    or not public_url(receipt.get('sourceUrl')) or not started or started > now + dt.timedelta(minutes=5)
                    or not outcome_ok
                    or started in seen):
                blocked = True; continue
            seen.add(started)
            receipts.append({key: receipt.get(key) for key in ('schemaVersion', 'code', 'targetPeriodEnd', 'taskDigest',
                'analysisDigest', 'sourceUrl', 'attemptedAt', 'outcome', 'errorCode')})
        if receipts:
            receipts.sort(key=lambda item: instant(item['attemptedAt']))
            result[digest] = {'attempts': len(receipts), 'receipts': receipts, 'lastAttemptAt': receipts[-1]['attemptedAt']}
            if type(entry.get('attempts')) is not int or entry['attempts'] != len(receipts): blocked = True
    return result, blocked


def claims_task(value, code, phase, now):
    if not isinstance(value, dict): return None
    from strategy_claim_patches import normalize_task
    return normalize_task(value, code=code, period_end=phase['targetPeriodEnd'], now=now)


def claims_receipt(value, task, planned_at, retrieved_at, now):
    if not task or not isinstance(value, dict): return None
    from strategy_claim_patches import normalize_receipt
    return normalize_receipt(value, task, planned_at, retrieved_at, now=now)


def claims_eligible(states, config, exclusions=()):
    if not claims_quota(config): return []
    excluded = set(exclusions)
    return [code for code, state in states.items() if code not in excluded
            and state['status'] in SUCCESS and state.get('strategyClaimsTask')
            and state.get('strategyClaimsRepresentativeCode') == code
            and state.get('strategyClaimsStatus') == 'pending'
            and not state.get('strategyClaimsHistoryBlocked')]


class ClaimsStateValidator:
    """One bounded synchronous NDJSON child, opened only for existing v2 scans."""
    FIELDS = ('periodEnd', 'strategySectionCoverage', 'strategyEvidenceRetrievedAt', 'strategyClaims',
              'strategyClaimsIdentityEvidence', 'strategyClaimsDerivedFromObservedAt', 'strategyEvidencePool',
              'strategyEvidenceBudget', 'strategyAnalysis', 'strategyExcerpts', 'narrative')
    def __init__(self, now):
        self.now, self.process = now, None
    def scanned(self, fund, task):
        report = fund.get('selectedReport') or {}
        if not isinstance(report.get('strategyClaims'), dict) or report['strategyClaims'].get('method') != 'explicit-strategy-claims-v2':
            return False
        if not date(fund.get('asOf')):
            return False
        try:
            value = {'code': task['code'], 'now': self.now.isoformat(), 'asOf': fund['asOf'],
                     'report': {key: report[key] for key in self.FIELDS if key in report}}
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            if len(encoded.encode('utf-8')) > 80000: return False
            if self.process is None:
                self.process = subprocess.Popen(['node', str(Path(__file__).with_name('strategy-claims-state.js'))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
            self.process.stdin.write(encoded + '\n'); self.process.stdin.flush()
            line = self.process.stdout.readline(1024)
            result = json.loads(line)
            return result == {'code': task['code'], 'analysisDigest': task['analysisDigest'], 'scanned': True}
        except (OSError, ValueError, TypeError, BrokenPipeError):
            return False
    def close(self):
        if self.process:
            try: self.process.stdin.close()
            except OSError: pass
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
            self.process.stdout.close()


def hydrate_claims(state, fund, phase, now, validator, *, representative=None):
    from strategy_claim_patches import binding_for
    task = binding_for(fund, phase, now=now, code=state['code'])
    if not task: return
    state['strategyClaimsTask'] = task
    state['strategyClaimsRepresentativeCode'] = representative or state['code']
    state['strategyClaimsAttribution'] = 'direct' if state['code'] == fund['code'] else 'quarter-report-identity'
    if validator.scanned(fund, task):
        state.update(strategyClaimsStatus='scanned', strategyClaimsErrorCode=None)
        return
    history = state['strategyClaimsAttempts']
    exhausted = (state['strategyClaimsHistoryBlocked'] or task['taskDigest'] not in history and len(history) >= STRATEGY_CLAIMS_MAX_DIGESTS
                 or history.get(task['taskDigest'], {}).get('attempts', 0) >= STRATEGY_CLAIMS_MAX_ATTEMPTS)
    receipts = history.get(task['taskDigest'], {}).get('receipts', [])
    state.update(strategyClaimsStatus='exhausted' if exhausted else 'pending',
                 strategyClaimsErrorCode=receipts[-1]['errorCode'] if receipts else None)


def hydrate_claim_group(states, fund, phase, now, validator):
    from strategy_claim_patches import binding_for
    from portfolio_identity import normalize_portfolio_identity
    task = binding_for(fund, phase, now=now)
    if not task: return
    report = fund['selectedReport']
    identity = normalize_portfolio_identity(report.get('strategyClaimsIdentityEvidence') or report.get('portfolioIdentityEvidence'),
        {**task['source'], 'fundCode': fund['code']}, now=now)
    codes = {fund['code']}
    if identity:
        codes.update(code for code in [identity['sourcePortfolioCode'], *identity['shareClassCodes']] if code in states)
    available = {code for code in codes if states[code]['status'] in SUCCESS}
    representative = (identity['sourcePortfolioCode'] if identity and identity['sourcePortfolioCode'] in available
                      else fund['code'] if fund['code'] in available else min(available) if available else fund['code'])
    for code in sorted(codes):
        hydrate_claims(states[code], fund, phase, now, validator, representative=representative)
    # A/C representative selection can change across runs. Count only actual
    # receipts in this report-proven group, without manufacturing alias events
    # or granting a fresh source budget just because its request code changed.
    seen, source_versions = set(), set()
    history_blocked = any(states[code]['strategyClaimsHistoryBlocked'] for code in codes)
    for code in codes:
        for entry in states[code]['strategyClaimsAttempts'].values():
            for received in entry['receipts']:
                source_versions.add(received['analysisDigest'])
                if (received['analysisDigest'] == task['analysisDigest']
                        and received['sourceUrl'] == task['source']['sourceUrl']):
                    seen.add((received['code'], received['taskDigest'], instant(received['attemptedAt'])))
    group_exhausted = (history_blocked or len(seen) >= STRATEGY_CLAIMS_MAX_ATTEMPTS
                       or task['analysisDigest'] not in source_versions and len(source_versions) >= STRATEGY_CLAIMS_MAX_DIGESTS)
    for code in codes:
        state = states[code]
        state['strategyClaimsGroupAttempts'] = len(seen)
        state['strategyClaimsGroupDigestCount'] = len(source_versions)
        if group_exhausted and state['strategyClaimsStatus'] == 'pending':
            state['strategyClaimsStatus'] = 'exhausted'


def claims_audit(states, config):
    rows = [state for state in states.values() if state['status'] != 'not-applicable']
    source, quality, prices = eligible_work(states, config)
    return {'enabled': claims_quota(config) > 0, 'quotaPerQuarter': claims_quota(config),
            'coverageUnit': 'fund-share-code', 'taskUnit': 'verified-report-representative-code',
            'readyFunds': len(claims_eligible(states, config, set(source) | quality | prices)),
            **{status + 'Funds': sum(row.get('strategyClaimsStatus') == status for row in rows)
               for status in ('scanned', 'pending', 'unavailable', 'exhausted')},
            'attempts': sum(item['attempts'] for state in rows for item in state['strategyClaimsAttempts'].values()),
            'maxAttemptsPerDigest': STRATEGY_CLAIMS_MAX_ATTEMPTS, 'maxTrackedDigests': STRATEGY_CLAIMS_MAX_DIGESTS,
            'semanticAnalysisComplete': False}


def claims_status_row(item, phase, now, planned_at, retrieved_at, supplied=None, deferred=False):
    task = claims_task(item.get('strategyClaimsOnly'), item['code'], phase, now)
    receipt = claims_receipt(supplied, task, planned_at, retrieved_at, now)
    error = (receipt.get('errorCode') or ('STRATEGY_CLAIMS_INTERRUPTED' if receipt['outcome'] == 'started' else None)) if receipt else (
        'BATCH_DEADLINE_REACHED' if deferred and task else 'STRATEGY_CLAIMS_REQUEST_NOT_STARTED' if task else 'STRATEGY_CLAIMS_BINDING_INVALID')
    return {'code': item['code'], 'companyCode': item.get('companyCode') or item.get('companyId'),
            'status': 'deferred-budget' if deferred and not receipt and task else 'failed', 'errorCode': error,
            'attempted': bool(receipt), 'strategyClaimsOnly': True, 'strategyClaimsTask': task,
            'strategyClaimsReceipt': receipt, 'strategyClaimsAttempt': bool(receipt),
            'qualityAttempt': False, 'managerAttempt': False, 'strategyAttempt': False, 'enrichmentAttempt': False}


def locator_report_matches(evidence, locator, receipt):
    if not isinstance(evidence, dict) or not locator or not receipt:
        return False
    from report_locators import candidate_id
    attempted = set(receipt["attemptedCandidateIds"])
    return evidence.get("sourceUrl") in {candidate["sourceUrl"] for candidate in locator["candidates"]
                                         if candidate_id(candidate) in attempted}


def stock_key(item):
    if not isinstance(item, dict) or item.get("market") not in {"CN", "HK", "US"}:
        return None
    code = item.get("stockCode")
    pattern = r"[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?" if item["market"] == "US" else r"\d{5,6}"
    return f"{item['market']}:{code}" if isinstance(code, str) and re.fullmatch(pattern, code) else None


def missing_stock_objects(value):
    """Decode compact checkpoints; status and collector interfaces keep objects."""
    if not isinstance(value, dict):
        return None
    if "missingStockKeys" in value:
        keys = value["missingStockKeys"]
        if not isinstance(keys, list) or any(not isinstance(key, str) or not re.fullmatch(r"(?:(?:CN|HK):\d{5,6}|US:[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?)", key) for key in keys):
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
    count, available, complete = value.get("holdingCount"), value.get("priceAvailable"), value.get("priceComplete")
    missing = missing_stock_objects(value)
    conflicts = value.get('conflictingStocks', [])
    conflict_count = value.get('priceConflicted', 0)
    fingerprint = value.get("holdingsIdentity")
    resolutions = value.get('referenceResolutions', [])
    return (value.get("coverageVersion") == PRICE_COVERAGE_VERSION and value.get("calendarId") == CALENDAR["id"]
            and isinstance(count, int) and not isinstance(count, bool) and count >= 0
            and isinstance(available, int) and not isinstance(available, bool) and 0 <= available <= count
            and isinstance(complete, int) and not isinstance(complete, bool) and 0 <= complete <= available
            and isinstance(conflicts, list) and type(conflict_count) is int and conflict_count == len(conflicts)
            and all(stock_key(item) for item in conflicts) and len({stock_key(item) for item in conflicts}) == len(conflicts)
            and (not conflicts or value.get('consistencyVersion') == 1)
            and isinstance(missing, list) and len(missing) + len(conflicts) == count - complete
            and all(stock_key(item) for item in missing)
            and len({stock_key(item) for item in missing}) == len(missing)
            and not ({stock_key(item) for item in missing} & {stock_key(item) for item in conflicts})
            and isinstance(resolutions, list) and len(resolutions) <= count
            and all(valid_resolution(item) for item in resolutions)
            and len({stock_key(item) for item in resolutions}) == len(resolutions)
            and not ({stock_key(item) for item in resolutions} & {stock_key(item) for item in missing + conflicts})
            and isinstance(fingerprint, str) and bool(re.fullmatch(r"[a-f0-9]{64}", fingerprint)))


def readable_legacy_price_coverage(value):
    """Read old source receipts without granting their availability a v2 proof."""
    if not isinstance(value, dict) or value.get('coverageVersion') not in (None, 1):
        return False
    count, available, missing = value.get('holdingCount'), value.get('priceAvailable'), missing_stock_objects(value)
    return (type(count) is int and type(available) is int and 0 <= available <= count
            and isinstance(missing, list) and len(missing) == count - available
            and all(stock_key(item) for item in missing) and len({stock_key(item) for item in missing}) == len(missing)
            and isinstance(value.get('holdingsIdentity'), str) and bool(re.fullmatch(r'[a-f0-9]{64}', value['holdingsIdentity'])))


def price_coverage(fund, now=None):
    holdings = fund.get("selectedReport", {}).get("holdings", [])
    if not isinstance(holdings, list) or any(not stock_key(item) for item in holdings):
        return None
    end = fund.get("selectedReport", {}).get("periodEnd")
    if not date(end):
        return None
    start = quarter_start(end)
    stocks = {stock_key(item): {"stockCode": item["stockCode"], "market": item["market"]} for item in holdings}
    baseline = fund.get("previousReport") or {}
    expected_baseline = (date(start) - dt.timedelta(days=1)).isoformat()
    if public_url(baseline.get("sourceUrl")) and baseline.get("periodEnd") == expected_baseline:
        for row in fund.get("rows", []):
            if isinstance(row, dict) and row.get("previous") is not None and stock_key(row):
                stocks[stock_key(row)] = {"stockCode": row["stockCode"], "market": row["market"]}
    observed, complete, conflicts, resolutions = set(), set(), set(), []
    for row in fund.get("rows", []):
        key = stock_key(row)
        price = row.get("priceRange", {}) if isinstance(row, dict) else {}
        identity = {'market': row.get('market'), 'stockCode': row.get('stockCode'),
                    'currency': CALENDAR['markets'].get(row.get('market'), {}).get('currency'),
                    'rangeStart': start, 'rangeEnd': end, 'calendarId': CALENDAR['id']}
        if key in stocks and unresolved_conflict(price, identity, now):
            conflicts.add(key)
        resolution = resolution_for(price, identity, now) if key in stocks else None
        if resolution: resolutions.append(resolution)
        original = price
        price = effective_price_range(price, identity, now) or price
        low, high = price.get("low"), price.get("high")
        if (key in stocks and price.get("status") == "observed" and price.get("priceBasis") == "unadjusted"
                and public_url(price.get("sourceUrl")) and isinstance(low, (int, float)) and not isinstance(low, bool)
                and isinstance(high, (int, float)) and not isinstance(high, bool) and 0 < low <= high < float('inf')
                and price.get("currency") == {"CN": "CNY", "HK": "HKD", "US": "USD"}[stocks[key]["market"]]
                and price.get("rangeStart") == start and price.get("rangeEnd") == end
                and type(price.get("observedCount")) is int and price["observedCount"] > 0
                and date(price.get("observedStart")) and date(price.get("observedEnd"))
                and start <= price["observedStart"] <= price["observedEnd"] <= end):
            observed.add(key)
            if valid_usable_complete_price(original, stocks[key]["market"], stocks[key]["stockCode"], start, end):
                complete.add(key)
    missing = [stocks[key] for key in sorted(set(stocks) - complete - conflicts)]
    return {"coverageVersion": PRICE_COVERAGE_VERSION, "calendarId": CALENDAR["id"], 'consistencyVersion': 1,
            "holdingCount": len(stocks), "priceAvailable": len(observed), "priceComplete": len(complete), "missingStocks": missing,
            'priceConflicted': len(conflicts), 'conflictingStocks': [stocks[key] for key in sorted(conflicts)],
            **({'referenceResolutions': resolutions} if resolutions else {}),
            "holdingsIdentity": hashlib.sha256("|".join(sorted(stocks)).encode()).hexdigest()}


def normalize_enrichment(previous, status, now):
    previous = previous if isinstance(previous, dict) else {}
    attempts = previous.get("attempts", 0)
    attempts = max(0, min(3, attempts)) if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
    previous_attempts = attempts
    migrated = previous.get("coverageVersion") != PRICE_COVERAGE_VERSION or previous.get("calendarId") != CALENDAR["id"]
    if migrated:
        attempts = 0
    last = previous.get("lastAttemptAt")
    if migrated or not instant(last) or instant(last) > now + dt.timedelta(minutes=5):
        last = None
    result = {"status": "not-applicable", "attempts": attempts, "lastAttemptAt": last,
              "totalAttempts": counter(previous.get("totalAttempts"), previous_attempts), "retry": retry_state(None if migrated else previous.get("retry"), last, now),
              "coverageVersion": PRICE_COVERAGE_VERSION, "calendarId": CALENDAR["id"],
              "holdingCount": None, "priceAvailable": None, "priceComplete": None, "missingStocks": [], "holdingsIdentity": None,
              'consistencyVersion': 1, 'priceConflicted': 0, 'conflictingStocks': [],
              "errorCode": None}
    if status in SUCCESS:
        if valid_price_coverage(previous):
            result.update({key: previous[key] for key in ("holdingCount", "priceAvailable", "priceComplete", "holdingsIdentity")})
            result["missingStocks"] = missing_stock_objects(previous)
            result['conflictingStocks'] = previous.get('conflictingStocks', [])
            result['priceConflicted'] = len(result['conflictingStocks'])
        full = result["holdingCount"] is not None and result["priceComplete"] == result["holdingCount"]
        conflict_only = result['priceConflicted'] and not result['missingStocks']
        result["status"] = "not-applicable" if status == "no-equities" and result['holdingCount'] == 0 else "complete" if full else 'conflict' if conflict_only else "exhausted" if attempts >= 3 else "pending"
        result["errorCode"] = None if full else 'PRICE_SOURCE_CONFLICT' if conflict_only else "PRICE_COVERAGE_INCOMPLETE"
    return result


def enrichment_eligible(states):
    return [code for code, state in states.items() if state["status"] in SUCCESS
            and not state.get("deferredUntil")
            and state["attribution"] != "portfolio-evidence" and state.get("representativeCode", code) == code
            and state["enrichment"]["status"] == "pending"
            and state["enrichment"]["attempts"] < 3]


def apply_coverage(state, coverage, *, trusted_current=False):
    current = state["enrichment"]
    if not valid_price_coverage(coverage):
        return
    missing = missing_stock_objects(coverage)
    available = coverage["priceAvailable"]
    conflicts = {stock_key(item): item for item in coverage.get('conflictingStocks', [])}
    # A receipt can be delayed and state retains only stock keys, not every
    # conflict fact. Only hydration of the actual current merged snapshot can
    # clear that sticky state; an incoming self-contained old proof cannot.
    resolved = {stock_key(item) for item in coverage.get('referenceResolutions', [])} if trusted_current else set()
    if valid_price_coverage(current) and current["holdingsIdentity"] == coverage["holdingsIdentity"]:
        still_missing = {stock_key(item) for item in missing_stock_objects(current)}
        missing = [item for item in missing if stock_key(item) in still_missing]
        available = max(available, current["priceAvailable"])
        # A delayed single-source receipt cannot resolve a witnessed conflict.
        conflicts.update({stock_key(item): item for item in current.get('conflictingStocks', []) if stock_key(item) not in resolved})
    missing = [item for item in missing if stock_key(item) not in conflicts]
    complete = coverage["holdingCount"] - len(missing) - len(conflicts)
    current.update({key: coverage[key] for key in ("coverageVersion", "calendarId", "holdingCount", "holdingsIdentity")})
    current.update({"missingStocks": missing, "priceAvailable": max(available, complete), "priceComplete": complete,
                    'consistencyVersion': 1, 'priceConflicted': len(conflicts), 'conflictingStocks': [conflicts[key] for key in sorted(conflicts)]})
    current["status"] = "not-applicable" if state['status'] == 'no-equities' and coverage['holdingCount'] == 0 else 'conflict' if not missing and conflicts else "complete" if not missing else "exhausted" if current["attempts"] >= 3 else "pending"
    current["errorCode"] = 'PRICE_SOURCE_CONFLICT' if not missing and conflicts else None if not missing else "PRICE_COVERAGE_INCOMPLETE"
    quality = state.get('quality')
    if conflicts and isinstance(quality, dict) and isinstance(quality.get('fields'), dict):
        quality['fields']['prices'] = 'partial'
        quality['priceConflictOnly'] = not missing
        quality['priceConsistencyVersion'] = 1
        quality['missingFields'] = [key for key in QUALITY_FIELDS if quality['fields'].get(key) not in QUALITY_ACCEPTED]
        if quality['missingFields'] == ['prices'] and not missing:
            quality['status'] = 'conflict'
    elif resolved and not missing and isinstance(quality, dict) and isinstance(quality.get('fields'), dict):
        quality['fields']['prices'] = 'complete'
        quality['priceConflictOnly'] = False
        quality['priceConsistencyVersion'] = 1
        quality['missingFields'] = [key for key in QUALITY_FIELDS if quality['fields'].get(key) not in QUALITY_ACCEPTED]
        if not quality['missingFields']:
            quality['status'] = 'complete'
        elif quality.get('status') == 'conflict':
            quality['status'] = 'pending' if quality.get('attempts', 0) < QUALITY_MAX_ATTEMPTS else 'exhausted'


def normalize_quality(previous, status, now, code=None):
    previous = previous if isinstance(previous, dict) else {}
    raw_fields = previous.get("fields") if isinstance(previous.get("fields"), dict) else {}
    if status == "not-applicable":
        return {"fields": {key: "not-applicable" for key in QUALITY_FIELDS}, "missingFields": [],
                "attempts": 0, "totalAttempts": counter(previous.get("totalAttempts")),
                "managerAttempts": 0, "managerTotalAttempts": counter(previous.get("managerTotalAttempts")), "managerLastAttemptAt": None,
                "strategyAttempts": 0, "strategyTotalAttempts": counter(previous.get("strategyTotalAttempts")), "strategyLastAttemptAt": None,
                "strategySectionCoverageVersion": STRATEGY_SECTION_COVERAGE_VERSION,
                "managerTableCoverageVersion": MANAGER_TABLE_COVERAGE_VERSION, "priceCoverageVersion": 2,
                "retry": retry_state(previous.get("retry"), previous.get("lastAttemptAt"), now),
                "lastAttemptAt": None, "status": "not-applicable"}
    fields = {key: raw_fields.get(key) if raw_fields.get(key) in QUALITY_VALUES else "missing"
              for key in QUALITY_FIELDS}
    manager_version_changed = previous.get("managerTableCoverageVersion") != MANAGER_TABLE_COVERAGE_VERSION
    if manager_version_changed and fields["managers"] == "complete":
        fields["managers"] = "partial"
    strategy_version_changed = previous.get("strategySectionCoverageVersion") != STRATEGY_SECTION_COVERAGE_VERSION
    if strategy_version_changed and fields["strategy"] == "complete":
        fields["strategy"] = "partial"
    if previous.get("priceCoverageVersion") != 2 and status in SUCCESS:
        fields["prices"] = "missing"
    if status == "no-equities":
        fields["holdings"] = "complete"
        if previous.get("priceUniverseEmpty") is True:
            fields["prices"] = "not-applicable"
    baseline_exemption = previous.get("baselineExemption")
    baseline_exempt = (valid_baseline_exemption(baseline_exemption, now)
                       and (code is None or baseline_exemption.get("fundCode") == code))
    for key in QUALITY_FIELDS:
        if fields[key] == "not-applicable" and not ((status == "no-equities" and key == "prices" and previous.get("priceUniverseEmpty") is True)
                                                    or key == "baseline" and baseline_exempt):
            fields[key] = "missing"
    attempts = previous.get("attempts", 0)
    attempts = max(0, min(QUALITY_MAX_ATTEMPTS, attempts)) if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
    # The new report-manager field has its own bounded repair budget. Historical
    # six-field attempts cannot exhaust a parser that did not yet exist.
    manager_attempts = 0 if manager_version_changed else min(MANAGER_MAX_ATTEMPTS, counter(previous.get("managerAttempts")))
    strategy_attempts = 0 if strategy_version_changed else min(STRATEGY_MAX_ATTEMPTS, counter(previous.get("strategyAttempts")))
    strategy_last = previous.get("strategyLastAttemptAt")
    if not instant(strategy_last) or instant(strategy_last) > now + dt.timedelta(minutes=5):
        strategy_last = None
    manager_last = previous.get("managerLastAttemptAt")
    if not instant(manager_last) or instant(manager_last) > now + dt.timedelta(minutes=5):
        manager_last = None
    last = previous.get("lastAttemptAt")
    if not instant(last) or instant(last) > now + dt.timedelta(minutes=5):
        last = None
    missing = [key for key in QUALITY_FIELDS if fields[key] not in QUALITY_ACCEPTED]
    conflict_only = previous.get('priceConflictOnly') is True and fields['prices'] not in QUALITY_ACCEPTED
    actionable_missing = [key for key in missing if key != 'prices' or not conflict_only]
    result = {"fields": fields, "missingFields": missing, "attempts": attempts, "lastAttemptAt": last,
              "managerAttempts": manager_attempts, "managerTotalAttempts": counter(previous.get("managerTotalAttempts"), counter(previous.get("managerAttempts"))),
              "managerLastAttemptAt": manager_last,
              "strategyAttempts": strategy_attempts, "strategyTotalAttempts": counter(previous.get("strategyTotalAttempts"), counter(previous.get("strategyAttempts"))),
              "strategyLastAttemptAt": strategy_last, "strategySectionCoverageVersion": STRATEGY_SECTION_COVERAGE_VERSION,
              "managerTableCoverageVersion": MANAGER_TABLE_COVERAGE_VERSION, "priceCoverageVersion": 2,
              'priceConsistencyVersion': 1, 'priceConflictOnly': conflict_only,
              "priceUniverseEmpty": previous.get("priceUniverseEmpty") is True,
              "totalAttempts": counter(previous.get("totalAttempts"), attempts), "retry": retry_state(previous.get("retry"), last, now),
              "pdfRetryable": previous.get("pdfRetryable") is not False,
              "status": "complete" if not missing else 'conflict' if not actionable_missing else "pending" if (
                  any(key not in {"managers", "strategy"} for key in actionable_missing) and attempts < QUALITY_MAX_ATTEMPTS
                  or "managers" in actionable_missing and manager_attempts < MANAGER_MAX_ATTEMPTS
                  or "strategy" in actionable_missing and strategy_attempts < STRATEGY_MAX_ATTEMPTS) else "exhausted"}
    if baseline_exempt and fields["baseline"] == "not-applicable":
        result["baselineExemption"] = {key: baseline_exemption[key] for key in ("reason", "date", "sourceUrl", "fundCode", "baselinePeriodEnd", "periodEnd")}
        if baseline_exemption.get('inceptionEvidence'):
            result['baselineExemption']['inceptionEvidence'] = baseline_exemption['inceptionEvidence']
    return result


def valid_baseline_exemption(evidence, now):
    if not isinstance(evidence, dict) or evidence.get("reason") != "ESTABLISHED_AFTER_BASELINE" or not is_code(evidence.get("fundCode")):
        return False
    established, baseline, target = (date(evidence.get(key)) for key in ("date", "baselinePeriodEnd", "periodEnd"))
    if not (bool(established and baseline and target) and baseline < established <= target <= china_date(now)
            and public_url(evidence.get('sourceUrl'))):
        return False
    from historical_inception import profile_source
    if profile_source(evidence['sourceUrl'], evidence['fundCode']):
        return True
    from historical_inception import normalize_inception
    historical = normalize_inception(evidence.get('inceptionEvidence'), evidence['fundCode'], evidence['periodEnd'], now=now)
    return bool(historical and historical['date'] == evidence['date'] and historical['sourceUrl'] == evidence['sourceUrl'])


def quality_for_code(observation, code, previous=None):
    """Portfolio reports can be shared; a product's founding exemption cannot."""
    result = {**observation, "fields": dict(observation.get("fields", {}))}
    exemption = result.get("baselineExemption")
    if isinstance(exemption, dict) and exemption.get("fundCode") != code:
        result.pop("baselineExemption", None)
        prior = previous if isinstance(previous, dict) else {}
        prior_exemption = prior.get("baselineExemption")
        if result["fields"].get("baseline") == "not-applicable":
            old_status = (prior.get("fields") or {}).get("baseline")
            result["fields"]["baseline"] = "complete" if old_status == "complete" else "missing"
            if (old_status == "not-applicable" and isinstance(prior_exemption, dict)
                    and prior_exemption.get("fundCode") == code):
                result["fields"]["baseline"] = "not-applicable"
                result["baselineExemption"] = prior_exemption
    return result


def quarter_start(period_end):
    value = date(period_end)
    return dt.date(value.year, ((value.month - 1) // 3) * 3 + 1, 1).isoformat() if value else None


def valid_manager_rows(fund, report, phase):
    """Verify retained report-attributed rows without asserting table coverage."""
    if fund.get("managerAttribution") != "report":
        return False
    managers = fund.get("reportManagers")
    if managers is None:
        managers = report.get("managers")
        if isinstance(managers, list):
            managers = [item for item in managers if isinstance(item, dict) and date(item.get("startDate"))
                        and item["startDate"] <= phase["targetPeriodEnd"] and
                        (item.get("endDate") is None or date(item.get("endDate")) and item["endDate"] >= quarter_start(phase["targetPeriodEnd"]))]
    if not isinstance(managers, list) or not 1 <= len(managers) <= 50:
        return False
    source = fund.get("managerSourceUrl") or report.get("managerSourceUrl")
    if not public_url(source):
        return False
    for manager in managers:
        if not isinstance(manager, dict):
            return False
        name, role = manager.get("name"), manager.get("role")
        start, end = date(manager.get("startDate")), date(manager.get("endDate"))
        proof = manager.get("evidence")
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
                or not isinstance(role, str) or "基金经理" not in role or "基金经理助理" in role
                or not start or manager.get("endDate") is not None and not end
                or start > date(phase["targetPeriodEnd"]) or end and (end < start or end < date(quarter_start(phase["targetPeriodEnd"])))
                or manager.get("periodEnd") != phase["targetPeriodEnd"]
                or not date(manager.get("periodStart")) or manager["periodStart"] > manager["periodEnd"]
                or not isinstance(proof, dict) or proof.get("method") != "explicit-report-manager-table"
                or not public_url(proof.get("sourceUrl")) or proof.get("periodEnd") != phase["targetPeriodEnd"]
                or not isinstance(proof.get("section"), str) or not re.fullmatch(r"4\.1(?:\.2)?", proof["section"])
                or type(proof.get("page")) is not int or not 1 <= proof["page"] <= 10000
                or type(proof.get("tablePage")) is not int or not 1 <= proof["tablePage"] <= proof["page"]
                or not isinstance(proof.get("pages"), list) or not 1 <= len(proof["pages"]) <= 4
                or any(type(page) is not int or page != proof["page"] + index or page > 10000 for index, page in enumerate(proof["pages"]))
                or not isinstance(proof.get("text"), str) or not 1 <= len(proof["text"]) <= 240):
            return False
        text = re.sub(r"\s+", "", proof["text"])
        if not text.startswith(re.sub(r"\s+", "", name)) or "基金经理" not in text:
            return False
        dates = []
        for match in re.finditer(r"((?:19|20)\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?", text):
            try:
                dates.append((dt.date(*map(int, match.groups())), match.end()))
            except ValueError:
                return False
        if not any(value == start for value, _offset in dates) or end and not any(value == end for value, _offset in dates):
            return False
        if end is None and not any(value == start and re.search(r"[-—－–]|至今|在任", text[offset:]) for value, offset in dates):
            return False
    return True


def valid_report_managers(fund, report, phase):
    """Complete requires a bounded whole-table proof, not merely one valid row."""
    if not valid_manager_rows(fund, report, phase):
        return False
    proof = normalize_manager_table_coverage(fund.get("managerTableCoverage") or report.get("managerTableCoverage"),
                                             period_end=phase["targetPeriodEnd"])
    if not proof or not proof["complete"] or not public_url(proof["sourceUrl"]) or fund.get("managerSourceUrl") != proof["sourceUrl"]:
        return False
    quarter = quarter_start(phase["targetPeriodEnd"])
    managers = fund.get("reportManagers")
    if managers is None:
        managers = [item for item in report.get("managers", []) if date(item.get("startDate")) and item["startDate"] <= phase["targetPeriodEnd"]
                    and (item.get("endDate") is None or date(item.get("endDate")) and item["endDate"] >= quarter)]
    expected = [key for key in proof["managerKeys"] if (lambda value: value[1] <= phase["targetPeriodEnd"]
                and (value[2] is None or value[2] >= quarter))(json.loads(key))]
    return (all(manager["evidence"]["sourceUrl"] == proof["sourceUrl"] for manager in managers)
            and sorted(manager_key(manager) for manager in managers) == sorted(expected))


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
    if not published or published < date(report["periodEnd"]) or published > china_date(observed):
        return None
    if report.get("fundCode") not in (None, fund["code"]) or report.get("sourceFundCode") not in (None, fund["code"]):
        return None
    fields = {key: "missing" for key in QUALITY_FIELDS}
    if valid_report_managers(fund, report, phase):
        fields["managers"] = "complete"
    elif valid_manager_rows(fund, report, phase):
        fields["managers"] = "partial"
    if (fund.get("status") == "no-equities" and not report.get("holdings")
            and not any(row.get("current") is not None for row in fund.get("rows", []) if isinstance(row, dict))
            and no_equities_report_matches(report, fund.get("noEquitiesEvidence") or report.get("noEquitiesEvidence"))):
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
        fields["strategy"] = "partial" if (narrative.get("periodStart") == quarter_start(phase["targetPeriodEnd"])
                                                and narrative.get("periodEnd") == phase["targetPeriodEnd"]) else "wrong-period"
    if fields["strategy"] != "partial":
        excerpts = report.get("strategyExcerpts")
        if isinstance(excerpts, list) and any(isinstance(item, dict) and isinstance(item.get("text"), str)
                and item["text"].strip() and public_url(item.get("sourceUrl"))
                and item.get("periodStart") == quarter_start(phase["targetPeriodEnd"])
                and item.get("periodEnd") == phase["targetPeriodEnd"] for item in excerpts):
            fields["strategy"] = "partial"
    strategy_proof = normalize_strategy_section_coverage(report.get("strategySectionCoverage"), phase["targetPeriodEnd"])
    if strategy_proof:
        strategy_observed = instant(report.get("strategyEvidenceRetrievedAt")) if report.get("strategyEvidenceRetrievedAt") is not None else observed
        period_conflict = any(item.get("periodEnd") != phase["targetPeriodEnd"] or item.get("kind") != "quarterly"
                              for item in strategy_proof["documentPeriodEvidence"])
        if period_conflict or strategy_proof["kind"] != "quarterly" or strategy_proof["periodStart"] != quarter_start(phase["targetPeriodEnd"]):
            fields["strategy"] = "wrong-period"
        elif (strategy_proof["complete"] and strategy_observed and strategy_observed <= now + dt.timedelta(minutes=5)
              and date(strategy_proof["publishedAt"]) <= china_date(strategy_observed)):
            fields["strategy"] = "complete"
        elif strategy_proof["sections"] and fields["strategy"] == "missing":
            fields["strategy"] = "partial"
    nav = report.get("navYuan")
    if isinstance(nav, (float, int)) and not isinstance(nav, bool) and 0 < nav < float("inf"):
        fields["nav"] = "complete"
    baseline_exemption = {"reason": "ESTABLISHED_AFTER_BASELINE", "date": fund.get("inceptionDate"),
                          "sourceUrl": fund.get("inceptionSourceUrl"), "fundCode": fund["code"],
                          "baselinePeriodEnd": phase["baselinePeriodEnd"], "periodEnd": phase["targetPeriodEnd"]}
    if fund.get('inceptionEvidence'):
        baseline_exemption['inceptionEvidence'] = fund['inceptionEvidence']
    baseline = fund.get("previousReport")
    if (isinstance(baseline, dict) and baseline.get("periodEnd") == phase["baselinePeriodEnd"]
            and public_url(baseline.get("sourceUrl")) and date(baseline.get("publishedAt"))
            and date(baseline["publishedAt"]) >= date(baseline["periodEnd"])
            and date(baseline["publishedAt"]) <= china_date(observed)
            and (baseline.get("holdings") or baseline.get("noEquitiesConfirmed") is True
                 and no_equities_report_matches(baseline, baseline.get("noEquitiesEvidence")))):
        fields["baseline"] = "complete"
    elif valid_baseline_exemption(baseline_exemption, now):
        fields["baseline"] = "not-applicable"
    coverage = price_coverage(fund, now)
    if valid_price_coverage(coverage):
        if coverage['holdingCount'] == 0 and fund.get('status') == 'no-equities':
            fields['prices'] = 'not-applicable'
        elif coverage['holdingCount'] > 0:
            fields["prices"] = "complete" if coverage["priceComplete"] == coverage["holdingCount"] else "partial" if coverage["priceAvailable"] or coverage.get('priceConflicted') else "missing"
    result = {"fields": fields, "managerTableCoverageVersion": MANAGER_TABLE_COVERAGE_VERSION, "priceCoverageVersion": 2,
              "strategySectionCoverageVersion": STRATEGY_SECTION_COVERAGE_VERSION,
              "priceUniverseEmpty": bool(coverage and coverage['holdingCount'] == 0),
              'priceConsistencyVersion': 1,
              'priceConflictOnly': bool(coverage and coverage.get('priceConflicted') and not coverage['missingStocks']),
              "pdfRetryable": not report.get("parseMethod") and report.get("parseErrorCode") in (None, *RECOVERABLE_ERRORS)}
    if fields["baseline"] == "not-applicable" and valid_baseline_exemption(baseline_exemption, now):
        result["baselineExemption"] = baseline_exemption
    return result


def quality_eligible(states):
    return [code for code, state in states.items() if state["status"] in SUCCESS
            and not state.get("deferredUntil")
            and state["attribution"] != "portfolio-evidence" and state.get("representativeCode", code) == code
            and state["quality"]["status"] == "pending" and (
                any(field not in {"managers", "strategy"} and (field != 'prices' or not state['quality'].get('priceConflictOnly')) for field in state["quality"]["missingFields"]) and state["quality"]["attempts"] < QUALITY_MAX_ATTEMPTS
                or "managers" in state["quality"]["missingFields"] and state["quality"]["managerAttempts"] < MANAGER_MAX_ATTEMPTS
                or "strategy" in state["quality"]["missingFields"] and state["quality"]["strategyAttempts"] < STRATEGY_MAX_ATTEMPTS)]


def manager_only_repair(quality):
    return ("managers" in quality["missingFields"] and quality["managerAttempts"] < MANAGER_MAX_ATTEMPTS
            and ([field for field in quality["missingFields"] if field != 'prices' or not quality.get('priceConflictOnly')] == ["managers"] or quality["attempts"] >= QUALITY_MAX_ATTEMPTS))


def strategy_only_repair(quality):
    return ("strategy" in quality["missingFields"] and quality["strategyAttempts"] < STRATEGY_MAX_ATTEMPTS
            and ([field for field in quality["missingFields"] if field != 'prices' or not quality.get('priceConflictOnly')] == ["strategy"] or quality["attempts"] >= QUALITY_MAX_ATTEMPTS))


def valid_quality(value, phase=None):
    valid = (isinstance(value, dict) and isinstance(value.get("fields"), dict)
            and set(value["fields"]) in (set(QUALITY_FIELDS), set(SIX_QUALITY_FIELDS))
            and all(item in QUALITY_VALUES for item in value["fields"].values()))
    if valid and value.get("baselineExemption") is not None:
        exemption = value["baselineExemption"]
        valid = isinstance(exemption, dict) and (phase is None or (
            exemption.get("periodEnd") == phase["targetPeriodEnd"]
            and exemption.get("baselinePeriodEnd") == phase["baselinePeriodEnd"]))
    return valid


def merge_quality_observation(previous, observation):
    """Old collector flags cannot manufacture newer coverage proofs."""
    incoming = dict(observation.get("fields", {}))
    for field, version, expected in (("managers", "managerTableCoverageVersion", MANAGER_TABLE_COVERAGE_VERSION),
                                      ("strategy", "strategySectionCoverageVersion", STRATEGY_SECTION_COVERAGE_VERSION),
                                      ("prices", "priceCoverageVersion", 2)):
        if observation.get(version) != expected:
            if previous["fields"].get(field) == "complete" or field not in incoming:
                incoming[field] = previous["fields"].get(field, "missing")
            elif incoming[field] == "complete":
                incoming[field] = "partial"
    if previous.get('priceConflictOnly') is True:
        incoming['prices'] = 'partial'
    return {**previous, **observation, "fields": incoming,
            'priceConsistencyVersion': 1, 'priceConflictOnly': previous.get('priceConflictOnly') is True or observation.get('priceConflictOnly') is True,
            "strategySectionCoverageVersion": STRATEGY_SECTION_COVERAGE_VERSION,
            "managerTableCoverageVersion": MANAGER_TABLE_COVERAGE_VERSION, "priceCoverageVersion": 2}


def hydrate_quality(states, retained, phase, now):
    """Recompute quality from the merger's effective observations on every pass."""
    def observations():
        validator = ClaimsStateValidator(now)
        try:
            for fund in retained or []:
                if isinstance(fund, dict) and fund.get('code') in states:
                    hydrate_claim_group(states, fund, phase, now, validator)
                yield fund
        finally:
            validator.close()
    for fund in observations():
        quality = report_quality(fund, phase, now)
        if quality is None:
            continue
        coverage = price_coverage(fund, now)
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
            scoped = quality_for_code(quality, code, state["quality"])
            state["quality"] = normalize_quality({**state["quality"], **scoped}, state["status"], now, code=code)
            if state["status"] in SUCCESS and valid_price_coverage(coverage):
                apply_coverage(state, coverage, trusted_current=True)


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


def no_equities_report_matches(report, evidence):
    return (isinstance(report, dict) and isinstance(evidence, dict) and evidence.get("confirmed") is True
            and evidence.get("sourceUrl") == report.get("sourceUrl")
            and evidence.get("periodEnd") == report.get("periodEnd")
            and evidence.get("publishedAt") == report.get("publishedAt")
            and (evidence.get("evidenceType") != "exhaustive-nonstock-assets" or evidence.get("kind") == report.get("kind"))
            and evidence_page(evidence.get("page")) and no_equities_content(evidence))


def no_equities_content(evidence):
    """Require the quoted stock section and both same-page zero asset rows."""
    if not isinstance(evidence, dict):
        return False
    kind = evidence.get("evidenceType")
    if kind == "exhaustive-nonstock-assets":
        from nonstock_assets import valid_nonstock_assets
        return valid_nonstock_assets(evidence)
    if kind == "zero-stock-balance-sheet":
        from zero_stock import valid_zero_stock
        return valid_zero_stock(evidence)
    if kind == "explicit-no-stock-detail":
        from zero_stock import valid_explicit_no_stock
        return valid_explicit_no_stock(evidence)
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


def clean_not_applicable(evidence, code, phase, now):
    if not isinstance(evidence, dict) or not public_url(evidence.get("sourceUrl")):
        return None
    if evidence.get("reason") == "TERMINATED_BEFORE_PERIOD":
        return normalize_lifecycle_evidence(evidence, code, phase["targetPeriodEnd"], china_date(now).isoformat())
    if evidence.get("reason") != "ESTABLISHED_AFTER_PERIOD":
        return None
    established = date(evidence.get("date"))
    if (evidence.get("fundCode") == code and evidence.get("periodEnd") == phase["targetPeriodEnd"]
            and established is not None and date(phase["targetPeriodEnd"]) < established <= china_date(now)
            and urlparse(evidence["sourceUrl"]).path.endswith(f"jbgk_{code}.html")):
        return {key: evidence[key] for key in ("reason", "date", "sourceUrl", "periodEnd", "fundCode")}
    return None


def valid_not_applicable(evidence, code, phase, now):
    return clean_not_applicable(evidence, code, phase, now) is not None


def exemption_counts(states):
    return {"establishedAfterPeriodFunds": sum(item["status"] == "not-applicable"
                and item.get("notApplicableEvidence", {}).get("reason") == "ESTABLISHED_AFTER_PERIOD" for item in states),
            "terminatedBeforePeriodFunds": sum(item["status"] == "not-applicable"
                and item.get("notApplicableEvidence", {}).get("reason") == "TERMINATED_BEFORE_PERIOD" for item in states)}


def fair_lane(codes, states):
    """Rotate institutions and prefer products with fewer prior attempts."""
    visits = {}
    for state in states.values():
        company = state["companyCode"] or "~unmapped"
        visits[company] = visits.get(company, 0) + state["totalAttempts"]
        visits[company] += state["quality"]["totalAttempts"] + state["enrichment"]["totalAttempts"]
        visits[company] += sum(item["attempts"] for item in state.get("reportLocatorAttempts", {}).values())
        visits[company] += sum(item['attempts'] for item in state.get('strategyClaimsAttempts', {}).values())
    queues = {}
    def order(code):
        state = states[code]
        # Capacity deferrals are scheduling observations, not source attempts.
        # Rotate them behind equally attempted work without an overnight lock.
        activity = [value for value in (instant(state.get("lastAttemptAt")), instant(state.get("lastDeferredAt"))) if value]
        locator_rows = state.get("reportLocatorAttempts", {}).values()
        activity.extend(value for item in locator_rows if (value := instant(item.get("lastAttemptAt"))))
        activity.extend(value for item in state.get('strategyClaimsAttempts', {}).values() if (value := instant(item.get('lastAttemptAt'))))
        if instant(state.get('strategyClaimsLastDeferredAt')):
            activity.append(instant(state['strategyClaimsLastDeferredAt']))
        return (state["totalAttempts"] + state["quality"]["totalAttempts"] + state["enrichment"]["totalAttempts"]
                + sum(item["attempts"] for item in state.get("reportLocatorAttempts", {}).values())
                + sum(item['attempts'] for item in state.get('strategyClaimsAttempts', {}).values()),
                max(activity).timestamp() if activity else float('-inf'), code)
    for code in sorted(codes, key=order):
        queues.setdefault(states[code]["companyCode"] or "~unmapped", deque()).append(code)
    companies = deque(sorted(queues, key=lambda company: (visits[company], company)))
    ordered = deque()
    while companies:
        company = companies.popleft()
        ordered.append(queues[company].popleft())
        if queues[company]:
            companies.append(company)
    return ordered


def fair_research_selection(states, source_codes, quality_codes, enrich_codes, capacity, locator_codes=()):
    """Reserve capacity for report repair without stalling the first pass.

    With every lane populated, each ten slots contain six new products, two
    report repairs, one source retry and one price repair. Empty lanes yield
    their capacity to the remaining work; per-product retry limits still apply.
    """
    locator_codes = set(locator_codes)
    lanes = {
        "fresh": fair_lane([code for code in source_codes if states[code]["status"] == "pending" and code not in locator_codes], states),
        "retry": fair_lane([code for code in source_codes if states[code]["status"] != "pending" and code not in locator_codes], states),
        "locators": fair_lane(locator_codes, states),
        "quality": fair_lane(quality_codes, states),
        "prices": fair_lane(set(enrich_codes) - set(quality_codes), states),
    }
    rotation = ("fresh", "quality", "fresh", "fresh", "retry", "fresh", "quality", "fresh", "prices", "fresh")
    if locator_codes:
        rotation = ("fresh", "quality", "fresh", "locators", "retry", "fresh", "quality", "fresh", "prices", "fresh")
    selected, seen = [], set()
    while len(selected) < capacity and any(lanes.values()):
        for lane in rotation:
            queue = lanes[lane]
            while queue and queue[0] in seen:
                queue.popleft()
            if queue and len(selected) < capacity:
                code = queue.popleft()
                selected.append(code)
                seen.add(code)
    return selected


SOURCE_REPAIR_LANE = 'source-repair-no-market-v1'


def source_repair_mode(config):
    """An explicit source lane cannot contain any market-request work."""
    if 'collectionLane' not in config:
        return False
    if (config['collectionLane'] != SOURCE_REPAIR_LANE
            or any(type(config.get(key)) is not int or config[key] != 0
                   for key in ('maxPriceSymbols', 'maxPriceSeconds'))
            or config.get('traversal', {}).get('enrichmentCodes', [])
            or any(any(key in item for key in ('priceOnly', 'priceTargets')) for item in config.get('funds', []))):
        raise TraversalError('INVALID_SOURCE_REPAIR_LANE')
    return True


def eligible_work(states, config, lane_scoped=False):
    """Use the same nonoverlapping task lanes for planning and coverage audits."""
    enrich_codes = set(enrichment_eligible(states))
    quality_codes = set(quality_eligible(states)) if quality_enabled(config) else set()
    for code in list(enrich_codes):
        if states[code].get('priceBindingNeedsReport'):
            enrich_codes.discard(code)
            if states[code]['quality']['attempts'] < QUALITY_MAX_ATTEMPTS:
                quality_codes.add(code)
    # A report with only missing quotes belongs to the price repair lane. Keep
    # its known missing securities as explicit targets rather than recollecting
    # every security under the broader report repair budget.
    quality_codes -= {code for code in quality_codes & enrich_codes
                      if states[code]["quality"]["missingFields"] == ["prices"]
                      and states[code]["enrichment"]["missingStocks"]}
    source_codes = sorted(set(eligible(states)) | set(locator_eligible(states)))
    if lane_scoped and source_repair_mode(config):
        # A missing price alone must not repeatedly consume a report attempt.
        quality_codes = {code for code in quality_codes
                         if set(states[code]['quality']['missingFields']) - {'prices'}}
        enrich_codes = set()
    return source_codes, quality_codes, enrich_codes


def quarter_universe_audit(catalog, config):
    """Keep known-directory work separate from an audited historical universe.

    No historical adapter in this release certifies all period-end products and
    share classes. A caller-supplied boolean cannot manufacture that certificate.
    """
    raw = catalog.get("quarterUniverse", {}) if isinstance(catalog, dict) else {}
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1 or raw.get("targetPeriodEnd") != config.get("targetPeriodEnd"):
        raw = {}
    return {"schemaVersion": 1, "targetPeriodEnd": config.get("targetPeriodEnd"),
            "required": config.get("requireHistoricalUniverse") is True,
            "historicalUniverseVerified": False,
            "status": raw.get("status") if raw.get("status") in {"current-directory-only", "supplemented-unverified"} else "unchecked",
            "currentDirectoryComplete": raw.get("currentDirectoryComplete") is True,
            "historicalParseStatus": raw.get("historicalParseStatus") if raw.get("historicalParseStatus") in {"unchecked", "partial", "complete"} else "unchecked",
            "historicalSourceCount": counter(raw.get("historicalSourceCount")),
            "historicalAddedFundCount": counter(raw.get("historicalAddedFundCount")),
            "recordUnit": "fund-share-code"}


def completion_audit(states, config, now, directory_pending=False, universe=None):
    """Small public counters for review; overlapping dimensions are explicit.

    No absence or exhausted attempt is converted into a completed observation.
    Share-code coverage and executable representative-product tasks have
    different units because source-proven share classes share a repair task.
    """
    rows = list(states.values())
    applicable = [item for item in rows if item["status"] != "not-applicable"]
    source, quality, enrichment = eligible_work(states, config)
    locators = set(locator_eligible(states))
    prices = set(enrichment) - set(quality)
    claims_ready = set(claims_eligible(states, config, set(source) | set(quality) | set(enrichment)))
    ready = set(source) | set(quality) | prices | claims_ready
    failed = [item for item in rows if item["status"] == "failed"]
    errors = {}
    for item in failed:
        code = item.get("errorCode") or "FUND_FAILED"
        errors[code] = errors.get(code, 0) + 1
    complete = [item for item in applicable if item["status"] in SUCCESS
                and (not quality_enabled(config) or item["quality"]["status"] == "complete")]
    complete_codes = {item["code"] for item in complete}
    unresolved_tasks = [item for item in applicable if item["code"] not in complete_codes
                        and item["attribution"] != "portfolio-evidence" and item["representativeCode"] == item["code"]]
    scheduled = {item["code"] for item in unresolved_tasks if instant(item.get("nextRetryAt"))
                 and instant(item["nextRetryAt"]) > now}
    actionable = ready | scheduled
    attention = [item for item in unresolved_tasks if item["code"] not in actionable]
    field_states = {key: {value: sum(item["quality"]["fields"][key] == value for item in applicable)
                          for value in sorted(QUALITY_VALUES)} for key in QUALITY_FIELDS}
    universe = universe or quarter_universe_audit({}, config)
    known_complete = len(complete) == len(applicable) and not directory_pending
    historical_pending = universe["required"] and not universe["historicalUniverseVerified"]
    return {"schemaVersion": 1, "asOf": now.isoformat(), "coverageUnit": "fund-share-code",
            "taskUnit": "representative-product-code", "directoryPending": bool(directory_pending),
            "knownDirectoryComplete": known_complete, "quarterUniverse": universe,
            "historicalUniverseReviewRequired": historical_pending,
            "complete": known_complete and not historical_pending,
            "sourceCoverage": {"totalFunds": len(rows), "pendingFunds": sum(item["status"] == "pending" for item in rows),
                "failedFunds": len(failed), "observedFunds": sum(item["status"] in SUCCESS for item in rows),
                "notApplicableFunds": len(rows) - len(applicable),
                **exemption_counts(rows),
                "portfolioEvidenceFunds": sum(item["attribution"] == "portfolio-evidence" for item in rows)},
            "sourceFailures": {"byErrorCode": dict(sorted(errors.items())),
                "recoverableErrorFunds": sum(count for code, count in errors.items() if code in RECOVERABLE_ERRORS),
                "parserOrUnsupportedErrorFunds": sum(count for code, count in errors.items() if code in PARSER_ERRORS),
                "otherErrorFunds": sum(count for code, count in errors.items() if code not in RECOVERABLE_ERRORS | PARSER_ERRORS)},
            "strategyClaims": claims_audit(states, config),
            "research": {"enabled": quality_enabled(config), "applicableFunds": len(applicable),
                "completeFunds": len(complete), "incompleteFunds": len(applicable) - len(complete),
                "sixFieldsCompleteFunds": sum(item["status"] in SUCCESS and all(item["quality"]["fields"][key] in QUALITY_ACCEPTED for key in SIX_QUALITY_FIELDS) for item in applicable),
                "fieldStates": field_states,
                "exhaustedFunds": sum(item["status"] in SUCCESS and item["quality"]["status"] == "exhausted" for item in applicable),
                "exhaustedByMissingField": {key: sum(item["status"] in SUCCESS and item["quality"]["status"] == "exhausted"
                    and item["quality"]["fields"][key] not in QUALITY_ACCEPTED for item in applicable) for key in QUALITY_FIELDS}},
            "work": {"readyTasks": len(ready), "freshSourceTasks": sum(states[code]["status"] == "pending" and code not in locators for code in source),
                "sourceRetryTasks": sum(states[code]["status"] != "pending" and code not in locators for code in source),
                "reportLocatorTasks": len(locators),
                "reportRepairTasks": len(quality), "priceRepairTasks": len(prices),
                "managerRepairTasks": sum(manager_only_repair(states[code]["quality"]) for code in quality),
                "strategyRepairTasks": sum(strategy_only_repair(states[code]["quality"]) for code in quality),
                "strategyClaimsTasks": len(claims_ready),
                "scheduledTasks": len(scheduled - ready), "requiresReviewTasks": len(attention),
                "requiresReviewSourceTasks": sum(item["status"] not in SUCCESS for item in attention),
                "requiresReviewResearchTasks": sum(item["status"] in SUCCESS for item in attention)},
            "countingNotes": ["Source coverage states are mutually exclusive and sum to the directory share-code count.",
                "Field-state counts use applicable share codes; gaps across different fields overlap and must not be added.",
                "Task queues count representative product codes; source-proven share aliases do not create duplicate repair tasks.",
                "Manager repair tasks are a subset of report repair tasks and have a separate bounded attempt budget.",
                "Strategy repair tasks are a subset of report repair tasks; section extraction has an independent versioned budget.",
                "Requires-review tasks have neither eligible work nor a future retry; they remain incomplete."]}


def plan_batches(catalog, previous, config, shards=4, batch_size=25, now=None, retained=()):
    current = utc_now(now)
    phase = phase_for(config)
    if not isinstance(shards, int) or not isinstance(batch_size, int) or not 1 <= shards <= 32 or not 1 <= batch_size <= 100:
        raise TraversalError("INVALID_ARGUMENTS")
    companies, records, states = base_states(catalog, previous, phase, current, config)
    from price_patches import binding_for
    price_bindings = {}
    def captured_funds():
        for fund in retained or ():
            binding = binding_for(fund, phase)
            if binding:
                price_bindings[fund['code']] = binding
            yield fund
    hydrate_quality(states, captured_funds(), phase, current)
    apply_retry_policy(states, config, current)
    selected = []
    source_codes, quality_codes, enrich_codes = eligible_work(states, config, lane_scoped=True)
    locator_codes = set(locator_eligible(states))
    # Existing quote repair state is insufficient without the exact retained
    # source/universe. Recover that source through the report lane instead.
    price_only = {}
    for code in set(enrich_codes) - set(quality_codes):
        binding = price_bindings.get(code)
        if binding and binding['holdingsIdentity'] == states[code]['enrichment'].get('holdingsIdentity'):
            price_only[code] = binding
        else:
            enrich_codes.discard(code)
            if states[code]['quality']['attempts'] < QUALITY_MAX_ATTEMPTS:
                quality_codes.add(code)
    claims_codes = set(claims_eligible(states, config, set(source_codes) | quality_codes | enrich_codes))
    capacity = shards * batch_size
    other_work = bool(source_codes or quality_codes or enrich_codes)
    # At most a tenth of a populated campaign is reserved for rule scans. A
    # single-slot first pass keeps its source slot; claims remain visible/ready.
    allowance = min(claims_quota(config), max(0, capacity // 10) if other_work else capacity)
    claims_selected = list(fair_lane(claims_codes, states))[:allowance]
    if quality_enabled(config):
        selected = fair_research_selection(states, source_codes, quality_codes, enrich_codes, capacity - len(claims_selected), locator_codes)
    else:
        for pool in (quality_codes, enrich_codes, source_codes):
            queue = fair_lane([code for code in pool if code not in selected], states)
            while queue and len(selected) < capacity - len(claims_selected):
                selected.append(queue.popleft())
    # Put the small independent work first in a batch so report timeouts do not
    # repeatedly defer it; the reserved count cannot crowd out the first pass.
    selected = claims_selected + selected
    batches, matrix = {}, []
    for index, start in enumerate(range(0, len(selected), batch_size)):
        batch = {key: value for key, value in config.items() if key not in {"funds", "traversal", "reportLocatorCandidates",
                                                                        "reportLocatorUniverseCodes", "reportLocatorRegistry"}}
        batch_codes = selected[start:start + batch_size]
        batch["funds"] = [{**records[code], **({"priceTargets": states[code]["enrichment"]["missingStocks"]}
                          if code in enrich_codes and code not in quality_codes and states[code]["enrichment"]["missingStocks"] else {}),
                           **({'priceOnly': price_only[code]} if code in price_only and code not in quality_codes else {}),
                           **({'strategyClaimsOnly': states[code]['strategyClaimsTask']} if code in claims_selected else {}),
                           **({'reportLocator': states[code]['reportLocator']} if states[code].get('reportLocator') and code not in claims_selected else {}),
                           **({'reportLocatorOnly': True} if code in locator_codes else {})} for code in batch_codes]
        batch["traversal"] = {"phase": phase, "batchId": str(index), "plannedAt": current.isoformat(),
                              "enrichmentCodes": [code for code in batch_codes if code in enrich_codes and code not in quality_codes],
                              "qualityCodes": [code for code in batch_codes if code in quality_codes],
                              "managerCodes": [code for code in batch_codes if code in quality_codes and manager_only_repair(states[code]["quality"])],
                              "strategyCodes": [code for code in batch_codes if code in quality_codes and strategy_only_repair(states[code]["quality"])],
                              "strategyClaimsCodes": [code for code in batch_codes if code in claims_selected],
                              "reportLocatorCodes": [code for code in batch_codes if code in locator_codes]}
        filename = f"batch-{index}.json"
        batches[filename] = batch
        matrix.append({"id": str(index), "config": filename})
    passes = previous.get("directoryPasses", 0) if isinstance(previous, dict) and previous.get("phase") == phase else 0
    passes = passes if isinstance(passes, int) else 0
    continuing = bool(selected)
    return {"batches": batches, "matrix": matrix,
            "meta": {"hasWork": bool(selected), "continueRun": continuing, "selectedCount": len(selected),
                     "freshCount": sum(code in source_codes and states[code]["status"] == "pending" and code not in locator_codes for code in selected),
                     "retryCount": sum(code in source_codes and states[code]["status"] != "pending" and code not in locator_codes for code in selected),
                     "reportLocatorCount": sum(code in locator_codes for code in selected),
                     "strategyClaimsCount": len(claims_selected), "strategyClaimsReadyCount": len(claims_codes),
                     "enrichmentCount": sum(code in enrich_codes and code not in quality_codes for code in selected),
                     "managerCount": sum(code in quality_codes and manager_only_repair(states[code]["quality"]) for code in selected),
                     "strategyCount": sum(code in quality_codes and strategy_only_repair(states[code]["quality"]) for code in selected),
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
    if not published or published < date(report["periodEnd"]) or published > china_date(now) or published > china_date(observed):
        return None
    result = {"sourceUrl": report["sourceUrl"], "periodEnd": report["periodEnd"],
              "publishedAt": report["publishedAt"], "observedAt": fund["retrievedAt"]}
    if fund.get("status") == "no-equities":
        evidence = fund.get("noEquitiesEvidence") or report.get("noEquitiesEvidence") or {}
        text = evidence.get("text", "") if isinstance(evidence, dict) else ""
        if (not no_equities_report_matches(report, evidence) or report.get("holdings")
                or any(row.get("current") is not None for row in fund.get("rows", []) if isinstance(row, dict))):
            return None
        if evidence.get("evidenceType") == "exhaustive-nonstock-assets":
            from nonstock_assets import normalize_nonstock_assets
            result["noEquities"] = normalize_nonstock_assets(evidence)
        else:
            result["noEquities"] = {"confirmed": True, "sourceUrl": evidence["sourceUrl"], "page": evidence["page"], "text": text[:600]}
        if evidence.get("evidenceType") in ("zero-stock-balance-sheet", "explicit-no-stock-detail"):
            result["noEquities"].update({key: evidence[key] for key in ("evidenceType", "kind", "periodEnd", "publishedAt")})
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
    manager_codes = set(config.get("traversal", {}).get("managerCodes", []))
    strategy_codes = set(config.get("traversal", {}).get("strategyCodes", []))
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
            coverage = price_coverage(fund, current)
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
        if 'strategyClaimsOnly' in item:
            supplied = next((value for value in (snapshot.get('strategyClaimsReceipts', []) if snapshot_ok else [])
                             if isinstance(value, dict) and value.get('code') == code), None)
            rows.append(claims_status_row(item, phase, current, config.get('traversal', {}).get('plannedAt'),
                snapshot.get('retrievedAt', current.isoformat()) if snapshot_ok else current.isoformat(), supplied,
                deferred=valid_deferred(results.get(code, {}))))
            continue
        if 'priceOnly' in item:
            patch = next((p for p in (snapshot.get('pricePatches', []) if snapshot_ok else [])
                          if isinstance(p, dict) and p.get('code') == code), None)
            rows.append(price_patch_status(item, patch, phase))
            continue
        declared = results.get(code, {})
        proof = observed.get(code)
        if valid_deferred(declared):
            row = {"code": code, "companyCode": item.get("companyCode") or item.get("companyId"),
                   "status": "deferred-budget", "attempted": False, "errorCode": "BATCH_DEADLINE_REACHED"}
        elif code in exemptions and declared.get("status") == "not-applicable":
            row = {"code": code, "companyCode": item.get("companyCode") or item.get("companyId"),
                   "status": "not-applicable", "errorCode": None,
                   "notApplicableEvidence": clean_not_applicable(exemptions[code], code, phase, current)}
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
                # Keep an exact announcement/target absence visible instead of
                # losing it behind the builder's generic missing-report wrapper.
                source_diagnostics = [entry.get("errorCode") for entry in snapshot.get("errors", [])
                                      if isinstance(entry, dict) and entry.get("code") == code
                                      and entry.get("stage") in {"target-report", "announcements"}
                                      and entry.get("errorCode") in RECOVERABLE_ERRORS]
                error = diagnostics[0] if diagnostics else source_diagnostics[0] if source_diagnostics else error
            row = {"code": code, "companyCode": item.get("companyCode") or item.get("companyId"), "status": "failed",
                   "errorCode": error if error in ERROR_CODES else "FUND_FAILED" if snapshot_ok else "BATCH_FAILED"}
        row["enrichmentAttempt"] = code in enrich_codes
        row["qualityAttempt"] = code in quality_codes
        row["managerAttempt"] = code in manager_codes
        row["managerTableCoverageVersion"] = MANAGER_TABLE_COVERAGE_VERSION
        row["strategyAttempt"] = code in strategy_codes
        row["strategySectionCoverageVersion"] = STRATEGY_SECTION_COVERAGE_VERSION
        if code in qualities:
            row["reportQuality"] = qualities[code]
        if item.get('reportLocatorOnly') is True:
            locator = normalized_locator(item['reportLocator'], code, phase, current)
            supplied = next((value for value in (snapshot.get('reportLocatorReceipts', []) if snapshot_ok else [])
                             if isinstance(value, dict) and value.get('code') == code), None)
            receipt = locator_receipt(supplied, locator, current)
            if receipt and (instant(receipt['attemptedAt']) > current
                            or planned and instant(receipt['attemptedAt']) < planned):
                receipt = None
            row.update({'reportLocatorOnly': True, 'reportLocator': locator, 'reportLocatorReceipt': receipt,
                        'reportLocatorAttempt': bool(receipt), 'attempted': bool(receipt)})
            if not receipt or row['status'] == 'not-applicable' or (row['status'] in SUCCESS
                    and not locator_report_matches(row.get('reportEvidence'), locator, receipt)):
                row = {key: value for key, value in row.items() if key not in {'reportEvidence', 'reportQuality', 'priceCoverage', 'notApplicableEvidence'}}
                row['status'] = 'deferred-budget' if not receipt and valid_deferred(declared) else 'failed'
                row['errorCode'] = ('BATCH_DEADLINE_REACHED' if row['status'] == 'deferred-budget'
                                    else 'REPORT_LOCATOR_NOT_ATTEMPTED' if not receipt else 'REPORT_IDENTITY_MISMATCH')
            elif row['status'] == 'deferred-budget':
                row.update({'status': 'failed', 'errorCode': 'FUND_FAILED'})
        rows.append(row)
    return {"schemaVersion": 1, "phase": phase, "attemptedAt": current.isoformat(), "funds": rows, "portfolioAliases": aliases}


def valid_deferred(item):
    return (isinstance(item, dict) and item.get("status") == "deferred-budget" and item.get("attempted") is False
            and item.get("errorCode") == "BATCH_DEADLINE_REACHED")


def valid_event_evidence(evidence, status, phase, attempted_at):
    if not isinstance(evidence, dict) or not public_url(evidence.get("sourceUrl")) or evidence.get("periodEnd") != phase["targetPeriodEnd"]:
        return False
    published, observed = date(evidence.get("publishedAt")), instant(evidence.get("observedAt"))
    if not published or published < date(phase["targetPeriodEnd"]) or not observed or published > china_date(observed) or observed > attempted_at:
        return False
    if status == "no-equities":
        noeq = evidence.get("noEquities") or {}
        if not isinstance(noeq, dict) or noeq.get("confirmed") is not True or noeq.get("sourceUrl") != evidence["sourceUrl"] or not evidence_page(noeq.get("page")):
            return False
        if noeq.get("evidenceType") in ("empty-stock-detail-section", "zero-stock-balance-sheet", "explicit-no-stock-detail", "exhaustive-nonstock-assets") and any(noeq.get(key) != evidence[key] for key in ("periodEnd", "publishedAt")):
            return False
        return no_equities_content(noeq)
    return status == "complete"


def valid_status(document, config, now=None):
    """Validate a saved attempt before reusing its event time during recovery."""
    current = utc_now(now)
    phase = phase_for(config)
    if (not isinstance(document, dict) or document.get("kind") == "batch-status-input-error"
            or document.get("schemaVersion") != 1 or document.get("phase") != phase):
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
    manager_codes = set(config.get("traversal", {}).get("managerCodes", []))
    strategy_codes = set(config.get("traversal", {}).get("strategyCodes", []))
    locator_codes = set(config.get("traversal", {}).get("reportLocatorCodes", []))
    claims_codes = set(config.get('traversal', {}).get('strategyClaimsCodes', []))
    if (claims_codes != {item['code'] for item in config['funds'] if 'strategyClaimsOnly' in item}
            or claims_codes.intersection(enrich_codes | quality_codes | manager_codes | strategy_codes | locator_codes)):
        return False
    if not manager_codes.issubset(quality_codes) or not strategy_codes.issubset(quality_codes):
        return False
    if (not locator_codes.issubset(wanted) or locator_codes.intersection(enrich_codes | quality_codes | manager_codes | strategy_codes)
            or locator_codes != {item['code'] for item in config['funds'] if item.get('reportLocatorOnly') is True}):
        return False
    for item in rows:
        if not isinstance(item, dict) or item.get("code") not in wanted or item["code"] in seen:
            return False
        seen.add(item["code"])
        planned_item = next(row for row in config['funds'] if row['code'] == item['code'])
        if 'strategyClaimsOnly' in planned_item:
            if any(key in planned_item for key in ('priceOnly', 'reportLocator', 'reportLocatorOnly')):
                return False
            expected = claims_status_row(planned_item, phase, current, config.get('traversal', {}).get('plannedAt'),
                document['attemptedAt'], item.get('strategyClaimsReceipt'), deferred=valid_deferred(item))
            if any(item.get(key) != value for key, value in expected.items()): return False
            if any(key in item for key in ('reportEvidence', 'reportQuality', 'priceCoverage', 'notApplicableEvidence')): return False
            continue
        if item.get('strategyClaimsOnly') or item.get('strategyClaimsReceipt') or item.get('strategyClaimsAttempt'):
            return False
        if planned_item.get('reportLocatorOnly') is True and 'priceOnly' in planned_item:
            return False
        if 'priceOnly' in planned_item:
            if (item.get('priceOnly') is not True or item.get('status') != 'failed'
                    or item.get('companyCode') != wanted[item['code']]
                    or item.get('qualityAttempt') is not False or item.get('managerAttempt') is not False
                    or item.get('strategyAttempt') is not False):
                return False
            keys = item.get('attemptedStockKeys')
            began = instant(item.get('priceAttemptedAt'))
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys) or item.get('attempted') is not bool(keys) or item.get('enrichmentAttempt') is not bool(keys): return False
            if keys and (not valid_price_target(planned_item, phase) or item.get('priceBinding') != planned_item['priceOnly']
                         or not began or began > attempted or planned and began < planned
                         or any(key not in {stock_key(x) for x in planned_item['priceTargets']} for key in keys)
                         or len(set(keys)) != len(keys)): return False
            if not keys and item.get('errorCode') not in ('PRICE_BUDGET_REACHED', 'PRICE_BINDING_INVALID'): return False
            continue
        if planned_item.get('reportLocatorOnly') is True:
            locator = normalized_locator(planned_item['reportLocator'], item['code'], phase, current)
            receipt = locator_receipt(item.get('reportLocatorReceipt'), locator, current)
            if (not locator or item.get('reportLocatorOnly') is not True or item.get('reportLocator') != locator
                    or item.get('reportLocatorAttempt') is not bool(receipt) or item.get('attempted') is not bool(receipt)
                    or item.get('reportLocatorReceipt') != receipt or item.get('companyCode') != wanted[item['code']]
                    or any(item.get(key) is not False for key in ('qualityAttempt', 'managerAttempt', 'strategyAttempt', 'enrichmentAttempt'))):
                return False
            if receipt and (instant(receipt['attemptedAt']) > attempted or planned and instant(receipt['attemptedAt']) < planned):
                return False
            if not receipt:
                if not (valid_deferred(item) or item.get('status') == 'failed' and item.get('errorCode') == 'REPORT_LOCATOR_NOT_ATTEMPTED'):
                    return False
                if any(key in item for key in ('reportEvidence', 'reportQuality', 'priceCoverage', 'notApplicableEvidence')):
                    return False
                continue
            if item.get('status') == 'not-applicable' or (item.get('status') in SUCCESS
                    and not locator_report_matches(item.get('reportEvidence'), locator, receipt)):
                return False
        elif item.get('reportLocatorOnly') or item.get('reportLocatorReceipt') or item.get('reportLocatorAttempt'):
            return False
        if item.get("companyCode") != wanted[item["code"]] or item.get("status") not in TERMINAL | {"failed", "deferred-budget"}:
            return False
        if bool(item.get("enrichmentAttempt")) != (item["code"] in enrich_codes):
            return False
        if bool(item.get("qualityAttempt")) != (item["code"] in quality_codes):
            return False
        if bool(item.get("managerAttempt")) != (item["code"] in manager_codes):
            return False
        if bool(item.get("strategyAttempt")) != (item["code"] in strategy_codes):
            return False
        if item.get("reportQuality") is not None and not valid_quality(item["reportQuality"], phase):
            return False
        if item.get("priceCoverage") is not None and not (valid_price_coverage(item["priceCoverage"]) or readable_legacy_price_coverage(item["priceCoverage"])):
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
    phase = phase_for(config)
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
                or not (valid_price_coverage(item.get("priceCoverage")) or readable_legacy_price_coverage(item.get("priceCoverage")))):
            continue
        observations[item["code"]] = item
    reconciled = []
    patch_receipts = {item.get('code'): item for item in receipt.get('pricePatches', []) if isinstance(item, dict)}
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
            if row.get('strategyClaimsOnly') is True:
                # Requests and original clocks survive rejected patches. Only
                # strict hydration of the merger's retained report marks scans.
                result['funds'].append(row)
                continue
            if row.get('reportLocatorOnly') is True and row.get('reportLocatorAttempt') is not True:
                # No started PDF request exists to reconcile. Missing merger
                # output cannot turn a capacity deferral into an attempt.
                result['funds'].append(row)
                continue
            if row.get('priceOnly') is True:
                proof = patch_receipts.get(row.get('code'))
                identity_ok = (valid_price_binding(row.get('priceBinding'), phase, row.get('code'))
                               and proof and proof.get('accepted') is True and proof.get('binding') == row.get('priceBinding')
                               and observation and observation.get('status') in SUCCESS
                               and observation.get('priceCoverage', {}).get('holdingsIdentity') == row.get('priceBinding', {}).get('holdingsIdentity'))
                if identity_ok:
                    row.update({key: observation[key] for key in ('status', 'reportEvidence', 'priceCoverage', 'representativeCode')})
                    row['patchAccepted'] = True
                    row['errorCode'] = None
                else:
                    row['patchAccepted'] = False
                    if proof and proof.get('errorCode') == 'PRICE_BINDING_INVALID': row['errorCode'] = 'PRICE_BINDING_INVALID'
                result['funds'].append(row)
                continue
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


def merge_progress(catalog, previous, config, statuses=(), now=None, retained=(), *, runtime_descriptors=None):
    if runtime_descriptors is not None and not isinstance(runtime_descriptors, dict):
        raise TraversalError("INVALID_ARGUMENTS")
    current = utc_now(now)
    phase = phase_for(config)
    companies, records, states = base_states(catalog, previous, phase, current, config)
    matching_previous = isinstance(previous, dict) and previous.get("schemaVersion") == 1 and previous.get("phase") == phase
    events = []
    for document in statuses:
        if (not isinstance(document, dict) or document.get("kind") == "batch-status-input-error"
                or document.get("schemaVersion") != 1 or document.get("phase") != phase):
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
            if item.get('strategyClaimsOnly') is True:
                task = claims_task(item.get('strategyClaimsTask'), item['code'], phase, current)
                supplied = item.get('strategyClaimsReceipt')
                receipt = claims_receipt(supplied, task,
                    supplied.get('attemptedAt') if isinstance(supplied, dict) else None, document['attemptedAt'], current)
                if (not task or any(item.get(key) is not False for key in ('qualityAttempt', 'managerAttempt', 'strategyAttempt', 'enrichmentAttempt'))
                        or item.get('strategyClaimsAttempt') is not bool(receipt) or item.get('attempted') is not bool(receipt)):
                    continue
                if not receipt:
                    if valid_deferred(item) and (not instant(state.get('strategyClaimsLastDeferredAt')) or attempted > instant(state['strategyClaimsLastDeferredAt'])):
                        state['strategyClaimsLastDeferredAt'] = document['attemptedAt']
                    continue
                history, digest = state['strategyClaimsAttempts'], task['taskDigest']
                prior = history.get(digest, {'attempts': 0, 'receipts': []})
                if (any(instant(row['attemptedAt']) == instant(receipt['attemptedAt']) for row in prior['receipts'])
                        or prior['attempts'] >= STRATEGY_CLAIMS_MAX_ATTEMPTS or state['strategyClaimsHistoryBlocked']
                        or digest not in history and len(history) >= STRATEGY_CLAIMS_MAX_DIGESTS): continue
                receipts = sorted([*prior['receipts'], receipt], key=lambda row: instant(row['attemptedAt']))
                history[digest] = {'attempts': len(receipts), 'receipts': receipts, 'lastAttemptAt': receipts[-1]['attemptedAt']}
                continue
            if item.get('priceOnly') is True:
                if state['status'] not in SUCCESS or item.get('qualityAttempt') or item.get('managerAttempt') or item.get('strategyAttempt'):
                    continue
                if item.get('errorCode') == 'PRICE_BINDING_INVALID': state['priceBindingNeedsReport'] = True
                if item.get('attempted') is not True or item.get('enrichmentAttempt') is not True:
                    continue
                started = instant(item.get('priceAttemptedAt'))
                keys = item.get('attemptedStockKeys')
                binding = item.get('priceBinding')
                if (not valid_price_binding(binding, phase, item['code']) or not started or started > attempted
                        or not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k in binding['stockKeys'] for k in keys)
                        or len(set(keys)) != len(keys)):
                    continue
                extra = state['enrichment']
                if instant(extra.get('lastAttemptAt')) and started <= instant(extra['lastAttemptAt']): continue
                extra['attempts'] = min(3, extra['attempts'] + 1)
                extra['totalAttempts'] += 1
                extra['lastAttemptAt'] = item['priceAttemptedAt']
                extra['status'] = 'exhausted' if extra['attempts'] >= 3 else 'pending'
                extra['errorCode'] = 'ENRICHMENT_FAILED'
                if (item.get('patchAccepted') is True and valid_price_binding(item.get('priceBinding'), phase, item['code'])
                        and valid_event_evidence(item.get('reportEvidence'), status, phase, attempted)
                        and item['priceBinding']['holdingsIdentity'] == extra.get('holdingsIdentity')):
                    apply_coverage(state, item.get('priceCoverage'))
                continue
            if item.get('reportLocatorOnly') is True or 'reportLocatorReceipt' in item or 'reportLocator' in item:
                locator = normalized_locator(item.get('reportLocator'), item['code'], phase, current)
                receipt = locator_receipt(item.get('reportLocatorReceipt'), locator, current)
                if (not locator or item.get('reportLocatorOnly') is not True
                        or any(item.get(key) is not False for key in ('qualityAttempt', 'managerAttempt', 'strategyAttempt', 'enrichmentAttempt'))):
                    continue
                if not receipt:
                    if valid_deferred(item) and (not instant(state.get('lastDeferredAt')) or attempted > instant(state['lastDeferredAt'])):
                        state['lastDeferredAt'] = document['attemptedAt']
                    continue
                began = instant(receipt['attemptedAt'])
                if (item.get('reportLocatorAttempt') is not True or item.get('attempted') is not True
                        or not began or began > attempted):
                    continue
                ledger = state['reportLocatorAttempts']
                prior = ledger.get(locator['digest'], {})
                if (prior.get('attempts', 0) >= REPORT_LOCATOR_MAX_ATTEMPTS
                        or instant(prior.get('lastAttemptAt')) and began <= instant(prior['lastAttemptAt'])
                        or locator['digest'] not in ledger and state.get('reportLocatorHistoryFull')):
                    continue
                success = (status in SUCCESS and valid_event_evidence(item.get('reportEvidence'), status, phase, attempted)
                           and locator_report_matches(item.get('reportEvidence'), locator, receipt))
                ledger[locator['digest']] = {'attempts': prior.get('attempts', 0) + 1, 'lastAttemptAt': receipt['attemptedAt'],
                                             'errorCode': None if success else item.get('errorCode') if item.get('errorCode') in ERROR_CODES else 'FUND_FAILED'}
                state['reportLocatorHistoryFull'] = state.get('reportLocatorHistoryFull') or len(ledger) >= REPORT_LOCATOR_MAX_DIGESTS
                # This is a separate actual-PDF request budget. A failed locator
                # must not advance/reset any legacy lane or refresh its time.
                if not success or state['status'] in TERMINAL:
                    continue
                state.update({'status': status, 'errorCode': None, 'attempted': True,
                              'lastAttemptAt': item['reportEvidence']['observedAt'], 'attribution': 'direct', 'deferredUntil': None})
                if valid_quality(item.get('reportQuality'), phase):
                    state['quality'] = normalize_quality(merge_quality_observation(state['quality'], item['reportQuality']), status, current, code=item['code'])
                else:
                    state['quality'] = normalize_quality(state['quality'], status, current, code=item['code'])
                state['enrichment'] = normalize_enrichment(state['enrichment'], status, current)
                apply_coverage(state, item.get('priceCoverage'))
                accepted_success.add(item['code'])
                continue
            if status == "deferred-budget":
                prior_deferred = instant(state.get("lastDeferredAt"))
                prior_attempt = instant(state.get("lastAttemptAt"))
                if (valid_deferred(item) and (not prior_deferred or attempted > prior_deferred)
                        and (not prior_attempt or attempted > prior_attempt)):
                    state["lastDeferredAt"] = document["attemptedAt"]
                    state["deferredUntil"] = None
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
                              "notApplicableEvidence": clean_not_applicable(item["notApplicableEvidence"], item["code"], phase, attempted)})
                state["quality"] = normalize_quality(None, status, current, code=item["code"])
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
                manager_only = item.get("managerAttempt") is True and manager_only_repair(quality)
                strategy_only = item.get("strategyAttempt") is True and strategy_only_repair(quality)
                if not manager_only and not strategy_only:
                    quality["attempts"] = min(QUALITY_MAX_ATTEMPTS, quality["attempts"] + 1)
                manager_observed = isinstance(item.get("reportQuality"), dict) and item["reportQuality"].get("managerTableCoverageVersion") == MANAGER_TABLE_COVERAGE_VERSION
                manager_attempted = item.get("managerTableCoverageVersion") == MANAGER_TABLE_COVERAGE_VERSION or manager_observed
                if manager_attempted and (quality["fields"]["managers"] not in QUALITY_ACCEPTED or item.get("managerAttempt") is True):
                    quality["managerAttempts"] = min(MANAGER_MAX_ATTEMPTS, quality["managerAttempts"] + 1)
                    quality["managerTotalAttempts"] += 1
                    quality["managerLastAttemptAt"] = document["attemptedAt"]
                strategy_observed = isinstance(item.get("reportQuality"), dict) and item["reportQuality"].get("strategySectionCoverageVersion") == STRATEGY_SECTION_COVERAGE_VERSION
                strategy_attempted = item.get("strategySectionCoverageVersion") == STRATEGY_SECTION_COVERAGE_VERSION or strategy_observed
                if strategy_attempted and (quality["fields"]["strategy"] not in QUALITY_ACCEPTED or item.get("strategyAttempt") is True):
                    quality["strategyAttempts"] = min(STRATEGY_MAX_ATTEMPTS, quality["strategyAttempts"] + 1)
                    quality["strategyTotalAttempts"] += 1
                    quality["strategyLastAttemptAt"] = document["attemptedAt"]
                quality["totalAttempts"] += 1
                state["totalAttempts"] += 1
                state["deferredUntil"] = None
                quality["lastAttemptAt"] = document["attemptedAt"]
                state["lastAttemptAt"] = document["attemptedAt"]
                state["attempted"] = True
                if valid_quality(item.get("reportQuality"), phase):
                    quality.update(merge_quality_observation(quality, item["reportQuality"]))
                state["quality"] = normalize_quality(quality, state["status"], current, code=item["code"])
                if status in SUCCESS:
                    state['priceBindingNeedsReport'] = False
                    apply_coverage(state, item.get("priceCoverage"))
                    accepted_success.add(item["code"])
                # Failed PDF enrichment must never erase a valid holding report.
                continue
            if state["status"] in SUCCESS and item.get("enrichmentAttempt") is True:
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
                if status in SUCCESS:
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
                state["quality"] = normalize_quality(merge_quality_observation(state["quality"], item["reportQuality"]), status, current, code=item["code"])
            if status == "failed":
                state.update({"status": "failed", "attempts": min(2, state["attempts"] + 1),
                              "errorCode": item.get("errorCode") if item.get("errorCode") in ERROR_CODES else "FUND_FAILED"})
            else:
                state.update({"status": status, "errorCode": None})
                state["enrichment"] = normalize_enrichment(state["enrichment"], status, current)
                state["quality"] = normalize_quality(state["quality"], status, current, code=item["code"])
                if status in SUCCESS:
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
            manager_attempts = max((states[code]["quality"]["managerAttempts"] for code in codes if code in states), default=0)
            manager_total = max((states[code]["quality"]["managerTotalAttempts"] for code in codes if code in states), default=0)
            strategy_attempts = max((states[code]["quality"]["strategyAttempts"] for code in codes if code in states), default=0)
            strategy_total = max((states[code]["quality"]["strategyTotalAttempts"] for code in codes if code in states), default=0)
            representative_quality = states[representative]["quality"]
            for code in codes:
                if code in states and states[code]["status"] == "not-applicable":
                    continue
                if code in states:
                    states[code]["representativeCode"] = representative
                    states[code]["enrichment"]["attempts"] = portfolio_attempts
                    scoped_quality = quality_for_code(representative_quality, code, states[code]["quality"])
                    states[code]["quality"] = normalize_quality({**scoped_quality, "attempts": quality_attempts,
                                                                 "managerAttempts": manager_attempts, "managerTotalAttempts": manager_total,
                                                                 "strategyAttempts": strategy_attempts, "strategyTotalAttempts": strategy_total}, status, current, code=code)
                if code in states and states[code]["status"] not in SUCCESS:
                    states[code].update({"status": status, "lastAttemptAt": document["attemptedAt"], "errorCode": None,
                                         "attribution": "portfolio-evidence"})
                    states[code]["enrichment"] = normalize_enrichment(states[code]["enrichment"], status, current)
                if code in states and states[code]["status"] in SUCCESS and status in SUCCESS:
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
        exemptions = exemption_counts(members)
        total = company["fundCount"] if company["directoryStatus"] == "complete" else None
        if total is not None:
            total = max(total, len(members))
        pending = max(0, total - complete - noeq - failed - not_applicable) if total is not None else sum(item["status"] == "pending" for item in members)
        last = max((item["lastAttemptAt"] for item in members if item["lastAttemptAt"]), default=None)
        company_rows.append({"code": code, "name": company["name"], "totalFunds": total,
                             "processedFunds": complete + noeq, "withHoldings": complete, "withoutEquities": noeq,
                             "failed": failed, "pending": pending, "notApplicable": not_applicable, "lastAttemptAt": last,
                             "establishedAfterPeriod": exemptions["establishedAfterPeriodFunds"],
                             "terminatedBeforePeriod": exemptions["terminatedBeforePeriodFunds"],
                             "deferredFunds": sum(bool(item.get("deferredUntil")) for item in members),
                             "enrichmentPending": sum(item["attribution"] != "portfolio-evidence" and item["representativeCode"] == item["code"] and item["enrichment"]["status"] == "pending" for item in members),
                             "enrichmentExhausted": sum(item["attribution"] != "portfolio-evidence" and item["representativeCode"] == item["code"] and item["enrichment"]["status"] == "exhausted" for item in members),
                             "researchComplete": sum(item["status"] in SUCCESS and item["quality"]["status"] == "complete" for item in members),
                             "directoryStatus": company["directoryStatus"]})
    all_states = sorted(states.values(), key=lambda state: state["code"])
    processed = sum(state["status"] in SUCCESS for state in all_states)
    failed = sum(state["status"] == "failed" for state in all_states)
    not_applicable = sum(state["status"] == "not-applicable" for state in all_states)
    continuing = bool(eligible(states) or locator_eligible(states) or enrichment_eligible(states) or claims_eligible(states, config)
                      or quality_enabled(config) and quality_eligible(states))
    source_continuing = None
    if source_repair_mode(config):
        source, quality, _ = eligible_work(states, config, lane_scoped=True)
        source_continuing = bool(source or quality or claims_eligible(states, config, set(source) | quality))
    enrichment_states = [state["enrichment"] for state in all_states if state["status"] in SUCCESS and state["attribution"] != "portfolio-evidence" and state["representativeCode"] == state["code"]]
    research_complete = sum(state["status"] in SUCCESS and state["quality"]["status"] == "complete" for state in all_states)
    six_fields_complete = sum(state["status"] in SUCCESS and all(state["quality"]["fields"][key] in QUALITY_ACCEPTED for key in SIX_QUALITY_FIELDS) for state in all_states)
    research_pending = sum(state["status"] in SUCCESS and state["quality"]["status"] == "pending" for state in all_states)
    research_exhausted = sum(state["status"] in SUCCESS and state["quality"]["status"] == "exhausted" for state in all_states)
    applicable = len(records) - not_applicable
    universe = quarter_universe_audit(catalog, config)
    audit = completion_audit(states, config, current, directory_incomplete(companies, records), universe)
    completed = audit["complete"]
    directory_retry = midnight(current.date() + dt.timedelta(days=1)) if directory_incomplete(companies, records) and passes < 12 else None
    next_retry = min((value for value in [directory_retry, *(state.get("nextRetryAt") for state in all_states)] if value), default=None)
    if runtime_descriptors is not None:
        # Quality-only reconciliation needs these hydrated task descriptors to
        # recompute work eligibility. They are not observations or retry ledgers
        # and never belong in the serialized public progress checkpoint.
        runtime_descriptors.clear()
        runtime_descriptors.update({state['code']: copy.deepcopy({
            key: state.get(key) for key in ('reportLocator', 'strategyClaimsTask')}) for state in all_states})
    return {"schemaVersion": 1, "phase": phase, "updatedAt": current.isoformat(), "collectorVersion": collector_version(config),
            "totalCompanies": len(companies), "totalFunds": len(records), "processedFunds": processed,
            "attemptedFunds": sum(state["attempted"] for state in all_states),
            "completedCompanies": sum(company["directoryStatus"] == "complete" and company["totalFunds"] == company["processedFunds"] + company["notApplicable"] for company in company_rows),
            "pendingFunds": len(records) - processed - failed - not_applicable, "failedFunds": failed,
            "notApplicableFunds": not_applicable,
            **exemption_counts(all_states),
            "deferredFunds": sum(bool(state.get("deferredUntil")) for state in all_states),
            "nextRetryAt": next_retry, "directoryNextRetryAt": directory_retry,
            "retryableFailedFunds": sum(state["status"] == "failed" and state["attempts"] < 2 for state in all_states),
            "reportLocators": {"readyFunds": len(locator_eligible(states)),
                               "attemptedFunds": sum(bool(state['reportLocatorAttempts']) for state in all_states),
                               "attempts": sum(item['attempts'] for state in all_states for item in state['reportLocatorAttempts'].values()),
                               "maxAttemptsPerDigest": REPORT_LOCATOR_MAX_ATTEMPTS,
                               "maxTrackedDigests": REPORT_LOCATOR_MAX_DIGESTS},
            "strategyClaims": claims_audit(states, config),
            "unmappedFunds": sum(state["companyCode"] is None for state in all_states),
            "completionStatus": "complete" if completed else "incomplete-running" if continuing else "incomplete-exhausted",
            "completionAudit": audit, "quarterUniverse": universe,
            **({'sourceRepairContinueRun': source_continuing} if source_continuing is not None else {}),
            "research": {"enabled": quality_enabled(config), "applicableFunds": applicable, "completeFunds": research_complete,
                         "sixFieldsCompleteFunds": six_fields_complete,
                         "incompleteFunds": applicable - research_complete, "pendingFunds": research_pending,
                         'priceConflictFunds': sum(bool(state['enrichment'].get('priceConflicted')) for state in all_states),
                         "exhaustedFunds": research_exhausted, "maxAttempts": QUALITY_MAX_ATTEMPTS,
                         "managerMaxAttempts": MANAGER_MAX_ATTEMPTS,
                         "strategyMaxAttempts": STRATEGY_MAX_ATTEMPTS,
                         "fieldCoverage": {key: sum(state["status"] != "not-applicable" and state["quality"]["fields"][key] in QUALITY_ACCEPTED for state in all_states)
                                           for key in QUALITY_FIELDS}},
            "enrichment": {"pendingFunds": sum(item["status"] == "pending" for item in enrichment_states),
                           "completeFunds": sum(item["status"] == "complete" for item in enrichment_states),
                           "exhaustedFunds": sum(item["status"] == "exhausted" for item in enrichment_states),
                           'conflictFunds': sum(bool(item.get('priceConflicted')) for item in enrichment_states),
                           'conflictingStocks': sum(item.get('priceConflicted', 0) for item in enrichment_states),
                           "attemptedFunds": sum(item["attempts"] > 0 for item in enrichment_states),
                           "attempts": sum(item["attempts"] for item in enrichment_states), "maxAttempts": 3},
            "companies": company_rows,
            # Candidate metadata lives once in the frozen registry. Progress
            # retains only its independent attempt ledger, not per-share copies.
            "funds": [{**{key: value for key, value in state.items() if key not in {'reportLocator', 'strategyClaimsTask'}},
                       "enrichment": compact_enrichment(state["enrichment"])} for state in all_states],
            "directoryPasses": passes,
            "lastDirectoryObservedAt": directory_observed, "continueRun": continuing}


def read_json(path, optional=False):
    path = Path(path)
    if optional and not path.exists():
        return None
    # Keep the publication bounds: only progress checkpoints have a larger
    # allowance. A missing input must not be mistaken for an oversized receipt.
    maximum = 96_000_000 if path.name == "progress.json" else 25_000_000
    byte_length = None
    try:
        before = path.lstat()
        byte_length = before.st_size
        if not stat.S_ISREG(before.st_mode):
            raise JsonReadError(path, "not-regular", byte_length, maximum)
        if byte_length > maximum:
            raise JsonReadError(path, "too-large", byte_length, maximum)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise JsonReadError(path, "changed-input", byte_length, maximum)
            if opened.st_size > maximum:
                raise JsonReadError(path, "too-large", opened.st_size, maximum)
            payload = handle.read(maximum + 1)
        byte_length = len(payload)
        if byte_length > maximum:
            raise JsonReadError(path, "too-large", byte_length, maximum)
        return json.loads(payload.decode("utf-8"))
    except JsonReadError:
        raise
    except FileNotFoundError:
        raise JsonReadError(path, "missing", byte_length, maximum) from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JsonReadError(path, "invalid-json", byte_length, maximum) from None
    except Exception:
        raise JsonReadError(path, "io-error", byte_length, maximum) from None


def read_status_snapshot(path, config):
    """Only genuine absence uses the legacy missing-batch outcome.

    Existing unreadable/oversize/malformed artifacts cannot supply product
    results or request accounting. Keep this stricter contract local to status
    generation; historical progress and planning readers remain unchanged.
    """
    phase = phase_for(config)
    path = Path(path)
    byte_length = None
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise StatusSnapshotError("SNAPSHOT_READ_FAILED") from None
    if not stat.S_ISREG(before.st_mode):
        raise StatusSnapshotError("SNAPSHOT_READ_FAILED")
    byte_length = before.st_size
    if byte_length > MAX_STATUS_SNAPSHOT_BYTES:
        raise StatusSnapshotError("SNAPSHOT_TOO_LARGE", byte_length)
    try:
        # Reject a swapped symlink/device and cap the read even if the file
        # grows after stat. O_NONBLOCK also prevents a replaced FIFO blocking.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise StatusSnapshotError("SNAPSHOT_READ_FAILED", byte_length)
            if opened.st_size > MAX_STATUS_SNAPSHOT_BYTES:
                raise StatusSnapshotError("SNAPSHOT_TOO_LARGE", opened.st_size)
            payload = handle.read(MAX_STATUS_SNAPSHOT_BYTES + 1)
        byte_length = len(payload)
    except StatusSnapshotError:
        raise
    except OSError:
        raise StatusSnapshotError("SNAPSHOT_READ_FAILED", byte_length) from None
    if byte_length > MAX_STATUS_SNAPSHOT_BYTES:
        raise StatusSnapshotError("SNAPSHOT_TOO_LARGE", byte_length)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-key")
            result[key] = value
        return result

    def finite_number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite")
        return result

    def reject_constant(_value):
        raise ValueError("non-finite")

    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_float=finite_number, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise StatusSnapshotError("SNAPSHOT_INVALID_JSON", byte_length) from None
    if (not isinstance(document, dict) or type(document.get("schemaVersion")) is not int
            or document["schemaVersion"] != 1 or document.get("sourceMode") != "actions"
            or not isinstance(document.get("funds"), list)):
        raise StatusSnapshotError("SNAPSHOT_INVALID_SCHEMA", byte_length)
    for key in ("funds", "batchResults", "errors", "pricePatches", "inceptionPatches", "reportLocatorReceipts",
                "strategyClaimsTasks", "strategyClaimsReceipts", "strategyClaimsPatches"):
        if key in document and (not isinstance(document[key], list)
                                or any(not isinstance(item, dict) for item in document[key])):
            raise StatusSnapshotError("SNAPSHOT_INVALID_SCHEMA", byte_length)
    # Earlier snapshots omitted period metadata; retain that compatibility.
    # Explicit metadata, including null, must agree with the frozen batch.
    if any(key in document and document[key] != value for key, value in phase.items()):
        raise StatusSnapshotError("SNAPSHOT_PHASE_MISMATCH", byte_length)
    return document


def status_input_diagnostic(config, error, now=None):
    # Deliberately omit attemptedAt, funds and product counts: no observation or
    # request budget can be reconstructed from an unreadable artifact.
    return {"schemaVersion": 1, "kind": "batch-status-input-error", "status": "failed",
            "phase": phase_for(config), "diagnosedAt": utc_now(now).isoformat(), "errorCode": error.code,
            "snapshotInput": {"state": "present-unreadable", "byteLength": error.byte_length,
                              "maxBytes": MAX_STATUS_SNAPSHOT_BYTES}}


def valid_status_input_diagnostic(document, config, now=None):
    if (not isinstance(document, dict) or set(document) != {"schemaVersion", "kind", "status", "phase", "diagnosedAt", "errorCode", "snapshotInput"}
            or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
            or document["kind"] != "batch-status-input-error" or document["status"] != "failed"
            or document["phase"] != phase_for(config)
            or not isinstance(document["errorCode"], str) or document["errorCode"] not in STATUS_SNAPSHOT_ERRORS):
        return False
    diagnosed = instant(document["diagnosedAt"])
    planned = instant(config.get("traversal", {}).get("plannedAt"))
    if not diagnosed or diagnosed > utc_now(now) + dt.timedelta(minutes=5) or planned and diagnosed < planned:
        return False
    value = document["snapshotInput"]
    if (not isinstance(value, dict) or set(value) != {"state", "byteLength", "maxBytes"}
            or value["state"] != "present-unreadable" or type(value["maxBytes"]) is not int
            or value["maxBytes"] != MAX_STATUS_SNAPSHOT_BYTES
            or value["byteLength"] is not None and (type(value["byteLength"]) is not int or value["byteLength"] < 0)):
        return False
    if document["errorCode"] == "SNAPSHOT_TOO_LARGE":
        return value["byteLength"] is not None and value["byteLength"] > MAX_STATUS_SNAPSHOT_BYTES
    return True


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
    if args.command in {'plan', 'merge'} and any(key in config for key in ('reportLocatorRegistry', 'reportLocatorUniverseCodes')):
        from report_locators import expand_config
        config = expand_config(config)
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
            snapshot = read_status_snapshot(args.snapshot, config)
        except StatusSnapshotError as error:
            atomic_json(args.output, status_input_diagnostic(config, error))
            raise
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
        print(json.dumps({"status": "failed", "errorCode": error.code if isinstance(error, TraversalError) else "TRAVERSAL_FAILED",
                          **({"readDiagnostic": error.diagnostic} if isinstance(error, JsonReadError) else {})}))
        raise SystemExit(1) from None
