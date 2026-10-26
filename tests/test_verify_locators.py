"""No-network fixtures exercise the real collect -> Node build -> status chain."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import verify_locators as verify
from portfolio_identity import parse_portfolio_identity
from providers import DataError, PublicProvider


class SyntheticPdfProvider(verify.FixedPdfProvider):
    """Synthetic pages only; the production collector and builder are not mocked."""
    def report_details(self, report, max_pages=80):
        self.pdf_requests += 1
        self.pdf_bytes = 200
        self.pdf_sha256 = hashlib.sha256(b'synthetic-fixture').hexdigest()
        pages = [self.sample['title'], '§2 基金产品概况\n基金主代码 ' + self.sample['codes'][0] + '\n',
                 '§3 主要财务指标和基金净值表现\n' + '这是合成测试文本。' * 30]
        identity = parse_portfolio_identity(pages, report,
            retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat(), pdf_sha256=self.pdf_sha256)
        if not identity: raise AssertionError('invalid synthetic identity')
        self.details = {'navYuan': 10000, 'narrative': None, 'parseMethod': 'pypdf',
            'sourceTextHash': identity['sourceTextHash'], 'portfolioIdentityEvidence': identity,
            'pageStats': {'charCount': 500, 'nonEmptyPages': 3, 'totalPages': 3, 'processedPages': 3,
                          'pageNumbersVerified': True, 'truncated': False, 'textStatus': 'usable'},
            'pdfHoldings': [{'stockCode': '600001', 'stockName': '合成股票', 'market': 'CN',
                            'currency': 'CNY', 'shares': 100, 'weightPct': 5, 'marketValueYuan': 500}]}
        return copy.deepcopy(self.details)


class VerifyLocatorTests(unittest.TestCase):
    def setUp(self):
        self.actions = patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'})
        self.actions.start(); self.addCleanup(self.actions.stop)
        self.mineru = patch.object(verify, 'mineru_tokens_from_env', return_value=[])
        self.mineru.start(); self.addCleanup(self.mineru.stop)
        # Even the positive test must fail rather than issue a real HTTP call.
        self.http = patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden in unit tests'))
        self.http.start(); self.addCleanup(self.http.stop)

    def test_both_quarters_use_production_chain_and_cached_missing_identity_is_receipt_only(self):
        for sample in verify.SAMPLES:
            with self.subTest(period=sample['targetPeriod']):
                result = verify.run_sample(sample, SyntheticPdfProvider)
                self.assertTrue(result['passed'], result)
                self.assertEqual(result['pdfRequestCount'], 1)
                self.assertEqual(result['negative']['pdfRequestCount'], 0)
                self.assertEqual(result['positive']['discoveryRetrievedAt'], sample['retrievedAt'])
                self.assertEqual(result['negative']['discoveryRetrievedAt'], sample['retrievedAt'])
                self.assertTrue(result['positive']['sourceAttemptIsSeparate'])
                self.assertEqual(result['positive']['holdingCount'], 1)
                self.assertEqual(result['negative']['fundCount'], 0)
                self.assertEqual(result['negative']['receiptCount'], 1)
                self.assertNotIn('sourceTextHash', json.dumps(result))
                self.assertNotIn('合成测试文本', json.dumps(result, ensure_ascii=False))

    def test_wrong_original_pdf_code_or_period_cannot_pass_on_list_association(self):
        for field, value in [('sourcePortfolioCode', '999999'), ('periodEnd', '2026-03-31')]:
            class WrongIdentity(SyntheticPdfProvider):
                def report_details(self, report, max_pages=80):
                    details = super().report_details(report, max_pages)
                    details['portfolioIdentityEvidence'][field] = value
                    self.details = details
                    return copy.deepcopy(details)
            result = verify.run_sample(verify.SAMPLES[0], WrongIdentity)
            self.assertFalse(result['passed'])
            self.assertEqual(result['positive']['fundCount'], 0)
            self.assertTrue(result['positive']['identityMismatchReported'])
            self.assertEqual(result['positive']['receiptCount'], 1)

    def test_actions_gate_precedes_provider_creation_and_output(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(verify, 'OUTPUT', Path(folder) / 'result.json'), \
                patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(verify, 'FixedPdfProvider') as provider:
            with self.assertRaises(verify.VerificationError) as error: verify.run_sample(verify.SAMPLES[0])
            self.assertEqual(error.exception.code, 'ACTIONS_REQUIRED')
            self.assertEqual(verify.main(), 2)
            provider.assert_not_called()
            self.assertFalse(verify.OUTPUT.exists())

    def test_fixed_source_only_one_binary_request_and_no_mineru(self):
        source = verify.FixedPdfProvider(verify.SAMPLES[0]); self.addCleanup(source.session.close)
        with patch.object(PublicProvider, 'get', return_value=b'%PDF-synthetic') as get:
            with self.assertRaises(DataError): source.get('https://example.org/other.pdf', binary=True)
            with self.assertRaises(DataError): source.get(source.sample['sourceUrl'])
            self.assertEqual(source.pdf_requests, 0)
            self.assertTrue(source.get(source.sample['sourceUrl'], binary=True).startswith(b'%PDF'))
            with self.assertRaises(DataError): source.get(source.sample['sourceUrl'], binary=True)
            self.assertEqual(get.call_count, 1)
        with patch.object(verify, 'mineru_tokens_from_env', return_value=['configured']):
            with self.assertRaises(verify.VerificationError) as error: verify.run_sample(verify.SAMPLES[0])
            self.assertEqual(error.exception.code, 'PARSER_CONFIGURATION_UNSUPPORTED')
        with self.assertRaises(verify.VerificationError):
            verify.run_sample({**verify.SAMPLES[0], 'sourceUrl': 'https://example.org/other.pdf'})

    def test_invalid_pdf_and_bytes_limit_fail_without_followup_request(self):
        for data, code in [(b'not a PDF', 'INVALID_PDF'), (b'%PDF' + b'x' * 32, 'RESPONSE_TOO_LARGE')]:
            source = verify.FixedPdfProvider(verify.SAMPLES[0]); self.addCleanup(source.session.close)
            with patch.object(PublicProvider, 'get', return_value=data) as get, patch.object(verify, 'MAX_PDF_BYTES', 32):
                with self.assertRaises(DataError) as error: source.get(source.sample['sourceUrl'], binary=True)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(get.call_count, 1)

    def test_failed_bridge_and_invalid_status_never_report_verified(self):
        with patch.object(verify.subprocess, 'run', side_effect=OSError('private path')):
            with self.assertRaises(verify.VerificationError) as error:
                verify.build_snapshot({}, {}, dt.datetime.now(dt.timezone.utc))
            self.assertEqual(error.exception.code, 'SNAPSHOT_BRIDGE_FAILED')
        with patch.object(verify, 'valid_status', return_value=False):
            with self.assertRaises(verify.VerificationError) as error:
                verify.run_sample(verify.SAMPLES[0], SyntheticPdfProvider)
            self.assertEqual(error.exception.code, 'INVALID_STATUS')

    def test_main_redacts_exceptions_preserves_two_results_and_never_claims_universe_complete(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(verify, 'OUTPUT', Path(folder) / 'result.json'), \
                patch.object(verify, 'run_sample', side_effect=[RuntimeError('private local path and source text'), {'passed': True}]):
            self.assertEqual(verify.main(), 1)
            result = json.loads(verify.OUTPUT.read_text())
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(len(result['results']), 2)
            self.assertEqual(result['results'][0]['errorCode'], 'LOCATOR_VERIFICATION_FAILED')
            self.assertNotIn('private local', verify.OUTPUT.read_text())
            for key in ('productionDataModified', 'allQuarterReportsVerified', 'historicalUniverseVerified'):
                self.assertFalse(result[key])

    def test_output_guard_and_size_limit_replace_unsafe_or_oversize_payload(self):
        for guard, maximum, code in [([{'reason': 'fixture'}], 32000, 'PUBLICATION_REJECTED'), ([], 10, 'VERIFICATION_TOO_LARGE')]:
            with tempfile.TemporaryDirectory() as folder, patch.object(verify, 'OUTPUT', Path(folder) / 'result.json'), \
                    patch.object(verify, 'run_sample', return_value={'passed': True}), \
                    patch.object(verify, 'inspect_content', return_value=guard), patch.object(verify, 'MAX_OUTPUT_BYTES', maximum):
                self.assertEqual(verify.main(), 1)
                self.assertEqual(json.loads(verify.OUTPUT.read_text())['errorCode'], code)

    def test_workflow_is_read_only_bounded_and_uploads_summary_only(self):
        workflow = (ROOT / '.github/workflows/verify-locators.yml').read_text()
        for literal in ('contents: read', 'timeout-minutes: 15', 'persist-credentials: false',
                        'path: .cache/locator-verification/result.json', 'retention-days: 7'):
            self.assertIn(literal, workflow)
        for literal in ('contents: write', 'git push', 'schedule:', 'web/data', 'secrets.'):
            self.assertNotIn(literal, workflow)


if __name__ == '__main__': unittest.main()
