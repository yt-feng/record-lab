"""Stored-price reconciliation cannot create observations or spend a lane."""
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import reconcile_stored_quality as reconcile
import traversal


class ReconcileStoredQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code = "const f=require('./tests/price-reference-fixture');console.log(JSON.stringify({data:f.fixture(),catalog:f.catalog,config:f.config,fund:f.snapshot('000001','yahoo').funds[0]}));"
        cls.fixture = json.loads(subprocess.check_output(['node', '-e', code], cwd=ROOT, text=True))

    def sample(self):
        value = copy.deepcopy(self.fixture)
        data, catalog, config, fund = (value[key] for key in ('data', 'catalog', 'config', 'fund'))
        clock = dt.datetime.fromisoformat(data['now'].replace('Z', '+00:00'))
        config.update(collectorVersion=5, requireReportQuality=True, requireHistoricalUniverse=True)
        catalog['retrievedAt'] = data['fetchedAt']
        catalog['companies'][0]['directoryStatus'] = 'complete'
        old_fund = copy.deepcopy(fund)
        old_fund['rows'][0]['priceRange'] = {**data['prices'][1], 'globalPriceConflict': data['witness']}
        fund['rows'][0]['priceRange'] = {**old_fund['rows'][0]['priceRange'], 'rangeReference': data['reference']}
        previous = traversal.merge_progress(catalog, None, config, now=clock)
        state = previous['funds'][0]
        state.update(status='complete', attempted=True, attempts=1, totalAttempts=4, lastAttemptAt=data['fetchedAt'])
        previous = traversal.merge_progress(catalog, previous, config, now=clock, retained=[old_fund])
        # A stored unknown ledger is intentionally outside current normalizers.
        # Its original bytes and all out-of-band counts remain authoritative.
        previous['updatedAt'] = '2026-10-29T20:00:00.000000+08:00'
        state = previous['funds'][0]
        state['unknownRequestLedger'] = {'outcome': 'unknown', 'attemptedAt': data['fetchedAt'], 'count': None}
        state['reportLocatorAttempts'] = {'unknown-legacy': {'outcome': 'unknown', 'observedAt': data['fetchedAt']}}
        state['enrichment'].update(attempts=7, totalAttempts=17, lastAttemptAt=data['fetchedAt'],
            retry={'nextRetryAt': '2026-11-01T00:00:00Z', 'unknownCycles': 3})
        state['quality'].update(attempts=8, totalAttempts=28, managerAttempts=5, managerTotalAttempts=15,
            strategyAttempts=6, strategyTotalAttempts=16, lastAttemptAt=data['fetchedAt'],
            managerLastAttemptAt=data['fetchedAt'], strategyLastAttemptAt=data['fetchedAt'],
            retry={'nextRetryAt': '2026-11-02T00:00:00Z', 'unknownCycles': 4})
        previous.update(continuationHold={'reason': 'UNKNOWN_REQUEST', 'observedAt': data['fetchedAt']},
            marketAccounting={'continuationAllowed': False, 'unknownBatches': [{'attemptedAt': data['fetchedAt']}]},
            privateFutureLedger={'status': 'unknown', 'rawCount': None}, continueRun=False,
            nextRetryAt='2026-11-01T00:00:00Z', directoryPasses=9)
        previous['research']['futureCounter'] = {'unchanged': 3}
        previous['research']['fieldCoverage']['futureField'] = 7
        audit = {key: copy.deepcopy(previous[key]) for key in ('phase', 'updatedAt', 'research', 'completionAudit',
            'completionStatus', 'continueRun', 'continuationHold', 'marketAccounting', 'nextRetryAt')}
        audit.update(schemaVersion=1, quarter='2026Q2', unknownAuditLedger={'status': 'unknown', 'count': None})
        return catalog, previous, config, fund, audit

    def test_real_current_hydration_updates_price_quality_and_preserves_every_history(self):
        catalog, previous, config, fund, audit = self.sample()
        before = copy.deepcopy((previous, fund, audit))
        result, result_audit, summary = reconcile.reconcile(catalog, previous, config, [fund], audit)
        self.assertEqual(summary['beforePriceCompleteFunds'], 0)
        self.assertEqual(summary['afterPriceCompleteFunds'], 1)
        self.assertGreater(summary['protectedNormalizationsNotApplied'], 0)
        self.assertEqual(result['funds'][0]['enrichment']['priceConflicted'], 0)
        self.assertEqual(result['funds'][0]['enrichment']['status'], 'complete')
        self.assertEqual(result['funds'][0]['quality']['fields']['prices'], 'complete')
        self.assertEqual(result['enrichment']['attempts'], 7)
        for old, new in zip(previous['funds'], result['funds']):
            self.assertEqual(reconcile.protected_fund(new), reconcile.protected_fund(old))
        for key in ('updatedAt', 'continueRun', 'nextRetryAt', 'directoryPasses', 'collectorVersion',
                    'continuationHold', 'marketAccounting', 'privateFutureLedger', 'strategyClaims'):
            self.assertEqual(result[key], previous[key], key)
        for key in ('updatedAt', 'continueRun', 'nextRetryAt', 'continuationHold', 'marketAccounting', 'unknownAuditLedger'):
            self.assertEqual(result_audit[key], audit[key], key)
        self.assertEqual(result['completionAudit']['asOf'], previous['completionAudit']['asOf'])
        self.assertFalse(result['completionAudit']['complete'])
        self.assertEqual(result['research']['futureCounter'], previous['research']['futureCounter'])
        self.assertEqual(result['research']['fieldCoverage']['futureField'], 7)
        self.assertEqual((previous, fund, audit), before)
        again, again_audit, second = reconcile.reconcile(catalog, result, config, [fund], result_audit)
        self.assertEqual(again, result); self.assertEqual(again_audit, result_audit)
        self.assertFalse(second['progressChanged']); self.assertFalse(second['auditChanged'])

    def test_version_migration_status_uses_preserved_attempts_on_the_first_pass(self):
        catalog, previous, config, fund, audit = self.sample()
        quality = previous['funds'][0]['quality']
        quality.update(managerTableCoverageVersion=0, strategySectionCoverageVersion=0)
        result, result_audit, _ = reconcile.reconcile(catalog, previous, config, [fund], audit)
        after = result['funds'][0]['quality']
        self.assertEqual(after['status'], 'exhausted')
        self.assertEqual((after['attempts'], after['managerAttempts'], after['strategyAttempts']), (8, 5, 6))
        self.assertEqual(result['research']['exhaustedFunds'], 1)
        self.assertEqual(result['completionAudit']['research']['exhaustedFunds'], 1)
        self.assertEqual(result['completionAudit']['work']['reportRepairTasks'], 0)
        again, again_audit, _ = reconcile.reconcile(catalog, result, config, [fund], result_audit)
        self.assertEqual(again, result); self.assertEqual(again_audit, result_audit)

    def test_compact_price_only_gap_builds_audit_without_persisting_runtime_fields(self):
        catalog, previous, config, fund, audit = self.sample()
        state = previous['funds'][0]
        state['quality'].update(fields={key: 'missing' if key == 'prices' else 'complete'
                                       for key in traversal.QUALITY_FIELDS},
                                attempts=0, status='pending', missingFields=['prices'],
                                priceConflictOnly=False)
        stock = fund['rows'][0]
        state['enrichment'].update(attempts=0, status='pending', holdingCount=1,
                                   priceAvailable=0, priceComplete=0, priceConflicted=0,
                                   conflictingStocks=[], referenceResolutions=[],
                                   missingStockKeys=[traversal.stock_key(stock)])
        state['enrichment'].pop('missingStocks', None)
        before = copy.deepcopy((previous, audit))
        # No source observations are supplied. This exercises the compact
        # checkpoint with a price-only task that enters eligible_work's branch.
        result, result_audit, _ = reconcile.reconcile(catalog, previous, config, [], audit)
        work = result['completionAudit']['work']
        self.assertEqual(work['priceRepairTasks'], 1)
        self.assertEqual(work['reportRepairTasks'], 0)
        after = result['funds'][0]
        self.assertNotIn('missingStocks', after['enrichment'])
        self.assertEqual(after['enrichment']['missingStockKeys'], state['enrichment']['missingStockKeys'])
        self.assertEqual(reconcile.protected_fund(after), reconcile.protected_fund(state))
        for key in ('updatedAt', 'continuationHold', 'marketAccounting', 'continueRun', 'nextRetryAt'):
            self.assertEqual(result[key], previous[key])
            self.assertEqual(result_audit[key], audit[key])
        self.assertFalse(result['completionAudit']['complete'])
        self.assertEqual((previous, audit), before)
        again, again_audit, _ = reconcile.reconcile(catalog, result, config, [], result_audit)
        self.assertEqual(again, result); self.assertEqual(again_audit, result_audit)

    def test_audit_expansion_rejects_inconsistent_compact_stock_targets(self):
        row = {'code': '000001', 'enrichment': {'missingStockKeys': ['CN:600519'],
                                              'missingStocks': []}}
        with self.assertRaisesRegex(ValueError, '^QUALITY_COMPACT_STOCKS_INVALID$'):
            reconcile.audit_states([row])

    @staticmethod
    def audit_for(previous):
        return {key: copy.deepcopy(previous[key]) for key in
                ('phase', 'updatedAt', 'research', 'completionAudit', 'completionStatus', 'continueRun')}

    def test_strategy_task_survives_audit_projection_without_becoming_a_stored_descriptor(self):
        import test_strategy_claim_queue as claims
        for blocked in (False, True):
            previous = claims.progress()
            previous['funds'][0]['strategyClaimsHistoryBlocked'] = blocked
            audit = self.audit_for(previous)
            original = copy.deepcopy(previous)
            retained = iter([claims.fund()])
            result, _, _ = reconcile.reconcile(claims.CATALOG, previous, claims.CONFIG, retained, audit)
            self.assertEqual(result['completionAudit']['work']['strategyClaimsTasks'], 0 if blocked else 1)
            self.assertNotIn('strategyClaimsTask', result['funds'][0])
            self.assertEqual(reconcile.protected_fund(result['funds'][0]),
                             reconcile.protected_fund(previous['funds'][0]))
            self.assertEqual(previous, original)

    def test_locator_task_survives_audit_projection_with_original_exhausted_source_budget(self):
        import test_report_locator_queue as locators
        config = locators.config()
        previous = locators.exhausted(config)
        audit = self.audit_for(previous)
        result, _, _ = reconcile.reconcile(locators.CATALOG, previous, config, [], audit)
        self.assertEqual(result['completionAudit']['work']['reportLocatorTasks'], 1)
        self.assertEqual(result['completionAudit']['work']['sourceRetryTasks'], 0)
        self.assertNotIn('reportLocator', result['funds'][0])
        self.assertEqual(result['funds'][0]['attempts'], 2)
        self.assertEqual(reconcile.protected_fund(result['funds'][0]),
                         reconcile.protected_fund(previous['funds'][0]))

    def test_manifest_preparation_reloads_only_the_retained_quarter_locator_registry(self):
        import test_report_locator_queue as locators
        from report_locators import empty_registry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'web/data/periods/2026Q3'
            folder.mkdir(parents=True); (root / 'config').mkdir()
            config = locators.config()
            previous = locators.exhausted(config)
            registry = empty_registry('2026Q3')
            registry.update(records=config['reportLocatorCandidates']['000001']['candidates'],
                            asOf='2026-10-26', lastAttemptAt=locators.NOW, refreshStatus='current')
            documents = {'catalog.json': locators.CATALOG, 'progress.json': previous,
                         'audit.json': self.audit_for(previous), 'report-locators.json': registry,
                         'latest.json': {'schemaVersion': 1, **locators.PHASE, 'funds': [], 'companyFiles': []}}
            for name, document in documents.items():
                (folder / name).write_text(json.dumps(document))
            (root / 'config/universe.json').write_text(json.dumps({'reportLocatorEnabled': True}))
            before = (folder / 'report-locators.json').read_bytes()
            outputs, _ = reconcile.prepare_quarter(root, '2026Q3')
            produced = {file.name: json.loads(raw) for file, raw in outputs}
            progress = produced.get('progress.json', previous)
            self.assertEqual(progress['completionAudit']['work']['reportLocatorTasks'], 1)
            self.assertNotIn('reportLocator', progress['funds'][0])
            self.assertEqual((folder / 'report-locators.json').read_bytes(), before)
            (folder / 'report-locators.json').unlink()
            with self.assertRaisesRegex(ValueError, '^QUALITY_LOCATOR_REGISTRY_MISSING$'):
                reconcile.prepare_quarter(root, '2026Q3')

    def test_newer_stored_strategy_evidence_uses_validation_clock_without_redating_progress(self):
        from test_traversal import TraversalTests
        catalog, previous, config, fund, audit = self.sample()
        latest = TraversalTests().retained_fund()
        latest['rows'] = fund['rows']
        latest['selectedReport']['strategyEvidenceRetrievedAt'] = '2026-10-29T14:00:00.123456Z'
        observed = copy.deepcopy(latest)
        old_clock, _, _ = reconcile.reconcile(catalog, previous, config, [latest], audit)
        self.assertNotEqual(old_clock['funds'][0]['quality']['fields']['strategy'], 'complete')
        now = dt.datetime(2026, 10, 29, 15, tzinfo=dt.timezone.utc)
        result, updated_audit, summary = reconcile.reconcile(catalog, previous, config, [latest], audit, now=now)
        self.assertEqual(result['funds'][0]['quality']['fields']['strategy'], 'complete')
        self.assertEqual(result['funds'][0]['quality']['fields']['prices'], 'complete')
        self.assertEqual(summary['validatedAt'], now.isoformat())
        self.assertEqual(result['updatedAt'], previous['updatedAt'])
        self.assertEqual(updated_audit['updatedAt'], audit['updatedAt'])
        self.assertEqual(latest, observed)
        self.assertEqual(reconcile.protected_fund(result['funds'][0]), reconcile.protected_fund(previous['funds'][0]))

    def test_sourced_not_applicable_product_keeps_its_proof_and_original_attempt_history(self):
        catalog, previous, config, fund, audit = self.sample()
        state = previous['funds'][1]
        proof = {'reason': 'ESTABLISHED_AFTER_PERIOD', 'date': '2026-07-02', 'fundCode': state['code'],
            'periodEnd': '2026-06-30', 'sourceUrl': 'https://fundf10.eastmoney.com/jbgk_000002.html'}
        state.update(status='not-applicable', attempted=True, lastAttemptAt=self.fixture['data']['fetchedAt'],
                     notApplicableEvidence=proof, attempts=1, totalAttempts=4)
        before = copy.deepcopy(state)
        result, _, _ = reconcile.reconcile(catalog, previous, config, [fund], audit)
        after = result['funds'][1]
        self.assertEqual(after['quality']['status'], 'not-applicable')
        self.assertNotIn('priceConflictOnly', after['quality'])
        self.assertEqual(after['notApplicableEvidence'], proof)
        self.assertEqual(reconcile.protected_fund(after), reconcile.protected_fund(before))

    def test_bare_or_invalid_reference_does_not_clear_the_conflict(self):
        for invalid in ('missing', 'bad-digest'):
            catalog, previous, config, fund, audit = self.sample()
            price = fund['rows'][0]['priceRange']
            if invalid == 'missing': price.pop('rangeReference')
            else: price['rangeReference']['factSetDigest'] = '0' * 64
            result, _, _ = reconcile.reconcile(catalog, previous, config, [fund], audit)
            self.assertEqual(result['funds'][0]['enrichment']['priceConflicted'], 1)
            self.assertNotEqual(result['funds'][0]['quality']['fields']['prices'], 'complete')

    def test_phase_directory_source_status_and_clock_changes_fail_closed(self):
        for kind in ('phase', 'directory', 'source-status', 'clock'):
            catalog, previous, config, fund, audit = self.sample()
            if kind == 'phase': config['targetPeriodEnd'] = '2026-09-30'
            if kind == 'directory': catalog['funds'].pop()
            if kind == 'source-status': previous['funds'][0]['lastAttemptAt'] = None
            if kind == 'clock': previous['updatedAt'] = 'invalid'
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, '^QUALITY_'):
                reconcile.reconcile(catalog, previous, config, [fund], audit)

    def files(self, root):
        catalog, previous, config, fund, audit = self.sample()
        folder = root / 'web/data/periods/2026Q2'
        (folder / 'companies').mkdir(parents=True)
        (root / 'config').mkdir()
        documents = {folder / 'catalog.json': catalog, folder / 'progress.json': previous,
            folder / 'audit.json': audit, root / 'config/universe.json': config,
            folder / 'latest.json': {'schemaVersion': 1, **previous['phase'], 'funds': [],
                'companyFiles': [{'path': './data/companies/example.json'}]},
            folder / 'companies/example.json': {'schemaVersion': 1, 'funds': [fund]}}
        for file, value in documents.items(): file.write_text(json.dumps(value), encoding='utf-8')
        (folder / 'market-request-ledger.json').write_text('{"outcome":"unknown"}\n')
        return folder, documents

    def test_actual_manifest_to_progress_and_audit_write_uses_no_new_source_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); folder, documents = self.files(root)
            untouched = {file: file.read_bytes() for file in documents if file.name not in ('progress.json', 'audit.json')}
            ledger = (folder / 'market-request-ledger.json').read_bytes()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main'}), patch('sys.stdout', io.StringIO()), \
                    patch.object(reconcile, 'validation_now', return_value=dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)):
                reconcile.main(['--root', str(root), '--quarter', '2026Q2'])
            progress = json.loads((folder / 'progress.json').read_text())
            audit = json.loads((folder / 'audit.json').read_text())
            self.assertEqual(progress['research']['fieldCoverage']['prices'], 1)
            self.assertEqual(audit['research'], progress['research'])
            self.assertEqual(progress['updatedAt'], documents[folder / 'progress.json']['updatedAt'])
            self.assertEqual((folder / 'market-request-ledger.json').read_bytes(), ledger)
            self.assertTrue((root / '.cache/stored-price-reference/quality-result.json').is_file())
            for file, original in untouched.items(): self.assertEqual(file.read_bytes(), original)

    def test_entry_requires_actions_main_and_prepares_both_quarters_before_writing(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false', 'GITHUB_REF': 'refs/heads/main'}):
            with self.assertRaisesRegex(ValueError, 'REQUIRES_ACTIONS_MAIN'): reconcile.main([])
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/topic'}):
            with self.assertRaisesRegex(ValueError, 'REQUIRES_ACTIONS_MAIN'): reconcile.main([])
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'progress.json'; file.write_bytes(b'original')
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main'}), \
                    patch.object(reconcile, 'prepare_quarter', side_effect=[([(file, b'changed')], {}), ValueError('QUALITY_PHASE_INVALID')]):
                with self.assertRaisesRegex(ValueError, 'QUALITY_PHASE_INVALID'): reconcile.main(['--root', directory])
            self.assertEqual(file.read_bytes(), b'original')

    def test_write_failure_propagates_and_cannot_become_a_successful_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); folder, _ = self.files(root)
            original = (folder / 'progress.json').read_bytes()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main'}), \
                    patch.object(Path, 'replace', side_effect=OSError('synthetic write failure')), \
                    patch.object(reconcile, 'validation_now', return_value=dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)):
                with self.assertRaises(OSError): reconcile.main(['--root', str(root), '--quarter', '2026Q2'])
            self.assertEqual((folder / 'progress.json').read_bytes(), original)
            self.assertFalse((root / '.cache/stored-price-reference/quality-result.json').exists())


if __name__ == '__main__': unittest.main()
