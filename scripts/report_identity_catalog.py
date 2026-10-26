"""Bounded, content-addressed proof registry for missing historical share codes."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

from portfolio_identity import normalize_portfolio_identity
from publication_guard import inspect_content

MAX_PART_BYTES = 5_000_000
MAX_TOTAL_BYTES = 256_000_000
MAX_PARTS = 128
MAX_SOURCES = 150000
MAX_CODES = 150000
CODE = re.compile(r"[0-9]{6}")
PART = re.compile(r"\./identity-sources/part-([a-f0-9]{64})\.json")


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def period_for(quarter):
    if quarter not in {"2026Q2", "2026Q3"}: raise ValueError("INVALID_IDENTITY_QUARTER")
    return "2026-06-30" if quarter == "2026Q2" else "2026-09-30"


def _code(value):
    return isinstance(value, str) and CODE.fullmatch(value) is not None and value != "000000"


def _source(proof, period_end, report=None, *, now=None):
    normalized = normalize_portfolio_identity(proof, report, now=now)
    if (not normalized or normalized["periodEnd"] != period_end or not normalized["eligibleForShareClassExpansion"]
            or not all(_code(code) for code in normalized["shareClassCodes"])): return None
    identity = hashlib.sha256(encoded(normalized)).hexdigest()
    return {"id": identity, "proof": normalized}


def _valid_source(raw, period_end, *, now=None):
    if not isinstance(raw, dict): return None
    value = _source(raw.get("proof"), period_end, now=now)
    return value if value and raw.get("id") == value["id"] else None


def _read(path, maximum):
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("IDENTITY_REGISTRY_MISSING")
    if path.stat().st_size > maximum: raise ValueError("IDENTITY_REGISTRY_TOO_LARGE")
    raw = path.read_bytes()
    if inspect_content(raw, maximum): raise ValueError("IDENTITY_REGISTRY_PUBLICATION_REJECTED")
    return raw, json.loads(raw)


def part_path(base, quarter, public_path, *, frozen=False):
    if not isinstance(public_path, str) or not PART.fullmatch(public_path):
        raise ValueError("INVALID_IDENTITY_REGISTRY_PATH")
    folder = Path(base) / "catalogs" / (quarter + "-identities") if frozen else Path(base) / "identity-sources"
    return folder / Path(public_path).name


def _registry(catalog, quarter):
    period = period_for(quarter)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("quarterUniverse"), dict):
        raise ValueError("INVALID_IDENTITY_REGISTRY")
    registry = catalog["quarterUniverse"].get("reportIdentityRegistry")
    if registry is None:
        if any(row.get("reportIdentityPresenceEvidence") for row in catalog.get("funds", []) if isinstance(row, dict)):
            raise ValueError("IDENTITY_REGISTRY_MISSING")
        return None
    if (catalog.get("targetPeriodEnd") != period or not isinstance(registry, dict) or registry.get("schemaVersion") != 1
            or registry.get("targetPeriodEnd") != period or registry.get("historicalUniverseVerified") is not False
            or registry.get("shareClassUniverseComplete") is not False or not isinstance(registry.get("files"), list)
            or len(registry["files"]) > MAX_PARTS):
        raise ValueError("INVALID_IDENTITY_REGISTRY")
    for field, maximum in (("sourceCount", MAX_SOURCES), ("codeCount", MAX_CODES), ("totalBytes", MAX_TOTAL_BYTES)):
        if type(registry.get(field)) is not int or not 0 <= registry[field] <= maximum:
            raise ValueError("INVALID_IDENTITY_REGISTRY")
    return registry


def load_registry(catalog, base, quarter, *, frozen=False, now=None, strict=True):
    """Revalidate cached proofs. Permissive recovery never blesses a bad part."""
    period = period_for(quarter)
    candidates = set()
    for row in catalog.get("funds", []) if isinstance(catalog, dict) else []:
        if isinstance(row, dict) and _code(row.get("code")) and row.get("reportIdentityPresenceEvidence"):
            candidates.add(row["code"])
    sources, failures, seen_paths, total = {}, 0, set(), 0
    try: registry = _registry(catalog, quarter)
    except ValueError:
        if strict: raise
        return {}, candidates, {"cacheIssueCount": 1}
    if registry is None: return {}, set(), {"cacheIssueCount": 0}
    for item in registry["files"]:
        try:
            if (not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] in seen_paths
                    or type(item.get("byteCount")) is not int or not 0 < item["byteCount"] <= MAX_PART_BYTES
                    or type(item.get("sourceCount")) is not int or not 0 < item["sourceCount"] <= MAX_SOURCES):
                raise ValueError("INVALID_IDENTITY_REGISTRY")
            seen_paths.add(item["path"])
            file = part_path(base, quarter, item["path"], frozen=frozen)
            raw, part = _read(file, MAX_PART_BYTES)
            digest = hashlib.sha256(raw).hexdigest()
            if item.get("sha256") != digest or PART.fullmatch(item["path"])[1] != digest or len(raw) != item["byteCount"]:
                raise ValueError("IDENTITY_REGISTRY_DIGEST_MISMATCH")
            if (not isinstance(part, dict) or part.get("schemaVersion") != 1 or part.get("targetPeriodEnd") != period
                    or not isinstance(part.get("sources"), list) or len(part["sources"]) != item["sourceCount"]):
                raise ValueError("INVALID_IDENTITY_REGISTRY")
            clean = {}
            for value in part["sources"]:
                normalized = _valid_source(value, period, now=now)
                if not normalized or normalized["id"] in clean or normalized["id"] in sources:
                    raise ValueError("INVALID_IDENTITY_REGISTRY_PROOF")
                clean[normalized["id"]] = normalized
            total += len(raw)
            if total > MAX_TOTAL_BYTES or len(sources) + len(clean) > MAX_SOURCES:
                raise ValueError("IDENTITY_REGISTRY_TOO_LARGE")
            sources.update(clean)
        except (ValueError, OSError, TypeError):
            if strict: raise
            failures += 1
    refs, valid_codes, used, source_codes = {}, set(), set(), {}
    for row in catalog.get("funds", []):
        if not isinstance(row, dict) or not row.get("reportIdentityPresenceEvidence"): continue
        code, references = row.get("code"), row["reportIdentityPresenceEvidence"]
        if not _code(code) or not isinstance(references, list) or not 1 <= len(references) <= 20:
            if strict: raise ValueError("INVALID_IDENTITY_REGISTRY_REFERENCE")
            failures += 1; continue
        ids = []
        for source_id in references:
            source = sources.get(source_id) if isinstance(source_id, str) else None
            if source and code in source["proof"]["shareClassCodes"]:
                ids.append(source_id); used.add(source_id)
                source_codes.setdefault(source_id, set()).add(code)
            else:
                if strict: raise ValueError("INVALID_IDENTITY_REGISTRY_REFERENCE")
                failures += 1
        if ids:
            refs[code] = sorted(set(ids)); valid_codes.add(code)
    if strict and (len(sources) != registry["sourceCount"] or len(valid_codes) != registry["codeCount"]
                   or total != registry["totalBytes"] or used != set(sources)):
        raise ValueError("IDENTITY_REGISTRY_COUNT_MISMATCH")
    if not strict and (len(sources) != registry["sourceCount"] or len(valid_codes) != registry["codeCount"]
                       or total != registry["totalBytes"]): failures += 1
    # Sources without a surviving catalogue reference do not establish a row.
    sources = {key: {**value, "codes": sorted(source_codes[key])}
               for key, value in sources.items() if key in used}
    return sources, candidates - valid_codes, {"cacheIssueCount": failures}


def report_sources(document, period_end, *, now=None):
    """Read identity proof only; neither global aliases nor manager text qualify."""
    if (not isinstance(document, dict) or document.get("schemaVersion") != 1 or document.get("sourceMode") != "actions"
            or document.get("targetPeriodEnd") != period_end or not isinstance(document.get("funds"), list)
            or len(document["funds"]) > 1000): return
    for fund in document["funds"]:
        if not isinstance(fund, dict) or not _code(fund.get("code")): continue
        history = fund.get("reports") or []
        if not isinstance(history, list): continue
        reports = [fund.get("selectedReport"), *history]
        if len(reports) > 100: continue
        seen = set()
        for report in reports:
            if not isinstance(report, dict) or report.get("periodEnd") != period_end: continue
            if report.get("fundCode") not in (None, fund["code"]): continue
            source = _source(report.get("portfolioIdentityEvidence"), period_end,
                             {**report, "fundCode": fund["code"]}, now=now)
            if source and source["id"] not in seen:
                seen.add(source["id"]); yield source


def retained_sources(directory, quarter, *, now=None):
    period = period_for(quarter)
    try: _, manifest = _read(Path(directory) / "latest.json", 25_000_000)
    except (ValueError, OSError): return
    if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or manifest.get("sourceMode") != "actions"
            or manifest.get("targetPeriodEnd") != period): return
    yield from report_sources(manifest, period, now=now)
    files = manifest.get("companyFiles", [])
    if not isinstance(files, list) or len(files) > 10000: return
    seen = set()
    for file in files:
        name = file.get("path") if isinstance(file, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r"\./data/companies/[A-Za-z0-9_-]{1,64}\.json", name) or name in seen: continue
        seen.add(name)
        try: _, document = _read(Path(directory) / name[len("./data/"):], 25_000_000)
        except (ValueError, OSError): continue
        yield from report_sources(document, period, now=now)


def pack_sources(sources, period_end):
    """O(total evidence bytes), including one shared proof for several codes."""
    parts, batch, byte_count, total = {}, [], 0, 0
    def flush():
        nonlocal batch, byte_count, total
        if not batch: return
        raw = encoded({"schemaVersion": 1, "targetPeriodEnd": period_end, "sources": batch})
        if len(raw) > MAX_PART_BYTES: raise ValueError("IDENTITY_REGISTRY_TOO_LARGE")
        digest = hashlib.sha256(raw).hexdigest(); parts[f"./identity-sources/part-{digest}.json"] = raw
        total += len(raw); batch, byte_count = [], 0
        if len(parts) > MAX_PARTS or total > MAX_TOTAL_BYTES: raise ValueError("IDENTITY_REGISTRY_TOO_LARGE")
    for key in sorted(sources):
        source = {field: sources[key][field] for field in ("id", "proof")}
        size = len(encoded(source))
        if size + 200 > MAX_PART_BYTES: raise ValueError("IDENTITY_REGISTRY_TOO_LARGE")
        if batch and byte_count + size + 200 > MAX_PART_BYTES: flush()
        batch.append(source); byte_count += size
    flush()
    return parts


def supplement_catalog(catalog, directory, quarter, *, now=None):
    """Add only missing explicitly listed codes and retain old supplement proof."""
    period = period_for(quarter)
    result = copy.deepcopy(catalog)
    rows = {row["code"]: row for row in result["funds"]}
    # Only the independently revalidated same-quarter registry may restore
    # these references; opaque input aliases or stale references cannot.
    for row in rows.values(): row.pop("reportIdentityPresenceEvidence", None)
    original = set(rows)
    sources, candidates, recovery = {}, set(), {"cacheIssueCount": 0}
    try:
        _, previous = _read(Path(directory) / "catalog.json", 25_000_000)
        if previous.get("targetPeriodEnd") == period:
            sources, candidates, recovery = load_registry(previous, directory, quarter, now=now, strict=False)
    except (ValueError, OSError, AttributeError): pass
    references = {}
    for source in sources.values():
        for code in source.pop("codes"):
            references.setdefault(code, []).append(source["id"])
    known = {code for code, row in rows.items() if row.get("historicalCandidate") is not True} | set(references)
    added_sources = 0
    for source in retained_sources(directory, quarter, now=now):
        missing = set(source["proof"]["shareClassCodes"]) - known
        if not missing: continue
        if source["id"] not in sources:
            if len(sources) >= MAX_SOURCES: raise ValueError("IDENTITY_REGISTRY_TOO_LARGE")
            sources[source["id"]] = source; added_sources += 1
        for code in sorted(missing): references.setdefault(code, []).append(source["id"])
        known.update(missing)
    for code, ids in sorted(references.items()):
        if code not in rows:
            rows[code] = {"code": code, "name": "报告列示份额 " + code, "type": "未分类",
                "companyCode": None, "companyId": None, "companyName": None, "company": None, "mappingStatus": "unmapped"}
        rows[code].pop("historicalCandidate", None)
        rows[code]["reportIdentityPresenceEvidence"] = sorted(set(ids))
    for code in sorted(candidates - set(references)):
        if code not in rows:
            rows[code] = {"code": code, "name": "历史待核验产品 " + code, "type": "未分类",
                "companyCode": None, "companyId": None, "companyName": None, "company": None,
                "mappingStatus": "unmapped", "historicalCandidate": True}
    if len(rows) > MAX_CODES: raise ValueError("IDENTITY_CATALOG_TOO_LARGE")
    parts = pack_sources(sources, period)
    registry = {"schemaVersion": 1, "targetPeriodEnd": period,
        "historicalUniverseVerified": False, "shareClassUniverseComplete": False,
        "status": "partial" if recovery["cacheIssueCount"] else "sourced-supplement" if sources else "empty",
        "sourceCount": len(sources), "codeCount": len(references), "addedFundCount": len(set(rows) - original),
        "newSourceCount": added_sources, "cacheIssueCount": recovery["cacheIssueCount"],
        "candidateCodeCount": len(candidates - set(references)), "totalBytes": sum(len(raw) for raw in parts.values()),
        "files": [{"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "byteCount": len(raw),
                   "sourceCount": len(json.loads(raw)["sources"])} for name, raw in parts.items()]}
    result["funds"] = sorted(rows.values(), key=lambda row: row["code"]); result["fundCount"] = len(rows)
    unmapped = sum(not (row.get("companyCode") or row.get("companyId")) for row in rows.values())
    result["coverage"].update(fundCount=len(rows), mappedCount=len(rows)-unmapped, unmappedCount=unmapped)
    universe = result["quarterUniverse"]
    universe["reportIdentityRegistry"] = registry
    universe["historicalUniverseVerified"] = False
    universe["historicalCandidateFundCount"] = sum(row.get("historicalCandidate") is True for row in rows.values())
    if sources or candidates: universe["status"] = "supplemented-unverified"
    return result, parts
