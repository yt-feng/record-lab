'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { strategyFor } = require('../scripts/analyze-data');
const app = require('../web/app');
const analysisUi = require('../web/analysis');
const samples = require('./fixtures/strategy-sections.json');
const phase = { start: '2026-04-01', target: '2026-06-30', baseline: '2026-03-31' };
const now = '2026-10-26T12:00:00Z';
const report = name => ({ ...structuredClone(samples[name]), periodEnd: phase.target,
  kind: 'semiannual', sourceUrl: 'https://example.org/holdings-interim.pdf', publishedAt: '2026-08-28' });

test('quarter strategy proof remains independent of interim holdings and selected evidence does not claim full interpretation', () => {
  const selectedReport = report('complete');
  const strategy = strategyFor({ selectedReport, retrievedAt: now }, phase);
  assert.equal(strategy.sectionStatus, 'complete');
  assert.equal(strategy.sectionCoverage.sourceUrl, 'https://example.org/strategy.pdf');
  assert.equal(strategy.analysis.categorySentenceCounts['portfolio-operations'], 1);
  assert.equal(strategy.analysis.categorySentenceCounts.outlook, 1);
  assert.equal(strategy.excerpts.length, 3);
  assert.equal(strategy.semanticAnalysisComplete, false);
  assert.equal(strategy.explainsIndividualStockTrades, false);
  assert.equal(strategy.excerpts[1].securityMentions[0].stockCode, '601318');
  assert.equal(strategy.excerpts[1].securityMentions[0].provesTradeReason, false);
  assert.equal(app.strategyEvidence(selectedReport, now.slice(0, 10)).complete, true);
  assert.equal(analysisUi.strategyEvidence(strategy, phase.target, now.slice(0, 10)).complete, true);
  assert.equal(app.strategyEvidence(selectedReport, '2026-07-01').complete, false);
});

test('a short legacy quote remains visible but cannot stand for complete strategy extraction', () => {
  const legacy = { periodEnd: phase.target, strategyExcerpts: [{ text: '本基金调整组合配置。',
    periodStart: phase.start, periodEnd: phase.target, page: 7, sourceUrl: 'https://example.org/report.pdf' }] };
  const strategy = strategyFor({ selectedReport: legacy, retrievedAt: now }, phase);
  assert.equal(strategy.status, 'partial-source');
  assert.equal(strategy.sectionStatus, 'partial');
  assert.equal(strategy.excerpts.length, 1);
  assert.equal(strategy.analysis, null);
  assert.equal(Boolean(app.strategyEvidence(legacy).complete), false);
});

test('missing boundaries or pages never become complete from a copied flag', () => {
  for (const name of ['missing-end', 'noncontiguous', 'unknown-page', 'page-cap']) {
    const selectedReport = report(name);
    selectedReport.strategySectionCoverage.complete = true;
    const strategy = strategyFor({ selectedReport, retrievedAt: now }, phase);
    assert.notEqual(strategy.sectionStatus, 'complete', name);
    assert.equal(strategy.semanticAnalysisComplete, false);
    assert.equal(app.strategyEvidence(selectedReport).complete, false);
  }
});

test('wrong quarter and future publication cannot be counted as the current quarter strategy', () => {
  for (const mutation of [proof => { proof.publishedAt = '2027-01-01'; },
    proof => { proof.periodStart = '2026-01-01'; proof.kind = 'semiannual'; }]) {
    const selectedReport = report('complete'); mutation(selectedReport.strategySectionCoverage);
    const strategy = strategyFor({ selectedReport, retrievedAt: now }, phase);
    assert.notEqual(strategy.sectionStatus, 'complete');
    assert.equal(strategy.excerpts.length, 0);
  }
  const strategy = strategyFor({ selectedReport: report('complete'), retrievedAt: now }, phase);
  assert.equal(Boolean(analysisUi.strategyEvidence(strategy, '2026-09-30').complete), false);
});

test('a separately observed strategy PDF can enrich an older holding record without inventing its observation time', () => {
  const selectedReport = report('complete');
  selectedReport.strategyEvidenceRetrievedAt = '2026-07-21T12:00:00Z';
  const oldFund = { selectedReport, retrievedAt: '2026-07-01T12:00:00Z' };
  const strategy = strategyFor(oldFund, { ...phase, generation: now });
  assert.equal(strategy.sectionStatus, 'complete');
  assert.equal(strategy.evidenceRetrievedAt, selectedReport.strategyEvidenceRetrievedAt);
  assert.equal(app.strategyEvidence(selectedReport, now.slice(0, 10), now).complete, true);
  assert.equal(analysisUi.strategyEvidence(strategy, phase.target, now.slice(0, 10)).complete, true);
  for (const timestamp of ['2027-01-01T00:00:00Z', 'invalid', '2026-07-01T12:00:00Z']) {
    selectedReport.strategyEvidenceRetrievedAt = timestamp;
    assert.notEqual(strategyFor(oldFund, { ...phase, generation: now }).sectionStatus, 'complete');
    assert.equal(app.strategyEvidence(selectedReport, now.slice(0, 10), now).complete, false);
  }
});
