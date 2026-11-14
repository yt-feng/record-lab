"""Deploy only referenced partitions without pruning or rewriting the source."""
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('prepare_site_partition_tests', ROOT / 'scripts/prepare-site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)


def write(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(value if isinstance(value, bytes) else (json.dumps(value) + '\n').encode())


def tree(root):
    return {file.relative_to(root).as_posix(): file.read_bytes() for file in root.rglob('*') if file.is_file()}


class PrepareSitePartitionTests(unittest.TestCase):
    def accounting_scope(self, root, quarter='2026Q3', outcome='returned', count=200):
        import campaign_accounting as accounting
        import request_receipts as receipts
        target, baseline, start = ('2026-06-30', '2026-03-31', '2026-04-01') if quarter == '2026Q2' else (
            '2026-09-30', '2026-06-30', '2026-07-01')
        phase = {'targetPeriodEnd': target, 'baselinePeriodEnd': baseline}
        config = {'quarter': quarter, **phase, 'funds': [{'code': '000001'}],
            'traversal': {'phase': phase, 'batchId': '0', 'plannedAt': '2026-10-26T00:00:00Z'}}
        env = {'GITHUB_RUN_ID': '100', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a' * 40, 'RECEIPT_BATCH_ID': '0'}
        capture = root / 'capture'
        write(capture / 'plan/batch-0.json', config)
        config_raw = (capture / 'plan/batch-0.json').read_bytes()
        journal = receipts.RequestJournal(capture / 'private', config, config_raw, env)
        for index in range(count):
            code = str(600000 + index)
            observer = journal.observer('CN:' + code, (code, 'CN', start, target))
            token = observer.begin('https://push2his.eastmoney.com/api/qt/stock/kline/get')
            if outcome != 'unknown' or index != count - 1: observer.finish(token, 'returned')
        if outcome != 'unknown': journal.close(True)
        receipt = receipts.encoded(receipts.validate(capture / 'private', config, config_raw, env))
        write(capture / 'receipts/request-accounting-100-1-0/receipts.json', receipt)
        matrix = [{'id': '0', 'config': 'batch-0.json', 'quarter': quarter}]
        rows = accounting.collect_batch_accounting(matrix, capture / 'plan', capture / 'receipts', env)[quarter]
        folder = root / 'web/data/periods' / quarter
        accounting.persist_accounting(folder, quarter, rows)
        return folder, receipt

    def test_accounting_receipts_fit_by_compression_and_round_trip_hashes_clocks_and_unknowns(self):
        for quarter in ('2026Q2', '2026Q3'):
            for outcome in ('returned', 'unknown'):
                with self.subTest(quarter=quarter, outcome=outcome), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    folder, receipt = self.accounting_scope(root, quarter, outcome)
                    source, output = root / 'web', root / 'site'
                    before = tree(source)
                    original = json.loads((folder / 'market-accounting.json').read_bytes())
                    result = site.prepare(source, output, max_bytes=len(receipt) // 2)
                    self.assertEqual(tree(source), before)
                    self.assertEqual(result['compressedAccountingFiles'], 1)
                    packed_folder = output / 'data/periods' / quarter
                    packed = json.loads((packed_folder / 'market-accounting.json').read_bytes())
                    row = packed['entries'][0]
                    self.assertEqual(row['receiptPath'], original['entries'][0]['receiptPath'] + '.gz')
                    self.assertFalse((packed_folder / original['entries'][0]['receiptPath']).exists())
                    restored = gzip.decompress((packed_folder / row['receiptPath']).read_bytes())
                    self.assertEqual(restored, receipt)
                    self.assertEqual(hashlib.sha256(restored).hexdigest(), row['receiptSha256'])
                    row['receiptPath'] = row['receiptPath'][:-3]
                    self.assertEqual(packed, original)
                    self.assertEqual(row['counts']['unknown'], 1 if outcome == 'unknown' else 0)
                    self.assertEqual(row['status'], 'incomplete' if outcome == 'unknown' else 'complete')

    def test_accounting_missing_changed_or_cross_scope_proofs_fail_before_output(self):
        for damage in ('missing', 'changed', 'quarter', 'path'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                folder, receipt = self.accounting_scope(root, count=1)
                index = folder / 'market-accounting.json'
                value = json.loads(index.read_bytes())
                proof = folder / value['entries'][0]['receiptPath']
                if damage == 'missing': proof.unlink()
                elif damage == 'changed': proof.write_bytes(receipt + b' ')
                elif damage == 'quarter': value['quarter'] = '2026Q2'; write(index, value)
                else: value['entries'][0]['receiptPath'] = '../2026Q2/market-accounting/100-1-0.json'; write(index, value)
                before, output = tree(root / 'web'), root / 'site'
                with self.assertRaisesRegex(ValueError, 'INVALID_SITE_ACCOUNTING'):
                    site.prepare(root / 'web', output)
                self.assertFalse(output.exists())
                self.assertEqual(tree(root / 'web'), before)

    def test_accounting_unresolved_entries_and_unindexed_files_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder, _ = self.accounting_scope(root, count=1)
            index = folder / 'market-accounting.json'
            original = json.loads(index.read_bytes())
            row = original['entries'][0]
            (folder / row['receiptPath']).unlink()
            row.update(status='missing', counts=None, receiptPath=None, receiptSha256=None, errorCode='MARKET_RECEIPT_MISSING')
            invalid = {**row, 'batchId': '1', 'status': 'invalid', 'errorCode': 'MARKET_RECEIPT_INVALID'}
            original['entries'].append(invalid)
            write(index, original)
            write(folder / 'market-accounting/legacy.json', {'unindexedPublicRecord': True})
            before = tree(root / 'web')
            result = site.prepare(root / 'web', root / 'site')
            packed_folder = root / 'site/data/periods/2026Q3'
            self.assertEqual(result['compressedAccountingFiles'], 0)
            self.assertEqual(json.loads((packed_folder / 'market-accounting.json').read_bytes()), original)
            self.assertEqual((packed_folder / 'market-accounting/legacy.json').read_bytes(),
                (folder / 'market-accounting/legacy.json').read_bytes())
            self.assertEqual(tree(root / 'web'), before)

    def scope(self, folder, label):
        company = {'retrievedAt': '2026-10-28T10:00:00.123456Z', 'funds': [{'code': '000001', 'scope': label}]}
        write(folder / 'companies/co1-current.json', company)
        write(folder / 'latest.json', {'retrievedAt': company['retrievedAt'], 'companyFiles': [
            {'path': './data/companies/co1-current.json'}]})
        write(folder / 'analysis/details-0007.json', {'stocks': [label], 'originalClock': company['retrievedAt']})
        write(folder / 'analysis/details-portfolios-0012.json', {'portfolios': [label]})
        # Reused older part numbers and a shared detail file are intentional.
        write(folder / 'analysis.json', {'analysisVersion': 6, 'stocks': [
            {'key': 'CN:600001', 'details': ['./analysis/details-0007.json']},
            {'key': 'CN:600002', 'details': ['./analysis/details-0007.json']}],
            'portfolioFiles': [{'path': './analysis/details-portfolios-0012.json', 'ids': ['one']}],
            'retrievedAt': company['retrievedAt']})
        historical = [folder / 'companies/co1-rhistorical.json', folder / 'analysis/details-9999.json']
        for file in historical: write(file, {'historical': label, 'payload': 'abc0123456789' * 10000})
        return historical

    def test_root_legacy_and_both_quarters_pack_only_their_current_references_without_reading_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory) / 'web', Path(directory) / 'site'
            historical = []
            for name in ('data', 'data/periods/2026Q2', 'data/periods/2026Q3'):
                historical.extend(self.scope(root / name, name))
            write(root / 'index.html', b'<script src="./app.js"></script>')
            write(root / 'app.js', b'const publicExample = true;\n')
            write(root / 'data/audit.json', {'continuationHold': {'status': 'unknown'}, 'updatedAt': '2026-10-28T10:00:00Z'})
            before = tree(root); original_read = Path.read_bytes
            def read(file):
                if file in historical: self.fail('Historical partition content was read')
                return original_read(file)
            with patch.object(Path, 'read_bytes', read): result = site.prepare(root, output, max_bytes=10000)
            self.assertEqual(tree(root), before)
            self.assertEqual(result['selectedPartitionFiles'], 9)
            self.assertEqual(result['skippedHistoricalPartitionFiles'], 6)
            self.assertEqual(result['skippedSourceBytes'], sum(len(before[file.relative_to(root).as_posix()]) for file in historical))
            self.assertLess(result['bytes'], 10000)
            self.assertGreater(result['skippedSourceBytes'], 700000)
            for name in ('data', 'data/periods/2026Q2', 'data/periods/2026Q3'):
                folder = output / name
                manifest = json.loads((folder / 'latest.json').read_text())
                self.assertEqual(manifest['retrievedAt'], '2026-10-28T10:00:00.123456Z')
                self.assertEqual(manifest['companyFiles'][0]['path'], './data/companies/co1-current.json.gz')
                for part in ('companies/co1-current.json', 'analysis/details-0007.json', 'analysis/details-portfolios-0012.json'):
                    self.assertEqual(gzip.decompress((folder / (part + '.gz')).read_bytes()), before[name + '/' + part])
                analysis = json.loads((folder / 'analysis.json').read_text())
                self.assertEqual(analysis['stocks'][0]['details'], ['./analysis/details-0007.json.gz'])
                self.assertEqual(analysis['stocks'][1]['details'], analysis['stocks'][0]['details'])
                self.assertEqual(analysis['portfolioFiles'][0]['path'], './analysis/details-portfolios-0012.json.gz')
            for file in historical: self.assertFalse((output / (file.relative_to(root).as_posix() + '.gz')).exists())
            for name in ('index.html', 'app.js', 'data/audit.json'):
                self.assertEqual((output / name).read_bytes(), before[name])

    def test_partition_directory_without_a_manifest_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory) / 'web', Path(directory) / 'site'
            for part in ('data/companies/legacy.json', 'data/analysis/details-123.json'):
                write(root / part, {'legacy': True})
            write(root / 'data/analysis/metadata.json', {'publicMetadata': True})
            result = site.prepare(root, output)
            self.assertEqual(result['skippedHistoricalPartitionFiles'], 0)
            self.assertEqual(result['files'], 3)
            for file in root.rglob('*.json'):
                self.assertEqual(gzip.decompress((output / (file.relative_to(root).as_posix() + '.gz')).read_bytes()), file.read_bytes())

    def test_quarter_metadata_fits_the_package_and_round_trips_all_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory) / 'web', Path(directory) / 'site'
            periods = {'schemaVersion': 1, 'defaultPeriod': '2026Q3', 'periods': []}
            for quarter in ('2026Q2', '2026Q3'):
                folder = root / 'data/periods' / quarter
                self.scope(folder, quarter)
                periods['periods'].append({'id': quarter, 'path': f'./data/periods/{quarter}/'})
                for name in ('catalog.json', 'progress.json'):
                    write(folder / name, {'quarter': quarter, 'retrievedAt': '2026-10-28T10:00:00.123456Z',
                        'continuationHold': {'reason': 'BATCH_ARTIFACTS_UNVERIFIED'},
                        'funds': [{'code': str(index), 'label': '原始记录与精确时点' * 10} for index in range(2000)]})
            write(root / 'data/periods.json', periods)
            write(root / 'data/catalog.json', {'legacy': True})
            before = tree(root)
            result = site.prepare(root, output, max_bytes=100_000)
            self.assertEqual(tree(root), before)
            self.assertEqual(result['compressedMetadataFiles'], 4)
            self.assertLess(result['bytes'], 100_000)
            packed_periods = json.loads((output / 'data/periods.json').read_text())
            for period in packed_periods['periods']:
                self.assertEqual(period['compressedMetadata'], ['catalog.json', 'progress.json'])
                for name in period['compressedMetadata']:
                    relative = f"data/periods/{period['id']}/{name}"
                    self.assertFalse((output / relative).exists())
                    self.assertEqual(gzip.decompress((output / (relative + '.gz')).read_bytes()), before[relative])
            self.assertEqual((output / 'data/catalog.json').read_bytes(), before['data/catalog.json'])
            with self.assertRaisesRegex(ValueError, 'SITE_SIZE_EXCEEDED'):
                site.prepare(root, Path(directory) / 'too-small', max_bytes=1)

    def test_metadata_quarters_cannot_redirect_to_another_scope(self):
        for period in ({'id': '../2026Q3', 'path': './data/periods/2026Q3/'},
                       {'id': '2026Q3', 'path': './data/periods/2026Q2/'}):
            with self.subTest(period=period), tempfile.TemporaryDirectory() as directory:
                root, output = Path(directory) / 'web', Path(directory) / 'site'
                write(root / 'data/periods.json', {'schemaVersion': 1, 'defaultPeriod': '2026Q3', 'periods': [period]})
                with self.assertRaisesRegex(ValueError, 'INVALID_SITE_PERIODS'): site.prepare(root, output)
                self.assertFalse(output.exists())

    def test_duplicate_references_within_an_owner_fail_before_any_output(self):
        for kind in ('company', 'stock', 'portfolio'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root, output = Path(directory) / 'web', Path(directory) / 'site'
                self.scope(root / 'data', 'root')
                file = root / 'data' / ('latest.json' if kind == 'company' else 'analysis.json')
                value = json.loads(file.read_text())
                items = value['companyFiles'] if kind == 'company' else value['stocks'][0]['details'] if kind == 'stock' else value['portfolioFiles']
                items.append(items[0]); write(file, value)
                with self.assertRaisesRegex(ValueError, 'INVALID_.*_PATH'): site.prepare(root, output)
                self.assertFalse(output.exists())

    def test_all_scope_paths_are_validated_before_pack_and_cannot_borrow_another_quarters_part(self):
        for kind in ('company', 'analysis'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root, output = Path(directory) / 'web', Path(directory) / 'site'
                self.scope(root / 'data/periods/2026Q2', 'Q2')
                self.scope(root / 'data/periods/2026Q3', 'Q3')
                suffix = 'companies/co1-current.json' if kind == 'company' else 'analysis/details-0007.json'
                (root / 'data/periods/2026Q3' / suffix).unlink()
                with self.assertRaisesRegex(ValueError, 'MISSING_.*_PART'): site.prepare(root, output)
                self.assertFalse(output.exists())

    def test_out_of_scope_absolute_or_already_compressed_references_are_rejected(self):
        for name, field, value in (
            ('latest.json', 'companyFiles', './data/companies/../../elsewhere.json'),
            ('latest.json', 'companyFiles', '/data/companies/co1-current.json'),
            ('latest.json', 'companyFiles', './data/companies/co1-current.json.gz'),
            ('analysis.json', 'portfolioFiles', './analysis/../details-0007.json'),
            ('analysis.json', 'portfolioFiles', './analysis/details-portfolios-0012.json.gz')):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root, output = Path(directory) / 'web', Path(directory) / 'site'
                self.scope(root / 'data', 'root')
                file = root / 'data' / name; body = json.loads(file.read_text()); body[field][0]['path'] = value; write(file, body)
                with self.assertRaisesRegex(ValueError, 'INVALID_.*_PATH'): site.prepare(root, output)
                self.assertFalse(output.exists())

    def test_symlinks_fail_even_when_the_path_would_be_unreferenced_history(self):
        for kind in ('current', 'historical', 'directory', 'root'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root, output = Path(directory) / 'web', Path(directory) / 'site'
                self.scope(root / 'data', 'root')
                if kind in ('current', 'historical'):
                    name = 'co1-current.json' if kind == 'current' else 'co1-rhistorical.json'
                    file = root / 'data/companies' / name; file.unlink(); file.symlink_to(root / 'data/latest.json')
                elif kind == 'directory': (root / 'unused').symlink_to(root / 'data/companies', target_is_directory=True)
                else:
                    alias = Path(directory) / 'alias'; alias.symlink_to(root, target_is_directory=True); root = alias
                with self.assertRaisesRegex(ValueError, 'SITE_SYMLINK'): site.prepare(root, output)
                self.assertFalse(output.exists())

    def test_non_partition_files_are_not_hidden_and_output_cannot_modify_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'web'; self.scope(root / 'data', 'root')
            write(root / 'data/companies/leftover.txt', b'not a supported public partition')
            with self.assertRaisesRegex(ValueError, 'SITE_FILE_TYPE'): site.prepare(root, Path(directory) / 'site')
            with self.assertRaisesRegex(ValueError, 'SITE_OUTPUT_INSIDE_SOURCE'): site.prepare(root, root / 'packed')
            self.assertFalse((root / 'packed').exists())

    def test_analyses_only_updates_stale_other_quarter_before_packing_without_source_clock_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory) / 'web', Path(directory) / 'site'
            write(root / 'data/periods.json', {'schemaVersion': 1, 'defaultPeriod': '2026Q3',
                'periods': [{'id': quarter, 'path': f'./data/periods/{quarter}/'} for quarter in ('2026Q2', '2026Q3')]})
            originals = {}
            for quarter, target, baseline in (('2026Q2', '2026-06-30', '2026-03-31'), ('2026Q3', '2026-09-30', '2026-06-30')):
                folder = root / 'data/periods' / quarter
                latest = {'targetPeriodEnd': target, 'baselinePeriodEnd': baseline,
                    'retrievedAt': '2026-10-28T10:00:00.123456Z', 'priceUpdatedAt': None, 'companyFiles': []}
                write(folder / 'latest.json', latest)
                originals[folder / 'latest.json'] = (folder / 'latest.json').read_bytes()
                analysis = {**latest, 'kind': 'quarter-analysis', 'analysisVersion': 8,
                    'priceReferenceVersion': 1, 'stocks': [], 'portfolioFiles': []}
                if quarter == '2026Q2': analysis.pop('priceReferenceVersion')
                write(folder / 'analysis.json', analysis)
            invoked = []
            def rebuild(command, check):
                self.assertTrue(check)
                folder = Path(command[command.index('--data') + 1]); invoked.append(folder.name)
                latest = json.loads((folder / 'latest.json').read_text())
                write(folder / 'analysis.json', {**latest, 'kind': 'quarter-analysis', 'analysisVersion': 8,
                    'priceReferenceVersion': 1, 'stocks': [], 'portfolioFiles': []})
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(site.subprocess, 'run', side_effect=rebuild), \
                    patch.object(site, 'prepare', side_effect=AssertionError('Analyses-only must not pack')), patch('sys.stdout', io.StringIO()) as output_log:
                self.assertEqual(site.main(['--source', str(root), '--output', str(output), '--analyses-only']), 0)
            self.assertEqual(invoked, ['2026Q2'])
            self.assertEqual(json.loads(output_log.getvalue()), {'status': 'analyses_ready', 'rebuiltPeriods': 1})
            self.assertFalse(output.exists())
            for file, original in originals.items(): self.assertEqual(file.read_bytes(), original)
            # After the analysis changes are persisted, ordinary packing must
            # see them as current and leave every source byte unchanged.
            before_pack = tree(root)
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(site.subprocess, 'run') as run, patch('sys.stdout', io.StringIO()):
                self.assertEqual(site.main(['--source', str(root), '--output', str(output)]), 0)
            run.assert_not_called()
            self.assertEqual(tree(root), before_pack)
            self.assertTrue(output.is_dir())

    def test_analyses_only_retains_the_actions_requirement(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch.object(site, 'prepare') as pack, patch('sys.stdout', io.StringIO()) as output:
            self.assertEqual(site.main(['--analyses-only']), 1)
        pack.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['errorCode'], 'SITE_PREPARATION_FAILED')

    def test_cli_reports_fixed_failure_categories_without_exception_details(self):
        for error, reason in ((ValueError('SITE_SIZE_EXCEEDED'), 'SITE_SIZE_EXCEEDED'),
                              (ValueError('INVALID_SITE_ACCOUNTING'), 'INVALID_SITE_ACCOUNTING'),
                              (OSError(28, 'private source details'), 'SITE_IO_FAILED'),
                              (ValueError('private source details'), None)):
            with self.subTest(reason=reason), patch.object(site, 'ensure_analyses', return_value=0), \
                    patch.object(site, 'prepare', side_effect=error), patch('sys.stdout', io.StringIO()) as output:
                self.assertEqual(site.main([]), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result.get('reasonCode'), reason)
            self.assertNotIn('private source details', output.getvalue())
            if isinstance(error, OSError): self.assertEqual(result['errno'], 28)

    def test_semantic_workflow_persists_all_analysis_changes_and_checks_source_after_pack(self):
        workflow = (ROOT / '.github/workflows/enrich-strategy-semantics.yml').read_text()
        target = workflow.index('run: node scripts/analyze-data.js --data')
        ensure = workflow.index('run: python scripts/prepare-site.py --analyses-only')
        guard = workflow.index('python scripts/publication_guard.py --root . --history')
        persist = workflow.index('python scripts/persist-observations.py', guard)
        readback = workflow.index('node scripts/strategy-semantic-production.js readback', persist)
        pack = workflow.index('          python scripts/prepare-site.py\n', readback)
        unchanged = workflow.index('git diff --exit-code -- web/data', pack)
        upload = workflow.index('actions/upload-pages-artifact@v4', unchanged)
        self.assertEqual(sorted((target, ensure, guard, persist, readback, pack, unchanged, upload)),
                         [target, ensure, guard, persist, readback, pack, unchanged, upload])
        self.assertNotIn('continue-on-error', workflow[pack:upload])


if __name__ == '__main__': unittest.main()
