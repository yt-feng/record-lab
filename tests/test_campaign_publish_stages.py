"""Tiny synthetic publication proves bounded stage logs and failure boundaries."""
import contextlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import campaign
from test_campaign_history import workspace

STAGES = ['MERGE_PARTS_START', 'MERGE_PARTS_END', 'RECONCILE_START', 'RECONCILE_END', 'ANALYSIS_START', 'ANALYSIS_END']


class FlushedOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        return super().flush()


class CampaignPublishStageTests(unittest.TestCase):
    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            campaign.plan('both', '2026-10-26', shards=1, batch_size=1, refresh=False, historical_overlays={})

    def logs(self, stream):
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(stream.flushes, len(rows), 'stage metadata is immediately visible while a child is running')
        for row in rows:
            self.assertEqual(set(row), {'stage', 'quarter', 'elapsedSeconds'})
            self.assertIn(row['stage'], STAGES)
            self.assertIn(row['quarter'], campaign.PERIODS)
            self.assertIsInstance(row['elapsedSeconds'], (int, float))
            self.assertTrue(math.isfinite(row['elapsedSeconds']))
            self.assertGreaterEqual(row['elapsedSeconds'], 0)
        for quarter in campaign.PERIODS:
            elapsed = [row['elapsedSeconds'] for row in rows if row['quarter'] == quarter]
            self.assertEqual(elapsed, sorted(elapsed))
        return rows

    def test_two_quarters_emit_six_ordered_stages_each_and_keep_data_incomplete(self):
        with workspace():
            self.prepare()
            output = FlushedOutput()
            with contextlib.redirect_stdout(output):
                result = campaign.publish()
            rows = self.logs(output)
            self.assertEqual([(row['quarter'], row['stage']) for row in rows],
                             [(quarter, stage) for quarter in campaign.PERIODS for stage in STAGES])
            self.assertEqual(len(rows), 12)
            self.assertFalse(result['continueRun'])
            self.assertTrue(result['batchInputErrors'], 'successful phases do not assert complete source coverage')
            self.assertTrue(all(row['completionStatus'] != 'complete' for row in result['quarters']))

    def test_failed_child_has_start_but_never_end_or_later_phase(self):
        children = ['scripts/merge-parts.js', 'scripts/traversal.py', 'scripts/analyze-data.js']
        for failure_index, failed_child in enumerate(children):
            with self.subTest(failed_child=failed_child), workspace():
                self.prepare()
                output = FlushedOutput()
                ordinary_run = campaign.subprocess.run

                def run(command, **kwargs):
                    started = [json.loads(line) for line in output.getvalue().splitlines()]
                    self.assertTrue(started)
                    self.assertTrue(started[-1]['stage'].endswith('_START'))
                    self.assertEqual(output.flushes, len(started))
                    if command[1] == failed_child:
                        raise subprocess.CalledProcessError(1, command)
                    return ordinary_run(command, **kwargs)

                with contextlib.redirect_stdout(output), patch('campaign.subprocess.run', side_effect=run):
                    with self.assertRaises(subprocess.CalledProcessError):
                        campaign.publish()
                rows = self.logs(output)
                self.assertEqual([row['stage'] for row in rows], STAGES[:failure_index * 2 + 1])
                self.assertTrue(all(row['quarter'] == '2026Q2' for row in rows))


if __name__ == '__main__': unittest.main()
