'use strict';

// A bounded, one-portfolio production lane. No market requests or full merge.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { canonical } = require('../lib/strategy-claim-patches');
const { normalizeStrategyEvidencePool, strategyEvidenceBudgetFor } = require('../lib/strategy-evidence-pool');
const { normalizePortfolioIdentity } = require('../lib/portfolio-identity');
const { applyStrategySemanticCandidate, retainedStrategySemanticsFor } = require('../lib/strategy-semantics');
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
  const ledgerPath = path.join(root, LEDGER), ledger = fs.existsSync(ledgerPath) ? read(ledgerPath) :
    { schemaVersion: 1, kind: 'strategy-semantic-ledger', products: {} };
  if (ledger.schemaVersion !== 1 || ledger.kind !== 'strategy-semantic-ledger' || !ledger.products || Array.isArray(ledger.products))
    fail('SEMANTIC_LEDGER_INVALID');
  function sourceFor(fund) {
    const report = fund?.selectedReport, proof = report?.strategySectionCoverage;
    const pool = normalizeStrategyEvidencePool(report?.strategyEvidencePool, proof, {}, now);
    const identity = pool && normalizePortfolioIdentity(report.strategyClaimsIdentityEvidence, { ...pool.source, fundCode: fund.code }, now);
    return pool && proof.periodEnd === PERIODS[quarter] && identity && strategyEvidenceBudgetFor(report, now).status === 'verified'
      ? { pool, identity } : null;
  }
  const located = locate(root, quarter, requestedCode, requestedCode ? null : fund => {
    const source = sourceFor(fund);
    const report = fund.selectedReport, stats = report?.pageStats;
    const reproducible = ['pypdf', 'pypdf-fallback'].includes(report?.parseMethod)
      && stats?.truncated === false && stats.pageNumbersVerified === true && Number.isInteger(stats.totalPages)
      && stats.processedPages === stats.totalPages && stats.totalPages <= 300
      && (report.reportPdfSourceUrl || report.sourceUrl) === report.strategySectionCoverage?.sourceUrl;
    return source && reproducible && source.identity.sourcePortfolioCode !== '168401' && fund.code !== '168401'
      && !ledger.products[`${quarter}:${source.identity.sourcePortfolioCode}`]
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
  if (identity.sourcePortfolioCode === '168401' && !ledger.products[`${quarter}:168401`]) fail('SEMANTIC_PRIOR_HISTORY_IMPORT_REQUIRED');
  const key = `${quarter}:${identity.sourcePortfolioCode}`, previous = ledger.products[key] || { usageHistory: [], attempts: [] };
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
    reservationId, runId, runAttempt, plannedAt: now, maxAttempts, ledgerKey: key };
  task.taskDigest = digest(task);
  ledger.products[key] = { ...previous, usageHistoryDigest: task.expectedUsageHistoryDigest,
    attempts: [...previous.attempts, { reservationId, runId, runAttempt, plannedAt: now, finishedAt: null,
      status: 'reserved', pdfRequests: 0, modelRequests: 0, errorCode: null, candidateDigest: null }] };
  write(path.join(root, CACHE, 'task.json'), task);
  write(ledgerPath, ledger);
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
  const receipt = read(path.join(root, CACHE, 'receipt.json'), 20000), ledger = read(path.join(root, LEDGER));
  const entry = ledger.products[task.ledgerKey], attempt = entry?.attempts?.at(-1);
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
  write(path.join(root, LEDGER), ledger); write(path.join(root, CACHE, 'summary.json'), summary);
  return summary;
}
function main() {
  try {
    if (process.env.GITHUB_ACTIONS !== 'true') fail('SEMANTIC_ACTIONS_REQUIRED');
    const command = process.argv[2]; let result;
    if (command === 'prepare') {
      if (!process.env.DEEPSEEK_API_KEY?.trim()) fail('SEMANTIC_CREDENTIALS_UNAVAILABLE');
      result = prepare('.', { quarter: process.env.SEMANTIC_QUARTER, requestedCode: process.env.SEMANTIC_CODE || '',
        runId: process.env.GITHUB_RUN_ID, runAttempt: process.env.GITHUB_RUN_ATTEMPT,
        maxAttempts: Number(process.env.SEMANTIC_MAX_ATTEMPTS || 1) });
      if (process.env.GITHUB_OUTPUT) fs.appendFileSync(process.env.GITHUB_OUTPUT, `has_work=${result.hasWork}\n`);
    } else if (command === 'finish') result = finish('.');
    else if (command === 'readback') {
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
module.exports = { prepare, finish, taskFor, locate, digest, CACHE, LEDGER };
