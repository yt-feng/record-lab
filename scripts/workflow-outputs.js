const fs = require('node:fs');
const path = require('node:path');
try {
  const root = process.argv[2];
  const matrix = JSON.parse(fs.readFileSync(path.join(root, 'matrix.json'), 'utf8'));
  if (!Array.isArray(matrix) || matrix.length > 64 || matrix.some(item => !/^\d+$/.test(item.id) || item.config !== `batch-${item.id}.json`
    || (item.quarter !== undefined && !/^2026Q[23]$/.test(item.quarter)))) throw new Error();
  if (new Set(matrix.map(item => item.id)).size !== matrix.length) throw new Error();
  if (matrix.length > 32) for (const item of matrix) {
    const config = JSON.parse(fs.readFileSync(path.join(root, item.config), 'utf8'));
    if (config.collectionLane !== 'source-repair-no-market-v1' || config.maxPriceSymbols !== 0
      || config.maxPriceSeconds !== 0 || !Array.isArray(config.funds)
      || !Array.isArray(config.traversal?.enrichmentCodes) || config.traversal.enrichmentCodes.length
      || config.funds.some(fund => !fund || Object.hasOwn(fund, 'priceOnly') || Object.hasOwn(fund, 'priceTargets')))
      throw new Error();
  }
  console.log(`matrix=${JSON.stringify(matrix)}`);
  console.log(`has_work=${matrix.length > 0}`);
} catch { console.error('invalid_workflow_plan'); process.exitCode = 1; }
