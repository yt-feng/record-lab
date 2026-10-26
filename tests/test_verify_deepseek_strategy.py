"""Pure synthetic production extraction and fake model response, no HTTP/PDF."""
import copy
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
import verify_deepseek_strategy as verify
from report_extract import _details_for_pages
from test_deepseek_strategy import envelope


def extractor(raw, source_url, period_end, kind, **options):
    pages = ['合成基金2026年第2季度报告\n§2 基金产品概况\n2.1 基金基本情况\n基金主代码 168401\n'
             '§3 主要财务指标和基金净值表现\n',
             '4.4 报告期内基金的投资策略和运作分析\n本基金看好科技行业。市场波动仍需观察。\n'
             '4.5 报告期内基金的业绩表现\n本基金净值表现。']
    return _details_for_pages(pages, source_url, period_end, kind, options['parse_pages'], None,
        published_at=options['published_at'], retrieved_at=options['retrieved_at'],
        pdf_sha256=hashlib.sha256(raw).hexdigest(), total_pages=2)


class VerifyDeepSeekTests(unittest.TestCase):
    def setUp(self):
        action = patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}); action.start(); self.addCleanup(action.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('no test network'))
        network.start(); self.addCleanup(network.stop)

    def run_sample(self, **options):
        return verify.verify('synthetic-credential', fetch_pdf=lambda: b'%PDF synthetic', extractor=extractor,
                             transport=lambda _, body: envelope(body), **options)

    def test_actual_extractor_packet_validator_bounded_artifact(self):
        events = []
        result = self.run_sample(checkpoint=lambda value: events.append(copy.deepcopy(value)))
        self.assertEqual(result['status'], 'candidate-validated', result)
        self.assertEqual((result['pdfRequests'], result['modelRequests']), (1, 1))
        self.assertTrue(all(result['checks'].values()))
        self.assertFalse(result['productionReady']); self.assertFalse(result['semanticAnalysisComplete'])
        self.assertEqual(result['source']['retrievedAt'], result['pdfReceipt']['fetchedAt'])
        self.assertTrue(any(row['modelReceipt'] and row['modelReceipt']['outcome'] == 'started' for row in events))
        self.assertLessEqual(len(result['publicEvidence']), 8)
        self.assertLessEqual(sum(len(row['text']) for row in result['publicEvidence']), 1280)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'; verify.write_result(result, output)
            text = output.read_text()
            self.assertNotIn('section_text', text); self.assertNotIn('synthetic-credential', text)
            self.assertNotIn('messages', text); self.assertNotIn('%PDF', text)

    def test_missing_secret_is_pending_and_has_zero_source_or_model_requests(self):
        result = verify.verify(None, fetch_pdf=lambda: self.fail('unneeded PDF'), transport=lambda *_: self.fail('unneeded model'))
        self.assertEqual((result['status'], result['pdfRequests'], result['modelRequests']), ('pending', 0, 0))
        self.assertTrue(result['executionSuccess']); self.assertEqual(result['dataStatus'], 'pending')

    def test_bad_pdf_identity_or_pool_prevents_paid_attempt(self):
        for field in ('portfolioIdentityEvidence', 'strategyEvidencePool'):
            def bad(*args, **kwargs):
                result = extractor(*args, **kwargs); result.pop(field); return result
            result = verify.verify('synthetic', fetch_pdf=lambda: b'%PDF synthetic', extractor=bad,
                                   transport=lambda *_: self.fail('invalid source must not reach model'))
            self.assertEqual(result['status'], 'failed'); self.assertEqual(result['modelRequests'], 0)

    def test_all_unknown_is_accounted_without_claiming_complete_interpretation(self):
        result = verify.verify('synthetic', fetch_pdf=lambda: b'%PDF synthetic', extractor=extractor,
                               transport=lambda _, body: envelope(body, interpreted=False))
        self.assertEqual(result['status'], 'candidate-validated')
        analysis = result['inference']['analysis']
        self.assertEqual(analysis['coverage']['unknown'], 2)
        self.assertFalse(analysis['semanticCorrectnessVerified']); self.assertEqual(result['publicEvidence'], [])

    def test_transport_timeout_retains_started_receipt_without_retry(self):
        calls = []
        def transport(*_): calls.append(True); return {'errorCode': 'PROVIDER_TIMEOUT'}
        result = verify.verify('synthetic', fetch_pdf=lambda: b'%PDF synthetic', extractor=extractor, transport=transport)
        self.assertEqual(len(calls), 1); self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['modelReceipt']['outcome'], 'unknown')
        self.assertEqual(result['modelRequests'], 1); self.assertFalse(result['executionSuccess'])

    def test_checkpoint_before_pdf_and_model_call(self):
        events = []
        def fetch():
            self.assertEqual(events[-1]['pdfReceipt']['outcome'], 'started'); return b'%PDF synthetic'
        def transport(_, body):
            self.assertEqual(events[-1]['modelReceipt']['outcome'], 'started'); return envelope(body)
        verify.verify('synthetic', fetch_pdf=fetch, extractor=extractor, transport=transport,
                      checkpoint=lambda item: events.append(copy.deepcopy(item)))

    def test_pdf_http_request_has_no_redirect_or_retry(self):
        from unittest.mock import MagicMock
        session, response = MagicMock(), MagicMock()
        session.__enter__.return_value = session; session.get.return_value.__enter__.return_value = response
        response.status_code = 302
        with patch.object(verify.requests, 'Session', return_value=session):
            with self.assertRaisesRegex(ValueError, 'PDF_HTTP_REJECTED'): verify.pdf_get()
        self.assertEqual(session.get.call_count, 1); self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        response.iter_content.assert_not_called()

    def test_workflow_uses_only_named_secret_in_one_step_and_no_production_write(self):
        workflow = (ROOT / '.github/workflows/verify-deepseek-strategy.yml').read_text()
        self.assertEqual(workflow.count('secrets.'), 1)
        self.assertIn('secrets.DEEPSEEK_API_KEY', workflow)
        self.assertIn('contents: read', workflow); self.assertIn('cancel-in-progress: false', workflow)
        self.assertNotIn('contents: write', workflow); self.assertNotIn('git push', workflow)
        self.assertIn('/config/', workflow); self.assertIn('/scripts/', workflow)

    def test_artifact_rejects_unknown_top_level_and_nested_full_text(self):
        good = self.run_sample()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            for mutate in (lambda row: row.update(extra='unexpected'),
                           lambda row: row['inference'].update(rawText='must not publish'),
                           lambda row: row['inference']['analysis'].update(section_text='must not publish'),
                           lambda row: row.update(productionReady=True)):
                changed = copy.deepcopy(good); mutate(changed)
                with self.assertRaisesRegex(ValueError, 'PUBLICATION_REJECTED'): verify.write_result(changed, output)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
