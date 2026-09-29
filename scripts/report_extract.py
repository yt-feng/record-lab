"""Private, bounded extraction of fund report PDFs.

The public snapshot stores only small, source-linked findings.  PDF bytes and
MinerU markdown stay in process memory (or the Actions runner's temporary
directory when a caller chooses to persist a cache) and are never written to
``web/data``.  MinerU is optional: when no key is configured, or when its
service cannot complete, the caller receives a conservative pypdf result.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import requests


MINERU_BASE_URL = "https://mineru.net"
DONE_STATES = {"done", "success", "completed"}
FAILED_STATES = {"failed", "fail", "error"}


class ExtractionError(Exception):
    """A fixed, public-safe extraction failure code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _split_tokens(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[\n,;]+", value) if part.strip()]


def mineru_tokens_from_env(env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """Read supported secret names without ever logging their values."""
    source = os.environ if env is None else env
    names = ["MINER_U_KEYS", "MINER_U", "MINERU_API_KEY"]
    names.extend(sorted((name for name in source if re.fullmatch(r"MINER_U_\d+", name)),
                        key=lambda name: int(name.rsplit("_", 1)[1])))
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in names:
        parts = _split_tokens(source.get(name))
        for index, token in enumerate(parts, 1):
            if token in seen:
                continue
            seen.add(token)
            result.append((name if len(parts) == 1 else f"{name}_{index}", token))
    if result:
        try:
            offset = int(source.get("MINER_U_TOKEN_OFFSET", "0")) % len(result)
        except (TypeError, ValueError):
            offset = 0
        result = result[offset:] + result[:offset]
    return result


def _safe_json(response: requests.Response, stage: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        raise ExtractionError(f"MINERU_{stage}_INVALID_RESPONSE") from None
    code = payload.get("code") if isinstance(payload, dict) else None
    if response.status_code >= 400 or code not in (None, 0, "0"):
        raise ExtractionError(f"MINERU_{stage}_REJECTED")
    if not isinstance(payload, dict):
        raise ExtractionError(f"MINERU_{stage}_INVALID_RESPONSE")
    return payload


@dataclass
class MinerUClient:
    """Small synchronous implementation of MinerU's v4 batch contract."""

    session: requests.Session | Any | None = None
    base_url: str = MINERU_BASE_URL
    timeout: float = 20.0
    poll_timeout: float = 75.0
    poll_interval: float = 2.0

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    def _headers(self, token: str) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    def parse(self, pdf_bytes: bytes, filename: str = "report.pdf") -> str:
        if not pdf_bytes:
            raise ExtractionError("MINERU_EMPTY_PDF")
        data_id = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[:128] or "report.pdf"
        try:
            response = self.session.post(
                self.base_url.rstrip("/") + "/api/v4/file-urls/batch",
                headers=self._headers(self._token),
                json={"files": [{"name": data_id, "data_id": data_id, "is_ocr": False}],
                      "model_version": "vlm", "language": "ch", "enable_table": True,
                      "enable_formula": True},
                timeout=min(60.0, max(3.0, self.timeout)),
            )
            body = _safe_json(response, "UPLOAD_URL")
            payload = body.get("data") or {}
            batch_id = payload.get("batch_id")
            urls = payload.get("file_urls") or []
            if not batch_id or not urls or not isinstance(urls[0], str):
                raise ExtractionError("MINERU_UPLOAD_URL_INVALID")
            uploaded = self.session.put(urls[0], data=pdf_bytes, timeout=min(300.0, max(5.0, self.timeout * 3)))
            if uploaded.status_code not in (200, 201, 204):
                raise ExtractionError("MINERU_UPLOAD_FAILED")
            row = self._poll(str(batch_id))
            zip_url = row.get("full_zip_url") or row.get("fullZipUrl")
            if not isinstance(zip_url, str) or not zip_url.startswith("https://"):
                raise ExtractionError("MINERU_ZIP_URL_MISSING")
            archive = self.session.get(zip_url, timeout=min(300.0, max(5.0, self.timeout * 3)))
            if archive.status_code >= 400:
                raise ExtractionError("MINERU_ZIP_DOWNLOAD_FAILED")
            return _full_markdown_from_zip(archive.content)
        except ExtractionError:
            raise
        except requests.exceptions.Timeout:
            raise ExtractionError("MINERU_TIMEOUT") from None
        except requests.exceptions.RequestException:
            raise ExtractionError("MINERU_NETWORK_ERROR") from None
        except (TypeError, ValueError, KeyError, IndexError, zipfile.BadZipFile):
            raise ExtractionError("MINERU_RESPONSE_INVALID") from None

    # Set by parse() only.  It is private and never emitted or logged.
    _token: str = ""

    def parse_with_token(self, pdf_bytes: bytes, token: str, filename: str = "report.pdf") -> str:
        self._token = token
        try:
            return self.parse(pdf_bytes, filename)
        finally:
            self._token = ""

    def _poll(self, batch_id: str) -> dict[str, Any]:
        endpoint = self.base_url.rstrip("/") + f"/api/v4/extract-results/batch/{batch_id}"
        deadline = time.monotonic() + max(1.0, self.poll_timeout)
        last_rows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            response = self.session.get(endpoint, headers=self._headers(self._token),
                                        timeout=min(60.0, max(3.0, self.timeout)))
            body = _safe_json(response, "POLL")
            rows = (body.get("data") or {}).get("extract_result") or []
            if isinstance(rows, list):
                last_rows = [row for row in rows if isinstance(row, dict)]
            if last_rows:
                done = [row for row in last_rows if str(row.get("state", "")).lower() in DONE_STATES]
                failed = [row for row in last_rows if str(row.get("state", "")).lower() in FAILED_STATES]
                if done:
                    return done[0]
                if len(failed) == len(last_rows):
                    raise ExtractionError("MINERU_PARSE_FAILED")
            time.sleep(max(0.05, self.poll_interval))
        raise ExtractionError("MINERU_TIMEOUT")


def _full_markdown_from_zip(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            candidates = [name for name in names if name.lower().endswith("full.md")]
            if not candidates:
                candidates = [name for name in names if name.lower().endswith(".md")]
            if not candidates:
                raise ExtractionError("MINERU_FULL_MD_MISSING")
            name = max(candidates, key=lambda candidate: archive.getinfo(candidate).file_size)
            text = archive.read(name).decode("utf-8", errors="replace")
    except ExtractionError:
        raise
    except (zipfile.BadZipFile, KeyError, UnicodeError):
        raise ExtractionError("MINERU_ZIP_INVALID") from None
    if not text.strip():
        raise ExtractionError("MINERU_FULL_MD_EMPTY")
    return text


_PAGE_MARKER = re.compile(
    r"<!--\s*(?:page(?:_number|[- ]number)?|page_no|page-no)\s*[:=]?\s*(\d+)\s*-->",
    re.I,
)


def markdown_pages(text: str) -> list[str]:
    """Split MinerU markdown while preserving its original page markers."""
    return [page for _number, page in markdown_page_records(text)]


def markdown_page_records(text: str) -> list[tuple[int, str]]:
    """Return ``(original_page_number, text)`` pairs from MinerU markdown."""
    matches = list(_PAGE_MARKER.finditer(text))
    if not matches:
        # A page break is a useful fallback for older MinerU exports, but do
        # not pretend ordinary paragraphs have independently verified pages.
        blocks = [block.strip() for block in re.split(r"\n\s*\f\s*\n|\n-{3,}\n", text) if block.strip()]
        return [(index, block) for index, block in enumerate(blocks, 1)] or [(1, text.strip())]
    pages: list[str] = []
    prefix = text[: matches[0].start()].strip()
    if prefix:
        pages.append(prefix)
    records: list[tuple[int, str]] = []
    if prefix:
        records.append((1, prefix))
    for index, marker in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[marker.end():end].strip()
        if body:
            records.append((int(marker.group(1)), body))
    return records or [(1, text.strip())]


_STRATEGY_HEADING = re.compile(
    r"(?:报告期内基金(?:的)?投资策略和运作分析|报告期内基金投资策略和运作分析|"
    r"管理人对报告期内本基金的投资策略和业绩表现的说明)", re.I,
)
_SECTION_END = re.compile(r"(?:基金的业绩表现|报告期内基金的投资策略和运作分析|基金份额持有人信息|其他重要事项)")
_STRATEGY_WORDS = re.compile(r"本基金|组合|配置|增持|减持|增配|减配|调整|买入|卖出|估值|行业|仓位|基本面")


def strategy_excerpts(pages: list[str], source_url: str, period_end: str, kind: str,
                     *, fallback_narrative: Mapping[str, Any] | None = None,
                     page_numbers: list[int] | None = None) -> list[dict[str, Any]]:
    """Extract verbatim strategy sentences and cite their original page."""
    excerpts: list[dict[str, Any]] = []
    numbers = page_numbers or list(range(1, len(pages) + 1))
    for position, raw in enumerate(pages):
        page_number = numbers[position] if position < len(numbers) else position + 1
        text = re.sub(r"\s+", " ", raw).strip()
        match = _STRATEGY_HEADING.search(text)
        if not match:
            continue
        # Headings are often the last line of one PDF page and the actual
        # discussion starts on the next page. Carry the section forward until
        # its next report heading while retaining each sentence's page.
        section_pages: list[tuple[int, str]] = [(page_number, text[match.end():])]
        for next_position in range(position + 1, min(len(pages), position + 4)):
            next_number = numbers[next_position] if next_position < len(numbers) else next_position + 1
            next_text = re.sub(r"\s+", " ", pages[next_position]).strip()
            section_pages.append((next_number, next_text))
            if _SECTION_END.search(next_text):
                break
        for cited_page, section in section_pages:
            end_match = _SECTION_END.search(section)
            if end_match:
                section = section[:end_match.start()]
            sentences = [part.strip() for part in re.split(r"(?<=[。！？；])", section) if len(part.strip()) >= 12]
            for sentence in sentences:
                if not _STRATEGY_WORDS.search(sentence):
                    continue
                excerpts.append({"text": sentence[:260], "sourceUrl": source_url, "page": cited_page,
                                 "periodStart": _period_start(period_end, kind), "periodEnd": period_end})
                if len(excerpts) >= 8:
                    return excerpts
    if not excerpts and fallback_narrative and fallback_narrative.get("text"):
        excerpts.append({"text": str(fallback_narrative["text"])[:260], "sourceUrl": source_url,
                         "page": fallback_narrative.get("page"), "periodStart": fallback_narrative.get("periodStart"),
                         "periodEnd": period_end})
    return excerpts


def strategy_themes(excerpts: list[Mapping[str, Any]]) -> list[str]:
    labels = (("行业/配置", r"行业|配置|仓位"), ("估值/基本面", r"估值|基本面|经营|盈利"),
              ("调仓/交易", r"调整|增持|减持|增配|减配|买入|卖出"), ("组合管理", r"组合|个股"),
              ("风险/防守", r"风险|防守|现金|债券"))
    combined = " ".join(str(item.get("text", "")) for item in excerpts)
    return [label for label, pattern in labels if re.search(pattern, combined)]


def _period_start(period_end: str, kind: str) -> str:
    if kind == "quarterly":
        month = int(period_end[5:7])
        start_month = ((month - 1) // 3) * 3 + 1
        return f"{period_end[:4]}-{start_month:02d}-01"
    return f"{period_end[:4]}-01-01"


def _pypdf_pages(pdf_bytes: bytes, max_pages: int) -> list[str]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        return [page.extract_text() or "" for page in reader.pages[:max(1, min(max_pages, 80))]]
    except Exception:
        raise ExtractionError("PDF_PARSE_FAILED") from None


def _details_for_pages(pages: list[str], source_url: str, period_end: str, kind: str,
                      parse_pages: Callable[[list[str], str, str, str], dict[str, Any]],
                      enrich_pages: Callable[[list[str]], Mapping[str, Any]] | None = None,
                      page_numbers: list[int] | None = None) -> dict[str, Any]:
    parsed = dict(parse_pages(pages, source_url, period_end, kind))
    normalized = "\n\n".join(page.strip() for page in pages if page and page.strip())
    excerpts = strategy_excerpts(pages, source_url, period_end, kind,
                                 fallback_narrative=parsed.get("narrative"), page_numbers=page_numbers)
    parsed.update({"sourceTextHash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                   "pageStats": {"pageCount": len(pages), "nonEmptyPages": sum(bool(page.strip()) for page in pages),
                                  "charCount": len(normalized)},
                   "strategyExcerpts": excerpts, "strategyThemes": strategy_themes(excerpts)})
    if enrich_pages:
        parsed.update(dict(enrich_pages(pages)))
    return parsed


def extract_report(pdf_bytes: bytes, source_url: str, period_end: str, kind: str,
                   *, parse_pages: Callable[[list[str], str, str, str], dict[str, Any]],
                   mineru_client: MinerUClient | None = None, filename: str = "report.pdf",
                   max_pages: int = 18, env: Mapping[str, str] | None = None,
                   enrich_pages: Callable[[list[str]], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Parse a report with MinerU when configured, falling back to pypdf."""
    tokens = mineru_tokens_from_env(env)
    mineru_error: str | None = None
    if tokens:
        client = mineru_client or MinerUClient(
            base_url=(env or os.environ).get("MINERU_BASE_URL", MINERU_BASE_URL),
            poll_timeout=float((env or os.environ).get("MINERU_POLL_TIMEOUT", "75")),
            poll_interval=float((env or os.environ).get("MINERU_POLL_INTERVAL", "2")),
        )
        for label, token in tokens:
            try:
                markdown = client.parse_with_token(pdf_bytes, token, filename)
                records = markdown_page_records(markdown)
                page_numbers = [number for number, _page in records]
                pages = [page for _number, page in records]
                result = _details_for_pages(pages, source_url, period_end, kind, parse_pages, enrich_pages, page_numbers)
                result.update({"parseMethod": "mineru", "sourceTextHash": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                               "pageStats": {**result["pageStats"], "markdownBytes": len(markdown.encode("utf-8"))},
                               "mineruTokenLabel": label})
                return result
            except ExtractionError as error:
                mineru_error = error.code
            except Exception:
                mineru_error = "MINERU_UNEXPECTED_ERROR"
    try:
        pages = _pypdf_pages(pdf_bytes, max_pages)
        result = _details_for_pages(pages, source_url, period_end, kind, parse_pages, enrich_pages)
        result["parseMethod"] = "pypdf-fallback" if mineru_error else "pypdf"
        if mineru_error:
            result["parseErrorCode"] = mineru_error
        return result
    except ExtractionError as error:
        if mineru_error:
            error = ExtractionError("PDF_PARSE_FAILED")
        raise error


__all__ = ["ExtractionError", "MinerUClient", "extract_report", "markdown_pages",
           "mineru_tokens_from_env", "strategy_excerpts", "strategy_themes"]
