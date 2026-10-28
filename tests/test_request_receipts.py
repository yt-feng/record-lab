"""Synthetic calls only: no HTTP and no real report or price download."""
import concurrent.futures
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import collect
import collect_prices
import providers
import request_receipts as rr

EM = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
YAHOO = 'https://query1.finance.yahoo.com/v8/finance/chart/600001.SS'
TENCENT = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
ARGS = ('600001', 'CN', '2026-04-01', '2026-06-30')


def config():
    return {'asOf': '2026-10-27', 'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31',
            'funds': [{'code': '000001'}], 'maxWorkers': 1, 'maxPriceSymbols': 2,
            'traversal': {'batchId': '10', 'plannedAt': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()}}


def environment():
    return {'GITHUB_RUN_ID': '1000', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40, 'RECEIPT_BATCH_ID': '10'}


class Response:
    status_code = 200
    encoding = 'utf-8'
    def raise_for_status(self): pass
    def iter_content(self, _): yield b'{}'
    def close(self): pass


class Session:
    def __init__(self, callback=None, failure=None):
        self.calls, self.callback, self.failure = [], callback, failure
    def get(self, url, **kwargs):
        if self.callback: self.callback()
        self.calls.append(url)
        if self.failure: raise self.failure
        return Response()


def provider(session):
    value = providers.PublicProvider()
    value.session = session
    return value


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = config()
        self.raw = rr.encoded(self.cfg)
        self.env = environment()
        self.journal = rr.RequestJournal(self.root / 'journal', self.cfg, self.raw, self.env)

    def result(self): return rr.validate(self.root / 'journal', self.cfg, self.raw, self.env)

    def instrumented(self, session=None):
        value = provider(session or Session())
        value.request_observer = self.journal.observer('CN:600001', ARGS)
        return value

    def test_real_provider_records_before_get_and_separates_three_fallbacks(self):
        seen = []
        def check():
            data = self.result()
            self.assertEqual(data['counts']['unknown'], 1)
            seen.append(data['records'][0]['calls'][-1]['reservedAt'])
        session = Session(check)
        value = self.instrumented(session)
        with self.assertRaises(providers.DataError): value.price_bars(*ARGS)
        self.assertEqual(session.calls, [EM, YAHOO, TENCENT])
        self.journal.close(True)
        result = self.result()
        self.assertEqual(result['counts'], {'logicalReservations': 1, 'sessionGetReservations': 3, 'returned': 3, 'failed': 0, 'unknown': 0})
        self.assertTrue(result['accountingCompleted'])
        self.assertEqual(seen, [call['reservedAt'] for call in result['records'][0]['calls']])
        self.assertEqual(result['header']['redirectAccounting'], 'not-observed')

    def test_us_two_fallbacks_same_logical_identity(self):
        value = provider(Session())
        value.request_observer = self.journal.observer('US:BRK.B', ('BRK.B', 'US', *ARGS[2:]))
        with self.assertRaises(providers.DataError): value.price_bars('BRK.B', 'US', *ARGS[2:])
        self.journal.close(True)
        result = self.result()
        self.assertEqual([x['source'] for x in result['records'][0]['calls']], ['yahoo-query1', 'yahoo-query2'])
        self.assertEqual(result['counts']['logicalReservations'], 1)

    def test_request_failure_fixed_code_not_exception_or_headers(self):
        value = self.instrumented(Session(failure=providers.requests.exceptions.Timeout('private response text')))
        with self.assertRaises(providers.DataError): value.get(EM, attempts_override=1)
        self.journal.close(True)
        result = self.result()
        self.assertEqual(result['counts']['failed'], 1)
        self.assertEqual(result['records'][0]['calls'][0]['errorCode'], 'TIMEOUT')
        self.assertNotIn('private response', rr.encoded(result).decode())
        self.assertNotIn('http', rr.encoded(result).decode())

    def test_interruption_and_callback_failure_keep_unknown_not_zero(self):
        value = self.instrumented()
        value.on_request = lambda: (_ for _ in ()).throw(RuntimeError('raw checkpoint failed'))
        with self.assertRaises(RuntimeError): value.get(EM, attempts_override=1)
        self.assertEqual(value.session.calls, [])
        result = self.result()
        self.assertEqual(result['counts']['unknown'], 1)
        self.assertFalse(result['accountingCompleted'])
        self.assertIsNone(result['header']['closedAt'])
        first = copy.deepcopy(result)
        self.assertEqual(first, self.result())

    def test_disk_failure_before_get_is_fatal_for_future_calls(self):
        value = self.instrumented()
        with patch.object(rr, 'write_atomic', side_effect=OSError('not-public')):
            with self.assertRaises(rr.ReceiptError): value.get(EM, attempts_override=1)
        with self.assertRaises(rr.ReceiptError): value.get(YAHOO, attempts_override=1)
        with self.assertRaises(rr.ReceiptError): self.journal.close(True)
        self.assertEqual(value.session.calls, [])
        self.assertFalse(self.result()['accountingCompleted'])

    def test_finish_write_failure_keeps_prior_unknown_and_blocks_next_get(self):
        original = rr.write_atomic
        def fail_finish(path, body):
            if body.get('calls') and body['calls'][-1]['outcome'] == 'returned': raise OSError('full')
            original(path, body)
        value = self.instrumented()
        with patch.object(rr, 'write_atomic', side_effect=fail_finish):
            with self.assertRaises(rr.ReceiptError): value.get(EM, attempts_override=1)
        with self.assertRaises(rr.ReceiptError): value.get(YAHOO, attempts_override=1)
        self.assertEqual(len(value.session.calls), 1)
        self.assertEqual(self.result()['counts']['unknown'], 1)

    def test_three_limits_fail_closed_before_tail_not_silent_truncation(self):
        value = self.instrumented()
        for _ in range(3): value.get(EM, attempts_override=1)
        with self.assertRaises(rr.ReceiptError): value.get(EM, attempts_override=1)
        self.assertEqual(len(value.session.calls), 3)
        self.assertEqual(rr.MAX_LOGICAL, 2000)
        self.assertEqual(rr.MAX_SESSION_GET, 6000)
        for cap in ('MAX_LOGICAL', 'MAX_SESSION_GET'):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as root:
                j = rr.RequestJournal(Path(root)/'journal', self.cfg, self.raw, self.env)
                p = provider(Session()); p.request_observer = j.observer('CN:600001', ARGS)
                with patch.object(rr, cap, 0):
                    with self.assertRaises(rr.ReceiptError): p.get(EM, attempts_override=1)
                self.assertEqual(p.session.calls, [])

    def test_two_workers_keep_counts_and_ids_without_overwrite(self):
        def fetch(code):
            p = provider(Session()); p.request_observer = self.journal.observer('CN:'+code, (code, *ARGS[1:]))
            p.get(EM, attempts_override=1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(fetch, ['600001', '600002', '600003', '600004']))
        self.journal.close(True)
        result = self.result()
        self.assertEqual(result['counts']['sessionGetReservations'], 4)
        self.assertEqual(len({r['logicalId'] for r in result['records']}), 4)

    def test_provider_non_market_endpoint_never_called_with_observer(self):
        value = self.instrumented()
        with self.assertRaises(rr.ReceiptError): value.get('https://example.org/report.pdf')
        self.assertEqual(value.session.calls, [])
        with self.assertRaises(rr.ReceiptError): value.get(EM)

    def test_deadline_invalid_range_zero_reservations(self):
        results = []
        collect_prices.execute([('CN:600001', ARGS)], time.monotonic()-1, 1,
                               lambda *x: self.fail('start'), lambda *x: results.append(x), request_journal=self.journal)
        value = self.instrumented()
        with self.assertRaises(providers.DataError): value.price_bars('600001', 'CN', 'bad', ARGS[3])
        self.journal.close(True)
        self.assertEqual(self.result()['counts']['sessionGetReservations'], 0)
        self.assertFalse(results[0][3])

    def test_whitelist_rejects_extra_fields_wrong_phase_and_replayed_finish(self):
        value = self.instrumented(); value.get(EM, attempts_override=1)
        path = next(p for p in (self.root/'journal').glob('*.json') if p.name != 'header.json')
        body = json.loads(path.read_bytes()); original = path.read_bytes()
        for target, key, data in [(body, 'rawResponse', 'private'), (body['calls'][0], 'headers', {}),
                                   (body['identity'], 'sourceUrl', EM)]:
            target[key] = data; path.write_bytes(rr.encoded(body))
            with self.assertRaises(rr.ReceiptError): self.result()
            del target[key]; path.write_bytes(original)
        wrong = copy.deepcopy(self.cfg); wrong['targetPeriodEnd'] = '2026-09-30'
        with self.assertRaises(rr.ReceiptError): rr.validate(self.root/'journal', wrong, rr.encoded(wrong), self.env)
        token = (body['logicalId'], body['calls'][0]['callId'])
        with self.assertRaises(rr.ReceiptError): self.journal.finish(token, 'returned')

    def test_original_directory_cannot_be_reopened_reset_or_symlink_read(self):
        with self.assertRaises(rr.ReceiptError): rr.RequestJournal(self.root/'journal', self.cfg, self.raw, self.env)
        header = self.root/'journal'/'header.json'; data = header.read_bytes(); header.unlink()
        outside = self.root/'other.json'; outside.write_bytes(data); header.symlink_to(outside)
        with self.assertRaises(rr.ReceiptError): self.result()

    def test_temp_file_not_exported_and_closed_counts_cannot_hide_missing_receipt(self):
        value = self.instrumented(); value.get(EM, attempts_override=1); self.journal.close(True)
        temporary = self.root/'journal'/'header.tmp'; temporary.write_text('private failed temporary bytes')
        result = self.result()
        self.assertFalse(result['accountingCompleted'])
        self.assertNotIn('private', rr.encoded(result).decode())
        next(p for p in (self.root/'journal').glob('*.json') if p.name != 'header.json').unlink()
        with self.assertRaises(rr.ReceiptError): self.result()

    def test_ordinary_collection_actual_start_not_parse_error_counts_and_price_only(self):
        fund = {'code': '000001', 'reports': [{'periodEnd': ARGS[3], 'holdings': [{'market': 'CN', 'stockCode': '600001'}], 'narrative': None}]}
        session = Session()
        with patch.object(collect, 'collect_fund', return_value=(fund, [])), patch.object(collect, 'PublicProvider', side_effect=lambda: provider(session)):
            raw = collect.collect(self.cfg, self.cfg['asOf'], request_journal=self.journal)
        self.assertEqual(raw['coverage']['priceSymbolsAttempted'], 1)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(self.result()['counts']['logicalReservations'], 1)
        # Invalid securities are rejected before provider transport and do not
        # fabricate an attached-price attempt as the previous broad except did.
        broken = copy.deepcopy(fund); broken['reports'][0]['holdings'][0]['stockCode'] = 'bad'
        with patch.object(collect, 'collect_fund', return_value=(broken, [])):
            raw = collect.collect(self.cfg, self.cfg['asOf'], request_journal=self.journal)
        self.assertEqual(raw['coverage']['priceSymbolsAttempted'], 0)

    def test_price_only_shared_transport_preserves_original_attempt_clock(self):
        from test_price_patches import PricePatchTests
        case = PricePatchTests(); case.setUp()
        case.config['traversal'].update(self.cfg['traversal'])
        session = Session()
        raw = collect_prices.collect(case.config, self.cfg['asOf'], provider_factory=lambda: provider(session), request_journal=self.journal)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(raw['pricePatches'][0]['attemptedStockKeys'], ['CN:600001'])
        self.assertLessEqual(rr.instant(self.result()['records'][0]['reservedAt']), rr.instant(raw['pricePatches'][0]['attemptedAt']))
        self.assertEqual(raw['funds'], [])

    def test_cli_exports_only_validated_data_even_after_collect_interruption(self):
        self.journal.observer('CN:600001', ARGS).begin(EM)
        plan = self.root/'plan.json'; plan.write_bytes(self.raw)
        output = self.root/'public'/'receipts.json'
        github_output = self.root/'step-output'
        command = [sys.executable, str(ROOT/'scripts/request_receipts.py'), '--journal', str(self.root/'journal'), '--config', str(plan), '--output', str(output)]
        run = subprocess.run(command, env={**os.environ, **self.env, 'GITHUB_OUTPUT': str(github_output)}, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout)
        result = json.loads(output.read_bytes()); self.assertEqual(result['counts']['unknown'], 1)
        self.assertFalse(result['accountingCompleted'])
        self.assertEqual(github_output.read_text(), 'completed=false\n')
        self.assertNotIn(str(self.root), output.read_text())
        output.unlink(); body = json.loads((self.root/'journal'/'header.json').read_bytes()); body['private'] = 'secret'
        (self.root/'journal'/'header.json').write_bytes(rr.encoded(body))
        bad = subprocess.run(command, env={**os.environ, **self.env}, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertFalse(output.exists()); self.assertNotIn('secret', bad.stdout+bad.stderr)

    def test_workflow_independent_upload_before_build_and_failure_gate(self):
        source = (ROOT/'.github/workflows/traverse.yml').read_text()
        block = source[source.index('      - name: Validate independent market'):source.index('  publish:')]
        self.assertLess(block.index('id: accounting_upload'), block.index('- name: Build batch'))
        upload = block[block.index('id: accounting_upload'):block.index('- name: Build batch')]
        self.assertIn("if: always() && steps.accounting.outcome == 'success'", upload)
        self.assertNotIn('publication.outcome', upload)
        self.assertIn('include-hidden-files: true', upload)
        self.assertIn('path: .cache/request-accounting-public/receipts.json', upload)
        self.assertNotIn('request-accounting-private\n', upload)
        self.assertIn("if: always() && steps.build.outcome == 'success'", block)
        gate = block[block.index('      - name: Preserve technical failure status'):]
        code = '\n'.join(line[10:] for line in gate.splitlines() if line.startswith('          '))
        code = code[code.index('import os'):code.rindex('PY')]
        names = ('COLLECT', 'BUILD', 'STATUS', 'PUBLICATION', 'ACCOUNTING', 'ACCOUNTING_UPLOAD')
        good_env = {**os.environ, **{name+'_OUTCOME': 'success' for name in names}, 'ACCOUNTING_COMPLETED': 'true'}
        self.assertEqual(subprocess.run([sys.executable, '-c', code], env=good_env, capture_output=True).returncode, 0)
        for failed in names:
            env = {**good_env, failed+'_OUTCOME': 'failure'}
            self.assertEqual(subprocess.run([sys.executable, '-c', code], env=env, capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run([sys.executable, '-c', code], env={**good_env, 'ACCOUNTING_COMPLETED': 'false'}, capture_output=True).returncode, 1)


if __name__ == '__main__': unittest.main()
