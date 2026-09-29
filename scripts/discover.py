"""Discover all public directory companies and product types without name guessing.

The two upstream static lists are atomic JSON arrays inside JavaScript assignments.
Company pages contain the entire server-rendered product tables, including money,
exchange-listed and new products. No website JavaScript is executed.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from providers import DataError, PublicProvider

COMPANIES_URL = "https://fund.eastmoney.com/js/jjjz_gs.js"
FUNDS_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
DIRECTORY_URL = "https://fund.eastmoney.com/Company/default.html"


def decode_array(text, start):
    try:
        value, end = json.JSONDecoder().raw_decode(text[start:])
    except (ValueError, TypeError):
        raise DataError("TRUNCATED_DIRECTORY") from None
    if not isinstance(value, list):
        raise DataError("INVALID_DIRECTORY")
    return value, start + end


def parse_companies(text):
    text = text.lstrip("\ufeff \r\n")
    if not re.search(r"}\s*;?\s*$", text):
        raise DataError("TRUNCATED_DIRECTORY")
    marker = re.search(r"(?:\"?op\"?)\s*:\s*(\[)", text)
    if not marker:
        raise DataError("INVALID_DIRECTORY")
    rows, _ = decode_array(text, marker.start(1))
    companies = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 2 or not re.fullmatch(r"\d{6,12}", str(row[0])):
            raise DataError("INVALID_COMPANY_ROW")
        code, name = str(row[0]), str(row[1]).strip()
        if not name or code in companies:
            raise DataError("DUPLICATE_COMPANY_ROW")
        companies[code] = {"id": code, "code": code, "name": name,
                           "sourceUrl": f"https://fund.eastmoney.com/Company/{code}.html"}
    if not companies:
        raise DataError("EMPTY_DIRECTORY")
    return list(companies.values())


def parse_funds(text):
    text = text.lstrip("\ufeff \r\n")
    marker = re.match(r"var\s+r\s*=\s*(\[)", text)
    if not marker:
        raise DataError("INVALID_DIRECTORY")
    rows, end = decode_array(text, marker.start(1))
    if text[end:].strip() not in ("", ";"):
        raise DataError("INVALID_DIRECTORY_TRAILER")
    funds = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 4 or not re.fullmatch(r"\d{6}", str(row[0])):
            raise DataError("INVALID_FUND_ROW")
        code, name, kind = str(row[0]), str(row[2]).strip(), str(row[3]).strip()
        if code in funds or not name:
            raise DataError("DUPLICATE_FUND_ROW")
        funds[code] = {"code": code, "name": name, "type": kind or "未分类", "catalogSourceUrl": FUNDS_URL}
    if not funds:
        raise DataError("EMPTY_DIRECTORY")
    return funds


def parse_directory_index(text):
    if "</html>" not in text.lower():
        raise DataError("TRUNCATED_DIRECTORY")
    soup = BeautifulSoup(text, "html.parser")
    codes = set()
    for anchor in soup.select("a[href]"):
        match = re.search(r"/Company/(\d{6,12})\.html(?:[?#]|$)", anchor["href"], re.I)
        if match:
            codes.add(match[1])
    if not codes:
        raise DataError("EMPTY_DIRECTORY")
    # Return a separate cross-check count; do not conflate parent-company totals
    # (which may exclude brokers) with this operational issuer directory.
    return codes


def parse_company_funds(text, company):
    if "</html>" not in text.lower():
        raise DataError("TRUNCATED_COMPANY_PAGE")
    declared = re.search(r"var\s+gsId\s*=\s*[\"'](\d+)[\"']", text)
    if not declared or declared[1] != company["code"]:
        raise DataError("COMPANY_IDENTITY_MISMATCH")
    soup = BeautifulSoup(text, "html.parser")
    funds, parsed_tables, raw_rows, checks = {}, 0, 0, []
    for table in soup.find_all("table"):
        headers = [re.sub(r"\s+", "", x.get_text()) for x in table.find_all("th")]
        if not headers or not any("基金名称" in h for h in headers) or "类型" not in headers:
            continue
        parsed_tables += 1
        name_index = next(i for i, h in enumerate(headers) if "基金名称" in h)
        type_index = headers.index("类型")
        manager_index = next((i for i, h in enumerate(headers) if "基金经理" in h), None)
        rows = table.select("tbody tr") or table.select("tr")[1:]
        table_count = 0
        for row in rows:
            cells = row.find_all("td", recursive=False)
            if len(cells) <= max(name_index, type_index):
                if "暂无" in row.get_text() or not row.get_text(strip=True):
                    continue
                raise DataError("INCOMPLETE_COMPANY_TABLE")
            candidates = []
            for a in cells[name_index].select("a[href]"):
                match = re.search(r"(?:/|^)(\d{6})\.html(?:[?#]|$)", a["href"])
                if match:
                    candidates.append((match[1], a.get_text(" ", strip=True)))
            if not candidates:
                # Fund codes also appear as text in some table variants.
                match = re.search(r"(?<!\d)(\d{6})(?!\d)", cells[name_index].get_text(" ", strip=True))
                if not match:
                    raise DataError("INCOMPLETE_COMPANY_TABLE")
                candidates = [(match[1], cells[name_index].get_text(" ", strip=True).replace(match[1], "").strip())]
            codes = {x[0] for x in candidates}
            if len(codes) != 1:
                raise DataError("AMBIGUOUS_FUND_IDENTITY")
            code = candidates[0][0]
            name = next((name for _, name in candidates if name and name != code), code)
            managers = []
            if manager_index is not None and manager_index < len(cells):
                managers = [a.get_text(" ", strip=True) for a in cells[manager_index].select("a") if a.get_text(strip=True)]
            funds[code] = {"code": code, "name": name, "type": cells[type_index].get_text(" ", strip=True) or "未分类",
                           "companyCode": company["code"], "companyName": company["name"],
                           "companyId": company["code"], "company": company["name"],
                           "managerNames": managers, "companySourceUrl": company["sourceUrl"]}
            table_count += 1
        raw_rows += table_count
        advertised = next((table.get(k) for k in ("data-total", "data-count", "total") if table.get(k)), None)
        if advertised and advertised.isdigit() and int(advertised) != table_count:
            raise DataError("UPSTREAM_TOTAL_MISMATCH")
        checks.append({"rows": table_count, "upstreamTotal": int(advertised) if advertised and advertised.isdigit() else None})
    if parsed_tables == 0:
        # A missing table may indicate a template change, never confirmed zero.
        raise DataError("NO_COMPANY_TABLES")
    for link in soup.select("a[href]"):
        if re.fullmatch(r"下一页|下页|Next", link.get_text(strip=True), re.I) and "disabled" not in (link.get("class") or []):
            href = link.get("href", "")
            if href and href not in ("#", "javascript:;"):
                raise DataError("PAGINATION_REQUIRES_ADAPTER")
    return list(funds.values()), {"tables": parsed_tables, "rawRows": raw_rows, "deduplicatedRows": len(funds),
                                  "tableChecks": checks, "pagination": "server-rendered-tables"}


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def normalize_attempt(value):
    if not isinstance(value, dict):
        return {"count": 0, "status": "pending"}
    count = value.get("count", 0)
    if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 1000:
        count = 0
    status = value.get("status") if value.get("status") in {"pending", "running", "failed", "complete"} else "pending"
    result = {"count": count, "status": status}
    for key in ("lastAttemptAt", "lastFinishedAt"):
        stamp = value.get(key)
        if not isinstance(stamp, str):
            continue
        try:
            parsed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo and parsed <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
                result[key] = parsed.isoformat()
        except ValueError:
            pass
    error = value.get("lastErrorCode")
    if isinstance(error, str) and re.fullmatch(r"[A-Z][A-Z_]{1,70}", error):
        result["lastErrorCode"] = error
    return result


def catalog_attempt(company):
    return normalize_attempt({"count": company.get("directoryAttempts", 0),
                              "status": company.get("directoryAttemptStatus", company.get("directoryStatus", "pending")),
                              "lastAttemptAt": company.get("lastDirectoryAttemptAt"),
                              "lastFinishedAt": company.get("lastDirectoryCompletedAt"),
                              "lastErrorCode": company.get("directoryLastErrorCode")})


def select_pending(companies, mappings, attempts, limit=None):
    pending = [c for c in companies if c["code"] not in mappings]
    def priority(company):
        attempt = normalize_attempt(attempts.get(company["code"], {}))
        seen = attempt["count"] > 0 or attempt["status"] in {"failed", "running"}
        return (seen, attempt["count"], attempt.get("lastAttemptAt", ""))
    pending.sort(key=priority)
    return pending if limit is None else pending[:max(0, limit)]


def restore_company_records(catalog, company):
    if isinstance(company.get("funds"), list):
        return company["funds"]
    code = company.get("code") or company.get("id")
    result = []
    for fund in catalog.get("funds", []):
        owner = fund.get("companyCode") or fund.get("companyId")
        if owner == code or code in fund.get("companyCandidateCodes", []):
            result.append({**fund, "companyCode": code, "companyId": code,
                           "companyName": company["name"], "company": company["name"],
                           "companySourceUrl": company["sourceUrl"]})
    return result


def build_catalog(companies, master_funds, mappings, as_of, index_codes, errors=None, attempts=None):
    all_funds = {code: dict(value) for code, value in master_funds.items()}
    owners = {}
    company_rows = []
    failed_codes = {e["code"] for e in (errors or [])}
    attempts = attempts or {}
    for company in companies:
        record = mappings.get(company["code"], {"funds": [], "validation": {}})
        rows = record["funds"]
        attempt = normalize_attempt(attempts.get(company["code"], {}))
        status = "complete" if company["code"] in mappings else ("failed" if company["code"] in failed_codes or attempt["status"] == "failed" else "pending")
        company_rows.append({**company, "fundCount": len(rows) if status == "complete" else None,
                             "validation": record["validation"], "directoryStatus": status,
                             "directoryAttempts": attempt["count"], "directoryAttemptStatus": attempt["status"],
                             "lastDirectoryAttemptAt": attempt.get("lastAttemptAt"),
                             "lastDirectoryCompletedAt": attempt.get("lastFinishedAt"),
                             "directoryLastErrorCode": attempt.get("lastErrorCode")})
        for item in rows:
            code = item["code"]
            owners.setdefault(code, []).append(company["code"])
            previous = all_funds.get(code, {})
            all_funds[code] = {**item, **{k: previous[k] for k in ("name", "type", "catalogSourceUrl") if k in previous}}
    conflicts = {code for code, values in owners.items() if len(set(values)) > 1}
    for code, item in all_funds.items():
        if code not in owners or code in conflicts:
            item.update({"companyCode": None, "companyName": None, "companyId": None, "company": None,
                         "mappingStatus": "conflicting-owners" if code in conflicts else "unmapped"})
            if code in conflicts:
                item["companyCandidateCodes"] = sorted(set(owners[code]))
        else:
            item["mappingStatus"] = "source-confirmed"
    unmapped = sum(x["companyCode"] is None for x in all_funds.values())
    return {"schemaVersion": 1, "asOf": as_of, "retrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "companyCount": len(companies), "fundCount": len(all_funds), "companies": company_rows,
            "funds": sorted(all_funds.values(), key=lambda x: x["code"]),
            "sourceUrls": {"companies": COMPANIES_URL, "funds": FUNDS_URL, "companyIndex": DIRECTORY_URL},
            "coverage": {"status": "complete-directory" if len(mappings) == len(companies) else "partial-directory",
                         "directoryComplete": len(mappings) == len(companies),
                         "companyCount": len(companies), "fundCount": len(all_funds),
                         "upstreamCompanyRows": len(companies), "upstreamFundRows": len(master_funds),
                         "indexCompanyCount": len(index_codes), "indexOnlyCompanyCount": len(index_codes - {c['code'] for c in companies}),
                         "companyPagesFetched": len(mappings), "mappedCount": len(all_funds) - unmapped,
                         "companyPagesAttempted": sum(normalize_attempt(a)["count"] > 0 for a in attempts.values()),
                         "unmappedCount": unmapped, "conflictingOwnershipCount": len(conflicts),
                         "extraCompanyFundCount": len(set(all_funds) - set(master_funds)),
                         "typesIncluded": sorted({f["type"] for f in all_funds.values()}),
                         "shareClassesMerged": False}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="raw/catalog.json")
    parser.add_argument("--state", default="raw/catalog-state.json")
    parser.add_argument("--as-of", default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    parser.add_argument("--limit", type=int)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--max-companies", type=int, help="Bound this run; state resumes remaining companies.")
    args = parser.parse_args()
    dt.date.fromisoformat(args.as_of)
    provider = PublicProvider()
    companies = parse_companies(provider.get(COMPANIES_URL))
    master = parse_funds(provider.get(FUNDS_URL))
    index_codes = parse_directory_index(provider.get(DIRECTORY_URL))
    # The independent all-company index must not contain issuers omitted by the
    # static directory. New issuers require a reviewed parser update, not silence.
    if index_codes - {c["code"] for c in companies}:
        raise DataError("COMPANY_DIRECTORY_MISMATCH")
    complete_scope = args.limit is None
    if args.limit is not None:
        companies = companies[:max(0, args.limit)]
    if not companies:
        raise DataError("EMPTY_DIRECTORY")
    if os.environ.get("GITHUB_ACTIONS") != "true" and len(companies) > 2:
        raise DataError("BATCH_REQUIRES_ACTIONS")
    fingerprint = hashlib.sha256(json.dumps(companies, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    state = {"schemaVersion": 1, "asOf": args.as_of, "catalogFingerprint": fingerprint, "mappings": {}, "attempts": {}, "errors": []}
    allowed = {c["code"] for c in companies}
    state_path = Path(args.state)
    if state_path.exists():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        if previous.get("catalogFingerprint") == fingerprint and previous.get("asOf") == args.as_of:
            state["mappings"] = previous.get("mappings", {})
            state["attempts"] = {code: normalize_attempt(a) for code, a in previous.get("attempts", {}).items() if code in allowed}
    previous_catalog = None
    if Path(args.output).exists():
        previous = json.loads(Path(args.output).read_text(encoding="utf-8"))
        previous_catalog = previous
        if previous.get("asOf") == args.as_of:
            for company in previous.get("companies", []):
                code = company.get("code") or company.get("id")
                if code not in allowed:
                    continue
                if company.get("directoryStatus") == "complete" and code not in state["mappings"]:
                    rows = restore_company_records(previous, company)
                    if len(rows) == company.get("fundCount"):
                        state["mappings"][code] = {"funds": rows, "validation": company.get("validation", {})}
                saved = catalog_attempt(company)
                current = state["attempts"].get(code, {})
                if not current or (saved["count"], saved.get("lastAttemptAt", "")) > (current.get("count", 0), current.get("lastAttemptAt", "")):
                    state["attempts"][code] = saved
    pending = select_pending(companies, state["mappings"], state["attempts"], args.max_companies)
    checkpoint_lock = threading.Lock()
    def save_progress():
        atomic_json(state_path, state)
        result = build_catalog(companies, master, state["mappings"], args.as_of, index_codes, state["errors"], state["attempts"])
        result["coverage"]["scope"] = "all-directory-companies" if complete_scope else "limited-probe"
        if not complete_scope:
            result["coverage"]["status"] = "limited-probe"
        if previous_catalog and previous_catalog.get("coverage", {}).get("status") == "complete-directory":
            if result["coverage"]["status"] != "complete-directory" or result["companyCount"] < previous_catalog.get("companyCount", 0) * 0.95 or result["fundCount"] < previous_catalog.get("fundCount", 0) * 0.95:
                return
        atomic_json(args.output, result)
    def get_company(company):
        code = company["code"]
        with checkpoint_lock:
            attempt = normalize_attempt(state["attempts"].get(code, {}))
            # Count only when a worker starts an actual provider attempt; queued
            # companies do not accrue synthetic attempts if a runner is stopped.
            state["attempts"][code] = {"count": attempt["count"] + 1, "status": "running",
                                       "lastAttemptAt": dt.datetime.now(dt.timezone.utc).isoformat()}
            save_progress()
        try:
            rows, validation = parse_company_funds(PublicProvider().get(company["sourceUrl"]), company)
            return company["code"], {"funds": rows, "validation": validation}, None
        except Exception as error:
            return company["code"], None, error.code if isinstance(error, DataError) else "PARSE_FAILED"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(get_company, c) for c in pending]
        for future in concurrent.futures.as_completed(futures):
            code, result, error = future.result()
            with checkpoint_lock:
                state["attempts"][code].update({"status": "complete" if result is not None else "failed",
                                                "lastFinishedAt": dt.datetime.now(dt.timezone.utc).isoformat()})
                if result is not None:
                    state["mappings"][code] = result
                else:
                    state["attempts"][code]["lastErrorCode"] = error
                    state["errors"].append({"code": code, "stage": "company-catalog", "errorCode": error})
                save_progress()
            count = len(state["mappings"])
            if count % 20 == 0:
                print(json.dumps({"status": "discovering", "companyCount": len(companies), "completedCount": count, "errorCount": len(state["errors"])}))
    if len(state["mappings"]) != len(companies) and not args.allow_partial:
        raise DataError("INCOMPLETE_COMPANY_CATALOG")
    result = build_catalog(companies, master, state["mappings"], args.as_of, index_codes, state["errors"], state["attempts"])
    result["coverage"]["scope"] = "all-directory-companies" if complete_scope else "limited-probe"
    if not complete_scope:
        result["coverage"]["status"] = "limited-probe"
    old_path = Path(args.output)
    if old_path.exists():
        old = json.loads(old_path.read_text(encoding="utf-8"))
        old_coverage = old.get("coverage", {})
        if old_coverage.get("status") == "complete-directory":
            if not complete_scope or result["coverage"]["status"] != "complete-directory" or result["companyCount"] < old.get("companyCount", 0) * 0.95 or result["fundCount"] < old.get("fundCount", 0) * 0.95:
                raise DataError("DIRECTORY_REGRESSION")
    atomic_json(args.output, result)
    print(json.dumps({"status": result["coverage"]["status"], "companyCount": result["companyCount"],
                      "fundCount": result["fundCount"], "unmappedCount": result["coverage"]["unmappedCount"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "errorCode": error.code if isinstance(error, DataError) else "DISCOVERY_FAILED"}))
        raise SystemExit(1) from None
