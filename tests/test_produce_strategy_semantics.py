"""Synthetic production execution uses the real parser, model binder and mapper."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deepseek_strategy
import report_extract
import produce_strategy_semantics as producer
from strategy_semantic_pool import canonical_digest
from test_strategy_semantic_adapter import baseline, PAGES, RAW, NEW, STARTED, FINISHED, NOW
from test_deepseek_strategy import envelope


def task_fixture():
    report = baseline()
    value = {'schemaVersion': 1, 'kind': 'strategy-semantic-production-task', 'quarter': '2026Q2', 'code': '000001',
        'requestedCode': '000001', 'targetPeriodEnd': '2026-06-30', 'companyPath': 'companies/co1.json', 'fundIndex': 0,
        'reportDigest': canonical_digest(report), 'report': report, 'baselinePoolDigest': report['strategyEvidencePool']['digest'],
        'usageHistory': [], 'expectedUsageHistoryDigest': canonical_digest([]), 'reservationId': '1:1',
        'runId': '1', 'runAttempt': '1', 'plannedAt': NEW, 'maxAttempts': 1, 'ledgerKey': '2026Q2:000001'}
    value['taskDigest'] = canonical_digest(value)
    return value


def run_synthetic(task, *, transport=None, fetch=None, checkpoint=None):
    with patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected HTTP')), \
         patch.object(report_extract, '_pypdf_pages', return_value=PAGES), \
         patch.object(deepseek_strategy, 'stamp', side_effect=[STARTED, FINISHED]), \
         patch.object(producer, 'stamp', side_effect=[NEW, NEW, NOW]):
        return producer.execute(task, credential='synthetic-credential',
            checkpoint=checkpoint or (lambda _: None), pdf_fetch=fetch or (lambda _: RAW),
            transport=transport or (lambda _, body: envelope(body)))


class ProductionExecutionTests(unittest.TestCase):
    def test_actual_producer_creates_independent_receipt_after_private_mapping(self):
        task = task_fixture(); before = copy.deepcopy(task); checkpoints = []
        result, receipt = run_synthetic(task, checkpoint=lambda row: checkpoints.append(copy.deepcopy(row)))
        self.assertEqual(result['status'], 'candidate-ready', result)
        self.assertEqual(result['pdfRequests'], 1)
        self.assertEqual(result['adapter']['modelRequests'], 1)
        self.assertEqual(receipt['candidateDigest'], result['adapter']['candidate']['candidateDigest'])
        self.assertEqual(receipt['resultDigest'], canonical_digest(result))
        self.assertEqual(receipt['usageHistoryDigest'], canonical_digest(result['adapter']['usageHistory']))
        self.assertEqual(task, before)
        self.assertEqual(checkpoints[0]['pdfReceipt']['outcome'], 'started')
        self.assertIsNone(checkpoints[0]['adapter'])
        self.assertTrue(any((row.get('adapter') or {}).get('modelStatus') == 'started' for row in checkpoints))
        public = json.dumps([result, receipt], ensure_ascii=False)
        for private in ['synthetic-credential', '%PDF', 'section_text', 'messages', 'reasoning_content', PAGES[0]]:
            self.assertNotIn(private, public)

    def test_unknown_model_attempt_is_accounted_without_retry(self):
        calls = []
        result, receipt = run_synthetic(task_fixture(), transport=lambda *_: calls.append(1) or {'errorCode': 'PROVIDER_TIMEOUT'})
        self.assertEqual(calls, [1])
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['adapter']['usageHistory'][-1]['outcome'], 'unknown')
        self.assertIsNone(receipt['candidateDigest'])

    def test_source_failure_preserves_prior_accounting_and_never_calls_model(self):
        task = task_fixture()
        task['usageHistory'] = [{'requestDigest': 'a' * 64, 'inputDigest': 'b' * 64,
            'attemptedAt': '2026-10-26T19:01:00Z', 'finishedAt': '2026-10-26T19:01:10Z',
            'outcome': 'failed', 'responseDigest': None, 'usage': None}]
        task['expectedUsageHistoryDigest'] = canonical_digest(task['usageHistory'])
        task['maxAttempts'] = 2
        task['taskDigest'] = canonical_digest({key: value for key, value in task.items() if key != 'taskDigest'})
        def failed(_): raise ValueError('SEMANTIC_PDF_HTTP_REJECTED')
        with patch.object(producer, 'stamp', return_value=NOW):
            result, receipt = producer.execute(task, credential='synthetic-credential', checkpoint=lambda _: None,
                pdf_fetch=failed, transport=lambda *_: self.fail('must not call model'))
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['adapter']['usageHistory'], task['usageHistory'])
        self.assertEqual(receipt['usageHistoryDigest'], task['expectedUsageHistoryDigest'])

    def test_changed_task_and_failed_checkpoint_prevent_requests(self):
        task = task_fixture(); task['code'] = '000003'
        with self.assertRaisesRegex(ValueError, 'SEMANTIC_TASK_CHANGED'):
            run_synthetic(task, fetch=lambda _: self.fail('task must be checked first'))
        def broken(_): raise OSError('synthetic checkpoint unavailable')
        with self.assertRaises(OSError):
            run_synthetic(task_fixture(), checkpoint=broken, fetch=lambda _: self.fail('checkpoint before GET'))


if __name__ == '__main__':
    unittest.main()
