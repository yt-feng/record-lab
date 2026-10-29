'use strict';

// Keep the legacy receipt API in memory. Only oversized transient disk
// receipts use bounded pages; every observation and source witness is retained.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const MAX_MANIFEST_BYTES = 25_000_000;
const MAX_PAGE_BYTES = 8_000_000;
const MAX_PAGES = 64;
const MAX_TOTAL_BYTES = MAX_PAGE_BYTES * MAX_PAGES;
const MAX_FUNDS = 4096;
const CODE = /^\d{6}$/;
const bytes = value => Buffer.from(JSON.stringify(value) + '\n');
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const fail = code => { throw Object.assign(new Error(code), { code }); };

function atomic(file, payload) {
  const temporary = `${file}.tmp-${process.pid}`;
  try {
    fs.writeFileSync(temporary, payload, { flag: 'wx' });
    fs.renameSync(temporary, file);
  } catch {
    try { fs.unlinkSync(temporary); } catch { /* Preserve the original error. */ }
    fail('MERGE_WRITE_FAILED');
  }
}

function writeMergeReceipt(file, receipt, options = {}) {
  const legacyLimit = options.legacyLimit ?? MAX_MANIFEST_BYTES;
  const pageLimit = options.pageLimit ?? MAX_PAGE_BYTES;
  if (!Number.isInteger(legacyLimit) || legacyLimit < 1 || legacyLimit > MAX_MANIFEST_BYTES
      || !Number.isInteger(pageLimit) || pageLimit < 256 || pageLimit > MAX_PAGE_BYTES)
    fail('MERGE_RECEIPT_INVALID');
  if (!receipt || receipt.schemaVersion !== 1 || !Array.isArray(receipt.funds)
      || !Array.isArray(receipt.requestedCodes) || receipt.funds.length > MAX_FUNDS
      || receipt.requestedCodes.length > MAX_FUNDS
      || receipt.requestedCodes.some(code => typeof code !== 'string' || !CODE.test(code))
      || new Set(receipt.requestedCodes).size !== receipt.requestedCodes.length
      || !receipt.phase || typeof receipt.phase !== 'object' || Array.isArray(receipt.phase)
      || typeof receipt.generatedAt !== 'string') fail('MERGE_RECEIPT_INVALID');
  const requested = new Set(receipt.requestedCodes);
  const { funds, ...metadata } = receipt;
  let totalRowsBytes = 0, previousCode = '';
  for (const fund of funds) {
    if (!fund || typeof fund.code !== 'string' || !CODE.test(fund.code)
        || fund.code <= previousCode || !requested.has(fund.code)) fail('MERGE_RECEIPT_INVALID');
    previousCode = fund.code;
    totalRowsBytes += Buffer.byteLength(JSON.stringify(fund));
    if (totalRowsBytes > MAX_TOTAL_BYTES) fail('MERGE_RECEIPT_TOO_LARGE');
  }
  const legacyBytes = Buffer.byteLength(JSON.stringify({ ...metadata, funds: [] }))
    + totalRowsBytes + Math.max(0, funds.length - 1) + 1;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const directory = fs.lstatSync(path.dirname(file));
    if (!directory.isDirectory() || directory.isSymbolicLink()) fail('MERGE_RECEIPT_INVALID');
  } catch (error) {
    if (error?.code?.startsWith('MERGE_')) throw error;
    fail('MERGE_WRITE_FAILED');
  }
  if (legacyBytes <= legacyLimit) {
    atomic(file, bytes(receipt));
    return { storageFormat: 'legacy', fundCount: funds.length, pageCount: 0 };
  }
  const envelope = rows => ({ schemaVersion: 1, kind: 'merge-receipt-fund-page',
    phase: receipt.phase, generatedAt: receipt.generatedAt, funds: rows });
  const overhead = bytes(envelope([])).length;
  // Preflight every page and the complete manifest before writing any file.
  const groups = [];
  let start = 0, used = overhead;
  for (let index = 0; index < funds.length; index++) {
    const size = Buffer.byteLength(JSON.stringify(funds[index]));
    if (size + overhead > pageLimit) fail('MERGE_RECEIPT_TOO_LARGE');
    if (index > start && used + size + 1 > pageLimit) {
      groups.push([start, index]); start = index; used = overhead;
    }
    used += size + (index > start ? 1 : 0);
  }
  if (funds.length) groups.push([start, funds.length]);
  if (!groups.length || groups.length > MAX_PAGES) fail('MERGE_RECEIPT_TOO_LARGE');
  const pageRows = [], pagesRoot = path.join(path.dirname(file), 'merge-receipt-pages');
  let totalPageBytes = 0;
  for (const [index, [begin, end]] of groups.entries()) {
    const payload = bytes(envelope(funds.slice(begin, end)));
    totalPageBytes += payload.length;
    const sha256 = digest(payload);
    pageRows.push({ path: `merge-receipt-pages/${String(index).padStart(4, '0')}-${sha256}.json`,
      sha256, byteLength: payload.length, fundCount: end - begin,
      firstCode: funds[begin].code, lastCode: funds[end - 1].code });
  }
  const manifest = { schemaVersion: 2, storageFormat: 'fund-pages-v1', receipt: metadata,
    fundCount: funds.length, totalPageBytes, fundPages: pageRows };
  const manifestBytes = bytes(manifest);
  if (manifestBytes.length > MAX_MANIFEST_BYTES || totalPageBytes > MAX_TOTAL_BYTES)
    fail('MERGE_RECEIPT_TOO_LARGE');
  try {
    if (fs.existsSync(pagesRoot)) {
      if (!fs.lstatSync(pagesRoot).isDirectory() || fs.lstatSync(pagesRoot).isSymbolicLink())
        fail('MERGE_RECEIPT_INVALID');
    } else fs.mkdirSync(pagesRoot);
    for (const [index, [begin, end]] of groups.entries()) {
      const payload = bytes(envelope(funds.slice(begin, end)));
      if (payload.length !== pageRows[index].byteLength || digest(payload) !== pageRows[index].sha256)
        fail('MERGE_RECEIPT_INVALID');
      const destination = path.join(path.dirname(file), pageRows[index].path);
      // Content-addressed pages never alter pages referenced by an older manifest.
      if (fs.existsSync(destination)) {
        const before = fs.lstatSync(destination);
        if (!before.isFile() || before.isSymbolicLink() || before.size !== payload.length) fail('MERGE_RECEIPT_INVALID');
        const descriptor = fs.openSync(destination, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
        try {
          const opened = fs.fstatSync(descriptor);
          if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino || opened.size !== payload.length)
            fail('MERGE_RECEIPT_INVALID');
          const existing = Buffer.alloc(payload.length + 1);
          let offset = 0, count;
          while (offset < existing.length && (count = fs.readSync(descriptor, existing, offset, existing.length - offset, null)) > 0)
            offset += count;
          if (offset !== payload.length || digest(existing.subarray(0, offset)) !== pageRows[index].sha256)
            fail('MERGE_RECEIPT_INVALID');
        } finally { fs.closeSync(descriptor); }
      } else atomic(destination, payload);
    }
    atomic(file, manifestBytes);
  } catch (error) {
    if (error?.code?.startsWith('MERGE_')) throw error;
    fail('MERGE_WRITE_FAILED');
  }
  return { storageFormat: 'fund-pages-v1', fundCount: funds.length, pageCount: groups.length, totalPageBytes };
}

module.exports = { writeMergeReceipt, MAX_MANIFEST_BYTES, MAX_PAGE_BYTES, MAX_PAGES, MAX_TOTAL_BYTES, MAX_FUNDS };
