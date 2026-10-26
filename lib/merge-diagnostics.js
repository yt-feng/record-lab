'use strict';
// Fixed, bounded diagnostic metadata. Never serialize error text or paths.
const fs = require('node:fs');
const os = require('node:os');
const PHASES = new Set(['inputs', 'components', 'prices', 'receipts', 'commit', 'done']);
const CODES = new Set(['ENOSPC', 'EMFILE', 'ENFILE', 'ENOMEM', 'EACCES', 'ENOENT', 'EIO', 'EROFS']);
const MODULES = new Set(['merge-parts.js', 'merge-storage.js', 'fund-analysis.js', 'build-data.js', 'reuse-prices.js',
  'price-validation-cache.js', 'price-consistency.js', 'price-coverage.js', 'strategy-claims.js', 'strategy-evidence-pool.js', 'portfolio-identity.js']);
let state = { phase: 'inputs', completed: 0 }, started = 0;
function checkpoint(phase, completed = 0) {
  if (!PHASES.has(phase) || !Number.isSafeInteger(completed) || completed < 0) return;
  if (!started || phase === 'inputs' && completed === 0) started = Date.now();
  state = { phase, completed };
}
function snapshot() {
  const value = { ...state, elapsedSeconds: Math.max(0, Math.floor((Date.now() - (started || Date.now())) / 1000)) };
  try {
    const stats = fs.statfsSync(os.tmpdir());
    const available = stats.bavail * stats.bsize;
    if (Number.isSafeInteger(available) && available >= 0) value.scratchAvailableBytes = available;
  } catch { /* The diagnostic remains valid without a filesystem statistic. */ }
  return value;
}
function diagnose(error) {
  const value = { ...snapshot(), errorClass: error instanceof TypeError ? 'TYPE_ERROR' : error instanceof RangeError ? 'RANGE_ERROR'
    : error instanceof SyntaxError ? 'SYNTAX_ERROR' : 'ERROR' };
  const code = error && Object.getOwnPropertyDescriptor(error, 'code')?.value;
  if (CODES.has(code)) value.systemCode = code;
  let stack;
  try { if (error instanceof Error) stack = error.stack; } catch { /* Never emit arbitrary getter failures. */ }
  value.frames = [];
  if (typeof stack === 'string') for (const line of stack.slice(0, 8192).split('\n').slice(1, 12)) {
    const match = /[\/\\]([A-Za-z0-9-]+\.js):(\d{1,6}):(\d{1,6})\)?$/.exec(line);
    if (match && MODULES.has(match[1])) value.frames.push({ module: match[1], line: Number(match[2]), column: Number(match[3]) });
    if (value.frames.length === 4) break;
  }
  return value;
}
module.exports = { checkpoint, diagnose };
