const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function emitPlan(count) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'public-plan-'));
  try {
    fs.writeFileSync(path.join(root, 'matrix.json'), JSON.stringify(Array.from({ length: count }, (_, index) => ({
      id: String(index), config: `batch-${index}.json`, quarter: index < 16 ? '2026Q2' : '2026Q3'
    }))));
    return spawnSync(process.execPath, [path.join(__dirname, '../scripts/workflow-outputs.js'), root], { encoding: 'utf8' });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test('both quarters can emit all 32 planned batch jobs', () => {
  const result = emitPlan(32);
  assert.equal(result.status, 0, result.stderr);
  const line = result.stdout.split('\n').find(value => value.startsWith('matrix='));
  const matrix = JSON.parse(line.slice('matrix='.length));
  assert.equal(matrix.filter(item => item.quarter === '2026Q2').length, 16);
  assert.equal(matrix.filter(item => item.quarter === '2026Q3').length, 16);
  assert.match(result.stdout, /has_work=true/);
});

test('workflow matrix remains bounded and an empty plan does not request jobs', () => {
  assert.notEqual(emitPlan(33).status, 0);
  const empty = emitPlan(0);
  assert.equal(empty.status, 0);
  assert.match(empty.stdout, /has_work=false/);
});
