"""Synthetic production execution uses the real parser, model binder and mapper."""
import copy
import hashlib
import json
import tempfile
import threading
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deepseek_strategy
import report_extract
import produce_strategy_semantics as producer
from strategy_semantic_pool import canonical_digest
from test_strategy_semantic_adapter import baseline, PAGES, RAW, NEW, STARTED, FINISHED, NOW, OLD, SOURCE
from providers import parse_pdf_pages
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


def pages_for(code):
    return [page.replace('000002', f'{int(code) + 1:06d}').replace('000001', code) for page in PAGES]


def baseline_for(code):
    source = {**SOURCE, 'fundCode': code}
    return {**source, 'retrievedAt': OLD,
        **report_extract._details_for_pages(pages_for(code), source['sourceUrl'], source['periodEnd'], source['kind'],
            parse_pdf_pages, published_at=source['publishedAt'], retrieved_at=OLD,
            pdf_sha256=hashlib.sha256(RAW).hexdigest(), total_pages=len(PAGES))}


def run_synthetic(task, *, transport=None, fetch=None, checkpoint=None):
    with patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected HTTP')), \
         patch.object(report_extract, '_pypdf_pages', return_value=pages_for(task['code'])), \
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

    def batch(self, root, count=3):
        items, products = [], {}
        for index in range(count):
            code = f'{index * 2 + 1:06d}'; task = task_fixture()
            task.update(code=code, requestedCode='', report=baseline_for(code), ledgerKey='2026Q2:' + code,
                        reservationId='1:1:' + code, ledgerPath='web/data/periods/2026Q2/semantic-model-ledgers/00.json')
            task['reportDigest'] = canonical_digest(task['report'])
            task['baselinePoolDigest'] = task['report']['strategyEvidencePool']['digest']
            task['taskDigest'] = canonical_digest({key: value for key, value in task.items() if key != 'taskDigest'})
            relative = 'tasks/' + code
            producer.write_public(Path(root) / producer.CACHE / relative / 'task.json', task)
            items.append({'code': code, 'path': relative, 'ledgerKey': task['ledgerKey'], 'taskDigest': task['taskDigest']})
            products[task['ledgerKey']] = {'usageHistory': [], 'usageHistoryDigest': canonical_digest([]),
                'attempts': [{'reservationId': task['reservationId'], 'status': 'reserved'}]}
        producer.write_public(Path(root) / task['ledgerPath'],
            {'schemaVersion': 1, 'kind': 'strategy-semantic-ledger', 'products': products})
        batch = {'schemaVersion': 1, 'kind': 'strategy-semantic-production-batch', 'quarter': '2026Q2',
                 'runId': '1', 'runAttempt': '1', 'maxWorkers': 2, 'maxSeconds': 1500, 'tasks': items}
        batch['batchDigest'] = canonical_digest(batch)
        return batch

    def test_two_worker_limit_independent_receipts_and_interruption_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            batch = self.batch(root)
            lock, two = threading.Lock(), threading.Event()
            active = {'current': 0, 'maximum': 0}; called = []
            def executor(task, *, credential, checkpoint):
                with lock:
                    active['current'] += 1; active['maximum'] = max(active['maximum'], active['current'])
                    called.append(task['code'])
                    if active['current'] == 2: two.set()
                self.assertTrue(two.wait(2))
                usage = {'requestDigest': task['taskDigest'], 'inputDigest': 'a' * 64,
                         'attemptedAt': STARTED, 'finishedAt': None, 'outcome': 'started', 'responseDigest': None, 'usage': None}
                result = {'schemaVersion': 1, 'kind': 'strategy-semantic-production-output', 'taskDigest': task['taskDigest'],
                          'reservationId': task['reservationId'], 'status': 'reserved', 'pdfRequests': 1,
                          'errorCode': None, 'finishedAt': None,
                          'adapter': {'status': 'pending', 'modelRequests': 1, 'usageHistory': [usage], 'candidate': None}}
                try:
                    checkpoint(result)
                    if task['code'] == '000005': raise RuntimeError('synthetic interruption')
                    result.update(status='rejected', finishedAt=FINISHED)
                    usage.update(outcome='rejected', finishedAt=FINISHED, usage={'prompt_tokens': 100, 'completion_tokens': 200, 'total_tokens': 300})
                    checkpoint(result)
                    return result, producer.receipt_for(task, result)
                finally:
                    with lock: active['current'] -= 1
            original = (Path(root) / 'web/data/periods/2026Q2/semantic-model-ledgers/00.json').read_bytes()
            result = producer.execute_batch(batch, root=root, credential='synthetic', executor=executor)
            self.assertEqual(sorted(called), ['000001', '000003', '000005']); self.assertEqual(active['maximum'], 2)
            self.assertEqual(result['products'][-1]['status'], 'unknown')
            for item in batch['tasks']:
                folder = Path(root) / producer.CACHE / item['path']
                task = producer.read_public(folder / 'task.json'); output = producer.read_public(folder / 'result.json')
                receipt = producer.read_public(folder / 'receipt.json')
                self.assertEqual(receipt, producer.receipt_for(task, output))
                self.assertEqual(output['adapter']['usageHistory'][0]['attemptedAt'], STARTED)
            partial = producer.read_public(Path(root) / producer.CACHE / 'tasks/000005/result.json')
            self.assertEqual(partial['adapter']['usageHistory'][0]['outcome'], 'started')
            self.assertEqual((Path(root) / 'web/data/periods/2026Q2/semantic-model-ledgers/00.json').read_bytes(), original)

    def test_deadline_records_deferral_with_no_requests_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as root:
            batch = self.batch(root); batch['maxSeconds'] = 1
            batch['batchDigest'] = canonical_digest({key: value for key, value in batch.items() if key != 'batchDigest'})
            clocks = iter([0, 2, 2, 2])
            result = producer.execute_batch(batch, root=root, credential='synthetic',
                executor=lambda *_args, **_kwargs: self.fail('deadline must prevent requests'), clock=lambda: next(clocks))
            self.assertTrue(all(row['status'] == 'deferred' for row in result['products']))
            for item in batch['tasks']:
                output = producer.read_public(Path(root) / producer.CACHE / item['path'] / 'result.json')
                self.assertEqual(output['pdfRequests'], 0); self.assertEqual(output['adapter']['modelRequests'], 0)
                self.assertEqual(output['adapter']['usageHistory'], [])

    def test_batch_checks_every_reservation_before_any_worker_request(self):
        with tempfile.TemporaryDirectory() as root:
            batch = self.batch(root)
            path = Path(root) / 'web/data/periods/2026Q2/semantic-model-ledgers/00.json'
            ledger = producer.read_public(path); ledger['products']['2026Q2:000005']['attempts'][0]['status'] = 'unknown'
            producer.write_public(path, ledger)
            with self.assertRaisesRegex(ValueError, 'SEMANTIC_RESERVATION_MISMATCH'):
                producer.execute_batch(batch, root=root, credential='synthetic',
                    executor=lambda *_args, **_kwargs: self.fail('all reservations must be checked before calls'))


if __name__ == '__main__':
    unittest.main()
