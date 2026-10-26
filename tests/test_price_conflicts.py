"""Synthetic security-quarter conflicts; no real directory or quote requests."""
import copy
import datetime as dt
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import price_conflicts as conflicts
from price_coverage import CALENDAR, date_set_hash, expected_sessions
from test_verify_price_conflicts import payload, urls

NOW = dt.datetime(2026, 10, 27, 2, tzinfo=dt.timezone.utc)
FETCHED = '2026-10-26T10:00:00+00:00'


def price(low=1, high=2, quarter='2026Q2', complete=True):
    start, end, _ = conflicts.PHASES[quarter]
    dates = expected_sessions('CN', start, end); observed = dates if complete else dates[:2]
    return {'status': 'observed', 'low': low, 'high': high, 'market': 'CN', 'stockCode': '600001', 'currency': 'CNY',
            'priceBasis': 'unadjusted', 'rangeStart': start, 'rangeEnd': end, 'coverageVersion': 2, 'calendarId': CALENDAR['id'],
            'sessionScope': CALENDAR['sessionScope'], 'completeness': 'complete' if complete else 'partial',
            'observedCount': len(observed), 'observedStart': observed[0], 'observedEnd': observed[-1],
            'expectedSessionCount': len(dates), 'expectedDateSetHash': date_set_hash(dates), 'observedDateSetHash': date_set_hash(observed),
            'missingSessionCount': len(dates) - len(observed), 'missingSessionDates': dates[len(observed):],
            'unexpectedSessionCount': 0, 'unexpectedSessionDates': [], 'rejectedRowCount': 0, 'conflictingDateCount': 0}


def group(low=1, high=2, source=None, quarter='2026Q2', complete=True):
    source = source or urls()[0]
    if quarter == '2026Q3': source = source.replace('20260401', '20260701').replace('20260630', '20260930').replace('2026-04-01', '2026-07-01').replace('2026-06-30', '2026-09-30')
    return {'price': price(low, high, quarter, complete), 'observations': [{'sourceUrl': source, 'sourceUrls': [source], 'fetchedAt': FETCHED}],
            'occurrenceCount': 1, 'omittedObservationCount': 0, 'firstFetchedAt': FETCHED, 'lastFetchedAt': FETCHED}


def observation(groups=None, quarter='2026Q2'):
    start, end, baseline = conflicts.PHASES[quarter]
    return {'schemaVersion': 1, 'kind': 'price-conflict-observations', 'quarter': quarter, 'targetPeriodEnd': end,
            'baselinePeriodEnd': baseline, 'generatedAt': NOW.isoformat(), 'manifestContentHash': 'a' * 64, 'dataCommit': 'b' * 40,
            'scannedCompanyPages': 2, 'scannedFunds': 2, 'eligiblePriceKeys': 1,
            'records': [{'key': '|'.join(['CN', '600001', 'CNY', 'unadjusted', start, end, CALENDAR['id']]),
                         'groups': groups or [group(1, 2, quarter=quarter), group(.9, 2.1, urls()[1], quarter)]}]}


def registry(groups=None, quarter='2026Q2'):
    return conflicts.registry_from_observations(observation(groups, quarter), NOW)


class PriceConflictsTests(unittest.TestCase):
    def test_scanner_timeout_is_explicit_and_only_fixed_progress_counters_are_relayed(self):
        progress = {'kind': 'price-conflict-scan-progress', 'phase': 'index', 'completedCompanyPages': 25, 'totalCompanyPages': 200,
                    'validation': dict(hits=50, misses=5, bypasses=1, evictions=0, checks=5, entries=4, bytes=1000, maxEntries=4096, maxBytes=16777216)}
        raw = b'untrusted child error text\n' + json.dumps(progress).encode() + b'\n'
        error = subprocess.TimeoutExpired(['node'], 600, stderr=raw)
        output = io.StringIO()
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts.subprocess, 'run', side_effect=error) as command, contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_SCAN_TIMEOUT'):
                conflicts.scan_registry('synthetic-data', '2026Q2')
        self.assertEqual(command.call_args.kwargs['timeout'], 600)
        value = json.loads(output.getvalue()); self.assertEqual(value['status'], 'scanner-timeout')
        self.assertEqual(value['completedCompanyPages'], 25); self.assertNotIn('untrusted', output.getvalue())
        self.assertNotIn('synthetic-data', output.getvalue())

    def test_scanner_progress_rejects_unbounded_or_unknown_diagnostic_values(self):
        self.assertIsNone(conflicts.scanner_progress('not json', '2026Q2'))
        for value in ({'kind': 'price-conflict-scan-progress', 'phase': 'unknown'},
                      {'kind': 'price-conflict-scan-progress', 'phase': 'index', 'totalCompanyPages': 10001},
                      {'kind': 'price-conflict-scan-progress', 'phase': 'index', 'totalCompanyPages': 100, 'completedCompanyPages': 101}):
            self.assertIsNone(conflicts.scanner_progress(json.dumps(value), '2026Q2'))

    def test_conflicting_complete_prices_enter_independent_lane_despite_no_missing_quotes(self):
        value = registry(); before = copy.deepcopy(value)
        self.assertEqual(value['summary']['completeSourceConflictKeyCount'], 1)
        self.assertTrue(all(fact['completeSource'] for fact in value['records'][0]['facts']))
        plan = conflicts.plan_rechecks(value, now=NOW)
        self.assertEqual(plan['summary']['plannedKeyCount'], 1)
        self.assertEqual(plan['summary']['plannedRequestCount'], 2)
        self.assertEqual(value, before)
        self.assertEqual(conflicts.normalize_registry(value, NOW), value)

    def test_partial_extreme_is_distinct_from_complete_source_disagreement(self):
        value = registry([group(), group(.8, 2.2, urls()[1], complete=False)])
        self.assertEqual(value['records'][0]['types'], ['partial-extreme'])
        self.assertEqual(conflicts.plan_rechecks(value, now=NOW)['summary']['plannedRequestCount'], 2)
        narrower = registry([group(), group(1.1, 1.9, urls()[1], complete=False)])
        self.assertEqual(narrower['records'], [])
        both = registry([group(), group(.9, 2.1, urls()[1]), group(.8, 3, 'https://example.org/quote', complete=False)])
        self.assertEqual(both['records'][0]['types'], ['complete-source-disagreement', 'partial-extreme'])

    def test_float32_representation_differences_remain_exact_unresolved_conflicts(self):
        value = registry([group(4.11, 5.82), group(4.110000133514404, 5.820000171661377, urls()[1])])
        row = value['records'][0]
        self.assertEqual(row['types'], ['complete-source-disagreement'])
        self.assertEqual({fact['price']['low'] for fact in row['facts']}, {4.11, 4.110000133514404})
        self.assertEqual(row['resolution'], 'unresolved')
        self.assertEqual(conflicts.plan_rechecks(value, now=NOW)['summary']['plannedKeyCount'], 1)

    def test_digest_ignores_fund_copies_times_manifest_and_yahoo_retrieval_day_not_price_facts(self):
        stamp = int(dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc).timestamp())
        source = 'https://query1.finance.yahoo.com/v8/finance/chart/600001.SS?' + urlencode({'period1': stamp,
            'period2': int(dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc).timestamp()), 'interval': '1d', 'events': 'splits', 'includeAdjustedClose': 'false'})
        raw = observation([group(), group(.9, 2.1, source)])
        first = conflicts.registry_from_observations(raw, NOW)
        raw['manifestContentHash'] = 'd' * 64; raw['dataCommit'] = 'e' * 40; raw['scannedFunds'] = 999
        for item in raw['records'][0]['groups']:
            item['occurrenceCount'] = 999; item['firstFetchedAt'] = item['lastFetchedAt'] = '2026-10-27T01:00:00Z'
            item['observations'][0]['fetchedAt'] = item['lastFetchedAt']
            for field in ('sourceUrl', 'sourceUrls'):
                old = item['observations'][0][field]
                transform = lambda text: text.replace('query1.', 'query2.').replace('period2=1792972800', 'period2=1793059200')
                item['observations'][0][field] = [transform(url) for url in old] if isinstance(old, list) else transform(old)
        raw['records'][0]['groups'].reverse()
        second = conflicts.registry_from_observations(raw, NOW)
        self.assertEqual(first['records'][0]['digest'], second['records'][0]['digest'])
        raw['records'][0]['groups'][0]['price']['low'] -= 1e-7
        self.assertNotEqual(first['records'][0]['digest'], conflicts.registry_from_observations(raw, NOW)['records'][0]['digest'])

    def test_invalid_identity_calendar_future_and_source_never_become_requests(self):
        for mutate in (lambda raw: raw.update(targetPeriodEnd='2026-09-30'),
                       lambda raw: raw['records'][0].update(key=raw['records'][0]['key'].replace('600001', '60001')),
                       lambda raw: raw['records'][0]['groups'][0]['observations'][0].update(sourceUrl='https://localhost/private'),
                       lambda raw: raw['records'][0]['groups'][0]['observations'][0].update(fetchedAt='2026-10-28T10:00:00Z')):
            raw = observation(); mutate(raw)
            with self.assertRaises(conflicts.ConflictError): conflicts.registry_from_observations(raw, NOW)
        invalid = group(.9, 2.1, urls()[1]); invalid['price']['observedDateSetHash'] = '0' * 64
        value = registry([group(), invalid])
        self.assertEqual(value['records'][0]['types'], ['partial-extreme'])
        unsupported = registry([group(source='https://example.org/one'), group(.9, 2.1, 'https://example.org/two')])
        self.assertEqual(conflicts.plan_rechecks(unsupported, now=NOW)['summary']['blocked']['sourceUnsupported'], 1)

    def test_start_checkpoint_precedes_transport_and_timeout_unknown_consumes_once(self):
        value = registry(); plan = conflicts.plan_rechecks(value, now=NOW); saved = []; calls = []
        class Interrupted(BaseException): pass
        def fetch(url):
            calls.append(url); self.assertEqual(saved[-1][0]['requests'][0]['outcome'], 'unknown'); raise Interrupted()
        with self.assertRaises(Interrupted):
            conflicts.execute_rechecks(plan, value, checkpoint=lambda rows: saved.append(rows), fetch=fetch, now=NOW)
        self.assertEqual(len(calls), 1)
        ledger = conflicts.merge_receipts(value, None, plan, saved[-1], now=NOW)
        self.assertEqual(len(ledger['entries'][0]['digests'][0]['rounds']), 1)
        self.assertEqual(conflicts.merge_receipts(value, ledger, plan, saved[-1], now=NOW), ledger)
        replay = conflicts.execute_rechecks(plan, value, ledger, checkpoint=lambda rows: self.fail('should not checkpoint'),
                                            fetch=lambda url: self.fail('should not request'), now=NOW)
        self.assertEqual(replay['summary']['attemptedRequestCount'], 0)

    def test_failed_checkpoint_and_not_started_deadline_do_not_request_or_charge(self):
        value = registry(); plan = conflicts.plan_rechecks(value, now=NOW); calls = []
        def checkpoint(_): raise OSError('synthetic checkpoint failure')
        with self.assertRaises(OSError): conflicts.execute_rechecks(plan, value, checkpoint=checkpoint, fetch=lambda url: calls.append(url), now=NOW)
        self.assertEqual(calls, [])
        result = conflicts.execute_rechecks(plan, value, checkpoint=lambda rows: self.fail('no requests'), fetch=lambda url: calls.append(url),
                                            should_start=lambda: False, now=NOW)
        self.assertEqual(result['receipts'], [])
        self.assertEqual(conflicts.merge_receipts(value, None, plan, [], now=NOW), conflicts.empty_ledger('2026Q2'))

    def test_two_round_budget_preserves_failures_replay_and_new_digest_old_history(self):
        value = registry(); ledger = None; calls = []
        def failed(url): calls.append(url); raise conflicts.diagnostic.DiagnosticError('REQUERY_NETWORK_ERROR')
        for n in range(2):
            time = NOW + dt.timedelta(minutes=n)
            plan = conflicts.plan_rechecks(value, ledger, now=time)
            result = conflicts.execute_rechecks(plan, value, ledger, checkpoint=lambda rows: None, fetch=failed, now=time)
            ledger = conflicts.merge_receipts(value, ledger, plan, result['receipts'], now=time)
            ledger = conflicts.merge_receipts(value, ledger, plan, result['receipts'], now=time)
        self.assertEqual(len(calls), 4)
        self.assertEqual(conflicts.plan_rechecks(value, ledger, now=NOW + dt.timedelta(minutes=3))['items'], [])
        changed = registry([group(), group(.85, 2.1, urls()[1])])
        self.assertEqual(conflicts.plan_rechecks(changed, ledger, now=NOW + dt.timedelta(minutes=3))['items'][0]['round'], 1)
        self.assertEqual(len(ledger['entries'][0]['digests']), 1)

    def test_results_agree_or_differ_remain_unresolved_and_only_normalized_summary_survives(self):
        for differing in (False, True):
            value = registry(); plan = conflicts.plan_rechecks(value, now=NOW); saved = []
            def fetch(url): return payload('eastmoney') if 'eastmoney' in url else payload('tencent', .9 if differing else 1, 2.1 if differing else 2)
            execution = conflicts.execute_rechecks(plan, value, checkpoint=lambda rows: saved.append(rows), fetch=fetch, now=NOW)
            receipt = execution['receipts'][0]
            self.assertEqual(receipt['verificationStatus'], 'repeatable-source-difference' if differing else 'current-ranges-agree')
            self.assertEqual(receipt['resolution'], 'unresolved'); self.assertFalse(receipt['automaticResolution'])
            self.assertNotIn('klines', json.dumps(execution)); self.assertNotIn('rawText', json.dumps(execution))
            ledger = None
            for checkpoint in saved: ledger = conflicts.merge_receipts(value, ledger, plan, checkpoint, now=NOW)
            final = copy.deepcopy(ledger)
            for checkpoint in reversed(saved): ledger = conflicts.merge_receipts(value, ledger, plan, checkpoint, now=NOW)
            self.assertEqual(ledger, final)

    def test_one_source_or_partial_does_not_claim_two_source_agreement(self):
        value = registry([group(), group(.9, 2.1)])
        plan = conflicts.plan_rechecks(value, now=NOW)
        self.assertEqual(plan['summary']['plannedRequestCount'], 1)
        result = conflicts.execute_rechecks(plan, value, checkpoint=lambda rows: None, fetch=lambda url: payload('eastmoney'), now=NOW)
        self.assertEqual(result['receipts'][0]['verificationStatus'], 'insufficient-verified-responses')
        value = registry(); plan = conflicts.plan_rechecks(value, now=NOW)
        result = conflicts.execute_rechecks(plan, value, checkpoint=lambda rows: None,
                    fetch=lambda url: payload('eastmoney') if 'eastmoney' in url else payload('tencent', partial=True), now=NOW)
        self.assertEqual(result['receipts'][0]['verificationStatus'], 'partial-responses')

    def test_malformed_stale_plan_or_cross_quarter_receipt_cannot_spend_other_key_budget(self):
        value = registry(); plan = conflicts.plan_rechecks(value, now=NOW)
        for mutate in (lambda row: row['items'][0].update(digest='0' * 64),
                       lambda row: row['items'][0]['requests'][0].update(sourceUrl=urls()[0].replace('fqt=0', 'fqt=1')),
                       lambda row: row.update(quarter='2026Q3')):
            invalid = copy.deepcopy(plan); mutate(invalid)
            with self.assertRaises(conflicts.ConflictError):
                conflicts.execute_rechecks(invalid, value, checkpoint=lambda rows: self.fail(), fetch=lambda url: self.fail(), now=NOW)
        result = conflicts.execute_rechecks(plan, value, checkpoint=lambda rows: None, fetch=lambda url: payload('eastmoney' if 'eastmoney' in url else 'tencent'), now=NOW)
        invalid = copy.deepcopy(result['receipts']); invalid[0]['quarter'] = '2026Q3'
        with self.assertRaises(conflicts.ConflictError): conflicts.merge_receipts(value, None, plan, invalid, now=NOW)
        invalid = copy.deepcopy(result['receipts']); invalid[0]['requests'][0]['quote']['rawText'] = 'not allowed'
        with self.assertRaises(conflicts.ConflictError): conflicts.merge_receipts(value, None, plan, invalid, now=NOW)
        invalid = copy.deepcopy(result['receipts']); request = invalid[0]['requests'][0]
        request['quote']['sourceUrl'] = urls()[0] if request['provider'] == 'tencent' else urls()[1]
        with self.assertRaises(conflicts.ConflictError): conflicts.merge_receipts(value, None, plan, invalid, now=NOW)

    def test_changed_registry_never_consumes_old_receipt_as_new_fact(self):
        old = registry(); plan = conflicts.plan_rechecks(old, now=NOW); saved = []
        conflicts.execute_rechecks(plan, old, checkpoint=lambda rows: saved.append(rows), fetch=lambda url: payload('eastmoney' if 'eastmoney' in url else 'tencent'), now=NOW)
        new = registry([group(), group(.85, 2.1, urls()[1])])
        with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_PLAN_STALE'):
            conflicts.merge_receipts(new, None, plan, saved[-1], now=NOW)
        ledger = conflicts.merge_receipts(old, None, plan, saved[-1], now=NOW)
        self.assertEqual(conflicts.plan_rechecks(new, ledger, now=NOW)['items'][0]['round'], 1)
        self.assertEqual(conflicts.plan_rechecks(old, ledger, now=NOW)['items'][0]['round'], 2)

    def test_limits_and_actions_only_gate_are_enforced_before_transport(self):
        value = registry()
        for limit in (-1, 21, True):
            with self.assertRaises(conflicts.ConflictError): conflicts.plan_rechecks(value, max_keys=limit, now=NOW)
        self.assertEqual(conflicts.plan_rechecks(value, max_keys=0, now=NOW)['items'], [])
        self.assertEqual(conflicts.plan_rechecks(value, max_requests=1, now=NOW)['items'], [])
        plan = conflicts.plan_rechecks(value, now=NOW)
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(conflicts.diagnostic.requests, 'get') as request:
            with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_REQUIRES_ACTIONS'):
                conflicts.execute_rechecks(plan, value, checkpoint=lambda rows: None, now=NOW)
            with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_REQUIRES_ACTIONS'): conflicts.scan_registry('unused', '2026Q2', NOW)
            request.assert_not_called()

    def test_artifact_run_reads_published_ledger_preserves_tree_and_checkpoints_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'source'; output = root / 'output'
            for quarter in conflicts.PHASES: (source / quarter).mkdir(parents=True)
            raw = b'{"privateSyntheticMarker":"must remain unchanged"}\n'; (source / 'keep.json').write_bytes(raw)
            def scan(_, quarter, now): return registry(quarter=quarter)
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'scan_registry', side_effect=scan):
                result = conflicts.run(source, output, max_keys=2, max_requests=4, now=NOW,
                    fetch=lambda url: payload('eastmoney' if 'eastmoney' in url else 'tencent'))
                self.assertEqual(result['summary']['attemptedKeyCount'], 2)
                self.assertEqual(result['summary']['attemptedRequestCount'], 4)
                self.assertEqual((source / 'keep.json').read_bytes(), raw)
                self.assertNotIn(str(root), (output / 'result.json').read_text())
                for quarter in conflicts.PHASES:
                    ledger = conflicts.read_json(output / quarter / 'ledger.json')
                    self.assertEqual(len(ledger['entries'][0]['digests'][0]['rounds']), 1)
                    conflicts.write_json(ledger, source / quarter / 'price-conflict-ledger.json')
                second = conflicts.run(source, root / 'second', max_keys=2, max_requests=4, now=NOW + dt.timedelta(minutes=1),
                    fetch=lambda url: payload('eastmoney' if 'eastmoney' in url else 'tencent'))
                self.assertEqual(second['summary']['attemptedKeyCount'], 2)
                with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_OUTPUT_ALREADY_EXISTS'):
                    conflicts.run(source, output, now=NOW, fetch=lambda url: self.fail('no request'))
                with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_OUTPUT_INSIDE_SOURCE'):
                    conflicts.run(source, source / 'new', now=NOW)

    def test_corrupt_ledger_does_not_reset_budget(self):
        value = registry()
        for invalid in ({}, {'schemaVersion': 1, 'kind': 'price-conflict-ledger', 'quarter': '2026Q3', 'entries': []}):
            with self.assertRaises(conflicts.ConflictError): conflicts.plan_rechecks(value, invalid, now=NOW)

    def test_history_is_bounded_and_switching_back_to_old_digest_does_not_rearm(self):
        ledger, first = None, None
        for index in range(16):
            value = registry([group(), group(.9 - index / 1000, 2.1, urls()[1])]); first = first or value
            plan = conflicts.plan_rechecks(value, ledger, now=NOW)
            saved = []
            conflicts.execute_rechecks(plan, value, ledger, checkpoint=lambda rows: saved.append(rows),
                fetch=lambda url: payload('eastmoney' if 'eastmoney' in url else 'tencent'), now=NOW)
            ledger = conflicts.merge_receipts(value, ledger, plan, saved[-1], now=NOW)
        self.assertEqual(len(ledger['entries'][0]['digests']), 16)
        later = registry([group(), group(.5, 3, urls()[1])])
        self.assertEqual(conflicts.plan_rechecks(later, ledger, now=NOW)['summary']['blocked']['historyFull'], 1)
        self.assertEqual(conflicts.plan_rechecks(first, ledger, now=NOW)['items'][0]['round'], 2)

    def test_artifact_run_interruption_retains_unknown_ledger_before_provider_returns(self):
        class Interrupted(BaseException): pass
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / 'source', Path(temporary) / 'output'; source.mkdir()
            def fail_after_start(url): raise Interrupted()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'scan_registry', return_value=registry()):
                with self.assertRaises(Interrupted): conflicts.run(source, output, quarters='2026Q2', now=NOW, fetch=fail_after_start)
            ledger = conflicts.read_json(output / '2026Q2/ledger.json')
            receipt = ledger['entries'][0]['digests'][0]['rounds'][0]
            self.assertEqual(receipt['requests'][0]['outcome'], 'unknown')
            self.assertEqual(conflicts.plan_rechecks(registry(), ledger, now=NOW)['items'][0]['round'], 2)
            self.assertEqual(list(source.iterdir()), [])

    def test_sparse_cli_scans_only_small_synthetic_pages_and_performs_zero_requests(self):
        source_repo = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('scripts', 'lib', 'config'):
                shutil.copytree(source_repo / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'web').mkdir()
            for file in (source_repo / 'web').glob('*.js'): shutil.copy2(file, root / 'web' / file.name)
            data = root / 'synthetic-source'
            for quarter in conflicts.PHASES:
                directory = data / quarter; (directory / 'companies').mkdir(parents=True)
                raw = observation(quarter=quarter)
                envelope = {'schemaVersion': 1, 'sourceMode': 'actions', 'targetPeriodEnd': raw['targetPeriodEnd'],
                            'baselinePeriodEnd': raw['baselinePeriodEnd'], 'retrievedAt': FETCHED}
                (directory / 'latest.json').write_text(json.dumps({**envelope, 'companyFiles': [{'path': './data/companies/sample.json'}]}))
                funds = []
                for index, fact in enumerate(raw['records'][0]['groups']):
                    evidence = fact['observations'][0]
                    funds.append({'code': '00000' + str(index + 1), 'status': 'ok', 'retrievedAt': FETCHED,
                                  'selectedReport': {'periodEnd': raw['targetPeriodEnd']},
                                  'rows': [{'market': 'CN', 'stockCode': '600001', 'currency': 'CNY', 'priceRange': {**fact['price'], **evidence}}]})
                (directory / 'companies/sample.json').write_text(json.dumps({**envelope, 'funds': funds, 'rawText': 'MUST_NOT_ENTER_ARTIFACT'}))
            before = {str(file.relative_to(data)): file.read_bytes() for file in data.rglob('*.json')}
            output = root / 'artifact'
            process = subprocess.run([sys.executable, 'scripts/price_conflicts.py', 'run', '--data', str(data), '--output', str(output),
                                      '--quarters', 'both', '--max-keys', '0', '--max-requests', '0'], cwd=root,
                                     env={**os.environ, 'GITHUB_ACTIONS': 'true'}, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            result = json.loads((output / 'result.json').read_bytes())
            self.assertEqual(result['summary']['conflictKeyCount'], 2)
            self.assertEqual(result['summary']['attemptedRequestCount'], 0)
            serialized = '\n'.join(file.read_text() for file in output.rglob('*.json'))
            self.assertNotIn(str(root), serialized); self.assertNotIn('MUST_NOT_ENTER_ARTIFACT', serialized)
            self.assertEqual(before, {str(file.relative_to(data)): file.read_bytes() for file in data.rglob('*.json')})

    def test_retained_marker_expansion_keeps_digest_and_exhausted_budget_with_missing_local_quotes(self):
        from price_consistency import conflict_for
        original = registry(); ledger = None
        for minute in range(2):
            time = NOW + dt.timedelta(minutes=minute)
            plan = conflicts.plan_rechecks(original, ledger, now=time)
            execution = conflicts.execute_rechecks(plan, original, ledger, checkpoint=lambda rows: None,
                fetch=lambda url: payload('eastmoney' if 'eastmoney' in url else 'tencent'), now=time)
            ledger = conflicts.merge_receipts(original, ledger, plan, execution['receipts'], now=time)
        raw = observation(); key = raw['records'][0]['key']; row = conflicts.identity(key, '2026Q2')
        facts = [{'price': item['price'], **item['observations'][0]} for item in raw['records'][0]['groups']]
        marker = conflict_for(facts, row, NOW)
        self.assertIsNotNone(marker)
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary); directory = data / '2026Q2'; (directory / 'companies').mkdir(parents=True)
            envelope = {'schemaVersion': 1, 'sourceMode': 'actions', 'targetPeriodEnd': row['rangeEnd'],
                        'baselinePeriodEnd': '2026-03-31', 'retrievedAt': FETCHED}
            (directory / 'latest.json').write_text(json.dumps({**envelope, 'companyFiles': [{'path': './data/companies/one.json'}]}))
            for missing, copies in ((False, 1), (True, 1), (False, 3)):
                local_price = ({**facts[0]['price'], **raw['records'][0]['groups'][0]['observations'][0]} if not missing else
                               {'status': 'missing', 'rangeStart': row['rangeStart'], 'rangeEnd': row['rangeEnd'], 'currency': 'CNY'})
                local_price['globalPriceConflict'] = marker
                funds = [{'code': '00000' + str(index + 1), 'status': 'ok', 'retrievedAt': FETCHED,
                          'selectedReport': {'periodEnd': row['rangeEnd']},
                          'rows': [{'market': 'CN', 'stockCode': '600001', 'currency': 'CNY', 'priceRange': local_price}]} for index in range(copies)]
                (directory / 'companies/one.json').write_text(json.dumps({**envelope, 'funds': funds}))
                command = "const {scanRegistry}=require('./scripts/price-conflict-scan');process.stdout.write(JSON.stringify(scanRegistry(process.argv[1],'2026Q2',{now:'2026-10-27T02:00:00Z'})));"
                result = subprocess.run(['node', '-e', command, str(data)], cwd=Path(__file__).parents[1], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                expanded = conflicts.registry_from_observations(json.loads(result.stdout), NOW)
                self.assertEqual(expanded['records'][0]['digest'], original['records'][0]['digest'])
                self.assertEqual({fact['observations'][0]['fetchedAt'] for fact in expanded['records'][0]['facts']}, {FETCHED})
                self.assertEqual(conflicts.plan_rechecks(expanded, ledger, now=NOW + dt.timedelta(minutes=2))['items'], [])


if __name__ == '__main__': unittest.main()
