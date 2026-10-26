"""Synthetic source-bound census proofs only; no GitHub or market requests."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).parents[1] / 'scripts'), str(Path(__file__).parent)]
import verify_price_source_census as census
import price_conflicts as conflicts
from test_price_daily_evidence import execute, LATER
from test_restore_price_conflict_ledger import FakeAPI, run_metadata


def fixture():
    registry, inherited, plan, execution, _, _ = execute()
    final = conflicts.merge_receipts(registry, inherited, plan, execution['receipts'], now=LATER)
    empty = conflicts.empty_ledger('2026Q2')
    files = {'2026Q3/registry.json': registry, '2026Q3/plan.json': plan, '2026Q3/receipts.json': execution['receipts'],
        '2026Q3/execution.json': execution, '2026Q3/input-ledger.json': inherited, '2026Q3/ledger.json': final,
        '2026Q2/input-ledger.json': empty, '2026Q2/ledger.json': empty,
        '2026Q2/plan.json': {'quarter': '2026Q2', 'items': []}, '2026Q2/receipts.json': []}
    files['result.json'] = {'kind': 'price-conflict-run', 'status': 'complete', 'target': plan['target'],
                            'productionDataModified': False, 'automaticResolution': False}
    files['provenance.json'] = {'schemaVersion': 1, 'kind': 'price-conflict-provenance',
        'repository': census.restore.REPOSITORY, 'workflowPath': census.restore.WORKFLOW,
        'runId': 500, 'runAttempt': 1, 'sourceCommit': 'a' * 40, 'dataCommit': 'b' * 40,
        'restoredFrom': {**conflicts.LEDGER_BOOTSTRAP, 'conclusion': 'success'}, 'bootstrap': conflicts.LEDGER_BOOTSTRAP,
        'inputLedgerHashes': {q: hashlib.sha256(conflicts.encoded(files[f'{q}/input-ledger.json'])).hexdigest() for q in conflicts.PHASES}}
    api = FakeAPI(files, run_metadata(500, 12, 'a' * 40))
    proof = {'runId': 500, 'runAttempt': 1, 'sourceCommit': 'a' * 40, 'dataCommit': 'b' * 40,
        'artifactId': api.artifact['id'], 'archiveSha256': hashlib.sha256(api.raw).hexdigest(),
        'evidenceSha256': execution['dailyEvidence'][0]['evidenceSha256']}
    return api, proof


def rebind(api, proof):
    api.repack(); proof['archiveSha256'] = hashlib.sha256(api.raw).hexdigest()


def sample_checkout(root, proof, *, new_observation=False):
    root = Path(root); directory = root / 'web/data/periods/2026Q3/companies'; directory.mkdir(parents=True)
    facts = {fact['factId']: fact for fact in proof['facts']}; funds = []
    for index, obs in enumerate(proof['coveredObservations']):
        quote = {**copy.deepcopy(facts[obs['factId']]['price']), **{key: obs[key] for key in ('sourceUrl', 'sourceUrls', 'fetchedAt')}}
        if new_observation and index == 0: quote['fetchedAt'] = '2026-10-26T12:00:00+00:00'
        funds.append({'code': f'{index + 1:06d}', 'status': 'ok', 'retrievedAt': '2026-10-26T19:00:00Z', 'priceUpdatedAt': None,
            'selectedReport': {'periodEnd': '2026-09-30'}, 'rows': [{'market': 'CN', 'stockCode': '000636', 'currency': 'CNY', 'priceRange': quote}]})
    manifest = {'schemaVersion': 1, 'sourceMode': 'actions', 'targetPeriodEnd': '2026-09-30', 'baselinePeriodEnd': '2026-06-30',
                'companyFiles': [{'path': './data/companies/co1.json'}]}
    (directory.parent / 'latest.json').write_text(json.dumps(manifest))
    (directory / 'co1.json').write_text(json.dumps({**manifest, 'funds': funds}))
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q'); git('add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'Synthetic source data')
    return git('rev-parse', 'HEAD')


def scan(root, commit, limits=None):
    script = "const {scan}=require('./scripts/price-source-census'); const x=JSON.parse(require('fs').readFileSync(0,'utf8')); process.stdout.write(JSON.stringify(scan(x.root,x.options)));"
    result = subprocess.run(['node', '-e', script], input=json.dumps({'root': str(Path(root).resolve() / 'web/data/periods'),
        'options': {'dataCommit': commit, 'now': LATER.isoformat(), 'limits': limits or {}}}),
        cwd=census.ROOT, capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


class SourceCensusProofTests(unittest.TestCase):
    def test_actual_js_census_and_python_certificate_bind_independent_data_scope(self):
        api, expected = fixture()
        with patch.object(census, 'PROOF', expected): proof = census.restore_proof(api, LATER)
        with tempfile.TemporaryDirectory() as directory:
            commit = sample_checkout(directory, proof); value = scan(directory, commit)
            with patch.object(census, 'DATA_COMMIT', commit), patch.object(census, 'PROOF', expected):
                result = census.certificate(value, proof, LATER)
                later_view = copy.deepcopy(value); later_view['generatedAt'] = '2026-10-27T02:00:59+00:00'
                self.assertEqual(census.certificate(later_view, proof, LATER)['diagnosticCandidate']['digest'], result['diagnosticCandidate']['digest'])
                changed_blob = copy.deepcopy(value); changed_blob['parts'][0]['sha256'] = '0' * 64
                self.assertNotEqual(census.certificate(changed_blob, proof, LATER)['diagnosticCandidate']['digest'], result['diagnosticCandidate']['digest'])
        self.assertEqual(result['status'], 'complete'); self.assertTrue(result['diagnosticCandidate']['eligible'])
        self.assertEqual(result['proofScope']['dataCommit'], 'b' * 40)
        self.assertEqual(result['censusScope']['dataCommit'], commit)
        self.assertEqual(result['priceLedgerParentRunId'], expected['runId'])
        self.assertEqual(result['diagnosticCandidate']['matchedObservationCount'], 2)
        self.assertEqual(result['diagnosticCandidate']['admission'], 'not-implemented')
        self.assertEqual(result['marketDataRequests'], 0); self.assertFalse(result['ledgerModified'])

    def test_new_original_clock_with_identical_fact_and_extremes_remains_uncovered(self):
        api, expected = fixture()
        with patch.object(census, 'PROOF', expected): proof = census.restore_proof(api, LATER)
        with tempfile.TemporaryDirectory() as directory:
            commit = sample_checkout(directory, proof, new_observation=True); value = scan(directory, commit)
            with patch.object(census, 'DATA_COMMIT', commit): result = census.certificate(value, proof, LATER)
        self.assertEqual(result['status'], 'partial'); self.assertFalse(result['diagnosticCandidate']['eligible'])
        self.assertEqual(result['diagnosticCandidate']['uncoveredObservationCount'], 1)
        self.assertEqual(result['diagnosticCandidate']['uncoveredFactIds'], [])
        self.assertEqual(result['censusScope']['rawRegistry']['digest'], proof['registryDigest'])

    def test_source_facts_observation_body_digest_and_completeness_cannot_be_self_asserted(self):
        api, expected = fixture()
        with patch.object(census, 'PROOF', expected): proof = census.restore_proof(api, LATER)
        with tempfile.TemporaryDirectory() as directory:
            commit = sample_checkout(directory, proof); value = scan(directory, commit)
        for change in ('fact', 'observation', 'digest', 'counter', 'unbounded', 'extra', 'generated-clock'):
            raw = copy.deepcopy(value)
            if change == 'fact': raw['facts'][0]['price']['high'] += 1
            if change == 'observation': raw['sourceObservations'][0]['fetchedAt'] = LATER.isoformat()
            if change == 'digest': raw['sourceUniverseDigest'] = '0' * 64
            if change == 'counter': raw['counts']['invalidTargetOccurrences'] = 1
            if change == 'unbounded': raw['sourceObservations'][0]['locations'][0]['partPath'] = '../outside'
            if change == 'extra': raw['privateText'] = 'PRIVATE_SENTINEL'
            if change == 'generated-clock': raw['generatedAt'] = '2026-10-26T09:00:00Z'
            with self.subTest(change=change), patch.object(census, 'DATA_COMMIT', commit), self.assertRaises(census.CensusError):
                census.normalize_census(raw, LATER)

    def test_bounded_partial_census_cannot_issue_eligible_candidate(self):
        api, expected = fixture()
        with patch.object(census, 'PROOF', expected): proof = census.restore_proof(api, LATER)
        with tempfile.TemporaryDirectory() as directory:
            commit = sample_checkout(directory, proof); value = scan(directory, commit, {'maxFacts': 1})
            with patch.object(census, 'DATA_COMMIT', commit): result = census.certificate(value, proof, LATER)
        self.assertEqual(result['status'], 'partial'); self.assertFalse(result['diagnosticCandidate']['eligible'])
        self.assertIn('CENSUS_EVIDENCE_LIMIT', result['censusScope']['reasonCodes'])

    def test_actions_gate_precedes_source_or_github_reads(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(census, 'data_identity', side_effect=AssertionError('no data read')):
            with self.assertRaisesRegex(census.CensusError, 'CENSUS_REQUIRES_ACTIONS'):
                census.run('unused', 'unused-output')

    def test_actions_entry_actual_child_scan_restores_only_existing_proof(self):
        api, expected = fixture()
        with patch.object(census, 'PROOF', expected): proof = census.restore_proof(api, LATER)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'data'; commit = sample_checkout(root, proof)
            env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': census.restore.REPOSITORY,
                   'GITHUB_WORKFLOW_REF': f'{census.restore.REPOSITORY}/{census.WORKFLOW}@refs/heads/main'}
            api.calls.clear()
            with patch.dict(os.environ, env), patch.object(census, 'DATA_COMMIT', commit), patch.object(census, 'PROOF', expected):
                result = census.run(root, Path(directory) / 'output', api=api, now=LATER)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual({p.name for p in (Path(directory) / 'output').iterdir()}, {'census.json', 'proof.json', 'result.json'})
            self.assertEqual(len(api.calls), 3)
            self.assertEqual(result['marketDataRequests'], 0)

    def test_invalid_child_output_never_enters_always_upload_artifact_directory(self):
        api, expected = fixture()
        with patch.object(census, 'PROOF', expected): proof = census.restore_proof(api, LATER)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'data'; commit = sample_checkout(root, proof); output = Path(directory) / 'output'
            env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': census.restore.REPOSITORY,
                   'GITHUB_WORKFLOW_REF': f'{census.restore.REPOSITORY}/{census.WORKFLOW}@refs/heads/main'}
            with patch.dict(os.environ, env), patch.object(census, 'DATA_COMMIT', commit), patch.object(census, 'PROOF', expected), \
                 patch.object(census, 'normalize_census', side_effect=census.CensusError('CENSUS_INPUT_INVALID')):
                with self.assertRaises(census.CensusError): census.run(root, output, api=api, now=LATER)
            self.assertFalse(output.exists())

    def test_restores_fixed_original_scope_without_new_requests_or_ledger_mutation(self):
        api, proof = fixture(); original = copy.deepcopy(api.files)
        with patch.object(census, 'PROOF', proof), patch.object(conflicts, 'execute_rechecks', side_effect=AssertionError('no market requests')):
            value = census.restore_proof(api, LATER)
        self.assertEqual(value['provenance']['dataCommit'], 'b' * 40)
        self.assertNotEqual(value['provenance']['dataCommit'], census.DATA_COMMIT)
        self.assertEqual(api.files, original); self.assertEqual(value['attemptRounds'], 2)
        self.assertEqual(value['remainingAttempts'], 0); self.assertEqual(value['marketDataRequests'], 0)
        self.assertFalse(value['ledgerModified']); self.assertFalse(value['automaticResolution'])
        self.assertEqual(value['resolution'], 'unresolved'); self.assertEqual(len(value['coveredObservations']), 2)
        self.assertEqual(len(api.calls), 3)
        self.assertFalse(any('workflows/' in endpoint or '501' in endpoint for endpoint in api.calls))

    def test_wrong_run_source_data_artifact_and_archive_hash_fail_closed(self):
        for change in ('run', 'source', 'data', 'artifact', 'archive'):
            api, proof = fixture()
            if change == 'run': api.prior['id'] = 599
            if change == 'source': api.prior['head_sha'] = 'e' * 40
            if change == 'data': api.files['provenance.json']['dataCommit'] = 'd' * 40; rebind(api, proof)
            if change == 'artifact': proof['artifactId'] += 1
            if change == 'archive': proof['archiveSha256'] = '0' * 64
            with self.subTest(change=change), patch.object(census, 'PROOF', proof), self.assertRaises(census.CensusError):
                census.restore_proof(api, LATER)

    def test_daily_evidence_literal_mutation_and_hash_cannot_pass(self):
        for change in ('cell', 'hash', 'provider', 'missing-day'):
            api, proof = fixture(); evidence = api.files['2026Q3/execution.json']['dailyEvidence'][0]
            if change == 'cell': evidence['sources'][0]['series']['rows'][0]['close'] += 0.01
            if change == 'hash': evidence['evidenceSha256'] = '0' * 64
            if change == 'provider': evidence['sources'][1]['provider'] = evidence['sources'][0]['provider']
            if change == 'missing-day': evidence['sources'][0]['series']['rows'].pop()
            rebind(api, proof)
            with self.subTest(change=change), patch.object(census, 'PROOF', proof), self.assertRaises(census.CensusError):
                census.restore_proof(api, LATER)

    def test_receipt_original_clock_and_plan_binding_cannot_be_substituted(self):
        for change in ('receipt-clock', 'plan-digest', 'data-new-scope'):
            api, proof = fixture()
            if change == 'receipt-clock': api.files['2026Q3/receipts.json'][0]['requests'][0]['originalFetchedAt'] = '2026-10-26T11:00:00Z'
            if change == 'plan-digest': api.files['2026Q3/plan.json']['items'][0]['digest'] = '0' * 64
            if change == 'data-new-scope': api.files['2026Q3/registry.json']['dataCommit'] = census.DATA_COMMIT
            rebind(api, proof)
            with self.subTest(change=change), patch.object(census, 'PROOF', proof), self.assertRaises(census.CensusError):
                census.restore_proof(api, LATER)

    def test_lost_round_or_inherited_ledger_cannot_reset_exhausted_budget(self):
        for change in ('lost-round', 'input-hash', 'other-quarter-change'):
            api, proof = fixture()
            if change == 'lost-round': api.files['2026Q3/ledger.json']['entries'][0]['digests'][0]['rounds'].pop(0)
            if change == 'input-hash': api.files['provenance.json']['inputLedgerHashes']['2026Q2'] = '0' * 64
            if change == 'other-quarter-change': api.files['2026Q2/receipts.json'] = [{}]
            rebind(api, proof)
            with self.subTest(change=change), patch.object(census, 'PROOF', proof), self.assertRaises(census.CensusError):
                census.restore_proof(api, LATER)

    def test_archive_duplicate_path_unknown_file_and_malformed_json_rejected(self):
        for addition in (('provenance.json', '{}'), ('../outside.json', '{}'), ('result.json', '{"duplicate":1,"duplicate":2}')):
            api, proof = fixture(); api.repack([addition]); proof['archiveSha256'] = hashlib.sha256(api.raw).hexdigest()
            with self.subTest(path=addition[0]), patch.object(census, 'PROOF', proof), self.assertRaises(census.CensusError):
                census.restore_proof(api, LATER)


if __name__ == '__main__': unittest.main()
