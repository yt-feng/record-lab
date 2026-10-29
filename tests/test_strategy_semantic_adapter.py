"""Same private parser/transport/mapper path with synthetic bytes and pages."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deepseek_strategy
import report_extract
import strategy_semantic_adapter as adapter
import strategy_semantic_pool as mapper
from providers import parse_pdf_pages
from test_deepseek_strategy import envelope

OLD = '2026-10-26T19:00:00.123456+00:00'
NEW = '2026-10-26T20:00:00.123456+00:00'
STARTED = '2026-10-26T20:01:00.123456+00:00'
FINISHED = '2026-10-26T20:01:05.123456+00:00'
NOW = '2026-10-26T21:00:00.123456+00:00'
RAW = b'%PDF synthetic private report bytes'
SOURCE = {'sourceUrl': 'https://example.org/quarter.pdf', 'periodEnd': '2026-06-30',
          'kind': 'quarterly', 'publishedAt': '2026-07-21', 'fundCode': '000001'}
PAGES = ['合成基金2026年第2季度报告\n报告期2026年4月1日至2026年6月30日\n'
         '§2 基金产品概况\n基金主代码 000001\n下属分级基金的交易代码\n000001 000002\n'
         '§3 主要财务指标和基金净值表现\n3.1 主要财务指标\n',
         '4.4 报告期内基金的投资策略和运作分析\n本基金看好科技行业。市场波动仍需观察。\n'
         '4.5 报告期内基金的业绩表现\n本期净值表现。']


def baseline():
    return {**SOURCE, 'retrievedAt': OLD,
        **report_extract._details_for_pages(PAGES, SOURCE['sourceUrl'], SOURCE['periodEnd'], SOURCE['kind'],
            parse_pdf_pages, published_at=SOURCE['publishedAt'], retrieved_at=OLD,
            pdf_sha256=hashlib.sha256(RAW).hexdigest(), total_pages=len(PAGES))}


class SemanticAdapterTests(unittest.TestCase):
    def setUp(self):
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected HTTP'))
        network.start(); self.addCleanup(network.stop)
        self.report = baseline()
        self.events = []

    def run_candidate(self, **changes):
        options = {'code': '000001', 'source_observed_at': NEW, 'credential': 'synthetic-credential',
            'usage_history': [], 'expected_usage_history_digest': mapper.canonical_digest([]),
            'expected_baseline_digest': self.report['strategyEvidencePool']['digest'],
            'checkpoint': lambda item: self.events.append(copy.deepcopy(item)),
            'transport': lambda _, body: envelope(body), 'now': NOW}
        options.update(changes)
        with patch.object(report_extract, '_pypdf_pages', return_value=PAGES) as parser, \
             patch.object(deepseek_strategy, 'stamp', side_effect=[STARTED, FINISHED]):
            result = adapter.prepare_candidate(RAW, self.report, **options)
        return result, parser.call_count

    def test_actual_single_parse_to_private_model_callback_to_canonical_mapper(self):
        old = copy.deepcopy(self.report)
        result, parses = self.run_candidate()
        self.assertEqual(result['status'], 'candidate-ready', result)
        self.assertEqual((parses, result['parseCount'], result['modelRequests']), (1, 1, 1))
        candidate = result['candidate']
        self.assertEqual(self.report, old)
        self.assertEqual(candidate['pool']['source']['retrievedAt'], OLD)
        self.assertEqual(candidate['sidecar']['derivedFromObservedAt'], OLD)
        self.assertEqual(candidate['sidecar']['source']['retrievedAt'], NEW)
        self.assertEqual(candidate['sidecar']['modelObservation']['observation']['attemptedAt'], STARTED)
        self.assertEqual(candidate['sidecar']['modelObservation']['observation']['finishedAt'], FINISHED)
        self.assertEqual(candidate['usageAccounting']['knownTotalTokens'], 300)
        self.assertTrue(candidate['sourceSectionComplete'])
        self.assertTrue(candidate['inputCoverageComplete'])
        self.assertFalse(candidate['semanticCorrectnessVerified'])
        self.assertFalse(candidate['productionReady'])
        self.assertFalse(result['productionDataModified'])
        self.assertEqual(candidate['claims'][0]['evidencePoolId'], candidate['pool']['quotes'][0]['id'])
        serialized = json.dumps(result, ensure_ascii=False)
        for private in ('synthetic-credential', '%PDF', 'section_text', 'reasoning_content', 'messages', PAGES[0]):
            self.assertNotIn(private, serialized)
        self.assertNotIn('text', candidate['claims'][0]['subject']['span'])
        self.assertTrue(any(event['requestReceipt'] and event['requestReceipt']['outcome'] == 'started'
                            for event in self.events))
        self.assertEqual(self.events[-1]['usageHistory'], candidate['usageHistory'])

    def test_no_default_model_transport_and_missing_secret_do_not_parse_or_request(self):
        for changes, code in [({'transport': None}, 'MODEL_TRANSPORT_NOT_CONFIGURED'),
                              ({'credential': None}, 'PROVIDER_CREDENTIALS_MISSING')]:
            result, parses = self.run_candidate(**changes)
            self.assertEqual(result['errorCode'], code)
            self.assertEqual((parses, result['modelRequests']), (0, 0))

    def test_known_zero_request_input_limit_is_not_reported_as_missing_receipt(self):
        inference = {'status': 'rejected', 'errorCode': 'SEMANTIC_INPUT_LIMIT',
                     'requestCount': 0, 'attempted': False, 'receipt': None}
        with patch.object(adapter, 'run_packet', return_value=inference):
            result, parses = self.run_candidate()
        self.assertEqual((result['status'], result['errorCode'], result['modelRequests']),
                         ('rejected', 'SEMANTIC_INPUT_LIMIT', 0))
        self.assertEqual((parses, result['usageHistory']), (1, []))

    def test_source_pool_pdf_identity_and_physical_page_changes_prevent_request(self):
        for change in ('pool', 'bytes', 'pages'):
            with self.subTest(change=change):
                self.report = baseline()
                options = {'transport': lambda *_: self.fail('source mismatch must prevent request')}
                if change == 'pool': options['expected_baseline_digest'] = 'f' * 64
                elif change == 'bytes': self.report['strategyClaimsIdentityEvidence']['pdfSha256'] = 'f' * 64
                else:
                    def extractor(raw, source, end, kind, **kwargs):
                        return report_extract._details_for_pages(PAGES, source, end, kind, kwargs['parse_pages'],
                            page_numbers=[1, None], published_at=kwargs['published_at'],
                            retrieved_at=kwargs['retrieved_at'], pdf_sha256=hashlib.sha256(raw).hexdigest(),
                            on_private_sections=kwargs['on_private_sections'])
                    options['extractor'] = extractor
                result, _ = self.run_candidate(**options)
                self.assertEqual(result['status'], 'rejected', result)
                self.assertEqual(result['modelRequests'], 0)

    def test_failed_history_and_rejected_output_usage_are_preserved(self):
        history = [{'requestDigest': 'a' * 64, 'inputDigest': 'b' * 64,
                    'attemptedAt': '2026-10-26T19:30:00Z', 'finishedAt': '2026-10-26T19:30:05Z',
                    'outcome': 'failed', 'responseDigest': None, 'usage': None}]
        original = copy.deepcopy(history)
        result, _ = self.run_candidate(usage_history=history, expected_usage_history_digest=mapper.canonical_digest(history),
                                      max_attempts=2)
        self.assertEqual(result['status'], 'candidate-ready', result)
        self.assertEqual(history, original)
        self.assertEqual(result['usageHistory'][0], original[0])
        self.assertEqual(result['candidate']['usageAccounting']['unknownUsageAttempts'], 1)
        self.assertEqual(result['candidate']['usageAccounting']['attemptCount'], 2)
        def truncated(_, body):
            outer = json.loads(envelope(body)['body'])
            outer['choices'][0]['finish_reason'] = 'length'
            return {'httpStatus': 200, 'body': json.dumps(outer)}
        rejected, _ = self.run_candidate(transport=truncated)
        self.assertEqual(rejected['status'], 'rejected')
        self.assertIsNone(rejected['candidate'])
        self.assertEqual(rejected['usageHistory'][-1]['usage']['total_tokens'], 300)
        self.assertIsNotNone(rejected['usageHistory'][-1]['responseDigest'])

    def test_unknown_is_checkpointed_and_cannot_be_automatically_retried(self):
        result, _ = self.run_candidate(transport=lambda *_: {'errorCode': 'PROVIDER_TIMEOUT'})
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['usageHistory'][-1]['outcome'], 'unknown')
        self.assertEqual(result['modelRequests'], 1)
        again, parses = self.run_candidate(usage_history=result['usageHistory'],
            expected_usage_history_digest=result['usageHistoryDigest'], max_attempts=2)
        self.assertEqual(again['errorCode'], 'MODEL_PRIOR_OUTCOME_UNRESOLVED')
        self.assertEqual((parses, again['modelRequests']), (0, 0))
        self.assertEqual(again['usageHistory'], result['usageHistory'])

    def test_duplicate_success_is_not_repeated_and_invalid_history_blocks_before_parse(self):
        first, _ = self.run_candidate()
        again, _ = self.run_candidate(usage_history=first['usageHistory'],
            expected_usage_history_digest=first['usageHistoryDigest'], max_attempts=2,
            transport=lambda *_: self.fail('must not repeat successful request'))
        self.assertEqual(again['status'], 'already-observed')
        self.assertEqual(again['modelRequests'], 0)
        self.assertEqual(again['usageHistory'], first['usageHistory'])
        invalid, parses = self.run_candidate(usage_history=first['usageHistory'],
            expected_usage_history_digest='f' * 64, max_attempts=2)
        self.assertEqual(invalid['errorCode'], 'MODEL_HISTORY_BINDING_INVALID')
        self.assertEqual((parses, invalid['modelRequests']), (0, 0))

    def test_checkpoint_failure_prevents_request_and_is_sanitized(self):
        def checkpoint(_): raise RuntimeError('PRIVATE_CHECKPOINT_SENTINEL')
        with self.assertRaisesRegex(adapter.SemanticCheckpointError, '^SEMANTIC_CHECKPOINT_FAILED$'):
            self.run_candidate(checkpoint=checkpoint, transport=lambda *_: self.fail('unexpected request'))

    def test_mineru_private_callback_failure_does_not_fallback_or_repeat(self):
        class MinerU:
            def parse_with_token(self, *_): return '\n\n'.join(PAGES)
        calls = []
        def callback(**context):
            calls.append(context)
            raise RuntimeError('PRIVATE_CALLBACK_SENTINEL')
        with patch.object(report_extract, '_pypdf_pages', side_effect=AssertionError('unexpected fallback')):
            with self.assertRaisesRegex(report_extract.ExtractionError, '^PRIVATE_SECTION_CALLBACK_FAILED$'):
                report_extract.extract_report(RAW, SOURCE['sourceUrl'], SOURCE['periodEnd'], SOURCE['kind'],
                    parse_pages=parse_pdf_pages, mineru_client=MinerU(), env={'MINER_U': 'synthetic'},
                    published_at=SOURCE['publishedAt'], retrieved_at=NEW, on_private_sections=callback)
        self.assertEqual(len(calls), 1)
        self.assertIn(None, calls[0]['page_numbers'])


if __name__ == '__main__':
    unittest.main()
