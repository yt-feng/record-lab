"""Source-only capacity expands without larger batches or market work."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import campaign
import traversal
from test_campaign_history import current, workspace


def workflow_shards(quarter, source_repair=True):
    workflow = (ROOT / '.github/workflows/traverse.yml').read_text()
    step = workflow.split('- name: Plan isolated quarter batches', 1)[1]
    body = step.split('run: |\n', 1)[1]
    setup, command = body.split('python scripts/campaign.py plan', 1)
    if '--shards "$SHARDS" --batch-size 75' not in command.splitlines()[0]:
        raise AssertionError('Workflow does not pass the bounded shard count')
    result = subprocess.run(['bash', '-c', textwrap.dedent(setup) + '\nprintf "%s\\n" "$SHARDS"'],
        env={**os.environ, 'QUARTER': quarter, 'SOURCE_REPAIR': str(source_repair).lower()},
        capture_output=True, text=True, check=True)
    return int(result.stdout)


def held(quarter):
    target, baseline = campaign.PERIODS[quarter]
    return {'schemaVersion': 1, 'quarter': quarter,
        'phase': {'targetPeriodEnd': target, 'baselinePeriodEnd': baseline},
        'continuationHold': {'reason': 'BATCH_ARTIFACTS_UNVERIFIED', 'quarter': quarter,
            'sourceRunId': '123', 'batchIds': ['0'], 'requestAccounting': 'unverified'},
        'batchInputErrors': [{'batchId': '0', 'quarter': quarter,
                              'errorCode': 'BATCH_ARTIFACT_MISSING'}]}


class SingleQuarterCapacityTests(unittest.TestCase):
    def test_workflow_doubles_only_source_matrix_allowance(self):
        for source, total in [(False, 32), (True, 64)]:
            self.assertEqual(workflow_shards('both', source), total // 2)
            self.assertEqual(workflow_shards('2026Q2', source), total)
            self.assertEqual(workflow_shards('2026Q3', source), total)

    def test_q3_source_plan_emits_64_bounded_batches_and_keeps_holds(self):
        shards = workflow_shards('2026Q3')
        with workspace():
            directory = current()
            directory['funds'] = [{**directory['funds'][0], 'code': f'{index:06d}'}
                                  for index in range(1, 4801)]
            directory['fundCount'] = directory['coverage']['fundCount'] = 4800
            directory['companies'][0]['fundCount'] = 4800
            traversal.atomic_json('web/data/catalog.json', directory)
            originals = {}
            for quarter in campaign.PERIODS:
                path = Path('web/data/periods') / quarter / 'audit.json'
                traversal.atomic_json(path, held(quarter))
                originals[path] = path.read_bytes()
            with patch('campaign.refresh_directory') as refresh, \
                    patch('campaign.fetch_locator_results') as locators, \
                    patch('campaign.fetch_historical_overlay') as history:
                result = campaign.plan('2026Q3', '2026-10-29', shards=shards,
                    batch_size=75, refresh=False, historical_overlays={}, source_repair=True)
            refresh.assert_not_called(); locators.assert_not_called(); history.assert_not_called()
            self.assertEqual(result['batchCount'], 64)
            self.assertEqual(result['quarters'], ['2026Q3'])
            self.assertEqual(result['heldQuarters'], ['2026Q3'])
            matrix = traversal.read_json('.cache/plan/matrix.json')
            self.assertEqual([item['id'] for item in matrix], [str(index) for index in range(64)])
            codes = []
            for item in matrix:
                self.assertEqual(item['quarter'], '2026Q3')
                batch = traversal.read_json(Path('.cache/plan') / item['config'])
                self.assertTrue(traversal.source_repair_mode(batch))
                self.assertEqual(batch['traversal']['batchId'], item['id'])
                self.assertEqual(len(batch['funds']), 75)
                self.assertEqual((batch['maxPriceSymbols'], batch['maxPriceSeconds']), (0, 0))
                self.assertEqual(batch['maxWorkers'], 2)
                self.assertEqual(batch['maxBatchSeconds'], 1500)
                self.assertEqual(batch['strategyClaimsQuota'], 20)
                self.assertEqual(batch['traversal']['enrichmentCodes'], [])
                codes.extend(item['code'] for item in batch['funds'])
            self.assertEqual(len(codes), 4800)
            self.assertEqual(len(set(codes)), 4800)
            emitted = subprocess.run(['node', 'scripts/workflow-outputs.js', '.cache/plan'],
                                     capture_output=True, text=True, check=True)
            values = dict(line.split('=', 1) for line in emitted.stdout.splitlines())
            self.assertEqual(json.loads(values['matrix']), matrix)
            self.assertEqual(values['has_work'], 'true')
            selection = traversal.read_json('.cache/campaign/selection.json')
            self.assertTrue(selection['sourceRepair'])
            self.assertEqual(selection['strategyClaimsQuotas'], {'2026Q3': 20})
            self.assertEqual(selection['continuationHolds']['2026Q3'], held('2026Q3')['continuationHold'])
            for path, raw in originals.items():
                self.assertEqual(path.read_bytes(), raw)
            # Full 64-batch missing-part reconciliation is local and synthetic.
            status = subprocess.run([sys.executable, 'scripts/complete-status.py'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(status.stdout)['repairedCount'], 64)
            before = {p: p.read_bytes() for p in Path('.cache/parts').glob('*/status.json')}
            repeat = subprocess.run([sys.executable, 'scripts/complete-status.py'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(repeat.stdout)['preservedCount'], 64)
            self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_larger_ordinary_and_oversized_source_plans_reject_before_io(self):
        for quarter, shards, source in [('both', 17, False), ('2026Q3', 33, False),
                                        ('both', 33, True), ('2026Q3', 65, True)]:
            with self.subTest(quarter=quarter, source=source), \
                    patch('campaign.refresh_directory') as fetch, \
                    self.assertRaisesRegex(ValueError, 'INVALID_BATCH_CAPACITY'):
                campaign.plan(quarter, '2026-10-29', shards=shards, source_repair=source)
            fetch.assert_not_called()

    def test_both_source_quarters_emit_32_batches_each_with_unique_global_ids(self):
        with workspace():
            directory = current()
            directory['funds'] = [{**directory['funds'][0], 'code': f'{index:06d}'}
                                  for index in range(1, 2401)]
            directory['fundCount'] = directory['coverage']['fundCount'] = 2400
            directory['companies'][0]['fundCount'] = 2400
            traversal.atomic_json('web/data/catalog.json', directory)
            result = campaign.plan('both', '2026-10-29', shards=workflow_shards('both'),
                batch_size=75, refresh=False, historical_overlays={}, source_repair=True)
            self.assertEqual(result['batchCount'], 64)
            matrix = traversal.read_json('.cache/plan/matrix.json')
            self.assertEqual(sorted((row['id'] for row in matrix), key=int), [str(i) for i in range(64)])
            for quarter in campaign.PERIODS:
                rows = [row for row in matrix if row['quarter'] == quarter]
                self.assertEqual(len(rows), 32)
                codes = []
                for row in rows:
                    config = traversal.read_json(Path('.cache/plan') / row['config'])
                    self.assertTrue(traversal.source_repair_mode(config))
                    self.assertEqual(config['traversal']['batchId'], row['id'])
                    codes.extend(fund['code'] for fund in config['funds'])
                self.assertEqual(len(codes), 2400)
                self.assertEqual(len(set(codes)), 2400)

    def test_continuation_hold_preserves_all_64_batch_ids_and_rejects_out_of_range(self):
        with workspace():
            audit = held('2026Q3')
            audit['continuationHold']['batchIds'] = [str(i) for i in range(64)]
            audit['batchInputErrors'] = [{'batchId': str(i), 'quarter': '2026Q3',
                                         'errorCode': 'BATCH_ARTIFACT_MISSING'} for i in range(64)]
            path = Path('web/data/periods/2026Q3/audit.json')
            traversal.atomic_json(path, audit)
            before = path.read_bytes()
            state = campaign.deployed_continuation_state(Path('web/data'), '2026Q3')
            self.assertEqual(state['continuationHold'], audit['continuationHold'])
            self.assertEqual(path.read_bytes(), before)
            audit['continuationHold']['batchIds'][-1] = '64'
            audit['batchInputErrors'][-1]['batchId'] = '64'
            traversal.atomic_json(path, audit)
            with self.assertRaisesRegex(ValueError, 'INVALID_CONTINUATION_HOLD'):
                campaign.deployed_continuation_state(Path('web/data'), '2026Q3')


if __name__ == '__main__': unittest.main()
