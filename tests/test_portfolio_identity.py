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
    def reit_fixture(self):
        return json.loads((Path(__file__).parent / 'fixtures/portfolio-identity-reit.json').read_text())

    def test_reit_observed_bounded_parent_child_format_and_matching_two_code_rows(self):
        fixture = self.reit_fixture()
        value = parse_portfolio_identity(fixture['pages'], fixture['report'], page_numbers=fixture['pageNumbers'],
            retrieved_at=fixture['proof']['retrievedAt'], pdf_sha256=fixture['proof']['pdfSha256'], now=NOW)
        self.assertEqual(value, fixture['proof'])
        self.assertEqual(value['method'], 'pdf-explicit-reit-code-rows-v1')
        self.assertEqual(value['sourcePortfolioCode'], '000008')
        self.assertEqual([r['field'] for r in value['rows']], ['mainCode', 'tradingCode'])
        self.assertEqual(value['shareClassCodes'], [])
        self.assertFalse(value['eligibleForShareClassExpansion'])
        self.assertEqual(value['identitySection']['endHeading'], '2.2 不动产 项目基本情况说明')
        self.assertEqual(value['identitySection']['subheadingPage'], 2)
        self.assertTrue(value['identitySection']['subheadingAdjacent'])
        self.assertNotIn('noEquitiesConfirmed', value)

    def test_reit_parser_rejects_unbounded_or_ambiguous_sections_and_conflicting_codes(self):
        fixture = self.reit_fixture()
        pages = fixture['pages']
        mutations = [
            [pages[0], pages[1].replace('§ 2  不动产 基金产品概况\n', ''), pages[2]],
            [pages[0], pages[1].replace('2.1 不动产 基金产品基本情况\n', ''), pages[2]],
            [pages[0], pages[1].replace('2.1 不动产', '基金代码 000009\n2.1 不动产'), pages[2]],
            [pages[0], pages[1].replace('基金代码  000008', '基金主代码 000008'), pages[2]],
            [pages[0], pages[1].replace('交易代码  000008', '交易代码 000009'), pages[2]],
            [pages[0], pages[1].replace('交易代码  000008', '交易代码 000008 000009'), pages[2]],
            [pages[0], pages[1].replace('交易代码  000008', ''), pages[2]],
            [pages[0], pages[1] + '\n基金代码 000008', pages[2]],
            [pages[0], pages[1] + '\n交易代码 000008', pages[2]],
            [pages[0], pages[1] + '\n基金主代码 000009', pages[2]],
            [pages[0], pages[1], pages[2].replace('2.2 不动产 项目基本情况说明', '2.2 其他说明')],
            [pages[0], pages[1], pages[2], pages[1], pages[2]],
            [pages[0].replace('第 2', '第 3'), pages[1], pages[2]],
            [pages[0], pages[1].replace('000008', '000009'), pages[2]],
        ]
        for changed in mutations:
            with self.subTest(changed=changed):
                self.assertIsNone(parse_portfolio_identity(changed, fixture['report'],
                    retrieved_at=fixture['proof']['retrievedAt'], pdf_sha256=fixture['proof']['pdfSha256'], now=NOW))
        for numbers in ([1, None, 3], [1, 2, 4]):
            self.assertIsNone(parse_portfolio_identity(pages, fixture['report'], page_numbers=numbers,
                retrieved_at=fixture['proof']['retrievedAt'], pdf_sha256=fixture['proof']['pdfSha256'], now=NOW))

    def test_reit_normalizer_revalidates_pair_and_format_not_generic_fund_code(self):
        fixture = self.reit_fixture()
        def check(change):
            value = copy.deepcopy(fixture['proof']); change(value)
            self.assertIsNone(normalize_portfolio_identity(value, fixture['report'], now=NOW))
        for change in [
            lambda p: p.update(method='pdf-explicit-fund-code-rows-v1'),
            lambda p: p.update(rows=p['rows'][:1]),
            lambda p: p['rows'][1].update(text='交易代码 000009', codes=['000009']),
            lambda p: p['rows'][1].update(text='基金代码 000008'),
            lambda p: p['rows'][1].update(field='shareClassCodes', text='下属分类基金的交易代码 000008'),
            lambda p: p.update(shareClassCodes=['000008']),
            lambda p: p['identitySection'].update(subheading='2.1 基金基本情况'),
            lambda p: p['identitySection'].update(subheadingPage=3),
            lambda p: p['identitySection'].update(subheadingAdjacent=False),
            lambda p: p['identitySection'].update(endHeading='§3 主要财务指标和不动产基金运作情况'),
        ]:
            with self.subTest(change=change): check(change)

    def test_reit_common_extraction_exposes_identity_without_inventing_holdings(self):
        fixture = self.reit_fixture()
        with patch('report_extract._pypdf_pages', return_value=fixture['pages']):
            report = fixture['report']
            result = extract_report(b'%PDF synthetic reit identity', report['sourceUrl'], report['periodEnd'], report['kind'],
                parse_pages=parse_pdf_pages, published_at=report['publishedAt'], retrieved_at=RECEIVED, env={})
        self.assertEqual(result['portfolioIdentityEvidence'], fixture['proof'])
        self.assertIsNone(result['noEquitiesEvidence'])
        self.assertIsNone(result.get('navYuan'))

    def test_quarterly_adjacent_parent_child_titles_share_one_bounded_identity_section(self):
        # The 159366 cloud diagnostic showed these exact two adjacent headings.
        pages = [PAGES[0], PAGES[1].replace('§2 基金产品概况\n', '§2 基金产品概况\n2.1 基金基本情况\n'), PAGES[2]]
        value = proof(pages)
        self.assertEqual(value['identitySection']['heading'], '§2 基金产品概况')
        self.assertEqual(value['identitySection']['endHeading'], '§3 主要财务指标和基金净值表现')
        self.assertEqual(value['identitySection']['pages'], [2, 3])
        self.assertEqual(value['sourcePortfolioCode'], '000001')
        self.assertEqual(value['shareClassCodes'], ['000001', '000002'])
        self.assertEqual(value['rows'][0]['pages'], [2])
        self.assertEqual(value['pdfSha256'], hashlib.sha256(PDF).hexdigest())
        # A later 2.2 subsection does not truncate the quarterly parent section.
        value = proof([pages[0], pages[1] + '2.2 基金其他说明\n本节无额外代码。\n', pages[2]])
        self.assertEqual(value['identitySection']['endHeading'], '§3 主要财务指标和基金净值表现')

    def test_nested_titles_do_not_admit_toc_disjoint_duplicate_or_conflicting_tables(self):
        nested = PAGES[1].replace('§2 基金产品概况\n', '§2 基金产品概况\n2.1 基金基本情况\n')
        cases = [
            [PAGES[0], '§2 基金产品概况\n2.1 基金基本情况\n目录\n', nested, PAGES[2]],
            [PAGES[0], nested + '2.1 基金基本情况\n基金主代码 000001\n', PAGES[2]],
            [PAGES[0], PAGES[1], PAGES[2], '2.1 基金基本情况\n基金主代码 000003\n2.2 基金产品说明'],
            [PAGES[0], nested.replace('2.1 基金基本情况', '基金主代码 000003\n2.1 基金基本情况'), PAGES[2]],
            [PAGES[0], nested + '基金主代码 000003\n', PAGES[2]],
            [PAGES[0], nested.replace('000001 000002', '000002 000003'), PAGES[2]],
            [PAGES[0].replace('第2', '第3'), nested, PAGES[2]],
            [PAGES[0], nested, PAGES[2], nested, PAGES[2]],
        ]
        for pages in cases:
            with self.subTest(pages=pages): self.assertIsNone(proof(pages))
        for numbers in ([1, None, 3], [1, 2, 4]):
            self.assertIsNone(proof([PAGES[0], nested, PAGES[2]], page_numbers=numbers))

    def test_interim_uses_its_own_2_1_to_2_2_section_and_cannot_borrow_quarterly_parent(self):
        report = {**REPORT, 'kind': 'semiannual'}
        pages = [PAGES[0].replace('第2季度', '中期'), PAGES[1].replace('§2 基金产品概况', '2.1 基金基本情况'),
                 '2.2 基金产品说明']
        value = proof(pages, report)
        self.assertEqual(value['identitySection']['heading'], '2.1 基金基本情况')
        self.assertEqual(value['identitySection']['endHeading'], '2.2 基金产品说明')
        self.assertIsNone(proof([pages[0], '§2 基金产品概况\n' + pages[1], pages[2]], report))

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
