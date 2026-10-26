'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { createAnalysisSpool } = require('../scripts/analysis-spool');
const { analyzeQuarter, generate } = require('../scripts/analyze-data');
const { buildSnapshot } = require('../scripts/build-data');
const ui = require('../web/analysis');
const manager = require('./fixtures/manager-table-coverage.json').complete;
const NOW = '2026-10-26T12:00:00Z', RECEIPT = '2026-10-26T11:00:00Z';
const TARGET = '2026-06-30', BASELINE = '2026-03-31';
function fixture() {
  const docs = {}, current = [{ stockCode: '600001', stockName: '合成股票', market: 'CN', currency: 'CNY', shares: 100, weightPct: 5, marketValueYuan: 500 }];
  for (let n = 1; n <= 25; n++) {
    const code = String(n).padStart(6, '0'), identity = n === 25 ? '000001' : code;
    const report = { fundCode: code, kind: 'quarterly', coverage: 'top10', navYuan: 10000, holdings: current,
      sourceUrl: `https://example.org/${code}-current.pdf`, periodEnd: TARGET, publishedAt: '2026-07-20',
      managers: manager.managers, managerTableCoverage: manager.coverage, managerEvidenceRetrievedAt: RECEIPT,
      narrative: { text: '报告期内调整行业配置并关注企业盈利。', sourceUrl: `https://example.org/${code}-current.pdf`,
        page: 7, periodStart: '2026-04-01', periodEnd: TARGET } };
    const input = { code, name: '合成基金' + code, company: '合成公司', companyId: 'co1', managers: [],
      shareClassCodes: ['000001', '000025'].includes(code) ? ['000001', '000025'] : [code], sourcePortfolioCode: identity,
      portfolioSourceUrl: 'https://example.org/identity.pdf', reports: [report,
        { ...report, periodEnd: BASELINE, sourceUrl: `https://example.org/${code}-prior.pdf`, publishedAt: '2026-04-20',
          holdings: [...current.map(row => ({ ...row, shares: 50 })), { ...current[0], stockCode: '600002' }],
          managers: [], managerTableCoverage: null, narrative: null }] };
    const snapshot = buildSnapshot({ asOf: '2026-10-26', retrievedAt: RECEIPT, funds: [input], barsByStock: {}, errors: [] },
      { now: NOW, config: { targetPeriodEnd: TARGET, baselinePeriodEnd: BASELINE, funds: [{ code }] } });
    docs[`./data/companies/co-${code}.json`] = snapshot;
  }
  const manifest = { schemaVersion: 1, sourceMode: 'actions', targetPeriodEnd: TARGET, baselinePeriodEnd: BASELINE,
    asOf: '2026-10-26', retrievedAt: RECEIPT, coverage: { fundCount: 25 },
    companyFiles: Object.keys(docs).reverse().map(file => ({ path: file })) };
  return { docs, manifest };
}
function readBundle(parts) {
  const portfolios = [...parts.values()].filter(p => p.kind === 'portfolio-evidence').flatMap(p => p.portfolios).sort((a,b) => a.id.localeCompare(b.id));
  const stocks = [...parts.values()].filter(p => p.kind === 'stock-contributions').flatMap(p => p.stocks);
  const contributions = stocks.flatMap(s => s.contributions.map(c => ({ key: s.key, ...c }))).sort((a,b) => a.key.localeCompare(b.key) || a.portfolioId.localeCompare(b.portfolioId));
  return { portfolios, contributions };
}

test('bounded spool preserves all evidence, deduplication, prior-only rows and stock-local detail routes', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analysis-spool-'));
  try {
    const { docs, manifest } = fixture(), memoryParts = new Map(), diskParts = new Map();
    const memory = analyzeQuarter(manifest, file => docs[file], { now: NOW, maxPartBytes: 12000, writePart: (p,v) => memoryParts.set(p,v) });
    const spool = createAnalysisSpool(path.join(root, 'spool'), { maxBufferBytes: 600 });
    let reads = 0;
    const disk = analyzeQuarter(manifest, file => {
      reads++;
      assert.ok(spool.stats().bufferedBytes <= 600);
      return docs[file];
    }, { now: NOW, spool, maxPartBytes: 12000, writePart: (p,v) => diskParts.set(p,v) });
    assert.equal(reads, 2 * manifest.companyFiles.length);
    assert.ok(spool.stats().totalBytes > 600);
    assert.ok(spool.stats().peakBufferedBytes <= 600);
    assert.equal(spool.stats().bufferedBytes, 0);
    assert.equal(spool.stats().portfolioCount, 24);
    assert.equal(spool.stats().contributionCount, 48);
    assert.deepEqual(disk.coverage, memory.coverage);
    assert.deepEqual(disk.stocks, memory.stocks);
    assert.deepEqual(disk.themes, memory.themes);
    assert.deepEqual(readBundle(diskParts), readBundle(memoryParts));
    assert.equal(readBundle(diskParts).portfolios[0].managerTableCoverage.complete, true);
    assert.equal(disk.coverage.duplicatesRemoved, 1);
    assert.equal(disk.stocks.find(s => s.key === 'CN:600002').changes.leftDisclosure, 24);
    ui.validate(disk, '2026Q2');
    for (const part of diskParts.values()) {
      assert.ok(Buffer.byteLength(JSON.stringify(part)) <= 12000);
      ui.validatePart(part, part.kind, disk);
    }
    for (const stock of disk.stocks) {
      const rows = stock.details.flatMap(file => diskParts.get(file).stocks.filter(s => s.key === stock.key).flatMap(s => s.contributions));
      assert.equal(rows.length, 24);
      assert.equal(new Set(rows.map(r => r.portfolioId)).size, 24);
    }
    spool.dispose(); assert.equal(fs.existsSync(path.join(root, 'spool')), false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('spool handles UTF-8 across read blocks and refuses unbounded items, total bytes or further writes', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analysis-spool-'));
  try {
    const spool = createAnalysisSpool(path.join(root, 'spool'), { maxBufferBytes: 500, maxItemBytes: 100000, maxTotalBytes: 200000 });
    const item = { id: 'portfolio:000001', text: '中文页码证据'.repeat(7000) };
    assert.throws(() => spool.addPortfolio(item), /ANALYSIS_ITEM_TOO_LARGE/);
    item.text = '中文页码证据'.repeat(5000);
    spool.addPortfolio(item);
    assert.deepEqual([...spool.portfolios()], [item]);
    assert.throws(() => spool.addPortfolio({ id: 'late' }), /ANALYSIS_SPOOL_FINISHED/);
    spool.dispose();
    const limited = createAnalysisSpool(path.join(root, 'limited'), { maxBufferBytes: 100, maxItemBytes: 200, maxTotalBytes: 200 });
    limited.addPortfolio({ text: 'a'.repeat(130) });
    assert.throws(() => limited.addPortfolio({ text: 'b'.repeat(130) }), /ANALYSIS_SPOOL_TOO_LARGE/);
    limited.dispose();
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('production generate uses spool, publishes only public partitions and preserves existing index after failure', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analysis-spool-'));
  try {
    const { docs, manifest } = fixture();
    fs.mkdirSync(path.join(root, 'companies'));
    fs.writeFileSync(path.join(root, 'latest.json'), JSON.stringify(manifest));
    for (const [file, value] of Object.entries(docs)) fs.writeFileSync(path.join(root, file.slice('./data/'.length)), JSON.stringify(value));
    const result = generate(root);
    assert.equal(result.coverage.analyzedRecords, 24);
    assert.ok(fs.readdirSync(root).every(file => !file.startsWith('.analysis-stage')));
    assert.ok(fs.readdirSync(path.join(root, 'analysis')).every(file => /^details-(?:portfolios-)?\d{4}\.json$/.test(file)));
    const originalIndex = fs.readFileSync(path.join(root, 'analysis.json'));
    const originalFiles = new Map(fs.readdirSync(path.join(root, 'analysis')).map(name => [name, fs.readFileSync(path.join(root, 'analysis', name))]));
    const writeFile = fs.writeFileSync;
    try {
      fs.writeFileSync = (file, ...args) => {
        if (String(file).includes('.analysis-stage-') && path.basename(String(file)) === 'details-0000.json') throw new Error('SIMULATED_PART_WRITE_FAILURE');
        return writeFile(file, ...args);
      };
      assert.throws(() => generate(root), /SIMULATED_PART_WRITE_FAILURE/);
    } finally { fs.writeFileSync = writeFile; }
    assert.deepEqual(fs.readFileSync(path.join(root, 'analysis.json')), originalIndex);
    for (const [name, raw] of originalFiles) assert.deepEqual(fs.readFileSync(path.join(root, 'analysis', name)), raw);
    assert.ok(fs.readdirSync(root).every(file => !file.startsWith('.analysis-stage')));
    const fund = docs[manifest.companyFiles[0].path].funds[0];
    fund.selectedReport.periodEnd = '2026-09-30';
    fs.writeFileSync(path.join(root, manifest.companyFiles[0].path.slice('./data/'.length)), JSON.stringify(docs[manifest.companyFiles[0].path]));
    assert.throws(() => generate(root), /ANALYSIS_PERIOD_MISMATCH/);
    assert.deepEqual(fs.readFileSync(path.join(root, 'analysis.json')), originalIndex);
    assert.ok(fs.readdirSync(root).every(file => !file.startsWith('.analysis-stage')));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
