"""Synthetic page tests; fixed real PDFs are requested only by Actions."""
import importlib.util
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('verify_managers', ROOT / 'scripts' / 'verify-managers.py')
verify = importlib.util.module_from_spec(spec); spec.loader.exec_module(verify)
import report_managers

HEADER = '4.1 基金经理（或基金经理小组）简介\n姓名 职务 任本基金的基金经理期限 证券从业年限\n任职日期 离任日期 说明\n'
ROW = '张明 本基金基金经理 2020年1月2日 - 8年 曾任投资研究员。\n'
END = '4.2 管理人对报告期内本基金运作遵规守信情况的说明\n'
SAMPLE = verify.SAMPLES[-1]


def diagnose(text):
    return verify.diagnose_pages([text], SAMPLE, pdf_sha256='a' * 64)


class VerifyManagersTests(unittest.TestCase):
    def test_actual_report_only_normalizer_requires_and_receives_pdf_extraction_facts(self):
        from report_extract import PDFPages
        from verify_repairs import VerificationError
        pages = PDFPages([HEADER + ROW + END], 1)
        details = verify.manager_details_from_pages(pages, SAMPLE)
        self.assertEqual(details['parseMethod'], 'pypdf')
        self.assertEqual(details['pageStats']['totalPages'], 1)
        self.assertGreater(details['pageStats']['charCount'], 100)
        now = dt.datetime(2026, 10, 26, 15, 25, tzinfo=dt.timezone.utc)
        with self.assertRaises(VerificationError):
            verify.normalized_manager_check(SAMPLE, {key: details[key] for key in
                ('reportManagers', 'managerTableCoverage')}, now)
        result = verify.normalized_manager_check(SAMPLE, details, now)
        self.assertTrue(result['complete'])
        self.assertEqual(result['managerCount'], 1)
        self.assertTrue(result['sourcePreserved'])

    def test_sample_uses_actual_normalization_and_fixed_failure_categories(self):
        from report_extract import PDFPages
        from providers import PublicProvider
        from verify_repairs import VerificationError
        pages = PDFPages([HEADER + ROW + END], 1)
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), \
                patch.object(PublicProvider, 'get', return_value=b'%PDF-synthetic'), \
                patch('report_extract._pypdf_pages', return_value=pages), \
                patch('manager_cells.enrich_manager_details'), patch.object(verify, 'add_layout_diagnostics'):
            result = verify.run_sample(SAMPLE)
        self.assertTrue(result['normalizedManagerCheck']['complete'])
        self.assertTrue(result['tableCoverageComplete'])
        self.assertLessEqual(result['sourceContextCharacters'], verify.MAX_CONTEXT_CHARACTERS)
        self.assertEqual(verify.normalization_error(VerificationError('SNAPSHOT_REJECTED')), 'SNAPSHOT_REJECTED')
        self.assertEqual(verify.normalization_error(ImportError('private local path')), 'NORMALIZATION_DEPENDENCY_MISSING')
        self.assertEqual(verify.normalization_error(RuntimeError('private source text')), 'NORMALIZATION_CHECK_FAILED')

    def test_unchanged_parser_control_and_ast_counter_alignment(self):
        text = HEADER + ROW + END
        result = diagnose(text)
        original = report_managers.parse_report_managers([text], dict(SAMPLE, kind='quarterly', periodEnd='2026-06-30'))
        self.assertTrue(result['counterAlignmentVerified'])
        self.assertEqual(result['tableCoverageComplete'], original.coverage['complete'])
        self.assertTrue(result['tableCoverageComplete'])
        self.assertEqual(result['branchCounts']['PARSED_ROW'], 1)
        self.assertEqual(result['parsedManagers'][0]['name'], '张明')
        self.assertEqual(result['parsedManagers'][0]['pages'], [1])
        self.assertEqual(result['sourceTextHash'], original.coverage['sourceTextHash'])

    def test_invalid_date_is_classified_at_actual_exclusion_branch(self):
        result = diagnose(HEADER + ROW.replace('1月2日', '2月31日') + END)
        self.assertFalse(result['tableCoverageComplete'])
        self.assertEqual(result['branchCounts']['INVALID_APPOINTMENT_DATE'], 1)
        self.assertEqual(result['counts']['excludedRows'], 1)
        self.assertEqual(result['branchExamples'][0]['page'], 1)

    def test_no_strict_row_match_and_residual_mention_counts_are_not_reclassified_as_valid(self):
        text = HEADER + ROW + '李伟 本基金基金经理 日期未知 -\n另一段 基金经理相关的历史履历。\n' + END
        result = diagnose(text)
        self.assertFalse(result['tableCoverageComplete'])
        self.assertGreater(result['counts']['excludedRows'], 0)
        self.assertEqual(sum(value for key, value in result['branchCounts'].items()
                             if key not in ('PARSED_ROW', 'KNOWN_ROLE_IGNORED', 'TABLE_HEADERS_NOT_MATCHED')),
                         result['counts']['excludedRows'])
        self.assertEqual(len(result['parsedManagers']), 1)

    def test_missing_headers_are_reported_without_fabricated_rows(self):
        result = diagnose('4.1 基金经理（或基金经理小组）简介\n姓名 职务\n' + ROW + END)
        self.assertEqual(result['managerHeadingCount'], 1)
        self.assertEqual(result['branchCounts']['TABLE_HEADERS_NOT_MATCHED'], 1)
        self.assertEqual(result['parsedManagers'], [])
        self.assertFalse(result['tableCoverageComplete'])

    def test_known_ignored_role_is_observed_not_assumed_complete(self):
        text = HEADER + ROW + '李伟 本基金基金经理助理 2020年1月2日 - 8年\n' + END
        result = diagnose(text)
        self.assertEqual(result['branchCounts']['KNOWN_ROLE_IGNORED'], 1)
        self.assertEqual(result['counts']['ignoredKnownRoleRows'], 1)

    def test_trace_is_always_restored(self):
        previous = sys.gettrace()
        diagnose(HEADER + ROW + END)
        self.assertIs(sys.gettrace(), previous)
        with patch.object(report_managers, 'parse_report_managers', side_effect=RuntimeError('stop')):
            with self.assertRaises(Exception): verify.trace_parser([HEADER], SAMPLE)
        self.assertIs(sys.gettrace(), previous)

    def test_original_context_budget_is_strict_and_no_full_page_is_emitted(self):
        text = HEADER + ROW + ''.join(f'候选经理 本基金基金经理 任期未知 专属尾部{i}。\n' for i in range(150)) + END
        result = diagnose(text)
        self.assertLessEqual(result['sourceContextCharacters'], 800)
        raw = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('专属尾部149', raw)
        self.assertNotIn('fullText', raw)
        self.assertEqual(result['pdfSha256'], 'a' * 64)

    def test_layout_structure_preserves_character_columns_without_cell_text(self):
        result = verify.layout_structure('4.1 基金经理（或基金经理小组）简介\n姓名    职务       任职日期       离任日期\n张明    本基金基金经理    2020年1月2日    -\n', 5)
        self.assertEqual(result['page'], 5)
        self.assertEqual(result['rows'][0]['cells'][1]['column'], 6)
        self.assertEqual(result['rows'][1]['cells'][1]['kind'], 'manager-role')
        self.assertFalse(result['validatedTableGeometry'])
        self.assertNotIn('张明', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('2020年1月2日', json.dumps(result, ensure_ascii=False))

    def test_actions_gate_fixed_five_sources_and_no_local_provider_creation(self):
        self.assertEqual([sample['code'] for sample in verify.SAMPLES], ['005892', '017052', '004845', '023937', '009817'])
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}):
            with self.assertRaises(verify.DiagnosticError) as error: verify.run_sample(SAMPLE)
            self.assertEqual(error.exception.code, 'ACTIONS_REQUIRED')
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
            with self.assertRaises(verify.DiagnosticError): verify.run_sample(SAMPLE | {'sourceUrl': 'https://example.test/other.pdf'})

    def test_main_preserves_partial_diagnostics_and_control_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            def fake(sample):
                result = diagnose(HEADER + ROW + END)
                result.update(code=sample['code'], case=sample['case'], sourceUrl=sample['sourceUrl'])
                result['normalizedManagerCheck'] = {'complete': True}
                if sample['code'] == '005892': result['tableCoverageComplete'] = False
                return result
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(verify, 'OUTPUT', path), patch.object(verify, 'run_sample', side_effect=fake):
                self.assertEqual(verify.main(), 0)
            result = json.loads(path.read_text())
            self.assertTrue(result['controlPassed'])
            self.assertFalse(result['results'][0]['tableCoverageComplete'])
            self.assertFalse(result['productionDataModified'])
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(verify, 'OUTPUT', path), patch.object(verify, 'run_sample', side_effect=RuntimeError('private source text')):
                self.assertEqual(verify.main(), 1)
            self.assertNotIn('private source', path.read_text())
            self.assertEqual(len(json.loads(path.read_text())['results']), 5)


if __name__ == '__main__':
    unittest.main()
