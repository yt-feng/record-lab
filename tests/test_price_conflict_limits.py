"""Tiny synthetic plans and the checked-in workflow command; no transport."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import price_conflicts as conflicts
from test_price_conflicts import observation, NOW


def registry(quarter, count=6):
    raw = observation(quarter=quarter); template = raw['records'][0]
    raw['records'] = [json.loads(json.dumps(template).replace('600001', str(600001 + index))) for index in range(count)]
    raw['eligiblePriceKeys'] = count; raw['scannedFunds'] = count * 2
    return conflicts.registry_from_observations(raw, NOW)


class PriceConflictLimitTests(unittest.TestCase):
    def execute(self, limit=1, empty_first=False, **kwargs):
        calls = []
        def failed(url):
            calls.append(url)
            raise conflicts.diagnostic.DiagnosticError('REQUERY_NETWORK_ERROR', network_error_class='DISCONNECTED')
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / 'source', Path(temporary) / 'out'; source.mkdir()
            before = list(source.iterdir())
            def scan(_, quarter, now): return registry(quarter, 0 if empty_first and quarter == '2026Q2' else 6)
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'scan_registry', side_effect=scan):
                result = conflicts.run(source, output, keys_per_quarter=limit, fetch=failed, now=NOW, **kwargs)
            self.assertEqual(list(source.iterdir()), before)
            self.assertEqual(json.loads((output / 'result.json').read_bytes()), result)
            for quarter in result['quarters']:
                ledger = conflicts.normalize_ledger(json.loads((output / quarter['quarter'] / 'ledger.json').read_bytes()), quarter['quarter'], NOW)
                self.assertEqual(len(ledger['entries']), quarter['execution']['attemptedKeyCount'])
            return result, calls

    def test_each_allowed_limit_bounds_each_quarter_and_total_actual_failed_requests(self):
        for limit in range(1, 5):
            result, calls = self.execute(limit, max_keys=20, max_requests=40)
            with self.subTest(limit=limit):
                self.assertEqual(result['keysPerQuarter'], limit)
                self.assertEqual(result['maxKeys'], limit * 2); self.assertEqual(result['maxRequests'], limit * 4)
                self.assertEqual(len(calls), limit * 4)
                self.assertEqual(result['summary']['attemptedKeyCount'], limit * 2)
                self.assertEqual([q['execution']['attemptedKeyCount'] for q in result['quarters']], [limit, limit])
                self.assertEqual([q['execution']['attemptedRequestCount'] for q in result['quarters']], [limit * 2, limit * 2])
                self.assertTrue(all(q['resolution'] == 'unresolved' for q in result['quarters']))

    def test_unused_first_quarter_budget_cannot_overfill_second_quarter(self):
        for limit in (1, 4):
            result, calls = self.execute(limit, empty_first=True, max_keys=20, max_requests=40)
            self.assertEqual([q['execution']['attemptedKeyCount'] for q in result['quarters']], [0, limit])
            self.assertEqual(len(calls), limit * 2)
        result, calls = self.execute(4, max_keys=1, max_requests=2)
        self.assertLessEqual(len(calls), 2); self.assertLessEqual(result['summary']['attemptedKeyCount'], 1)
        result, calls = self.execute(4, max_keys=1, max_requests=1)
        self.assertEqual(calls, []); self.assertEqual(result['summary']['attemptedKeyCount'], 0)

    def test_invalid_python_limits_are_rejected_before_scanning_or_requesting(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'scan_registry') as scanner:
            for value in (None, False, True, 0, -1, 5, 1.0, '1'):
                with self.subTest(value=value), self.assertRaisesRegex(conflicts.ConflictError, 'CONFLICT_LIMIT_INVALID'):
                    conflicts.run(Path(temporary) / 'source', Path(temporary) / 'out', keys_per_quarter=value,
                                  fetch=lambda _: self.fail('no request'))
            scanner.assert_not_called()

    def test_invalid_cli_limits_emit_only_fixed_error_before_reading_parent_or_data(self):
        for value in ('0', '5', '-1', '1.5', '01', 'PRIVATE_INPUT_SENTINEL'):
            output = io.StringIO()
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'read_json') as read, patch.object(conflicts, 'run') as run, contextlib.redirect_stdout(output):
                code = conflicts.main(['run', '--data', 'synthetic', '--output', 'out', '--keys-per-quarter', value, '--restore-state', 'parent.json'])
            self.assertEqual(code, 1); self.assertEqual(json.loads(output.getvalue())['errorCode'], 'CONFLICT_LIMIT_INVALID')
            self.assertNotIn('PRIVATE_INPUT_SENTINEL', output.getvalue()); read.assert_not_called(); run.assert_not_called()

    def test_workflow_default_choices_and_exact_command_reach_cli_with_parent_unchanged(self):
        text = (Path(__file__).parents[1] / '.github/workflows/recheck-price-conflicts.yml').read_text()
        options = text.split('      keys_per_quarter:\n', 1)[1].split('      data_commit:\n', 1)[0]
        self.assertIn("default: '1'", options); self.assertIn('type: choice', options)
        self.assertEqual(re.findall(r"^          - '([^']+)'$", options, re.M), ['1', '2', '3', '4'])
        command = next(line.strip() for line in text.splitlines() if line.strip().startswith('python scripts/price_conflicts.py run '))
        self.assertIn('KEYS_PER_QUARTER: ${{ inputs.keys_per_quarter }}', text)
        self.assertIn('--keys-per-quarter "$KEYS_PER_QUARTER"', command)
        self.assertIn('cancel-in-progress: false', text); self.assertIn('Restore explicitly bound prior ledger', text)
        for value in ('1', '4'):
            args = [value if item == '$KEYS_PER_QUARTER' else item for item in shlex.split(command)[2:]]
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'read_json', return_value={'parent': 'unchanged'}) as read, \
                 patch.object(conflicts, 'run', return_value={'kind': 'price-conflict-run', 'summary': {}}) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(conflicts.main(args), 0)
            self.assertEqual(run.call_args.kwargs['keys_per_quarter'], int(value))
            self.assertEqual(run.call_args.kwargs['restored_state'], {'parent': 'unchanged'})
            read.assert_called_once_with('.cache/price-conflicts-restore/state.json')
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(conflicts, 'run', return_value={'kind': 'price-conflict-run'}) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(conflicts.main(['run', '--data', 'synthetic', '--output', 'out']), 0)
        self.assertEqual(run.call_args.kwargs['keys_per_quarter'], 1)


if __name__ == '__main__': unittest.main()
