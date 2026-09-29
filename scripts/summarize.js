const fs = require('node:fs');
try {
  const data = JSON.parse(fs.readFileSync('web/data/latest.json', 'utf8'));
  const c = data.coverage || {};
  const number = (value) => Number.isFinite(value) ? value : 0;
  console.log('## Record coverage\n');
  console.log(`- Products: ${number(c.fundCount)}`);
  console.log(`- Institutions: ${number(c.companyCount)}`);
  console.log(`- Position observations: ${number(c.holdingCount)}`);
  console.log(`- Price intervals: ${number(c.priceAvailable)}`);
  console.log(`- Report commentaries: ${number(c.narrativeCount)}`);
  console.log(`- Collection issues: ${Array.isArray(data.errors) ? data.errors.length : 0}`);
} catch {
  console.error('summary_unavailable');
  process.exitCode = 1;
}
