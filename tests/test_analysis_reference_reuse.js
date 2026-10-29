'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const referenceApi = require('../lib/price-range-reference');
const { conflictFor } = require('../lib/price-consistency');
const f = require('./price-reference-fixture');

// Keep the former two-decision evaluator as an independent regression oracle.
// The complete analyzer and emitted evidence parts run around both versions.
const legacyDecision = `        if (entry?.conflict || candidate && Object.hasOwn(candidate, 'globalPriceConflict')
          && !effectiveReference(candidate, priceIdentity, currentTime)) stock._completePriceConflict = true;
        const effective = effectivePriceRange(candidate, priceIdentity, currentTime) || row.priceRange;`;

function instrumentedAnalyzer(legacy = false) {
  const filename = require.resolve('../scripts/analyze-data');
  let source = fs.readFileSync(filename, 'utf8');
  if (legacy) {
    const decision = /        \/\/ Both decisions use the same immutable candidate and generation clock\.\n[\s\S]*?        const effective = reference[^\n]*;/g;
    assert.equal([...source.matchAll(decision)].length, 1, 'the legacy comparison must replace exactly one decision');
    source = source.replace(decision, legacyDecision).replace(
      "const { effectiveReference } = require('../lib/price-range-reference');",
      "const { effectivePriceRange, effectiveReference } = require('../lib/price-range-reference');");
  }
  const calls = [], loaded = new Module(filename, module);
  loaded.filename = filename; loaded.paths = Module._nodeModulePaths(require('node:path').dirname(filename));
  const ordinaryRequire = loaded.require.bind(loaded);
  const measuredReference = (range, identity, now) => {
    calls.push({ range, identity, now });
    return referenceApi.effectiveReference(range, identity, now);
  };
  loaded.require = name => name === '../lib/price-range-reference' ? {
    ...referenceApi, effectiveReference: measuredReference,
    effectivePriceRange: (range, identity, now) => measuredReference(range, identity, now)
      || (range && !Object.hasOwn(range, 'globalPriceConflict') ? range : null),
  } : ordinaryRequire(name);
  loaded._compile(source, filename);
  return { analyze: loaded.exports.analyzeQuarter, calls };
}

function sample(kind) {
  const data = f.fixture(), document = f.snapshot('000001', 'yahoo');
  const fund = document.funds[0];
  Object.assign(fund, { companyId: 'co1', shareClassCodes: ['000001', '000002'],
    sourcePortfolioCode: '000001', portfolioSourceUrl: fund.selectedReport.sourceUrl });
  let price = { ...data.prices[1], rangeReference: data.reference, globalPriceConflict: data.witness };
  if (kind === 'no-witness') delete price.globalPriceConflict;
  if (kind === 'no-reference') delete price.rangeReference;
  if (kind === 'tampered-reference') price.rangeReference.factSetDigest = '0'.repeat(64);
  if (kind === 'tampered-reference-no-witness') {
    price.rangeReference.factSetDigest = '0'.repeat(64); delete price.globalPriceConflict;
  }
  if (kind === 'invalid-witness') price.globalPriceConflict = {};
  if (kind === 'null-witness') price.globalPriceConflict = null;
  if (kind === 'material-conflict') {
    const changed = f.price('eastmoney', 74, 100.1);
    price.globalPriceConflict = conflictFor([data.facts[0], referenceApi.factFor(changed)], f.identity, f.now);
  }
  if (kind === 'partial-outside') price = { ...f.price('eastmoney', 70, 100.1, true), rangeReference: data.reference };
  if (kind === 'missing-with-proof') price.status = 'missing';
  if (kind === 'missing') price = {};
  if (kind === 'absent') price = undefined;
  fund.rows[0].priceRange = price;
  const filename = './data/companies/co1.json';
  return { document, filename, manifest: { schemaVersion: 1, sourceMode: 'actions', ...f.phase,
    asOf: document.asOf, retrievedAt: document.retrievedAt, coverage: { fundCount: 1, catalogFundCount: 2 },
    companyFiles: [{ path: filename }] } };
}

function execute(analyzer, input) {
  const parts = new Map();
  const index = analyzer.analyze(input.manifest, filename => {
    assert.equal(filename, input.filename); return input.document;
  }, { now: f.now, writePart: (filename, value) => parts.set(filename, value) });
  return { index, parts };
}

for (const kind of ['resolved', 'no-witness', 'no-reference', 'tampered-reference',
  'tampered-reference-no-witness', 'invalid-witness', 'null-witness', 'material-conflict',
  'partial-outside', 'missing-with-proof', 'missing', 'absent']) {
  test(`${kind}: one price resolution preserves the full analysis and evidence`, () => {
    const input = sample(kind), original = structuredClone(input);
    const prior = instrumentedAnalyzer(true), current = instrumentedAnalyzer();
    const expected = execute(prior, input), actual = execute(current, input);
    assert.deepEqual(actual, expected);
    assert.deepEqual(input, original, 'source quotes, witnesses and original observation clocks are immutable');
    assert.equal(current.calls.length, 1, 'each selected holding performs one reference validation');
    assert.ok(prior.calls.length >= current.calls.length);
    assert.equal(current.calls[0].now.toISOString(), f.now.replace('Z', '.000Z'));
    assert.equal(actual.index.coverage.contributionCount, 1);
    assert.equal(actual.index.stocks[0].priceRange.isCost, false);
    if (['resolved', 'no-reference', 'tampered-reference', 'invalid-witness', 'null-witness', 'missing-with-proof'].includes(kind)) {
      assert.equal(prior.calls.length, 2, 'former conflict and price decisions repeated the validation');
    }
    if (['resolved', 'no-witness', 'no-reference', 'tampered-reference', 'missing-with-proof'].includes(kind)) {
      assert.equal(actual.index.coverage.completePriceStocks, 1);
      assert.equal(actual.index.stocks[0].priceRange.fetchedAt, f.fetchedAt);
    }
    if (['invalid-witness', 'null-witness', 'material-conflict', 'partial-outside', 'missing', 'absent'].includes(kind)) {
      assert.equal(actual.index.coverage.completePriceStocks, 0);
    }
  });
}

test('a later mutation is revalidated instead of reusing a prior object result', () => {
  const analyzer = instrumentedAnalyzer(), input = sample('resolved');
  assert.equal(execute(analyzer, input).index.coverage.completePriceStocks, 1);
  input.document.funds[0].rows[0].priceRange.globalPriceConflict = null;
  assert.equal(execute(analyzer, input).index.coverage.completePriceStocks, 0);
  assert.equal(analyzer.calls.length, 2);
});
