const fs = require('node:fs');
const path = require('node:path');
try {
  const root = process.argv[2];
  const matrix = JSON.parse(fs.readFileSync(path.join(root, 'matrix.json'), 'utf8'));
  if (!Array.isArray(matrix) || matrix.length > 8 || matrix.some(item => !/^\d+$/.test(item.id) || !/^batch-\d+\.json$/.test(item.config))) throw new Error();
  console.log(`matrix=${JSON.stringify(matrix)}`);
  console.log(`has_work=${matrix.length > 0}`);
} catch { console.error('invalid_workflow_plan'); process.exitCode = 1; }
