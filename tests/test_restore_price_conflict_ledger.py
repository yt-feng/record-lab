"""Synthetic API metadata and ZIPs only; never fetch a GitHub artifact."""
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import price_conflicts as conflicts
import restore_price_conflict_ledger as restore
from test_price_conflicts import registry, group, NOW
from test_verify_price_conflicts import payload

PRIOR, CURRENT = 500, 501
SOURCE, CURRENT_SOURCE, DATA, CURRENT_DATA = 'a' * 40, 'c' * 40, 'b' * 40, 'd' * 40


def run_metadata(run_id, number, source, status='completed', conclusion='success'):
    return {'id': run_id, 'run_number': number, 'run_attempt': 1, 'workflow_id': 77,
            'head_sha': source, 'status': status, 'conclusion': conclusion, 'event': 'workflow_dispatch',
            'path': restore.WORKFLOW, 'head_branch': 'main',
            'repository': {'id': 100, 'full_name': restore.REPOSITORY}, 'head_repository': {'id': 100, 'full_name': restore.REPOSITORY}}


def env():
    return {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': restore.REPOSITORY,
            'GITHUB_WORKFLOW_REF': f'{restore.REPOSITORY}/{restore.WORKFLOW}@refs/heads/main',
            'GITHUB_RUN_ID': str(CURRENT), 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': CURRENT_SOURCE,
            'PRICE_DIAGNOSTIC_DATA_COMMIT': CURRENT_DATA}


def archive_bytes(files, additions=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items(): archive.writestr(name, json.dumps(value, ensure_ascii=False))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            for name, raw in additions: archive.writestr(name, raw)
    return buffer.getvalue()


class FakeAPI:
    def __init__(self, files, prior=None):
        self.prior = prior or run_metadata(PRIOR, 12, SOURCE)
        self.current = run_metadata(CURRENT, 13, CURRENT_SOURCE, 'in_progress', None)
        self.listing = {'total_count': 2, 'workflow_runs': [copy.deepcopy(self.current), copy.deepcopy(self.prior)]}
        self.files, self.calls = copy.deepcopy(files), []
        self.artifact = {'id': 900, 'name': f'{restore.ARTIFACT_NAME}-{self.prior["id"]}-1', 'expired': False,
                         'workflow_run': {'id': self.prior['id'], 'repository_id': 100, 'head_repository_id': 100,
                                          'head_branch': 'main', 'head_sha': self.prior['head_sha']}}
        self.repack()

    def repack(self, additions=()):
        self.raw = archive_bytes(self.files, additions)
        self.artifact.update(size_in_bytes=len(self.raw), digest='sha256:' + hashlib.sha256(self.raw).hexdigest())

    def get(self, suffix, binary=False):
        self.calls.append(suffix)
        if suffix == f'actions/runs/{CURRENT}': return copy.deepcopy(self.current)
        if suffix == f'actions/runs/{self.prior["id"]}': return copy.deepcopy(self.prior)
        if suffix == 'actions/workflows/77/runs?per_page=100': return copy.deepcopy(self.listing)
        if suffix == f'actions/runs/{self.prior["id"]}/artifacts?per_page=100':
            return {'total_count': 1, 'artifacts': [copy.deepcopy(self.artifact)]}
        if suffix == f'actions/artifacts/{self.artifact["id"]}/zip': return self.raw
        raise AssertionError('unexpected API endpoint')


def bundle(interrupted=False):
    files = {}
    for quarter in conflicts.PHASES:
        value = registry(quarter=quarter); plan = conflicts.plan_rechecks(value, now=NOW)
        checkpoints = []
        class Interrupted(BaseException): pass
        def fetch(url):
            if interrupted: raise Interrupted()
            return payload('eastmoney' if 'eastmoney' in url else 'tencent')
        try: conflicts.execute_rechecks(plan, value, checkpoint=lambda rows: checkpoints.append(rows), fetch=fetch, now=NOW)
        except Interrupted: pass
        receipts = checkpoints[-1]
        # An interrupted write can leave receipts ahead of the ledger. Restore
        # must replay the real request-start checkpoint before planning work.
        ledger = conflicts.empty_ledger(quarter) if interrupted else conflicts.merge_receipts(value, None, plan, receipts, now=NOW)
        files.update({f'{quarter}/registry.json': value, f'{quarter}/plan.json': plan,
                      f'{quarter}/receipts.json': receipts, f'{quarter}/ledger.json': ledger,
                      f'{quarter}/input-ledger.json': conflicts.empty_ledger(quarter)})
    parent = {**restore.BOOTSTRAP, 'conclusion': 'success'}
    files['provenance.json'] = {'schemaVersion': 1, 'kind': 'price-conflict-provenance', 'repository': restore.REPOSITORY,
                                'workflowPath': restore.WORKFLOW, 'runId': PRIOR, 'runAttempt': 1, 'sourceCommit': SOURCE,
                                'dataCommit': DATA, 'restoredFrom': parent, 'bootstrap': restore.BOOTSTRAP,
                                'inputLedgerHashes': {q: hashlib.sha256(conflicts.encoded(files[f'{q}/input-ledger.json'])).hexdigest()
                                                      for q in conflicts.PHASES}}
    return files


def restore_fake(api, **changes):
    args = {'prior_run_id': api.prior['id'], 'prior_source_commit': api.prior['head_sha'], 'prior_data_commit': DATA,
            'data_commit': CURRENT_DATA, 'env': env(), 'api': api, 'now': NOW}
    args.update(changes)
    return restore.restore(**args)


class RestoreConflictLedgerTests(unittest.TestCase):
    def test_completed_success_restores_exact_history_and_allows_explicit_new_data_commit(self):
        api = FakeAPI(bundle()); state = restore_fake(api)
        self.assertEqual(state['currentDataCommit'], CURRENT_DATA); self.assertEqual(state['restoredFrom']['dataCommit'], DATA)
        for quarter in conflicts.PHASES:
            self.assertEqual(state['ledgers'][quarter], api.files[f'{quarter}/ledger.json'])
            self.assertEqual(conflicts.plan_rechecks(registry(quarter=quarter), state['ledgers'][quarter], now=NOW)['items'][0]['round'], 2)
        self.assertEqual(len(api.calls), 5)
        self.assertEqual(state['bootstrap'], restore.BOOTSTRAP)

    def test_failed_timed_out_and_cancelled_runs_restore_unknown_receipts_ahead_of_ledger(self):
        for conclusion in ('failure', 'timed_out', 'cancelled'):
            api = FakeAPI(bundle(interrupted=True), run_metadata(PRIOR, 12, SOURCE, conclusion=conclusion))
            state = restore_fake(api)
            for quarter in conflicts.PHASES:
                receipt = state['ledgers'][quarter]['entries'][0]['digests'][0]['rounds'][0]
                self.assertEqual(receipt['requests'][0]['outcome'], 'unknown')
                self.assertEqual(receipt['attemptedAt'], api.files[f'{quarter}/receipts.json'][0]['attemptedAt'])
                self.assertEqual(conflicts.plan_rechecks(registry(quarter=quarter), state['ledgers'][quarter], now=NOW)['items'][0]['round'], 2)

    def test_stale_prior_rejects_serial_or_queued_duplicate_dispatch_before_archive(self):
        for status, conclusion in (('completed', 'success'), ('completed', 'failure'), ('in_progress', None), ('queued', None)):
            api = FakeAPI(bundle()); between = run_metadata(600, 13, CURRENT_SOURCE, status, conclusion)
            api.current['run_number'] = 14
            api.listing = {'total_count': 3, 'workflow_runs': [copy.deepcopy(api.current), between, copy.deepcopy(api.prior)]}
            with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_RESTORE_STALE_PRIOR'): restore_fake(api)
            self.assertFalse(any('/artifacts' in call for call in api.calls))

    def test_missing_metadata_and_deleted_intervening_run_do_not_prove_empty_lineage(self):
        for change in ('missing-parent', 'gap', 'mismatch'):
            api = FakeAPI(bundle())
            if change == 'missing-parent': api.listing['workflow_runs'].pop()
            elif change == 'gap':
                api.current['run_number'] = 14; api.listing['workflow_runs'][0] = copy.deepcopy(api.current)
            else: api.listing['workflow_runs'][1]['head_sha'] = 'f' * 40
            with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_RESTORE_LINEAGE_UNVERIFIED'): restore_fake(api)

    def test_incomplete_prior_foreign_repository_source_or_workflow_fail_closed(self):
        changes = [lambda run: run.update(status='in_progress', conclusion=None),
                   lambda run: run.update(head_sha='f' * 40), lambda run: run['head_repository'].update(id=999),
                   lambda run: run.update(path='.github/workflows/other.yml'), lambda run: run.update(head_branch='other')]
        for mutate in changes:
            api = FakeAPI(bundle()); mutate(api.prior)
            with self.assertRaises(conflicts.ConflictError): restore_fake(api, prior_source_commit=SOURCE)
            self.assertFalse(any('/artifacts' in call for call in api.calls))

    def test_current_attempt_or_no_actions_cannot_restart_budget(self):
        for changed in ({'GITHUB_RUN_ATTEMPT': '2'}, {'GITHUB_ACTIONS': 'false'}, {'GITHUB_REPOSITORY': 'example/other'}):
            api = FakeAPI(bundle())
            with self.assertRaises(conflicts.ConflictError): restore_fake(api, env={**env(), **changed})
            self.assertEqual(api.calls, [])
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'scan_registry') as scanner:
            with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_PRIOR_LEDGER_REQUIRED'):
                conflicts.run(Path(temporary) / 'data', Path(temporary) / 'out')
            scanner.assert_not_called()

    def test_archive_digest_size_identity_and_expiry_are_verified(self):
        for mutate in (lambda api: setattr(api, 'raw', api.raw + b'x'), lambda api: api.artifact.update(digest='sha256:' + '0' * 64),
                       lambda api: api.artifact.update(expired=True), lambda api: api.artifact['workflow_run'].update(head_sha='f' * 40),
                       lambda api: api.artifact['workflow_run'].update(repository_id=99)):
            api = FakeAPI(bundle()); mutate(api)
            with self.assertRaises(conflicts.ConflictError): restore_fake(api)

    def test_zip_traversal_duplicate_symlink_and_unknown_files_are_rejected(self):
        for name in ('../ledger.json', '/ledger.json', '2026Q2/ledger.json', 'unexpected.json'):
            api = FakeAPI(bundle()); api.repack([(name, '{}')])
            with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_RESTORE_ARCHIVE_INVALID'): restore_fake(api)
        api = FakeAPI(bundle()); buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            item = zipfile.ZipInfo('result.json'); item.external_attr = (0o120777 << 16); archive.writestr(item, 'elsewhere')
        api.raw = buffer.getvalue(); api.artifact.update(size_in_bytes=len(api.raw), digest='sha256:' + hashlib.sha256(api.raw).hexdigest())
        with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_RESTORE_ARCHIVE_INVALID'): restore_fake(api)

    def test_provenance_data_source_or_missing_quarter_never_falls_back_to_empty(self):
        for mutate in (lambda files: files['provenance.json'].update(sourceCommit='f' * 40),
                       lambda files: files['2026Q2/registry.json'].update(dataCommit='f' * 40),
                       lambda files: files.pop('2026Q3/ledger.json'), lambda files: files.pop('provenance.json'),
                       lambda files: files['provenance.json']['bootstrap'].update(runId=999)):
            files = copy.deepcopy(bundle()); mutate(files); api = FakeAPI(files)
            with self.assertRaises(conflicts.ConflictError): restore_fake(api)

    def test_only_exact_bootstrap_pin_can_restore_a_legacy_artifact(self):
        files = bundle(); files.pop('provenance.json'); files['result.json'] = {'status': 'complete'}
        for quarter in conflicts.PHASES: files.pop(f'{quarter}/input-ledger.json')
        prior = run_metadata(restore.BOOTSTRAP['runId'], 12, restore.BOOTSTRAP['sourceCommit'])
        api = FakeAPI(files, prior); api.artifact['name'] = restore.ARTIFACT_NAME; api.artifact['id'] = restore.BOOTSTRAP['artifactId']
        with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_RESTORE_BOOTSTRAP_MISMATCH'):
            restore_fake(api, prior_data_commit=restore.BOOTSTRAP['dataCommit'])
        # Synthetic bytes can satisfy only a test-local pin. Production exposes
        # no flag/environment escape for replacing the audited bootstrap digest.
        pin = {**restore.BOOTSTRAP, 'archiveSha256': hashlib.sha256(api.raw).hexdigest(), 'dataCommit': DATA}
        with patch.object(restore, 'BOOTSTRAP', pin), patch.object(conflicts, 'LEDGER_BOOTSTRAP', pin):
            state = restore_fake(api)
            self.assertEqual(state['restoredFrom']['runId'], pin['runId'])
            with self.assertRaises(conflicts.ConflictError): restore_fake(api, prior_data_commit='f' * 40)

    def test_monotonic_union_preserves_unknown_and_completed_receipts_without_extra_attempts(self):
        unknown, complete = bundle(True), bundle(False)
        # Same frozen plan gives the same attempt ID and request start time.
        for quarter in conflicts.PHASES:
            a = conflicts.merge_receipts(unknown[f'{quarter}/registry.json'], None, unknown[f'{quarter}/plan.json'], unknown[f'{quarter}/receipts.json'], now=NOW)
            b = complete[f'{quarter}/ledger.json']
            merged = conflicts.union_ledgers(a, b, quarter, NOW)
            self.assertEqual(merged, b); self.assertEqual(conflicts.union_ledgers(b, a, quarter, NOW), b)
            bad = copy.deepcopy(b); bad['entries'][0]['digests'][0]['rounds'][0]['requests'][0]['startedAt'] = '2026-10-27T02:00:01+00:00'
            with self.assertRaises(conflicts.ConflictError): conflicts.union_ledgers(a, bad, quarter, NOW)

    def test_input_snapshot_hash_and_full_history_inclusion_are_required(self):
        for change in ('missing', 'null', 'hash', 'dropped-history', 'null-ledger'):
            files = bundle(); quarter = '2026Q2'
            if change == 'missing': files.pop(f'{quarter}/input-ledger.json')
            elif change == 'null': files[f'{quarter}/input-ledger.json'] = None
            elif change == 'hash': files['provenance.json']['inputLedgerHashes'][quarter] = 'e' * 64
            elif change == 'null-ledger': files[f'{quarter}/ledger.json'] = None
            else:
                files[f'{quarter}/input-ledger.json'] = copy.deepcopy(files[f'{quarter}/ledger.json'])
                files['provenance.json']['inputLedgerHashes'][quarter] = hashlib.sha256(conflicts.encoded(files[f'{quarter}/input-ledger.json'])).hexdigest()
                files[f'{quarter}/ledger.json'] = conflicts.empty_ledger(quarter)
                for name in ('registry', 'plan', 'receipts'): files.pop(f'{quarter}/{name}.json')
            with self.subTest(change=change), self.assertRaises(conflicts.ConflictError): restore_fake(FakeAPI(files))

    def test_run_saves_all_inherited_quarters_before_first_scan_and_keeps_source_unchanged(self):
        state = restore_fake(FakeAPI(bundle(True)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, output = root / 'source', root / 'out'; source.mkdir()
            with patch.dict(os.environ, env()), patch.object(conflicts, 'scan_registry', side_effect=conflicts.ConflictError('CONFLICT_SCAN_FAILED')):
                with self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_SCAN_FAILED'):
                    conflicts.run(source, output, restored_state=state, now=NOW, fetch=lambda _: self.fail('no quote request'))
            for quarter in conflicts.PHASES:
                self.assertEqual(conflicts.read_json(output / quarter / 'ledger.json'), state['ledgers'][quarter])
                self.assertEqual(conflicts.read_json(output / quarter / 'input-ledger.json'), state['ledgers'][quarter])
            proof = conflicts.read_json(output / 'provenance.json')
            self.assertEqual(proof['sourceCommit'], CURRENT_SOURCE); self.assertEqual(proof['dataCommit'], CURRENT_DATA)
            self.assertEqual(list(source.iterdir()), [])

    def test_run_unions_published_and_restored_digest_history_without_refreshing_original_times(self):
        state = restore_fake(FakeAPI(bundle(True)))
        quarter = '2026Q2'
        # Use two eligible original endpoints while changing only the actual facts.
        from test_verify_price_conflicts import urls
        newer = registry([group(1, 2), group(.7, 2.4, urls()[1])])
        plan = conflicts.plan_rechecks(newer, now=NOW)
        execution = conflicts.execute_rechecks(plan, newer, checkpoint=lambda _: None, fetch=lambda _: {}, now=NOW)
        extra = conflicts.merge_receipts(newer, None, plan, execution['receipts'], now=NOW)
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / 'source', Path(temporary) / 'out'
            conflicts.write_json(extra, source / quarter / 'price-conflict-ledger.json')
            before = (source / quarter / 'price-conflict-ledger.json').read_bytes()
            with patch.dict(os.environ, env()), patch.object(conflicts, 'scan_registry', side_effect=conflicts.ConflictError('CONFLICT_SCAN_FAILED')):
                with self.assertRaises(conflicts.ConflictError): conflicts.run(source, output, restored_state=state, now=NOW, fetch=lambda _: {})
            combined = conflicts.read_json(output / quarter / 'input-ledger.json')
            self.assertEqual(combined, conflicts.union_ledgers(extra, state['ledgers'][quarter], quarter, NOW))
            self.assertEqual(len(combined['entries'][0]['digests']), 2)
            self.assertEqual((source / quarter / 'price-conflict-ledger.json').read_bytes(), before)

    def test_restored_second_round_unknown_checkpoint_survives_failure_and_exhausts_budget(self):
        state = restore_fake(FakeAPI(bundle(True)))
        class Interrupted(BaseException): pass
        def stop(_): raise Interrupted()
        later = NOW + dt.timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / 'source', Path(temporary) / 'out'; source.mkdir()
            with patch.dict(os.environ, env()), patch.object(conflicts, 'scan_registry', side_effect=lambda _, q, now: registry(quarter=q)):
                with self.assertRaises(Interrupted): conflicts.run(source, output, restored_state=state, now=later, fetch=stop)
            ledger = conflicts.read_json(output / '2026Q2/ledger.json')
            rounds = ledger['entries'][0]['digests'][0]['rounds']
            self.assertEqual(len(rounds), 2); self.assertTrue(all(r['requests'][0]['outcome'] == 'unknown' for r in rounds))
            self.assertEqual(conflicts.plan_rechecks(registry(), ledger, now=later)['items'], [])
            self.assertEqual(conflicts.read_json(output / '2026Q3/ledger.json'), state['ledgers']['2026Q3'])
            self.assertEqual(list(source.iterdir()), [])

    def test_workflow_requires_explicit_parent_serializes_and_uploads_failed_checkpoints(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/recheck-price-conflicts.yml').read_text()
        for name in ('prior_run_id', 'prior_source_commit', 'prior_data_commit'):
            section = workflow.split('      ' + name + ':', 1)[1].split('        type:', 1)[0]
            self.assertIn('required: true', section)
        self.assertIn('actions: read', workflow); self.assertIn('cancel-in-progress: false', workflow)
        self.assertIn('if: always()', workflow); self.assertIn('--restore-state .cache/price-conflicts-restore/state.json', workflow)
        self.assertIn('name: price-conflict-ledger-verification-${{ github.run_id }}-${{ github.run_attempt }}', workflow)
        self.assertIn('"$DATA_COMMIT_INPUT" =~ ^[a-f0-9]{40}$', workflow)
        self.assertIn('"$data_sha" != "$DATA_COMMIT_INPUT"', workflow)
        self.assertNotIn('contents: write', workflow); self.assertNotIn('git push', workflow)

    def test_sparse_restore_cli_has_no_extra_dependency_and_never_fetches_outside_actions(self):
        source = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('scripts', 'config'):
                shutil.copytree(source / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
            process = subprocess.run([sys.executable, 'scripts/restore_price_conflict_ledger.py', '--prior-run-id', str(PRIOR),
                                      '--prior-source-commit', SOURCE, '--prior-data-commit', DATA,
                                      '--data-commit', CURRENT_DATA, '--output', 'out/state.json'], cwd=root,
                                     env={**os.environ, 'GITHUB_ACTIONS': 'false'}, capture_output=True, text=True)
            self.assertEqual(process.returncode, 1, process.stderr)
            self.assertEqual(json.loads(process.stdout)['errorCode'], 'CONFLICT_RESTORE_REQUIRES_ACTIONS')
            self.assertEqual(process.stderr, ''); self.assertFalse((root / 'out').exists())
            self.assertNotIn(str(root), process.stdout)


if __name__ == '__main__': unittest.main()
