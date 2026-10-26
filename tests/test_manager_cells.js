'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), path = require('node:path');
const { spawnSync } = require('node:child_process');
const { normalizeGeometryEvidence } = require('../lib/manager-cells');
const { managerCoverageFor } = require('../lib/report-managers');
const { normalizeReport } = require('../lib/fund-analysis');
const clone = value => JSON.parse(JSON.stringify(value));
const script = [
  "import json,sys",
  "sys.path[:0]=['scripts','tests']",
  "from test_manager_cells import parse,page,row,HEADER,END,Cell,COLUMNS",
  "samples={}",
  "def put(name,grids,pages):",
  "    r=parse(grids,pages);samples[name]={'managers':list(r),'coverage':r.coverage}",
  "put('complete',[page(1,row(300,700),header=True,end_y=250)],[HEADER+'张明 本基金基金经理 2020年1月2日 -\\n'+END])",
  "prefix='总经理助理兼权益投资部总经理、本基';suffix='金基金经理、投资经理'",
  "put('continued',[page(1,row(60,700,name='徐超',role=prefix),header=True),page(2,row(600,780,name='',role=suffix,start='',end='',experience=''),end_y=550)],[HEADER+'徐超 '+prefix+' 2020年1月2日 -\\n',suffix+'\\n'+END])",
  "put('roleDescription',[page(1,row(60,700,name='徐超',role=prefix),header=True),page(2,[Cell(*COLUMNS['role'],300,780,suffix),Cell(*COLUMNS['description'],300,780,'这是已知经理的履历续写。')],end_y=250)],[HEADER+'徐超 '+prefix+' 2020年1月2日 -\\n',suffix+'\\n'+END])",
  "put('classified',[page(1,row(550,700)+row(400,550,name='李明',role='本基金基金经理助理')+row(250,400,name='王明',end='2026年3月31日'),header=True,end_y=200)],[HEADER+'张明 本基金基金经理 2020年1月2日 -\\n李明 本基金基金经理助理 2020年1月2日 -\\n王明 本基金基金经理 2020年1月2日 2026年3月31日\\n'+END])",
  "put('partial',[page(1,row(500,700)+row(300,500,name='李明',start='2026年2月31日'),header=True,end_y=250)],[HEADER+'张明 本基金基金经理 2020年1月2日 -\\n'+END])",
  "put('description',[page(1,row(60,700),header=True),page(2,[Cell(*COLUMNS['description'],600,780,'这是已知经理的履历续写。')]+row(300,600,name='李明'),end_y=250)],[HEADER+'张明 本基金基金经理 2020年1月2日 -\\n','续写履历\\n李明 本基金基金经理 2020年1月2日 -\\n'+END])",
  "put('descriptionChain',[page(1,row(60,700),header=True),page(2,[Cell(*COLUMNS['description'],60,780,'已知经理履历。')]),page(3,[Cell(*COLUMNS['description'],60,780,'已知经理履历。')]),page(4,[Cell(*COLUMNS['description'],600,780,'已知经理履历。')],end_y=550)],[HEADER+'张明 本基金基金经理 2020年1月2日 -\\n','续写履历','续写履历','续写履历\\n'+END])",
  "print(json.dumps(samples,ensure_ascii=False))"
].join('\n');
const generated = spawnSync('python3', ['-c', script], { cwd: path.resolve(__dirname, '..'), encoding: 'utf8' });
assert.equal(generated.status, 0, generated.stderr);
const fixtures = JSON.parse(generated.stdout);

test('Python original-cell proof survives JS normalization including exact adjacent-page continuation', () => {
  for (const key of ['complete', 'continued', 'classified', 'description', 'descriptionChain', 'roleDescription']) {
    const sample = fixtures[key];
    assert.equal(sample.coverage.complete, true);
    const proof = managerCoverageFor(sample.coverage, sample.managers, '2026-06-30');
    assert.equal(proof.complete, true, key);
    assert.equal(proof.parserVersion, 2);
    assert.equal(proof.extractionMethod, 'pdf-clipping-manager-table');
    assert.ok(proof.geometryEvidence);
    assert.equal(JSON.stringify(proof).includes('曾任其他基金'), false);
    assert.equal(managerCoverageFor(proof, sample.managers, '2026-06-30').complete, true);
  }
  assert.equal(fixtures.continued.coverage.geometryEvidence.continuationCount, 1);
  assert.equal(fixtures.classified.coverage.ignoredKnownRoleRows, 1);
  assert.equal(fixtures.classified.coverage.outsidePeriodRows, 1);
  assert.equal(fixtures.description.coverage.geometryEvidence.physicalRowCount, 3);
  assert.equal(fixtures.description.coverage.geometryEvidence.logicalRowCount, 2);
  assert.equal(fixtures.descriptionChain.coverage.geometryEvidence.continuationCount, 3);
  assert.equal(fixtures.roleDescription.coverage.geometryEvidence.tables[0].rows[0].fragments[1].continuationType, 'role-description');
  assert.equal(fixtures.roleDescription.managers[0].role, '总经理助理兼权益投资部总经理、本基金基金经理、投资经理');
});

test('role-description continuation requires both empty identity regions and consecutive page-edge cells', () => {
  const mutations = [
    fragment => { delete fragment.emptyIdentityRegions; },
    fragment => { fragment.emptyIdentityRegions.pop(); },
    fragment => { fragment.emptyIdentityRegions[0].spanCount = 1; },
    fragment => { fragment.emptyIdentityRegions[1].spanCount = 1; },
    fragment => { fragment.emptyIdentityRegions[0].textHash = 'b'.repeat(64); },
    fragment => { fragment.emptyIdentityRegions[1].textHash = 'b'.repeat(64); },
    fragment => { fragment.emptyIdentityRegions[0].rect[0] += 1; },
    fragment => { fragment.emptyIdentityRegions[1].rect[0] += 1; },
    fragment => { fragment.page = 1; },
    fragment => { fragment.page = 3; },
    fragment => { fragment.cells.role[0] += 3; },
    fragment => { fragment.cells.role[3] = 650; fragment.cells.description[3] = 650;
      fragment.emptyIdentityRegions.forEach(region => { region.rect[3] = 650; }); },
    fragment => { fragment.cellText.role = '另一位经理'; },
    fragment => { delete fragment.continuationType; },
  ];
  for (const mutate of mutations) {
    const sample = clone(fixtures.roleDescription);
    mutate(sample.coverage.geometryEvidence.tables[0].rows[0].fragments[1]);
    assert.equal(managerCoverageFor(sample.coverage, sample.managers).complete, false);
  }
  const sample = clone(fixtures.roleDescription), fragments = sample.coverage.geometryEvidence.tables[0].rows[0].fragments;
  fragments[0] = { ...clone(fragments[1]), page: 1 };
  assert.equal(managerCoverageFor(sample.coverage, sample.managers).complete, false);
});

test('description-only continuation requires empty identity proof, adjacent pages and original full row', () => {
  for (const mutate of [
    fragment => { fragment.emptyIdentityRegion.spanCount = 1; },
    fragment => { fragment.emptyIdentityRegion.textHash = 'b'.repeat(64); },
    fragment => { fragment.emptyIdentityRegion.rect[0] += 1; },
    fragment => { fragment.page = 3; },
    fragment => { fragment.cells.description[3] = 650; fragment.emptyIdentityRegion.rect[3] = 650; },
    fragment => { fragment.cellText.name = '李明'; },
    fragment => { delete fragment.continuationType; },
  ]) {
    const sample = clone(fixtures.description);
    mutate(sample.coverage.geometryEvidence.tables[0].rows[0].fragments[1]);
    assert.equal(managerCoverageFor(sample.coverage, sample.managers).complete, false);
  }
  const sample = clone(fixtures.description);
  const fragments = sample.coverage.geometryEvidence.tables[0].rows[0].fragments;
  fragments[0] = { ...clone(fragments[1]), page: 1 };
  assert.equal(managerCoverageFor(sample.coverage, sample.managers).complete, false);
  const chain = clone(fixtures.descriptionChain);
  chain.coverage.geometryEvidence.tables[0].rows[0].fragments[1].cells.description[1] = 150;
  chain.coverage.geometryEvidence.tables[0].rows[0].fragments[1].emptyIdentityRegion.rect[1] = 150;
  assert.equal(managerCoverageFor(chain.coverage, chain.managers).complete, false);
});

test('partial geometry remains partial while valid manager names remain available', () => {
  const sample = fixtures.partial;
  const normalized = normalizeReport({ sourceUrl: sample.coverage.sourceUrl, periodEnd: '2026-06-30',
    publishedAt: '2026-07-20', kind: 'quarterly', holdings: [], managers: sample.managers, managerTableCoverage: sample.coverage });
  assert.equal(normalized.managers[0].name, '张明');
  assert.equal(normalized.managerTableCoverage.complete, false);
  assert.equal(normalized.managerTableCoverage.status, 'partial');
});

test('altered hashes counts header cells roles source keys and continuation pages cannot prove completeness', () => {
  const changes = [
    proof => { proof.geometryEvidence.pdfSha256 = 'bad'; },
    proof => { proof.geometryEvidence.sourceTextHash = 'b'.repeat(64); },
    proof => { proof.geometryEvidence.physicalRowCount++; },
    proof => { proof.geometryEvidence.tablePages = [2]; },
    proof => { proof.geometryEvidence.tables[0].header.cellText.name = '说明'; },
    proof => { proof.geometryEvidence.tables[0].header.cells.start[0] += 5; },
    proof => { proof.geometryEvidence.tables[0].rows[0].rowKey = '["其他人",null,null,"基金经理"]'; },
    proof => { proof.geometryEvidence.tables[0].rows[0].fragments[0].cellTextHashes.role = 'b'.repeat(64); },
    proof => { proof.geometryEvidence.tables[0].rows[0].fragments[0].cells.end[0] += 5; },
    proof => { proof.geometryEvidence.tables[0].rows[0].fragments[0].pageBox[3] = 500; },
    proof => { proof.geometryEvidence.tables[0].rows[0].classification = 'assistant'; },
  ];
  for (const change of changes) {
    const sample = clone(fixtures.complete); change(sample.coverage);
    assert.equal(managerCoverageFor(sample.coverage, sample.managers).complete, false);
  }
  for (const mutate of [
    fragment => { fragment.page = 3; }, fragment => { fragment.cells.name[3] = 650; },
    fragment => { fragment.cellText.name = '张明'; },
  ]) {
    const sample = clone(fixtures.continued); mutate(sample.coverage.geometryEvidence.tables[0].rows[0].fragments[1]);
    assert.equal(managerCoverageFor(sample.coverage, sample.managers).complete, false);
  }
});

test('old parser proof cannot count complete and unknown geometry payload is never copied', () => {
  const sample = clone(fixtures.complete);
  delete sample.coverage.parserVersion;
  assert.equal(managerCoverageFor(sample.coverage, sample.managers).complete, false);
  sample.coverage.parserVersion = 1;
  assert.equal(managerCoverageFor(sample.coverage, sample.managers).complete, false);
  sample.coverage.parserVersion = 2;
  sample.coverage.geometryEvidence.fullDescription = 'unbounded text';
  sample.coverage.geometryEvidence.tables[0].rows[0].fullDescription = 'unbounded text';
  const clean = normalizeGeometryEvidence(sample.coverage.geometryEvidence, sample.coverage);
  assert.ok(clean);
  assert.equal(JSON.stringify(clean).includes('unbounded text'), false);
});

test('an assistant label cannot hide an additional manager role', () => {
  const sample = clone(fixtures.classified), row = sample.coverage.geometryEvidence.tables[0].rows[1];
  const role = '本基金基金经理兼基金经理助理', fragment = row.fragments[0];
  fragment.cellText.role = role;
  fragment.cellTextHashes.role = require('node:crypto').createHash('sha256').update(role).digest('hex');
  const key = JSON.parse(row.rowKey); key[3] = role; row.rowKey = JSON.stringify(key);
  assert.equal(managerCoverageFor(sample.coverage, sample.managers).complete, false);
});
