'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { strategyClaimsFor, retainStrategyClaims, canonical, METHOD } = require('../lib/strategy-claims');
const { normalizeReport } = require('../lib/fund-analysis');
const { buildSnapshot } = require('../scripts/build-data');
const { mergeSnapshots } = require('../scripts/merge-parts');
const { strategyEvidenceBudgetFor } = require('../lib/strategy-evidence-pool');

// Only in-memory, synthetic extracted pages: no PDF or network requests.
const NOW = '2026-10-27T02:00:00Z', AT = '2026-10-26T19:00:00.123456+00:00';
const clone = x => JSON.parse(JSON.stringify(x));
const sha = x => crypto.createHash('sha256').update(x).digest('hex');
const opts = { code: '000001', asOf: '2026-10-27', now: NOW };
const bodies = [undefined, '本基金增配科技行业😀，并保持审慎。',
  '如果盈利改善，本基金将增配股票。', '本基金未增配股票。',
  '报告引用：“市场平稳。在本报告期我们持续看好人工智能方向。后续观察”。',
  '本季度行情未知。', '本基金增配股票和债券。', '我们可能看好股票。',
  '本基金对股票保持谨慎。', '本基金减少对股票的谨慎。',
  '本基金增配科技行业' + '😀'.repeat(150) + '。', '如果上季度盈利改善，本基金将增配股票。'];
const python = 'import json,sys;from test_strategy_evidence_pool import extracted,REPORT,BODY;print(json.dumps([{**REPORT,**extracted(body=s if s is not None else BODY)} for s in json.loads(sys.stdin.read())],ensure_ascii=False))';
const fixtures = JSON.parse(execFileSync('python3', ['-c', python], {
  cwd: path.join(__dirname, '..'), env: { ...process.env, PYTHONPATH: 'tests:scripts' },
  input: JSON.stringify(bodies), encoding: 'utf8', maxBuffer: 1024 * 1024,
}));
const record = (index = 0) => clone(fixtures[index]);
const config = { targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31', funds: [{ code: '000001' }] };
const catalog = { fundCount: 2, companyCount: 1, companies: [{ code: 'co1', name: '合成公司', fundCount: 2 }],
  funds: ['000001', '000002'].map(code => ({ code, companyCode: 'co1', companyName: '合成公司' })) };
const holding = { stockCode: '601318', stockName: '中国平安', market: 'CN', currency: 'CNY', shares: 100, weightPct: 10, marketValueYuan: 100 };
function report(value = record()) { return { coverage: 'top10', holdings: [holding], navYuan: 1000, ...value }; }
function snapshot(value = record(), { interim = false, at = AT } = {}) {
  const reports = [report(value)];
  if (interim) reports.unshift({ fundCode: '000001', periodEnd: '2026-06-30', publishedAt: '2026-08-28', kind: 'semiannual',
    coverage: 'full', holdings: [holding], navYuan: 1000, sourceUrl: 'https://example.org/interim.pdf' });
  return buildSnapshot({ asOf: '2026-10-27', retrievedAt: at, errors: [], barsByStock: {}, funds: [{ code: '000001', name: '合成基金',
    company: '合成公司', companyId: 'co1', managers: [], reports }] }, { config, now: NOW });
}
function legacy(value = record()) {
  for (const key of ['strategyClaims', 'strategyClaimsIdentityEvidence', 'strategyEvidencePool', 'strategyEvidenceBudget', 'strategyClaimsStatus']) delete value[key];
  return value;
}
function observed(value, at) {
  value.strategyEvidenceRetrievedAt = at;
  value.strategyClaims.source.retrievedAt = at;
  value.strategyEvidencePool.source.retrievedAt = at;
  value.strategyClaimsIdentityEvidence.retrievedAt = at;
  return value;
}

test('Python private section extraction is admitted without changing rule scope or original clocks', () => {
  const raw = record(), normalized = normalizeReport(raw, opts);
  assert.equal(strategyClaimsFor(raw, opts).method, METHOD);
  assert.equal(normalized.strategyClaimsStatus, 'scanned');
  assert.equal(normalized.strategyClaims.semanticAnalysisComplete, false);
  assert.equal(normalized.strategyClaims.managerAttribution, 'fund-report-not-individual-speaker');
  assert.equal(normalized.strategyClaims.source.retrievedAt, AT);
  assert.equal(normalized.strategyClaimsIdentityEvidence.retrievedAt, AT);
  assert.equal(normalized.strategyEvidenceBudget.status, 'verified');
  assert.deepEqual(normalizeReport(normalized, opts).strategyClaims, normalized.strategyClaims);
  assert.equal(strategyEvidenceBudgetFor(normalized, NOW).status, 'verified');
  assert.notEqual(raw.strategyClaims.source.sourceTextHash, raw.strategyClaimsIdentityEvidence.sourceTextHash);
});

test('Unicode codepoint spans and bounded 160-codepoint projections survive repeated normalization', () => {
  for (const index of [1, 10]) {
    const raw = record(index), normalized = normalizeReport(raw, opts);
    assert.ok(strategyClaimsFor(raw, opts));
    assert.ok(normalized.strategyClaims);
    assert.equal(strategyEvidenceBudgetFor(normalized, NOW).status, 'verified');
    assert.deepEqual(normalizeReport(normalized, opts).strategyClaims, normalized.strategyClaims);
    assert.equal(normalized.strategyAnalysis.evidence[0].text, raw.strategyAnalysis.evidence[0].text);
  }
  const raw = record(1), second = raw.strategyClaims.claims.find(x => x.clauseIndex === 1);
  assert.ok(second.clauseStart < raw.strategyClaims.evidence[0].text.indexOf('并'));
  second.clauseStart += 1;
  assert.equal(strategyClaimsFor(raw, opts), null);
});

test('conditional, negative, quoted, ambiguous and unrecognized statements retain unknown distinctions', () => {
  for (let index = 2; index < 10; index++) assert.ok(strategyClaimsFor(record(index), opts), `fixture ${index}`);
  assert.equal(strategyClaimsFor(record(2), opts).claims.at(-1).direction, 'unknown');
  assert.equal(strategyClaimsFor(record(3), opts).claims[0].negation, 'negated');
  assert.ok(strategyClaimsFor(record(4), opts).claims.every(x => x.classificationStatus === 'unknown'));
  assert.equal(strategyClaimsFor(record(5), opts).coverage.classifiedClauseCount, 0);
  const oldConditional = strategyClaimsFor(record(11), opts);
  assert.ok(oldConditional);
  assert.ok(oldConditional.claims.some(x => x.time === 'unspecified' && x.conditional && x.unknownReasons.includes('OTHER_PERIOD_REFERENCE')));
});

test('source, identity, timestamps, full-scan accounting and quote budget each fail closed', () => {
  const mutations = [
    x => { x.strategyClaims.method = 'explicit-strategy-claims-v1'; },
    x => { x.strategyClaims.source.sourceUrl = 'https://example.org/other.pdf'; },
    x => { x.strategyClaims.source.sectionContentHash = '0'.repeat(64); },
    x => { x.strategyClaims.source.pages = [99]; },
    x => { x.strategyClaims.source.periodStart = '2026-01-01'; },
    x => { x.strategyClaims.source.digest = '0'.repeat(64); },
    x => { x.strategyClaims.source.retrievedAt = '2026-10-27T03:00:00Z'; },
    x => { x.strategyClaimsIdentityEvidence = null; },
    x => { x.strategyClaimsIdentityEvidence.sourceUrl = 'https://example.org/interim.pdf'; },
    x => { x.strategyClaimsIdentityEvidence.kind = 'semiannual'; },
    x => { x.strategyClaimsIdentityEvidence.pdfSha256 = 'bad'; },
    x => { x.strategyClaimsIdentityEvidence.retrievedAt = '2026-10-26T19:00:00.123457+00:00'; },
    x => { x.strategyClaims.semanticAnalysisComplete = true; },
    x => { x.strategyClaims.coverage.scannedClauseCount++; },
    x => { x.strategyClaims.coverage.omittedSentenceCount++; },
    x => { x.strategyClaims.coverage.unknownClauseCount = 0; },
    x => { x.strategyClaims.evidence[0].textHash = '0'.repeat(64); },
    x => { x.strategyClaims.claims[0].sentenceHash = '0'.repeat(64); },
    x => { x.strategyClaims.claims[0].matchedSpans[0].end++; },
    x => { x.strategyClaims.claims[0].subject.isIndividualManager = true; },
    x => { x.strategyClaims.claims[0].provesTradeReason = true; },
    x => { x.strategyClaims.publicQuoteBudget.usedQuoteChars++; },
    x => { x.strategyEvidencePool.quotes[0].text += '外加'; },
    x => { x.narrative.text = '新造调仓原因'; },
    x => { x.strategyExcerpts[0].evidencePoolId = 'q' + '0'.repeat(64); },
  ];
  for (const [index, mutate] of mutations.entries()) {
    const raw = record(); mutate(raw);
    assert.equal(strategyClaimsFor(raw, opts), null, `mutation ${index}`);
  }
  assert.equal(strategyClaimsFor(record(), { ...opts, code: '999999' }), null);
  assert.equal(strategyClaimsFor(record(), { ...opts, asOf: '2026-07-01' }), null);
});

test('literal predicates cannot be reclassified as a different time, polarity, direction or individual cause', () => {
  const a = record(3); a.strategyClaims.claims[0].negation = 'affirmed'; a.strategyClaims.claims[0].direction = 'increase';
  assert.equal(strategyClaimsFor(a, opts), null);
  const b = record(2); Object.assign(b.strategyClaims.claims.at(-1), { time: 'report-period', conditional: false, conditionText: null, kind: 'portfolio-operation', direction: 'increase' });
  assert.equal(strategyClaimsFor(b, opts), null);
  const c = record(); const reason = c.strategyClaims.claims.find(x => x.reason); reason.reason.provesIndividualStockReason = true;
  assert.equal(strategyClaimsFor(c, opts), null);
  const d = record(); d.strategyClaims.claims.find(x => x.stance).stance = 'negative';
  assert.equal(strategyClaimsFor(d, opts), null);
});

test('new real PDF observation binds an explicit old strategy clock without updating it', () => {
  const raw = observed(record(), '2026-10-27T01:00:00.123456Z');
  raw.strategyEvidenceRetrievedAt = AT; raw.strategyClaimsDerivedFromObservedAt = AT;
  assert.ok(strategyClaimsFor(raw, opts));
  const normalized = normalizeReport(raw, opts);
  assert.equal(normalized.strategyEvidenceRetrievedAt, AT);
  assert.equal(normalized.strategyClaims.source.retrievedAt, '2026-10-27T01:00:00.123456Z');
  assert.equal(normalized.strategyClaims.source.digest, record().strategyClaims.source.digest);
  const earlier = observed(record(), '2026-10-26T19:00:00.123455+00:00');
  earlier.strategyEvidenceRetrievedAt = AT; earlier.strategyClaimsDerivedFromObservedAt = AT;
  assert.equal(strategyClaimsFor(earlier, opts), null);
  delete raw.strategyClaimsDerivedFromObservedAt;
  assert.equal(strategyClaimsFor(raw, opts), null);
});

test('legacy snippets remain useful but never claim a verified shared budget or new rule scan', () => {
  const raw = legacy(), normalized = normalizeReport(raw, opts);
  assert.equal(normalized.strategySectionCoverage.complete, true);
  assert.equal(normalized.narrative.text, raw.narrative.text);
  assert.equal(normalized.strategyEvidenceBudget.status, 'legacy-unbudgeted');
  assert.equal(strategyClaimsFor(normalized, opts), null);
  const textOnly = { ...raw }; delete textOnly.strategySectionCoverage;
  assert.equal(normalizeReport(textOnly, opts).strategyEvidenceBudget.status, 'legacy-unbudgeted');
});

test('admission failure does not erase valid holdings and adds a fixed analysis error', () => {
  const value = record(); value.strategyClaims.coverage.scannedClauseCount++;
  const built = snapshot(value);
  assert.equal(built.funds.length, 1); assert.equal(built.funds[0].rows.length, 1);
  assert.equal(built.funds[0].selectedReport.strategyClaims, null);
  assert.ok(built.errors.some(x => x.errorCode === 'INVALID_STRATEGY_CLAIMS'));
});

test('new invalid quote projections cannot publish extra text or silently repair their own budget', () => {
  for (const mutate of [x => { x.narrative.text = '池外文字'.repeat(400); },
    x => { x.strategyEvidencePool.digest = '0'.repeat(64); }]) {
    const value = record(); mutate(value);
    const once = normalizeReport(value, opts), twice = normalizeReport(once, opts);
    for (const normalized of [once, twice]) {
      assert.equal(normalized.strategyEvidenceBudget.status, 'partial');
      assert.equal(normalized.narrative.text, null);
      assert.deepEqual(normalized.strategyExcerpts, []);
      assert.equal(normalized.strategyAnalysis, null);
      assert.equal(normalized.strategyClaims, null);
      assert.equal(normalized.strategySectionCoverage.complete, true);
      assert.ok(!JSON.stringify(normalized).includes('池外文字'));
    }
    const built = snapshot(value);
    assert.equal(built.funds.length, 1);
    assert.ok(built.errors.some(x => x.errorCode === 'INVALID_STRATEGY_QUOTE_BUDGET'));
    assert.equal(built.funds[0].selectedReport.strategyEvidenceBudget.status, 'partial');
  }
});

test('full interim holdings retain independent quarterly source, identity, hashes and observed time', () => {
  const built = snapshot(record(), { interim: true }), selected = built.funds[0].selectedReport;
  assert.equal(selected.kind, 'semiannual'); assert.equal(selected.sourceUrl, 'https://example.org/interim.pdf');
  assert.equal(selected.strategyClaims.source.kind, 'quarterly');
  assert.equal(selected.strategyClaimsIdentityEvidence.sourceUrl, 'https://example.org/quarter.pdf');
  assert.equal(selected.strategyClaims.source.retrievedAt, AT);
  assert.ok(strategyClaimsFor(selected, opts));
});

test('same verified source and hashes upgrade complete legacy analysis while preserving all old clocks', () => {
  const old = report(legacy()); old.managerEvidenceRetrievedAt = AT; old.navRetrievedAt = AT; old.evidenceRetrievedAt = AT;
  const incoming = report(observed(record(), '2026-10-27T01:00:00Z'));
  const before = clone(old);
  assert.equal(retainStrategyClaims(old, incoming, opts), true);
  for (const key of ['strategyEvidenceRetrievedAt', 'managerEvidenceRetrievedAt', 'navRetrievedAt', 'evidenceRetrievedAt', 'holdings', 'sourceUrl', 'publishedAt']) assert.deepEqual(old[key], before[key]);
  assert.equal(old.strategyClaimsDerivedFromObservedAt, AT);
  assert.ok(strategyClaimsFor(old, opts));
  assert.equal(retainStrategyClaims(old, incoming, opts), false);
  const wrong = report(legacy()); wrong.strategySectionCoverage.sections[0].contentHash = '0'.repeat(64);
  assert.equal(retainStrategyClaims(wrong, incoming, opts), false);
  const oneMicrosecondNewer = report(legacy());
  oneMicrosecondNewer.strategyEvidenceRetrievedAt = '2026-10-26T19:00:00.123457+00:00';
  assert.equal(retainStrategyClaims(oneMicrosecondNewer, record(), opts), true);
  assert.equal(oneMicrosecondNewer.strategyEvidenceRetrievedAt, AT);
  assert.ok(strategyClaimsFor(oneMicrosecondNewer, opts));
});

test('merge upgrades same-source claims even when retained holdings and complete section already win', () => {
  const old = mergeSnapshots({ catalog, config, now: NOW, snapshots: [snapshot(legacy(), { interim: true })] });
  const input = snapshot(observed(record(), '2026-10-27T01:00:00Z'), { at: '2026-10-27T01:00:00Z' });
  assert.ok(strategyClaimsFor(input.funds[0].selectedReport, opts), 'input admitted');
  assert.equal(retainStrategyClaims(clone(old.companySnapshots.co1.funds[0].selectedReport), input.funds[0].selectedReport, opts), true, 'direct upgrade');
  const merged = mergeSnapshots({ catalog, config, now: NOW, previousManifest: old.manifest, previousCompanies: old.companySnapshots, snapshots: [input] });
  assert.ok(merged.companySnapshots.co1, JSON.stringify({keys:Object.keys(merged.companySnapshots),errors:merged.errors,old:Object.keys(old.companySnapshots),counts:merged.counts}));
  const fund = merged.companySnapshots.co1.funds[0], previous = old.companySnapshots.co1.funds[0];
  assert.equal(fund.selectedReport.kind, 'semiannual');
  assert.equal(fund.retrievedAt, previous.retrievedAt);
  assert.equal(fund.selectedReport.strategyEvidenceRetrievedAt, AT);
  assert.equal(fund.selectedReport.strategyClaims.source.retrievedAt, '2026-10-27T01:00:00Z');
  assert.equal(fund.selectedReport.strategyClaimsDerivedFromObservedAt, AT);
  assert.ok(strategyClaimsFor(fund.selectedReport, opts));
  assert.deepEqual(fund.narrative, fund.selectedReport.narrative);
  assert.ok(fund.selectedReport.strategyEvidencePool.quotes.some(quote => quote.text === fund.narrative.text));
  assert.equal(merged.changed, true);
});

test('later partial or invalid analysis cannot overwrite previously admitted finite scan', () => {
  const old = mergeSnapshots({ catalog, config, now: NOW, snapshots: [snapshot(record(), { interim: true })] });
  const bad = record(); bad.strategyClaims.coverage.scannedClauseCount++;
  const merged = mergeSnapshots({ catalog, config, now: NOW, previousManifest: old.manifest, previousCompanies: old.companySnapshots,
    snapshots: [snapshot(bad, { at: '2026-10-27T01:00:00Z' })] });
  const selected = (merged.companySnapshots.co1 || old.companySnapshots.co1).funds[0].selectedReport;
  assert.equal(selected.strategyClaims.source.digest, record().strategyClaims.source.digest);
  assert.equal(selected.strategyClaims.source.retrievedAt, AT);
});

test('new equal or stronger holdings keep older verified analysis with its own actual source clock', () => {
  for (const strengthen of [false, true]) {
    const old = mergeSnapshots({ catalog, config, now: NOW, snapshots: [snapshot(record())] });
    const bad = observed(record(), '2026-10-27T01:00:00Z');
    bad.strategyClaims.coverage.scannedClauseCount++;
    if (strengthen) {
      bad.coverage = 'full';
      bad.holdings = [holding, { ...holding, stockCode: '600001', stockName: '合成证券' }];
    }
    const input = snapshot(bad, { at: '2026-10-27T01:00:00Z' });
    const merged = mergeSnapshots({ catalog, config, now: NOW, previousManifest: old.manifest, previousCompanies: old.companySnapshots, snapshots: [input] });
    const fund = merged.companySnapshots.co1.funds[0], selected = fund.selectedReport;
    assert.equal(fund.retrievedAt, '2026-10-27T01:00:00Z');
    assert.equal(selected.holdings.length, strengthen ? 2 : 1);
    assert.equal(selected.strategyClaims.source.retrievedAt, AT);
    assert.equal(selected.strategyEvidenceRetrievedAt, AT);
    assert.equal(selected.strategyClaimsIdentityEvidence.retrievedAt, AT);
    assert.equal(selected.strategyEvidencePool.source.retrievedAt, AT);
    assert.ok(strategyClaimsFor(selected, opts));
    assert.equal(strategyEvidenceBudgetFor(selected, NOW).status, 'verified');
    assert.deepEqual(fund.narrative, selected.narrative);
  }
});

test('pool digest and claims source digest are independent of observed clock and bound to method', () => {
  const raw = record(), source = raw.strategyClaims.source;
  assert.equal(source.digest, sha(canonical({ ...Object.fromEntries(Object.entries(source).filter(([key]) => !['retrievedAt', 'digest'].includes(key))), method: METHOD })));
  const changed = clone(raw); changed.strategyClaims.source.digest = sha(canonical({ ...Object.fromEntries(Object.entries(source).filter(([key]) => !['retrievedAt', 'digest'].includes(key))), method: 'explicit-strategy-claims-v1' }));
  assert.equal(strategyClaimsFor(changed, opts), null);
});
