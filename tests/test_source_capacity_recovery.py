"""Synthetic expanded source plans retain bounded recovery admission."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import resume_traversal as resume
from merge_receipt import read_merge_receipt, MAX_FUNDS, MAX_PAGES, MAX_TOTAL_BYTES
from test_resume_traversal import API, identity


def source_plan():
    matrix = [{'id': str(i), 'config': f'batch-{i}.json', 'quarter': '2026Q3'} for i in range(64)]
    plan = {'matrix.json': resume.encoded(matrix), 'meta.json': b'{}'}
    for row in matrix:
        plan[row['config']] = resume.encoded({'quarter': row['quarter'], 'funds': [],
            'collectionLane': 'source-repair-no-market-v1', 'maxPriceSymbols': 0, 'maxPriceSeconds': 0,
            'traversal': {'batchId': row['id'], 'enrichmentCodes': []}})
    campaign = {'selection.json': resume.encoded({'quarters': ['2026Q3'], 'sourceRepair': True}),
                '2026Q3.json': b'{}', 'catalogs/2026Q3.json': b'{}', 'locators/2026Q3.json': b'{}'}
    return matrix, plan, campaign


class PagedAPI(API):
    def __init__(self):
        self.run, self.env = identity()
        self.calls, self.artifacts, self.archives = [], [], {}
        self.matrix, plan, campaign = source_plan()
        self.part = b'{ "retrievedAt":"2026-10-29T00:01:02.123456Z", "funds":[] }\n'
        self.add('plan', plan); self.add('campaign', campaign)
        self.add('directory', {'catalog.json': b'{"asOf":"2026-10-28","funds":[]}'})
        for row in self.matrix:
            self.add(f"part-{row['id']}", {'part.json': self.part, 'status.json': b'{}'})
            self.add(f"request-accounting-123-1-{row['id']}", {'receipts.json': b'{}'})

    def get(self, path, binary=False):
        if '/artifacts?' in path:
            self.calls.append(path)
            offset = 100 if path.endswith('&page=2') else 0
            return {'total_count': len(self.artifacts), 'artifacts': self.artifacts[offset:offset + 100]}
        if '/jobs?' in path:
            self.calls.append(path)
            return {'total_count': 64, 'jobs': [{'name': f"batch ({r['id']}, {r['config']}, 2026Q3)",
                                                'conclusion': 'success'} for r in self.matrix]}
        return super().get(path, binary)


class SourceCapacityRecoveryTests(unittest.TestCase):
    def test_64_parts_restore_from_two_pages_with_exact_bytes_and_allowlisted_downloads(self):
        api = PagedAPI(); api.add('trusted-history-cache', {'unused.json': b'{}'})
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = resume.resume('123', 'a' * 40, root=root, api=api, env=api.env,
                                   execute=lambda *a, **kw: calls.append((a, kw)))
            self.assertEqual(len(result['artifacts']), 131)
            self.assertEqual(len(calls), 1)
            self.assertEqual((root / '.cache/parts/part-63/part.json').read_bytes(), api.part)
            self.assertEqual(api.calls.count('actions/runs/123/artifacts?per_page=100&page=2'), 1)
            self.assertNotIn('actions/artifacts/132/zip', api.calls)
            self.assertTrue((root / 'web/data/traversal-resumptions/123-1.json').exists())

    def test_inventory_rejects_duplicate_ids_names_incomplete_pages_changed_totals_and_third_page(self):
        for mode in ('id', 'name', 'short', 'total', 'limit'):
            api = PagedAPI()
            if mode in ('id', 'name'): api.artifacts[-1][mode] = api.artifacts[0][mode]
            original = api.get
            def get(path, binary=False):
                result = original(path, binary)
                if '/artifacts?' not in path: return result
                if mode == 'limit': result['total_count'] = 201
                if path.endswith('&page=2'):
                    if mode == 'short': result['artifacts'] = result['artifacts'][:-1]
                    if mode == 'total': result['total_count'] += 1
                return result
            api.get = get
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'ARTIFACT_LIST_INVALID'):
                resume.artifact_inventory(api, '123')
            self.assertFalse(any('&page=3' in call for call in api.calls))

    def test_last_receipt_digest_fails_before_merge_and_marker(self):
        api = PagedAPI(); api.artifacts[-1]['digest'] = 'sha256:' + '0' * 64
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'ARCHIVE_IDENTITY_INVALID'):
                resume.resume('123', 'a' * 40, root=Path(directory).resolve(), api=api, env=api.env,
                              execute=lambda *a, **kw: self.fail('unverified merge'))
            self.assertFalse((Path(directory) / 'web/data/traversal-resumptions/123-1.json').exists())

    def test_64_plan_requires_source_proof_in_every_config_and_selection(self):
        matrix, plan, campaign = source_plan()
        self.assertEqual(resume.verify_matrix(plan, campaign), matrix)
        for field, value in [('collectionLane', 'ordinary'), ('maxPriceSymbols', 1), ('maxPriceSeconds', 1),
                             ('funds', [{'code': '000001', 'priceOnly': True}]),
                             ('traversal', {'batchId': '63', 'enrichmentCodes': ['000001']})]:
            bad = dict(plan); config = json.loads(bad['batch-63.json']); config[field] = value
            bad['batch-63.json'] = resume.encoded(config)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'PLAN_INVALID'):
                resume.verify_matrix(bad, campaign)
        with self.assertRaisesRegex(ValueError, 'PLAN_INVALID'):
            resume.verify_matrix(plan, {**campaign, 'selection.json': b'{"quarters":["2026Q3"]}'})

    def test_invalid_last_source_config_cannot_create_partial_statuses(self):
        _, plan, _ = source_plan()
        config = json.loads(plan['batch-63.json']); config.pop('collectionLane')
        plan['batch-63.json'] = resume.encoded(config)
        spec = importlib.util.spec_from_file_location('capacity_complete_status', ROOT / 'scripts/complete-status.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); resume.stage(root / 'plan', plan)
            with self.assertRaisesRegex(ValueError, 'INVALID_PLAN'):
                module.reconcile(root / 'plan', root / 'parts')
            self.assertFalse((root / 'parts').exists())

    def test_4800_receipt_rows_roundtrip_between_javascript_and_python_with_unchanged_byte_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'merge-receipt.json'
            script = """
const {writeMergeReceipt}=require('./lib/merge-receipt');
const codes=Array.from({length:4800},(_,i)=>String(i+1).padStart(6,'0'));
writeMergeReceipt(process.argv[1], {schemaVersion:1,phase:{targetPeriodEnd:'2026-09-30',baselinePeriodEnd:'2026-06-30'},
 generatedAt:'2026-10-29T00:01:02.123456Z',requestedCodes:codes,
 funds:codes.map(code=>({code,observedAt:'2026-10-28T12:34:56.123456Z'}))}, {legacyLimit:1,pageLimit:100000});
"""
            subprocess.run(['node', '-e', script, str(path)], cwd=ROOT, check=True, capture_output=True)
            result = read_merge_receipt(path, read_json=lambda p: json.loads(Path(p).read_bytes()))
            self.assertEqual(len(result['funds']), 4800)
            self.assertEqual(result['funds'][-1], {'code': '004800', 'observedAt': '2026-10-28T12:34:56.123456Z'})
            self.assertEqual(result['generatedAt'], '2026-10-29T00:01:02.123456Z')
            self.assertEqual((MAX_FUNDS, MAX_PAGES, MAX_TOTAL_BYTES), (8192, 64, 512000000))


if __name__ == '__main__': unittest.main()
