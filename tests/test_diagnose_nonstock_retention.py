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

    def test_fetch_adapter_requests_only_each_fixed_pdf_once_no_mineru(self):
        class Provider:
            def __init__(self): self.calls = []
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return b'%PDF-synthetic-not-parsed'
        provider = Provider()
        def synthetic_extract(_bytes, url, period, kind, **kwargs):
            self.assertEqual(kwargs['env'], {})
            self.assertEqual(kwargs['max_pages'], 300)
            kwargs['on_private_sections'](pages=['5.1 报告期末基金资产组合情况\n1 债券 100\n5.2 后续情况'], page_numbers=[2])
            return details(next(s for s in diag.SAMPLES if s['sourceUrl'] == url))
        with patch.object(diag, 'extract_report', side_effect=synthetic_extract):
            for sample in diag.SAMPLES:
                current, meta = diag.extract_quarter(provider, sample, NOW)
                self.assertEqual(current['reportPdfSourceUrl'], sample['sourceUrl'])
                self.assertEqual(len(meta['windows']), 1)
                self.assertEqual(meta['documentStructure']['identityRecognizer']['branch'], 'PERIOD_TITLE_NOT_FOUND')
                self.assertTrue(meta['documentStructure']['diagnosticOnly'])
        self.assertEqual([url for url, _ in provider.calls], [s['sourceUrl'] for s in diag.SAMPLES])
        self.assertTrue(all(kw == {'binary': True, 'attempts_override': 1} for _, kw in provider.calls))

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
