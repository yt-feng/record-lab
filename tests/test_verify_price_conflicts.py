"""Bounded synthetic price responses only; tests never contact quote providers."""
import copy
import datetime as dt
import errno
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import verify_price_conflicts as verify
from price_coverage import expected_sessions

NOW = dt.datetime(2026, 10, 27, 2, tzinfo=dt.timezone.utc)
START, END = '2026-04-01', '2026-06-30'


def urls(code='600001'):
    query = {'secid': '1.' + code, 'klt': '101', 'fqt': '0', 'beg': '20260401', 'end': '20260630',
             'fields1': 'f1,f2,f3,f4,f5,f6', 'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'}
    return ['https://push2his.eastmoney.com/api/qt/stock/kline/get?' + urlencode(query),
            'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?' + urlencode({'param': f'sh{code},day,{START},{END},1000,'})]


def sampled():
    return {'kind': 'price-conflict-diagnostic', 'quarters': [{'quarter': '2026Q2', 'selected': [
        {'market': 'CN', 'stockCode': '600001', 'currency': 'CNY', 'rangeStart': START, 'rangeEnd': END,
         'groups': [{'sourceUrl': source, 'low': 1 if index == 0 else .9, 'high': 2 if index == 0 else 2.1,
                     'reusableCompleteSource': True} for index, source in enumerate(urls())],
         'differences': [{'firstGroup': 0, 'secondGroup': 1}], 'resolution': 'unresolved'}]}],
        'productionDataModified': False, 'automaticResolution': False}


def payload(provider, low=1, high=2, partial=False):
    dates = expected_sessions('CN', START, END)
    if partial: dates = dates[:1]
    if provider == 'eastmoney':
        return json.dumps({'data': {'code': '600001', 'klines': [f'{date},1,1.5,{high},{low},10' for date in dates]}}).encode()
    return json.dumps({'data': {'sh600001': {'qt': {'sh600001': ['', '', '600001']},
                                           'day': [[date, 1, 1.5, high, low] for date in dates]}}}).encode()


class PriceConflictVerificationTests(unittest.TestCase):
    def test_http_failures_emit_only_fixed_class_without_reading_response_details(self):
        class Response:
            def __init__(self, status): self.status_code = status
            def __enter__(self): return self
            def __exit__(self, *args): pass
            @property
            def headers(self): raise AssertionError('must not read headers')
            @property
            def text(self): raise AssertionError('must not read response body')
            def iter_content(self, *args): raise AssertionError('must not read response body')
        for status, category in ((400, 'HTTP_4XX'), (403, 'HTTP_4XX'), (429, 'HTTP_429'), (500, 'HTTP_5XX'), (503, 'HTTP_5XX'), (302, 'OTHER')):
            result = sampled()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(verify.requests, 'get', return_value=Response(status)):
                verify.requery(result, now=NOW)
            for quote in result['quarters'][0]['selected'][0]['requotes']:
                self.assertEqual(quote['errorCode'], 'REQUERY_HTTP_REJECTED')
                self.assertEqual(quote['networkErrorClass'], category); self.assertEqual(quote['status'], 'unavailable')
                self.assertEqual(quote['observedAt'], NOW.isoformat()); self.assertNotIn('fetchedAt', quote)
                self.assertNotIn('responseSha256', quote)

    def test_wrapped_transport_errors_use_types_and_errno_never_messages(self):
        req, low = verify.requests.exceptions, verify.urllib3_errors
        hidden = 'PRIVATE_DIAGNOSTIC_TEXT'
        cases = [(req.Timeout(hidden), 'REQUERY_TIMEOUT', 'TIMEOUT'),
                 (req.SSLError(hidden), 'REQUERY_NETWORK_ERROR', 'TLS'),
                 (req.ConnectionError(low.SSLError(ssl.SSLError(hidden))), 'REQUERY_NETWORK_ERROR', 'TLS'),
                 (req.ConnectionError(low.ProtocolError(hidden, ConnectionResetError(errno.ECONNRESET, hidden))), 'REQUERY_NETWORK_ERROR', 'CONNECTION_RESET'),
                 (req.ConnectionError(low.ProtocolError(hidden, http.client.RemoteDisconnected(hidden))), 'REQUERY_NETWORK_ERROR', 'DISCONNECTED'),
                 (req.ConnectionError(socket.gaierror(socket.EAI_NONAME, hidden)), 'REQUERY_NETWORK_ERROR', 'DNS'),
                 (req.ConnectionError(low.MaxRetryError(None, 'not-output', reason=socket.gaierror(socket.EAI_AGAIN, hidden))), 'REQUERY_NETWORK_ERROR', 'DNS'),
                 (req.ConnectionError(low.ReadTimeoutError(None, 'not-output', hidden)), 'REQUERY_NETWORK_ERROR', 'TIMEOUT'),
                 (req.ConnectionError(BrokenPipeError(errno.EPIPE, hidden)), 'REQUERY_NETWORK_ERROR', 'DISCONNECTED'),
                 (req.ConnectionError(hidden + ' TLS DNS reset timeout'), 'REQUERY_NETWORK_ERROR', 'OTHER')]
        for error, code, category in cases:
            result = sampled()
            with self.subTest(category=category), patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(verify.requests, 'get', side_effect=error):
                verify.requery(result, now=NOW)
            quotes = result['quarters'][0]['selected'][0]['requotes']
            self.assertTrue(all(q['status'] == 'unavailable' and q['errorCode'] == code and q['networkErrorClass'] == category for q in quotes))
            self.assertEqual(result['quarters'][0]['selected'][0]['requeryComparison']['status'], 'insufficient-verified-responses')
            self.assertNotIn(hidden, json.dumps(result)); self.assertNotIn('not-output', json.dumps(result))

    def test_exception_walk_is_bounded_and_legacy_errors_do_not_gain_inferred_classes(self):
        error = verify.requests.exceptions.ConnectionError('TLS is only text'); error.__cause__ = error
        self.assertEqual(verify.network_error_class(error), 'OTHER')
        nested = socket.gaierror(socket.EAI_NONAME, 'private')
        for _ in range(40): nested = verify.requests.exceptions.ConnectionError(nested)
        self.assertEqual(verify.network_error_class(nested), 'OTHER')
        result = sampled()
        def legacy(_): raise verify.DiagnosticError('REQUERY_NETWORK_ERROR')
        verify.requery(result, fetch=legacy, now=NOW)
        self.assertTrue(all('networkErrorClass' not in q for q in result['quarters'][0]['selected'][0]['requotes']))
        with self.assertRaisesRegex(ValueError, 'INVALID_NETWORK_ERROR_CLASS'):
            verify.DiagnosticError('REQUERY_NETWORK_ERROR', network_error_class='unbounded description')

    def test_two_original_sources_only_and_strict_daily_differences_are_retained(self):
        result = sampled(); calls = []
        def fetch(source):
            calls.append(source)
            return payload('eastmoney') if 'eastmoney' in source else payload('tencent', .9, 2.1)
        verify.requery(result, fetch=fetch, now=NOW)
        row = result['quarters'][0]['selected'][0]
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['requery']['requestCount'], 2)
        self.assertEqual(row['requeryComparison']['status'], 'repeatable-source-difference')
        self.assertTrue(row['requeryComparison']['bothCompleteCalendars'])
        self.assertGreater(row['requeryComparison']['strictlyDifferentSessionCount'], 0)
        self.assertGreater(row['requeryComparison']['dailyExamples'][0]['absoluteDelta']['low'], 0)
        self.assertEqual(row['resolution'], 'unresolved')
        self.assertTrue(all(value['storedRangeExactlyReproduced'] for value in row['requotes']))
        self.assertTrue(all(len(value['responseSha256']) == 64 for value in row['requotes']))
        self.assertNotIn('klines', json.dumps(result)); self.assertNotIn('qt', row['requotes'][0])

    def test_current_agreement_does_not_automatically_resolve_historical_disagreement(self):
        result = sampled()
        verify.requery(result, fetch=lambda source: payload('eastmoney' if 'eastmoney' in source else 'tencent'), now=NOW)
        row = result['quarters'][0]['selected'][0]
        self.assertEqual(row['requeryComparison']['status'], 'current-responses-agree')
        self.assertEqual(row['resolution'], 'unresolved')
        self.assertFalse(row['requotes'][1]['storedRangeExactlyReproduced'])
        self.assertFalse(result['automaticResolution'])

    def test_partial_response_and_duplicate_original_url_never_form_complete_source_agreement(self):
        result = sampled()
        verify.requery(result, fetch=lambda source: payload('eastmoney' if 'eastmoney' in source else 'tencent', partial='gtimg' in source), now=NOW)
        self.assertEqual(result['quarters'][0]['selected'][0]['requeryComparison']['status'], 'partial-responses')
        single = sampled(); row = single['quarters'][0]['selected'][0]
        row['groups'][1]['sourceUrl'] = row['groups'][0]['sourceUrl']; calls = []
        verify.requery(single, fetch=lambda source: calls.append(source) or payload('eastmoney'), now=NOW)
        self.assertEqual(len(calls), 1)
        self.assertEqual(row['requeryComparison']['status'], 'insufficient-verified-responses')

    def test_request_validation_rejects_private_hosts_adjusted_windows_codes_and_credentials_before_fetch(self):
        credential_url = 'https://' + 'user:password' + '@' + 'push2his.eastmoney.com/api/qt/stock/kline/get'
        sources = ['https://127.0.0.1/quote', 'https://localhost/quote', credential_url,
                   urls()[0].replace('fqt=0', 'fqt=1'), urls()[0].replace('600001', '600002'),
                   urls()[0].replace('beg=20260401', 'beg=20260402'), urls()[0] + '&token=x', urls()[0] + '#private',
                   urls()[0] + '&fqt=0', urls()[1].replace('1000%2C', '1000%2Cqfq')]
        for source in sources:
            result = sampled(); row = result['quarters'][0]['selected'][0]
            row['groups'] = [{**row['groups'][0], 'sourceUrl': source}]; row['differences'] = [{'firstGroup': 0, 'secondGroup': 0}]
            calls = []; verify.requery(result, fetch=lambda url: calls.append(url), now=NOW)
            self.assertEqual(calls, [], source)
            self.assertFalse(row['requotes'][0]['requested'])
            self.assertEqual(row['requotes'][0]['status'], 'unavailable')

    def test_yahoo_split_inspection_extends_to_now_but_retains_quarter_and_blocks_split_adjustment(self):
        stamp = int(dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc).timestamp())
        end = int(dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc).timestamp())
        source = 'https://query1.finance.yahoo.com/v8/finance/chart/AAPL?' + urlencode({'period1': stamp, 'period2': end,
            'interval': '1d', 'events': 'splits', 'includeAdjustedClose': 'false'})
        result = sampled(); row = result['quarters'][0]['selected'][0]
        row.update({'market': 'US', 'stockCode': 'AAPL', 'currency': 'USD'})
        row['groups'] = [{'sourceUrl': source, 'low': 1, 'high': 2, 'reusableCompleteSource': True}]
        row['differences'] = [{'firstGroup': 0, 'secondGroup': 0}]
        urls_seen = []
        body = {'chart': {'result': [{'meta': {'symbol': 'AAPL', 'currency': 'USD', 'instrumentType': 'EQUITY',
                'exchangeName': 'NMS', 'fullExchangeName': 'NasdaqGS', 'exchangeTimezoneName': 'America/New_York'},
                'events': {'splits': {'one': {'date': int(NOW.timestamp()), 'numerator': 2, 'denominator': 1}}}}], 'error': None}}
        verify.requery(result, fetch=lambda url: urls_seen.append(url) or json.dumps(body).encode(), now=NOW)
        quote = row['requotes'][0]
        self.assertEqual(parse_qs(urlparse(urls_seen[0]).query)['period1'], [str(stamp)])
        self.assertGreater(int(parse_qs(urlparse(urls_seen[0]).query)['period2'][0]), end)
        self.assertTrue(quote['splitInspectionExtended']); self.assertEqual(quote['splitEventCount'], 1)
        self.assertEqual(quote['errorCode'], 'PRICE_CORPORATE_ACTION_UNRESOLVED')
        self.assertEqual(quote['status'], 'unavailable')

    def test_response_limits_and_parser_failures_preserve_other_source_result(self):
        for raw in (b'x' * (verify.MAX_RESPONSE + 1), b'not json', b'{}'):
            result = sampled()
            verify.requery(result, fetch=lambda url: raw if 'eastmoney' in url else payload('tencent'), now=NOW)
            quotes = result['quarters'][0]['selected'][0]['requotes']
            self.assertEqual(quotes[0]['status'], 'unavailable'); self.assertEqual(quotes[1]['status'], 'observed')
            self.assertEqual(result['requery']['requestCount'], 2)

    def test_transport_requires_actions_and_disables_redirects(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(verify.requests, 'get') as request:
            with self.assertRaisesRegex(verify.DiagnosticError, 'DIAGNOSTIC_REQUIRES_ACTIONS'):
                verify.ActionsTransport()(urls()[0])
            request.assert_not_called()
        class Response:
            status_code = 302
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(verify.requests, 'get', return_value=Response()) as request:
            with self.assertRaisesRegex(verify.DiagnosticError, 'REQUERY_HTTP_REJECTED'):
                verify.ActionsTransport()(urls()[0])
            self.assertFalse(request.call_args.kwargs['allow_redirects'])

    def test_compact_output_guard_and_cli_gate_do_not_write_production(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'result.json'
            result = sampled(); verify.requery(result, fetch=lambda url: payload('eastmoney' if 'eastmoney' in url else 'tencent'), now=NOW)
            verify.write_result(result, output)
            self.assertLess(output.stat().st_size, 65536)
            with self.assertRaisesRegex(verify.DiagnosticError, 'DIAGNOSTIC_OUTPUT_REJECTED'):
                verify.write_result({'tooLarge': 'x' * 65536}, output)
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(verify.subprocess, 'run') as runner:
                self.assertEqual(verify.main(['--data', temporary]), 1); runner.assert_not_called()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(verify.subprocess, 'run') as runner:
                self.assertEqual(verify.main(['--data', temporary, '--output', str(output)]), 1); runner.assert_not_called()

    def test_workflow_is_read_only_and_limits_real_execution_to_actions(self):
        source = Path(__file__).parents[1]
        workflow = (source / '.github/workflows/verify-price-conflicts.yml').read_text()
        self.assertIn('contents: read', workflow); self.assertNotIn('contents: write', workflow)
        self.assertIn('persist-credentials: false', workflow); self.assertIn('default: false', workflow)
        self.assertIn('timeout-minutes: 15', workflow)
        self.assertNotIn('git push', workflow); self.assertIn('retention-days: 7', workflow)

    def test_sparse_modules_run_end_to_end_on_empty_synthetic_quarters_without_transport(self):
        source = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ('scripts', 'lib', 'config'):
                shutil.copytree(source / directory, root / directory, ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'web').mkdir()
            for file in (source / 'web').glob('*.js'): shutil.copy2(file, root / 'web' / file.name)
            data = root / 'synthetic-source'
            for quarter, end, baseline in [('2026Q2', '2026-06-30', '2026-03-31'), ('2026Q3', '2026-09-30', '2026-06-30')]:
                directory = data / quarter; (directory / 'companies').mkdir(parents=True)
                (directory / 'latest.json').write_text(json.dumps({'schemaVersion': 1, 'sourceMode': 'actions', 'targetPeriodEnd': end,
                    'baselinePeriodEnd': baseline, 'retrievedAt': None, 'companyFiles': []}))
            before = {str(file.relative_to(data)): file.read_bytes() for file in data.rglob('*.json')}
            output = root / 'diagnostic/result.json'
            result = subprocess.run([sys.executable, 'scripts/verify_price_conflicts.py', '--data', str(data), '--output', str(output)],
                                    cwd=root, env={**os.environ, 'GITHUB_ACTIONS': 'true', 'PRICE_DIAGNOSTIC_REFETCH': 'false',
                                                   'PRICE_DIAGNOSTIC_DATA_COMMIT': 'a' * 40}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_bytes())
            self.assertEqual(report['requery']['requestCount'], 0); self.assertFalse(report['productionDataModified'])
            self.assertEqual([row['completeSourceConflictKeys'] for row in report['quarters']], [0, 0])
            self.assertEqual(before, {str(file.relative_to(data)): file.read_bytes() for file in data.rglob('*.json')})


if __name__ == '__main__': unittest.main()
