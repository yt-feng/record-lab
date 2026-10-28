"""Synthetic journals plus a tiny real publish, without provider requests."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import campaign
import campaign_accounting as accounting
import request_receipts as receipts
from traversal import atomic_json, read_json
from test_campaign_history import workspace, current

ENV = {'GITHUB_RUN_ID': '100', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40,
       'RECEIPT_BATCH_ID': '0'}
PHASE = {'targetPeriodEnd': '2026-09-30', 'baselinePeriodEnd': '2026-06-30'}


def config():
    return {'quarter': '2026Q3', **PHASE, 'funds': [{'code': '000001'}],
            'traversal': {'phase': PHASE, 'batchId': '0', 'plannedAt': '2026-10-26T00:00:00Z'}}


def make_receipt(root, cfg, raw, env=ENV, outcome='returned'):
    journal = receipts.RequestJournal(root / 'private', cfg, raw, env)
    if outcome not in {'empty', 'before-call'}:
        observer = journal.observer('CN:600001', ('600001', 'CN', '2026-07-01', '2026-09-30'))
        token = observer.begin('https://push2his.eastmoney.com/api/qt/stock/kline/get')
        if outcome != 'unknown': observer.finish(token, outcome)
    if outcome not in {'unknown', 'before-call'}: journal.close(True)
    return receipts.validate(root / 'private', cfg, raw, env)


class CampaignAccountingTests(unittest.TestCase):
    def fixture(self, root, outcome='returned', env=ENV):
        cfg = config(); plan = root / 'plan'; plan.mkdir()
        atomic_json(plan / 'batch-0.json', cfg)
        raw = (plan / 'batch-0.json').read_bytes()
        value = make_receipt(root, cfg, raw, env, outcome)
        path = root / 'receipts' / f"request-accounting-{env['GITHUB_RUN_ID']}-1-0" / 'receipts.json'
        path.parent.mkdir(parents=True); path.write_bytes(receipts.encoded(value))
        matrix = [{'id': '0', 'config': 'batch-0.json', 'quarter': '2026Q3'}]
        return cfg, raw, value, path, matrix

    def test_complete_receipt_is_bound_and_persisted_byte_exact_idempotently(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); cfg, raw, value, path, matrix = self.fixture(root)
            records = accounting.collect_batch_accounting(matrix, root/'plan', root/'receipts', ENV)['2026Q3']
            quarter = root/'quarter'
            summary = accounting.persist_accounting(quarter, '2026Q3', records)
            self.assertTrue(summary['continuationAllowed'])
            entry = accounting.load_index(quarter, '2026Q3')['entries'][0]
            self.assertEqual(entry['counts']['returned'], 1)
            self.assertEqual((quarter/entry['receiptPath']).read_bytes(), path.read_bytes())
            self.assertEqual(entry['receiptSha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            before = (quarter/accounting.INDEX).read_bytes()
            again = copy.deepcopy(records); again[0][0]['observedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
            accounting.persist_accounting(quarter, '2026Q3', again)
            self.assertEqual((quarter/accounting.INDEX).read_bytes(), before)

    def test_unknown_missing_and_invalid_do_not_become_zero_or_disappear_later(self):
        for outcome in ('unknown', 'missing', 'invalid'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                root=Path(folder); cfg,raw,value,path,matrix=self.fixture(root, 'unknown' if outcome=='unknown' else 'empty')
                if outcome == 'missing': path.unlink()
                if outcome == 'invalid': path.write_text('{"raw":"PRIVATE_SENTINEL"}')
                rows=accounting.collect_batch_accounting(matrix,root/'plan',root/'receipts',ENV)['2026Q3']
                summary=accounting.persist_accounting(root/'quarter','2026Q3',rows)
                self.assertFalse(summary['continuationAllowed'])
                entry=accounting.load_index(root/'quarter','2026Q3')['entries'][0]
                if outcome=='unknown':
                    self.assertEqual(entry['counts']['unknown'],1)
                    kept=read_json(root/'quarter'/entry['receiptPath'])
                    self.assertEqual(kept['records'],value['records'])
                else:
                    self.assertIsNone(entry['counts']); self.assertIsNone(entry['receiptPath'])
                later=copy.deepcopy(rows)
                later[0][0].update(runId='101', status='missing', errorCode='MARKET_RECEIPT_MISSING',
                    counts=None,receiptPath=None,receiptSha256=None)
                later=[(later[0][0],None)]
                summary=accounting.persist_accounting(root/'quarter','2026Q3',later)
                self.assertEqual(summary['unresolvedBatchCount'],2)
                self.assertNotIn('PRIVATE_SENTINEL',(root/'quarter'/accounting.INDEX).read_text())

    def test_wrong_run_attempt_source_phase_config_counts_and_duplicate_rows_reject(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);cfg,raw,value,path,matrix=self.fixture(root)
            for kind in ('run','attempt','source','phase','config','counts','duplicate','extra'):
                bad=copy.deepcopy(value)
                if kind=='run':bad['header']['binding']['runId']='999'
                elif kind=='attempt':bad['header']['binding']['runAttempt']=2
                elif kind=='source':bad['header']['binding']['sourceCommit']='b'*40
                elif kind=='phase':bad['header']['binding']['phase']['targetPeriodEnd']='2026-06-30'
                elif kind=='config':bad['header']['binding']['configSha256']='0'*64
                elif kind=='counts':bad['counts']['returned']=0
                elif kind=='duplicate':bad['records'].append(bad['records'][0])
                else:bad['PRIVATE_SENTINEL']='PRIVATE_SENTINEL'
                path.write_bytes(receipts.encoded(bad))
                rows=accounting.collect_batch_accounting(matrix,root/'plan',root/'receipts',ENV)['2026Q3']
                with self.subTest(kind=kind):
                    self.assertEqual(rows[0][0]['status'],'invalid')
                    self.assertIsNone(rows[0][0]['counts']);self.assertIsNone(rows[0][1])

    def test_a_completed_new_run_cannot_clear_an_old_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);first=root/'first';first.mkdir()
            *_,matrix=self.fixture(first,'unknown')
            rows=accounting.collect_batch_accounting(matrix,first/'plan',first/'receipts',ENV)['2026Q3']
            accounting.persist_accounting(root/'quarter','2026Q3',rows)
            second=root/'second';second.mkdir();env={**ENV,'GITHUB_RUN_ID':'101'}
            self.fixture(second,'returned',env)
            rows=accounting.collect_batch_accounting(matrix,second/'plan',second/'receipts',env)['2026Q3']
            summary=accounting.persist_accounting(root/'quarter','2026Q3',rows)
            self.assertFalse(summary['continuationAllowed']);self.assertEqual(summary['recordedBatchCount'],2)
            self.assertEqual(summary['unresolvedBatches'][0]['runId'],'100')

    def test_historical_complete_or_incomplete_index_requires_original_receipt_bytes(self):
        for outcome in ('returned','unknown'):
            for damage in ('missing','changed','symlink'):
                with self.subTest(outcome=outcome,damage=damage), tempfile.TemporaryDirectory() as folder:
                    root=Path(folder);*_,matrix=self.fixture(root,outcome)
                    rows=accounting.collect_batch_accounting(matrix,root/'plan',root/'receipts',ENV)['2026Q3']
                    quarter=root/'quarter';accounting.persist_accounting(quarter,'2026Q3',rows)
                    entry=accounting.load_index(quarter,'2026Q3')['entries'][0]
                    proof=quarter/entry['receiptPath']
                    if damage=='changed': proof.write_bytes(proof.read_bytes()+b' ')
                    else:
                        proof.unlink()
                        if damage=='symlink':proof.symlink_to(root/'absent')
                    with self.assertRaisesRegex(ValueError,'MARKET_ACCOUNTING_PROOF_'):
                        accounting.load_index(quarter,'2026Q3')

    def test_interruption_before_call_and_after_reservation_remains_loadable(self):
        for outcome,logical,calls in [('before-call',0,0),('unknown',1,1)]:
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);*_,matrix=self.fixture(root,outcome)
                rows=accounting.collect_batch_accounting(matrix,root/'plan',root/'receipts',ENV)['2026Q3']
                summary=accounting.persist_accounting(root/'quarter','2026Q3',rows)
                self.assertFalse(summary['continuationAllowed'])
                for _ in range(2):
                    entry=accounting.load_index(root/'quarter','2026Q3')['entries'][0]
                    self.assertEqual(entry['status'],'incomplete')
                    self.assertEqual(entry['counts']['logicalReservations'],logical)
                    self.assertEqual(entry['counts']['sessionGetReservations'],calls)

    def test_logical_without_first_call_is_invalid_in_current_atomic_journal_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);cfg,raw,value,path,matrix=self.fixture(root,'unknown')
            # begin() writes the logical identity and its first call in one
            # atomic operation; the original validator requires >= 1 call.
            value['records'][0]['calls']=[]
            value['counts'].update(sessionGetReservations=0,unknown=0)
            path.write_bytes(receipts.encoded(value))
            rows=accounting.collect_batch_accounting(matrix,root/'plan',root/'receipts',ENV)['2026Q3']
            accounting.persist_accounting(root/'quarter','2026Q3',rows)
            entry=accounting.load_index(root/'quarter','2026Q3')['entries'][0]
            self.assertEqual(entry['status'],'invalid');self.assertIsNone(entry['counts'])

    def test_new_plan_requires_accounting_without_workflow_environment_flag(self):
        with workspace(),patch.dict(os.environ,{'CAMPAIGN_MARKET_ACCOUNTING':''}):
            campaign.plan('2026Q3','2026-10-29',shards=1,batch_size=1,refresh=False,historical_overlays={})
            self.assertEqual(read_json('.cache/campaign/selection.json')['marketAccountingVersion'],1)

    def test_real_source_publish_survives_missing_receipt_but_stops_chain(self):
        with workspace(), patch.dict(os.environ,{**ENV,'CAMPAIGN_MARKET_ACCOUNTING':'required'}):
            catalog=current();catalog['funds'].append({'code':'000002','companyCode':'co1','name':'合成乙'})
            catalog['fundCount']=2;catalog['coverage']['fundCount']=2;catalog['companies'][0]['fundCount']=2
            atomic_json('web/data/catalog.json',catalog)
            campaign.plan('2026Q3','2026-10-29',shards=1,batch_size=1,refresh=False,historical_overlays={})
            selection=read_json('.cache/campaign/selection.json')
            self.assertEqual(selection.pop('marketAccountingVersion'),1)
            # A retained pre-rollout plan has no marker and no receipts. The
            # current publisher still preserves source data, but cannot claim
            # the legacy market requests were zero or permit automatic replay.
            atomic_json('.cache/campaign/selection.json',selection)
            selected=read_json('.cache/plan/batch-0.json')['funds'][0]['code']
            stamp=dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_json('raw/input.json',{'asOf':'2026-10-29','retrievedAt':stamp,'funds':[{
                'code':selected,'name':'合成已验证产品','reports':[],'managers':[],
                'notApplicableEvidence':{'reason':'ESTABLISHED_AFTER_PERIOD','date':'2026-10-01',
                    'sourceUrl':f'https://fundf10.eastmoney.com/jbgk_{selected}.html',
                    'periodEnd':'2026-09-30','fundCode':selected}}],'barsByStock':{},'errors':[]})
            subprocess.run(['node','scripts/build-data.js','--input','raw/input.json','--output',
                '.cache/parts/part-0/part.json','--config','.cache/plan/batch-0.json'],check=True,capture_output=True)
            result=campaign.publish()
            self.assertFalse(result['continueRun'])
            audit=read_json('web/data/periods/2026Q3/audit.json')
            self.assertEqual(audit['notApplicableFunds'],1);self.assertEqual(audit['pendingFunds'],1)
            self.assertIsNone(audit['continuationHold'])
            self.assertEqual(audit['marketAccounting']['unresolvedBatches'][0]['status'],'missing')
            self.assertTrue(read_json('web/data/periods/2026Q3/progress.json')['continueRun'])

    def test_publisher_workflow_requires_receipts_without_rejecting_source_download(self):
        text=(Path(__file__).parents[1]/'.github/workflows/traverse.yml').read_text()
        publisher=text[text.index('  publish:'):]
        self.assertIn('pattern: request-accounting-${{ github.run_id }}-${{ github.run_attempt }}-*',publisher)
        self.assertIn('CAMPAIGN_MARKET_ACCOUNTING: required',publisher)
        self.assertLess(publisher.index('path: .cache/request-accounting'),publisher.index('python scripts/campaign.py publish'))


if __name__=='__main__':unittest.main()
