"""Synthetic fixed-source diagnosis checks; no downloads or PDF parsing."""
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import diagnose_nonstock_retention as diag
from providers import DataError, parse_no_equities

NOW = dt.datetime(2026, 10, 29, 2, tzinfo=dt.timezone.utc)


def details(sample, proof=True):
    pages = ['合成基金2026年第2季度报告\n5.1 报告期末基金资产组合情况\n本报告期末本基金未持有股票。\n5.2 其他投资']
    return {'parseMethod': 'pypdf', 'sourceTextHash': hashlib.sha256(pages[0].encode()).hexdigest(),
            'pageStats': {'pageCount': 1, 'totalPages': 1, 'processedPages': 1, 'charCount': len(pages[0]),
                          'truncated': False, 'pageNumbersVerified': True}, 'pdfHoldings': [],
            'noEquitiesEvidence': parse_no_equities(pages, diag.document(sample)) if proof else None,
            'reportPdfSourceUrl': sample['sourceUrl'], 'reportPdfKind': sample['kind'],
            'reportPdfPeriodEnd': sample['periodEnd']}


def anchors():
    result = {}
    for sample in diag.SAMPLES:
        report = {'title': '合成基金2026年中期报告', 'sourceUrl': sample['interimUrl'], 'kind': 'semiannual',
                  'periodEnd': '2026-06-30', 'publishedAt': '2026-08-31', 'holdings': [],
                  'noEquitiesEvidence': None, 'noEquitiesConfirmed': False, 'reportOnly': True,
                  'reportPdfSourceUrl': sample['sourceUrl'], 'reportPdfKind': 'quarterly',
                  'reportPdfPeriodEnd': '2026-06-30', 'sourceTextHash': 'a' * 64, 'parseMethod': 'pypdf',
                  'strategySectionCoverage': {**diag.document(sample), 'complete': False},
                  'pageStats': details(sample)['pageStats'], 'evidenceRetrievedAt': '2026-10-28T12:34:56.123456Z'}
        result[sample['code']] = {'code': sample['code'], 'name': '合成基金', 'company': '合成机构',
                                  'companyId': '80000000', 'selectedReport': report}
    return result


def reit_pages():
    """Synthetic 69-page layout using observed boundaries, never real text."""
    pages = ['合成基金2026年中期报告'] + ['合成正文'] * 68
    pages[3] = '目录\n7.1.2 基金经理（或基金经理小组）简介 ...... 20\n7.4 合成投资运作分析 ...... 22'
    pages[5] = '\n'.join(f'4.{i} 合成项目运营情况\n合成项目资料。' for i in range(1, 26))
    pages[19] = ('§ 7 管理人报告\n7.1 基金管理人情况\n'
                 '7.1.2 基金经理（或基金经理小组）简介\n'
                 '姓名 职务 任本基金的基金经理期限\n任职日期 离任日期 证券从业年限 说明\n'
                 '张甲 不动产基金经理 2022年1月1日 - 20年\n'
                 '曾任某机构负责人，毕业于合成大学。\n资格证书编号 12345678901234\n')
    pages[20] = ('李乙 本基金基金经理 2023年1月1日 - 15年\n'
                 '联系人 私人联系信息\n'
                 '7.1.3 报告期末兼任私募资产管理计划投资经理的不动产基金经理\n'
                 '同时管理的产品情况\n无。\n7.2 管理人合规情况\n本节为合成合规说明。\n'
                 '7.4 合成投资运作分析\n本基金根据合成运营情况维持投资安排。\n'
                 '本基金重视项目现金流与服务质量。\n7.5 合成未来展望\n未来将关注合成项目运行。\n'
                 '7.6 其他情况\n无。\n§ 8 托管人报告')
    pages[65] = ('14.3 不动产基金投资策略的改变\n本报告期内，本基金无投资策略的改变。\n'
                 '14.4 为不动产基金进行审计的会计师事务所情况\n合成事务所。')
    return pages


def manager_grids():
    from foreign_holdings import Cell, GridPage
    ranges = [(0, 30), (30, 130), (130, 200), (200, 270), (270, 320), (320, 500)]
    labels = ['姓名', '职务', '任职日期', '离任日期', '证券从业年限', '说明']
    header = [Cell(*bounds, 700, 720, value) for bounds, value in zip(ranges, labels)]
    def row(values): return [Cell(*bounds, 600, 680, value) for bounds, value in zip(ranges, values)]
    first = header + row(['张甲', '不动产基金经理', '2022年1月1日', '-', '20年', '曾任负责人，资格证书编号 12345678901234'])
    second = row(['李乙', '本基金基金经理', '2023年1月1日', '-', '15年', '毕业于合成大学，联系电话12345678901'])
    return [GridPage(20, first, [(750, '7.1.2基金经理（或基金经理小组）简介')]),
            GridPage(21, second, [(500, '7.1.3报告期末兼任私募资产管理计划投资经理的不动产基金经理同时管理的产品情况')])]


class NonstockRetentionTests(unittest.TestCase):
    def test_real_collector_and_builder_retain_quarter_proof_without_source_transplant(self):
        sample = diag.SAMPLES[0]
        observed = anchors()[sample['code']]
        before = copy.deepcopy(observed)
        current = details(sample)
        direct = diag.replay(sample, current, observed, NOW, False)
        mixed = diag.replay(sample, current, observed, NOW, True)
        self.assertEqual(direct['fundStatus'], 'no-equities')
        self.assertTrue(direct['selectedReport']['noEquitiesEvidence']['sourceMatchesDocument'])
        self.assertEqual(direct['selectedReport']['sourceUrl'], sample['sourceUrl'])
        self.assertEqual(mixed['fundStatus'], 'no-equities')
        self.assertEqual(mixed['selectedReport']['sourceUrl'], sample['sourceUrl'])
        self.assertEqual(mixed['selectedReport']['reportPdfSourceUrl'], sample['sourceUrl'])
        self.assertTrue(mixed['selectedReport']['noEquitiesEvidence']['sourceMatchesDocument'])
        self.assertEqual(len(mixed['collectorReports']), 2)
        self.assertEqual(mixed['collectorReports'][0]['sourceUrl'], sample['interimUrl'])
        self.assertFalse(mixed['collectorReports'][0]['noEquitiesEvidence']['present'])
        self.assertIn('HOLDINGS_UNRESOLVED', mixed['collectorErrorCodes'])
        self.assertEqual(mixed['detailsReadOrder'], [sample['interimUrl'], sample['sourceUrl']])
        self.assertFalse(mixed['independentInterimReparse'])
        self.assertEqual(mixed['priceRequests'], 0)
        self.assertEqual(observed, before)

    def test_source_mismatch_rejects_quarter_and_preserves_separate_fallback_source(self):
        sample = diag.SAMPLES[0]
        current = details(sample)
        current['noEquitiesEvidence']['sourceUrl'] = sample['interimUrl']
        current['noEquitiesEvidence']['kind'] = 'semiannual'
        outcome = diag.replay(sample, current, anchors()[sample['code']], NOW, False)
        # The builder rejects the quarter input and may make a separate
        # fund-level evidence report. Its status alone cannot prove that
        # the quarterly input survived validation.
        self.assertEqual(outcome['fundStatus'], 'no-equities')
        self.assertFalse(outcome['collectorReports'][0]['noEquitiesEvidence']['sourceMatchesDocument'])
        self.assertEqual(outcome['buildErrorCount'], 1)
        self.assertIn('INVALID_NO_EQUITIES_EVIDENCE', outcome['buildErrorCodes'])
        self.assertEqual(outcome['selectedReport']['sourceUrl'], sample['interimUrl'])
        self.assertEqual(outcome['selectedReport']['kind'], 'semiannual')
        self.assertTrue(outcome['selectedReport']['noEquitiesEvidence']['sourceMatchesDocument'])
        result = diag.run_diagnosis(object(), anchors(), now=NOW,
            extract=lambda _provider, _sample, _now: (current, {'windows': []}))
        self.assertEqual(result['results'][0]['classification'], 'REVIEW_RETAINED_PATH')

    def test_missing_proof_remains_unresolved_not_fund_type_inference(self):
        result = diag.run_diagnosis(object(), anchors(), now=NOW,
            extract=lambda _provider, sample, _now: (details(sample, False), {'windows': []}))
        self.assertEqual(result['status'], 'diagnosed')
        self.assertTrue(all(r['classification'] == 'QUARTER_PROOF_UNAVAILABLE' for r in result['results']))
        self.assertTrue(all(r['quarterOnly']['fundStatus'] != 'no-equities' for r in result['results']))
        self.assertFalse(result['productionDataUpdated'])
        self.assertFalse(result['provesFullCollection'])

    def test_small_output_two_cases_and_original_anchor_clock_preserved(self):
        result = diag.run_diagnosis(object(), anchors(), now=NOW,
            extract=lambda _provider, sample, _now: (details(sample), {'pdfSha256': 'b' * 64, 'pdfBytes': 100, 'windows': []}))
        self.assertEqual(len(result['results']), 2)
        self.assertEqual(result['marketRequests'], 0)
        self.assertEqual(result['modelRequests'], 0)
        for row in result['results']:
            self.assertEqual(row['classification'], 'QUARTER_PROOF_RETAINED_SEPARATELY')
            self.assertFalse(row['quarterDetails']['identityVerified'])
            self.assertEqual(row['retainedAnchor']['originalEvidenceRetrievedAt'], '2026-10-28T12:34:56.123456Z')
            self.assertEqual(row['retainedAnchor']['quarterPublicationMetadata'],
                [{'field': 'selectedReport.strategySectionCoverage', 'publishedAt': '2026-07-21'}])
        encoded = json.dumps(result, ensure_ascii=False).encode()
        self.assertLess(len(encoded), diag.MAX_OUTPUT)
        self.assertEqual(diag.inspect_content(encoded), [])

    def test_fixed_anchor_digest_size_source_and_no_equity_assertion_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'part.json'
            data = json.dumps({'funds': list(anchors().values())}).encode()
            path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual(set(diag.load_anchors(path, expected_digest=digest)), {'003589', '508007'})
            with self.assertRaisesRegex(DataError, 'ANCHOR_DIGEST_MISMATCH'):
                diag.load_anchors(path)
            bad = anchors()
            bad['003589']['selectedReport']['sourceUrl'] = diag.SAMPLES[0]['sourceUrl']
            modified = json.dumps({'funds': list(bad.values())}).encode()
            path.write_bytes(modified)
            with self.assertRaisesRegex(DataError, 'ANCHOR_INVALID'):
                diag.load_anchors(path, expected_digest=hashlib.sha256(modified).hexdigest())

    def test_anchor_quarter_date_is_retained_metadata_not_pdf_filename_date(self):
        sample = diag.SAMPLES[1]
        self.assertIn('20260720', sample['sourceUrl'])
        self.assertEqual(sample['publishedAt'], '2026-07-21')
        fund = anchors()[sample['code']]
        self.assertEqual(diag.quarter_publication_metadata(fund, sample)[0]['publishedAt'], '2026-07-21')
        fund['selectedReport']['strategySectionCoverage']['publishedAt'] = '2026-07-20'
        with self.assertRaisesRegex(DataError, 'ANCHOR_PUBLICATION_MISMATCH'):
            diag.quarter_publication_metadata(fund, sample)

    def test_load_anchor_rejects_missing_or_conflicting_quarter_publication_even_with_valid_digest(self):
        for change, expected in [('missing', 'ANCHOR_PUBLICATION_MISSING'), ('conflict', 'ANCHOR_PUBLICATION_MISMATCH')]:
            items = anchors()
            report = items['508007']['selectedReport']
            if change == 'missing':
                del report['strategySectionCoverage']
            else:
                report['reportPdfPublishedAt'] = '2026-07-20'
            data = json.dumps({'funds': list(items.values())}).encode()
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'part.json'
                path.write_bytes(data)
                with self.assertRaisesRegex(DataError, expected):
                    diag.load_anchors(path, expected_digest=hashlib.sha256(data).hexdigest())

    def test_partial_quarter_metadata_binds_date_without_claiming_strategy_complete(self):
        sample = diag.SAMPLES[1]
        fund = anchors()[sample['code']]
        report = fund['selectedReport']
        report['managerTableCoverage'] = {**diag.document(sample), 'complete': False}
        before = copy.deepcopy(fund)
        bindings = diag.quarter_publication_metadata(fund, sample)
        self.assertEqual(len(bindings), 2)
        self.assertEqual(fund, before)
        self.assertFalse(report['strategySectionCoverage']['complete'])
        self.assertFalse(report['managerTableCoverage']['complete'])

    def test_table_windows_are_bounded_and_exclude_personal_and_manager_sections(self):
        private_email = 'private' + '@' + 'example.org'
        pages = ['4.1 基金经理情况\n姓名 张某\n联系方式 不输出\n'
                 '5.1 报告期末基金资产组合情况\n' + ('1 固定收益投资 100.00 100.00\n' * 80) +
                 '\n5.2 其他投资\n经理私人信息\n'
                 f'5.9.3 其他资产构成\n1 应收利息 0.00\n电子邮件 {private_email}\n不输出\n§6 基金份额'] * 5
        windows = diag.bounded_table_windows(pages, [1, 2, 3, 4, 5])
        self.assertTrue(windows)
        self.assertLessEqual(len(windows), 6)
        self.assertLessEqual(sum(len(w['text']) for w in windows), 1800)
        self.assertTrue(all(len(w['text']) <= 500 for w in windows))
        text = json.dumps(windows, ensure_ascii=False)
        for private in ('张某', private_email, '不输出', '经理私人信息'):
            self.assertNotIn(private, text)
        self.assertTrue(windows[0]['truncated'])
        self.assertEqual(windows[0]['followingHeading'], '5.2 其他投资')

    def test_fetch_adapter_requests_only_three_fixed_pdfs_once_no_mineru(self):
        class Provider:
            def __init__(self): self.calls = []
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return b'%PDF-synthetic-not-parsed'
        provider = Provider()
        samples = [*diag.SAMPLES, diag.interim_sample(anchors()['508007'], NOW)]
        def synthetic_extract(_bytes, url, period, kind, **kwargs):
            self.assertEqual(kwargs['env'], {})
            self.assertEqual(kwargs['max_pages'], 300)
            kwargs['on_private_sections'](pages=['5.1 报告期末基金资产组合情况\n1 债券 100\n5.2 后续情况'], page_numbers=[2])
            return details(next(s for s in samples if s['sourceUrl'] == url))
        with patch.object(diag, 'extract_report', side_effect=synthetic_extract):
            for sample in samples:
                current, meta = diag.extract_quarter(provider, sample, NOW)
                self.assertEqual(current['reportPdfSourceUrl'], sample['sourceUrl'])
                self.assertEqual(len(meta['windows']), 0 if sample['kind'] == 'semiannual' else 1)
                if sample['kind'] == 'semiannual':
                    self.assertEqual(meta['reitFieldObservation']['additionalHttpRequests'], 0)
                    self.assertEqual(meta['reitFieldObservation']['geometry']['reasonCode'], 'OBSERVED_BOUNDARY_NOT_FOUND')
                self.assertEqual(meta['documentStructure']['identityRecognizer']['branch'], 'PERIOD_TITLE_NOT_FOUND')
                self.assertTrue(meta['documentStructure']['diagnosticOnly'])
        self.assertEqual([url for url, _ in provider.calls], [s['sourceUrl'] for s in samples])
        self.assertTrue(all(kw == {'binary': True, 'attempts_override': 1} for _, kw in provider.calls))

    def test_independent_interim_uses_own_source_metadata_hash_and_nav_without_anchor_refresh(self):
        original = anchors(); before = copy.deepcopy(original); calls = []
        def extract(_provider, sample, _now):
            calls.append((sample['sourceUrl'], sample['kind'], sample['publishedAt']))
            current = details(sample, proof=sample['kind'] == 'quarterly')
            current['sourceTextHash'] = hashlib.sha256(sample['sourceUrl'].encode()).hexdigest()
            if sample['kind'] == 'semiannual':
                current['navYuan'] = 2388000000.00
            return current, {'pdfSha256': hashlib.sha256(sample['sourceUrl'].encode()).hexdigest(), 'windows': []}
        result = diag.run_diagnosis(object(), original, now=NOW, extract=extract)
        self.assertEqual(len(calls), 3)
        interim = result['results'][1]['independentInterim']
        self.assertEqual(calls[-1], (diag.SAMPLES[1]['interimUrl'], 'semiannual', '2026-08-31'))
        self.assertTrue(result['independentInterimReparse'])
        self.assertTrue(interim['independentlyParsed'])
        self.assertFalse(interim['quarterTextReused'])
        self.assertNotEqual(interim['sourceTextHash'], result['results'][1]['quarterDetails']['sourceTextHash'])
        nav = interim['fieldCompleteness']['nav']
        self.assertEqual(nav['valueYuan'], 2388000000.00)
        self.assertEqual(nav['periodEnd'], '2026-06-30')
        self.assertEqual(nav['sourceUrl'], diag.SAMPLES[1]['interimUrl'])
        self.assertTrue(nav['acceptedInSnapshot'])
        self.assertFalse(nav['portfolioSubtotalUsed'])
        self.assertFalse(interim['fieldCompleteness']['strategy']['complete'])
        self.assertFalse(interim['fieldCompleteness']['managers']['complete'])
        self.assertFalse(interim['noEquitiesEvidence']['present'])
        self.assertTrue(interim['snapshot']['independentInterimReparse'])
        self.assertEqual(interim['snapshot']['mode'], 'independently-parsed-interim-only')
        self.assertEqual(original, before)

    def test_interim_missing_or_conflicting_metadata_rejected_before_fetch(self):
        for edit in [lambda x: x['selectedReport'].pop('publishedAt'),
                     lambda x: x['selectedReport'].update(sourceUrl=diag.SAMPLES[1]['sourceUrl']),
                     lambda x: x['selectedReport'].update(publishedAt='2026-10-30'),
                     lambda x: x.update(reports=[{**x['selectedReport'], 'publishedAt': '2026-08-28'}])]:
            anchor = anchors()['508007']; edit(anchor)
            with self.subTest(edit=edit), patch.object(diag, 'extract_quarter') as extract:
                with self.assertRaisesRegex(DataError, 'INTERIM_BINDING_INVALID'):
                    diag.diagnose_interim(object(), anchor, NOW, extract)
                extract.assert_not_called()
        with patch.object(diag.PublicProvider, 'get') as fetch:
            for change in ({'sourceUrl': 'https://example.org/unapproved.pdf'}, {'kind': 'annual'}):
                with self.assertRaisesRegex(DataError, 'SOURCE_NOT_ALLOWED'):
                    diag.extract_quarter(diag.PublicProvider(), {**diag.SAMPLES[1], **change}, NOW)
            fetch.assert_not_called()

    def test_interim_failure_preserves_quarter_result_and_never_reuses_its_text(self):
        def extract(_provider, sample, _now):
            if sample['kind'] == 'semiannual': raise DataError('HTTP_ERROR')
            return details(sample), {'windows': []}
        result = diag.run_diagnosis(object(), anchors(), now=NOW, extract=extract)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['independentInterimReparse'])
        self.assertEqual(result['results'][1]['status'], 'diagnosed')
        self.assertEqual(result['results'][1]['independentInterim']['errorCode'], 'HTTP_ERROR')
        self.assertFalse(result['results'][1]['independentInterim']['quarterTextReused'])

    def test_independent_interim_common_extraction_and_builder_accept_same_date_total_nav(self):
        pages = ['合成基金2026年中期报告',
                 '2.1 基金基本情况\n基金主代码 508007\n2.2 基金产品说明',
                 '4.1 基金经理简介\n姓名 职务 任职日期 离任日期 证券从业年限\n4.2 管理人报告',
                 '4.4 报告期内基金的投资策略和运作分析\n'
                 '本基金在报告期内维持审慎配置，并根据市场变化调整组合，重视现金流管理和长期投资价值。\n'
                 '4.5 报告期内基金的业绩表现',
                 '8.1 资产负债表\n单位：人民币元\n本期末 2026年6月30日 上年度末 2025年12月31日\n'
                 '净资产合计 2,388,000,000.00 2,400,000,000.00\n8.2 利润表']
        class Provider:
            def __init__(self): self.calls = []
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs)); return b'%PDF independent synthetic interim'
        provider = Provider()
        with patch('report_extract._pypdf_pages', return_value=pages):
            result = diag.diagnose_interim(provider, anchors()['508007'], NOW, diag.extract_quarter)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0][0], diag.SAMPLES[1]['interimUrl'])
        self.assertTrue(result['identityVerified'])
        self.assertEqual(result['sourcePortfolioCode'], '508007')
        self.assertEqual(result['fieldCompleteness']['nav']['valueYuan'], 2388000000.0)
        self.assertTrue(result['fieldCompleteness']['nav']['acceptedInSnapshot'])
        self.assertTrue(result['fieldCompleteness']['strategy']['complete'])
        self.assertFalse(result['fieldCompleteness']['strategy']['semanticComplete'])
        self.assertFalse(result['fieldCompleteness']['managers']['complete'])
        self.assertTrue(result['fieldCompleteness']['pdf']['allPagesProcessed'])
        self.assertFalse(result['noEquitiesEvidence']['present'])
        self.assertEqual(result['snapshot']['selectedReport']['navSourceUrl'], diag.SAMPLES[1]['interimUrl'])
        self.assertLessEqual(sum(len(row['text']) for row in result['windows']), 1800)

    def test_late_balance_sheet_and_nav_windows_survive_opening_metrics_without_full_text(self):
        pages = ['3.1 主要财务指标\n' + '本期收入 100.00\n' * 80 + '\n3.2 其他财务指标\n无。\n3.3 后续情况',
                 '8.1 资产负债表\n单位：人民币元\n本期末 2026年6月30日 上年度末 2025年12月31日\n'
                 + '其他资产 100.00\n' * 80 + '净资产合计 123,456,789.00 100,000,000.00\n8.2 利润表',
                 '4.1 基金经理简介\n姓名 职务 任职日期 离任日期 证券从业年限\n姓名 张某\n'
                 '4.2 其他报告\n5.2 其他投资情况说明\n本节是合成的其他投资说明。\n5.3 后续情况']
        windows = diag.bounded_table_windows(pages, [4, 40, 12])
        selections = [row['selection'] for row in windows]
        self.assertIn('nav-row-context', selections)
        self.assertIn('balance-sheet-header', selections)
        self.assertIn('manager-header-only', selections)
        self.assertIn('other-investment', selections)
        self.assertTrue(any('2026年6月30日' in row['text'] for row in windows))
        self.assertTrue(any('净资产合计 123,456,789.00' in row['text'] for row in windows))
        self.assertNotIn('张某', json.dumps(windows, ensure_ascii=False))
        self.assertLessEqual(sum(len(row['text']) for row in windows), 1800)
        self.assertLessEqual(len(windows), 6)
        self.assertTrue(all(len(row['text']) <= 500 for row in windows))

    def test_reit_probe_discovers_late_real_boundaries_before_bounded_selection(self):
        pages = reit_pages()
        with patch.dict('os.environ', {}, clear=True), patch.object(diag, '_read_reit_manager_grids') as read:
            result, headings, windows = diag.reit_field_observation(pages, list(range(1, 70)), b'not-parsed')
        read.assert_not_called()
        self.assertEqual(result['scannedPages'], 69)
        self.assertTrue(result['allPagesScanned'])
        self.assertTrue(result['pageNumbersVerified'])
        self.assertTrue(result['manager']['headingFound'])
        self.assertTrue(result['manager']['sectionBounded'])
        self.assertEqual((result['manager']['startPage'], result['manager']['endPage']), (20, 21))
        self.assertEqual(result['manager']['plainIdentityCandidates'], 2)
        self.assertEqual(result['manager']['currentRoleAccepted'], 1)
        self.assertEqual(result['geometry']['reasonCode'], 'ACTIONS_REQUIRED')
        self.assertGreater(result['discoveredHeadingCount'], 16)
        self.assertTrue(any('7.4 合成投资运作分析' == row['text'] for row in headings))
        self.assertTrue(any('7.1.3' in row['text'] and '同时管理的产品情况' in row['text'] for row in headings))
        self.assertTrue(any(row['section'] == '14.3' and row['classification'] == 'strategy-change-statement' for row in result['sections']))
        self.assertEqual(next(row for row in result['sections'] if row['section'] == '7.1.2')['endSection'], '7.1.3')
        self.assertLessEqual(sum(len(row['text']) for row in headings), 640)
        self.assertLessEqual(sum(len(row['text']) for row in windows), 1800)
        self.assertLessEqual(result['managerExcerptChars'], 800)
        self.assertLessEqual(result['strategyExcerptChars'], 1000)
        self.assertFalse(result['productionEvidenceCreated'])
        encoded = json.dumps([result, headings, windows], ensure_ascii=False)
        for unwanted in ('毕业于', '曾任', '资格证', '12345678901234', '私人联系信息', '联系人', '......'):
            self.assertNotIn(unwanted, encoded)
        self.assertIn('张甲', encoded)
        self.assertIn('李乙', encoded)
        self.assertEqual(diag.inspect_content(encoded.encode()), [])

    def test_reit_geometry_uses_only_observed_two_pages_and_identity_columns(self):
        pages = reit_pages()
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), \
                patch.object(diag, '_read_reit_manager_grids', return_value=manager_grids()) as read:
            result, _, windows = diag.reit_field_observation(pages, list(range(1, 70)), b'one-existing-document')
        read.assert_called_once_with(b'one-existing-document')
        geometry = result['geometry']
        self.assertEqual(geometry['status'], 'observed')
        self.assertEqual(geometry['pagesRead'], [20, 21])
        self.assertEqual((geometry['physicalRows'], geometry['identityRows'], geometry['unresolvedRows']), (2, 2, 0))
        self.assertEqual([row['roleAllowedByCurrentParser'] for row in geometry['rowShapes']], [False, True])
        self.assertEqual(set(geometry['columns']), {'name', 'role', 'start', 'end', 'experience', 'description'})
        encoded = json.dumps([result, windows], ensure_ascii=False)
        for unwanted in ('毕业于', '曾任', '资格证', '联系电话', '12345678901'):
            self.assertNotIn(unwanted, encoded)
        self.assertLessEqual(result['excerptChars'], 1800)

    def test_reit_geometry_failure_and_wrong_boundaries_stay_explicitly_unavailable(self):
        pages = reit_pages()
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), \
                patch.object(diag, '_read_reit_manager_grids', side_effect=RuntimeError('private-debug')) as read:
            result, _, _ = diag.reit_field_observation(pages, list(range(1, 70)), b'not-parsed')
        self.assertTrue(result['geometry']['attempted'])
        self.assertEqual(result['geometry']['status'], 'unavailable')
        self.assertEqual(result['geometry']['reasonCode'], 'GEOMETRY_UNAVAILABLE')
        self.assertNotIn('private-debug', json.dumps(result))
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), patch.object(diag, '_read_reit_manager_grids') as read:
            wrong = pages.copy(); wrong[20] = wrong[20].replace('7.1.3 ', '7.1.4 ')
            result, _, _ = diag.reit_field_observation(wrong, list(range(1, 70)), b'not-parsed')
        read.assert_not_called()
        self.assertFalse(result['manager']['sectionBounded'])
        self.assertEqual(result['geometry']['reasonCode'], 'OBSERVED_BOUNDARY_NOT_FOUND')

    def test_reit_geometry_unknown_experience_label_keeps_safe_header_for_repair(self):
        from foreign_holdings import Cell
        grids = manager_grids()
        old = grids[0].cells[4]
        grids[0].cells[4] = Cell(old.x0, old.x1, old.y0, old.y1, '不动产项目运营或投资管理经验年限')
        pages = reit_pages()
        pages[19] = pages[19].replace('证券从业年限', '不动产项目运营或投资管理经验年限')
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), patch.object(diag, '_read_reit_manager_grids', return_value=grids):
            result, _, windows = diag.reit_field_observation(pages, list(range(1, 70)), b'not-parsed')
        self.assertEqual(result['geometry']['status'], 'unavailable')
        self.assertEqual(result['geometry']['reasonCode'], 'HEADER_COLUMNS_UNRECOGNIZED')
        self.assertTrue(any('不动产项目运营或投资管理经验年限' in row['text'] for row in windows))

    def test_reit_grid_reader_indexes_only_physical_pages_twenty_and_twenty_one(self):
        accessed = []
        class FakePages:
            def __len__(self): return 69
            def __getitem__(self, index): accessed.append(index); return f'synthetic-page-{index + 1}'
        fake = types.SimpleNamespace(PdfReader=lambda _: types.SimpleNamespace(pages=FakePages()))
        with patch.dict('sys.modules', {'pypdf': fake}), patch('foreign_holdings.extract_grid_page', side_effect=lambda page, number: (page, number)) as extract:
            result = diag._read_reit_manager_grids(b'synthetic-bytes')
        self.assertEqual(accessed, [19, 20])
        self.assertEqual(result, [('synthetic-page-20', 20), ('synthetic-page-21', 21)])
        self.assertEqual(extract.call_count, 2)

    def test_reit_combined_budget_and_complete_private_census_are_independent_of_early_volume(self):
        pages = reit_pages()
        pages[5] = '\n'.join(f'4.{i} 合成项目运营情况\n' + '合成运营观察内容。' * 200 for i in range(1, 95))
        pages[20] = pages[20].replace('本基金根据合成运营情况维持投资安排。', '本基金根据合成运营情况维持投资安排。' * 200)
        result, headings, windows = diag.reit_field_observation(pages, list(range(1, 70)), b'not-parsed')
        self.assertGreater(result['discoveredHeadingCount'], 90)
        self.assertTrue(result['headingsTruncated'])
        self.assertTrue(result['sectionMetadataTruncated'])
        self.assertTrue(result['strategyCandidatesTruncated'])
        self.assertLessEqual(result['headingChars'], 640)
        self.assertLessEqual(len(result['sections']), 32)
        self.assertLessEqual(sum(len(row['text']) for row in windows), 1800)
        self.assertTrue(any(row['section'] == '7.4' and row['truncated'] for row in windows))
        self.assertTrue(any('7.1.2' in row['text'] for row in headings))

    def test_reit_probe_replaces_raw_nav_windows_without_changing_structured_nav(self):
        pages = reit_pages()
        pages[30] = ('10.1 资产负债表\n单位：人民币元\n本期末 2026年6月30日 上年度末 2025年12月31日\n'
                     '净资产合计 2,388,000,000.00 2,400,000,000.00\n10.2 利润表')
        class Provider:
            def __init__(self): self.calls = []
            def get(self, url, **kwargs): self.calls.append(url); return b'%PDF-synthetic-never-parsed'
        provider = Provider()
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}), patch('report_extract._pypdf_pages', return_value=pages), \
                patch.object(diag, '_read_reit_manager_grids') as read:
            result = diag.diagnose_interim(provider, anchors()['508007'], NOW, diag.extract_quarter)
        read.assert_not_called()
        self.assertEqual(provider.calls, [diag.SAMPLES[1]['interimUrl']])
        self.assertEqual(result['fieldCompleteness']['nav']['valueYuan'], 2388000000.0)
        self.assertTrue(result['fieldCompleteness']['nav']['acceptedInSnapshot'])
        self.assertEqual(result['reitFieldObservation']['additionalHttpRequests'], 0)
        self.assertTrue(result['reitFieldObservation']['manager']['headingFound'])
        self.assertFalse(any('净资产合计' in row['text'] for row in result['windows']))
        self.assertTrue(all(row['selection'].startswith(('manager-', 'strategy-')) for row in result['windows']))
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), diag.MAX_OUTPUT)

    def test_completeness_summary_does_not_accept_unbound_flags_or_portfolio_subtotals(self):
        sample = diag.interim_sample(anchors()['508007'], NOW)
        raw = details(sample, False)
        raw.update(navYuan=float('nan'), portfolioSubtotalYuan=39582826.55,
            strategySectionCoverage={'complete': True}, managerTableCoverage={'complete': True})
        result = diag.field_summary(raw, diag.document(sample))
        self.assertIsNone(result['nav']['valueYuan'])
        self.assertFalse(result['nav']['portfolioSubtotalUsed'])
        self.assertFalse(result['strategy']['complete'])
        self.assertFalse(result['managers']['complete'])

    def test_strict_table_recognizer_counts_use_actual_shared_parser(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/nonstock-assets.json').read_text())
        counts = diag.table_recognition(fixture['pages'], fixture['pageNumbers'])
        self.assertEqual(counts['assetHeadingCount'], 1)
        self.assertEqual(counts['otherAssetsHeadingCount'], 1)
        self.assertEqual(counts['recognizedClosedAssetTables'], 1)
        self.assertEqual(counts['recognizedClosedOtherAssetsTables'], 1)

    def test_reit_structure_exposes_actual_heading_rejection_without_proving_no_stocks(self):
        sample = diag.SAMPLES[1]
        pages = ['合成高速公路封闭式基础设施证券投资基金\n2026年第2季度报告',
                 '2 基金产品概况\n基金代码\n508007\n基金名称 合成基金\n'
                 '3 主要财务指标\n3.1 基础设施项目情况\n4 基金管理情况']
        result = diag.document_structure(pages, [1, 2], diag.document(sample), pdf_sha256='a' * 64, now=NOW)
        self.assertEqual(result['identityRecognizer']['branch'], 'IDENTITY_SECTION_START_COUNT')
        self.assertFalse(result['identityRecognizer']['requestedCodeMatched'])
        self.assertEqual(result['titleWindow']['text'], pages[0])
        self.assertEqual(result['periodTitles'][0]['periodEnd'], '2026-06-30')
        self.assertTrue(result['periodTitles'][0]['matchesExpected'])
        self.assertEqual(result['headings'][0]['text'], '2 基金产品概况')
        self.assertEqual(result['codeRows'][0]['codes'], ['508007'])
        self.assertTrue(result['codeRows'][0]['requestedCodeMentioned'])
        self.assertFalse(result['codeRows'][0]['provesIdentity'])
        self.assertGreater(result['formatSignals']['infrastructureFundMentions'], 0)
        self.assertGreater(result['formatSignals']['infrastructureProjectHeadings'], 0)
        self.assertTrue(result['diagnosticOnly'])
        self.assertNotIn('noEquitiesConfirmed', result)

    def test_structure_distinguishes_wrong_document_code_label_and_period(self):
        sample = diag.SAMPLES[1]
        pages = ['合成基金2026年第2季度报告', '§2 基金产品概况\n基金主代码 000001',
                 '§3 主要财务指标和基金净值表现']
        def inspect(values):
            return diag.document_structure(values, [1, 2, 3], diag.document(sample), pdf_sha256='a' * 64, now=NOW)
        result = inspect(pages)
        self.assertEqual(result['identityRecognizer']['branch'], 'VERIFIED')
        self.assertEqual(result['identityRecognizer']['parsedPortfolioCode'], '000001')
        self.assertFalse(result['identityRecognizer']['requestedCodeMatched'])
        label = inspect([pages[0], pages[1].replace('基金主代码', '基金代码'), pages[2]])
        self.assertEqual(label['identityRecognizer']['branch'], 'MAIN_CODE_LABEL_NOT_FOUND')
        self.assertEqual(label['codeRows'][0]['text'], '基金代码 000001')
        period = inspect([pages[0].replace('第2', '第3'), *pages[1:]])
        self.assertEqual(period['identityRecognizer']['branch'], 'PERIOD_TITLE_MISMATCH')
        self.assertFalse(period['periodTitles'][0]['matchesExpected'])
        self.assertEqual(period['periodTitles'][0]['periodEnd'], '2026-09-30')

    def test_observed_reit_structure_reports_verified_identity_and_specific_code_conflict(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/portfolio-identity-reit.json').read_text())
        result = diag.document_structure(fixture['pages'], fixture['pageNumbers'], fixture['report'],
            pdf_sha256=fixture['proof']['pdfSha256'], now=NOW)
        self.assertEqual(result['identityRecognizer']['branch'], 'VERIFIED')
        self.assertTrue(result['identityRecognizer']['requestedCodeMatched'])
        pages = [p.replace('交易代码  000008', '交易代码 000009') for p in fixture['pages']]
        result = diag.document_structure(pages, fixture['pageNumbers'], fixture['report'],
            pdf_sha256=fixture['proof']['pdfSha256'], now=NOW)
        self.assertEqual(result['identityRecognizer']['branch'], 'REIT_CODE_MISMATCH')
        self.assertFalse(result['identityRecognizer']['requestedCodeMatched'])

    def test_reit_financial_and_other_security_windows_keep_original_short_quote_budget(self):
        pages = ['§3 主要财务指标和不动产基金运作情况\n3.1 主要财务指标\n'
                 '项目 本期金额\n期末基金资产净值 100.00\n3.2 其他财务指标\n'
                 '7.1 其他证券投资情况\n本期证券投资表\n7.2 其他说明',
                 '8.1 投资组合报告\n' + '证券金额 100\n' * 200 + '\n8.2 基金份额'] * 5
        windows = diag.bounded_table_windows(pages, list(range(1, len(pages) + 1)))
        self.assertTrue(any('期末基金资产净值' in row['text'] for row in windows))
        self.assertTrue(any('其他证券投资情况' in row['text'] for row in windows))
        self.assertLessEqual(sum(len(row['text']) for row in windows), 1800)
        self.assertLessEqual(len(windows), 6)
        self.assertTrue(all(len(row['text']) <= 500 for row in windows))

    def test_observed_reit_asset_title_and_other_financial_metrics_are_captured(self):
        pages = ['3.1 主要财务指标\n本期收入 10.00\n3.2 其他财务指标\n'
                 '本期末指标 未披露净资产\n3.3 不动产基金收益分配情况',
                 '§ 5 除不动产资产支持证券之外的投资组合报告\n'
                 '5.1 报告期末 不动产 基金资产组合情况\n序号 项目 金额 比例\n'
                 '1 银行存款 100.00 100.00\n2 合计 100.00 100.00\n'
                 '5.2 其他证券投资情况\n本节为合成诊断内容\n5.3 其他说明']
        windows = diag.bounded_table_windows(pages, [4, 11])
        asset = next(row for row in windows if row['text'].startswith('5.1'))
        self.assertEqual(asset['page'], 11)
        self.assertIn('1 银行存款', asset['text'])
        self.assertEqual(asset['followingHeading'], '5.2 其他证券投资情况')
        self.assertTrue(asset['boundaryOnSamePage'])
        self.assertTrue(any(row['text'].startswith('3.2') for row in windows))
        self.assertLessEqual(sum(len(row['text']) for row in windows), 1800)
        self.assertNotIn('noEquitiesConfirmed', asset)
        self.assertNotIn('navYuan', asset)

    def test_late_manager_and_investment_headings_replace_early_outline_without_body_leaks(self):
        pages = ['合成基金2026年第2季度报告\n' + '\n'.join(
            f'4.{index} 不动产项目运营情况说明' for index in range(1, 26)),
            '6.1 基金经理简介\n姓名 张某\n任职日期 2020年1月1日\n'
            '6.2 基金经理联系方式\n不要输出\n'
            '7.1 投资策略和运作分析\n这里的全文不得输出\n'
            '7.2 股票投资明细\n股票正文不得输出\n'
            '1.其他可能的调整项，如不动产基金发行份额募集的']
        result = diag.document_structure(pages, [1, 12], diag.document(diag.SAMPLES[1]),
            pdf_sha256='a' * 64, now=NOW)
        headings = [row['text'] for row in result['headings']]
        for required in ('6.1 基金经理简介', '7.1 投资策略和运作分析', '7.2 股票投资明细'):
            self.assertIn(required, headings)
        self.assertEqual(headings[:4], [f'4.{index} 不动产项目运营情况说明' for index in range(1, 5)])
        self.assertTrue(result['headingsTruncated'])
        self.assertLessEqual(len(headings), 16)
        self.assertLessEqual(sum(map(len, headings)), 640)
        encoded = json.dumps(result, ensure_ascii=False)
        for excluded in ('张某', '不要输出', '这里的全文不得输出', '股票正文不得输出', '联系方式', '1.其他可能'):
            self.assertNotIn(excluded, encoded)

    def test_structure_excerpts_are_bounded_and_omit_contact_or_free_prose(self):
        email = 'private' + '@' + 'example.org'
        text = ('联系人 不输出\n' + email + '\n2026年第2季度报告\n'
                '2 基金产品概况\n基金代码 508007 私人备注\n'
                '基金主代码 508007\n联系方式 不输出\n'
                '3.1 基础设施项目管理情况\n' + '项目正文不得输出\n' * 80)
        pages = [text] * 40
        result = diag.document_structure(pages, list(range(1, 41)), diag.document(diag.SAMPLES[1]),
            pdf_sha256='a' * 64, now=NOW)
        encoded = json.dumps(result, ensure_ascii=False)
        for private in ('联系人', email, '私人备注', '联系方式', '项目正文不得输出'):
            self.assertNotIn(private, encoded)
        self.assertIsNone(result['titleWindow'])
        self.assertLessEqual(len(result['headings']), 16)
        self.assertLessEqual(sum(len(row['text']) for row in result['headings']), 640)
        self.assertLessEqual(len(result['codeRows']), 4)
        self.assertTrue(all(len(row['text']) <= 100 for row in result['codeRows']))
        self.assertEqual(diag.inspect_content(encoded.encode()), [])

    def test_one_source_failure_keeps_other_diagnosis_without_raw_error(self):
        def extract(_provider, sample, _now):
            if sample['code'] == '003589': raise RuntimeError('private-debug-not-for-output')
            return details(sample), {'windows': []}
        result = diag.run_diagnosis(object(), anchors(), now=NOW, extract=extract)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['results'][0]['errorCode'], 'DIAGNOSTIC_FAILED')
        self.assertEqual(result['results'][1]['status'], 'diagnosed')
        self.assertNotIn('private-debug', json.dumps(result))

    def test_local_main_refuses_before_provider_or_artifact_access(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(diag, 'load_anchors') as loader, \
                patch.object(diag, 'PublicProvider') as provider, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(diag.main(), 2)
        provider.assert_not_called()
        loader.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())['errorCode'], 'ACTIONS_REQUIRED')

    def test_workflow_is_read_only_and_uploads_only_bounded_result(self):
        text = (diag.ROOT / '.github/workflows/diagnose-nonstock-retention.yml').read_text()
        self.assertIn('actions: read', text)
        self.assertIn('contents: read', text)
        self.assertIn('name: part-1', text)
        self.assertIn('run-id: 40790220286', text)
        self.assertIn('path: .cache/nonstock-retention/result.json', text)
        for forbidden in ('contents: write', 'secrets.', 'published-records', 'persist-observations', 'git push'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()
