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
from deepseek_strategy import content_format
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
        diagnostic = result['inference']['bindingDiagnostics']
        self.assertEqual(diagnostic['status'], 'bound')
        self.assertEqual((diagnostic['examinedClaimCount'], diagnostic['examinedSpanCount'], diagnostic['boundSpanCount']), (0, 0, 0))
        self.assertTrue(verify.valid_binding_diagnostics(diagnostic))

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

    def test_plain_and_whole_json_fence_keep_source_receipt_and_finite_diagnostics(self):
        results = []
        for fenced in (False, True):
            events, calls = [], []
            def transport(_, body):
                calls.append(True)
                self.assertEqual(events[-1]['modelReceipt']['outcome'], 'started')
                value = envelope(body)
                if fenced:
                    outer = json.loads(value['body'])
                    content = outer['choices'][0]['message']['content']
                    outer['choices'][0]['message']['content'] = '```json\n' + content + '\n```'
                    value['body'] = json.dumps(outer, ensure_ascii=False)
                return value
            result = verify.verify('synthetic', fetch_pdf=lambda: b'%PDF synthetic', extractor=extractor,
                transport=transport, checkpoint=lambda value: events.append(copy.deepcopy(value)))
            self.assertEqual(result['status'], 'candidate-validated'); self.assertEqual(len(calls), 1)
            diagnostic = result['inference']['formatDiagnostics']
            self.assertTrue(verify.valid_format_diagnostics(diagnostic))
            self.assertEqual(diagnostic['normalization'], 'removed-single-json-fence' if fenced else 'none')
            self.assertEqual(diagnostic['jsonStatus'], 'valid'); self.assertIsNone(diagnostic['jsonError'])
            self.assertEqual(result['source']['retrievedAt'], result['pdfReceipt']['fetchedAt'])
            self.assertEqual(result['inference']['analysis']['source'], result['source'])
            self.assertEqual(result['modelReceipt'], result['inference']['receipt'])
            self.assertEqual(result['modelRequests'], 1); self.assertFalse(result['productionReady'])
            self.assertFalse(result['semanticAnalysisComplete'])
            binding = result['inference']['bindingDiagnostics']
            self.assertTrue(verify.valid_binding_diagnostics(binding)); self.assertEqual(binding['status'], 'bound')
            self.assertGreater(binding['examinedSpanCount'], 0)
            self.assertEqual(binding['boundSpanCount'], binding['examinedSpanCount'])
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'result.json'; verify.write_result(result, output)
                saved = json.loads(output.read_bytes())
                self.assertEqual(saved['inference']['formatDiagnostics'], diagnostic)
                self.assertNotIn('```', output.read_text()); self.assertNotIn('synthetic', output.read_text())
            results.append(result)
        self.assertEqual(results[0]['inputDigest'], results[1]['inputDigest'])
        self.assertEqual(results[0]['modelReceipt']['requestDigest'], results[1]['modelReceipt']['requestDigest'])

    def test_format_diagnostics_reject_arbitrary_text_fields_and_nonfinite_shapes(self):
        good = self.run_sample(); original = good['inference']['formatDiagnostics']
        malformed = []
        for key, value in [('schemaVersion', True), ('digestEncoding', 'private error body'),
                           ('originalBytes', False), ('originalCharacters', 1.0), ('normalizedBytes', 600001),
                           ('normalizedCharacters', -1), ('originalDigest', 'private provider body'),
                           ('normalizedDigest', 'F' * 64), ('leadingToken', 'object with private text'),
                           ('fence', 'unexpected'), ('normalization', 'fixed-json'), ('jsonStatus', 'complete')]:
            changed = copy.deepcopy(original); changed[key] = value; malformed.append(changed)
        changed = copy.deepcopy(original); changed['originalDigest'] = '0' * 64; malformed.append(changed)
        changed = copy.deepcopy(original); changed['normalization'] = 'removed-single-json-fence'; malformed.append(changed)
        for key in ('body', 'privateDiagnostic', 'reasoning_content'):
            changed = copy.deepcopy(original); changed[key] = 'PRIVATE_FORMAT_SENTINEL'; malformed.append(changed)
        invalid = content_format('{ "x": }')[1]
        self.assertTrue(verify.valid_format_diagnostics(invalid))
        for mutate in (lambda row: row['jsonError'].update(message='PRIVATE_ERROR_SENTINEL'),
                       lambda row: row['jsonError'].update(category='arbitrary response text'),
                       lambda row: row['jsonError'].update(line=True),
                       lambda row: row['jsonError'].update(column=0),
                       lambda row: row['jsonError'].update(position=600001),
                       lambda row: row.update(jsonError=None)):
            changed = copy.deepcopy(invalid); mutate(changed); malformed.append(changed)
        nonfinite = content_format('{"x": NaN}')[1]
        self.assertTrue(verify.valid_format_diagnostics(nonfinite))
        changed = copy.deepcopy(nonfinite); changed['jsonError']['line'] = 1; malformed.append(changed)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            for diagnostic in malformed:
                with self.subTest(diagnostic=diagnostic):
                    self.assertFalse(verify.valid_format_diagnostics(diagnostic))
                    changed = copy.deepcopy(good); changed['inference']['formatDiagnostics'] = diagnostic
                    with self.assertRaisesRegex(ValueError, 'PUBLICATION_REJECTED'): verify.write_result(changed, output)
                    self.assertFalse(output.exists())

    def test_invalid_json_only_saves_error_category_and_coordinates_not_body(self):
        private = 'PRIVATE_PROVIDER_ERROR_SENTINEL'
        def transport(_, body):
            value = envelope(body); outer = json.loads(value['body'])
            outer['choices'][0]['message']['content'] = '{"private": "' + private + '",}'
            value['body'] = json.dumps(outer); return value
        result = verify.verify('synthetic', fetch_pdf=lambda: b'%PDF synthetic', extractor=extractor, transport=transport)
        self.assertEqual(result['status'], 'rejected'); self.assertEqual(result['modelRequests'], 1)
        self.assertFalse(result['executionSuccess']); self.assertEqual(result['modelReceipt']['outcome'], 'rejected')
        diagnostic = result['inference']['formatDiagnostics']
        self.assertEqual(diagnostic['jsonStatus'], 'invalid'); self.assertEqual(diagnostic['jsonError']['category'], 'syntax')
        self.assertTrue(verify.valid_format_diagnostics(diagnostic))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'; verify.write_result(result, output)
            self.assertNotIn(private, output.read_text()); self.assertNotIn('private', json.dumps(diagnostic))
        self.assertFalse(result['semanticAnalysisComplete']); self.assertFalse(result['productionDataModified'])

    def test_transport_error_body_is_ignored_and_unknown_receipt_remains_spent(self):
        calls = []
        def transport(*_):
            calls.append(True)
            return {'errorCode': 'PROVIDER_TIMEOUT', 'body': 'PRIVATE_TRANSPORT_BODY_SENTINEL',
                    'formatDiagnostics': {'private': 'PRIVATE_FORMAT_SENTINEL'}}
        result = verify.verify('synthetic', fetch_pdf=lambda: b'%PDF synthetic', extractor=extractor, transport=transport)
        self.assertEqual(len(calls), 1); self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['modelRequests'], 1); self.assertEqual(result['modelReceipt']['outcome'], 'unknown')
        self.assertIsNone(result['inference']['formatDiagnostics'])
        self.assertIsNone(result['inference']['bindingDiagnostics'])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'; verify.write_result(result, output)
            self.assertNotIn('PRIVATE_', output.read_text())

    def test_binding_diagnostics_have_exact_fields_counts_and_no_literal_or_error_text(self):
        good = self.run_sample(); original = good['inference']['bindingDiagnostics']
        self.assertTrue(verify.valid_binding_diagnostics(original))
        malformed = []
        for key, value in [('schemaVersion', True), ('method', 'repair-model-text'), ('modelSpanFormat', 'offsets'),
                           ('status', 'complete'), ('sentenceCount', 0), ('sentenceCount', 129),
                           ('examinedClaimCount', 1.0), ('examinedClaimCount', 513),
                           ('examinedSpanCount', True), ('examinedSpanCount', 19457),
                           ('boundSpanCount', -1), ('boundSpanCount', original['examinedSpanCount'] + 1),
                           ('boundSpanCount', original['examinedSpanCount'] - 1),
                           ('errorCode', 'PRIVATE_BINDING_ERROR_SENTINEL'), ('boundResponseDigest', None),
                           ('boundResponseDigest', 'G' * 64), ('boundResponseDigest', {'text': 'PRIVATE_SENTINEL'})]:
            changed = copy.deepcopy(original); changed[key] = value; malformed.append(changed)
        for key in ('text', 'message', 'body', 'privateTrace', 'span'):
            changed = copy.deepcopy(original); changed[key] = 'PRIVATE_LITERAL_SENTINEL'; malformed.append(changed)
        rejected = {**copy.deepcopy(original), 'status': 'rejected', 'errorCode': 'MODEL_LITERAL_AMBIGUOUS',
                    'boundSpanCount': 0, 'boundResponseDigest': None}
        self.assertTrue(verify.valid_binding_diagnostics(rejected))
        for update in ({'errorCode': None}, {'errorCode': 'MODEL_LITERAL_AMBIGUOUS: PRIVATE_SENTINEL'},
                       {'boundResponseDigest': '0' * 64}):
            malformed.append({**rejected, **update})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            for diagnostic in malformed:
                with self.subTest(diagnostic=diagnostic):
                    self.assertFalse(verify.valid_binding_diagnostics(diagnostic))
                    changed = copy.deepcopy(good); changed['inference']['bindingDiagnostics'] = diagnostic
                    with self.assertRaisesRegex(ValueError, 'PUBLICATION_REJECTED'): verify.write_result(changed, output)
                    self.assertFalse(output.exists())
            for diagnostic in (None, rejected):
                changed = copy.deepcopy(good); changed['inference']['bindingDiagnostics'] = diagnostic
                with self.assertRaisesRegex(ValueError, 'PUBLICATION_REJECTED'): verify.write_result(changed, output)

    def test_nonliteral_binding_rejection_has_no_model_text_and_does_not_repeat_request(self):
        calls, events = [], []
        private = 'PRIVATE_MODEL_LITERAL_SENTINEL'
        def transport(_, body):
            calls.append(True); self.assertEqual(events[-1]['modelReceipt']['outcome'], 'started')
            value = envelope(body); outer = json.loads(value['body']); content = json.loads(outer['choices'][0]['message']['content'])
            content['sentences'][0]['claims'][0]['predicate'] = {'text': private}
            outer['choices'][0]['message']['content'] = json.dumps(content, ensure_ascii=False)
            value['body'] = json.dumps(outer, ensure_ascii=False); return value
        result = verify.verify('synthetic', fetch_pdf=lambda: b'%PDF synthetic', extractor=extractor, transport=transport,
                               checkpoint=lambda value: events.append(copy.deepcopy(value)))
        self.assertEqual(result['status'], 'rejected'); self.assertFalse(result['executionSuccess'])
        self.assertEqual((len(calls), result['modelRequests']), (1, 1))
        self.assertEqual(result['modelReceipt']['outcome'], 'rejected')
        self.assertEqual(result['source']['retrievedAt'], result['pdfReceipt']['fetchedAt'])
        binding = result['inference']['bindingDiagnostics']
        self.assertEqual(binding['status'], 'rejected'); self.assertEqual(binding['errorCode'], 'MODEL_LITERAL_NOT_FOUND')
        self.assertTrue(verify.valid_binding_diagnostics(binding)); self.assertIsNone(binding['boundResponseDigest'])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'; verify.write_result(result, output)
            self.assertNotIn(private, output.read_text())
        self.assertFalse(result['semanticAnalysisComplete']); self.assertFalse(result['productionReady'])

    def test_literal_binding_success_does_not_bypass_strict_attribution_validator(self):
        def transport(_, body):
            value = envelope(body); outer = json.loads(value['body']); content = json.loads(outer['choices'][0]['message']['content'])
            # This text occurs exactly once, but is not an allowed fund-report speaker.
            content['sentences'][0]['claims'][0]['subject']['span'] = {'text': '科技行业'}
            outer['choices'][0]['message']['content'] = json.dumps(content, ensure_ascii=False)
            value['body'] = json.dumps(outer, ensure_ascii=False); return value
        result = verify.verify('synthetic', fetch_pdf=lambda: b'%PDF synthetic', extractor=extractor, transport=transport)
        self.assertEqual(result['inference']['bindingDiagnostics']['status'], 'bound')
        self.assertEqual(result['status'], 'rejected'); self.assertFalse(result['executionSuccess'])
        self.assertEqual(result['inference']['analysis']['reasonCodes'], ['INDIVIDUAL_MANAGER_ATTRIBUTION_NOT_PROVEN'])
        self.assertEqual(result['modelRequests'], 1); self.assertFalse(result['semanticAnalysisComplete'])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'; verify.write_result(result, output)
            saved = json.loads(output.read_bytes()); self.assertEqual(saved['status'], 'rejected')
            self.assertEqual(saved['modelReceipt'], result['modelReceipt'])

    def test_repeated_literal_is_ambiguous_and_never_selects_first_occurrence(self):
        def repeated_extractor(raw, source_url, period_end, kind, **options):
            pages = ['合成基金2026年第2季度报告\n§2 基金产品概况\n2.1 基金基本情况\n基金主代码 168401\n'
                     '§3 主要财务指标和基金净值表现\n',
                     '4.4 报告期内基金的投资策略和运作分析\n本基金看好科技行业，本基金保持谨慎。\n'
                     '4.5 报告期内基金的业绩表现\n本基金净值表现。']
            return _details_for_pages(pages, source_url, period_end, kind, options['parse_pages'], None,
                published_at=options['published_at'], retrieved_at=options['retrieved_at'],
                pdf_sha256=hashlib.sha256(raw).hexdigest(), total_pages=2)
        calls = []
        def transport(_, body): calls.append(True); return envelope(body)
        result = verify.verify('synthetic', fetch_pdf=lambda: b'%PDF synthetic', extractor=repeated_extractor, transport=transport)
        self.assertEqual(result['status'], 'rejected'); self.assertEqual((len(calls), result['modelRequests']), (1, 1))
        binding = result['inference']['bindingDiagnostics']
        self.assertEqual(binding['errorCode'], 'MODEL_LITERAL_AMBIGUOUS'); self.assertEqual(binding['status'], 'rejected')
        self.assertEqual((binding['examinedSpanCount'], binding['boundSpanCount']), (1, 0))
        self.assertTrue(verify.valid_binding_diagnostics(binding)); self.assertIsNone(binding['boundResponseDigest'])
        self.assertFalse(result['executionSuccess']); self.assertFalse(result['semanticAnalysisComplete'])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'; verify.write_result(result, output)
            self.assertNotIn('本基金看好科技行业，本基金保持谨慎', output.read_text())


if __name__ == '__main__':
    unittest.main()
