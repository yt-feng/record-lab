'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const path = require('node:path'), { spawnSync } = require('node:child_process');
const helper = require('../lib/price-consistency');
const ROOT = path.join(__dirname, '..'), NOW = '2026-10-27T02:00:00Z';
function python(script, input) {
  const result = spawnSync('python3', ['-c', `import sys,json;sys.path[:0]=['scripts','tests'];${script}`], {
    cwd: ROOT, input: input === undefined ? undefined : JSON.stringify(input), encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr); return JSON.parse(result.stdout);
}
const fixtures = python("from test_price_consistency import fixture,NOW;import price_consistency as h;print(json.dumps([{'identity':i,'facts':f,'witness':h.conflict_for(f,i,NOW)} for i,f in [fixture(False,m) for m in ['CN','HK','US']]+[fixture(True)]]))");

test('complete and partial witnesses round-trip exactly through Python and preserve original observations', () => {
  for (const fixture of fixtures) {
    const before = structuredClone(fixture.facts);
    for (const now of [NOW, new Date(NOW), Date.parse(NOW)]) {
      const value = helper.conflictFor(fixture.facts, fixture.identity, now);
      assert.deepEqual(value, fixture.witness);
      assert.deepEqual(helper.normalizeConflict(value, fixture.identity, now), value);
      assert.equal(helper.isConflict(value, fixture.identity, now), true);
      assert.deepEqual(helper.conflictFor([...fixture.facts].reverse(), fixture.identity, now), value);
    }
    assert.deepEqual(fixture.facts, before);
  }
});

test('IEEE754 hexadecimal numbers and canonical URL identities match the ledger without rounding', () => {
  const values = [0, 1, .9, .1, 1.00000000001, 1e-30, 1e30, Number.MIN_VALUE, Number.MAX_VALUE];
  const expected = python("import price_conflicts as h;v=json.loads(sys.stdin.read());print(json.dumps([{'hex':float(x).hex(),'digest':h.digest(x)} for x in v]))", values);
  values.forEach((value, index) => { assert.equal(helper.floatHex(value), expected[index].hex); assert.equal(helper.digest(value), expected[index].digest); });
  // JSON serializes negative zero as zero; compare its representation directly.
  assert.equal(helper.floatHex(-0), '-0x0.0p+0');
  assert.equal(helper.digest(-0), python("import price_conflicts as h;print(json.dumps(h.digest(-0.0)))"));
  const urls = ['https://example.org/x?b=2&a=1', 'https://example.org?x=%21%27%28%29%2A&x=hello+world',
    'https://query2.finance.yahoo.com/v8/finance/chart/META?period2=99&period1=10&interval=1d',
    'https://example.org/路径?a=%E4%B8%AD&b=%F0%9F%98%80'];
  const canonical = python("import price_conflicts as h;print(json.dumps([h.source_identity(x) for x in json.loads(sys.stdin.read())],ensure_ascii=False))", urls);
  assert.deepEqual(urls.map(helper.sourceIdentity), canonical);
});

test('partial proof tampering and wrong source identity fail both language validators', () => {
  const fixture = fixtures.at(-1), cases = [];
  for (const change of [{ observedCount: 1 }, { observedStart: '2026-01-01' }, { observedEnd: '2026-06-31' },
    { observedDateSetHash: '0'.repeat(64) }, { missingSessionDates: [] }, { missingSessionCount: 0 },
    { coverageVersion: 1 }, { calendarId: 'unknown' }, { rejectedRowCount: 1 }, { conflictingDateCount: 1 },
    { currency: 'USD' }, { priceBasis: 'adjusted' }]) {
    const facts = structuredClone(fixture.facts); Object.assign(facts[1].price, change); cases.push(facts);
  }
  for (const fetchedAt of ['2026-06-29T00:00:00Z', '2026-10-27T02:05:00.000001Z', '2026-10-26T11:00:00+00:60', '2026-10-26T24:00:00Z']) {
    const facts = structuredClone(fixture.facts); facts[0].fetchedAt = fetchedAt; cases.push(facts);
  }
  const expected = python("from test_price_consistency import NOW;import price_consistency as h;v=json.loads(sys.stdin.read());print(json.dumps([h.conflict_for(f,v['identity'],NOW) for f in v['cases']]))", { identity: fixture.identity, cases });
  assert.ok(expected.every(value => value === null));
  for (const facts of cases) assert.equal(helper.conflictFor(facts, fixture.identity, NOW), null);
});

test('normalization rejects changed digests, source facts, ordering and unknown fields', () => {
  const { witness, identity, facts } = fixtures[0];
  const mutations = [value => { value.witnessDigest = '0'.repeat(64); }, value => { value.resolution = 'resolved'; },
    value => { value.facts[0].factId = '0'.repeat(64); }, value => value.facts.reverse(),
    value => { value.types = ['partial-extreme']; }, value => { value.extra = true; },
    value => { value.facts[0].price.secret = 'not allowed'; }];
  for (const mutate of mutations) {
    const value = structuredClone(witness); mutate(value); assert.equal(helper.normalizeConflict(value, identity, NOW), null);
  }
  const raw = structuredClone(facts); raw[0].price.globalPriceConflict = { untrusted: true };
  assert.deepEqual(helper.conflictFor(raw, identity, NOW), witness);
});

test('equal complete ranges and narrower partial observations do not manufacture a conflict', () => {
  for (const fixture of [fixtures[0], fixtures.at(-1)]) {
    const facts = structuredClone(fixture.facts); facts[1].price.low = 1;
    assert.equal(helper.conflictFor(facts, fixture.identity, NOW), null);
    assert.equal(helper.conflictFor(facts.slice(0, 1), fixture.identity, NOW), null);
  }
});

test('individual fact validation rejects malformed partial extrema before they enter an index', () => {
  const { identity, facts, witness } = fixtures.at(-1);
  assert.deepEqual(helper.normalizeFact(facts[1], identity, Date.parse(NOW)), witness.facts.find(value => value.price.low === .9));
  const invalid = structuredClone(facts[1]); invalid.price.observedDateSetHash = '0'.repeat(64);
  assert.equal(helper.normalizeFact(invalid, identity, NOW), null);
  for (const sourceUrl of ['https://例子.com/price', 'https://%65xample.org/price', 'https://example.org/#', 'https://example.org/a\u0001b']) {
    const fact = { ...facts[1], sourceUrl, sourceUrls: [sourceUrl] };
    assert.equal(helper.normalizeFact(fact, identity, NOW), null);
    const expected = python("from test_price_consistency import NOW;import price_consistency as h;v=json.loads(sys.stdin.read());print(json.dumps(h.conflict_for(v['facts'],v['identity'],NOW)))", { identity, facts: [facts[0], fact] });
    assert.equal(expected, null);
  }
});
