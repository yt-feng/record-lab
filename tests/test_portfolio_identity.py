"""Small synthetic report identities and collect/extract bridges; no requests."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from portfolio_identity import parse_portfolio_identity, normalize_portfolio_identity
from report_extract import extract_report
from providers import PublicProvider, parse_pdf_pages
from collect import collect_fund

NOW = "2026-10-26T12:00:00+00:00"
RECEIVED = "2026-07-21T10:00:00+00:00"
PDF = b"%PDF synthetic identity"
REPORT = {"title": "合成基金2026年第2季度报告", "fundCode": "000001",
          "sourceUrl": "https://example.org/quarter.pdf", "periodEnd": "2026-06-30",
          "publishedAt": "2026-07-21", "kind": "quarterly"}
PAGES = ["合成基金2026年第2季度报告\n报告期2026年4月1日至2026年6月30日",
         "§2 基金产品概况\n基金主代码 000001\n基金名称 合成基金\n下属分级基金的交易代码\n000001 000002\n",
         "§3 主要财务指标和基金净值表现\n3.1 主要财务指标"]


def proof(pages=None, report=None, **kwargs):
    return parse_portfolio_identity(pages or PAGES, report or REPORT, retrieved_at=RECEIVED,
        pdf_sha256=hashlib.sha256(PDF).hexdigest(), now=NOW, **kwargs)


class PortfolioIdentityTests(unittest.TestCase):
    def test_optional_branch_diagnostics_do_not_change_identity_acceptance(self):
        diagnostics = {}
        self.assertEqual(proof(diagnostics=diagnostics), proof())
        self.assertEqual(diagnostics, {'branch': 'VERIFIED', 'field': None})
        cases = [([PAGES[0].replace('第2', '第3'), *PAGES[1:]], None, 'PERIOD_TITLE_MISMATCH'),
                 ([PAGES[0], PAGES[1].replace('基金主代码', '基金代码'), PAGES[2]], None, 'MAIN_CODE_LABEL_NOT_FOUND'),
                 (PAGES[:2], None, 'IDENTITY_SECTION_END_NOT_FOUND'),
                 (PAGES, [1, 2, 4], 'IDENTITY_SECTION_PAGE_MAPPING_INVALID'),
                 ([PAGES[0], PAGES[1].replace('基金主代码 000001', '基金主代码 待定'), PAGES[2]], None, 'CODE_ROW_INVALID')]
        for pages, numbers, reason in cases:
            diagnostics = {}
            self.assertIsNone(proof(pages, page_numbers=numbers, diagnostics=diagnostics))
            self.assertIsNone(proof(pages, page_numbers=numbers))
            self.assertEqual(diagnostics['branch'], reason)
            self.assertNotIn('基金', json.dumps(diagnostics, ensure_ascii=False))

    def test_explicit_rows_and_independent_physical_pages(self):
        value = proof()
        self.assertEqual(value["shareClassCodes"], ["000001", "000002"])
        self.assertEqual(value["identitySection"]["pages"], [2, 3])
        self.assertTrue(value["eligibleForShareClassExpansion"])
        self.assertFalse(value["shareClassUniverseComplete"])
        self.assertFalse(value["shareClassSpecificMetricsProvided"])
        self.assertEqual(value["retrievedAt"], RECEIVED)
        self.assertNotIn("基金名称", json.dumps(value, ensure_ascii=False))
        remapped = proof(page_numbers=[4, 5, 6])
        self.assertEqual(remapped["reportPeriodEvidence"]["page"], 4)
        self.assertEqual(remapped["rows"][1]["page"], 5)

    def test_cross_page_explicit_code_row(self):
        pages = [PAGES[0], PAGES[1].replace("000001 000002\n", "000001\n"),
                 "000002\n" + PAGES[2]]
        value = proof(pages)
        self.assertEqual(value["rows"][1]["pages"], [2, 3])
        self.assertEqual(value["shareClassCodes"], ["000001", "000002"])

    def test_rejects_unknown_gapped_pages_wrong_period_and_unbounded_section(self):
        for pages, numbers in [(PAGES, [None, None, None]), (PAGES, [1, 2, 4]),
                               ([PAGES[0].replace("第2", "第3"), *PAGES[1:]], None),
                               (PAGES[:2], None), ([*PAGES, PAGES[1], PAGES[2]], None)]:
            with self.subTest(pages=pages, numbers=numbers):
                self.assertIsNone(proof(pages, page_numbers=numbers))

    def test_no_name_inference_no_current_profile_codes_and_duplicate_rejection(self):
        main_only = [PAGES[0], "§2 基金产品概况\n基金主代码 000001\n下属份额简称 合成A 合成C\n", PAGES[2]]
        value = proof(main_only)
        self.assertEqual(value["shareClassCodes"], [])
        self.assertFalse(value["eligibleForShareClassExpansion"])
        for row in ["000001 000001", "000002 000003", "000001 000002 600000公司地址",
                    "000001 000002\n基金主代码 000003"]:
            self.assertIsNone(proof([PAGES[0], PAGES[1].replace("000001 000002", row), PAGES[2]]))

    def test_receipt_hash_period_source_and_fund_identity_are_required(self):
        value = proof()
        changes = [{"retrievedAt": None}, {"retrievedAt": "2026-10-27T00:00:00Z"},
                   {"retrievedAt": "2026-07-20T00:00:00Z"}, {"pdfSha256": None},
                   {"sourceUrl": "https://example.org/other.pdf"}, {"periodEnd": "2026-09-30"},
                   {"shareClassUniverseComplete": True}, {"shareClassSpecificMetricsProvided": True},
                   {"sourceTextHash": "bad"}]
        for changeset in changes:
            with self.subTest(changes=changeset):
                self.assertIsNone(normalize_portfolio_identity({**value, **changeset}, REPORT, now=NOW))
        self.assertIsNone(normalize_portfolio_identity(value, {**REPORT, "fundCode": "000003"}, now=NOW))
        self.assertIsNone(parse_portfolio_identity(PAGES, REPORT, pdf_sha256=hashlib.sha256(PDF).hexdigest()))
        clean = normalize_portfolio_identity({**value, "untrustedFullText": "omit"}, REPORT, now=NOW)
        self.assertNotIn("untrustedFullText", clean)

    def test_pypdf_common_extraction_has_raw_hash_and_original_receipt(self):
        with patch("report_extract._pypdf_pages", return_value=PAGES):
            result = extract_report(PDF, REPORT["sourceUrl"], REPORT["periodEnd"], REPORT["kind"],
                parse_pages=parse_pdf_pages, published_at=REPORT["publishedAt"], retrieved_at=RECEIVED, env={})
        self.assertEqual(result["portfolioIdentityEvidence"], proof())

    def test_mineru_only_accepts_verified_physical_page_markers(self):
        class Miner:
            def __init__(self, numbered): self.numbered = numbered
            def parse_with_token(self, *_args):
                return "\n".join((f"<!-- page: {index} -->\n" if self.numbered else "") + text
                                 for index, text in enumerate(PAGES, 1))
        for numbered in [True, False]:
            result = extract_report(PDF, REPORT["sourceUrl"], REPORT["periodEnd"], REPORT["kind"],
                parse_pages=parse_pdf_pages, mineru_client=Miner(numbered), env={"MINERU_API_KEY": "synthetic"},
                published_at=REPORT["publishedAt"], retrieved_at=RECEIVED)
            self.assertEqual(result["parseMethod"], "mineru")
            self.assertEqual(bool(result["portfolioIdentityEvidence"]), numbered)

    def test_provider_captures_download_receipt_before_extraction(self):
        source = PublicProvider()
        with patch.object(source, "get", return_value=PDF), patch("report_extract._pypdf_pages", return_value=PAGES), \
                patch.dict("os.environ", {}, clear=True):
            value = source.report_details(REPORT)["portfolioIdentityEvidence"]
        self.assertIsNotNone(value)
        self.assertEqual(value["pdfSha256"], hashlib.sha256(PDF).hexdigest())
        self.assertIsNotNone(normalize_portfolio_identity(value, REPORT))

    def test_collect_target_identity_cannot_be_overwritten_by_baseline(self):
        baseline = {**REPORT, "periodEnd": "2026-03-31", "publishedAt": "2026-04-21",
                    "sourceUrl": "https://example.org/baseline.pdf"}
        class Source:
            def overview(self, _code): return {"name": "合成A", "company": "合成机构", "managers": []}
            def announcements(self, *_args): return [REPORT, baseline]
            def holdings(self, *_args):
                return {r["periodEnd"]: [{"stockCode": "600001", "stockName": "合成证券", "market": "CN",
                    "currency": "CNY", "shares": 100, "weightPct": 1, "marketValueYuan": 100}] for r in [REPORT, baseline]}
            def report_details(self, document):
                pages = PAGES if document == REPORT else [p.replace("第2", "第1").replace("000001 000002", "000001 000003") for p in PAGES]
                value = proof(pages, document)
                return {"portfolioCode": value["sourcePortfolioCode"], "shareClassCodes": value["shareClassCodes"],
                        "portfolioIdentityEvidence": value, "navYuan": 10000, "narrative": None}
        for target in ["2026-06-30", None]:
            fund, _ = collect_fund({"code": "000001"}, "2026-10-26", provider=Source(),
                target_period=target, baseline_period="2026-03-31" if target else None)
            self.assertEqual(fund["shareClassCodes"], ["000001", "000002"])
            self.assertEqual(fund["portfolioSourceUrl"], REPORT["sourceUrl"])
            self.assertEqual([r["portfolioIdentityEvidence"]["shareClassCodes"] for r in fund["reports"]],
                             [["000001", "000002"], ["000001", "000003"]])

    def test_interim_identity_stays_local_when_quarterly_strategy_is_attached(self):
        interim = {**REPORT, "kind": "semiannual", "publishedAt": "2026-08-31",
                   "sourceUrl": "https://example.org/interim.pdf"}
        class Source:
            def overview(self, _code): return {"name": "合成基金", "company": "合成机构", "managers": []}
            def announcements(self, *_args): return [interim, REPORT]
            def holdings(self, *_args):
                return {"2026-06-30": [{"stockCode": "600001", "market": "CN", "currency": "CNY",
                                       "shares": 100, "weightPct": 1, "marketValueYuan": 100}]}
            def report_details(self, document):
                pages = PAGES if document["kind"] == "quarterly" else [
                    PAGES[0].replace("第2季度", "中期"),
                    PAGES[1].replace("§2 基金产品概况", "2.1 基金基本情况").replace("000001 000002", "000001 000002 000003"),
                    "2.2 基金产品说明"]
                value = parse_portfolio_identity(pages, document, retrieved_at="2026-09-01T10:00:00Z",
                    pdf_sha256=hashlib.sha256(PDF).hexdigest(), now=NOW)
                return {"portfolioCode": "000001", "shareClassCodes": value["shareClassCodes"],
                        "portfolioIdentityEvidence": value, "navYuan": 10000, "narrative": None}
        fund, _ = collect_fund({"code": "000001"}, "2026-10-26", provider=Source(), target_period="2026-06-30")
        self.assertEqual(fund["reports"][0]["portfolioIdentityEvidence"]["sourceUrl"], interim["sourceUrl"])
        self.assertEqual(fund["reports"][0]["portfolioIdentityEvidence"]["kind"], "semiannual")
        self.assertEqual(fund["shareClassCodes"], ["000001", "000002", "000003"])


if __name__ == "__main__":
    unittest.main()
