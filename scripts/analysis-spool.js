'use strict';

// Private, bounded buffers preserve stock-local output without retaining every
// contribution or every portfolio's PDF evidence in the analysis process.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { StringDecoder } = require('node:string_decoder');
const DEFAULT_BUFFER_BYTES = 4_000_000;
const MAX_ITEM_BYTES = 8_000_000;
const MAX_TOTAL_BYTES = 4_000_000_000;
const fail = code => { throw new Error(code); };

function* jsonLines(filename, maxItemBytes) {
  if (!fs.existsSync(filename)) return;
  const stat = fs.lstatSync(filename);
  if (!stat.isFile() || stat.isSymbolicLink()) fail('INVALID_ANALYSIS_SPOOL');
  const fd = fs.openSync(filename, 'r'), block = Buffer.alloc(65_536), decoder = new StringDecoder('utf8');
  let pending = '';
  try {
    while (true) {
      const size = fs.readSync(fd, block, 0, block.length, null);
      pending += size ? decoder.write(block.subarray(0, size)) : decoder.end();
      let boundary;
      while ((boundary = pending.indexOf('\n')) !== -1) {
        const line = pending.slice(0, boundary); pending = pending.slice(boundary + 1);
        if (!line || Buffer.byteLength(line) + 1 > maxItemBytes) fail('INVALID_ANALYSIS_SPOOL');
        yield JSON.parse(line);
      }
      if (Buffer.byteLength(pending) > maxItemBytes) fail('INVALID_ANALYSIS_SPOOL');
      if (!size) break;
    }
    if (pending) fail('INVALID_ANALYSIS_SPOOL');
  } finally { fs.closeSync(fd); }
}

function createAnalysisSpool(directory, { maxBufferBytes = DEFAULT_BUFFER_BYTES, maxItemBytes = MAX_ITEM_BYTES,
  maxTotalBytes = MAX_TOTAL_BYTES } = {}) {
  if (![maxBufferBytes, maxItemBytes, maxTotalBytes].every(Number.isSafeInteger)
    || maxBufferBytes < 100 || maxBufferBytes > DEFAULT_BUFFER_BYTES || maxItemBytes < 100 || maxItemBytes > MAX_ITEM_BYTES
    || maxTotalBytes < maxItemBytes || maxTotalBytes > MAX_TOTAL_BYTES || fs.existsSync(directory)) fail('INVALID_ANALYSIS_SPOOL');
  fs.mkdirSync(directory, { mode: 0o700 });
  const buffers = new Map(), stockFiles = new Map();
  let bufferedBytes = 0, peakBufferedBytes = 0, totalBytes = 0, portfolioCount = 0, contributionCount = 0, finished = false;
  const portfolioFile = path.join(directory, 'portfolios.ndjson');
  function flush() {
    for (const [filename, lines] of buffers) fs.appendFileSync(filename, lines.join(''), { mode: 0o600 });
    buffers.clear(); bufferedBytes = 0;
  }
  function append(filename, value) {
    if (finished) fail('ANALYSIS_SPOOL_FINISHED');
    const line = JSON.stringify(value) + '\n', bytes = Buffer.byteLength(line);
    if (bytes > maxItemBytes) fail('ANALYSIS_ITEM_TOO_LARGE');
    if (totalBytes + bytes > maxTotalBytes) fail('ANALYSIS_SPOOL_TOO_LARGE');
    if (bufferedBytes + bytes > maxBufferBytes) flush();
    // A single large proof is still item-bounded; bypass the shared buffer.
    if (bytes > maxBufferBytes) fs.appendFileSync(filename, line, { mode: 0o600 });
    else {
      if (!buffers.has(filename)) buffers.set(filename, []);
      buffers.get(filename).push(line); bufferedBytes += bytes;
      peakBufferedBytes = Math.max(peakBufferedBytes, bufferedBytes);
    }
    totalBytes += bytes;
  }
  function finish() { if (!finished) { flush(); finished = true; } }
  return {
    addPortfolio(value) {
      if (++portfolioCount > 150000) fail('ANALYSIS_SPOOL_TOO_LARGE');
      append(portfolioFile, value);
    },
    addContribution(key, value) {
      if (typeof key !== 'string' || !/^(?:CN:\d{6}|HK:\d{5}|US:[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?)$/.test(key)) fail('INVALID_ANALYSIS_SPOOL');
      if (!stockFiles.has(key)) {
        if (stockFiles.size >= 30000) fail('ANALYSIS_SPOOL_TOO_LARGE');
        stockFiles.set(key, path.join(directory, crypto.createHash('sha256').update(key).digest('hex') + '.ndjson'));
      }
      append(stockFiles.get(key), value); contributionCount++;
    },
    portfolios() { finish(); return jsonLines(portfolioFile, maxItemBytes); },
    contributions(key) { finish(); const file = stockFiles.get(key); return file ? jsonLines(file, maxItemBytes) : []; },
    finish,
    stats: () => ({ bufferedBytes, peakBufferedBytes, totalBytes, portfolioCount, contributionCount, stockCount: stockFiles.size }),
    dispose() { buffers.clear(); bufferedBytes = 0; fs.rmSync(directory, { recursive: true, force: true }); }
  };
}

module.exports = { createAnalysisSpool };
