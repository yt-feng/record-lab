"""Synthetic artifact admission; no network, PDF, quote or model calls."""
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import admit_batch_recovery as admission
import recover_batches as recovery
import test_recover_batches as support
from traversal import atomic_json, merge_progress, load_retained_funds

ROOT = Path(__file__).resolve().parents[1]


def commit(repo):
    for args in [('add', 'web'), ('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.test', 'commit', '-qm', 'fixture')]:
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    return subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()


def collector(config, raw, receipt):
    def one(item, *args, **kwargs):
        return {'code': item['code'], 'name': '合成产品', 'managers': [], 'shareClassCodes': [item['code']],
            'reports': [{'fundCode': item['code'], 'periodEnd': '2026-06-30', 'publishedAt': '2026-07-20',
                'kind': 'quarterly', 'sourceUrl': 'https://example.org/report.pdf', 'navYuan': 10000, 'coverage': 'top10',
                'holdings': [{'stockCode': '600001', 'stockName': '合成证券', 'market': 'CN', 'currency': 'CNY',
                             'shares': 100, 'weightPct': 5, 'marketValueYuan': 500}]}],
            'collectionAttempt': {'attempted': True}}, []
    return recovery.collect_reports(config, raw, receipt, collector=one, provider_factory=lambda: object())


class AdmissionAPI:
    def __init__(self, repo, bundle, base):
        self.calls = []; self.downloads = 0
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zipped:
            for file in sorted(bundle.rglob('*.json')): zipped.writestr(file.relative_to(bundle).as_posix(), file.read_bytes())
        self.raw = buffer.getvalue(); self.artifact_id = 123; self.source = 'a' * 40
        self.binding = admission.binding_for(base, 999, self.source, self.artifact_id, recovery.digest(self.raw))
        original, current, _ = support.identities()
        current.update(path=recovery.WORKFLOW, status='completed', conclusion='success')
        self.metadata = {'id': self.artifact_id, 'name': 'batch-recovery-999-1', 'expired': False,
            'size_in_bytes': len(self.raw), 'digest': 'sha256:' + self.binding['archiveSha256'],
            'workflow_run': {'id': 999, 'head_sha': self.source, 'repository_id': 7, 'head_repository_id': 7, 'head_branch': 'main'}}
        self.responses = {'commits/main': {'sha': base}, f'actions/runs/{recovery.ORIGINAL_RUN}': original,
            f'actions/runs/{recovery.ORIGINAL_RUN}/jobs?per_page=100': {'total_count': 1, 'jobs': [
                {'name': 'publish', 'status': 'completed', 'conclusion': 'success', 'steps': [
                    {'name': 'Merge source-linked results', 'conclusion': 'success'},
                    {'name': 'Prepare compressed public site', 'conclusion': 'success'},
                    {'name': 'Persist observations and checkpoint', 'conclusion': 'success'}]}]},
            'actions/runs/999': current, 'actions/runs/999/artifacts?per_page=100': {'total_count': 1, 'artifacts': [self.metadata]}}
        self.env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': recovery.REPOSITORY, 'GITHUB_SHA': base,
            'GITHUB_WORKFLOW_REF': f'{recovery.REPOSITORY}/{admission.WORKFLOW}@refs/heads/main'}
    def get(self, endpoint): self.calls.append(endpoint); return copy.deepcopy(self.responses[endpoint])
    def download(self, artifact_id, path):
        if artifact_id != self.artifact_id: raise AssertionError()
        self.downloads += 1; Path(path).write_bytes(self.raw)


def setup(root):
    repo = root / 'repo'; repo.mkdir()
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True, capture_output=True)
    quarter = repo / 'web/data/periods/2026Q2'; quarter.mkdir(parents=True)
    plan, campaign, configs = support.plans()
    catalog = recovery.parse(campaign['catalogs/2026Q2.json'])
    atomic_json(quarter / 'catalog.json', catalog)
    q3 = repo / 'web/data/periods/2026Q3'; q3.mkdir(); (q3 / 'audit.json').write_bytes(b'{ "quarter":"2026Q3","unchanged":true }\n')
    parts = root / 'old-parts'; parts.mkdir()
    raw = {'asOf': '2026-10-26', 'retrievedAt': '2026-10-26T19:00:00Z', 'errors': [], 'funds': [{
        'code': '000001', 'name': '合成旧产品', 'managers': [], 'reports': [{
            'fundCode': '000001', 'periodEnd': '2026-06-30', 'publishedAt': '2026-07-20', 'kind': 'quarterly',
            'coverage': 'top10', 'sourceUrl': 'https://example.org/retained.pdf', 'navYuan': 20000,
            'holdings': [{'stockCode': '600002', 'stockName': '旧证券', 'market': 'CN', 'currency': 'CNY',
                         'shares': 50, 'weightPct': 3, 'marketValueYuan': 600}]}]}],
        'barsByStock': {'CN:600002': {'currency': 'CNY', 'priceBasis': 'unadjusted', 'sourceUrl': 'https://example.org/prices',
            'rows': [{'date': '2026-04-10', 'low': 8, 'high': 12, 'close': 10}]}}}
    raw_file = root / 'raw.json'; raw_file.write_bytes(recovery.encoded(raw))
    config = configs['0']; config_path = root / 'config.json'; atomic_json(config_path, config)
    subprocess.run(['node', str(ROOT / 'scripts/build-data.js'), '--input', str(raw_file), '--output', str(parts / 'part.json'),
                    '--config', str(config_path)], check=True, capture_output=True)
    subprocess.run(['node', str(ROOT / 'scripts/merge-parts.js'), '--parts', str(parts), '--data', str(quarter),
                    '--catalog', str(quarter / 'catalog.json'), '--config', str(config_path)], check=True, capture_output=True)
    state = merge_progress(catalog, None, config, retained=load_retained_funds(quarter))
    for row in state['funds']:
        if row['code'] == '000001': continue
        row.update(status='failed', attempts=2, totalAttempts=2, lastAttemptAt='2026-10-26T23:00:00Z')
    for row in state['funds']:
        if row['code'] in {'000751', '000976'}:
            row.update(status='failed', attempts=1, totalAttempts=1)
    atomic_json(quarter / 'progress.json', state)
    hold = {'reason': 'BATCH_ARTIFACTS_UNVERIFIED', 'quarter': '2026Q2', 'sourceRunId': str(recovery.ORIGINAL_RUN),
            'batchIds': ['10', '13'], 'requestAccounting': 'unverified'}
    atomic_json(quarter / 'audit.json', {'schemaVersion': 1, 'quarter': '2026Q2', 'phase': support.PHASE,
        'continuationHold': hold, 'batchInputErrors': [{'batchId': code, 'quarter': '2026Q2', 'errorCode': 'BATCH_ARTIFACT_MISSING'} for code in recovery.MISSING]})
    base = commit(repo); fake = support.FakeAPI(base); bundle = root / 'bundle'
    # Route only the test collector; use the real recovery checkpoint/build/status.
    original_collect = recovery.collect_reports
    def collect_wrapper(config, raw, receipt):
        def one(item, *args, **kwargs):
            return {'code': item['code'], 'name': '合成新产品', 'managers': [], 'reports': [{
                'fundCode': item['code'], 'periodEnd': '2026-06-30', 'publishedAt': '2026-07-20', 'kind': 'quarterly',
                'coverage': 'top10', 'sourceUrl': 'https://example.org/report.pdf', 'navYuan': 10000,
                'holdings': [{'stockCode': '600001', 'stockName': '新证券', 'market': 'CN', 'currency': 'CNY',
                             'shares': 100, 'weightPct': 5, 'marketValueYuan': 500}]}], 'collectionAttempt': {'attempted': True}}, []
        return original_collect(config, raw, receipt, collector=one, provider_factory=lambda: object())
    with patch.object(recovery, 'collect_reports', side_effect=collect_wrapper): recovery.run(repo, bundle, base, api=fake, env=fake.env)
    api = AdmissionAPI(repo, bundle, base)
    return repo, bundle, api


class AdmissionTests(unittest.TestCase):
    def test_binding_is_stable_and_strict(self):
        value = admission.binding_for('a'*40, 1, 'b'*40, 2, 'c'*64)
        self.assertEqual(admission.digest(value), admission.digest(copy.deepcopy(value)))
        for values in [('a'*39,1,'b'*40,2,'c'*64), ('a'*40,True,'b'*40,2,'c'*64), ('a'*40,1,'b'*40,2,'x'*64)]:
            with self.assertRaises(admission.AdmissionError): admission.binding_for(*values)

    def test_real_build_merge_progress_admission_preserves_old_data_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, bundle, api = setup(Path(directory)); quarter = repo / 'web/data/periods/2026Q2'
            protected = admission.protected_files(repo / 'web/data'); old = admission.fund_protection(quarter)
            progress = json.loads((quarter / 'progress.json').read_text()); hold = json.loads((quarter / 'audit.json').read_text())['continuationHold']
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
                result = admission.run(repo, api.binding, api=api, env=api.env)
            self.assertEqual(result['status'], 'staged'); self.assertEqual(result['persistence'], 'pending'); self.assertFalse(result['continueRun'])
            self.assertEqual(result['sourceRequests'], 0); self.assertEqual(result['marketRequests'], 0)
            self.assertEqual(result['requestedFundCount'], 2); self.assertEqual(api.downloads, 1)
            self.assertEqual(admission.protected_files(repo / 'web/data'), protected)
            self.assertEqual(admission.fund_protection(quarter)['000001'], old['000001'])
            self.assertEqual(json.loads((quarter / 'audit.json').read_text())['continuationHold'], hold)
            after = json.loads((quarter / 'progress.json').read_text())
            for code in ('000751', '000976'):
                before_row = next(row for row in progress['funds'] if row['code'] == code)
                after_row = next(row for row in after['funds'] if row['code'] == code)
                self.assertEqual(after_row['totalAttempts'], before_row['totalAttempts'] + 1)
                self.assertEqual(after_row['attempts'], before_row['attempts'])
            api.env['GITHUB_SHA'] = commit(repo); downloads = api.downloads; bytes_before = (quarter / 'progress.json').read_bytes()
            self.assertEqual(admission.run(repo, api.binding, api=api, env=api.env)['status'], 'already-admitted')
            self.assertEqual(api.downloads, downloads); self.assertEqual((quarter / 'progress.json').read_bytes(), bytes_before)
            bad = {**api.binding, 'archiveSha256': '0'*64}
            with self.assertRaisesRegex(admission.AdmissionError, 'REPLAY_CONFLICT'): admission.run(repo, bad, api=api, env=api.env)

    def test_original_publisher_persist_is_a_hard_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, bundle, api = setup(Path(directory))
            api.responses[f'actions/runs/{recovery.ORIGINAL_RUN}/jobs?per_page=100']['jobs'][0]['steps'][-1]['conclusion'] = 'failure'
            with self.assertRaisesRegex(admission.AdmissionError, 'ORIGINAL_NOT_PERSISTED'): admission.run(repo, api.binding, api=api, env=api.env)
            self.assertEqual(api.downloads, 0)

    def test_only_terminal_continuation_failure_is_compatible_with_confirmed_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,bundle,api=setup(Path(directory))
            publisher=api.responses[f'actions/runs/{recovery.ORIGINAL_RUN}/jobs?per_page=100']['jobs'][0]
            publisher['conclusion']='failure'
            publisher['steps'].append({'name':'Continue unfinished traversal','conclusion':'failure'})
            admission.verified_metadata(api,api.binding)
            for name in ('Merge source-linked results','Persist observations and checkpoint','Prepare compressed public site'):
                step=next(row for row in publisher['steps'] if row['name']==name)
                step['conclusion']='failure'
                with self.subTest(name=name),self.assertRaisesRegex(admission.AdmissionError,'ORIGINAL_NOT_PERSISTED'):
                    admission.verified_metadata(api,api.binding)
                step['conclusion']='success'
            publisher['steps'][-1]['conclusion']='cancelled'
            with self.assertRaisesRegex(admission.AdmissionError,'ORIGINAL_NOT_PERSISTED'): admission.verified_metadata(api,api.binding)

    def test_run_artifact_identity_and_preserved_batch_inventory_are_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,bundle,api=setup(Path(directory)); original=copy.deepcopy(api.responses)
            cases=[(f'actions/runs/{recovery.ORIGINAL_RUN}','id',1),
                   ('actions/runs/999','head_sha','b'*40), ('actions/runs/999','run_attempt',2),
                   ('actions/runs/999','run_number',2), ('actions/runs/999','path','.github/workflows/other.yml')]
            for endpoint,key,value in cases:
                api.responses=copy.deepcopy(original);api.responses[endpoint][key]=value
                with self.subTest(key=key),self.assertRaises(admission.AdmissionError): admission.verified_metadata(api,api.binding)
            api.responses=copy.deepcopy(original)
            api.responses['actions/runs/999/artifacts?per_page=100']['artifacts'][0]['workflow_run']['head_sha']='b'*40
            with self.assertRaisesRegex(admission.AdmissionError,'ARTIFACT_INVALID'): admission.verified_metadata(api,api.binding)
            manifest=admission.read_json(bundle/'result.json');manifest['preserved'].pop();atomic_json(bundle/'result.json',manifest)
            with self.assertRaisesRegex(admission.AdmissionError,'MANIFEST_INVALID'): admission.verify_bundle(bundle,api.binding,repo)

    def test_malformed_committed_ledger_cannot_claim_idempotent_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            minimal={'schemaVersion':1,'quarter':'2026Q2','entries':[{'binding':admission.binding_for('a'*40,1,'b'*40,2,'c'*64)}]}
            minimal['entries'][0]['admissionDigest']=admission.digest(minimal['entries'][0]['binding'])
            atomic_json(root/admission.LEDGER,minimal)
            with self.assertRaisesRegex(admission.AdmissionError,'LEDGER_INVALID'): admission.load_ledger(root)
            for raw in (b'null',b'not-json',b'{"schemaVersion":true,"quarter":"2026Q2","entries":[]}',
                        b'{"schemaVersion":1,"quarter":"2026Q2","entries":[],"entries":[]}',
                        b'{"schemaVersion":1,"quarter":"2026Q2","entries":NaN}'):
                (root/admission.LEDGER).write_bytes(raw)
                with self.subTest(raw=raw),self.assertRaisesRegex(admission.AdmissionError,'LEDGER_INVALID'): admission.load_ledger(root)
            (root/admission.LEDGER).unlink()
            self.assertEqual(admission.load_ledger(root)['entries'],[])
            (root/admission.LEDGER).symlink_to(root/'absent')
            with self.assertRaisesRegex(admission.AdmissionError,'LEDGER_INVALID'): admission.load_ledger(root)

    def test_source_only_advancement_is_allowed_but_changed_data_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, bundle, api = setup(Path(directory))
            (repo / 'source.txt').write_text('source-only')
            subprocess.run(['git','-C',str(repo),'add','source.txt'], check=True, capture_output=True)
            subprocess.run(['git','-C',str(repo),'-c','user.name=Synthetic','-c','user.email=synthetic@example.test','commit','-qm','source'], check=True, capture_output=True)
            api.env['GITHUB_SHA'] = admission.git(repo, 'rev-parse', 'HEAD')
            admission.check_base(repo, api.binding, api.env)
            (repo / 'web/data/periods/2026Q3/audit.json').write_text('{}')
            api.env['GITHUB_SHA'] = commit(repo)
            with self.assertRaisesRegex(admission.AdmissionError, 'DATA_CHANGED'): admission.check_base(repo, api.binding, api.env)

    def test_archive_hash_path_and_duplicate_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); archive=root/'archive.zip'; output=root/'out'; output.mkdir()
            for names in [['../part.json'], ['recovered-10/part.json','recovered-10/part.json'], ['raw/input.json']]:
                with warnings.catch_warnings(), zipfile.ZipFile(archive,'w') as z:
                    warnings.simplefilter('ignore', UserWarning)
                    for name in names: z.writestr(name,'{}')
                raw=archive.read_bytes(); binding=admission.binding_for('a'*40,1,'b'*40,2,recovery.digest(raw))
                with self.assertRaises(admission.AdmissionError): admission.extract_archive(archive,output,{'size_in_bytes':len(raw)},binding)
            with self.assertRaisesRegex(admission.AdmissionError,'DIGEST_MISMATCH'):
                admission.extract_archive(archive,output,{'size_in_bytes':len(raw)},{**binding,'archiveSha256':'0'*64})

    def test_quote_and_market_counter_protections_are_exact(self):
        before={'000001':{'sourceHash':'s','prices':{'CN|600001|CNY':'p'},'priceUpdatedAt':'2026-10-26T19:00:00.123456Z'}}
        for mutate in ('price','clock','source','removed'):
            after=copy.deepcopy(before)
            if mutate=='price': after['000001']['prices']['CN|600001|CNY']='changed'
            if mutate=='clock': after['000001']['priceUpdatedAt']='2026-10-26T19:00:00.123457Z'
            if mutate=='source': after['000001']['sourceHash']='changed'
            if mutate=='removed': after={}
            with self.assertRaises(admission.AdmissionError): admission.protect_funds(before,after,set())
        old={'funds':[{'code':'000001','attempts':1,'totalAttempts':1,'enrichment':{'attempts':2,'totalAttempts':3,'lastAttemptAt':'old','retry':{'cycleDate':'2026-10-26','scheduledCycles':0,'nextRetryAt':None}}}]}
        for key in ('attempts','totalAttempts','lastAttemptAt','retry'):
            after=copy.deepcopy(old); after['funds'][0]['enrichment'][key]=0
            with self.assertRaisesRegex(admission.AdmissionError,'MARKET_LEDGER'): admission.protect_market_budgets(old,after,{'000001'})
        after=copy.deepcopy(old); after['funds'][0]['totalAttempts']=2
        with self.assertRaisesRegex(admission.AdmissionError,'SOURCE_BUDGET'): admission.protect_market_budgets(old,after,set())
        admission.protect_market_budgets(old,after,{'000001'})
        not_applicable=copy.deepcopy(old)
        not_applicable['funds'][0]['status']='not-applicable'
        not_applicable['funds'][0]['enrichment']['retry']={'cycleDate':'2026-10-27','scheduledCycles':0,'nextRetryAt':None}
        admission.protect_market_budgets(old,not_applicable,set())
        changed_schedule=copy.deepcopy(not_applicable)
        changed_schedule['funds'][0]['enrichment']['retry']['scheduledCycles']=1
        with self.assertRaisesRegex(admission.AdmissionError,'MARKET_LEDGER'):
            admission.protect_market_budgets(old,changed_schedule,set())

    def test_fund_source_hash_allows_only_requested_portfolio_alias_sync(self):
        before = {
            '000744': {'sourceHash': 'representative-old', 'prices': {}, 'priceUpdatedAt': None,
                       'sourcePortfolioCode': '000744', 'shareClassCodes': ['000744', '000745']},
            '000745': {'sourceHash': 'alias-old', 'prices': {}, 'priceUpdatedAt': None,
                       'sourcePortfolioCode': '000744', 'shareClassCodes': ['000744', '000745']},
            '000746': {'sourceHash': 'unrelated-old', 'prices': {}, 'priceUpdatedAt': None,
                       'sourcePortfolioCode': '000746', 'shareClassCodes': ['000746', '000747']},
        }
        after = copy.deepcopy(before)
        after['000745']['sourceHash'] = 'alias-normalized'
        admission.protect_funds(before, after, {'000744'})
        after['000746']['sourceHash'] = 'unrelated-changed'
        with self.assertRaisesRegex(admission.AdmissionError, 'UNRELATED_SOURCE_CHANGED'):
            admission.protect_funds(before, after, {'000744'})
        broken = copy.deepcopy(before)
        broken['000745']['sourceHash'] = 'alias-normalized'
        broken['000745']['sourcePortfolioCode'] = '000745'
        with self.assertRaisesRegex(admission.AdmissionError, 'UNRELATED_SOURCE_CHANGED'):
            admission.protect_funds(before, broken, {'000744'})

    def test_fund_protection_ignores_only_reconciliation_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            quarter = Path(directory) / '2026Q2'; (quarter / 'companies').mkdir(parents=True)
            (quarter / 'latest.json').write_text(json.dumps({'companyFiles': [{'path': './data/companies/co.json'}]}))
            fund = {'code': '000001', 'name': '合成基金', 'status': 'ok', 'companyId': 'co',
                    'companyMapping': 'catalog', 'portfolioDeduplicated': False,
                    'portfolioRepresentativeCode': '000001', 'aliases': [],
                    'rows': [{'stockCode': '600001', 'market': 'CN', 'currency': 'CNY',
                              'priceRange': {'status': 'observed', 'low': 1, 'high': 2}}]}
            envelope = {'funds': [fund]}
            (quarter / 'companies' / 'co.json').write_text(json.dumps(envelope))
            before = admission.fund_protection(quarter)
            fund['status'] = 'no-equities'; fund['companyMapping'] = 'catalog'; fund['portfolioDeduplicated'] = True
            fund['portfolioRepresentativeCode'] = '000001'; fund['aliases'] = [{'code': '000001', 'name': '合成基金'}]
            (quarter / 'companies' / 'co.json').write_text(json.dumps(envelope))
            after = admission.fund_protection(quarter)
            self.assertEqual(before['000001']['sourceHash'], after['000001']['sourceHash'])

    def test_quality_budget_schema_backfill_is_not_a_new_attempt(self):
        old={'funds':[{'code':'000001','quality':{'attempts':1,'totalAttempts':1,
            'managerAttempts':0,'strategyAttempts':0}}]}
        after=copy.deepcopy(old)
        after['funds'][0]['quality'].update({'managerTotalAttempts':0,'strategyTotalAttempts':0})
        admission.protect_market_budgets(old,after,set())
        after['funds'][0]['quality']['managerTotalAttempts']=1
        with self.assertRaisesRegex(admission.AdmissionError,'SOURCE_BUDGET'):
            admission.protect_market_budgets(old,after,set())
        admission.protect_market_budgets(old,after,{'000001'})

    def test_quality_budget_allows_shared_portfolio_alias_sync(self):
        def quality(total, attempts=1):
            return {'attempts': attempts, 'totalAttempts': total, 'managerAttempts': 0,
                    'managerTotalAttempts': 0, 'strategyAttempts': 0, 'strategyTotalAttempts': 0}
        old={'funds':[
            {'code':'000744','representativeCode':'000744','quality':quality(4)},
            {'code':'000745','representativeCode':'000744','quality':quality(3)},
        ]}
        after=copy.deepcopy(old)
        after['funds'][0]['quality']=quality(5, attempts=2)
        after['funds'][1]['quality']=quality(5, attempts=2)
        admission.protect_market_budgets(old, after, {'000744'})
        admission.protect_market_budgets(old, after, {'000744', '000745'})
        after['funds'][0]['quality']=quality(6, attempts=3)
        after['funds'][1]['quality']=quality(6, attempts=3)
        admission.protect_market_budgets(old, after, {'000744', '000745'})
        after['funds'][0]['quality']=quality(7, attempts=3)
        with self.assertRaisesRegex(admission.AdmissionError,'SOURCE_BUDGET'):
            admission.protect_market_budgets(old, after, {'000744', '000745'})
        changed=copy.deepcopy(after); changed['funds'][1]['quality']['totalAttempts']=6
        with self.assertRaisesRegex(admission.AdmissionError,'SOURCE_BUDGET'):
            admission.protect_market_budgets(old, changed, {'000744'})
        unstarted=copy.deepcopy(after)
        with self.assertRaisesRegex(admission.AdmissionError,'SOURCE_BUDGET'):
            admission.protect_market_budgets(old, unstarted, set())

    def test_no_original_parts_or_missing_status_reconciliation_enters_admission(self):
        source=(ROOT/'scripts/admit_batch_recovery.py').read_text()
        self.assertIn("if row.filename.startswith('original-parts/'): continue",source)
        self.assertNotIn('build_status(',source)
        self.assertNotIn('complete-status.py',source)
        self.assertNotIn('collect_fund(',source)
        self.assertNotIn('requests.',source)

    def test_bundle_rejects_price_injection_outside_scope_and_unbound_source_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,bundle,api=setup(Path(directory)); folder=bundle/'recovered-10'
            original={name:(folder/name).read_bytes() for name in ('part.json','source-attempts.json','config.json')}
            for kind in ('price','fund-clock','outside-code','receipt-clock','receipt-unknown','configuration'):
                for name,raw in original.items(): (folder/name).write_bytes(raw)
                if kind.startswith('receipt'):
                    receipt=json.loads(original['source-attempts.json'])
                    if kind=='receipt-clock': receipt['sourceAttempts'][0]['attemptedAt']='2099-01-01T00:00:00Z'
                    else: receipt['sourceAttempts'][0]['outcome']='unknown'
                    atomic_json(folder/'source-attempts.json',receipt)
                elif kind=='configuration':
                    config=json.loads(original['config.json']);config['maxPriceSymbols']=1;atomic_json(folder/'config.json',config)
                else:
                    part=json.loads(original['part.json']);fund=part['funds'][0]
                    if kind=='price': fund['rows'][0]['priceRange'].update(status='observed',low=8,high=12,fetchedAt=part['retrievedAt'])
                    if kind=='fund-clock': fund['priceUpdatedAt']=part['retrievedAt']
                    if kind=='outside-code': fund['code']='999999'
                    atomic_json(folder/'part.json',part)
                with self.subTest(kind=kind),self.assertRaises(admission.AdmissionError): admission.verify_bundle(bundle,api.binding,repo)

    def test_committed_data_and_runtime_are_required_even_for_idempotent_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            repo,bundle,api=setup(Path(directory));quarter=repo/'web/data/periods/2026Q2'
            atomic_json(quarter/admission.LEDGER,{'schemaVersion':1,'quarter':'2026Q2','entries':[{
                'binding':api.binding,'admissionDigest':admission.digest(api.binding)}]})
            with self.assertRaisesRegex(admission.AdmissionError,'DIRTY_DATA'): admission.run(repo,api.binding,api=api,env=api.env)
            (quarter/admission.LEDGER).unlink()
            with self.assertRaisesRegex(admission.AdmissionError,'REQUIRES_ACTIONS'):
                admission.run(repo,api.binding,api=api,env={**api.env,'GITHUB_ACTIONS':'false'})

    def test_workflow_has_guarded_persistence_no_collection_and_post_prepare_tree_check(self):
        text=(ROOT/'.github/workflows/admit-batch-recovery.yml').read_text()
        for forbidden in ('secrets.','collect.py','campaign.py publish','gh workflow run','continue-on-error','actions: write'):
            self.assertNotIn(forbidden,text)
        for required in ('group: published-records','scripts/persist-observations.py','--verify-protected-output','include-hidden-files: true','if-no-files-found: error'):
            self.assertIn(required,text)
        self.assertLess(text.index('scripts/publication_guard.py'),text.index('scripts/persist-observations.py'))
        self.assertLess(text.index('scripts/prepare-site.py'),text.index('--verify-protected-output'))
        self.assertLess(text.index('--verify-protected-output'),text.index('actions/upload-pages-artifact'))


if __name__ == '__main__': unittest.main()
