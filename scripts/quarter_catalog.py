"""Compose and lock quarter-specific directories without certifying history.

An overlay is normalized input, not a parser for any remote source. Reading all
rows from a source and verifying the whole historical universe are separate.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit, parse_qsl

from publication_guard import content_limit, inspect_content

PERIODS = {"2026Q2": "2026-06-30", "2026Q3": "2026-09-30"}
PARSE_STATUSES = {"unchecked", "partial", "complete"}


def _date(value):
    try: return isinstance(value, str) and dt.date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError): return False


def _stamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return isinstance(value, str) and "T" in value and parsed.tzinfo is not None
    except (AttributeError, TypeError, ValueError): return False


def _label(value, maximum=200):
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum and not re.search(r"[\x00-\x1f\x7f]", value)


def _url(value):
    if not _label(value, 2048): return False
    try:
        url = urlsplit(value); host = url.hostname or ""
        return (url.scheme in {"https", "http"} and "." in host and ":" not in host
                and not re.fullmatch(r"\d+(?:\.\d+){3}", host) and not host.endswith((".local", ".localhost"))
                and not url.username and not url.password and not url.fragment
                and not any(re.search(r"token|key|secret|password|signature|authorization|credential", key, re.I)
                            for key, _ in parse_qsl(url.query)))
    except ValueError: return False


def normalize_overlay(raw, period_end, *, now=None):
    """Whitelist source metadata and row references; never guess an owner."""
    observed_now = dt.datetime.fromisoformat(now.replace('Z', '+00:00')) if isinstance(now, str) else now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(observed_now, dt.datetime) or observed_now.tzinfo is None: raise ValueError("INVALID_OBSERVATION_TIME")
    if (not isinstance(raw, dict) or type(raw.get("schemaVersion")) is not int or raw.get("schemaVersion") != 1 or raw.get("targetPeriodEnd") != period_end
            or raw.get("parseStatus") not in PARSE_STATUSES or not isinstance(raw.get("records"), list)
            or not isinstance(raw.get("evidence"), list) or len(raw["records"]) > 150000 or len(raw["evidence"]) > 100
            or raw["records"] and raw["parseStatus"] == "unchecked"):
        raise ValueError("INVALID_HISTORICAL_OVERLAY")
    evidence, ids = [], set()
    for source in raw["evidence"]:
        if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", source["id"]) or source["id"] in ids
                or not _url(source.get("sourceUrl")) or source.get("effectiveDate") != period_end
                or not _stamp(source.get("retrievedAt"))
                or not re.fullmatch(r"[a-f0-9]{64}", str(source.get("contentHash", "")))
                or source.get("format") not in {"xlsx", "csv", "json", "pdf"}
                or source.get("recordUnit") not in {"fund-share-code", "fund-product-code", "unknown"}):
            raise ValueError("INVALID_HISTORICAL_SOURCE")
        if source.get("publishedAt") is not None and (not _date(source["publishedAt"])
                or source["publishedAt"] < period_end or source["publishedAt"] > source["retrievedAt"][:10]):
            raise ValueError("INVALID_HISTORICAL_SOURCE")
        if source["retrievedAt"][:10] < period_end: raise ValueError("INVALID_HISTORICAL_SOURCE")
        retrieved = dt.datetime.fromisoformat(source["retrievedAt"].replace('Z', '+00:00'))
        if retrieved > observed_now + dt.timedelta(minutes=5): raise ValueError("FUTURE_HISTORICAL_SOURCE")
        clean = {key: source[key] for key in ("id", "sourceUrl", "effectiveDate", "retrievedAt", "contentHash", "format", "recordUnit")}
        clean["publishedAt"] = source.get("publishedAt")
        for key in ("title", "authority", "parserVersion"):
            if source.get(key) is not None:
                if not _label(source[key]): raise ValueError("INVALID_HISTORICAL_SOURCE")
                clean[key] = source[key].strip()
        for key in ("parsedRows", "expectedRows", "unresolvedRows"):
            if source.get(key) is not None:
                if type(source[key]) is not int or not 0 <= source[key] <= 1000000: raise ValueError("INVALID_HISTORICAL_SOURCE")
                clean[key] = source[key]
        evidence.append(clean); ids.add(source["id"])
    records, codes = [], set()
    for row in raw["records"]:
        if (not isinstance(row, dict) or not isinstance(row.get("code"), str) or not re.fullmatch(r"\d{6}", row["code"])
                or row["code"] in codes or not _label(row.get("name")) or not isinstance(row.get("evidence"), list)
                or not 1 <= len(row["evidence"]) <= 20):
            raise ValueError("INVALID_HISTORICAL_RECORD")
        refs = []
        for ref in row["evidence"]:
            if not isinstance(ref, dict) or ref.get("sourceId") not in ids: raise ValueError("INVALID_HISTORICAL_REFERENCE")
            clean = {"sourceId": ref["sourceId"]}
            if ref.get("row") is not None:
                if type(ref["row"]) is not int or not 1 <= ref["row"] <= 1048576 or not _label(ref.get("sheet"), 100):
                    raise ValueError("INVALID_HISTORICAL_REFERENCE")
                clean.update(row=ref["row"], sheet=ref["sheet"].strip())
            if ref.get("page") is not None:
                if type(ref["page"]) is not int or not 1 <= ref["page"] <= 10000: raise ValueError("INVALID_HISTORICAL_REFERENCE")
                clean["page"] = ref["page"]
            if "row" not in clean and "page" not in clean: raise ValueError("INVALID_HISTORICAL_REFERENCE")
            refs.append(clean)
        record = {"code": row["code"], "name": row["name"].strip(), "type": row.get("type") or "未分类", "evidence": refs}
        if not _label(record["type"], 100): raise ValueError("INVALID_HISTORICAL_RECORD")
        if row.get("sourceCompanyName") is not None:
            if not _label(row["sourceCompanyName"]): raise ValueError("INVALID_HISTORICAL_RECORD")
            record["sourceCompanyName"] = row["sourceCompanyName"].strip()
        # Incoming companyCode/companyId and any claimed verified flag are not
        # association evidence and deliberately do not enter the result.
        records.append(record); codes.add(row["code"])
    return {"records": records, "evidence": evidence, "parseStatus": raw["parseStatus"]}


def compose_quarter_catalog(current, quarter, overlay=None, *, legacy=False, now=None):
    if quarter not in PERIODS or not isinstance(current, dict) or current.get("schemaVersion") != 1:
        raise ValueError("INVALID_QUARTER_CATALOG")
    if not isinstance(current.get("funds"), list) or not isinstance(current.get("companies"), list) or not isinstance(current.get("coverage"), dict):
        raise ValueError("INVALID_QUARTER_CATALOG")
    result = copy.deepcopy(current)
    records = {}
    for row in result["funds"]:
        if not isinstance(row, dict) or not isinstance(row.get("code"), str) or not re.fullmatch(r"\d{6}", row["code"]) or row["code"] in records:
            raise ValueError("INVALID_QUARTER_CATALOG")
        records[row["code"]] = row
    normalized = normalize_overlay(overlay, PERIODS[quarter], now=now) if overlay is not None else {"records": [], "evidence": [], "parseStatus": "unchecked"}
    original_codes = set(records)
    sources = {item["id"]: item for item in normalized["evidence"]}
    for row in normalized["records"]:
        if row["code"] not in records:
            records[row["code"]] = {"code": row["code"], "name": row["name"], "type": row["type"],
                "companyCode": None, "companyName": None, "companyId": None, "company": None, "mappingStatus": "unmapped",
                "catalogSourceUrl": sources[row["evidence"][0]["sourceId"]]["sourceUrl"]}
        records[row["code"]]["historicalPresenceEvidence"] = row["evidence"]
        if row.get("sourceCompanyName"):
            records[row["code"]]["historicalSourceCompanyName"] = row["sourceCompanyName"]
    result.update(targetPeriodEnd=PERIODS[quarter], quarter=quarter, fundCount=len(records),
                  funds=sorted(records.values(), key=lambda row: row["code"]))
    unmapped = sum(not (row.get("companyCode") or row.get("companyId")) for row in records.values())
    result["coverage"].update(fundCount=len(records), mappedCount=len(records)-unmapped, unmappedCount=unmapped)
    result["quarterUniverse"] = {"schemaVersion": 1, "targetPeriodEnd": PERIODS[quarter],
        "currentDirectoryComplete": current["coverage"].get("directoryComplete") is True,
        "historicalUniverseVerified": False,
        "status": "supplemented-unverified" if normalized["evidence"] or normalized["records"] else "current-directory-only",
        "historicalSourceCount": len(normalized["evidence"]), "historicalAddedFundCount": len(set(records)-original_codes),
        "historicalParseStatus": normalized["parseStatus"], "recordUnit": "fund-share-code", "sources": normalized["evidence"]}
    if legacy: result["quarterUniverse"]["legacyPlanRecovered"] = True
    return result


def frozen_relative_path(quarter):
    if quarter not in PERIODS: raise ValueError("INVALID_PERIOD")
    return f"catalogs/{quarter}.json"


def scan_catalog(raw, quarter):
    if inspect_content(raw, content_limit(f"web/data/periods/{quarter}/catalog.json")):
        raise ValueError("CATALOG_PUBLICATION_REJECTED")


def freeze_catalog(catalog, quarter, campaign_root=".cache/campaign"):
    relative = frozen_relative_path(quarter)
    raw = (json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    scan_catalog(raw, quarter)
    destination = Path(campaign_root) / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.parent.is_symlink(): raise ValueError("INVALID_CATALOG_LOCK")
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name); handle.write(raw)
    try: os.replace(temporary, destination)
    finally: temporary.unlink(missing_ok=True)
    return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "targetPeriodEnd": PERIODS[quarter]}


def validate_catalog_lock(quarter, config, binding, campaign_root=".cache/campaign"):
    relative = frozen_relative_path(quarter)
    if (not isinstance(binding, dict) or binding.get("path") != relative or binding.get("targetPeriodEnd") != PERIODS[quarter]
            or not re.fullmatch(r"[a-f0-9]{64}", str(binding.get("sha256", "")))
            or config.get("catalogLockVersion") != 1 or config.get("catalogPath") != relative
            or config.get("catalogDigest") != binding["sha256"] or config.get("targetPeriodEnd") != PERIODS[quarter]
            or config.get("requireHistoricalUniverse") is not True):
        raise ValueError("INVALID_CATALOG_LOCK")
    file = Path(campaign_root) / relative
    if not file.is_file() or file.is_symlink() or file.parent.is_symlink(): raise ValueError("MISSING_CATALOG_LOCK")
    if file.stat().st_size > content_limit(f"web/data/periods/{quarter}/catalog.json"):
        raise ValueError("CATALOG_PUBLICATION_REJECTED")
    raw = file.read_bytes(); scan_catalog(raw, quarter)
    if hashlib.sha256(raw).hexdigest() != binding["sha256"]: raise ValueError("CATALOG_DIGEST_MISMATCH")
    catalog = json.loads(raw)
    universe = catalog.get("quarterUniverse", {})
    if (catalog.get("targetPeriodEnd") != PERIODS[quarter] or universe.get("schemaVersion") != 1
            or universe.get("targetPeriodEnd") != PERIODS[quarter] or universe.get("historicalUniverseVerified") is not False):
        raise ValueError("INVALID_QUARTER_CATALOG")
    return file
