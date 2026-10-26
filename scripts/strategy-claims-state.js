#!/usr/bin/env node
'use strict';

// One streaming validator per traversal pass. Only the bounded strategy group
// is admitted; neither holdings nor private PDF text enters this protocol.
const { strategyClaimsFor } = require('../lib/strategy-claims');
const MAX_LINE_BYTES = 80000;
function validate(value) {
  const result = { code: /^\d{6}$/.test(value?.code || '') ? value.code : null, analysisDigest: null, scanned: false };
  if (!result.code || typeof value.now !== 'string' || typeof value.asOf !== 'string') return result;
  const claims = strategyClaimsFor(value.report, { code: value.code, now: value.now, asOf: value.asOf });
  if (claims) Object.assign(result, { analysisDigest: claims.source.digest, scanned: true });
  return result;
}
async function main() {
  let pending = Buffer.alloc(0);
  for await (const chunk of process.stdin) {
    pending = Buffer.concat([pending, chunk]);
    let end;
    while ((end = pending.indexOf(10)) !== -1) {
      if (end > MAX_LINE_BYTES) throw Error('STRATEGY_STATE_INPUT_INVALID');
      const line = pending.subarray(0, end); pending = pending.subarray(end + 1);
      const value = JSON.parse(line.toString('utf8'));
      process.stdout.write(JSON.stringify(validate(value)) + '\n');
    }
    if (pending.length > MAX_LINE_BYTES) throw Error('STRATEGY_STATE_INPUT_INVALID');
  }
  if (pending.length) throw Error('STRATEGY_STATE_INPUT_INVALID');
}
if (require.main === module) main().catch(() => {
  process.stderr.write('STRATEGY_STATE_INPUT_INVALID\n'); process.exitCode = 1;
});
module.exports = { validate, MAX_LINE_BYTES };
