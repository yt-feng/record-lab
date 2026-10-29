"""Deploy only referenced partitions without pruning or rewriting the source."""
import gzip
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
            write(root / 'data/periods.json', {'periods': [{'id': '2026Q2'}, {'id': '2026Q3'}]})
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
