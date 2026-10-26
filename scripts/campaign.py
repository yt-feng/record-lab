"""Quarter-isolated, resumable collection; all bulk work runs on Actions."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from traversal import atomic_json, read_json
from publication_guard import inspect_content

PERIODS = {
    '2026Q2': ('2026-06-30', '2026-03-31'),
    '2026Q3': ('2026-09-30', '2026-06-30'),
}


def selected_periods(value):
    if value == 'both': return list(PERIODS)
    if value in PERIODS: return [value]
    raise ValueError('INVALID_PERIOD')


def quarter_config(base, quarter, as_of):
    target, baseline = PERIODS[quarter]
    if dt.date.fromisoformat(as_of).isoformat() != as_of or as_of < target:
        raise ValueError('INVALID_AS_OF')
    return {**base, 'funds': [], 'quarter': quarter, 'asOf': as_of,
            'targetPeriodEnd': target, 'baselinePeriodEnd': baseline,
            'collectorVersion': 4, 'requireReportQuality': True,
            'retryPolicy': {'dailyUntil': '2026-11-30' if quarter == '2026Q3' else None,
                            'intervalDays': 7, 'maxScheduledCycles': 4},
            'scope': f'{quarter} 全目录产品与报告逐项补齐'}


def seed_quarter(data_root, quarter):
    """Copy the legacy Q2 snapshot exactly once, retaining source timestamps."""
    root = Path(data_root)
    target = root / 'periods' / quarter
    target.mkdir(parents=True, exist_ok=True)
    legacy = root / 'latest.json'
    if quarter == '2026Q2' and not (target / 'latest.json').exists() and legacy.exists():
        old = read_json(legacy)
        period = old.get('targetPeriodEnd') or old.get('coverage', {}).get('targetPeriodEnd')
        if period != PERIODS[quarter][0]: raise ValueError('LEGACY_PERIOD_MISMATCH')
        for name in ('latest.json', 'progress.json'):
            if (root / name).is_file(): shutil.copy2(root / name, target / name)
        if (root / 'companies').is_dir(): shutil.copytree(root / 'companies', target / 'companies', dirs_exist_ok=True)
    return target


def refresh_directory(root, as_of):
    current = read_json(root / 'catalog.json', optional=True)
    if current and current.get('asOf') == as_of and current.get('coverage', {}).get('directoryComplete'):
        return {'status': 'current', 'asOf': current['asOf']}
    # The first new date refreshes all institution pages. Subsequent waves reuse
    # that verified directory, while unowned codes remain direct collection tasks.
    try:
        subprocess.run([sys.executable, 'scripts/discover.py', '--output', str(root / 'catalog.json'),
                        '--state', 'raw/catalog-state.json', '--as-of', as_of,
                        '--allow-partial', '--max-companies', '250'], check=True)
        refreshed = read_json(root / 'catalog.json')
        return {'status': 'refreshed' if refreshed.get('coverage', {}).get('directoryComplete') else 'partial',
                'asOf': refreshed.get('asOf'),
                'staleCompanyCount': refreshed.get('coverage', {}).get('staleCompanyCount', 0)}
    except subprocess.CalledProcessError:
        # Discovery's regression gate preserves the old complete directory. A
        # failed refresh must not prevent known products' reports being read.
        preserved = read_json(root / 'catalog.json', optional=True)
        if (not current or not preserved or not current.get('coverage', {}).get('directoryComplete')
                or preserved != current):
            raise
        return {'status': 'cached', 'asOf': current.get('asOf'), 'errorCode': 'DIRECTORY_REFRESH_FAILED'}


def plan(quarter, as_of, data='web/data', shards=16, batch_size=75, refresh=True):
    selected = selected_periods(quarter)
    # Planning, Actions output and missing-part reconciliation share one bound.
    # Reject unsupported configurations before fetching or changing any files.
    if (type(shards) is not int or not 1 <= shards <= 32
            or shards * len(selected) > 32
            or type(batch_size) is not int or not 1 <= batch_size <= 100):
        raise ValueError('INVALID_BATCH_CAPACITY')
    root = Path(data)
    directory_status = refresh_directory(root, as_of) if refresh else {'status': 'provided'}
    catalog = read_json(root / 'catalog.json')
    base = read_json('config/universe.json')
    matrix = []
    for name in selected:
        output = seed_quarter(root, name)
        config = quarter_config(base, name, as_of)
        atomic_json(Path('.cache/campaign') / f'{name}.json', config)
        shutil.copy2(root / 'catalog.json', output / 'catalog.json')
        # CLI hydration reads existing reports without serializing their raw text
        # into the public batch plan.
        private_plan = Path('.cache') / ('plan-' + name)
        subprocess.run([sys.executable, 'scripts/traversal.py', 'plan', '--catalog', str(root / 'catalog.json'),
                        '--progress', str(output / 'progress.json'), '--config', f'.cache/campaign/{name}.json',
                        '--data-root', str(output), '--output', str(private_plan),
                        '--shards', str(shards), '--batch-size', str(batch_size)], check=True)
        for item in read_json(private_plan / 'matrix.json'):
            batch = read_json(private_plan / item['config'])
            index = str(len(matrix))
            batch['traversal']['batchId'] = index
            filename = f'batch-{index}.json'
            atomic_json(Path('.cache/plan') / filename, batch)
            matrix.append({'id': index, 'config': filename, 'quarter': name})
    atomic_json('.cache/plan/matrix.json', matrix)
    atomic_json('.cache/plan/meta.json', {'quarters': selected, 'asOf': as_of, 'hasWork': bool(matrix), 'directoryRefresh': directory_status})
    atomic_json('.cache/campaign/selection.json', {'quarters': selected, 'asOf': as_of, 'selection': quarter, 'directoryRefresh': directory_status})
    for file in Path('.cache/campaign').glob('*.json'):
        if inspect_content(file.read_bytes()): raise ValueError('PLAN_PUBLICATION_REJECTED')
    return {'batchCount': len(matrix), 'quarters': selected, 'directoryRefresh': directory_status}


def publish(data='web/data', parts='.cache/parts'):
    root, part_root = Path(data), Path(parts)
    selection = read_json('.cache/campaign/selection.json')
    matrix = read_json('.cache/plan/matrix.json')
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location('complete_status', 'scripts/complete-status.py')
    module = module_from_spec(spec); spec.loader.exec_module(module)
    module.reconcile('.cache/plan', str(part_root))
    summaries = []
    for quarter in selection['quarters']:
        if quarter not in PERIODS: raise ValueError('INVALID_PERIOD')
        output = seed_quarter(root, quarter)
        shutil.copy2(root / 'catalog.json', output / 'catalog.json')
        scoped_parts = Path('.cache') / ('parts-' + quarter)
        scoped_parts.mkdir(parents=True, exist_ok=True)
        for item in matrix:
            if item['quarter'] == quarter:
                src = part_root / ('part-' + item['id'])
                if src.is_dir(): shutil.copytree(src, scoped_parts / src.name, dirs_exist_ok=True)
        config = f'.cache/campaign/{quarter}.json'
        subprocess.run(['node', 'scripts/merge-parts.js', '--parts', str(scoped_parts), '--data', str(output),
                        '--catalog', str(root / 'catalog.json'), '--config', config], check=True)
        subprocess.run([sys.executable, 'scripts/traversal.py', 'merge', '--parts', str(scoped_parts),
                        '--catalog', str(root / 'catalog.json'), '--progress', str(output / 'progress.json'),
                        '--config', config, '--data-root', str(output)], check=True)
        progress = read_json(output / 'progress.json')
        atomic_json(output / 'audit.json', {
            'schemaVersion': 1, 'quarter': quarter, 'phase': progress.get('phase'),
            'updatedAt': progress.get('updatedAt'), 'completionStatus': progress.get('completionStatus'),
            'totalFunds': progress.get('totalFunds'), 'processedFunds': progress.get('processedFunds'),
            'failedFunds': progress.get('failedFunds'), 'pendingFunds': progress.get('pendingFunds'),
            'notApplicableFunds': progress.get('notApplicableFunds'), 'research': progress.get('research'),
            'continueRun': progress.get('continueRun'), 'nextRetryAt': progress.get('nextRetryAt'),
            'completionAudit': progress.get('completionAudit'),
        })
        subprocess.run(['node', 'scripts/analyze-data.js', '--data', str(output),
                        '--output', str(output / 'analysis.json')], check=True)
        summaries.append({'quarter': quarter, **{k: progress.get(k) for k in (
            'totalFunds', 'processedFunds', 'failedFunds', 'pendingFunds', 'research', 'completionStatus', 'continueRun')}})
    periods = []
    for quarter, (target, baseline) in PERIODS.items():
        directory = root / 'periods' / quarter
        if (directory / 'latest.json').exists() or (directory / 'progress.json').exists():
            periods.append({'id': quarter, 'label': quarter, 'path': f'./data/periods/{quarter}/',
                            'targetPeriodEnd': target, 'baselinePeriodEnd': baseline})
    default = '2026Q3' if any(p['id'] == '2026Q3' for p in periods) else '2026Q2'
    atomic_json(root / 'periods.json', {'schemaVersion': 1, 'defaultPeriod': default, 'periods': periods})
    result = {'quarters': summaries, 'continueRun': any(s['continueRun'] for s in summaries), 'directoryRefresh': selection.get('directoryRefresh'),
              'selection': selection['selection']}
    atomic_json('.cache/campaign/result.json', result)
    summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_file:
        with open(summary_file, 'a') as handle:
            handle.write('## Collection coverage\n\n')
            for s in summaries:
                research = s.get('research') or {}
                handle.write(f"- {s['quarter']}: {s['totalFunds']} codes; {s['processedFunds']} with holdings/no-equity evidence; "
                             f"{research.get('completeFunds', 0)} fully evidenced; {s['failedFunds']} failed; "
                             f"{s['pendingFunds']} pending. Status: {s.get('completionStatus', 'incomplete')}.\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['plan', 'publish'])
    parser.add_argument('--quarter', choices=['both', *PERIODS], default='both')
    parser.add_argument('--as-of', default=dt.datetime.now(dt.timezone.utc).date().isoformat())
    parser.add_argument('--shards', type=int, default=16)
    parser.add_argument('--batch-size', type=int, default=75)
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true': raise ValueError('BATCH_REQUIRES_ACTIONS')
    result = plan(args.quarter, args.as_of, shards=args.shards, batch_size=args.batch_size) if args.command == 'plan' else publish()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try: main()
    except Exception:
        print(json.dumps({'status': 'failed', 'errorCode': 'CAMPAIGN_FAILED'}))
        raise SystemExit(1) from None
