"""Synthetic retained funds only; no source, market, PDF or model requests."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import profile_stored_coverage as profiler

NOW = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)


def sleeping_worker(send, _root, _now):
    time.sleep(2)
    send.close()


def unsafe_worker(send, _root, now):
    value = profiler.base_summary(profiler.traversal.utc_now(now))
    value.update(status='complete', rawException='/private/hidden/example SECRET')
    send.send_bytes(json.dumps(value).encode()); send.close()


class StoredCoverageProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = "const f=require('./tests/price-reference-fixture');console.log(JSON.stringify({fund:f.snapshot('000001','yahoo').funds[0],data:f.fixture()}));"
        cls.fixture = json.loads(subprocess.check_output(['node', '-e', source], cwd=ROOT, text=True))

    def fund(self, code, *, reference=False):
        fund = copy.deepcopy(self.fixture['fund']); fund['code'] = code
        fund['selectedReport']['fundCode'] = code
        if reference:
            fund['rows'][0]['priceRange'].update(rangeReference=copy.deepcopy(self.fixture['data']['reference']),
                                               globalPriceConflict=copy.deepcopy(self.fixture['data']['witness']))
        return fund

    def corpus(self, root, pages):
        folder = Path(root) / 'web/data/periods/2026Q2'; folder.mkdir(parents=True)
        (folder / 'companies').mkdir()
        manifest = {**profiler.PHASE, 'companyFiles': []}
        for index, funds in enumerate(pages):
            name = f'page-{index}.json'
            (folder / 'companies' / name).write_text(json.dumps({'funds': funds}))
            manifest['companyFiles'].append({'companyCode': f'co{index}', 'path': './data/companies/' + name})
        (folder / 'latest.json').write_text(json.dumps(manifest))
        return folder

    def test_selection_bounds_manifest_only_and_reference_preference(self):
        with tempfile.TemporaryDirectory() as root:
            pages = [[self.fund(f'{index * 4 + offset + 1:06d}', reference=index == 19 and offset == 3)
                      for offset in range(4)] for index in range(21)]
            folder = self.corpus(root, pages)
            (folder / 'companies/unlisted.json').write_text('must not be opened')
            with patch.object(profiler, 'read_input', wraps=profiler.read_input) as read:
                samples, counts, identity = profiler.select_samples(root, NOW)
            self.assertEqual(counts['manifestPages'], 21); self.assertEqual(counts['scannedPages'], 20)
            self.assertEqual(counts['scannedFundRecords'], 80); self.assertEqual(counts['sampledFunds'], 64)
            self.assertEqual(samples[0]['code'], '000080'); self.assertEqual(counts['referenceRowCount'], 1)
            self.assertEqual(counts['referenceFactCount'], 2); self.assertEqual(counts['witnessFactCount'], 2)
            self.assertEqual(len(read.call_args_list), 21)
            self.assertTrue(all(Path(call.args[0]).name not in {'page-20.json', 'unlisted.json'} for call in read.call_args_list))
            self.assertEqual(profiler.select_samples(root, NOW)[2], identity)
            self.assertNotEqual(profiler.select_samples(root, NOW + dt.timedelta(seconds=1))[2], identity)

    def test_rejects_manifest_escape_duplicate_paths_and_symlink(self):
        for mode in ('escape', 'duplicate', 'symlink'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                folder = self.corpus(root, [[self.fund('000001')]])
                file = folder / 'latest.json'; manifest = json.loads(file.read_text())
                if mode == 'escape': manifest['companyFiles'][0]['path'] = '../private.json'
                elif mode == 'duplicate': manifest['companyFiles'].append(manifest['companyFiles'][0])
                else:
                    page = folder / 'companies/page-0.json'; replacement = folder / 'safe.json'
                    page.rename(replacement); page.symlink_to(replacement)
                file.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(profiler.ProfileError, 'PROFILE_INPUT_INVALID'):
                    profiler.select_samples(root, NOW)

    def test_sample_byte_limits_are_visible_and_do_not_truncate_funds(self):
        with tempfile.TemporaryDirectory() as root:
            self.corpus(root, [[self.fund('000001'), self.fund('000002', reference=True)]])
            tiny_size = len(profiler.encoded(self.fund('000001')))
            with patch.object(profiler, 'MAX_FUND_BYTES', tiny_size + 1):
                funds, counts, _ = profiler.select_samples(root, NOW)
            self.assertEqual([fund['code'] for fund in funds], ['000001'])
            self.assertEqual(counts['oversizedFundRecords'], 1)
            with patch.object(profiler, 'MAX_SAMPLE_BYTES', tiny_size + 1):
                funds, counts, _ = profiler.select_samples(root, NOW)
            self.assertEqual(len(funds), 1); self.assertLessEqual(counts['sampledBytes'], tiny_size + 1)

    def test_read_only_guard_blocks_outbound_subprocess_and_writes_and_restores_apis(self):
        original_connection, original_open = socket.create_connection, os.open
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'unchanged'; file.write_bytes(b'original')
            with profiler.read_only_execution():
                self.assertEqual(file.read_bytes(), b'original')
                for action in (lambda: socket.create_connection(('example.org', 443)),
                               lambda: subprocess.run([sys.executable, '-c', 'pass'])):
                    with self.assertRaisesRegex(profiler.ProfileError, 'PROFILE_OUTBOUND_BLOCKED'): action()
                for action in (lambda: file.write_bytes(b'changed'), lambda: file.unlink(),
                               lambda: os.open(file, os.O_WRONLY)):
                    with self.assertRaisesRegex(profiler.ProfileError, 'PROFILE_WRITE_BLOCKED'): action()
            self.assertEqual(file.read_bytes(), b'original')
        self.assertIs(socket.create_connection, original_connection); self.assertIs(os.open, original_open)

    def test_fixed_clock_only_supplies_missing_original_api_clock(self):
        original = profiler.price_range_reference.effective_reference
        explicit = NOW + dt.timedelta(seconds=1)
        with patch.object(profiler.price_range_reference, 'effective_reference', return_value=None) as measured:
            with profiler.fixed_reference_clock(NOW):
                profiler.price_range_reference.effective_reference({})
                profiler.price_range_reference.effective_reference({}, now=explicit)
                profiler.price_range_reference.effective_reference({}, None, explicit)
            self.assertIs(measured.call_args_list[0].args[2], NOW)
            self.assertIs(measured.call_args_list[1].args[2], explicit)
            self.assertIs(measured.call_args_list[2].args[2], explicit)
        self.assertIs(profiler.price_range_reference.effective_reference, original)

    def test_tiny_known_profile_has_real_hotspots_and_only_aggregate_output(self):
        with tempfile.TemporaryDirectory() as root:
            folder = self.corpus(root, [[self.fund('000001', reference=True), self.fund('000002')]])
            before = {str(file): file.read_bytes() for file in folder.rglob('*.json')}
            with profiler.read_only_execution():
                result = profiler.profile_sample(root, NOW)
            raw = profiler.checked_output(result).decode()
            self.assertEqual(result['status'], 'complete'); self.assertEqual(result['counts']['sampledFunds'], 2)
            self.assertEqual([stage['stage'] for stage in result['stages']], ['coverage', 'quality'])
            for stage in result['stages']:
                self.assertEqual(stage['nonemptyResults'], 2)
                entry = next(row for row in stage['hotspots'] if row['module'] == 'traversal' and row['function'] == 'price_coverage')
                self.assertEqual(entry['calls'], 2); self.assertGreater(entry['cumulativeCpuSeconds'], 0)
            for value in (root, '000001', '600001', '合成基金', 'https://', 'sourceUrl', 'traceback'):
                self.assertNotIn(value, raw)
            self.assertEqual({str(file): file.read_bytes() for file in folder.rglob('*.json')}, before)

    def test_unknown_function_raw_fields_and_unbounded_counts_are_rejected(self):
        for mode in ('field', 'count', 'function'):
            with self.subTest(mode=mode):
                result = profiler.base_summary(NOW)
                if mode == 'field': result['rawFund'] = {'private': 'text'}
                elif mode == 'count': result['counts']['sampledFunds'] = 65
                else:
                    result['counts']['sampledFunds'] = 1
                    result['stages'] = [{'stage': 'coverage', 'sampledFunds': 1, 'processedFunds': 1, 'complete': True, 'nonemptyResults': 1,
                        'cpuSeconds': 0.1, 'wallSeconds': 0.1, 'hotspots': [{'module': 'traversal',
                        'function': '/private/raw', 'calls': 1, 'primitiveCalls': 1, 'selfCpuSeconds': 0.1, 'cumulativeCpuSeconds': 0.1}]}]
                with self.assertRaisesRegex(profiler.ProfileError, 'PROFILE_OUTPUT_INVALID'):
                    profiler.checked_output(result)

    def test_partial_profile_checkpoint_keeps_measured_counts_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as root:
            self.corpus(root, [[self.fund(f'{index + 1:06d}') for index in range(10)]])
            checkpoints = []
            result = profiler.profile_sample(root, NOW, lambda value: checkpoints.append(copy.deepcopy(value)))
            partial = [value for value in checkpoints if value['stages']
                       and value['stages'][-1]['processedFunds'] == 8]
            self.assertEqual(len(partial), 2)
            for value in partial:
                self.assertEqual(value['status'], 'running'); self.assertFalse(value['stages'][-1]['complete'])
                self.assertTrue(value['stages'][-1]['hotspots'])
                self.assertFalse(value['fullCollectionAndAnalysisComplete']); profiler.checked_output(value)
            self.assertEqual(result['status'], 'complete')

    def test_child_timeout_and_untrusted_output_have_only_fixed_classification(self):
        for target, expected in ((sleeping_worker, 'PROFILE_TIMEOUT'), (unsafe_worker, 'PROFILE_OUTPUT_INVALID')):
            with self.subTest(target=target.__name__):
                result = profiler.run('.', now=NOW, timeout_seconds=0.1 if target is sleeping_worker else 5, worker_target=target)
                self.assertEqual(result['errorCode'], expected)
                raw = profiler.checked_output(result).decode()
                self.assertNotIn('SECRET', raw); self.assertNotIn('/private/', raw)
        self.assertEqual(profiler.CHILD_SECONDS, 180)

    def test_real_child_uses_only_the_tiny_synthetic_corpus(self):
        with tempfile.TemporaryDirectory() as root:
            self.corpus(root, [[self.fund('000001', reference=True)]])
            result = profiler.run(root, now=NOW, timeout_seconds=10)
            self.assertEqual(result['status'], 'complete', result)
            self.assertEqual(result['counts']['referenceFactCount'], 2)
            profiler.checked_output(result)

    def test_cli_requires_actions_before_any_input_and_workflow_is_independent_read_only(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(profiler, 'run') as run, patch('builtins.print'):
            self.assertEqual(profiler.main(), 1); run.assert_not_called()
        workflow = (ROOT / '.github/workflows/profile-stored-coverage.yml').read_text()
        self.assertIn('timeout-minutes: 10', workflow); self.assertIn('contents: read', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('/web/*.js', workflow)
        self.assertNotIn('published-records', workflow); self.assertNotIn('contents: write', workflow)
        self.assertNotIn('secrets.', workflow); self.assertNotIn('workflow run', workflow)
        self.assertNotIn('persist-observations', workflow)

    def test_cli_failure_never_prints_arbitrary_exception_or_path(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), \
                patch.object(profiler, 'run', side_effect=RuntimeError('/private/hidden SECRET')), patch('builtins.print') as output:
            self.assertEqual(profiler.main(), 1)
        self.assertEqual(json.loads(output.call_args.args[0]), {'status': 'failed', 'errorCode': 'PROFILE_OUTPUT_INVALID'})


if __name__ == '__main__': unittest.main()
