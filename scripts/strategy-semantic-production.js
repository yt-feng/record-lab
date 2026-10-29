'use strict';

// A bounded, one-portfolio production lane. No market requests or full merge.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { canonical } = require('../lib/strategy-claim-patches');
const { normalizeStrategyEvidencePool, strategyEvidenceBudgetFor } = require('../lib/strategy-evidence-pool');
const { normalizePortfolioIdentity } = require('../lib/portfolio-identity');
const { applyStrategySemanticCandidate, retainedStrategySemanticsFor } = require('../lib/strategy-semantics');
const { Ledger, fileFor: ledgerFileFor } = require('./semantic-ledger');
const PERIODS = { '2026Q2': '2026-06-30', '2026Q3': '2026-09-30' };
const CACHE = '.cache/semantic-production';
const LEDGER = 'web/data/semantic-model-ledger.json';
const digest = value => crypto.createHash('sha256').update(canonical(value)).digest('hex');
const same = (a, b) => canonical(a) === canonical(b);
const clone = value => JSON.parse(JSON.stringify(value));
const fail = code => { const error = new Error(code); error.code = code; throw error; };
const read = (file, max = 10000000) => {
  if (fs.lstatSync(file).isSymbolicLink() || fs.statSync(file).size > max) fail('SEMANTIC_FILE_INVALID');
  return JSON.parse(fs.readFileSync(file, 'utf8'));
};
function write(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, JSON.stringify(value) + '\n', { mode: 0o600 }); fs.renameSync(temporary, file);
}
function locate(root, quarter, requestedCode, selectFund = null) {
  const folder = path.join(root, 'web/data/periods', quarter), manifest = read(path.join(folder, 'latest.json'));
  if (manifest.targetPeriodEnd !== PERIODS[quarter] || !Array.isArray(manifest.companyFiles)) fail('SEMANTIC_PHASE_INVALID');
  const matches = [];
  for (const item of manifest.companyFiles) {
    if (!/^\.\/data\/companies\/[A-Za-z0-9_-]{1,64}\.json$/.test(item.path)) fail('SEMANTIC_COMPANY_PATH_INVALID');
    const relative = item.path.slice('./data/'.length), file = path.join(folder, relative), company = read(file, 20000000);
    if (!Array.isArray(company.funds)) fail('SEMANTIC_COMPANY_INVALID');
    company.funds.forEach((fund, index) => {
      if (selectFund ? selectFund(fund) : fund.code === requestedCode || [fund.aliases, fund.shareClassCodes].some(values => Array.isArray(values)
        && values.some(value => value === requestedCode || value?.code === requestedCode))) matches.push({ folder, manifest, company, relative, item, index, fund });
    });
    if (selectFund && matches.length) return matches[0];
  }
  if (selectFund && !matches.length) fail('SEMANTIC_NO_ELIGIBLE_PRODUCT');
  if (matches.length !== 1) fail('SEMANTIC_PORTFOLIO_NOT_UNIQUE');
  return matches[0];
}
function prepare(root, { quarter, requestedCode, runId, runAttempt, maxAttempts = 1, now = new Date().toISOString() }) {
  if (!Object.hasOwn(PERIODS, quarter) || !/^(?:\d{6})?$/.test(requestedCode) || !/^\d{1,20}$/.test(runId)
    || !/^\d{1,5}$/.test(runAttempt) || !Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 8) fail('SEMANTIC_ARGUMENTS_INVALID');
  const store = new Ledger(root);
  function sourceFor(fund) {
    const report = fund?.selectedReport, proof = report?.strategySectionCoverage;
    const pool = normalizeStrategyEvidencePool(report?.strategyEvidencePool, proof, {}, now);
    const identity = pool && normalizePortfolioIdentity(report.strategyClaimsIdentityEvidence, { ...pool.source, fundCode: fund.code }, now);
    return pool && proof.periodEnd === PERIODS[quarter] && identity && strategyEvidenceBudgetFor(report, now).status === 'verified'
      ? { pool, identity } : null;
  }
  const located = locate(root, quarter, requestedCode, requestedCode ? null : fund => {
    const source = sourceReady(fund, quarter, now);
    return source && source.identity.sourcePortfolioCode !== '168401' && fund.code !== '168401'
      && !store.get(`${quarter}:${source.identity.sourcePortfolioCode}`)
      && !retainedStrategySemanticsFor(fund.selectedReport, { code: fund.code, now });
  });
  const { fund } = located, report = fund.selectedReport;
  if (typeof located.item.companyCode !== 'string' || !/^[A-Za-z0-9_-]{1,40}$/.test(located.item.companyCode))
    fail('SEMANTIC_COMPANY_INVALID');
  if (retainedStrategySemanticsFor(report, { code: fund.code, now })) return { status: 'already-admitted', hasWork: false };
  const source = sourceFor(fund);
  if (!source) fail('SEMANTIC_SOURCE_NOT_READY');
  const { pool, identity } = source;
  // This fixed diagnostic source has earlier requests outside this production
  // ledger. It cannot start at zero; import those receipts before using it.
  if (identity.sourcePortfolioCode === '168401' && !store.get(`${quarter}:168401`)) fail('SEMANTIC_PRIOR_HISTORY_IMPORT_REQUIRED');
  const key = `${quarter}:${identity.sourcePortfolioCode}`, previous = clone(store.get(key) || { usageHistory: [], attempts: [] });
  if (!Array.isArray(previous.usageHistory) || !Array.isArray(previous.attempts)
    || previous.usageHistory.length && previous.usageHistoryDigest !== digest(previous.usageHistory)) fail('SEMANTIC_LEDGER_INVALID');
  if (['reserved', 'unknown'].includes(previous.attempts.at(-1)?.status)) fail('SEMANTIC_PRIOR_ATTEMPT_UNRESOLVED');
  if (previous.usageHistory.length >= maxAttempts) fail('SEMANTIC_ATTEMPT_LIMIT');
  const reservationId = `${runId}:${runAttempt}`;
  if (previous.attempts.some(row => row.reservationId === reservationId)) fail('SEMANTIC_RESERVATION_EXISTS');
  const task = { schemaVersion: 1, kind: 'strategy-semantic-production-task', quarter, code: fund.code, requestedCode,
    targetPeriodEnd: PERIODS[quarter], companyPath: located.relative, fundIndex: located.index,
    reportDigest: digest(report), report: clone(report), baselinePoolDigest: pool.digest,
    usageHistory: clone(previous.usageHistory), expectedUsageHistoryDigest: digest(previous.usageHistory),
    reservationId, runId, runAttempt, plannedAt: now, maxAttempts, ledgerKey: key, ledgerPath: ledgerFileFor(key) };
  task.taskDigest = digest(task);
  store.set(key, { ...previous, usageHistoryDigest: task.expectedUsageHistoryDigest,
    attempts: [...previous.attempts, { reservationId, runId, runAttempt, plannedAt: now, finishedAt: null,
      status: 'reserved', pdfRequests: 0, modelRequests: 0, errorCode: null, candidateDigest: null }] });
  write(path.join(root, CACHE, 'task.json'), task);
  store.save();
  return { status: 'reserved', hasWork: true, quarter, code: fund.code, reservationId };
}
function taskFor(root) {
  const task = read(path.join(root, CACHE, 'task.json'), 2000000), value = { ...task }; delete value.taskDigest;
  if (!Object.hasOwn(PERIODS, task.quarter) || task.targetPeriodEnd !== PERIODS[task.quarter]
    || digest(value) !== task.taskDigest || digest(task.report) !== task.reportDigest) fail('SEMANTIC_TASK_CHANGED');
  return task;
}
function finish(root, { now = new Date().toISOString() } = {}) {
  const task = taskFor(root), output = read(path.join(root, CACHE, 'result.json'), 2000000);
  const receipt = read(path.join(root, CACHE, 'receipt.json'), 20000), store = new Ledger(root);
  const entry = clone(store.get(task.ledgerKey)), attempt = entry?.attempts?.at(-1);
  if (!attempt || attempt.reservationId !== task.reservationId || receipt.taskDigest !== task.taskDigest
    || output.taskDigest !== task.taskDigest || receipt.reservationId !== task.reservationId
    || receipt.resultDigest !== digest(output) || receipt.usageHistoryDigest !== digest(output.adapter?.usageHistory || []))
    fail('SEMANTIC_PRODUCER_RECEIPT_INVALID');
  const history = output.adapter?.usageHistory || [];
  if (!same(history.slice(0, task.usageHistory.length), task.usageHistory)) fail('SEMANTIC_HISTORY_RESET');
  entry.usageHistory = clone(history); entry.usageHistoryDigest = digest(history);
  Object.assign(attempt, { finishedAt: output.finishedAt, pdfRequests: output.pdfRequests,
    modelRequests: output.adapter?.modelRequests || 0, status: output.status === 'unknown' ? 'unknown' : 'rejected',
    errorCode: output.errorCode, candidateDigest: receipt.candidateDigest });
  const summary = { schemaVersion: 1, kind: 'strategy-semantic-production-result', quarter: task.quarter, code: task.code,
    status: attempt.status, publication: 'not-published', pdfRequests: attempt.pdfRequests, modelRequests: attempt.modelRequests,
    candidateDigest: receipt.candidateDigest, interpretedSentences: 0, unknownSentences: 0, selectedClaims: 0,
    errorCode: output.errorCode, semanticAnalysisComplete: false };
  let admitted = null;
  try {
    if (output.adapter?.status === 'candidate-ready') {
      const located = locate(root, task.quarter, task.code), before = located.fund.selectedReport;
      if (digest(before) !== task.reportDigest || located.relative !== task.companyPath || located.index !== task.fundIndex)
        fail('SEMANTIC_CONCURRENT_REPORT_CHANGED');
      const checked = applyStrategySemanticCandidate(before, output.adapter.candidate, { code: task.code,
        targetPeriodEnd: task.targetPeriodEnd, now, expectedInputDigest: receipt.inputDigest,
        expectedUsageHistoryDigest: receipt.usageHistoryDigest, expectedCandidateDigest: receipt.candidateDigest });
      located.fund.selectedReport = checked.report;
      // Mirror the same selected report only when the stored report is exact;
      // unrelated quarterly/interim observations retain their original facts.
      for (let i = 0; i < (located.fund.reports || []).length; i++) {
        if (digest(located.fund.reports[i]) === task.reportDigest) located.fund.reports[i] = clone(checked.report);
      }
      const name = `${located.item.companyCode}-r${digest(located.company).slice(0, 20)}.json`;
      const nextRelative = `companies/${name}`;
      located.item.path = `./data/${nextRelative}`;
      admitted = { located, nextRelative };
    }
  } catch (error) {
    if (!/^SEMANTIC_[A-Z_]+$/.test(error?.code || '')) throw error;
    attempt.status = 'rejected'; attempt.errorCode = /^SEMANTIC_[A-Z_]+$/.test(error?.code || '') ? error.code : 'SEMANTIC_ADMISSION_FAILED';
    summary.status = 'rejected'; summary.errorCode = attempt.errorCode;
  }
  if (admitted) {
    // All semantic checks finished before staging. Any I/O failure must stop
    // the workflow, leaving the remote reservation unresolved; it must never
    // become a successful rejected-result step followed by a partial push.
    const { located, nextRelative } = admitted;
    write(path.join(located.folder, nextRelative), located.company);
    write(path.join(located.folder, 'latest.json'), located.manifest);
    if (nextRelative !== located.relative) fs.unlinkSync(path.join(located.folder, located.relative));
    attempt.status = 'admitted'; attempt.errorCode = null;
    Object.assign(summary, { status: 'admitted', publication: 'staged', errorCode: null,
      interpretedSentences: output.adapter.candidate.coverage.interpreted,
      unknownSentences: output.adapter.candidate.coverage.unknown, selectedClaims: output.adapter.candidate.claims.length });
  }
  store.set(task.ledgerKey, entry); store.save(); write(path.join(root, CACHE, 'summary.json'), summary);
  return summary;
}
function sourceReady(fund, quarter, now, explicit = false) {
  const report = fund?.selectedReport, proof = report?.strategySectionCoverage;
  const pool = normalizeStrategyEvidencePool(report?.strategyEvidencePool, proof, {}, now);
  const identity = pool && normalizePortfolioIdentity(report.strategyClaimsIdentityEvidence, { ...pool.source, fundCode: fund.code }, now);
  if (!pool || proof.periodEnd !== PERIODS[quarter] || !identity || strategyEvidenceBudgetFor(report, now).status !== 'verified') return null;
  // Q2 holdings can come from the interim PDF while the strategy proof comes
  // from the quarter PDF. Its parser/pages must belong to that source, not to
  // the selected holdings report. Normalization can retain only the strategy
  // proof; absent parser metadata is then a bounded reparse, never permission
  // to send a model request before the producer verifies the full source/PDF.
  const sourceReports = [report, ...(Array.isArray(fund.reports) ? fund.reports : [])].filter(value => {
    const candidate = value?.strategySectionCoverage;
    return (value?.reportPdfSourceUrl || value?.sourceUrl) === proof.sourceUrl && candidate
      && ['sourceUrl', 'publishedAt', 'kind', 'periodStart', 'periodEnd', 'sourceTextHash']
        .every(key => candidate[key] === proof[key]);
  });
  const reproducible = value => {
    const stats = value.pageStats;
    return ['pypdf', 'pypdf-fallback'].includes(value.parseMethod) && stats?.truncated === false
      && stats.pageNumbersVerified === true && Number.isInteger(stats.totalPages) && stats.totalPages > 0
      && stats.processedPages === stats.totalPages && stats.totalPages <= 300;
  };
  const observedParsers = sourceReports.filter(value => value.parseMethod != null || value.pageStats != null);
  if (observedParsers.length && !observedParsers.some(reproducible) && !explicit) return null;
  return { pool, identity };
}
function prepareBatch(root, { quarter, requestedCode = '', runId, runAttempt, maxAttempts = 1,
  batchSize = 10, now = new Date().toISOString() }) {
  if (!Object.hasOwn(PERIODS, quarter) || !/^(?:\d{6})?$/.test(requestedCode) || !/^[1-9]\d{0,19}$/.test(runId)
    || !/^[1-9]\d{0,4}$/.test(runAttempt) || !Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 8
    || !Number.isInteger(batchSize) || batchSize < 1 || batchSize > 10 || requestedCode && batchSize !== 1) fail('SEMANTIC_ARGUMENTS_INVALID');
  const store = new Ledger(root), folder = path.join(root, 'web/data/periods', quarter);
  const manifest = read(path.join(folder, 'latest.json'));
  if (manifest.targetPeriodEnd !== PERIODS[quarter] || !Array.isArray(manifest.companyFiles)
    || manifest.companyFiles.length > 10000) fail('SEMANTIC_PHASE_INVALID');
  const seen = new Set(), first = [], extra = [], companies = new Set(), paths = new Set();
  const counts = { scannedFundRecords: 0, sourceNotReadyRecords: 0, independentSourceProducts: 0,
    aliasRecords: 0, alreadyAdmittedProducts: 0, priorHistoryBlockedProducts: 0, eligibleProducts: 0 };
  let explicitMatches = 0;
  for (const item of manifest.companyFiles) {
    if (!/^\.\/data\/companies\/[A-Za-z0-9_-]{1,64}\.json$/.test(item.path) || paths.has(item.path)
      || !/^[A-Za-z0-9_-]{1,40}$/.test(item.companyCode)) fail('SEMANTIC_COMPANY_PATH_INVALID');
    paths.add(item.path);
    const relative = item.path.slice('./data/'.length), company = read(path.join(folder, relative), 20000000);
    if (!Array.isArray(company.funds) || company.funds.length > 1000) fail('SEMANTIC_COMPANY_INVALID');
    for (const [index, fund] of company.funds.entries()) {
      if (requestedCode && !(fund.code === requestedCode || [fund.aliases, fund.shareClassCodes].some(values => Array.isArray(values)
        && values.some(value => value === requestedCode || value?.code === requestedCode)))) continue;
      if (requestedCode) explicitMatches++;
      counts.scannedFundRecords++;
      const source = sourceReady(fund, quarter, now, Boolean(requestedCode));
      if (!source) { counts.sourceNotReadyRecords++; continue; }
      if (Buffer.byteLength(JSON.stringify(fund.selectedReport)) > 1900000) { counts.sourceNotReadyRecords++; continue; }
      const key = `${quarter}:${source.identity.sourcePortfolioCode}`;
      if (seen.has(key)) { counts.aliasRecords++; continue; }
      seen.add(key); counts.independentSourceProducts++;
      if (retainedStrategySemanticsFor(fund.selectedReport, { code: fund.code, now })) { counts.alreadyAdmittedProducts++; continue; }
      const previous = store.get(key), last = previous?.attempts.at(-1);
      // A known failed PDF fetch spent no model attempt. Permit one bounded
      // source retry; stable parser failures wait for changed source evidence.
      // Explicit product selection can recheck a known failure after a repair.
      const sourceFailures = previous?.attempts.filter(row => row.status === 'rejected' && row.modelRequests === 0
        && row.pdfRequests === 1 && ['SEMANTIC_PDF_HTTP_REJECTED', 'SEMANTIC_PRODUCER_FAILED'].includes(row.errorCode)) || [];
      const sourceRetry = sourceFailures.length < 2 && sourceFailures.includes(last);
      const retry = previous && !['reserved', 'unknown'].includes(last?.status)
        && previous.usageHistory.length < maxAttempts && (last?.status === 'deferred'
          || requestedCode || sourceRetry || last?.modelRequests > 0
          || last?.reportDigest && last.reportDigest !== digest(fund.selectedReport));
      if (source.identity.sourcePortfolioCode === '168401' && !previous || previous && !retry) {
        counts.priorHistoryBlockedProducts++; continue;
      }
      counts.eligibleProducts++;
      const capture = () => ({ code: fund.code, report: clone(fund.selectedReport), source, key, relative, index });
      if (!companies.has(item.companyCode) && first.length < batchSize) { companies.add(item.companyCode); first.push(capture()); }
      else if (extra.length < batchSize) extra.push(capture());
    }
  }
  if (requestedCode && explicitMatches !== 1) fail('SEMANTIC_PORTFOLIO_NOT_UNIQUE');
  const selected = [...first, ...extra].slice(0, batchSize), tasks = [];
  for (const item of selected) {
    const previous = clone(store.get(item.key) || { usageHistory: [], attempts: [] });
    const reservationId = `${runId}:${runAttempt}:${item.code}`;
    if (previous.attempts.some(row => row.reservationId === reservationId)) fail('SEMANTIC_RESERVATION_EXISTS');
    const task = { schemaVersion: 1, kind: 'strategy-semantic-production-task', quarter, code: item.code, requestedCode,
      targetPeriodEnd: PERIODS[quarter], companyPath: item.relative, fundIndex: item.index,
      reportDigest: digest(item.report), report: item.report, baselinePoolDigest: item.source.pool.digest,
      usageHistory: previous.usageHistory, expectedUsageHistoryDigest: digest(previous.usageHistory), reservationId,
      runId, runAttempt, plannedAt: now, maxAttempts, ledgerKey: item.key, ledgerPath: ledgerFileFor(item.key) };
    task.taskDigest = digest(task);
    const relative = `tasks/${item.code}`;
    write(path.join(root, CACHE, relative, 'task.json'), task);
    tasks.push({ code: item.code, path: relative, ledgerKey: item.key, taskDigest: task.taskDigest });
    store.set(item.key, { ...previous, usageHistoryDigest: task.expectedUsageHistoryDigest,
      attempts: [...previous.attempts, { reservationId, runId, runAttempt, plannedAt: now, finishedAt: null,
        status: 'reserved', reportDigest: task.reportDigest, pdfRequests: 0, modelRequests: 0, errorCode: null, candidateDigest: null }] });
  }
  const batch = { schemaVersion: 1, kind: 'strategy-semantic-production-batch', quarter, runId, runAttempt,
    plannedAt: now, batchSize, maxWorkers: 2, maxSeconds: 1500, maxAttempts, counts, tasks };
  batch.batchDigest = digest(batch);
  write(path.join(root, CACHE, 'batch.json'), batch); store.save();
  return { status: tasks.length ? 'reserved' : 'no-ready-products', hasWork: Boolean(tasks.length), quarter,
    selectedProducts: tasks.length, counts, continueRun: false };
}
function batchFor(root) {
  const batch = read(path.join(root, CACHE, 'batch.json'), 100000), value = { ...batch }; delete value.batchDigest;
  if (digest(value) !== batch.batchDigest || !Object.hasOwn(PERIODS, batch.quarter) || !Array.isArray(batch.tasks)
    || batch.tasks.length > 10 || new Set(batch.tasks.map(row => row.ledgerKey)).size !== batch.tasks.length
    || new Set(batch.tasks.map(row => row.code)).size !== batch.tasks.length) fail('SEMANTIC_TASK_CHANGED');
  for (const item of batch.tasks) if (!/^\d{6}$/.test(item.code) || item.path !== `tasks/${item.code}`) fail('SEMANTIC_TASK_CHANGED');
  return batch;
}
function finishBatch(root, { now = new Date().toISOString() } = {}) {
  const batch = batchFor(root), store = new Ledger(root), folder = path.join(root, 'web/data/periods', batch.quarter);
  const manifest = read(path.join(folder, 'latest.json')), pages = new Map(), summaries = [];
  if (manifest.targetPeriodEnd !== PERIODS[batch.quarter] || !Array.isArray(manifest.companyFiles)) fail('SEMANTIC_PHASE_INVALID');
  for (const item of batch.tasks) {
    const directory = path.join(root, CACHE, item.path), task = read(path.join(directory, 'task.json'), 2000000);
    const unsigned = { ...task }; delete unsigned.taskDigest;
    if (digest(unsigned) !== item.taskDigest || task.taskDigest !== item.taskDigest || task.ledgerKey !== item.ledgerKey
      || task.runId !== batch.runId || task.runAttempt !== batch.runAttempt || task.quarter !== batch.quarter
      || digest(task.report) !== task.reportDigest) fail('SEMANTIC_TASK_CHANGED');
    const entry = clone(store.get(task.ledgerKey)), attempt = entry?.attempts.at(-1);
    if (!attempt || attempt.reservationId !== task.reservationId || attempt.status !== 'reserved'
      || !same(entry.usageHistory, task.usageHistory)) fail('SEMANTIC_RESERVATION_MISMATCH');
    const summary = { code: task.code, ledgerKey: task.ledgerKey, status: 'unknown', candidateDigest: null,
      pdfRequests: null, modelRequests: null, errorCode: 'SEMANTIC_PRODUCER_RECEIPT_MISSING' };
    let output, receipt;
    try {
      output = read(path.join(directory, 'result.json'), 2000000); receipt = read(path.join(directory, 'receipt.json'), 20000);
      const history = output.adapter?.usageHistory || task.usageHistory;
      const modelRequests = output.adapter?.modelRequests || 0;
      if (receipt.taskDigest !== task.taskDigest || output.taskDigest !== task.taskDigest
        || receipt.reservationId !== task.reservationId || receipt.resultDigest !== digest(output)
        || !Array.isArray(history) || receipt.usageHistoryDigest !== digest(history)
        || !same(history.slice(0, task.usageHistory.length), task.usageHistory)
        || history.length > task.usageHistory.length + 1 || ![0, 1].includes(output.pdfRequests)
        || ![0, 1].includes(modelRequests)
        || history.length - task.usageHistory.length !== modelRequests) fail('SEMANTIC_PRODUCER_RECEIPT_INVALID');
      output = { ...output, adapter: output.adapter || { usageHistory: task.usageHistory, modelRequests: 0, status: 'pending' } };
    } catch (error) {
      if (error.code !== 'ENOENT' && !/^SEMANTIC_[A-Z_]+$/.test(error?.code || '') && !(error instanceof SyntaxError)) throw error;
      output = null;
      if (error.code !== 'ENOENT') summary.errorCode = 'SEMANTIC_PRODUCER_RECEIPT_INVALID';
    }
    if (output) {
      entry.usageHistory = clone(output.adapter.usageHistory); entry.usageHistoryDigest = digest(entry.usageHistory);
      Object.assign(summary, { status: output.status === 'unknown' || output.finishedAt === null
          || ['started', 'unknown'].includes(entry.usageHistory.at(-1)?.outcome) ? 'unknown' : output.status === 'deferred' ? 'deferred' : 'rejected',
        pdfRequests: output.pdfRequests, modelRequests: output.adapter.modelRequests, errorCode: output.errorCode,
        candidateDigest: receipt.candidateDigest });
      if (output.adapter.status === 'candidate-ready') {
        try {
          if (!pages.has(task.companyPath)) {
            const matches = manifest.companyFiles.filter(row => row.path === './data/' + task.companyPath);
            if (matches.length !== 1) fail('SEMANTIC_CONCURRENT_REPORT_CHANGED');
            pages.set(task.companyPath, { item: matches[0], company: read(path.join(folder, task.companyPath), 20000000), changed: false });
          }
          const page = pages.get(task.companyPath), fund = page.company.funds?.[task.fundIndex], before = fund?.selectedReport;
          if (fund?.code !== task.code || !before || digest(before) !== task.reportDigest) fail('SEMANTIC_CONCURRENT_REPORT_CHANGED');
          const checked = applyStrategySemanticCandidate(before, output.adapter.candidate, { code: task.code,
            targetPeriodEnd: task.targetPeriodEnd, now, expectedInputDigest: receipt.inputDigest,
            expectedUsageHistoryDigest: receipt.usageHistoryDigest, expectedCandidateDigest: receipt.candidateDigest });
          fund.selectedReport = checked.report;
          for (let index = 0; index < (fund.reports || []).length; index++)
            if (digest(fund.reports[index]) === task.reportDigest) fund.reports[index] = clone(checked.report);
          page.changed = true;
          Object.assign(summary, { status: 'admitted', errorCode: null, selectedClaims: output.adapter.candidate.claims.length });
        } catch (error) {
          if (!/^SEMANTIC_[A-Z_]+$/.test(error?.code || '')) throw error;
          summary.status = 'rejected'; summary.errorCode = error.code;
        }
      }
    }
    Object.assign(attempt, { finishedAt: output?.finishedAt || null, status: summary.status,
      pdfRequests: summary.pdfRequests, modelRequests: summary.modelRequests, errorCode: summary.errorCode,
      candidateDigest: summary.candidateDigest });
    store.set(task.ledgerKey, entry); summaries.push(summary);
  }
  // Every task was checked against its original report before any page rename.
  // I/O errors propagate: the workflow must not publish a partially staged set.
  const replaced = []; let changedPages = 0;
  for (const [old, page] of pages) if (page.changed) {
    changedPages++;
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(page.item.companyCode)) fail('SEMANTIC_COMPANY_INVALID');
    const relative = `companies/${page.item.companyCode}-r${digest(page.company).slice(0, 20)}.json`;
    write(path.join(folder, relative), page.company); page.item.path = './data/' + relative;
    if (relative !== old) replaced.push(old);
  }
  if (changedPages) write(path.join(folder, 'latest.json'), manifest);
  for (const old of replaced) fs.unlinkSync(path.join(folder, old));
  store.save();
  const result = { schemaVersion: 1, kind: 'strategy-semantic-production-batch-result', quarter: batch.quarter,
    batchDigest: batch.batchDigest, status: 'staged', publication: 'staged', products: summaries,
    admittedProducts: summaries.filter(row => row.status === 'admitted').length,
    unknownProducts: summaries.filter(row => row.status === 'unknown').length,
    knownModelRequests: summaries.reduce((total, row) => total + (row.modelRequests || 0), 0),
    unknownRequestAccountingProducts: summaries.filter(row => row.modelRequests === null).length,
    continueRun: false, semanticAnalysisComplete: false };
  write(path.join(root, CACHE, 'summary.json'), result); return result;
}
function readbackBatch(root) {
  const batch = batchFor(root), summary = read(path.join(root, CACHE, 'summary.json')), store = new Ledger(root);
  if (summary.batchDigest !== batch.batchDigest) fail('SEMANTIC_PERSISTED_READBACK_FAILED');
  for (const row of summary.products) {
    const entry = store.get(row.ledgerKey), attempt = entry?.attempts.at(-1);
    if (attempt?.status !== row.status || attempt.candidateDigest !== row.candidateDigest) fail('SEMANTIC_PERSISTED_READBACK_FAILED');
    if (row.status === 'admitted' && retainedStrategySemanticsFor(locate(root, batch.quarter, row.code).fund.selectedReport,
      { code: row.code })?.candidate.candidateDigest !== row.candidateDigest) fail('SEMANTIC_PERSISTED_READBACK_FAILED');
  }
  summary.publication = 'persisted'; write(path.join(root, CACHE, 'summary.json'), summary); return summary;
}
function main() {
  try {
    if (process.env.GITHUB_ACTIONS !== 'true') fail('SEMANTIC_ACTIONS_REQUIRED');
    const command = process.argv[2]; let result;
    if (command === 'prepare') {
      if (!process.env.DEEPSEEK_API_KEY?.trim()) fail('SEMANTIC_CREDENTIALS_UNAVAILABLE');
      result = prepareBatch('.', { quarter: process.env.SEMANTIC_QUARTER, requestedCode: process.env.SEMANTIC_CODE || '',
        runId: process.env.GITHUB_RUN_ID, runAttempt: process.env.GITHUB_RUN_ATTEMPT,
        maxAttempts: Number(process.env.SEMANTIC_MAX_ATTEMPTS || 1),
        batchSize: process.env.SEMANTIC_CODE ? 1 : Number(process.env.SEMANTIC_BATCH_SIZE || 10) });
      if (process.env.GITHUB_OUTPUT) fs.appendFileSync(process.env.GITHUB_OUTPUT, `has_work=${result.hasWork}\n`);
    } else if (command === 'finish') result = fs.existsSync(path.join(CACHE, 'batch.json')) ? finishBatch('.') : finish('.');
    else if (command === 'readback') {
      if (fs.existsSync(path.join(CACHE, 'batch.json'))) {
        result = readbackBatch('.'); process.stdout.write(JSON.stringify(result) + '\n'); return;
      }
      const summary = read(path.join(CACHE, 'summary.json')), task = taskFor('.');
      if (summary.status === 'admitted') {
        const fund = locate('.', task.quarter, task.code).fund;
        if (retainedStrategySemanticsFor(fund.selectedReport, { code: task.code })?.candidate.candidateDigest !== summary.candidateDigest)
          fail('SEMANTIC_PERSISTED_READBACK_FAILED');
        summary.publication = 'persisted'; write(path.join(CACHE, 'summary.json'), summary);
      }
      result = summary;
    } else fail('SEMANTIC_ARGUMENTS_INVALID');
    process.stdout.write(JSON.stringify(result) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ status: 'blocked', errorCode: /^SEMANTIC_[A-Z_]+$/.test(error?.code || '')
      ? error.code : 'SEMANTIC_PRODUCTION_FAILED' }) + '\n'); process.exitCode = 1;
  }
}
if (require.main === module) main();
module.exports = { prepare, finish, taskFor, locate, digest, CACHE, LEDGER, Ledger, ledgerFileFor,
  prepareBatch, finishBatch, batchFor, readbackBatch };
