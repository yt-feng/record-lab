"""Synthetic MinerU/PDF extraction tests; no external service is called."""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import report_extract
from report_extract import MinerUClient, extract_report, markdown_page_records, markdown_pages, mineru_tokens_from_env


class Response:
    def __init__(self, *, payload=None, content=b"", status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class MinerFixture:
    def __init__(self, archive: bytes):
        self.archive = archive
        self.polls = 0

    def post(self, _url, **_kwargs):
        return Response(payload={"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://upload.invalid/1"]}})

    def put(self, _url, **_kwargs):
        return Response(status_code=200)

    def get(self, url, **_kwargs):
        if "extract-results" in url:
            self.polls += 1
            if self.polls == 1:
                return Response(payload={"code": 0, "data": {"extract_result": [{"state": "running"}]}})
            return Response(payload={"code": 0, "data": {"extract_result": [{"state": "done", "full_zip_url": "https://download.invalid/1.zip"}]}})
        return Response(content=self.archive)


def fixture_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("report/full.md", "<!-- page: 1 -->\n目录\n<!-- page: 2 -->\n4.4 报告期内基金投资策略和运作分析\n本基金本季度增持先进制造，并依据企业经营和估值调整组合。\n4.5 报告期内基金的业绩表现\n")
    return output.getvalue()


class ExtractionTests(unittest.TestCase):
    def test_env_names_and_page_markers(self):
        tokens = mineru_tokens_from_env({"MINER_U_KEYS": "first,second", "MINERU_API_KEY": "first"})
        self.assertEqual(tokens, [("MINER_U_KEYS_1", "first"), ("MINER_U_KEYS_2", "second")])
        self.assertEqual(markdown_pages("<!-- page: 1 -->one\n<!-- page_number: 2 -->two"), ["one", "two"])
        self.assertEqual(markdown_page_records("<!-- page: 7 -->seven")[0][0], 7)

    def test_mineru_batch_put_poll_and_full_markdown_are_in_memory(self):
        fixture = MinerFixture(fixture_zip())
        parser = lambda pages, source_url, period_end, kind: {"navYuan": None, "narrative": {"text": pages[-1], "page": 2}}
        with patch.dict("os.environ", {}, clear=True):
            result = extract_report(
                b"%PDF synthetic", "https://example.invalid/report.pdf", "2026-06-30", "quarterly",
                parse_pages=parser, mineru_client=MinerUClient(session=fixture, poll_interval=0.01),
                env={"MINERU_API_KEY": "synthetic-token"},
            )
        self.assertEqual(result["parseMethod"], "mineru")
        self.assertEqual(result["pageStats"]["pageCount"], 2)
        self.assertEqual(result["strategyExcerpts"][0]["page"], 2)
        self.assertIn("调仓/交易", result["strategyThemes"])
        self.assertEqual(fixture.polls, 2)
        self.assertNotIn("full.md", json.dumps(result, ensure_ascii=False))

    def test_no_key_uses_pypdf_and_reports_method(self):
        parser = lambda pages, source_url, period_end, kind: {"navYuan": 1, "narrative": None}
        with patch.object(report_extract, "_pypdf_pages", return_value=["synthetic page"]):
            result = extract_report(b"%PDF synthetic", "https://example.invalid/report.pdf", "2026-06-30", "quarterly",
                                    parse_pages=parser, env={})
        self.assertEqual(result["parseMethod"], "pypdf")
        self.assertEqual(result["pageStats"]["nonEmptyPages"], 1)

    def test_strategy_heading_can_continue_on_next_original_page(self):
        parser = lambda pages, source_url, period_end, kind: {"navYuan": 1, "narrative": None}
        with patch.object(report_extract, "_pypdf_pages", return_value=[
            "4.4 报告期内基金投资策略和运作分析", "本基金根据估值和基本面调整组合，增持先进制造。",
        ]):
            result = extract_report(b"%PDF synthetic", "https://example.invalid/report.pdf", "2026-06-30", "quarterly",
                                    parse_pages=parser, env={})
        self.assertEqual(result["strategyExcerpts"][0]["page"], 2)

    def test_mineru_failure_is_fixed_code_and_falls_back(self):
        class Failed:
            def parse_with_token(self, *_args, **_kwargs):
                raise report_extract.ExtractionError("MINERU_TIMEOUT")
        parser = lambda pages, source_url, period_end, kind: {"navYuan": 1, "narrative": None}
        with patch.object(report_extract, "_pypdf_pages", return_value=["synthetic page"]):
            result = extract_report(b"%PDF synthetic", "https://example.invalid/report.pdf", "2026-06-30", "quarterly",
                                    parse_pages=parser, mineru_client=Failed(), env={"MINER_U": "synthetic-token"})
        self.assertEqual(result["parseMethod"], "pypdf-fallback")
        self.assertEqual(result["parseErrorCode"], "MINERU_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
