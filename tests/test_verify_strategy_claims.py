"""Fixed-sample verification through synthetic providers, never network PDFs."""
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import verify_strategy_claims as verify
from portfolio_identity import parse_portfolio_identity
from providers import PublicProvider

BODY = ('本季度市场震荡，消费行业估值回落。'
        '报告期内本基金增加了中国平安的配置，主要原因是估值具有吸引力。'
        '本基金降低了高估值行业的仓位。'
        '展望后市，我们将关注盈利改善的行业，警惕市场波动。')
SECURITIES = [{'stockCode': '601318', 'market': 'CN', 'stockName': '中国平安', 'currency': 'CNY'}]


class SyntheticProvider(verify.FixedPdfProvider):
    identity_change = None
    missing_end = False
    capture_count = 1

    def report_details(self, report, max_pages=80):
        import strategy_sections
        self.get(report['sourceUrl'], binary=True)
        pages = [self.sample['title'], '§2 基金产品概况\n2.1 基金基本情况\n基金主代码 ' + report['fundCode'] + '\n',
                 '§3 主要财务指标和基金净值表现\n',
                 '4.4 报告期内基金的投资策略和运作分析\n' + BODY
                 + ('' if self.missing_end else '\n4.5 报告期内基金的业绩表现\n本基金净值表现。')]
        identity = parse_portfolio_identity(pages, report, pdf_sha256=self.pdf_sha256,
            retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat())
        if not identity: raise AssertionError('Invalid synthetic identity')
        if self.identity_change:
            field, value = self.identity_change
            identity[field] = value
        for _ in range(self.capture_count):
            strategy_sections.parse_strategy_sections(pages, report, securities=SECURITIES)
        return {'portfolioIdentityEvidence': identity}


class StrategyClaimVerificationTests(unittest.TestCase):
    def setUp(self):
        for context in (patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}),
                        patch.object(verify, 'mineru_tokens_from_env', return_value=[]),
                        patch.object(PublicProvider, 'get', return_value=b'%PDF synthetic claim fixture'),
                        patch('requests.sessions.Session.request', side_effect=AssertionError('Unit tests cannot request HTTP'))):
            context.start(); self.addCleanup(context.stop)

    def run_sample(self, sample=None, provider=SyntheticProvider):
        return verify.run_sample(sample or verify.SAMPLES[0], provider_factory=provider)

    def assert_rejected(self, call):
        try: result = call()
        except ValueError: return
        self.assertFalse(result['passed'], result.get('checks'))

    def test_actual_claim_module_both_quarters_preserve_identity_hash_and_original_time(self):
        import strategy_claims
        original = strategy_claims.parse_strategy_claims
        for sample in verify.SAMPLES:
            captured = []
            def observe(pages, report, **kwargs):
                result = original(pages, report, **kwargs)
                captured.append((copy.deepcopy(report), copy.deepcopy(result['strategyClaims'])))
                return result
            with patch.object(strategy_claims, 'parse_strategy_claims', side_effect=observe):
                result = self.run_sample(sample)
            self.assertTrue(result['passed'], result.get('checks'))
            self.assertEqual(result['pdfRequestCount'], 1)
            self.assertEqual(len(captured), 1)
            self.assertEqual(result['strategyClaims'], captured[0][1])
            self.assertEqual(result['strategyClaims']['source']['retrievedAt'], captured[0][0]['retrievedAt'])
            self.assertEqual(result['strategyClaims']['source']['sourceUrl'], sample['sourceUrl'])
            self.assertEqual(result['strategyClaims']['source']['periodEnd'], '2026-06-30' if sample['targetPeriod'] == '2026Q2' else '2026-09-30')
            self.assertFalse(result['strategyClaims']['semanticAnalysisComplete'])
            self.assertTrue(result['strategyClaims']['claims'])
            self.assertNotIn(BODY, json.dumps(result, ensure_ascii=False))

    def test_local_and_nonfixed_source_or_mineru_are_rejected_before_provider_creation(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(verify, 'FixedPdfProvider') as factory, \
                patch.object(verify, 'save_result') as save, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'ACTIONS_REQUIRED'): verify.run_sample(verify.SAMPLES[0])
            self.assertEqual(verify.main(), 2)
            factory.assert_not_called(); save.assert_not_called()
        for sample, tokens in [({**verify.SAMPLES[0], 'sourceUrl': 'https://example.org/other.pdf'}, []),
                               (verify.SAMPLES[0], ['configured'])]:
            with patch.object(verify, 'mineru_tokens_from_env', return_value=tokens), patch.object(verify, 'FixedPdfProvider') as factory:
                with self.assertRaisesRegex(ValueError, 'FIXED_SAMPLE_CONFIGURATION_INVALID'): verify.run_sample(sample)
                factory.assert_not_called()

    def test_missing_or_wrong_pdf_identity_never_becomes_verified(self):
        for change in [('sourcePortfolioCode', '000001'), ('periodEnd', '2026-09-30'),
                       ('sourceUrl', 'https://example.org/other.pdf'), ('pdfSha256', '0' * 64), ('retrievedAt', None)]:
            class BadIdentity(SyntheticProvider): identity_change = change
            self.assert_rejected(lambda: self.run_sample(provider=BadIdentity))

    def test_section_must_be_captured_exactly_once_and_parser_patch_is_restored(self):
        import strategy_sections
        original = strategy_sections.parse_strategy_sections
        for count in (0, 2):
            class BadCapture(SyntheticProvider): capture_count = count
            with self.assertRaisesRegex(ValueError, 'SOURCE_IDENTITY_OR_SECTION_CAPTURE_FAILED'):
                self.run_sample(provider=BadCapture)
            self.assertIs(strategy_sections.parse_strategy_sections, original)

    def test_original_provider_enforces_single_fixed_pdf_get(self):
        class DuplicateRequest(SyntheticProvider):
            def report_details(self, report, max_pages=80):
                details = super().report_details(report, max_pages)
                self.get(report['sourceUrl'], binary=True)
                return details
        with self.assertRaises(Exception) as error: self.run_sample(provider=DuplicateRequest)
        self.assertEqual(getattr(error.exception, 'code', None), 'UNEXPECTED_REQUEST')

    def test_original_provider_and_extractor_share_one_pdf_and_one_page_extraction(self):
        from report_extract import PDFPages
        sample = verify.SAMPLES[1]
        pages = PDFPages([sample['title'], '§2 基金产品概况\n2.1 基金基本情况\n基金主代码 159366\n',
                          '§3 主要财务指标和基金净值表现\n',
                          '4.4 报告期内基金的投资策略和运作分析\n' + BODY + '\n4.5 报告期内基金的业绩表现\n'], 4)
        with patch('report_extract._pypdf_pages', return_value=pages) as extract, patch('manager_cells.enrich_manager_details'):
            result = verify.run_sample(sample)
        self.assertTrue(result['passed'], result.get('checks'))
        self.assertEqual(result['pdfRequestCount'], 1)
        self.assertEqual(result['pdfSha256'], hashlib.sha256(b'%PDF synthetic claim fixture').hexdigest())
        extract.assert_called_once()

    def test_short_evidence_quote_page_hash_and_reference_guards(self):
        import strategy_claims
        original = strategy_claims.parse_strategy_claims
        mutations = [lambda value: value['evidence'][0].update(text='长' * 161, textHash=hashlib.sha256(('长' * 161).encode()).hexdigest()),
                     lambda value: value['evidence'][0].update(textHash='0' * 64),
                     lambda value: value['evidence'][0].update(page=99, pages=[99]),
                     lambda value: value['evidence'][0].update(periodEnd='2026-09-30'),
                     lambda value: value['claims'][0].update(evidenceId='unknown'),
                     lambda value: value.update(evidence=[value['evidence'][0]] * 9)]
        for mutation in mutations:
            def altered(*args, **kwargs):
                result = copy.deepcopy(original(*args, **kwargs)); mutation(result['strategyClaims'])
                return result
            with patch.object(strategy_claims, 'parse_strategy_claims', side_effect=altered):
                self.assert_rejected(self.run_sample)

    def test_source_interval_section_and_pages_must_match_complete_section(self):
        import strategy_claims
        original = strategy_claims.parse_strategy_claims
        for field, value in [('periodStart', '2026-01-01'), ('pages', [99]), ('section', '9.9'), ('sectionContentHash', '0' * 64)]:
            def altered(*args, **kwargs):
                result = copy.deepcopy(original(*args, **kwargs)); result['strategyClaims']['source'][field] = value
                return result
            with self.subTest(field=field), patch.object(strategy_claims, 'parse_strategy_claims', side_effect=altered):
                self.assert_rejected(self.run_sample)

    def test_module_failure_status_and_broken_scan_counts_are_not_overridden_by_true_flag(self):
        import strategy_claims
        original = strategy_claims.parse_strategy_claims
        mutations = [lambda value: value.update(status='unavailable'),
                     lambda value: value.update(reasonCodes=['SECTION_POSITION_MISMATCH']),
                     lambda value: value['coverage'].update(scannedClauseCount=value['coverage']['clauseCount'] + 1),
                     lambda value: value['coverage'].update(classifiedClauseCount=value['coverage']['clauseCount'] + 1)]
        for mutation in mutations:
            def altered(*args, **kwargs):
                result = copy.deepcopy(original(*args, **kwargs)); mutation(result['strategyClaims'])
                return result
            with patch.object(strategy_claims, 'parse_strategy_claims', side_effect=altered):
                self.assert_rejected(self.run_sample)

    def test_incomplete_section_and_module_exception_are_not_reported_complete(self):
        import strategy_claims
        class Partial(SyntheticProvider): missing_end = True
        self.assertFalse(self.run_sample(provider=Partial)['passed'])
        with patch.object(strategy_claims, 'parse_strategy_claims', side_effect=RuntimeError('private parser exception')):
            with self.assertRaisesRegex(RuntimeError, 'private parser exception'): self.run_sample()

    def test_claim_source_document_time_and_hash_cannot_differ_from_original_section(self):
        import strategy_claims
        original = strategy_claims.parse_strategy_claims
        for field, value in [('sourceUrl', 'https://example.org/wrong.pdf'), ('periodEnd', '2026-09-30'),
                             ('kind', 'semiannual'), ('publishedAt', '2026-08-20'),
                             ('retrievedAt', '2026-10-25T00:00:00Z'), ('sourceTextHash', '0' * 64)]:
            def altered(*args, **kwargs):
                result = copy.deepcopy(original(*args, **kwargs))
                result['strategyClaims']['source'][field] = value
                return result
            with self.subTest(field=field), patch.object(strategy_claims, 'parse_strategy_claims', side_effect=altered):
                self.assert_rejected(self.run_sample)

    def test_claim_module_false_or_inflated_completion_flags_cannot_be_hidden(self):
        import strategy_claims
        original = strategy_claims.parse_strategy_claims
        for field, value in [('ruleScanComplete', False), ('sourceSectionComplete', False), ('semanticAnalysisComplete', True)]:
            def altered(*args, **kwargs):
                result = copy.deepcopy(original(*args, **kwargs)); result['strategyClaims'][field] = value
                return result
            with patch.object(strategy_claims, 'parse_strategy_claims', side_effect=altered):
                self.assert_rejected(self.run_sample)

    def test_result_must_include_both_distinct_fixed_samples_not_two_copies(self):
        first = self.run_sample()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'result.json'
            self.assertFalse(verify.save_result([first, copy.deepcopy(first)], output))
            self.assertNotEqual(json.loads(output.read_text())['status'], 'verified')

    def test_save_guard_and_output_limit_publish_only_failure_marker(self):
        results = [self.run_sample(sample) for sample in verify.SAMPLES]
        for findings, maximum in [(['fixture-rejection'], 40000), ([], 100)]:
            with tempfile.TemporaryDirectory() as folder, patch.object(verify, 'inspect_content', return_value=findings), \
                    patch.object(verify, 'MAX_OUTPUT_BYTES', maximum):
                output = Path(folder) / 'result.json'
                self.assertFalse(verify.save_result(results, output))
                saved = json.loads(output.read_text())
                self.assertEqual(saved['errorCode'], 'VERIFICATION_PUBLICATION_REJECTED')
                self.assertNotIn('results', saved)
                self.assertEqual([path.name for path in Path(folder).iterdir()], ['result.json'])

    def test_main_keeps_two_failures_redacted_and_never_modifies_production(self):
        save = verify.save_result
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'result.json'
            with patch.object(verify, 'run_sample', side_effect=RuntimeError('private raw source or path')), \
                    patch.object(verify, 'save_result', side_effect=lambda results: save(results, output)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verify.main(), 1)
            result = json.loads(output.read_text())
            self.assertEqual(len(result['results']), 2)
            self.assertEqual({row['errorCode'] for row in result['results']}, {'CLAIM_VERIFICATION_FAILED'})
            self.assertFalse(result['productionDataModified'])
            self.assertFalse(result['semanticAnalysisComplete'])
            self.assertNotIn('private raw', output.read_text())

    def test_workflow_is_manual_read_only_bounded_and_contains_its_test_dependencies(self):
        text = (ROOT / '.github/workflows/verify-strategy-claims.yml').read_text()
        for expected in ('contents: read', 'timeout-minutes: 12', 'persist-credentials: false',
                         '/.github/workflows/verify-strategy-claims.yml', '/config/*.json', 'retention-days: 7',
                         'path: .cache/strategy-claims-verification/result.json'):
            self.assertIn(expected, text)
        for forbidden in ('git push', 'contents: write', 'web/data/', 'schedule:', 'secrets.'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__': unittest.main()
