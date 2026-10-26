'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');
const { buildSnapshot } = require('../scripts/build-data');
const { analyzeQuarter, generate } = require('../scripts/analyze-data');

const NOW = '2026-10-26T12:00:00Z';
const RETRIEVED = '2026-10-26T11:00:00Z';
const TARGET = '2026-06-30', BASELINE = '2026-03-31';
const holding = (code = '600001', changes = {}) => ({ stockCode: code, stockName: '合成证券', market: 'CN', currency: 'CNY',
  shares: 100, weightPct: 5, marketValueYuan: 500, ...changes });
function makeFund(code = '000001', options = {}) {
  const { current = [holding()], previous = [holding('600001', { shares: 50 })], navYuan = 10000,
    proven = true, identity = code, aliases = [code], barsByStock = {}, narrative = null } = options;
  const base = { fundCode: code, kind: 'quarterly', coverage: 'top10', navYuan, holdings: current,
    sourceUrl: `https://example.org/${code}-current.pdf`, periodEnd: TARGET, publishedAt: '2026-07-20', narrative };
  const input = { code, name: `合成基金${code}`, company: '合成公司', companyId: 'co1', managers: [{ name: '合成经理' }],
    managersAsOf: '2026-10-26', metadataSourceUrl: 'https://example.org/profile', shareClassCodes: aliases,
    sourcePortfolioCode: proven ? identity : null, portfolioSourceUrl: proven ? 'https://example.org/identity.pdf' : null,
    reports: [base, ...(previous === null ? [] : [{ ...base, sourceUrl: `https://example.org/${code}-prior.pdf`,
      periodEnd: BASELINE, publishedAt: '2026-04-20', holdings: previous, narrative: null }])] };
  return buildSnapshot({ asOf: '2026-10-26', retrievedAt: RETRIEVED, funds: [input], barsByStock, errors: [] },
    { now: NOW, config: { funds: [{ code }], targetPeriodEnd: TARGET, baselinePeriodEnd: BASELINE } }).funds[0];
}
function fixture(funds, options = {}) {
  const docs = {};
  funds.forEach((fund, i) => { docs[`./data/companies/co-${i}.json`] = { schemaVersion: 1, sourceMode: 'actions',
    targetPeriodEnd: TARGET, baselinePeriodEnd: BASELINE, funds: [fund] }; });
  const manifest = { schemaVersion: 1, sourceMode: 'actions', targetPeriodEnd: TARGET, baselinePeriodEnd: BASELINE,
    asOf: '2026-10-26', retrievedAt: RETRIEVED, coverage: { fundCount: funds.length, catalogFundCount: funds.length + 10 },
    companyFiles: Object.keys(docs).map(file => ({ path: file })) };
  const parts = new Map();
  const result = analyzeQuarter(manifest, file => docs[file], { now: NOW, writePart: (file, value) => parts.set(file, value), ...options });
  return { result, parts, docs, manifest };
}
const contributions = (parts, key) => [...parts.values()].filter(part => part.kind === 'stock-contributions')
  .flatMap(part => part.stocks).filter(stock => stock.key === key).flatMap(stock => stock.contributions);
const portfolios = parts => [...parts.values()].filter(part => part.kind === 'portfolio-evidence').flatMap(part => part.portfolios);

test('all stocks retain separate disclosed and NAV-weight amounts without adding both or mixing quoted currencies', () => {
  const a = makeFund('000001', { current: [holding(), holding('00700', { market: 'HK', currency: 'HKD', marketValueYuan: 200, weightPct: 2 })] });
  const b = makeFund('000002', { navYuan: 20000, current: [holding('600001', { marketValueYuan: 700 })] });
  const { result, parts } = fixture([a, b]);
  const cn = result.stocks.find(stock => stock.key === 'CN:600001');
  const hk = result.stocks.find(stock => stock.key === 'HK:00700');
  assert.equal(cn.currentPortfolioCount, 2);
  assert.equal(cn.disclosedValueYuan, 1200);
  assert.equal(cn.navWeightEstimateYuan, 1500);
  assert.equal(cn.knownValueYuan, 1200);
  assert.equal(cn.valueCoverage.unit, 'CNY');
  assert.equal(hk.currency, 'HKD');
  assert.equal(hk.disclosedValueYuan, 200);
  assert.equal(hk.valueCoverage.unit, 'CNY');
  assert.equal(contributions(parts, cn.key).length, 2);
  assert.equal(result.coverage.complete, false);
});

test('share classes with explicit common identity contribute one portfolio, not summed NAV or positions', () => {
  const a = makeFund('000001', { identity: '000001', aliases: ['000001', '000002'] });
  const c = makeFund('000002', { identity: '000001', aliases: ['000001', '000002'] });
  const { result, parts } = fixture([a, c]);
  assert.equal(result.coverage.duplicatesRemoved, 1);
  assert.equal(result.stocks[0].currentPortfolioCount, 1);
  assert.equal(result.stocks[0].disclosedValueYuan, 500);
  assert.deepEqual(portfolios(parts)[0].shareClassCodes, ['000001', '000002']);
  assert.equal(contributions(parts, 'CN:600001')[0].portfolioId, 'portfolio:000001');
});

test('unproven identities and contradictory aliases remain listed but are excluded from additive amounts', () => {
  const unresolved = fixture([makeFund('000001', { proven: false }), makeFund('000002', { proven: false })]);
  assert.equal(unresolved.result.stocks[0].currentPortfolioCount, 2);
  assert.equal(unresolved.result.stocks[0].identityUnresolvedCount, 2);
  assert.equal(unresolved.result.stocks[0].disclosedValueYuan, null);
  const a = makeFund('000001', { aliases: ['000001', '000003'] });
  const b = makeFund('000002', { aliases: ['000002', '000003'] });
  const conflicting = fixture([a, b]);
  assert.equal(conflicting.result.coverage.identityConflictGroups, 2);
  assert.equal(conflicting.result.stocks[0].aggregationEligiblePortfolioCount, 0);
  assert.equal(conflicting.result.stocks[0].knownValueYuan, null);
});

test('missing current disclosure is never treated as zero position or a proven sale', () => {
  const { result, parts } = fixture([makeFund('000001', { current: [holding('600002')], previous: [holding()] })]);
  const gone = result.stocks.find(stock => stock.key === 'CN:600001');
  assert.equal(gone.currentPortfolioCount, 0);
  assert.equal(gone.disclosedValueYuan, null);
  assert.equal(gone.changes.leftDisclosure, 1);
  const row = contributions(parts, gone.key)[0];
  assert.equal(row.current, null);
  assert.equal(row.sharesDelta, null);
  assert.equal(row.change, 'leftDisclosure');
  assert.equal(row.provesTrade, false);
  assert.equal(row.disclosedValueYuan, null);
});

test('only an exact adjacent baseline supports quantities and quarter price intervals', () => {
  const fund = makeFund();
  fund.previousReport.periodEnd = '2025-12-31';
  const { result, parts } = fixture([fund]);
  assert.equal(result.stocks[0].changes.noBaseline, 1);
  assert.equal(result.stocks[0].comparableSharesDelta, null);
  assert.equal(contributions(parts, result.stocks[0].key)[0].previous, null);
  const valid = fixture([makeFund()]);
  assert.equal(valid.result.stocks[0].changes.increase, 1);
  assert.equal(valid.result.stocks[0].comparableSharesDelta, 50);
  assert.equal(contributions(valid.parts, 'CN:600001')[0].change, 'increase');
});

test('an invalid current holding does not turn a malformed row into a disclosure exit', () => {
  const fund = makeFund();
  fund.rows[0].current.currency = 'HKD';
  const { result } = fixture([fund]);
  assert.equal(result.stocks.length, 0);
  assert.equal(result.coverage.invalidRows, 1);
  assert.equal(result.coverage.complete, false);
});

test('NAV uses the selected report date and missing disclosed value can use the separate estimate', () => {
  const fund = makeFund('000001', { navYuan: 10000, current: [holding('600001', { weightPct: 10, marketValueYuan: null })] });
  fund.overviewNavYuan = 999999999;
  fund.previousReport.navYuan = 888888888;
  const { result, parts } = fixture([fund]);
  assert.equal(result.stocks[0].disclosedValueYuan, null);
  assert.equal(result.stocks[0].navWeightEstimateYuan, 1000);
  assert.equal(result.stocks[0].knownValueYuan, 1000);
  assert.equal(portfolios(parts)[0].report.navYuan, 10000);
  assert.equal(portfolios(parts)[0].report.periodEnd, TARGET);
});

test('quarter strategy citations do not borrow the interim half-year rationale or infer stock motives', () => {
  const fund = makeFund('000001', { narrative: { text: '半年配置观点不能替代单季原因。', periodStart: '2026-01-01', periodEnd: TARGET,
    sourceUrl: 'https://example.org/interim.pdf', page: 8 } });
  fund.selectedReport.strategyExcerpts = [{ text: '本季度组合根据估值调整行业配置。', periodStart: '2026-04-01', periodEnd: TARGET,
    sourceUrl: 'https://example.org/quarter.pdf', page: 7 }];
  const { result, parts } = fixture([fund]);
  const strategy = portfolios(parts)[0].strategy;
  assert.deepEqual(strategy.excerpts.map(item => item.sourceUrl), ['https://example.org/quarter.pdf']);
  assert.equal(strategy.excerpts[0].page, 7);
  assert.equal(strategy.explainsIndividualStockTrades, false);
  assert.ok(result.themes.some(theme => theme.name === '估值/基本面' && theme.portfolioCount === 1));
  fund.selectedReport.strategyExcerpts = [];
  assert.equal(portfolios(fixture([fund]).parts)[0].strategy.status, 'missing');
});

test('current profile managers are not silently attributed to the historical report', () => {
  const current = makeFund();
  current.reportManagers = [{ name: '未核验任期' }];
  current.managerAttribution = 'current-profile';
  const result = fixture([current]);
  assert.equal(result.result.stocks[0].reportManagerCount, 0);
  assert.equal(result.result.stocks[0].currentProfileManagerCount, 1);
  assert.deepEqual(portfolios(result.parts)[0].reportManagers, []);
  current.managerAttribution = 'report';
  const verified = fixture([current]);
  assert.equal(verified.result.stocks[0].reportManagerCount, 0);
  assert.equal(verified.result.stocks[0].currentProfileManagerCount, 1);
});

test('observed quotes stay in native currency and only the exact quarter window is accepted', () => {
  const bars = { 'CN:600001': { market: 'CN', stockCode: '600001', currency: 'CNY', priceBasis: 'unadjusted',
    sourceUrl: 'https://example.org/prices', rows: [{ date: '2026-04-10', low: 8, high: 12 }] } };
  const fund = makeFund('000001', { barsByStock: bars });
  const first = fixture([fund]).result.stocks[0].priceRange;
  assert.equal(first.status, 'observed');
  assert.equal(first.low, 8);
  assert.equal(first.high, 12);
  assert.equal(first.isCost, false);
  assert.equal(first.completeness, 'partial');
  fund.rows[0].priceRange.rangeStart = '2026-01-01';
  assert.equal(fixture([fund]).result.stocks[0].priceRange.status, 'missing');
  fund.rows[0].priceRange.rangeStart = '2026-04-01';
  fund.rows[0].priceRange.currency = 'HKD';
  assert.equal(fixture([fund]).result.stocks[0].priceRange.status, 'missing');
});

test('bounded parts retain every contribution and every portfolio route', () => {
  const funds = Array.from({ length: 24 }, (_, i) => makeFund(String(i + 1).padStart(6, '0')));
  const { result, parts } = fixture(funds, { maxPartBytes: 3000 });
  assert.equal(contributions(parts, 'CN:600001').length, 24);
  assert.ok(result.stocks[0].details.length > 1);
  assert.equal(result.portfolioFiles.flatMap(file => file.ids).length, 24);
  for (const [file, value] of parts) {
    assert.match(file, /^\.\/analysis\/details-(?:portfolios-)?\d{4}\.json$/);
    assert.ok(Buffer.byteLength(JSON.stringify(value)) <= 3000);
  }
  const ids = new Set(portfolios(parts).map(fund => fund.id));
  assert.ok(contributions(parts, 'CN:600001').every(row => ids.has(row.portfolioId)));
});

test('mixed periods, traversal paths and future observations fail closed before publishing an index', () => {
  const { manifest, docs } = fixture([makeFund()]);
  const load = name => docs[name];
  const wrong = structuredClone(manifest); wrong.baselinePeriodEnd = '2025-12-31';
  assert.throws(() => analyzeQuarter(wrong, load, { now: NOW }), /INVALID_ANALYSIS_PERIOD/);
  docs[manifest.companyFiles[0].path].funds[0].selectedReport.periodEnd = '2026-09-30';
  assert.throws(() => analyzeQuarter(manifest, load, { now: NOW }), /ANALYSIS_PERIOD_MISMATCH/);
  const escape = structuredClone(manifest); escape.companyFiles[0].path = './data/companies/../../private.json';
  assert.throws(() => analyzeQuarter(escape, load, { now: NOW }), /INVALID_ANALYSIS_COMPANY_PATH/);
  const future = structuredClone(manifest); future.retrievedAt = '2026-10-27T12:00:00Z';
  assert.throws(() => analyzeQuarter(future, load, { now: NOW }), /ANALYSIS_FUTURE_OBSERVATION/);
});

test('CLI requires Actions and failed input retains an existing analysis index', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'public-analysis-'));
  try {
    const { manifest, docs } = fixture([makeFund()]);
    fs.mkdirSync(path.join(root, 'companies'));
    fs.writeFileSync(path.join(root, 'latest.json'), JSON.stringify(manifest));
    for (const [file, data] of Object.entries(docs)) fs.writeFileSync(path.join(root, file.slice('./data/'.length)), JSON.stringify(data));
    const script = path.join(__dirname, '../scripts/analyze-data.js');
    const blocked = spawnSync(process.execPath, [script, '--data', root], { env: { ...process.env, GITHUB_ACTIONS: 'false' }, encoding: 'utf8' });
    assert.notEqual(blocked.status, 0);
    assert.equal(fs.existsSync(path.join(root, 'analysis.json')), false);
    const output = generate(root);
    assert.equal(output.stocks.length, 1);
    const previous = fs.readFileSync(path.join(root, 'analysis.json'));
    const invalid = structuredClone(manifest); invalid.companyFiles[0].path = './data/companies/missing.json';
    fs.writeFileSync(path.join(root, 'latest.json'), JSON.stringify(invalid));
    assert.throws(() => generate(root));
    assert.deepEqual(fs.readFileSync(path.join(root, 'analysis.json')), previous);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('terminated products remain audited records without manufactured positions, while unproven exemptions fail', () => {
  const fund = makeFund('000001');
  fund.selectedReport = null; fund.previousReport = null; fund.rows = [];
  const quote = '本基金基金合同自动终止。';
  fund.notApplicableEvidence = { reason: 'TERMINATED_BEFORE_PERIOD', fundCode: fund.code,
    periodEnd: TARGET, date: '2026-05-06', dateBasis: 'lastOperationDate',
    lastOperationDate: '2026-05-06', terminationDate: null, liquidationStartDate: null,
    sourceUrl: 'https://example.org/termination.pdf', publishedAt: '2026-05-07',
    eventType: 'termination', terminationConfirmed: true, identityFundCodes: [fund.code],
    sourceTextHash: 'a'.repeat(64), parseMethod: 'pypdf', pageNumbersVerified: true,
    totalPages: 1, page: 1, quote, scope: 'quarter-end-applicability-only',
    evidence: [{ field: 'terminationConfirmed', page: 1, quote },
      { field: 'lastOperationDate', page: 1, quote: '本基金最后运作日为2026年5月6日' },
      { field: 'identityFundCodes', page: 1, quote: '基金份额代码：000001' }] };
  const { result, parts } = fixture([fund]);
  assert.equal(result.coverage.inputRecords, 1);
  assert.equal(result.coverage.excludedNotApplicable, 1);
  assert.equal(result.stocks.length, 0);
  assert.equal(portfolios(parts).length, 0);
  fund.notApplicableEvidence.terminationConfirmed = false;
  assert.throws(() => fixture([fund]), /ANALYSIS_PERIOD_MISMATCH/);
  fund.notApplicableEvidence.terminationConfirmed = true;
  Object.assign(fund.notApplicableEvidence, { date: TARGET, lastOperationDate: TARGET, publishedAt: '2026-07-01' });
  fund.notApplicableEvidence.evidence[1].quote = '本基金最后运作日为2026年6月30日';
  assert.throws(() => fixture([fund]), /ANALYSIS_PERIOD_MISMATCH/);
});
