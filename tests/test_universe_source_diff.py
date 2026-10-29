"""Synthetic workbook code-set comparison; no official downloads locally."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_historical_universe import workbook, files, product, synthetic_pin, NOW
import universe_source_diff as diff
import verify_universe_sources as diagnostic


def serial(value):
    return str((dt.date.fromisoformat(value) - dt.date(1899, 12, 30)).days)


def landing(date='20260831'):
    return (f'<html><h1>公募基金产品索引（截至{date}）</h1><a href="{diff.AUGUST_URL}">下载</a>'
            '<script>const irrelevant="截至20260630";</script></html>').encode()


def comparison(june, august, root, page=None):
    fetched = []
    def fetch(url):
        fetched.append(url)
        if url == diff.INDEX_URL: return landing() if page is None else page
        if url == diff.AUGUST_URL: return august
        raise AssertionError('No other source request is permitted')
    with synthetic_pin(june):
        result = diff.compare_sources(june, dates=diagnostic.explicit_period_dates,
            core_properties=diagnostic.core_properties, fetch=fetch, catalog_root=root)
    return result, fetched


class UniverseSourceDiffTests(unittest.TestCase):
    def test_unrecognized_table_preserves_bounded_header_shape_without_inferred_mapping(self):
        data_rows = [product(3, '508001', '公开样本基金甲'), product(4, '890017', '公开样本基金乙')]
        entries = files(data_rows)
        entries['xl/worksheets/sheet1.xml'] = entries['xl/worksheets/sheet1.xml'].replace('设立日期', '终止日期')
        with tempfile.TemporaryDirectory() as directory:
            result, _ = comparison(workbook(), workbook(entries), directory)
        august = result['sources'][1]
        self.assertEqual(august['parseStatus'], 'failed')
        self.assertEqual(august['errorCode'], 'UNSUPPORTED_SCHEMA')
        shape = august['schemaDiagnostic']
        self.assertEqual(shape['status'], 'observed'); self.assertEqual(shape['schemaMapping'], 'not-inferred')
        self.assertEqual(shape['sheetCount'], 1); self.assertEqual(shape['date1904Values'], ['0'])
        sheet = shape['sheets'][0]
        self.assertEqual(sheet['name'], 'Sheet1'); self.assertEqual(sheet['rowCount'], 4)
        self.assertEqual([c['headerCandidateValue'] for c in sheet['firstRows'][1]['cells']],
                         ['序号', '基金代码', '基金简称', '终止日期'])
        self.assertEqual(sheet['fixedSampleMatches']['508001']['cells'][0]['reference'], 'B3')
        self.assertEqual(sheet['fixedSampleMatches']['890017']['cells'][0]['reference'], 'B4')
        text = json.dumps(shape)
        self.assertNotIn('CONCATENATE', text)
        # Public product names within this small preview are useful schema
        # evidence; they are not private local user data.
        self.assertEqual(sheet['firstRows'][2]['cells'][2]['headerCandidateValue'], '公开样本基金甲')
        self.assertFalse(shape['fixedSamplesProveProductMembership'])
        self.assertFalse(result['codeSetComparisonReady'])

    def test_schema_diagnostic_bounds_rows_cells_and_sample_matches_and_reports_date_system(self):
        entries = files([product(row, '508001') for row in range(3, 14)])
        entries['xl/workbook.xml'] = entries['xl/workbook.xml'].replace('date1904="0"', 'date1904="1"')
        shape = diff.schema_diagnostic(workbook(entries))
        self.assertEqual(shape['date1904Values'], ['1'])
        self.assertEqual(len(shape['sheets'][0]['firstRows']), 12)
        self.assertEqual(shape['sheets'][0]['fixedSampleMatches']['508001']['cellCount'], 11)
        self.assertEqual(len(shape['sheets'][0]['fixedSampleMatches']['508001']['cells']), 8)
        self.assertFalse(shape['historicalUniverseVerified'])
        self.assertFalse(shape['catalogCompletenessVerified'])
        with self.assertRaises(diff.verify.VerificationError):
            diff.parse_product_table(workbook(entries), diff.SOURCES[1], diagnostic.explicit_period_dates)

    def test_later_header_text_is_visible_and_public_preview_is_truncated(self):
        entries = files([product(row, '508001', '甲' * 100 if row == 6 else '第十三行不可导出' if row == 13 else '公开名称')
                         for row in range(3, 14)])
        entries['xl/worksheets/sheet1.xml'] = entries['xl/worksheets/sheet1.xml'].replace(
            '<v>公开名称</v></c><c r="D4"', '<v>基金管理人</v></c><c r="D4"')
        shape = diff.schema_diagnostic(workbook(entries)); rows = shape['sheets'][0]['firstRows']
        self.assertEqual(rows[3]['cells'][2]['headerCandidateValue'], '基金管理人')
        self.assertEqual(len(rows[5]['cells'][2]['headerCandidateValue']), 80)
        self.assertTrue(rows[5]['cells'][2]['headerCandidateTruncated'])
        self.assertNotIn('第十三行不可导出', json.dumps(shape, ensure_ascii=False))

    def test_code_sets_metadata_counts_dates_and_sample_membership_are_separate(self):
        june = workbook(files([product(3, '508001', '合成旧名'), product(4, '890017')]))
        august = workbook(files([product(3, '508001', '合成新名', date=serial('2024-06-30')),
                                 product(4, '508603', date=serial('2026-07-01'))]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = json.dumps({'asOf': NOW, 'funds': [{'code': '508001'}, {'code': '890017'}, {'code': '890018'}]}).encode()
            (root / 'catalog.json').write_bytes(raw)
            result, fetched = comparison(june, august, root)
            self.assertEqual((root / 'catalog.json').read_bytes(), raw)
            self.assertEqual([p.name for p in root.iterdir()], ['catalog.json'])
        self.assertEqual(fetched, [diff.INDEX_URL, diff.AUGUST_URL])
        self.assertTrue(result['codeSetComparisonReady'])
        delta = result['comparison']
        self.assertEqual(delta['addedCodes'], ['508603'])
        self.assertEqual(delta['removedCodes'], ['890017'])
        self.assertEqual(delta['nameChangedCodes'], ['508001'])
        self.assertEqual(delta['inceptionChangedCodes'], ['508001'])
        self.assertEqual(delta['addedCodesDatedAfterJune'], ['508603'])
        self.assertEqual(delta['addedCodesDatedOnOrBeforeJune'], [])
        self.assertFalse(delta['removedCodesProveLiquidation'])
        self.assertFalse(delta['addedCodesProveNewInception'])
        self.assertEqual(result['sources'][0]['aggregateMinusParsedCodes'], 14351)
        self.assertEqual(result['sources'][1]['aggregateMinusParsedCodes'], 14583)
        self.assertFalse(delta['aggregateGapStable'])
        self.assertFalse(result['sources'][0]['dateEvidence']['landingBodyAndLinkCorroborates'])
        self.assertTrue(result['sources'][1]['dateEvidence']['landingBodyAndLinkCorroborates'])
        sample = next(s for s in result['samples'] if s['code'] == '890017')
        self.assertTrue(sample['directoryMembership']['2026-06-30']['found'])
        self.assertFalse(sample['directoryMembership']['2026-08-31']['found'])
        self.assertTrue(result['catalogs']['catalog.json']['sampleMembership']['890017'])
        self.assertFalse(result['catalogs']['periods/2026Q3/catalog.json']['membershipKnown'])
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('合成旧名', text); self.assertNotIn('合成新名', text); self.assertNotIn('CONCATENATE', text)
        self.assertFalse(result['productionCatalogModified']); self.assertFalse(result['historicalUniverseVerified'])

    def test_august_dates_are_not_rejected_by_june_adapter_date_cutoff(self):
        data = workbook(files([product(3, '508603', date=serial('2026-08-31'))]))
        summary, rows = diff.parse_product_table(data, diff.SOURCES[1], diagnostic.explicit_period_dates)
        self.assertTrue(summary['codeRowsComplete']); self.assertEqual(rows['508603']['inceptionDate'], '2026-08-31')
        june, _ = diff.parse_product_table(data, diff.SOURCES[0], diagnostic.explicit_period_dates)
        self.assertFalse(june['codeRowsComplete'])
        self.assertEqual(june['rowRejections'], {'FOUNDATION_AFTER_INDEX_DATE': 1})
        self.assertEqual(diff.history.SOURCE_AS_OF, '2026-06-30')

    def test_unknown_inception_and_duplicate_or_missing_codes_never_look_complete(self):
        data = workbook(files([product(3, '508001', date='60')]))
        summary, rows = diff.parse_product_table(data, diff.SOURCES[1], diagnostic.explicit_period_dates)
        self.assertTrue(summary['codeRowsComplete']); self.assertEqual(summary['inceptionDatesUnresolved'], 1)
        self.assertIsNone(rows['508001']['inceptionDate'])
        for bad in [workbook(files([product(3, '508001'), product(4, '508001')])),
                    workbook(files([product(3, '508001'), product(5, '890017')])),
                    workbook(files([product(3, '508001'), product(4, '890017', cache=False)]))]:
            with tempfile.TemporaryDirectory() as directory:
                result, _ = comparison(workbook(), bad, directory)
            self.assertFalse(result['codeSetComparisonReady']); self.assertNotIn('comparison', result)
            sample = next(s for s in result['samples'] if s['code'] == '508603')
            self.assertIsNone(sample['directoryMembership']['2026-08-31']['found'])

    def test_landing_source_and_filename_are_distinct_from_workbook_period(self):
        evidence = diff.landing_evidence(landing(), diagnostic.explicit_period_dates)
        self.assertEqual(evidence['bodyExplicitDates'], ['2026-08-31'])
        self.assertEqual(evidence['linkedProductPeriods'], ['2026-08-31'])
        entries = files(); entries['xl/worksheets/sheet1.xml'] = entries['xl/worksheets/sheet1.xml'].replace(
            '基金产品索引</t>', '基金产品索引（截至20260930）</t>')
        with tempfile.TemporaryDirectory() as directory:
            result, _ = comparison(workbook(), workbook(entries), directory)
        self.assertTrue(result['sources'][1]['dateEvidence']['workbookDateContradiction'])
        self.assertFalse(result['codeSetComparisonReady'])
        self.assertFalse(result['catalogCompletenessVerified'])

    def test_equal_aggregate_gaps_and_identical_code_sets_do_not_certify_scope(self):
        sources = copy.deepcopy(diff.SOURCES); sources[0]['aggregateProducts'] = 185; sources[1]['aggregateProducts'] = 185
        with patch.object(diff, 'SOURCES', sources), tempfile.TemporaryDirectory() as directory:
            result, _ = comparison(workbook(), workbook(), directory)
        self.assertEqual(result['comparison']['juneAggregateGap'], 183)
        self.assertTrue(result['comparison']['aggregateGapStable'])
        self.assertFalse(result['comparison']['equalGapsExplainCause'])
        self.assertFalse(result['historicalUniverseVerified']); self.assertEqual(result['status'], 'partial')

    def test_failed_source_retains_observed_other_period_without_invented_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = comparison(workbook(), b'not a workbook', directory, page=b'<html>unavailable</html>')
        self.assertFalse(result['codeSetComparisonReady']); self.assertNotIn('comparison', result)
        self.assertEqual(result['sources'][0]['parseStatus'], 'complete')
        self.assertEqual(result['sources'][1]['parseStatus'], 'failed')
        self.assertFalse(result['sources'][0]['dateEvidence']['landingBodyAndLinkCorroborates'])
        self.assertNotIn('2026-08-31', result['samples'][0]['directoryMembership'])

    def test_unpinned_june_and_changed_headers_do_not_become_accepted_differences(self):
        changed = files(); changed['xl/worksheets/sheet1.xml'] = changed['xl/worksheets/sheet1.xml'].replace('设立日期', '终止日期')
        with tempfile.TemporaryDirectory() as directory:
            result, _ = comparison(workbook(), workbook(changed), directory)
            self.assertFalse(result['codeSetComparisonReady'])
            with patch.object(diff.history, 'SOURCE_HASH', '0' * 64):
                result = diff.compare_sources(workbook(), dates=diagnostic.explicit_period_dates,
                    core_properties=diagnostic.core_properties,
                    fetch=lambda url: landing() if url == diff.INDEX_URL else workbook(), catalog_root=directory)
        self.assertEqual(result['sources'][0]['errorCode'], 'SOURCE_HASH_MISMATCH')
        self.assertFalse(result['codeSetComparisonReady'])

    def test_catalog_duplicates_share_codes_and_invalid_input_are_not_portfolio_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'catalog.json').write_text(json.dumps({'funds': [{'code': '508001'}, {'code': '508001'}]}))
            summary = diff.read_catalogs(root)['catalog.json']
            self.assertEqual(summary['duplicateCodeRows'], 1)
            self.assertEqual(summary['recordUnit'], 'catalog-codes-not-independent-portfolios')
            (root / 'catalog.json').write_text(json.dumps({'funds': [{'code': '../bad'}]}))
            self.assertFalse(diff.read_catalogs(root)['catalog.json']['membershipKnown'])

    def test_local_real_download_gate_precedes_all_network_requests(self):
        with patch.dict(diff.os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(diff.verify, '_download_attempt') as request:
            with self.assertRaises(diff.verify.VerificationError): diff.download_source(diff.AUGUST_URL)
            request.assert_not_called()
        with patch.dict(diff.os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(diff.verify, '_download_attempt') as request:
            with self.assertRaises(diff.verify.VerificationError): diff.download_source('https://example.test/not-allowed')
            request.assert_not_called()

    def test_parent_diagnostic_includes_comparison_without_second_june_download(self):
        data = workbook(); requested = []
        def fetch(url):
            requested.append(url)
            return landing() if url == diff.INDEX_URL else data
        with synthetic_pin(data), tempfile.TemporaryDirectory() as directory:
            result = diagnostic.run_diagnostic(fetch=lambda: data, now=NOW, comparison_fetch=fetch, catalog_root=directory)
        self.assertTrue(result['twoPeriodComparison']['codeSetComparisonReady'])
        self.assertEqual(requested, [diff.INDEX_URL, diff.AUGUST_URL])
        self.assertEqual(result['status'], 'partial')
        guarded, raw = diagnostic.encode_result(result)
        self.assertTrue(guarded['diagnosticComplete']); self.assertLess(len(raw), diagnostic.MAX_OUTPUT_BYTES)


if __name__ == '__main__': unittest.main()
