"""Synthetic recovery checks; no source, quote or artifact network calls."""
import copy
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recover_batches as recovery

NOW = dt.datetime(2026, 10, 27, 3, tzinfo=dt.timezone.utc)
BEFORE = '2026-10-26T20:00:00Z'
AFTER = '2026-10-27T01:00:00Z'
PHASE = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}


def config(index='10'):
    return {'version': 1, 'collectorVersion': 5, 'quarter': '2026Q2', **PHASE, 'asOf': '2026-10-27',
            'maxBatchSeconds': 30, 'maxPriceSymbols': 1200, 'maxPriceSeconds': 900,
            'funds': [{'code': '000001', 'name': '合成基金', 'companyCode': None}],
            'traversal': {'phase': PHASE, 'batchId': index, 'plannedAt': '2026-10-26T22:40:06Z',
                          'qualityCodes': [], 'managerCodes': [], 'strategyCodes': [], 'enrichmentCodes': [],
                          'strategyClaimsCodes': [], 'reportLocatorCodes': []}}


def progress(count=0, last=BEFORE):
    return {'schemaVersion': 1, 'collectorVersion': 5, 'phase': PHASE,
            'funds': [{'code': '000001', 'status': 'pending', 'attempts': count, 'lastAttemptAt': last}]}


def identities():
    repo = {'id': 7, 'full_name': recovery.REPOSITORY}
    common = {'repository': repo, 'head_repository': repo, 'head_branch': 'main', 'run_attempt': 1}
    original = {**common, 'id': recovery.ORIGINAL_RUN, 'head_sha': recovery.ORIGINAL_SOURCE,
                'path': '.github/workflows/traverse.yml', 'status': 'completed', 'conclusion': 'failure', 'workflow_id': 1}
    current = {**common, 'id': 999, 'head_sha': 'a' * 40, 'path': recovery.WORKFLOW, 'status': 'in_progress',
               'event': 'workflow_dispatch', 'run_number': 1, 'workflow_id': 2}
    env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': recovery.REPOSITORY, 'GITHUB_RUN_ID': '999',
           'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40,
           'GITHUB_WORKFLOW_REF': f'{recovery.REPOSITORY}/{recovery.WORKFLOW}@refs/heads/main'}
    return original, current, env


def zip_record(files, identifier=1, name='plan'):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for key, value in files.items(): archive.writestr(key, value)
    raw = buffer.getvalue()
    metadata = {'id': identifier, 'name': name, 'expired': False, 'size_in_bytes': len(raw),
                'digest': 'sha256:' + recovery.digest(raw), 'workflow_run': {'id': recovery.ORIGINAL_RUN,
                'head_sha': recovery.ORIGINAL_SOURCE, 'repository_id': 7, 'head_repository_id': 7, 'head_branch': 'main'}}
    return metadata, raw


def plans():
    plan, campaign, configs, bindings = {}, {}, {}, {}
    for quarter, target in [('2026Q2', '2026-06-30'), ('2026Q3', '2026-09-30')]:
        catalog = {'schemaVersion': 1, 'targetPeriodEnd': target, 'quarterUniverse': {'schemaVersion': 1, 'targetPeriodEnd': target, 'historicalUniverseVerified': False},
                   'companies': [], 'funds': [{'code': f'{n:06d}'} for n in range(1, 1201)]}
        raw = recovery.encoded(catalog); name = f'catalogs/{quarter}.json'; campaign[name] = raw
        bindings[quarter] = {'path': name, 'targetPeriodEnd': target, 'sha256': recovery.digest(raw)}
    matrix = []
    for index in range(32):
        quarter = '2026Q2' if index < 16 else '2026Q3'; base = (index % 16) * 75
        value = config(str(index)); phase = PHASE if index < 16 else {'targetPeriodEnd': '2026-09-30', 'baselinePeriodEnd': '2026-06-30'}
        value.update(quarter=quarter, **phase, catalogLockVersion=1, catalogPath=bindings[quarter]['path'],
                     catalogDigest=bindings[quarter]['sha256'], requireHistoricalUniverse=True)
        value['traversal']['phase'] = phase
        value['funds'] = [{'code': f'{n:06d}', 'companyCode': None} for n in range(base + 1, base + 76)]
        configs[str(index)] = value; plan[f'batch-{index}.json'] = recovery.encoded(value)
        matrix.append({'id': str(index), 'config': f'batch-{index}.json', 'quarter': quarter})
        campaign[quarter + '.json'] = recovery.encoded(value)
    plan['matrix.json'] = recovery.encoded(matrix); plan['meta.json'] = b'{}'
    campaign['selection.json'] = recovery.encoded({'quarters': ['2026Q2', '2026Q3'], 'catalogs': bindings})
    return plan, campaign, configs


class FakeAPI:
    def __init__(self, data_commit):
        from traversal import build_status
        self.original, self.current, self.env = identities(); self.calls = []
        plan, campaign, self.configs = plans(); self.archives = {}; self.artifacts = []; self.original_bytes = {}
        def add(name, files):
            metadata, raw = zip_record(files, len(self.artifacts) + 1, name)
            self.artifacts.append(metadata); self.archives[metadata['id']] = raw
        add('plan', plan); add('campaign', campaign)
        for index in range(32):
            if str(index) in recovery.MISSING: continue
            # Deliberate whitespace and microseconds prove byte preservation.
            part = b'{ "schemaVersion":1,"sourceMode":"actions","retrievedAt":"2026-10-26T23:59:00.123456Z","funds":[] }\n'
            status = recovery.encoded(build_status(self.configs[str(index)], None, dt.datetime.now(dt.timezone.utc)))
            add(f'part-{index}', {'part.json': part, 'status.json': status})
            self.original_bytes[str(index)] = {'part.json': part, 'status.json': status}
        jobs = []
        for index in range(32):
            failed = str(index) in recovery.MISSING
            jobs.append({'name': f'batch ({index}, batch-{index}.json, 2026Q{2 if index < 16 else 3})',
                         'status': 'completed', 'conclusion': 'failure' if failed else 'success',
                         'steps': [{'name': 'Validate batch publication', 'conclusion': 'failure'}] if failed else []})
        self.responses = {'commits/main': {'sha': data_commit}, f'actions/runs/{recovery.ORIGINAL_RUN}': self.original,
                          'actions/runs/999': self.current,
                          'actions/workflows/2/runs?per_page=100': {'total_count': 1, 'workflow_runs': [self.current]},
                          'actions/workflows/1/runs?status=queued&per_page=100': {'total_count': 0, 'workflow_runs': []},
                          'actions/workflows/1/runs?status=in_progress&per_page=100': {'total_count': 0, 'workflow_runs': []},
                          f'actions/runs/{recovery.ORIGINAL_RUN}/jobs?per_page=100': {'total_count': 32, 'jobs': jobs},
                          f'actions/runs/{recovery.ORIGINAL_RUN}/artifacts?per_page=100': {'total_count': len(self.artifacts), 'artifacts': self.artifacts}}
    def get(self, suffix, binary=False):
        self.calls.append(suffix)
        if binary: return self.archives[int(suffix.split('/')[2])]
        return self.responses[suffix]


def data_checkout(root):
    root.mkdir(); folder = root / 'web/data/periods/2026Q2'; folder.mkdir(parents=True)
    rows = [{'code': f'{n:06d}', 'status': 'failed', 'attempts': 2, 'lastAttemptAt': AFTER} for n in range(1, 1201)]
    for n in (751, 976): rows[n - 1].update(status='pending', attempts=0, lastAttemptAt=BEFORE)
    (folder / 'progress.json').write_bytes(recovery.encoded({'schemaVersion': 1, 'collectorVersion': 5, 'phase': PHASE, 'funds': rows}))
    (folder / 'audit.json').write_bytes(recovery.encoded({'schemaVersion': 1, 'quarter': '2026Q2', 'phase': PHASE,
        'continuationHold': {'reason': 'BATCH_ARTIFACTS_UNVERIFIED', 'quarter': '2026Q2', 'sourceRunId': str(recovery.ORIGINAL_RUN),
                             'batchIds': ['10', '13'], 'requestAccounting': 'unverified'},
        'batchInputErrors': [{'batchId': i, 'quarter': '2026Q2', 'errorCode': 'BATCH_ARTIFACT_MISSING'} for i in recovery.MISSING]}))
    for args in [('init', '-q'), ('add', '.'), ('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.test', 'commit', '-qm', 'fixture')]:
        subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True)
    return subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()


class RecoveryTests(unittest.TestCase):
    def test_explicit_terminal_identity_latest_commit_and_first_execution(self):
        original, current, env = identities()
        def check(o=original, c=current, e=env, history=None, active=None, latest='b' * 40):
            return recovery.verify_runs(o, c, history or {'total_count': 1, 'workflow_runs': [c]},
                active or {'workflow_runs': [o], 'total_count': 1}, data_commit='b' * 40, latest_commit=latest, env=e)
        check()
        for field, value in [('status', 'in_progress'), ('head_sha', 'f' * 40), ('run_attempt', 2), ('id', 123)]:
            with self.subTest(field=field), self.assertRaises(recovery.RecoveryError): check(o={**original, field: value})
        for change in [{'run_number': 2}, {'run_number': True}, {'run_attempt': True}, {'run_attempt': 2}, {'event': 'schedule'}, {'head_repository': {'id': 8, 'full_name': 'other/repo'}}]:
            with self.subTest(change=change), self.assertRaises(recovery.RecoveryError): check(c={**current, **change})
        with self.assertRaises(recovery.RecoveryError): check(history={'total_count': 2, 'workflow_runs': [current, current]})
        with self.assertRaises(recovery.RecoveryError): check(history={'total_count': True, 'workflow_runs': [current]})
        with self.assertRaises(recovery.RecoveryError): check(active={'workflow_runs': [{**original, 'status': 'queued'}]})
        with self.assertRaises(recovery.RecoveryError): check(latest='c' * 40)

    def test_archive_bytes_hash_size_run_and_members_are_bound(self):
        original, _, _ = identities(); content = b'{ "retrievedAt" : "2026-10-26T23:00:00.123456+00:00" }\n'
        meta, raw = zip_record({'part.json': content})
        self.assertEqual(recovery.archive_members(raw, meta, original, lambda p: p == 'part.json')['part.json'], content)
        for bad in [{**meta, 'digest': 'sha256:' + '0' * 64}, {**meta, 'size_in_bytes': len(raw) + 1},
                    {**meta, 'expired': True}, {**meta, 'workflow_run': {**meta['workflow_run'], 'id': 1}}]:
            with self.assertRaises(recovery.RecoveryError): recovery.archive_members(raw, bad, original, lambda p: True)
        for name in ['../part.json', 'raw/input.json', 'status.log']:
            meta, raw = zip_record({name: b'{}'})
            with self.assertRaises(recovery.RecoveryError): recovery.archive_members(raw, meta, original, lambda p: p == 'part.json')

    def test_frozen_matrix_catalog_membership_and_quarter(self):
        plan, campaign, _ = plans(); self.assertEqual(len(recovery.validate_plan(plan, campaign)), 32)
        for mutate in ['quarter', 'digest', 'code', 'company', 'duplicate']:
            altered = dict(plan); value = recovery.parse(plan['batch-10.json'])
            if mutate == 'quarter': value['quarter'] = '2026Q3'
            if mutate == 'digest': value['catalogDigest'] = '0' * 64
            if mutate == 'code': value['funds'][0]['code'] = '999999'
            if mutate == 'company': value['funds'][0]['companyCode'] = 'wrong'
            if mutate == 'duplicate': value['funds'][0] = value['funds'][1]
            altered['batch-10.json'] = recovery.encoded(value)
            with self.subTest(mutate=mutate), self.assertRaises(recovery.RecoveryError): recovery.validate_plan(altered, campaign)

    def test_unknown_source_attempt_reserved_not_reset_by_new_date(self):
        original = config(); before = copy.deepcopy(original)
        result, skipped = recovery.recovery_config(original, progress(), NOW)
        self.assertEqual(len(result['funds']), 1); self.assertEqual(skipped, [])
        self.assertEqual(result['maxPriceSymbols'], 0); self.assertEqual(result['maxPriceSeconds'], 0)
        self.assertEqual(result['traversal']['plannedAt'], NOW.isoformat())
        self.assertEqual(result['recovery']['originalPlannedAt'], original['traversal']['plannedAt'])
        self.assertEqual(original, before)
        for count, last, expected in [(1, BEFORE, 0), (1, AFTER, 1), (2, AFTER, 0), (True, AFTER, 0)]:
            with self.subTest(count=count, last=last):
                actual, _ = recovery.recovery_config(original, progress(count, last), NOW + dt.timedelta(days=5))
                self.assertEqual(len(actual['funds']), expected)

    def test_quality_and_manager_budgets_remain_independent_and_price_only_is_skipped(self):
        original = config(); original['traversal']['qualityCodes'] = ['000001']
        prior = progress(); state = prior['funds'][0]; state['status'] = 'complete'
        state['quality'] = {'attempts': 2, 'lastAttemptAt': BEFORE, 'missingFields': ['pdf'], 'managerAttempts': 0}
        self.assertEqual(recovery.recovery_config(original, prior, NOW)[0]['funds'], [])
        state['quality']['lastAttemptAt'] = AFTER
        self.assertEqual(len(recovery.recovery_config(original, prior, NOW)[0]['funds']), 1)
        state['quality']['missingFields'] = ['prices']
        self.assertEqual(recovery.recovery_config(original, prior, NOW)[0]['funds'], [])
        original['traversal']['managerCodes'] = ['000001']; state['quality']['missingFields'] = ['managers']
        state['quality'].update(attempts=3, managerAttempts=1, managerLastAttemptAt=BEFORE)
        self.assertEqual(len(recovery.recovery_config(original, prior, NOW)[0]['funds']), 1)
        state['quality']['managerAttempts'] = 2
        self.assertEqual(recovery.recovery_config(original, prior, NOW)[0]['funds'], [])

    def test_no_lanes_or_locator_can_be_smuggled_into_report_recovery(self):
        original = config(); original['funds'][0]['reportLocator'] = {'private': 'unused'}
        result, _ = recovery.recovery_config(original, progress(), NOW)
        self.assertNotIn('reportLocator', result['funds'][0])
        for key in ['priceOnly', 'strategyClaimsOnly', 'reportLocatorOnly']:
            changed = copy.deepcopy(original); changed['funds'][0][key] = True
            result, skipped = recovery.recovery_config(changed, progress(), NOW)
            self.assertFalse(result['funds']); self.assertEqual(skipped, ['000001'])
        for change in [{'phase': {**PHASE, 'targetPeriodEnd': '2026-09-30'}}, {'collectorVersion': 4}, {'funds': []}]:
            changed = {**progress(), **change}
            if change == {'funds': []}:
                self.assertFalse(recovery.recovery_config(original, changed, NOW)[0]['funds'])
            else:
                with self.assertRaises(recovery.RecoveryError): recovery.recovery_config(original, changed, NOW)

    def test_provider_denies_market_hosts_and_redirects_before_any_transport(self):
        provider = recovery.provider_class()()
        from providers import DataError
        with patch('requests.Session.request') as request:
            for url in ['https://push2his.eastmoney.com/api/qt/stock/kline/get',
                        'https://query1.finance.yahoo.com/v8/finance/chart/000636.SZ',
                        'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get', 'http://pdf.dfcfw.com/report.pdf',
                        'https://pdf.dfcfw.com.evil.test/pdf/H2_AN12345678_1.pdf']:
                with self.assertRaises(DataError): provider.get(url)
            with self.assertRaises(DataError): provider.price_bars('000636', 'CN', '2026-07-01', '2026-09-30')
            request.assert_not_called()
            provider.session.get('https://api.fund.eastmoney.com/f10/JJGG')
            self.assertIs(request.call_args.kwargs['allow_redirects'], False)

    def test_real_report_collector_entry_has_no_quote_stage_and_checkpoints_unknown_first(self):
        original, _ = recovery.recovery_config(config(), progress(), NOW)
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); raw = root / 'raw.json'; receipt = root / 'receipt.json'
            seen = []
            def collector(item, *args, **kwargs):
                checkpoint = json.loads(receipt.read_text()); seen.append(checkpoint['sourceAttempts'][0]['outcome'])
                self.assertEqual(checkpoint['marketRequests'], 0)
                self.assertIn('provider', kwargs)
                return {'code': item['code'], 'reports': [], 'collectionAttempt': {'attempted': True}}, []
            result = recovery.collect_reports(original, raw, receipt, collector=collector, provider_factory=lambda: object())
            self.assertEqual(seen, ['unknown']); self.assertEqual(result['barsByStock'], {})
            self.assertEqual(json.loads(receipt.read_text())['sourceAttempts'][0]['outcome'], 'returned')
            self.assertGreater(recovery.instant(result['retrievedAt']), recovery.instant(config()['traversal']['plannedAt']))
            def interrupted(*args, **kwargs): raise RuntimeError('never public')
            with self.assertRaises(RuntimeError): recovery.collect_reports(original, raw, receipt, collector=interrupted, provider_factory=lambda: object())
            self.assertEqual(json.loads(receipt.read_text())['sourceAttempts'][0]['outcome'], 'unknown')

    def test_build_failure_keeps_fixed_diagnostic_without_status_and_rejects_old_output(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'recovered-10' / 'part.json'
            diagnostic = {'status': 'failed', 'errorCode': 'OUTPUT_TOO_LARGE', 'outputBytes': 25_000_001,
                          'maxOutputBytes': 25_000_000, 'serialization': 'compact-json', 'outputSha256': 'a' * 64, 'fundCount': 75}
            def runner(*args, **kwargs): return subprocess.CompletedProcess(args, 1, recovery.encoded(diagnostic), b'private error')
            self.assertFalse(recovery.build_part('unused', 'unused', output, runner=runner))
            self.assertEqual(json.loads((output.parent / 'build-diagnostic.json').read_text())['outputBytes'], 25_000_001)
            self.assertFalse((output.parent / 'status.json').exists()); self.assertFalse(output.exists())
            output.write_text('{}')
            with self.assertRaises(recovery.RecoveryError): recovery.build_part('unused', 'unused', output, runner=runner)

    def test_build_diagnostics_reject_text_and_valid_success_requires_file_hash(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'recovered-10' / 'part.json'
            for bad in [{'status': 'ok', 'responseBody': 'private'}, {'status': 'private'}, {'status': 'failed', 'errorCode': 'PRIVATE_MESSAGE'},
                        {'status': 'ok', 'outputBytes': True}, {'status': 'ok', 'serialization': 'private'}]:
                def runner(*args, **kwargs): return subprocess.CompletedProcess(args, 0, recovery.encoded(bad))
                self.assertFalse(recovery.build_part('x', 'y', output, runner=runner))
                text = (output.parent / 'build-diagnostic.json').read_text()
                self.assertNotIn('private', text.lower()); self.assertNotIn('PRIVATE_MESSAGE', text)
            def runner(*args, **kwargs): return subprocess.CompletedProcess(args, 0, b'{"status":"ok","outputBytes":2,"outputSha256":"' + b'a' * 64 + b'"}')
            with self.assertRaises(recovery.RecoveryError): recovery.build_part('x', 'y', output, runner=runner)

    def test_output_guard_disallows_raw_and_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); recovery.public_write(root / 'result.json', {'status': 'partial'})
            self.assertTrue(recovery.validate_output(root))
            (root / 'raw.json').write_text('{}')
            with self.assertRaises(recovery.RecoveryError): recovery.validate_output(root)
            (root / 'raw.json').unlink(); (root / 'link').symlink_to(root / 'result.json')
            with self.assertRaises(recovery.RecoveryError): recovery.validate_output(root)

    def test_workflow_has_no_publish_dispatch_secrets_or_continued_failed_build(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/recover-batches.yml').read_text()
        for forbidden in ['contents: write', 'actions: write', 'secrets.', 'continue-on-error', 'persist-observations', 'gh workflow run', 'scripts/campaign.py publish']:
            self.assertNotIn(forbidden, text)
        self.assertIn('group: published-records', text)
        self.assertIn("if: always() && steps.publication.outcome == 'success'", text)
        upload = text.split('      - uses: actions/upload-artifact@v4\n', 1)[1]
        self.assertIn("if: always() && steps.publication.outcome == 'success'", upload)
        self.assertIn('          path: .cache/batch-recovery/\n          include-hidden-files: true\n', upload)
        self.assertIn('          if-no-files-found: error', upload)
        self.assertIn('run: python scripts/recover_batches.py --validate-output', text)
        self.assertIn('run: python scripts/recover_batches.py --data source-data', text)

    def test_full_fake_api_preserves_thirty_blobs_builds_only_two_and_never_changes_data(self):
        from collect import collect_fund
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = root / 'data'; commit = data_checkout(data); api = FakeAPI(commit)
            before = {p.relative_to(data).as_posix(): p.read_bytes() for p in (data / 'web').rglob('*.json')}
            collected = []
            original_collect = recovery.collect_reports
            def run_collect(config, raw, receipt):
                collected.append(config['traversal']['batchId'])
                def collector(item, *args, **kwargs):
                    return {'code': item['code'], 'name': '合成产品', 'managers': [], 'shareClassCodes': [item['code']],
                        'reports': [{'fundCode': item['code'], 'periodEnd': '2026-06-30', 'publishedAt': '2026-07-20',
                          'kind': 'quarterly', 'sourceUrl': 'https://example.org/report.pdf', 'navYuan': 10000, 'coverage': 'top10',
                          'holdings': [{'stockCode': '600001', 'stockName': '合成证券', 'market': 'CN', 'currency': 'CNY',
                                        'shares': 100, 'weightPct': 5, 'marketValueYuan': 500}]}],
                        'collectionAttempt': {'attempted': True}}, []
                return original_collect(config, raw, receipt, collector=collector, provider_factory=lambda: object())
            output = root / 'out'
            with patch.object(recovery, 'collect_reports', side_effect=run_collect):
                result = recovery.run(data, output, commit, api=api, env=api.env)
            self.assertEqual(collected, ['10', '13']); self.assertEqual(len(result['preserved']), 30)
            self.assertEqual(result['marketRequests'], 0); self.assertFalse(result['productionWrite'])
            self.assertEqual(result['status'], 'partial')  # 148 source budgets are exhausted.
            for row in result['batches']:
                self.assertEqual(row['requested'], 1); self.assertEqual(len(row['deferredCodes']), 74)
                self.assertEqual(row['status'], 'recovered')
            for index, files in api.original_bytes.items():
                for name, content in files.items(): self.assertEqual((output / 'original-parts' / f'part-{index}' / name).read_bytes(), content)
            self.assertEqual(before, {p.relative_to(data).as_posix(): p.read_bytes() for p in (data / 'web').rglob('*.json')})
            self.assertTrue(recovery.validate_output(output))
            calls = len(api.calls)
            with self.assertRaises(recovery.RecoveryError): recovery.run(data, output, commit, api=api, env=api.env)
            self.assertEqual(len(api.calls), calls)

    def test_full_run_rejects_unbound_progress_before_artifact_download_or_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = root / 'data'; commit = data_checkout(data); api = FakeAPI(commit)
            (data / 'web/data/periods/2026Q2/progress.json').write_text('{}')
            with patch.object(recovery, 'collect_reports') as collector:
                with self.assertRaisesRegex(recovery.RecoveryError, 'DATA_COMMIT_MISMATCH'):
                    recovery.run(data, root / 'out', commit, api=api, env=api.env)
                collector.assert_not_called()
            self.assertFalse(any('/zip' in value for value in api.calls))

    def test_full_run_build_failure_never_calls_status_for_missing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); data = root / 'data'; commit = data_checkout(data); api = FakeAPI(commit)
            def collect(config, raw, receipt): Path(raw).write_text('{}')
            with patch.object(recovery, 'collect_reports', side_effect=collect), patch.object(recovery, 'build_part', return_value=False), patch('traversal.build_status') as status:
                result = recovery.run(data, root / 'out', commit, api=api, env=api.env)
                status.assert_not_called()
            self.assertEqual([row['status'] for row in result['batches']], ['build-failed', 'build-failed'])
            self.assertFalse(list((root / 'out').glob('recovered-*/status.json')))


if __name__ == '__main__': unittest.main()
