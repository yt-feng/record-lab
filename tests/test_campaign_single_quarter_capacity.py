"""One quarter uses the existing matrix allowance without larger source batches."""
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


def workflow_shards(quarter):
    workflow = (ROOT / '.github/workflows/traverse.yml').read_text()
    step = workflow.split('- name: Plan isolated quarter batches', 1)[1]
    body = step.split('run: |\n', 1)[1]
    setup, command = body.split('python scripts/campaign.py plan', 1)
    if '--shards "$SHARDS" --batch-size 75' not in command.splitlines()[0]:
        raise AssertionError('Workflow does not pass the bounded shard count')
    result = subprocess.run(['bash', '-c', textwrap.dedent(setup) + '\nprintf "%s\\n" "$SHARDS"'],
        env={**os.environ, 'QUARTER': quarter, 'SOURCE_REPAIR': 'true'},
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
    def test_workflow_assigns_the_same_total_matrix_allowance(self):
        self.assertEqual(workflow_shards('both'), 16)
        self.assertEqual(workflow_shards('2026Q2'), 32)
        self.assertEqual(workflow_shards('2026Q3'), 32)

    def test_q3_source_plan_emits_32_bounded_batches_and_keeps_holds(self):
        shards = workflow_shards('2026Q3')
        with workspace():
            directory = current()
            directory['funds'] = [{**directory['funds'][0], 'code': f'{index:06d}'}
                                  for index in range(1, 2401)]
            directory['fundCount'] = directory['coverage']['fundCount'] = 2400
            directory['companies'][0]['fundCount'] = 2400
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
            self.assertEqual(result['batchCount'], 32)
            self.assertEqual(result['quarters'], ['2026Q3'])
            self.assertEqual(result['heldQuarters'], ['2026Q3'])
            matrix = traversal.read_json('.cache/plan/matrix.json')
            self.assertEqual([item['id'] for item in matrix], [str(index) for index in range(32)])
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
            self.assertEqual(len(codes), 2400)
            self.assertEqual(len(set(codes)), 2400)
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


if __name__ == '__main__': unittest.main()
