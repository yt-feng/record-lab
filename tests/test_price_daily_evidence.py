"""Two-source daily precision diagnostics on synthetic bars only; no transport."""
import copy
import contextlib
import datetime as dt
import io
import json
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import price_conflicts as conflicts
import price_daily_evidence as daily
from price_coverage import expected_sessions
from test_price_conflicts import group, observation, NOW

START, END = '2026-07-01', '2026-09-30'
DAYS = expected_sessions('CN', START, END)
LATER = NOW + dt.timedelta(minutes=1)


def yahoo_url():
    return 'https://query1.finance.yahoo.com/v8/finance/chart/000636.SZ?' + urlencode({
        'period1': int(dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc).timestamp()),
        'period2': int(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp()),
        'interval': '1d', 'events': 'splits', 'includeAdjustedClose': 'false'})


def registry():
    groups = [group(37.69, 76.8, quarter='2026Q3'), group(daily.f32(37.69), daily.f32(76.8), yahoo_url(), quarter='2026Q3')]
    raw = observation(groups, '2026Q3')
    raw = json.loads(json.dumps(raw).replace('600001', '000636').replace('1.000636', '0.000636'))
    return conflicts.registry_from_observations(raw, NOW)


def bodies():
    em = {'data': {'code': '000636', 'market': 0, 'klines': [f'{day},50.3,60.2,76.8,37.69,10' for day in DAYS]}}
    yahoo = {'chart': {'result': [{'meta': {'symbol': '000636.SZ', 'currency': 'CNY', 'exchangeName': 'SHZ',
                                         'exchangeTimezoneName': 'Asia/Shanghai', 'instrumentType': 'EQUITY'},
        'events': {}, 'timestamp': [int(dt.datetime.fromisoformat(day + 'T01:30:00+00:00').timestamp()) for day in DAYS],
        'indicators': {'quote': [{k: [daily.f32(value)] * len(DAYS) for k, value in
                                {'open': 50.3, 'close': 60.2, 'high': 76.8, 'low': 37.69}.items()}]}}], 'error': None}}
    return em, yahoo


def prior(value=None):
    value = value or registry(); plan = conflicts.plan_rechecks(value, now=NOW)
    def failed(_): raise conflicts.diagnostic.DiagnosticError('REQUERY_NETWORK_ERROR')
    first = conflicts.execute_rechecks(plan, value, checkpoint=lambda _: None, fetch=failed, now=NOW)
    return conflicts.merge_receipts(value, None, plan, first['receipts'], now=NOW)


def target_for(value):
    row = value['records'][0]
    return {'quarter': value['quarter'], 'key': row['key'], 'digest': row['digest']}


def execute(em=None, yahoo=None, fail_yahoo=False, should_start=None):
    value = registry(); ledger = prior(value); target = target_for(value)
    plan = conflicts.plan_rechecks(value, ledger, target=target, now=LATER)
    base_em, base_yahoo = bodies(); em = base_em if em is None else em; yahoo = base_yahoo if yahoo is None else yahoo
    calls, checkpoints = [], []
    def fetch(url):
        calls.append(url)
        if 'yahoo' in url and fail_yahoo: raise conflicts.diagnostic.DiagnosticError('REQUERY_NETWORK_ERROR')
        return json.dumps(yahoo if 'yahoo' in url else em).encode()
    result = conflicts.execute_rechecks(plan, value, ledger, checkpoint=lambda rows: checkpoints.append(rows),
                                        fetch=fetch, now=LATER, should_start=should_start)
    return value, ledger, plan, result, calls, checkpoints


class DailyEvidenceTests(unittest.TestCase):
    def test_exact_float32_every_ohlc_cell_complete_and_original_receipts_unchanged(self):
        value, ledger, plan, result, calls, checkpoints = execute()
        proof = result['dailyEvidence'][0]; comparison = proof['comparison']
        self.assertEqual(len(calls), 2); self.assertEqual(len(checkpoints), 4)
        self.assertEqual(comparison['status'], 'daily-float32-compatible')
        self.assertEqual(comparison['comparedCellCount'], 65 * 4)
        self.assertEqual(comparison['float32OnlyCellCount'], 65 * 4)
        self.assertTrue(comparison['bothCompleteCalendars']); self.assertEqual(comparison['nonFloat32CellCount'], 0)
        self.assertEqual(daily.normalize_evidence(proof, plan['items'][0], result['receipts'][0]), proof)
        self.assertLess(len(daily.encoded(proof)), 65536)
        self.assertLess(len(conflicts.encoded(result)), 65536)
        self.assertNotIn('klines', json.dumps(result)); self.assertNotIn('indicators', json.dumps(result))
        self.assertEqual(proof['resolution'], 'unresolved'); self.assertFalse(proof['automaticResolution'])
        self.assertFalse(proof['productionDataModified'])
        for request in result['receipts'][0]['requests']:
            self.assertNotIn('series', request['quote']); self.assertEqual(request['originalFetchedAt'], '2026-10-26T10:00:00+00:00')
        final = conflicts.merge_receipts(value, ledger, plan, result['receipts'], now=LATER)
        self.assertEqual(final['entries'][0]['digests'][0]['rounds'][0], ledger['entries'][0]['digests'][0]['rounds'][0])
        self.assertEqual(conflicts.merge_receipts(value, final, plan, result['receipts'], now=LATER), final)
        self.assertEqual(conflicts.plan_rechecks(value, final, now=LATER, target=target_for(value))['items'], [])

    def test_one_close_mismatch_cannot_hide_behind_matching_quarter_extremes(self):
        em, yahoo = bodies(); yahoo['chart']['result'][0]['indicators']['quote'][0]['close'][3] = 60.25
        proof = execute(em, yahoo)[3]['dailyEvidence'][0]
        self.assertEqual(proof['comparison']['status'], 'daily-non-float32-difference')
        self.assertEqual(proof['comparison']['nonFloat32CellCount'], 1)
        self.assertTrue(all(s['storedRangeExactlyReproduced'] for s in proof['sources']))

    def test_symmetric_float32_bucket_is_not_directed_equality(self):
        em, yahoo = bodies(); value = 50.3000001
        self.assertEqual(daily.f32(value), daily.f32(50.3)); self.assertNotEqual(value, daily.f32(50.3))
        yahoo['chart']['result'][0]['indicators']['quote'][0]['open'][2] = value
        proof = execute(em, yahoo)[3]['dailyEvidence'][0]
        self.assertEqual(proof['comparison']['nonFloat32CellCount'], 1)
        self.assertEqual(proof['comparison']['status'], 'daily-non-float32-difference')

    def test_missing_open_null_open_bad_ohlc_and_wrong_timezone_are_partial(self):
        for mutation in ('missing', 'null', 'outside', 'timezone', 'instrument'):
            em, yahoo = bodies(); data = yahoo['chart']['result'][0]; q = data['indicators']['quote'][0]
            if mutation == 'missing': del q['open']
            elif mutation == 'null': q['open'][0] = None
            elif mutation == 'outside': q['open'][0] = 100
            elif mutation == 'timezone': data['meta']['exchangeTimezoneName'] = 'America/New_York'
            else: data['meta']['instrumentType'] = 'INDEX'
            with self.subTest(mutation=mutation):
                result = execute(em, yahoo)[3]; proof = result['dailyEvidence'][0]
                self.assertEqual(proof['comparison']['status'], 'partial-evidence')
                self.assertFalse(proof['comparison']['bothCompleteCalendars'])
                self.assertEqual(proof['comparison']['comparedCellCount'], 0)
                self.assertEqual(next(r for r in result['receipts'][0]['requests'] if r['provider'] == 'yahoo')['outcome'], 'observed')

    def test_missing_day_duplicate_day_holiday_and_invalid_numeric_are_partial(self):
        for mutation in ('missing', 'duplicate', 'holiday', 'badnumber'):
            em, yahoo = bodies(); lines = em['data']['klines']
            if mutation == 'missing': lines.pop()
            elif mutation == 'duplicate': lines.append(lines[0])
            elif mutation == 'holiday': lines.append('2026-07-04,50.3,60.2,76.8,37.69,10')
            else: lines[0] = lines[0].replace(',50.3,', ',NaN,')
            with self.subTest(mutation=mutation):
                proof = execute(em, yahoo)[3]['dailyEvidence'][0]
                self.assertEqual(proof['comparison']['status'], 'partial-evidence')
                self.assertFalse(proof['comparison']['bothCompleteCalendars'])

    def test_identity_currency_and_split_failures_keep_requests_charged_and_unresolved(self):
        for mutation in ('symbol', 'currency', 'split', 'exchange'):
            em, yahoo = bodies(); data = yahoo['chart']['result'][0]
            if mutation == 'symbol': data['meta']['symbol'] = '000637.SZ'
            elif mutation == 'currency': data['meta']['currency'] = 'USD'
            elif mutation == 'exchange': data['meta']['exchangeName'] = 'SHH'
            else: data['events']['splits'] = {'one': {'date': int(NOW.timestamp()), 'numerator': 2, 'denominator': 1}}
            with self.subTest(mutation=mutation):
                value, ledger, plan, result, calls, _ = execute(em, yahoo)
                self.assertEqual(len(calls), 2); self.assertEqual(result['dailyEvidence'][0]['comparison']['status'], 'partial-evidence')
                final = conflicts.merge_receipts(value, ledger, plan, result['receipts'], now=LATER)
                self.assertEqual(len(final['entries'][0]['digests'][0]['rounds']), 2)
                self.assertEqual(result['receipts'][0]['resolution'], 'unresolved')

    def test_adjclose_never_used_and_postquarter_bars_do_not_change_window(self):
        em, yahoo = bodies(); data = yahoo['chart']['result'][0]
        data['indicators']['adjclose'] = [{'adjclose': [1] * 65}]
        data['timestamp'].append(int(dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc).timestamp()))
        for values in data['indicators']['quote'][0].values(): values.append(999)
        proof = execute(em, yahoo)[3]['dailyEvidence'][0]
        self.assertEqual(proof['comparison']['status'], 'daily-float32-compatible')
        y = next(s for s in proof['sources'] if s['provider'] == 'yahoo')
        self.assertEqual(y['series']['ignoredPostQuarterCount'], 1); self.assertEqual(len(y['series']['rows']), 65)

    def test_changed_stored_extrema_cannot_use_current_daily_agreement_to_resolve(self):
        em, yahoo = bodies()
        em['data']['klines'] = [line.replace(',76.8,37.69,', ',77.2,37.69,') for line in em['data']['klines']]
        yahoo['chart']['result'][0]['indicators']['quote'][0]['high'] = [daily.f32(77.2)] * 65
        proof = execute(em, yahoo)[3]['dailyEvidence'][0]
        self.assertEqual(proof['comparison']['status'], 'stored-range-changed')
        self.assertTrue(proof['comparison']['bothCompleteCalendars']); self.assertEqual(proof['resolution'], 'unresolved')

    def test_failure_and_deferred_second_request_preserve_first_quote_without_extra_request(self):
        value, ledger, plan, result, calls, checkpoints = execute(fail_yahoo=True)
        self.assertEqual(len(calls), 2); self.assertEqual(result['dailyEvidence'][0]['comparison']['status'], 'partial-evidence')
        final = conflicts.merge_receipts(value, ledger, plan, result['receipts'], now=LATER)
        for rows in reversed(checkpoints): final = conflicts.merge_receipts(value, final, plan, rows, now=LATER)
        self.assertEqual(len(final['entries'][0]['digests'][0]['rounds']), 2)
        starts = iter([True, False]); result = execute(should_start=lambda: next(starts))[3]
        self.assertEqual(result['summary']['attemptedRequestCount'], 1)
        self.assertFalse(result['dailyEvidence'][0]['comparison']['bothCompleteCalendars'])

    def test_tampered_proof_fields_or_extra_payload_are_rejected(self):
        _, _, plan, result, _, _ = execute(); proof = result['dailyEvidence'][0]
        for mutation in ('hash', 'count', 'body', 'url', 'clock', 'row', 'unknown'):
            bad = copy.deepcopy(proof)
            if mutation == 'hash': bad['evidenceSha256'] = '0' * 64
            elif mutation == 'count': bad['comparison']['nonFloat32CellCount'] = 1
            elif mutation == 'body': bad['rawResponse'] = 'private sentinel'
            elif mutation == 'url': bad['sources'][0]['sourceUrl'] = 'https://invalid.example/'
            elif mutation == 'clock': bad['sources'][0]['originalFetchedAt'] = NOW.isoformat()
            elif mutation == 'row': bad['sources'][0]['series']['rows'][0]['open'] = 40
            else: bad['sources'][0]['series']['metadata']['unknown'] = True
            with self.subTest(mutation=mutation), self.assertRaises(daily.DailyEvidenceError):
                daily.normalize_evidence(bad, plan['items'][0], result['receipts'][0])

    def test_target_must_match_existing_digest_and_cannot_start_empty_ledger(self):
        value = registry(); ledger = prior(value); target = target_for(value)
        with self.assertRaisesRegex(conflicts.ConflictError, 'NOT_PREVIOUSLY_ATTEMPTED'):
            conflicts.plan_rechecks(value, target=target, now=LATER)
        for bad in ({**target, 'digest': '0' * 64}, {**target, 'key': target['key'].replace('000636', '000637')}):
            with self.assertRaisesRegex(conflicts.ConflictError, 'TARGET_STALE'):
                conflicts.plan_rechecks(value, ledger, target=bad, now=LATER)
        plan = conflicts.plan_rechecks(value, ledger, target=target, now=LATER)
        self.assertEqual(plan['items'][0]['round'], 2)
        with self.assertRaisesRegex(conflicts.ConflictError, 'NOT_PREVIOUSLY_ATTEMPTED'):
            conflicts.execute_rechecks(plan, value, checkpoint=lambda _: self.fail('no checkpoint'), fetch=lambda _: self.fail('no request'), now=LATER)

    def test_target_run_preserves_both_quarters_and_uses_two_requests_only(self):
        value = registry(); ledger = prior(value); target = target_for(value); calls = []
        q2 = conflicts.registry_from_observations(observation(), NOW)
        q2ledger = prior(q2)
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / 'source', Path(temporary) / 'out'
            for quarter, old in (('2026Q2', q2ledger), ('2026Q3', ledger)):
                path = source / quarter; path.mkdir(parents=True); (path / 'price-conflict-ledger.json').write_text(json.dumps(old))
            def fetch(url): calls.append(url); return json.dumps(bodies()[1 if 'yahoo' in url else 0]).encode()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'scan_registry', side_effect=lambda _, q, now: q2 if q == '2026Q2' else value):
                result = conflicts.run(source, output, target=target, fetch=fetch, now=LATER)
            self.assertEqual(len(calls), 2); self.assertEqual(result['summary']['attemptedKeyCount'], 1)
            self.assertEqual([q['execution']['attemptedRequestCount'] for q in result['quarters']], [0, 2])
            self.assertEqual(json.loads((output / '2026Q2/ledger.json').read_bytes()), q2ledger)
            self.assertEqual(json.loads((source / '2026Q3/price-conflict-ledger.json').read_bytes()), ledger)
            self.assertEqual(result['quarters'][1]['dailyEvidenceStatuses'], ['daily-float32-compatible'])

    def test_workflow_explicit_target_fields_reach_cli_without_interpolation(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/recheck-price-conflicts.yml').read_text()
        for name in ('quarter', 'key', 'digest'):
            self.assertIn('TARGET_' + name.upper() + ': ${{ inputs.target_' + name + ' }}', workflow)
        line = next(s.strip() for s in workflow.splitlines() if s.strip().startswith('python scripts/price_conflicts.py run '))
        target = target_for(registry()); env = {'$KEYS_PER_QUARTER': '1', **{'$TARGET_' + k.upper(): v for k, v in target.items()}}
        args = [env.get(v, v) for v in shlex.split(line)[2:]]
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'read_json', return_value={'parent': 'retained'}), \
             patch.object(conflicts, 'run', return_value={'kind': 'price-conflict-run'}) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(conflicts.main(args), 0)
        self.assertEqual(run.call_args.kwargs['target'], target)
        self.assertEqual(run.call_args.kwargs['restored_state'], {'parent': 'retained'})
        for flags in (['--target-quarter', '2026Q3'], ['--target-key', 'PRIVATE_INPUT_SENTINEL'],
                      ['--target-quarter', '2026Q3', '--target-key', target['key'], '--target-digest', '0']):
            output = io.StringIO()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'run') as run, contextlib.redirect_stdout(output):
                self.assertEqual(conflicts.main(['run', '--output', 'out', *flags]), 1)
            run.assert_not_called(); self.assertNotIn('PRIVATE_INPUT_SENTINEL', output.getvalue())


    def test_normalizer_rejects_boolean_count_and_oversized_series(self):
        _, _, plan, result, _, _ = execute(); proof = result['dailyEvidence'][0]
        bad = copy.deepcopy(proof); bad['comparison']['nonFloat32CellCount'] = False
        with self.assertRaises(daily.DailyEvidenceError): daily.normalize_evidence(bad, plan['items'][0], result['receipts'][0])
        bad = copy.deepcopy(proof); bad['sources'][0]['series']['rows'] *= 2
        with self.assertRaises(daily.DailyEvidenceError): daily.normalize_evidence(bad, plan['items'][0], result['receipts'][0])

    def test_only_supported_independent_sources_and_unadjusted_security_scope(self):
        from test_price_conflicts import registry as other_registry
        value = other_registry(); ledger = prior(value)
        plan = conflicts.plan_rechecks(value, ledger, target=target_for(value), now=LATER)
        from test_verify_price_conflicts import payload
        result = conflicts.execute_rechecks(plan, value, ledger, checkpoint=lambda _: None,
            fetch=lambda url: payload('eastmoney' if 'eastmoney' in url else 'tencent'), now=LATER)
        self.assertEqual(result['dailyEvidence'][0]['comparison']['status'], 'unsupported-source-pair')
        row = registry()['records'][0]; em, _ = bodies()
        for changes in ({'market': 'HK'}, {'currency': 'USD'}, {'priceBasis': 'adjusted'}):
            self.assertEqual(daily.extract_daily(em, 'eastmoney', {**row, **changes})['errorCode'], 'DAILY_SOURCE_UNSUPPORTED')
        bad = copy.deepcopy(row); bad['facts'][0]['observations'][0]['sourceUrl'] = bad['facts'][0]['observations'][0]['sourceUrl'].replace('fqt=0', 'fqt=1')
        calls = []
        with self.assertRaises(conflicts.ConflictError):
            conflicts.plan_rechecks({**registry(), 'records': [bad]}, prior(), target=target_for(registry()), now=LATER)
        self.assertEqual(calls, [])

    def test_artifact_restore_replays_target_round_and_ignores_diagnostic_for_budget(self):
        from test_restore_price_conflict_ledger import bundle, FakeAPI, restore_fake, DATA
        value, ledger, plan, result, _, _ = execute()
        files = bundle(); quarter = '2026Q3'
        self.assertEqual(value['dataCommit'], DATA)
        final = conflicts.merge_receipts(value, ledger, plan, result['receipts'], now=LATER)
        for name, content in (('registry', value), ('plan', plan), ('receipts', result['receipts']),
                              ('ledger', final), ('input-ledger', ledger), ('execution', result)):
            files[f'{quarter}/{name}.json'] = content
        files['provenance.json']['inputLedgerHashes'][quarter] = hashlib.sha256(conflicts.encoded(ledger)).hexdigest()
        state = restore_fake(FakeAPI(files), now=LATER)
        self.assertEqual(state['ledgers'][quarter], final)
        self.assertEqual(conflicts.plan_rechecks(value, state['ledgers'][quarter], target=target_for(value), now=LATER)['items'], [])
        # Daily evidence is diagnostic-only. Its absence cannot restore budget.
        del files[f'{quarter}/execution.json']
        self.assertEqual(restore_fake(FakeAPI(files), now=LATER)['ledgers'][quarter], final)

    def test_midrequest_interruption_retains_unknown_second_round_without_daily_claim(self):
        value = registry(); ledger = prior(value)
        plan = conflicts.plan_rechecks(value, ledger, target=target_for(value), now=LATER)
        checkpoints = []
        class Interrupted(BaseException): pass
        def stop(_): raise Interrupted()
        with self.assertRaises(Interrupted):
            conflicts.execute_rechecks(plan, value, ledger, checkpoint=lambda rows: checkpoints.append(rows), fetch=stop, now=LATER)
        self.assertEqual(len(checkpoints), 1); self.assertEqual(checkpoints[0][0]['requests'][0]['outcome'], 'unknown')
        final = conflicts.merge_receipts(value, ledger, plan, checkpoints[0], now=LATER)
        self.assertEqual(conflicts.plan_rechecks(value, final, target=target_for(value), now=LATER)['items'], [])
        self.assertEqual(final['entries'][0]['digests'][0]['rounds'][0], ledger['entries'][0]['digests'][0]['rounds'][0])

    def test_new_daily_module_is_present_in_sparse_scripts_import_path(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            shutil.copytree(root / 'scripts', path / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(root / 'config', path / 'config')
            command = "import sys;sys.path.insert(0,'scripts');import price_conflicts,verify_price_conflicts,price_daily_evidence;print(price_daily_evidence.MAX_DAYS)"
            result = subprocess.run([sys.executable, '-c', command], cwd=path, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr); self.assertEqual(result.stdout.strip(), '90')


    def test_eastmoney_explicit_market_identity_must_match_original_secid(self):
        for market in (None, 1, False, '0'):
            em, yahoo = bodies()
            if market is None: del em['data']['market']
            else: em['data']['market'] = market
            with self.subTest(market=market):
                proof = execute(em, yahoo)[3]['dailyEvidence'][0]
                self.assertEqual(proof['comparison']['status'], 'partial-evidence')
                self.assertFalse(proof['comparison']['bothCompleteCalendars'])
                source = next(s for s in proof['sources'] if s['provider'] == 'eastmoney')
                self.assertEqual(source['series']['errorCode'], 'DAILY_IDENTITY_UNVERIFIED')
                self.assertIsNone(source['series']['metadata'])


    def test_rehashed_evidence_cannot_turn_boolean_market_into_verified_identity(self):
        _, _, plan, result, _, _ = execute()
        item, receipt, proof = plan['items'][0], result['receipts'][0], result['dailyEvidence'][0]
        for value in (False, '0', 0.0, None):
            captures = {s['factId']: copy.deepcopy(s['series']) for s in proof['sources']}
            em = next(s for s in captures.values() if s['provider'] == 'eastmoney')
            em['metadata']['market'] = value
            with self.subTest(value=value), self.assertRaises(daily.DailyEvidenceError):
                daily.build_evidence(item, receipt, captures)


if __name__ == '__main__': unittest.main()
