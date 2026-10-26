(function (root, factory) {
  'use strict';
  const api = factory(typeof module === 'object' && module.exports ? require('./lifecycle') : root.RecordLabLifecycle,
    typeof module === 'object' && module.exports ? require('./zero-stock') : root.RecordLabZeroStock,
    typeof module === 'object' && module.exports ? require('./nonstock-assets') : root.RecordLabNonstockAssets,
    typeof module === 'object' && module.exports ? require('./strategy-sections') : root.RecordLabStrategySections,
    typeof module === 'object' && module.exports ? require('./source-dates') : root.RecordLabSourceDates);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) api.mount(root.document, root);
})(typeof globalThis !== 'undefined' ? globalThis : this, function (lifecycle, zeroStock, nonstockAssets, strategySections, sourceDates) {
  'use strict';

  const CHANGE_GROUPS = {
    increase: 'increased', decrease: 'decreased', 'entered-disclosure': 'entered',
    'left-disclosure': 'left', unchanged: 'unchanged', 'no-baseline': 'unknown', 'not-comparable': 'unknown'
  };
  const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const isFiniteNumber = value => typeof value === 'number' && Number.isFinite(value);
  const textValue = value => typeof value === 'string' ? value : '';
  const dateOnly = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    !Number.isNaN(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
  const validTimestamp = value => typeof value === 'string' && dateOnly(value.slice(0, 10)) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && !Number.isNaN(Date.parse(value));
  const nullableText = value => value === null || typeof value === 'string';
  const countValue = value => Number.isInteger(value) && value >= 0;
  const STRATEGY_CATEGORIES = { 'market-review': '市场回顾', 'portfolio-operations': '组合操作表述', outlook: '后市展望', unclassified: '其他说明' };
  function strategyEvidence(report, asOf = null, retrievedAt = null) {
    const proof = strategySections?.strategySectionCoverageFor(report?.strategySectionCoverage, report?.periodEnd);
    const month = Number(report?.periodEnd?.slice(5, 7));
    const start = `${report?.periodEnd?.slice(0, 4)}-${String(month - 2).padStart(2, '0')}-01`;
    const current = proof?.kind === 'quarterly' && proof.periodStart === start;
    const observedAt = report?.strategyEvidenceRetrievedAt || retrievedAt;
    const ceiling = validTimestamp(retrievedAt) ? Date.parse(retrievedAt) : sourceDates.chinaDayEnd(asOf);
    const observed = observedAt ? validTimestamp(observedAt) && Date.parse(observedAt) <= ceiling + 300000
      && proof?.publishedAt <= sourceDates.chinaDate(observedAt) : dateOnly(asOf) && proof?.publishedAt <= asOf;
    return { proof, analysis: proof && current && observed ? strategySections.strategyAnalysisFor(report?.strategyAnalysis, proof) : null,
      complete: Boolean(current && observed && proof.complete), label: !proof ? '策略章节尚未完整核验' : !current ? '说明覆盖期并非本季度，未计入本季完整度'
        : !observed ? '策略公告日期与当前观测时点尚未核对'
        : proof.complete ? '本季度投资策略和运作分析章节已完整提取' : '本季度策略章节仍有缺页、边界或文本待核验' };
  }
  const MAX_DATA_BYTES = 80 * 1024 * 1024;
  function safeCompanyPath(path) { return typeof path === 'string' && /^\.\/data\/companies\/[A-Za-z0-9_-]+\.json(?:\.gz)?$/.test(path) ? path : null; }
  function companyPages(manifest, code) { return (manifest?.companyFiles || []).filter(file => file.companyCode === code).slice().sort((a, b) => (a.partIndex || 1) - (b.partIndex || 1)); }
  function companyRecordSummaries(manifest, catalog = null, query = '') {
    const summaries = new Map();
    for (const file of manifest?.companyFiles || []) {
      if (!summaries.has(file.companyCode)) summaries.set(file.companyCode, { code: file.companyCode, name: file.companyName, records: 0, holdings: 0, pages: 0, managers: new Set(), directoryFunds: [] });
      const company = summaries.get(file.companyCode);
      company.records += file.fundCount;
      company.holdings += file.holdingCount || 0;
      company.pages++;
      for (const manager of file.managers || []) company.managers.add(manager);
    }
    for (const fund of catalog?.funds || []) {
      const company = summaries.get(fund.companyCode);
      if (company) company.directoryFunds.push(`${fund.code} ${fund.name || ''}`);
    }
    const tokens = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    return [...summaries.values()].filter(company => {
      const search = [company.code, company.name, ...company.managers, ...company.directoryFunds].join(' ').toLocaleLowerCase();
      return tokens.every(token => search.includes(token));
    }).map(({ directoryFunds, managers, ...company }) => ({ ...company, managers: [...managers] }));
  }
  function catalogComplete(catalog) { return Boolean(catalog && catalog.coverage.complete !== false && catalog.coverage.directoryComplete !== false &&
    !catalog.companies?.some(company => company.directoryStatus === 'stale') && !['bootstrap', 'partial-directory', 'limited-probe', 'unavailable'].includes(catalog.coverage.status)); }
  function companyDirectoryLabel(company, catalogAsOf) {
    if (company.directoryStatus === 'stale') return `沿用缓存归属 · 核验截至 ${company.directoryAsOf || '日期未提供'} · 本轮刷新未成功`;
    if (company.directoryStatus === 'complete') return `归属核验截至 ${company.directoryAsOf || catalogAsOf || '日期未提供'}`;
    return '机构归属本轮尚未核验';
  }
  function catalogFreshness(catalog) {
    return { staleCompanyCount: catalog?.companies?.filter(company => company.directoryStatus === 'stale').length || 0,
      retainedHistoricFundCount: catalog?.coverage?.retainedHistoricFundCount ?? 0 };
  }
  const LEGACY_PERIOD = { id: '2026Q2', label: '2026Q2', path: './data/', targetPeriodEnd: '2026-06-30', baselinePeriodEnd: '2026-03-31' };
  function quarterDates(id) {
    const match = typeof id === 'string' && /^(20\d{2})Q([1-4])$/.exec(id);
    if (!match) return null;
    const year = Number(match[1]), quarter = Number(match[2]);
    const end = month => new Date(Date.UTC(year, month, 0)).toISOString().slice(0, 10);
    return { targetPeriodEnd: end(quarter * 3), baselinePeriodEnd: end((quarter - 1) * 3) };
  }
  function validatePeriods(data) {
    const fail = () => { throw new Error('INVALID_PERIODS'); };
    if (!isRecord(data) || data.schemaVersion !== 1 || !Array.isArray(data.periods) || !data.periods.length || data.periods.length > 40) fail();
    const ids = new Set();
    for (const period of data.periods) {
      const dates = isRecord(period) && quarterDates(period.id);
      if (!dates || ids.has(period.id) || typeof period.label !== 'string' || !period.label.trim() || period.label.length > 64 ||
        period.path !== `./data/periods/${period.id}/` || period.targetPeriodEnd !== dates.targetPeriodEnd || period.baselinePeriodEnd !== dates.baselinePeriodEnd) fail();
      ids.add(period.id);
    }
    if (!ids.has(data.defaultPeriod)) fail();
    return data;
  }
  function selectQuarter(manifest, requested) {
    const periods = manifest ? validatePeriods(manifest).periods : [LEGACY_PERIOD];
    if (requested) return periods.find(period => period.id === requested) || null;
    return periods.find(period => period.id === manifest?.defaultPeriod) || periods[0];
  }
  function periodDataPath(period, file) {
    if (!period || (!((period.id === LEGACY_PERIOD.id && period.path === LEGACY_PERIOD.path)) &&
      (!quarterDates(period.id) || period.path !== `./data/periods/${period.id}/`))) throw new Error('INVALID_PERIOD_PATH');
    if (['latest.json', 'catalog.json', 'progress.json'].includes(file)) return period.path + file;
    const companyPath = safeCompanyPath(file);
    if (!companyPath) throw new Error('INVALID_PERIOD_PATH');
    return period.path + companyPath.slice('./data/'.length);
  }
  function validatePeriodDataset(data, period) {
    validateDataset(data);
    const targets = [data.targetPeriodEnd, data.coverage.targetPeriodEnd].filter(value => value != null);
    const baselines = [data.baselinePeriodEnd, data.coverage.baselinePeriodEnd].filter(value => value != null);
    if (!period || !targets.length || targets.some(value => value !== period.targetPeriodEnd) || baselines.some(value => value !== period.baselinePeriodEnd) ||
      data.funds.some(fund => (fund.selectedReport && fund.selectedReport.periodEnd !== period.targetPeriodEnd) ||
        (fund.notApplicableEvidence && fund.notApplicableEvidence.periodEnd !== period.targetPeriodEnd))) throw new Error('PERIOD_MISMATCH');
    return data;
  }
  async function responseText(response, path, runtime = globalThis) {
    if (!path.endsWith('.gz') || /\bgzip\b/i.test(response.headers?.get('content-encoding') || '')) {
      const raw = await response.text();
      if (raw.length > MAX_DATA_BYTES) throw new Error('INVALID_DATASET');
      return raw;
    }
    if (!response.body || typeof runtime.DecompressionStream !== 'function') throw new Error('GZIP_UNAVAILABLE');
    const reader = response.body.pipeThrough(new runtime.DecompressionStream('gzip')).getReader();
    const decoder = new runtime.TextDecoder('utf-8', { fatal: true });
    const parts = []; let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_DATA_BYTES) throw new Error('INVALID_DATASET');
        parts.push(decoder.decode(value, { stream: true }));
      }
      parts.push(decoder.decode()); return parts.join('');
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  }
  function researchOverview(progress) {
    const research = progress?.research;
    if (!research || research.enabled !== true) return null;
    const notApplicableFunds = countValue(progress.notApplicableFunds) ? progress.notApplicableFunds : 0;
    return { ...research, totalFunds: progress.totalFunds, applicableFunds: progress.totalFunds - notApplicableFunds,
      notApplicableFunds, ...notApplicableBreakdown(progress), completionStatus: progress.completionStatus || 'incomplete-running',
      nextRetryAt: progress.nextRetryAt || null, continueRun: progress.continueRun };
  }
  function notApplicableBreakdown(progress) {
    const rows = progress?.funds || [];
    const count = reason => rows.filter(row => row.status === 'not-applicable' && row.notApplicableEvidence?.reason === reason).length;
    const establishedAfterPeriodFunds = countValue(progress?.establishedAfterPeriodFunds) ? progress.establishedAfterPeriodFunds : count('ESTABLISHED_AFTER_PERIOD');
    const terminatedBeforePeriodFunds = countValue(progress?.terminatedBeforePeriodFunds) ? progress.terminatedBeforePeriodFunds : count('TERMINATED_BEFORE_PERIOD');
    const total = progress?.notApplicableFunds ?? rows.filter(row => row.status === 'not-applicable').length;
    return { establishedAfterPeriodFunds, terminatedBeforePeriodFunds,
      otherNotApplicableFunds: Math.max(0, total - establishedAfterPeriodFunds - terminatedBeforePeriodFunds) };
  }
  function formatRetryAt(value) {
    if (!validTimestamp(value)) return '时间待核验';
    return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) + '（北京时间）';
  }
  function retryState(progress) {
    if (!progress) return { label: '研究完整度待核验', detail: '' };
    if (progress.quarterUniverse?.required === true && progress.quarterUniverse.historicalUniverseVerified !== true
      && progress.completionAudit?.knownDirectoryComplete === true) return { label: '已知目录字段齐全，历史范围待核验', detail: '' };
    if (progress.completionStatus === 'complete') return { label: '适用产品采集证据已齐', detail: '' };
    const scheduled = validTimestamp(progress.nextRetryAt);
    const detail = scheduled ? `后续复查不早于 ${formatRetryAt(progress.nextRetryAt)}，具体执行以任务运行记录为准。` : '';
    if (scheduled && (progress.continueRun === false || progress.completionStatus === 'incomplete-exhausted')) return { label: '本轮结束，等待复查', detail };
    if (progress.completionStatus === 'incomplete-exhausted') return { label: '仍有缺项，需继续处理', detail };
    return { label: '仍在补采', detail };
  }
  function universeDescription(progress, catalog, targetPeriodEnd) {
    const candidate = progress?.quarterUniverse || catalog?.quarterUniverse;
    const value = isRecord(candidate) && candidate.schemaVersion === 1 && candidate.targetPeriodEnd === targetPeriodEnd ? candidate : {};
    const added = countValue(value.historicalAddedFundCount) ? value.historicalAddedFundCount : 0;
    return `报告期历史产品名单尚未完整核验；当前分母为已发现的基金份额代码${added ? `，其中历史来源补入 ${formatNumber(added, 0)} 个` : ''}。${value.historicalParseStatus === 'complete' ? '历史文件已解析，产品及份额范围仍需逐项核对。' : '期后清盘或合并、已从当前目录消失的产品仍在核对。'}`;
  }
  function fundQuality(progress, fund) {
    const direct = progress?.funds?.find(item => item.code === fund.code);
    return direct?.quality || null;
  }

  function safeHttpUrl(value) {
    if (typeof value !== 'string' || /[\u0000-\u001f\u007f]/.test(value)) return null;
    try {
      const url = new URL(value);
      const credentialQuery = [...url.searchParams.keys()].some(key => /(?:token|secret|password|passwd|authorization|signature|credential|api[-_]?key|^key$|^auth$)/i.test(key));
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password && !credentialQuery ? url.href : null;
    } catch (_) { return null; }
  }
  function validEmptyStockEvidence(evidence, report, asOf) {
    if (!isRecord(evidence) || !['empty-stock-detail-section', 'zero-stock-balance-sheet', 'explicit-no-stock-detail', 'exhaustive-nonstock-assets'].includes(evidence.evidenceType) || !isRecord(report) ||
      evidence.confirmed !== true || !['quarterly', 'semiannual', 'annual'].includes(evidence.kind) || evidence.kind !== report.kind ||
      !Number.isInteger(evidence.page) || evidence.page < 1 || !safeHttpUrl(evidence.sourceUrl) ||
      evidence.sourceUrl !== report.sourceUrl || evidence.periodEnd !== report.periodEnd || evidence.publishedAt !== report.publishedAt ||
      !dateOnly(evidence.periodEnd) || !dateOnly(evidence.publishedAt) || !dateOnly(asOf) || evidence.periodEnd > evidence.publishedAt || evidence.publishedAt > asOf) return false;
    if (evidence.evidenceType === 'exhaustive-nonstock-assets') return nonstockAssets?.validNonstockAssets(evidence) === true;
    if (typeof evidence.text !== 'string' || evidence.text.length > 600) return false;
    if (evidence.evidenceType === 'zero-stock-balance-sheet') return zeroStock?.validZeroStock(evidence) === true;
    if (evidence.evidenceType === 'explicit-no-stock-detail') return zeroStock?.validExplicitNoStock(evidence) === true;
    if (typeof evidence.assetAllocationText !== 'string' || evidence.assetAllocationText.length > 600) return false;
    const excerpt = evidence.text.replace(/\s+/g, '');
    const zero = '(?:[-—－–]|0(?:\\.0+)?)';
    const allocation = evidence.assetAllocationText.trim().split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    return /^\d+(?:\.\d+){1,2}期末按公允价值占基金资产净值比例大小排序的所有股票投资明细(?:注[:：])?无[。.]?$/.test(excerpt)
      && allocation.length === 2
      && new RegExp('^\\d*\\s*权益投资\\s+' + zero + '\\s+' + zero + '$').test(allocation[0])
      && new RegExp('^其中[：:]?\\s*股票\\s+' + zero + '\\s+' + zero + '$').test(allocation[1]);
  }

  function validateDataset(data) {
    function fail() { throw new Error('INVALID_DATASET'); }
    const awaiting = isRecord(data) && data.coverage?.status === 'awaiting-data' && data.coverage.fundCount === 0 &&
      Array.isArray(data.funds) && data.funds.length === 0 && Array.isArray(data.companyFiles) && data.companyFiles.length === 0 &&
      data.asOf === null && data.retrievedAt === null && dateOnly(data.targetPeriodEnd);
    if (!isRecord(data) || data.schemaVersion !== 1 || (!awaiting && (!dateOnly(data.asOf) || !validTimestamp(data.retrievedAt))) ||
      !['actions', 'cached'].includes(data.sourceMode) || !isRecord(data.coverage) || !Array.isArray(data.funds) ||
      !Array.isArray(data.errors) || data.funds.length > 150000) fail();
    if (!Number.isInteger(data.coverage.fundCount) || data.coverage.fundCount < 0 ||
      !Number.isInteger(data.coverage.companyCount) || data.coverage.companyCount < 0 ||
      typeof data.coverage.scope !== 'string' || !Array.isArray(data.coverage.limitations) ||
      data.coverage.limitations.some(item => typeof item !== 'string')) fail();
    if (data.companyFiles != null && (!Array.isArray(data.companyFiles) || data.companyFiles.some(file => !isRecord(file) ||
      typeof file.companyCode !== 'string' || typeof file.companyName !== 'string' || !safeCompanyPath(file.path) ||
      !countValue(file.fundCount) || (file.partIndex != null && (!countValue(file.partIndex) || file.partIndex < 1))))) fail();
    for (const fund of data.funds) {
      if (!isRecord(fund) || typeof fund.code !== 'string' || !/^\d{6}$/.test(fund.code) || !nullableText(fund.name) ||
        !nullableText(fund.company) || !Array.isArray(fund.managers) ||
        !['ok', 'unavailable', 'no-equities'].includes(fund.status) || !Array.isArray(fund.rows) || fund.rows.length > 20000) fail();
      if (fund.managers.some(manager => typeof manager !== 'string' && (!isRecord(manager) || typeof manager.name !== 'string'))) fail();
      if (fund.reportManagers != null && (!Array.isArray(fund.reportManagers) || fund.reportManagers.some(manager => typeof manager !== 'string' && (!isRecord(manager) || typeof manager.name !== 'string')))) fail();
      if (fund.shareClassCodes != null && (!Array.isArray(fund.shareClassCodes) || fund.shareClassCodes.some(code => typeof code !== 'string'))) fail();
      if (fund.summary != null && !isRecord(fund.summary)) fail();
      if (fund.interval != null && !isRecord(fund.interval)) fail();
      if (['ok', 'no-equities'].includes(fund.status) && (!isRecord(fund.selectedReport) || !dateOnly(fund.selectedReport.periodEnd))) fail();
      const reportEvidence = fund.selectedReport;
      if (reportEvidence?.strategyExcerpts != null && (!Array.isArray(reportEvidence.strategyExcerpts) ||
        reportEvidence.strategyExcerpts.length > 8 || reportEvidence.strategyExcerpts.some(item => !isRecord(item) ||
          typeof item.text !== 'string' || item.text.length > 260 || !safeHttpUrl(item.sourceUrl) ||
          (item.page !== null && (!Number.isInteger(item.page) || item.page < 1))))) fail();
      if (reportEvidence?.strategyThemes != null && (!Array.isArray(reportEvidence.strategyThemes) ||
        reportEvidence.strategyThemes.some(theme => typeof theme !== 'string' || theme.length > 30))) fail();
      if (fund.status === 'no-equities') {
        const evidence = fund.noEquitiesEvidence || fund.selectedReport?.noEquitiesEvidence;
        if (!isRecord(evidence) || evidence.confirmed !== true || !safeHttpUrl(evidence.sourceUrl) || evidence.periodEnd !== fund.selectedReport.periodEnd
          || fund.rows.some(row => row.current != null || !row.previous) || (fund.rows.length && !isRecord(fund.previousReport))) fail();
        if (['empty-stock-detail-section', 'zero-stock-balance-sheet', 'explicit-no-stock-detail', 'exhaustive-nonstock-assets'].includes(evidence.evidenceType)) {
          if (!validEmptyStockEvidence(evidence, fund.selectedReport, data.asOf)) fail();
        } else {
          const text = typeof evidence.text === 'string' ? evidence.text.replace(/\s+/g, '') : '';
          if (evidence.evidenceType != null || /并非未|不是未|不能确认|尚未确认|无法确认/.test(text) ||
            !/(?:未持有|未投资于?)股票(?:资产|投资|组合)?(?:[。；，,.;：:]|$)/.test(text)) fail();
        }
      }
      if (fund.notApplicableEvidence != null) {
        const evidence = fund.notApplicableEvidence;
        const closed = evidence?.reason === 'TERMINATED_BEFORE_PERIOD' && lifecycle?.normalizeLifecycleEvidence(evidence,
          { code: fund.code, periodEnd: data.targetPeriodEnd || data.coverage.targetPeriodEnd || evidence.periodEnd, asOf: fund.asOf || data.asOf });
        const founded = evidence?.reason === 'ESTABLISHED_AFTER_PERIOD' && dateOnly(evidence.date) && dateOnly(evidence.periodEnd)
          && evidence.date > evidence.periodEnd && evidence.date <= data.asOf && evidence.fundCode === fund.code && safeHttpUrl(evidence.sourceUrl);
        if (!isRecord(evidence) || fund.status !== 'unavailable' || fund.selectedReport !== null || fund.rows.length || (!closed && !founded)) fail();
      }
      if (fund.previousReport && (!isRecord(fund.previousReport) || !dateOnly(fund.previousReport.periodEnd))) fail();
      if (fund.narrative && (!isRecord(fund.narrative) || !nullableText(fund.narrative.text))) fail();
      if (fund.warnings && (!Array.isArray(fund.warnings) || fund.warnings.some(item => typeof item !== 'string'))) fail();
      for (const row of fund.rows) {
        if (!isRecord(row) || typeof row.stockCode !== 'string' || !nullableText(row.stockName) ||
          !isRecord(row.change) || !Object.hasOwn(CHANGE_GROUPS, row.change.kind) || !isRecord(row.position) || !isRecord(row.priceRange)) fail();
        for (const holding of [row.current, row.previous]) {
          if (holding !== null && !isRecord(holding)) fail();
          if (holding && ['shares', 'weightPct', 'marketValueYuan'].some(key => holding[key] != null && !isFiniteNumber(holding[key]))) fail();
        }
        for (const key of ['disclosedYuan', 'estimatedYuan', 'valueYuan']) {
          if (row.position[key] != null && !isFiniteNumber(row.position[key])) fail();
        }
        for (const key of ['low', 'high']) {
          if (row.priceRange[key] != null && !isFiniteNumber(row.priceRange[key])) fail();
        }
      }
    }
    return data;
  }

  function validateCatalog(data) {
    const fail = () => { throw new Error('INVALID_CATALOG'); };
    if (!isRecord(data) || data.schemaVersion !== 1 || !dateOnly(data.asOf) || !validTimestamp(data.retrievedAt) ||
      !Array.isArray(data.companies) || !Array.isArray(data.funds) || !isRecord(data.coverage) ||
      data.companies.length > 10000 || data.funds.length > 150000) fail();
    const codes = new Set();
    for (const company of data.companies) {
      if (!isRecord(company) || typeof company.code !== 'string' || !company.code || typeof company.name !== 'string' ||
        !(company.fundCount === null || countValue(company.fundCount)) || codes.has(company.code)) fail();
      if (company.directoryAsOf != null && (!dateOnly(company.directoryAsOf) || company.directoryAsOf > data.asOf)) fail();
      if (company.directoryStatus === 'stale' && !dateOnly(company.directoryAsOf)) fail();
      codes.add(company.code);
    }
    const fundCodes = new Set();
    for (const fund of data.funds) {
      if (!isRecord(fund) || typeof fund.code !== 'string' || !/^\d{6}$/.test(fund.code) || typeof fund.name !== 'string' ||
        !nullableText(fund.companyCode) || !nullableText(fund.companyName) || (fund.companyCode !== null && !codes.has(fund.companyCode)) || fundCodes.has(fund.code)) fail();
      fundCodes.add(fund.code);
    }
    if (!countValue(data.coverage.companyCount) || !countValue(data.coverage.fundCount)) fail();
    if (data.coverage.staleCompanyCount != null && (!countValue(data.coverage.staleCompanyCount) ||
      data.coverage.staleCompanyCount !== data.companies.filter(company => company.directoryStatus === 'stale').length)) fail();
    if (data.coverage.retainedHistoricFundCount != null && (!countValue(data.coverage.retainedHistoricFundCount) || data.coverage.retainedHistoricFundCount > data.funds.length)) fail();
    return data;
  }

  function validateProgress(data) {
    const fail = () => { throw new Error('INVALID_PROGRESS'); };
    if (!isRecord(data) || data.schemaVersion !== 1 || !validTimestamp(data.updatedAt) || !Array.isArray(data.companies) || !Array.isArray(data.funds) ||
      ['totalCompanies', 'totalFunds', 'processedFunds', 'completedCompanies', 'pendingFunds'].some(key => !countValue(data[key]))) fail();
    if (data.completedCompanies > data.totalCompanies || data.processedFunds > data.totalFunds || data.pendingFunds > data.totalFunds) fail();
    if (data.notApplicableFunds != null && (!countValue(data.notApplicableFunds) || data.notApplicableFunds + data.processedFunds > data.totalFunds)) fail();
    for (const key of ['establishedAfterPeriodFunds', 'terminatedBeforePeriodFunds']) if (data[key] != null && (!countValue(data[key]) || data[key] > (data.notApplicableFunds || 0))) fail();
    if ((data.establishedAfterPeriodFunds || 0) + (data.terminatedBeforePeriodFunds || 0) > (data.notApplicableFunds || 0)) fail();
    if (data.deferredFunds != null && (!countValue(data.deferredFunds) || data.deferredFunds > data.totalFunds)) fail();
    for (const key of ['nextRetryAt', 'directoryNextRetryAt']) if (data[key] != null && !validTimestamp(data[key])) fail();
    if (data.research != null) {
      const research = data.research;
      if (!isRecord(research) || typeof research.enabled !== 'boolean' ||
        ['completeFunds', 'incompleteFunds', 'pendingFunds', 'exhaustedFunds'].some(key => !countValue(research[key]) || research[key] > data.totalFunds) ||
        !isRecord(research.fieldCoverage) || ['pdf', 'strategy', 'nav', 'baseline', 'prices'].some(key => !countValue(research.fieldCoverage[key]) || research.fieldCoverage[key] > data.totalFunds)) fail();
      if (research.fieldCoverage.holdings != null && (!countValue(research.fieldCoverage.holdings) || research.fieldCoverage.holdings > data.totalFunds)) fail();
    }
    if (data.completionStatus != null && !['complete', 'incomplete-running', 'incomplete-exhausted'].includes(data.completionStatus)) fail();
    const companies = new Set();
    for (const company of data.companies) {
      if (!isRecord(company) || typeof company.code !== 'string' || typeof company.name !== 'string' ||
        !(company.totalFunds === null || countValue(company.totalFunds)) || ['processedFunds', 'withHoldings', 'withoutEquities', 'failed', 'pending'].some(key => !countValue(company[key])) || companies.has(company.code)) fail();
      if (company.notApplicable != null && !countValue(company.notApplicable)) fail();
      for (const key of ['establishedAfterPeriod', 'terminatedBeforePeriod']) if (company[key] != null && (!countValue(company[key]) || company[key] > (company.notApplicable || 0))) fail();
      if ((company.establishedAfterPeriod || 0) + (company.terminatedBeforePeriod || 0) > (company.notApplicable || 0)) fail();
      if (company.deferredFunds != null && !countValue(company.deferredFunds)) fail();
      companies.add(company.code);
    }
    const funds = new Set();
    for (const fund of data.funds) {
      if (!isRecord(fund) || typeof fund.code !== 'string' || !nullableText(fund.companyCode) ||
        !['complete', 'no-equities', 'not-applicable', 'failed', 'pending'].includes(fund.status) || funds.has(fund.code)) fail();
      for (const key of ['deferredUntil', 'lastDeferredAt']) if (fund[key] != null && !validTimestamp(fund[key])) fail();
      funds.add(fund.code);
    }
    return data;
  }

  function catalogIndex(catalog) {
    return { funds: new Map((catalog?.funds || []).map(fund => [fund.code, fund])), companies: new Map((catalog?.companies || []).map(company => [company.code, company])) };
  }
  function companyCodeFor(fund, index) {
    if (fund.companyCode) return fund.companyCode;
    return index.funds.get(fund.code)?.companyCode || (fund.shareClassCodes || []).map(code => index.funds.get(code)?.companyCode).find(Boolean) || '';
  }
  function coverageOverview(catalog, progress, data) {
    const index = catalogIndex(catalog);
    const available = (data?.funds || []).filter(fund => ['ok', 'no-equities'].includes(fund.status));
    const codes = new Set(available.flatMap(fund => [fund.code, ...(fund.shareClassCodes || [])]));
    const companies = new Set(available.map(fund => companyCodeFor(fund, index) || fund.company).filter(Boolean));
    const directoryComplete = catalogComplete(catalog);
    const catalogFunds = directoryComplete ? catalog.coverage.fundCount : null;
    const catalogCompanies = directoryComplete ? catalog.coverage.companyCount : null;
    const coveredFunds = data?.companyFiles ? data.coverage.catalogAvailableFundCount ?? data.coverage.fundCount : catalog ? [...codes].filter(code => index.funds.has(code)).length : new Set(available.map(fund => fund.code)).size;
    const processed = progress?.processedFunds ?? null;
    return { coveredFunds, coveredCompanies: data?.companyFiles ? data.coverage.companyCount : companies.size, totalFunds: catalogFunds, totalCompanies: catalogCompanies,
      processedFunds: processed, pendingFunds: progress?.pendingFunds ?? null,
      percent: processed !== null && progress.totalFunds > 0 ? Math.min(100, processed / progress.totalFunds * 100) : null };
  }
  function companyCoverage(catalog, progress, query = '') {
    const tokens = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    const progressMap = new Map((progress?.companies || []).map(company => [company.code, company]));
    return (catalog?.companies || progress?.companies || []).filter(company =>
      tokens.every(token => `${company.code} ${company.name}`.toLocaleLowerCase().includes(token))).map(company => {
      const entry = progressMap.get(company.code);
      const exemptions = notApplicableBreakdown({ notApplicableFunds: entry?.notApplicable,
        establishedAfterPeriodFunds: entry?.establishedAfterPeriod, terminatedBeforePeriodFunds: entry?.terminatedBeforePeriod,
        funds: (progress?.funds || []).filter(fund => fund.companyCode === company.code) });
      return { ...company, totalFunds: company.fundCount ?? company.totalFunds ?? null,
        processedFunds: entry?.processedFunds ?? null, withHoldings: entry?.withHoldings ?? null,
        withoutEquities: entry?.withoutEquities ?? null, failed: entry?.failed ?? null, pending: entry?.pending ?? null,
        notApplicable: entry?.notApplicable ?? null,
        establishedAfterPeriod: entry ? exemptions.establishedAfterPeriodFunds : null,
        terminatedBeforePeriod: entry ? exemptions.terminatedBeforePeriodFunds : null,
        deferredFunds: entry?.deferredFunds ?? null,
        lastAttemptAt: entry?.lastAttemptAt || null };
    });
  }

  function managerNames(fund) {
    const managers = ['report', 'tenure-overlap'].includes(fund.managerAttribution) && fund.reportManagers?.length ? fund.reportManagers : fund.managers;
    return managers.map(manager => typeof manager === 'string' ? manager : manager.name).filter(Boolean);
  }
  function managerAttributionLabel(fund) {
    if (fund.managerAttribution === 'report' && fund.reportManagers?.length) return fund.managerCoverageStatus === 'complete'
      ? '报告期内经理（经理表完整）' : '报告期内已核验经理（经理表待补）';
    if (fund.managerAttribution === 'tenure-overlap' && fund.reportManagers?.length) return '任期与报告期重叠（经理表待核验）';
    return '当前经理概况';
  }
  function managerTermLabel(manager, periodEnd) {
    if (!manager || typeof manager === 'string' || !dateOnly(manager.startDate) || !dateOnly(periodEnd)
      || manager.startDate > periodEnd || manager.endDate && (!dateOnly(manager.endDate) || manager.endDate < manager.startDate)) return '任期待核验';
    return `任职 ${manager.startDate} 至 ${manager.endDate || '未披露离任日'} · ${manager.endDate && manager.endDate <= periodEnd ? '期内离任' : '期末在任'}`;
  }
  function reportPeriod(fund) { return fund.selectedReport && textValue(fund.selectedReport.periodEnd) || ''; }
  function changeGroup(row) { return CHANGE_GROUPS[row.change.kind] || 'unknown'; }
  function latestFunds(funds) {
    const periods = new Map();
    for (const fund of funds) {
      const period = reportPeriod(fund);
      if (!periods.has(fund.code) || period > periods.get(fund.code)) periods.set(fund.code, period);
    }
    return funds.filter(fund => reportPeriod(fund) === periods.get(fund.code));
  }

  function filterFunds(funds, filters = {}, catalog = null) {
    const tokens = textValue(filters.query).toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    const source = filters.period === 'latest' ? latestFunds(funds) : funds;
    const index = catalogIndex(catalog);
    return source.flatMap(fund => {
      if (filters.company?.startsWith('code:')) {
        if (companyCodeFor(fund, index) !== filters.company.slice(5)) return [];
      } else if (filters.company && fund.company !== filters.company) return [];
      if (filters.manager && !managerNames(fund).includes(filters.manager)) return [];
      if (filters.period && filters.period !== 'latest' && reportPeriod(fund) !== filters.period) return [];
      const fundSearch = [fund.code, fund.name, fund.company, index.companies.get(companyCodeFor(fund, index))?.name, ...managerNames(fund), ...(fund.shareClassCodes || [])].join(' ').toLocaleLowerCase();
      const visibleRows = fund.rows.filter(row => {
        const rowSearch = `${fundSearch} ${row.stockCode} ${row.stockName} ${row.market || ''}`.toLocaleLowerCase();
        return (!filters.change || changeGroup(row) === filters.change) && tokens.every(token => rowSearch.includes(token));
      });
      const matchEmptyFund = !filters.change && fund.rows.length === 0 && tokens.every(token => fundSearch.includes(token));
      return visibleRows.length || matchEmptyFund ? [{ ...fund, visibleRows }] : [];
    });
  }

  function formatNumber(value, digits = 2) {
    if (!isFiniteNumber(value)) return '未提供';
    return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(value);
  }
  function formatMoney(value) {
    if (!isFiniteNumber(value)) return '未提供';
    const abs = Math.abs(value);
    if (abs >= 1e8) return `${formatNumber(value / 1e8)} 亿元`;
    if (abs >= 1e4) return `${formatNumber(value / 1e4)} 万元`;
    return `${formatNumber(value)} 元`;
  }
  function signed(value, suffix = '') {
    if (!isFiniteNumber(value)) return '不可比较';
    return `${value > 0 ? '+' : ''}${formatNumber(value)}${suffix}`;
  }
  function holdingValue(holding, key, suffix = '') {
    if (holding === null || holding === undefined) return '未在披露名单';
    return isFiniteNumber(holding[key]) ? `${formatNumber(holding[key])}${suffix}` : '未提供';
  }
  function priceDescription(range) {
    if (!range || range.status !== 'observed' || !isFiniteNumber(range.low) || !isFiniteNumber(range.high)) {
      return { value: '价格区间缺失', note: range && textValue(range.label) || '尚无可用行情', observed: false };
    }
    // A source calendar can be complete while another retained observation
    // disagrees. Even malformed conflict metadata must not upgrade the view.
    const conflictPending = Object.hasOwn(range, 'globalPriceConflict');
    const complete = !conflictPending && range.coverageVersion === 2 && range.calendarId === 'exchange-sessions-2026-q2-q3-v1'
      && range.sessionScope === 'regular-trading-session' && range.priceBasis === 'unadjusted'
      && range.currency === { CN: 'CNY', HK: 'HKD', US: 'USD' }[range.market]
      && range.low > 0 && range.high >= range.low && range.completeness === 'complete'
      && Number.isInteger(range.expectedSessionCount) && range.expectedSessionCount > 0
      && range.expectedSessionCount === range.observedCount && range.missingSessionCount === 0
      && range.rejectedRowCount === 0 && range.unexpectedSessionCount === 0 && range.conflictingDateCount === 0
      && typeof range.expectedDateSetHash === 'string' && /^[a-f0-9]{64}$/.test(range.expectedDateSetHash)
      && range.observedDateSetHash === range.expectedDateSetHash;
    const coverage = conflictPending ? '存在来源冲突待核验' : complete ? '交易日完整' : Number.isInteger(range.missingSessionCount) && range.missingSessionCount > 0
      ? `仍缺 ${range.missingSessionCount} 个交易日或上市/停牌证据` : '完整交易日覆盖未核验';
    return { value: `${formatNumber(range.low, 3)} – ${formatNumber(range.high, 3)} ${textValue(range.currency)}`.trim(),
      note: `${range.observedStart || '日期未提供'} → ${range.observedEnd || '日期未提供'} · ${isFiniteNumber(range.observedCount) ? range.observedCount : '—'} 条行情 · ${coverage}`, observed: true, complete, conflictPending };
  }

  function priceConflictSources(range) {
    const facts = range?.globalPriceConflict?.facts;
    if (!Array.isArray(facts) || facts.length !== 2) return [];
    return facts.flatMap(fact => {
      const price = fact?.price;
      if (!isRecord(price) || !isFiniteNumber(price.low) || !isFiniteNumber(price.high)
        || price.low <= 0 || price.high < price.low || !safeHttpUrl(fact.sourceUrl)
        || !validTimestamp(fact.fetchedAt)
        || ['market', 'stockCode', 'currency', 'rangeStart', 'rangeEnd', 'priceBasis'].some(key => price[key] !== range[key])) return [];
      return [{ sourceUrl: safeHttpUrl(fact.sourceUrl), fetchedAt: fact.fetchedAt,
        interval: `${formatNumber(price.low, 8)} – ${formatNumber(price.high, 8)} ${textValue(price.currency)}` }];
    });
  }

  function csvCell(value) {
    let string = value == null ? '' : String(value);
    if (typeof value === 'string' && (/^[\s\u0000-\u001f]*[=+\-@]/u.test(string) || /^[\t\r\n]/.test(string))) string = `'${string}`;
    return `"${string.replace(/"/g, '""')}"`;
  }
  function exportCsv(funds) {
    const lines = [['基金代码', '基金名称', '基金公司', '基金经理', '本期报告期', '上期报告期', '证券代码', '证券名称', '市场', '币种',
      '变化类型', '变化说明', '本期持股数', '上期持股数', '持股数变化', '本期净资产占比(%)', '上期净资产占比(%)', '占比变化(百分点)',
      '报告披露市值(人民币元)', '净资产乘占比估算(人民币元)', '期间最低价', '期间最高价', '行情状态', '价格口径', '观察窗口起始', '观察窗口结束',
      '实际行情起始', '实际行情结束', '行情条数', '实际持有成本', '持仓报告来源', '行情来源', '运作说明期间', '运作说明来源',
      '持仓数据来源', '净资产数据来源', '经理归属观测日', '经理归属口径', '经理概况来源',
      '该基金观察截至', '该基金采集时间', '该基金数据状态', '行情采集时间', '行情数据状态',
      '价格区间完整性', '应有交易日数', '缺失交易日数', '异常行情行数', '应有日期集合哈希', '观测日期集合哈希',
      '待核验行情来源1', '对照价格区间1', '原始采集时间1', '待核验行情来源2', '对照价格区间2', '原始采集时间2']];
    for (const fund of funds) for (const row of fund.visibleRows || fund.rows) {
      const price = row.priceRange || {};
      const validPrice = priceDescription(price).observed;
      const conflictSources = priceConflictSources(price);
      lines.push([fund.code, fund.name, fund.company, managerNames(fund).join(' / '), reportPeriod(fund), fund.previousReport?.periodEnd,
        row.stockCode, row.stockName, row.market, row.currency, row.change.label || row.change.kind, row.change.reason,
        row.current?.shares, row.previous?.shares, row.change.sharesDelta, row.current?.weightPct, row.previous?.weightPct, row.change.weightDeltaPp,
        row.position.disclosedYuan, row.position.estimatedYuan, validPrice ? price.low : null, validPrice ? price.high : null,
        price.label || price.status, price.priceBasis, price.rangeStart, price.rangeEnd, price.observedStart, price.observedEnd,
        price.observedCount, '未知；区间价格不代表持有成本', safeHttpUrl(fund.selectedReport?.sourceUrl), safeHttpUrl(price.sourceUrl),
        fund.narrative ? `${fund.narrative.periodStart || ''} / ${fund.narrative.periodEnd || ''}` : '', safeHttpUrl(fund.narrative?.sourceUrl),
        safeHttpUrl(fund.selectedReport?.holdingsSourceUrl), safeHttpUrl(fund.selectedReport?.navSourceUrl), fund.managersAsOf,
        managerAttributionLabel(fund), safeHttpUrl(fund.managerSourceUrl || fund.metadataSourceUrl),
        fund.asOf, fund.retrievedAt, fund.dataStatus, price.fetchedAt, price.dataStatus,
        priceDescription(price).conflictPending ? '来源冲突待核验' : priceDescription(price).complete ? '完整交易日区间' : '待补全', price.expectedSessionCount, price.missingSessionCount,
        price.rejectedRowCount, price.expectedDateSetHash, price.observedDateSetHash,
        ...[0, 1].flatMap(index => [conflictSources[index]?.sourceUrl, conflictSources[index]?.interval, conflictSources[index]?.fetchedAt])]);
    }
    return '\ufeff' + lines.map(line => line.map(csvCell).join(',')).join('\r\n');
  }

  function observationState(data, mode, failed) {
    if (!data) return { badge: failed ? '尚无可用数据' : '正在载入', className: 'badge warning', dates: '等待首份有效公开数据' };
    if (data.coverage?.status === 'awaiting-data' && data.asOf === null && data.retrievedAt === null) {
      return { badge: failed ? '等待首份有效数据 · 载入未成功' : '等待首份有效数据', className: 'badge warning',
        dates: `目标报告期 ${data.targetPeriodEnd} · 尚无有效观察或采集时点` };
    }
    const badge = mode === 'imported' ? '本地导入' : data.sourceMode === 'cached' ? '保留的有效数据' : '公开数据快照';
    return { badge: failed ? `${badge} · 载入未成功` : badge,
      className: `badge ${failed || data.sourceMode === 'cached' ? 'warning' : mode === 'imported' ? 'imported' : 'success'}`,
      dates: `观察截至 ${data.asOf} · 采集 ${data.retrievedAt.replace('T', ' ').replace(/\.\d+Z$/, ' UTC').replace(/Z$/, ' UTC')}` };
  }

  function mount(doc, win) {
    if (!doc.getElementById('fund-results')) return;
    const $ = id => doc.getElementById(id);
    const state = { data: null, manifest: null, companyCode: '', pageIndex: 0, companyRequest: 0, loadingCompany: false, catalog: null, progress: null, directoryRequest: 0, directoryFailures: [], mode: 'remote', failed: false, filtered: [], limit: 30, request: 0,
      period: null, periods: null, periodsReady: false };
    const inputs = { query: $('search'), company: $('filter-company'), manager: $('filter-manager'), period: $('filter-period'), change: $('filter-change') };

    function el(tag, className, text) {
      const node = doc.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = String(text);
      return node;
    }
    function addLink(parent, label, url) {
      const safe = safeHttpUrl(url);
      if (!safe) return false;
      const link = el('a', '', label);
      link.href = safe; link.target = '_blank'; link.rel = 'noopener noreferrer';
      parent.append(link); return true;
    }
    function emptyState(title, description, compact) {
      const box = el('div', `empty-state${compact ? ' compact' : ''}`);
      if (!compact) { const icon = el('span', 'empty-icon', '≋'); icon.setAttribute('aria-hidden', 'true'); box.append(icon); }
      box.append(el('h3', '', title), el('p', '', description)); return box;
    }
    function updateObservation(message) {
      const observation = observationState(state.manifest || state.data, state.mode, state.failed);
      $('source-badge').textContent = observation.badge;
      $('source-badge').className = observation.className;
      $('observation-dates').textContent = observation.dates;
      $('load-status').textContent = message;
      $('load-status').className = state.failed ? 'is-error' : '';
    }
    function updateQuarterHeading() {
      const period = state.period;
      $('quarter-eyebrow').textContent = period ? `${period.label} · FUND DISCLOSURE EXPLORER` : 'FUND DISCLOSURE EXPLORER';
      $('quarter-title').textContent = period ? period.label : '所选季度尚未发布';
      $('quarter-description').textContent = period ? `报告期 ${period.targetPeriodEnd}，对比 ${period.baselinePeriodEnd}。各季度独立保留，持仓覆盖与研究完整度分别统计。` : '这个季度尚无已发布数据；请选择可用季度。';
    }
    function clearQuarterData() {
      win.RecordAnalysis?.reset();
      state.request++; state.companyRequest++; state.directoryRequest++;
      state.data = null; state.manifest = null; state.catalog = null; state.progress = null;
      state.companyCode = ''; state.pageIndex = 0; state.loadingCompany = false;
      state.directoryFailures = []; state.filtered = []; state.failed = false; state.dataUpdated = false; state.mode = 'remote';
      Object.values(inputs).forEach(input => { input.value = ''; });
      $('company-search').value = '';
      $('coverage-notes').replaceChildren();
      $('stat-period').textContent = state.period?.targetPeriodEnd || '—';
      $('stat-prices').textContent = '—'; $('stat-prices-note').textContent = '等待本季度行情统计';
      $('fund-results').replaceChildren(emptyState('正在读取所选季度', '各季度数据独立载入；不会用其他季度记录补齐。'));
      $('result-count').textContent = '等待本季度数据'; $('export-csv').disabled = true;
      updatePagination(); renderDirectory();
    }
    async function chooseQuarter(id, writeUrl = true) {
      state.period = selectQuarter(state.periods, id);
      clearQuarterData(); updateQuarterHeading();
      if (writeUrl && win.history?.replaceState) {
        const url = new win.URL(win.location.href); url.searchParams.set('quarter', id);
        win.history.replaceState(null, '', url);
      }
      if (!state.period) {
        state.failed = true; $('reload').disabled = false;
        updateObservation('所选季度尚未发布，未显示其他季度数据。');
        $('fund-results').replaceChildren(emptyState('所选季度尚未发布', '请从季度选择器选择已发布季度；尚未采集的数据不会由旧季度替代。'));
        return;
      }
      $('quarter-select').value = state.period.id;
      await reload();
    }
    async function initializePeriods() {
      const controller = new win.AbortController(); const timer = win.setTimeout(() => controller.abort(), 15000);
      $('reload').disabled = true;
      try {
        const response = await win.fetch('./data/periods.json', { cache: 'no-store', credentials: 'omit', signal: controller.signal });
        if (response.ok) {
          const raw = await response.text(); if (raw.length > 100000) throw new Error('INVALID_PERIODS');
          state.periods = validatePeriods(JSON.parse(raw));
        }
      } catch (_) { state.periods = null; }
      finally { win.clearTimeout(timer); }
      const available = state.periods?.periods || [LEGACY_PERIOD];
      const select = $('quarter-select'); select.replaceChildren();
      for (const period of available) { const option = el('option', '', period.label); option.value = period.id; select.append(option); }
      const requested = new win.URL(win.location.href).searchParams.get('quarter');
      const period = selectQuarter(state.periods, requested);
      if (!period && requested) {
        const option = el('option', '', `${quarterDates(requested) ? requested : '所选季度'}（尚未发布）`);
        option.value = requested; select.append(option); select.value = requested;
      }
      state.periodsReady = true;
      await chooseQuarter(period?.id || requested, false);
    }
    function selectOptions(node, entries, defaults) {
      const previous = node.value;
      node.replaceChildren();
      for (const [value, label] of [...defaults, ...entries.map(value => [value, value])]) {
        const option = el('option', '', label); option.value = value; node.append(option);
      }
      if (Array.from(node.options).some(option => option.value === previous)) node.value = previous;
    }
    function updateCompanyOptions() {
      const previous = inputs.company.value;
      inputs.company.replaceChildren();
      const entries = [['', state.manifest ? '全部已收录公司总览' : state.catalog ? '全部目录公司' : '全部已收录公司']];
      const records = new Map(companyRecordSummaries(state.manifest).map(company => [company.code, company.records]));
      const index = catalogIndex(state.catalog);
      for (const company of state.catalog?.companies || state.progress?.companies || []) entries.push([`code:${company.code}`, state.manifest ? `${company.name} · ${records.get(company.code) || 0} 份已收录记录` : company.name]);
      for (const file of state.manifest?.companyFiles || []) if (!entries.some(([value]) => value === `code:${file.companyCode}`)) entries.push([`code:${file.companyCode}`, `${file.companyName} · ${records.get(file.companyCode)} 份已收录记录`]);
      const extra = new Set((state.data?.funds || []).filter(fund => !companyCodeFor(fund, index)).map(fund => fund.company).filter(Boolean));
      for (const company of [...extra].sort((a, b) => a.localeCompare(b, 'zh-CN'))) entries.push([company, company]);
      for (const [value, label] of entries) { const option = el('option', '', label); option.value = value; inputs.company.append(option); }
      if (entries.some(([value]) => value === previous)) inputs.company.value = previous;
      else if (previous) inputs.company.value = entries.find(([, label]) => label === previous)?.[0] || '';
    }
    function updateStatistics() {
      const overview = coverageOverview(state.catalog, state.progress, state.manifest || state.data);
      $('stat-funds').textContent = `${formatNumber(overview.coveredFunds, 0)} / ${overview.totalFunds === null ? '—' : formatNumber(overview.totalFunds, 0)}`;
      $('stat-companies').textContent = `${formatNumber(overview.coveredCompanies, 0)} / ${overview.totalCompanies === null ? '—' : formatNumber(overview.totalCompanies, 0)}`;
      $('stat-scope').textContent = overview.totalFunds !== null ? '已发现目录按份额计数；历史范围仍待核验' : '目录生成中，历史分母尚未确认';
      $('stat-company-note').textContent = overview.totalCompanies !== null ? '目录公司均可查询处理状态' : '全量目录生成中，已有报告不代表全量';
      const freshness = catalogFreshness(state.catalog);
      if (freshness.staleCompanyCount) $('stat-company-note').textContent = `${freshness.staleCompanyCount} 家归属沿用缓存，核验日期见公司行`;
      const global = state.manifest?.coverage;
      if (global) {
        $('stat-period').textContent = state.manifest.targetPeriodEnd || global.targetPeriodEnd || global.latestPeriod || state.period?.targetPeriodEnd || '未提供';
        $('stat-prices').textContent = `${formatNumber(global.priceComplete || 0, 0)} / ${formatNumber(global.holdingCount || 0, 0)}`;
        $('stat-prices-note').textContent = `完整交易日区间 / 当前持仓；另有 ${formatNumber(global.priceAvailable || 0, 0)} 条已有观测`;
      }
    }
    function renderDirectory() {
      updateCompanyOptions(); updateStatistics();
      const overview = coverageOverview(state.catalog, state.progress, state.data);
      const incomplete = !catalogComplete(state.catalog);
      const freshness = catalogFreshness(state.catalog);
      const staleLabel = freshness.staleCompanyCount ? ` · ${freshness.staleCompanyCount} 家沿用缓存归属` : '';
      $('catalog-badge').textContent = (incomplete ? `目录核验未齐${state.catalog ? ` · 已发现 ${state.catalog.companies.length} 家` : ''}` : `${state.catalog.companies.length} 家目录公司`) + staleLabel;
      $('catalog-badge').className = `badge ${incomplete ? 'warning' : 'success'}`;
      $('progress-count').textContent = state.progress ? `${incomplete ? '当前已发现目录' : ''}已取得记录 ${formatNumber(state.progress.processedFunds, 0)} / ${formatNumber(state.progress.totalFunds, 0)} 只基金份额` : '处理进度待生成';
      const exemptions = notApplicableBreakdown(state.progress);
      const exemptionText = `${formatNumber(exemptions.establishedAfterPeriodFunds, 0)} 只季末后成立 · ${formatNumber(exemptions.terminatedBeforePeriodFunds, 0)} 只季末前已终止${exemptions.otherNotApplicableFunds ? ` · ${formatNumber(exemptions.otherNotApplicableFunds, 0)} 只其他不适用` : ''}`;
      $('progress-detail').textContent = state.progress ? `${formatNumber(state.progress.failedFunds ?? 0, 0)} 只抓取后仍未取得 · ${formatNumber(state.progress.pendingFunds, 0)} 只待处理 · ${formatNumber(state.progress.deferredFunds ?? 0, 0)} 只本批未轮到 · ${exemptionText}` : '已有记录与字段完整是两项独立计数。';
      $('progress-percent').textContent = overview.percent === null ? '—' : `${formatNumber(overview.percent, 1)}%`;
      $('coverage-progress').value = overview.percent || 0;
      const research = researchOverview(state.progress);
      const quality = $('research-status'); quality.replaceChildren();
      if (research) {
        const retry = retryState(state.progress);
        const managerAudited = countValue(research.fieldCoverage.managers);
        quality.append(el('strong', '', `${managerAudited ? '七项采集证据齐全' : '原六项采集证据齐全'} ${formatNumber(research.completeFunds, 0)} / ${formatNumber(research.applicableFunds, 0)} 只适用份额 · ${retry.label}`));
        quality.append(el('p', '', '采集证据齐全不代表基金经理观点的全文语义解读已完成；解读进度单独列示。'));
        if (countValue(research.sixFieldsCompleteFunds)) quality.append(el('p', '', `六项基础字段齐全 ${formatNumber(research.sixFieldsCompleteFunds, 0)}（按当前校验）；七项口径另要求完整的报告经理表证据。`));
        if (!managerAudited) quality.append(el('p', '', '报告经理证据尚未纳入这一版完整度，仍待回补核验。'));
        quality.append(el('span', '', `待补 ${formatNumber(research.pendingFunds, 0)} · 本轮重试已用完 ${formatNumber(research.exhaustedFunds, 0)}`));
        if (retry.detail) quality.append(el('p', '', retry.detail));
        if (state.progress?.deferredFunds) quality.append(el('p', '', `本批未轮到 ${formatNumber(state.progress.deferredFunds, 0)} 只：保留原状态与尝试次数，不记为一次新的抓取失败；该数可能与原有待处理或失败状态重叠。`));
        if (research.notApplicableFunds) quality.append(el('span', '', `另有 ${exemptionText}，按对应公开证据单列，不计为无股票持仓或研究已完成。`));
        const labels = { holdings: '持仓披露表', pdf: 'PDF', strategy: '策略章节', nav: '净资产', baseline: '上期基准', prices: '行情', managers: '报告经理' };
        quality.append(el('p', '', Object.entries(labels).map(([key, label]) => `${label} ${formatNumber(research.fieldCoverage[key], 0)}`).join(' · ')));
      } else {
        quality.append(el('p', '', '研究完整度尚未核验；取得持仓或无股票证据不代表 PDF、策略、规模和比较基准都已齐全。'));
        const retry = retryState(state.progress);
        if (retry.detail) quality.append(el('p', '', `${retry.label}。${retry.detail}`));
      }
      quality.append(el('p', 'narrative-meta', universeDescription(state.progress, state.catalog, state.period?.targetPeriodEnd)));
      $('catalog-dates').textContent = [state.catalog ? `主目录截至 ${state.catalog.asOf} · 本次目录获取 ${state.catalog.retrievedAt}` : '目录待生成',
        state.progress ? `进度更新 ${state.progress.updatedAt}` : '进度待生成'].join(' · ');
      const companies = companyCoverage(state.catalog, state.progress, $('company-search').value);
      $('catalog-status').textContent = state.directoryFailures.length ? `${state.directoryFailures.join('；')}。已有目录与进度保留原时点。` :
        `显示 ${companies.length} 家公司。点击公司名称查看当前已收录报告；“未取得”表示本次未完成报告或数据获取。`;
      if (freshness.staleCompanyCount) $('catalog-status').textContent += ` ${freshness.staleCompanyCount} 家公司保留旧核验归属，本次主目录日期不代表这些公司已重新核验。`;
      if (freshness.retainedHistoricFundCount) $('catalog-status').textContent += ` 另保留 ${formatNumber(freshness.retainedHistoricFundCount, 0)} 个历史产品代码，未因本次主目录缺少它们而删除。`;
      const container = $('company-coverage'); container.replaceChildren();
      if (!companies.length) {
        container.append(emptyState(state.catalog || state.progress ? '没有匹配的公司' : '全量目录待生成',
          state.catalog || state.progress ? '请尝试公司名称或目录代码；这项搜索覆盖全部已载入公司。' : '等待目录与进度文件生成。下方已有报告仍可独立查看，不代表全量公司已遍历。', true));
        return;
      }
      const table = el('table', 'company-table'); const thead = el('thead'); const head = el('tr');
      ['基金公司', '目录份额', '已取得记录', '有股票持仓', '无股票持仓', '季末后成立', '季末前已终止', '抓取后仍未取得', '待处理', '本批未轮到', '来源'].forEach(label => {
        const th = el('th', '', label); th.scope = 'col'; head.append(th);
      });
      thead.append(head); const tbody = el('tbody');
      for (const company of companies) {
        const tr = el('tr', inputs.company.value === `code:${company.code}` ? 'company-selected' : '');
        const identity = el('td'); const button = el('button', 'company-button', company.name); button.type = 'button';
        button.addEventListener('click', () => {
          inputs.company.value = `code:${company.code}`; inputs.query.value = ''; inputs.manager.value = ''; inputs.change.value = ''; inputs.period.value = 'latest';
          state.limit = 30; if (state.manifest) loadCompany(company.code, 0); else renderResults(); renderDirectory(); $('results').scrollIntoView({ behavior: 'auto', block: 'start' });
        });
        identity.append(button, el('span', 'company-code', company.code));
        identity.append(el('small', 'company-state', companyDirectoryLabel(company, state.catalog?.asOf)));
        if (company.lastAttemptAt) identity.append(el('small', '', `最近处理 ${company.lastAttemptAt}`));
        else identity.append(el('small', '', '尚无处理时点记录'));
        tr.append(identity);
        for (const key of ['totalFunds', 'processedFunds', 'withHoldings', 'withoutEquities', 'establishedAfterPeriod', 'terminatedBeforePeriod', 'failed', 'pending', 'deferredFunds']) {
          tr.append(el('td', key === 'failed' ? 'count-failed' : key === 'pending' ? 'count-pending' : '', company[key] === null ? '—' : formatNumber(company[key], 0)));
        }
        const source = el('td'); if (!addLink(source, '目录 ↗', company.sourceUrl)) source.textContent = '—'; tr.append(source); tbody.append(tr);
      }
      table.append(thead, tbody); container.append(table);
    }
    async function loadDirectory() {
      const request = ++state.directoryRequest;
      const tasks = [
        { key: 'catalog', path: periodDataPath(state.period, 'catalog.json'), label: '目录', validate: validateCatalog },
        { key: 'progress', path: periodDataPath(state.period, 'progress.json'), label: '进度', validate: value => {
          validateProgress(value);
          if (value.phase && (value.phase.targetPeriodEnd !== state.period.targetPeriodEnd || value.phase.baselinePeriodEnd !== state.period.baselinePeriodEnd)) throw new Error('PERIOD_MISMATCH');
          return value;
        } }
      ];
      const outcomes = await Promise.all(tasks.map(async task => {
        const controller = new win.AbortController(); const timer = win.setTimeout(() => controller.abort(), 20000);
        try {
          const response = await win.fetch(task.path, { cache: 'no-store', signal: controller.signal, credentials: 'omit' });
          if (!response.ok) throw new Error(response.status === 404 ? 'NOT_GENERATED' : 'LOAD_FAILED');
          const raw = await response.text(); if (raw.length > MAX_DATA_BYTES) throw new Error('INVALID_DATA');
          return { ...task, data: task.validate(JSON.parse(raw)) };
        } catch (error) { return { ...task, failure: `${task.label}${error.message === 'NOT_GENERATED' ? '待生成' : '载入未成功'}` }; }
        finally { win.clearTimeout(timer); }
      }));
      if (request !== state.directoryRequest) return;
      state.directoryFailures = [];
      for (const outcome of outcomes) {
        if (outcome.data) state[outcome.key] = outcome.data;
        else state.directoryFailures.push(outcome.failure);
      }
      renderDirectory(); renderResults();
    }
    function updateDataset() {
      const funds = state.data.funds;
      const unique = list => [...new Set(list.filter(Boolean))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
      updateCompanyOptions();
      selectOptions(inputs.manager, unique(funds.flatMap(managerNames)), [['', '全部经理']]);
      selectOptions(inputs.period, unique(funds.map(reportPeriod)).sort().reverse(), [['latest', '各基金最新报告'], ['', '全部已收录报告期']]);
      if (!state.dataUpdated) { inputs.period.value = 'latest'; state.dataUpdated = true; }
      const current = latestFunds(funds);
      const rows = current.flatMap(fund => fund.rows.filter(row => row.current));
      updateStatistics();
      if (!state.manifest) {
        $('stat-period').textContent = state.data.targetPeriodEnd || current.map(reportPeriod).filter(Boolean).sort().at(-1) || '暂无报告';
        $('stat-prices').textContent = `${rows.filter(row => priceDescription(row.priceRange).complete).length} / ${rows.length}`;
        $('stat-prices-note').textContent = `完整交易日区间 / 披露证券；${rows.filter(row => priceDescription(row.priceRange).observed).length} 条已有观测`;
      }
      const notes = $('coverage-notes'); notes.replaceChildren();
      const scope = el('p'); scope.append(el('strong', '', '覆盖范围：'), doc.createTextNode(state.data.coverage.scope)); notes.append(scope);
      for (const limitation of state.data.coverage.limitations) notes.append(el('p', '', limitation));
      if (state.data.errors.length) notes.append(el('p', '', `本次数据生成记录 ${state.data.errors.length} 项未完成项，已保留可用结果；缺失处未补值。`));
      state.limit = 30; renderResults();
    }
    function addMetadata(container, title, content) {
      const item = el('span'); item.append(doc.createTextNode(`${title} `), el('strong', '', content || '未提供')); container.append(item);
    }
    function addCell(row, value, sub, className) {
      const cell = el('td', className || '', value);
      if (sub) cell.append(el('span', 'cell-sub', sub));
      row.append(cell); return cell;
    }

    function holdingRow(row) {
      const tr = el('tr');
      const stock = el('td'); stock.append(el('span', 'stock-name', row.stockName || row.stockCode), el('span', 'cell-sub', `${row.stockCode} · ${row.market || '市场未提供'}`)); tr.append(stock);
      const change = el('td'); const pill = el('span', `change-pill ${changeGroup(row)}`, row.change.label || '无法比较');
      if (row.change.reason) pill.title = row.change.reason;
      change.append(pill, el('span', 'cell-sub', signed(row.change.sharesDelta, ' 股'))); tr.append(change);
      addCell(tr, holdingValue(row.current, 'shares'), `上期 ${holdingValue(row.previous, 'shares')}`, 'number');
      addCell(tr, holdingValue(row.current, 'weightPct', '%'), `上期 ${holdingValue(row.previous, 'weightPct', '%')} · ${signed(row.change.weightDeltaPp, ' pp')}`, 'number');
      addCell(tr, formatMoney(row.position.disclosedYuan), '本期报告披露 · 人民币', 'number');
      addCell(tr, formatMoney(row.position.estimatedYuan), row.position.estimateNote || '期末净资产 × 披露占比', 'number');
      const price = priceDescription(row.priceRange);
      const priceCell = el('td'); priceCell.append(el('span', price.observed ? 'price-value' : 'missing', price.value),
        el('span', 'cell-sub', `窗口 ${row.priceRange.rangeStart || '未提供'} → ${row.priceRange.rangeEnd || '未提供'}`), el('span', 'cell-sub', price.note));
      if (price.observed) priceCell.append(el('span', 'cell-sub', `口径 ${row.priceRange.priceBasis || '未提供'} · ${price.conflictPending ? '来源冲突待核验' : price.complete ? '已核对完整交易日' : '区间极值尚未完整'} · 非实际持仓成本`));
      if (row.priceRange.fetchedAt) priceCell.append(el('span', 'cell-sub', `${row.priceRange.dataStatus === 'cached' ? '保留行情 · ' : ''}采集 ${row.priceRange.fetchedAt}`));
      const priceSource = el('span', 'cell-sub'); if (addLink(priceSource, '行情来源 ↗', row.priceRange.sourceUrl)) priceCell.append(priceSource);
      for (const [index, source] of priceConflictSources(row.priceRange).entries()) {
        const comparison = el('span', 'cell-sub', `待核验对照 ${index + 1}：${source.interval} · 采集 ${source.fetchedAt} `);
        addLink(comparison, '来源 ↗', source.sourceUrl); priceCell.append(comparison);
      }
      tr.append(priceCell); return tr;
    }

    function fundCard(fund, open) {
      const card = el('details', 'fund-card'); card.open = open;
      const summary = el('summary');
      const heading = el('div');
      const title = el('div', 'fund-summary-title'); title.append(el('h3', '', fund.name || '名称未提供'), el('span', 'fund-code', fund.code));
      if (fund.status === 'unavailable') title.append(el('span', 'badge warning', fund.notApplicableEvidence
        ? (fund.notApplicableEvidence.reason === 'TERMINATED_BEFORE_PERIOD' ? '不适用：季末前已终止' : '不适用：季末后成立')
        : fund.selectedReport ? '持仓解析待补' : '报告暂缺'));
      if (fund.status === 'no-equities') title.append(el('span', 'badge success',
        ['zero-stock-balance-sheet', 'exhaustive-nonstock-assets'].includes((fund.noEquitiesEvidence || fund.selectedReport?.noEquitiesEvidence)?.evidenceType)
          ? '期末股票账面金额为零' : '报告确认期末未持股'));
      if (fund.dataStatus === 'cached') title.append(el('span', 'badge warning', '保留的有效记录'));
      heading.append(title);
      const description = el('div', 'fund-summary-description');
      description.append(el('span', '', fund.company || '公司未提供'), el('span', '', `持仓期 ${reportPeriod(fund) || '未提供'}`),
        el('span', '', `对比期 ${fund.previousReport?.periodEnd || '无可用基期'}`));
      heading.append(description);
      const count = el('div', 'fund-summary-count'); const positions = el('span', 'value-count');
      positions.append(el('strong', '', formatMoney(fund.summary?.knownPositionYuan)), doc.createTextNode('已知持仓'));
      const rows = el('span'); rows.append(el('strong', '', String(fund.visibleRows.length)), doc.createTextNode('条'));
      const chevron = el('span', 'chevron'); chevron.setAttribute('aria-hidden', 'true'); count.append(positions, rows, chevron);
      summary.append(heading, count); card.append(summary);
      const body = el('div', 'fund-body');
      const metadata = el('div', 'fund-metadata');
      const companyIndex = catalogIndex(state.catalog);
      const directoryCompany = companyIndex.companies.get(companyCodeFor(fund, companyIndex));
      if (directoryCompany?.directoryStatus === 'stale') addMetadata(metadata, '机构归属', companyDirectoryLabel(directoryCompany));
      if (fund.asOf) addMetadata(metadata, '该基金观察截至', fund.asOf);
      if (fund.retrievedAt) addMetadata(metadata, '该基金采集', fund.retrievedAt);
      if (validTimestamp(fund.priceUpdatedAt)) addMetadata(metadata, '行情单独更新', fund.priceUpdatedAt);
      if (validTimestamp(fund.selectedReport?.strategyEvidenceRetrievedAt)) addMetadata(metadata, '策略原文采集', fund.selectedReport.strategyEvidenceRetrievedAt);
      addMetadata(metadata, '期末净资产', formatMoney(fund.selectedReport?.navYuan));
      addMetadata(metadata, '公告日', fund.selectedReport?.publishedAt);
      addMetadata(metadata, '报告范围', { top10: '前十大重仓股', full: '完整持仓', unknown: '未提供' }[fund.selectedReport?.coverage]);
      addMetadata(metadata, '经理归属观测日', fund.managerEvidenceRetrievedAt || fund.managersAsOf || fund.managerObservedAt || fund.managerAsOf || fund.managerSource?.observedAt);
      addMetadata(metadata, '经理归属口径', `${managerAttributionLabel(fund)}${managerAttributionLabel(fund) === '当前经理概况' ? '，历史归属须查对应报告' : ''}`);
      if (fund.interval) addMetadata(metadata, '比较窗口', fund.interval.label || `${fund.interval.start || '—'} → ${fund.interval.end || '—'}`);
      if (fund.shareClassCodes?.length > 1) addMetadata(metadata, '份额类别', fund.shareClassCodes.join(' / '));
      addLink(metadata, '本期原报告 ↗', fund.selectedReport?.sourceUrl); addLink(metadata, '上期原报告 ↗', fund.previousReport?.sourceUrl);
      addLink(metadata, '持仓数据来源 ↗', fund.selectedReport?.holdingsSourceUrl); addLink(metadata, '净资产数据来源 ↗', fund.selectedReport?.navSourceUrl);
      addLink(metadata, '经理归属来源 ↗', fund.managerSourceUrl || fund.metadataSourceUrl);
      body.append(metadata);
      if (fund.managerAttribution === 'report' && fund.reportManagers?.length) {
        const appointments = el('div', 'fund-metadata');
        for (const manager of fund.reportManagers) {
          addMetadata(appointments, typeof manager === 'string' ? manager : manager.name, managerTermLabel(manager, fund.selectedReport?.periodEnd));
          if (manager?.evidence?.page) addLink(appointments, `经理表第 ${manager.evidence.page} 页 ↗`, manager.evidence.sourceUrl);
        }
        body.append(appointments, el('p', 'narrative-meta', '名单覆盖报告期内任职；期内离任者不自动归为期末组合管理人。'));
      }
      const quality = fundQuality(state.progress, fund);
      if (quality?.fields) {
        const labels = { holdings: '持仓披露表', pdf: 'PDF', strategy: '策略章节', nav: '净资产', baseline: '上期基准', prices: '行情', managers: '报告经理' };
        const statuses = { complete: '齐全', missing: '缺失', partial: '部分', truncated: '截断', 'wrong-period': '期间不符', 'not-applicable': '不适用' };
        const qualityRow = el('div', 'fund-metadata research-fields');
        for (const [key, label] of Object.entries(labels)) addMetadata(qualityRow, label, statuses[quality.fields[key]] || '待核验');
        body.append(qualityRow);
      }
      const warnings = [...(fund.warnings || [])];
      if (fund.dataStatus === 'cached') warnings.unshift('本轮未取得完整更新，继续保留这只基金之前的有效记录；观测截止与采集时间见上方。');
      if (fund.interval?.adjacent === false) warnings.unshift('比较报告期不相邻；持仓差异不能视为单季交易。');
      if (warnings.length) { const alerts = el('div', 'fund-alerts'); warnings.forEach(message => alerts.append(el('p', '', message))); body.append(alerts); }
      if (fund.notApplicableEvidence) {
        const evidence = fund.notApplicableEvidence;
        const note = el('div', 'narrative');
        if (evidence.reason === 'TERMINATED_BEFORE_PERIOD') {
          note.append(el('p', 'narrative-text', `公告确认基金合同终止，${evidence.dateBasis === 'lastOperationDate' ? '最后运作日' : '明确终止日'}为 ${evidence.date}，早于目标季末 ${evidence.periodEnd}。基金仍保留在目录中；季度内可能曾持仓，不将其记为零持股或研究已完成。`));
          addMetadata(note, '公告日', evidence.publishedAt);
          addMetadata(note, '最后运作日', evidence.lastOperationDate);
          addMetadata(note, '清算开始日', evidence.liquidationStartDate);
          addMetadata(note, '清算报告期结束日', evidence.finalLiquidationEnd);
          for (const citation of evidence.evidence.filter(item => item.field !== 'identityFundCodes')) {
            note.append(el('p', 'narrative-text', citation.quote), el('p', 'narrative-meta', `原 PDF 第 ${citation.page} 页`));
          }
          addLink(note, '终止／清算公告 ↗', `${evidence.sourceUrl}#page=${evidence.page}`);
        } else {
          note.append(el('p', 'narrative-text', `公开成立日期为 ${evidence.date}，晚于本季度期末 ${evidence.periodEnd}，本季度不适用。不将其记为无股票持仓或已完成研究。`));
          addLink(note, '成立日期来源 ↗', evidence.sourceUrl);
        }
        body.append(note);
      } else if (fund.status === 'no-equities') {
        const evidence = fund.noEquitiesEvidence || fund.selectedReport?.noEquitiesEvidence;
        const bookZero = ['zero-stock-balance-sheet', 'exhaustive-nonstock-assets'].includes(evidence?.evidenceType);
        const note = emptyState(bookZero ? '报告期末股票投资账面金额为零' : '报告确认本期期末未持有股票',
          evidence?.evidenceType === 'exhaustive-nonstock-assets'
            ? '期末资产组合和其他资产明细逐项按分核对至合计，非股票项目解释全部资产；结论仅对应本期期末股票投资账面金额。'
            : evidence?.text || '按报告证据记录本期期末无股票持仓，不按基金类型推断。', true);
        if (evidence?.evidenceType === 'exhaustive-nonstock-assets') {
          for (const table of [evidence.assetTable, evidence.otherAssetsTable]) {
            note.append(el('p', 'narrative-meta', `原 PDF 第 ${table.pageNumbers.join('、')} 页 · ${table.heading}`),
              el('p', 'narrative-text', table.text));
            addLink(note, '查看原表 ↗', `${evidence.sourceUrl}#page=${table.startPage}`);
          }
        } else if (evidence?.evidenceType === 'empty-stock-detail-section') {
          note.append(el('p', 'narrative-meta', `同一物理页第 ${evidence.page} 页 · 股票明细原文与资产组合表共同核验`),
            el('p', 'narrative-text', evidence.assetAllocationText));
        } else if (evidence?.page) note.append(el('p', 'narrative-meta', `原报告第 ${evidence.page} 页`));
        addLink(note, '无股票持仓披露来源 ↗', evidence?.sourceUrl || fund.selectedReport?.sourceUrl); body.append(note);
      }
      if (!fund.notApplicableEvidence && !fund.visibleRows.length && fund.status !== 'no-equities') body.append(emptyState('暂无可展示持仓', '这只基金尚无可用的持仓披露明细；报告与经理信息仅按已提供字段展示。', true));
      else if (!fund.notApplicableEvidence && fund.visibleRows.length) {
        if (fund.status === 'no-equities') body.append(el('p', 'narrative-meta', '下表保留上期披露股票及本期未见明细的状态，不据此确定卖出时间或交易原因。'));
        const scroll = el('div', 'table-scroll'); scroll.tabIndex = 0; scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', `${fund.name}持仓表，可横向滚动`);
        const table = el('table', 'holdings-table'); const thead = el('thead'); const head = el('tr');
        const labels = ['证券', '披露变化 / 股数差', '本期 / 上期持股数', '净资产占比 / 变动', '本期披露市值', '持仓体量估算', '期间最低 — 最高价 · 非成本'];
        labels.forEach((label, index) => { const th = el('th', index >= 2 && index <= 5 ? 'number' : '', label); th.scope = 'col'; head.append(th); });
        thead.append(head); const tbody = el('tbody'); fund.visibleRows.forEach(row => tbody.append(holdingRow(row))); table.append(thead, tbody); scroll.append(table); body.append(scroll);
      }
      if (fund.notApplicableEvidence) { card.append(body); return card; }
      const narrative = el('section', 'narrative'); const narrativeTitle = el('div', 'narrative-title'); narrativeTitle.append(el('h4', '', '报告中的运作说明'));
      addLink(narrativeTitle, '阅读说明来源 ↗', fund.narrative?.sourceUrl); narrative.append(narrativeTitle);
      if (fund.narrative?.text) {
        const period = `${fund.narrative.periodStart || '起始日未提供'} → ${fund.narrative.periodEnd || '结束日未提供'}`;
        const mismatch = fund.narrative.periodEnd && reportPeriod(fund) && fund.narrative.periodEnd !== reportPeriod(fund);
        narrative.append(el('p', 'narrative-meta', `说明期间 ${period}${fund.narrative.page ? ` · 第 ${fund.narrative.page} 页` : ' · 页码未核验'}${mismatch ? ' · 与当前持仓期不同' : ''}`));
        narrative.append(el('p', 'narrative-text', fund.narrative.text));
        const selectionNote = { 'operation-sentences': '按运作相关关键词选取原文句子。', 'section-opening': '摘录运作分析章节开头。' }[fund.narrative.excerptSelection] || '';
        narrative.append(el('p', 'narrative-note', `${selectionNote}${fund.narrative.truncated ? '当前显示短摘录，请前往来源阅读完整内容。' : ''}报告层面的原文摘录；不能据此确定每只证券的买卖动机。`));
      } else narrative.append(el('p', 'narrative-meta', '尚未提取到可核验的经理运作说明，请查看原报告；不自动推断换仓逻辑。'));
      body.append(narrative);
      const evidence = fund.selectedReport || {};
      const structuredStrategy = strategyEvidence(evidence, state.data?.asOf, state.data?.retrievedAt);
      const excerpts = structuredStrategy.analysis?.evidence.length ? structuredStrategy.analysis.evidence
        : Array.isArray(evidence.strategyExcerpts) ? evidence.strategyExcerpts : [];
      const themes = Array.isArray(evidence.strategyThemes) ? evidence.strategyThemes : [];
      if (evidence.parseMethod || excerpts.length || themes.length || structuredStrategy.proof) {
        const strategy = el('section', 'narrative strategy-evidence');
        const strategyTitle = el('div', 'narrative-title');
        strategyTitle.append(el('h4', '', '报告解析出的策略要点'));
        strategy.append(strategyTitle);
        strategy.append(el('p', 'narrative-meta', structuredStrategy.label));
        if (structuredStrategy.proof) addLink(strategy, '策略章节原文 ↗', structuredStrategy.proof.sourceUrl);
        if (structuredStrategy.analysis) {
          const analysis = structuredStrategy.analysis;
          strategy.append(el('p', 'narrative-meta', `原章节 ${analysis.sentenceCount} 句 · 展示 ${analysis.selectedSentenceCount} 条短引文 · 其余 ${analysis.omittedSentenceCount} 句请读原报告。分类可重叠，按原文措辞归类，不代表全文语义分析已完成。`));
          const counts = Object.entries(analysis.categorySentenceCounts).map(([key, value]) => `${STRATEGY_CATEGORIES[key]} ${value} 句`);
          strategy.append(el('p', 'narrative-meta', counts.join(' · ')));
        }
        if (evidence.parseMethod) {
          const methodLabels = { mineru: 'MinerU', pypdf: 'pypdf', 'pypdf-fallback': 'pypdf 回退' };
          const method = methodLabels[evidence.parseMethod] || evidence.parseMethod;
          const suffix = evidence.parseErrorCode ? ` · MinerU 回退码：${evidence.parseErrorCode}` : '';
          strategy.append(el('p', 'narrative-meta', `PDF 解析方式：${method}${suffix}`));
          const pages = evidence.pageStats;
          if (pages) {
            const total = countValue(pages.totalPages) ? String(pages.totalPages) : '未核验';
            const processed = countValue(pages.processedPages) ? String(pages.processedPages) : '未核验';
            strategy.append(el('p', 'narrative-meta', `已处理页数 ${processed} / 原文件页数 ${total}${pages.truncated === true ? ' · 文本被截断，需继续解析' : ''}${pages.textStatus === 'sparse' ? ' · 文本不足，需补解析' : ''}`));
          }
        }
        if (!structuredStrategy.analysis && themes.length) strategy.append(el('p', 'narrative-meta', `主题：${themes.join(' · ')}`));
        excerpts.forEach(item => {
          const row = el('p', 'narrative-text');
          if (item.categories) strategy.append(el('p', 'narrative-meta', item.categories.map(key => STRATEGY_CATEGORIES[key]).filter(Boolean).join(' · ')));
          row.append(doc.createTextNode(item.text));
          row.append(doc.createTextNode(item.page ? `（第 ${item.page} 页）` : '（页码未核验）'));
          const source = el('span', 'cell-sub');
          if (addLink(source, '原文来源 ↗', item.sourceUrl)) row.append(doc.createTextNode(' '), source);
          strategy.append(row);
          if (item.quoteTruncated) strategy.append(el('p', 'narrative-meta', '该句仅展示短引文，完整表述见原报告。'));
        });
        strategy.append(el('p', 'narrative-note', '以上为报告原文摘录与规则主题归类；不把报告层面的观点自动解释为单只证券的交易动机。'));
        body.append(strategy);
      }
      card.append(body); return card;
    }

    function updatePagination() {
      const pages = companyPages(state.manifest, state.companyCode);
      $('company-navigation').hidden = !state.manifest || !state.companyCode;
      $('browse-companies').hidden = !state.manifest || !state.companyCode;
      $('company-page-status').textContent = pages.length ? `${pages[0].companyName} · 第 ${state.pageIndex + 1} / ${pages.length} 页 · 公司共 ${pages[0].companyFundCount ?? pages.reduce((sum, page) => sum + page.fundCount, 0)} 份记录${state.loadingCompany ? ' · 正在载入' : ''}` : '这家公司尚无可载入记录，处理状态保留在上方目录中';
      $('company-prev').disabled = state.loadingCompany || state.pageIndex <= 0;
      $('company-next').disabled = state.loadingCompany || state.pageIndex + 1 >= pages.length;
    }
    async function loadCompany(code, pageIndex = 0) {
      const request = ++state.companyRequest;
      const period = state.period;
      const samePage = state.companyCode === code && state.pageIndex === pageIndex;
      const pages = companyPages(state.manifest, code);
      state.companyCode = code; state.pageIndex = pageIndex; state.loadingCompany = Boolean(pages[pageIndex]);
      if (!samePage || !pages[pageIndex]) state.data = null;
      inputs.company.value = code ? `code:${code}` : '';
      updatePagination(); renderResults();
      if (!code) { updateObservation('当前显示本季度全部已收录公司；点击公司查看记录。目录规模不代表采集完成。'); return; }
      if (!pages[pageIndex]) return;
      const controller = new win.AbortController(); const timer = win.setTimeout(() => controller.abort(), 20000);
      try {
        const path = periodDataPath(period, pages[pageIndex].path);
        const response = await win.fetch(path, { cache: 'no-store', credentials: 'omit', signal: controller.signal });
        if (!response.ok) throw new Error('COMPANY_LOAD_FAILED');
        const raw = await responseText(response, path, win);
        const data = validatePeriodDataset(JSON.parse(raw), period);
        if (request !== state.companyRequest) return;
        state.data = { ...data, funds: data.funds.map(fund => ({ ...fund, companyCode: code })) };
        state.loadingCompany = false; state.failed = false;
        updateDataset(); updatePagination();
        updateObservation(`按公司逐家查看：${pages[pageIndex].companyName}，本页 ${data.funds.length} 份记录；搜索与导出限当前页。`);
      } catch (_) {
        if (request !== state.companyRequest) return;
        state.loadingCompany = false; state.failed = true;
        updateObservation(`该公司本页未能载入${state.data ? '；保留本页原有数据与原始日期。' : '；未显示其他公司的记录。'}`);
        renderResults(); updatePagination();
      } finally { win.clearTimeout(timer); }
    }
    function renderResults() {
      const overviewMode = Boolean(state.manifest && !state.companyCode);
      $('results-heading').textContent = overviewMode ? '本季度已收录公司总览' : '经理与持仓明细';
      $('filter-tip').textContent = overviewMode ? '总览可按公司、经理或基金目录名称 / 代码定位公司。基金目录命中不代表该基金报告已取得；进入公司后可搜索股票与导出当前页。' : '当前只显示所选公司的一页记录；搜索与导出限当前页。点击「全部已收录公司」返回本季度总览。';
      inputs.query.placeholder = overviewMode ? '查找公司、经理或基金目录名称 / 代码' : '搜索当前公司页的基金、经理或股票';
      for (const key of ['manager', 'period', 'change']) inputs[key].disabled = overviewMode;
      if (overviewMode) {
        state.filtered = []; $('export-csv').disabled = true;
        const all = companyRecordSummaries(state.manifest);
        const companies = companyRecordSummaries(state.manifest, state.catalog, inputs.query.value);
        $('result-count').textContent = `${companies.length} / ${all.length} 家已收录公司 · 全季 ${formatNumber(state.manifest.coverage.fundCount, 0)} 份记录`;
        const target = $('fund-results'); target.replaceChildren();
        target.append(el('p', 'browse-note', '以下是本季度实际已收录的公司与记录，包含字段待补的报告；其余公司的采集状态见上方覆盖清单。点击公司进入基金明细。'));
        if (!companies.length) { target.append(emptyState('未找到有已收录记录的匹配公司', '可在上方全量公司清单查询尚未取得报告的公司；未取得的数据不会补成空持仓。')); return; }
        const grid = el('div', 'company-record-grid');
        for (const company of companies) {
          const button = el('button', 'company-record-card'); button.type = 'button';
          button.append(el('strong', '', company.name), el('span', '', `${formatNumber(company.records, 0)} 份记录 · 本期已解析 ${formatNumber(company.holdings, 0)} 条持仓`), el('small', '', `${company.pages} 页 · 查看基金明细 →`));
          button.addEventListener('click', () => { inputs.query.value = ''; inputs.manager.value = ''; inputs.change.value = ''; inputs.period.value = 'latest'; loadCompany(company.code, 0); renderDirectory(); });
          grid.append(button);
        }
        target.append(grid); return;
      }
      if (state.loadingCompany) {
        $('fund-results').replaceChildren(emptyState('正在载入所选公司的当前页', '每次只载入一页记录；其他公司继续保留在覆盖目录中。'));
        $('export-csv').disabled = true; $('result-count').textContent = '正在载入'; return;
      }
      if (!state.data) {
        if (state.manifest) {
          $('fund-results').replaceChildren(emptyState(state.failed ? '这家公司本页暂未载入' : '所选公司尚无已收录记录', '可在上方目录查看待处理、无股票持仓与报告未取得数量；不会改为显示其他公司。'));
          $('result-count').textContent = '当前公司 0 份已载入记录'; $('export-csv').disabled = true;
        }
        return;
      }
      const filters = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value]));
      state.filtered = filterFunds(state.data.funds, filters, state.catalog);
      const rowCount = state.filtered.reduce((sum, fund) => sum + fund.visibleRows.length, 0);
      $('result-count').textContent = `${state.manifest ? '当前公司页：' : ''}${new Set(state.filtered.map(fund => fund.code)).size} 只基金 · ${state.filtered.length} 份记录 · ${rowCount} 条披露对比`;
      $('export-csv').disabled = rowCount === 0;
      const target = $('fund-results'); target.replaceChildren();
      if (!state.filtered.length) {
        target.append(emptyState(state.data.funds.length ? '没有匹配的已收录记录' : '尚未生成可用的基金记录',
          state.data.funds.length ? '尝试减少筛选条件，或检查基金、经理与股票代码。新代码需要先由数据生成流程收录。' : '公开数据文件已载入，但目前没有基金记录。可在数据生成完成后重新载入，或导入已有的有效结果 JSON。'));
        return;
      }
      const groups = new Map();
      state.filtered.slice(0, state.limit).forEach(fund => {
        const label = managerNames(fund).join(' / ') || '经理信息暂缺';
        const attribution = managerAttributionLabel(fund);
        const key = `${fund.company}\u0000${label}\u0000${attribution}`;
        if (!groups.has(key)) groups.set(key, { label, company: fund.company, attribution, funds: [] });
        groups.get(key).funds.push(fund);
      });
      let cardIndex = 0;
      for (const group of groups.values()) {
        const section = el('section', 'manager-group'); const heading = el('div', 'manager-title');
        const avatar = el('span', 'manager-avatar', group.label.slice(0, 1)); avatar.setAttribute('aria-hidden', 'true');
        heading.append(avatar, el('h3', '', group.label), el('span', '', `${group.attribution} · ${group.company || '公司未提供'} · ${group.funds.length} 份基金记录`)); section.append(heading);
        group.funds.forEach(fund => section.append(fundCard(fund, cardIndex++ === 0))); target.append(section);
      }
      if (state.filtered.length > state.limit) {
        const more = el('button', 'button button-light', `继续显示（还有 ${state.filtered.length - state.limit} 份记录）`);
        more.type = 'button'; more.addEventListener('click', () => { state.limit += 30; renderResults(); }); target.append(more);
      }
    }

    async function reload() {
      if (!state.periodsReady) return;
      if (!state.period) return initializePeriods();
      const request = ++state.request;
      const period = state.period;
      win.RecordAnalysis?.loadQuarter(period);
      loadDirectory();
      const controller = new win.AbortController();
      const timer = win.setTimeout(() => controller.abort(), 20000);
      $('reload').disabled = true;
      updateObservation(state.data ? '正在重载；当前数据与原始观测日期继续保留。' : '正在读取公开数据文件…');
      try {
        const response = await win.fetch(periodDataPath(period, 'latest.json'), { cache: 'no-store', signal: controller.signal, credentials: 'omit' });
        if (!response.ok) throw new Error(response.status === 404 ? 'NOT_GENERATED' : 'LOAD_FAILED');
        const raw = await response.text();
        if (raw.length > MAX_DATA_BYTES) throw new Error('INVALID_DATASET');
        const data = validatePeriodDataset(JSON.parse(raw), period);
        if (request !== state.request) return;
        if (state.data?.funds.length && !data.funds.length && !data.companyFiles?.length) throw new Error('EMPTY_REPLACEMENT');
        state.failed = false; state.mode = 'remote'; updateQuarterHeading();
        if (data.companyFiles?.length) {
          state.manifest = data; updateCompanyOptions(); updateStatistics();
          const code = inputs.company.value.startsWith('code:') ? inputs.company.value.slice(5) : '';
          await loadCompany(code, 0);
        } else {
          state.manifest = null; state.data = data; updateDataset(); updatePagination();
          updateObservation(data.coverage.status === 'awaiting-data' ? `${period.label} 已建立采集任务，正在等待首份有效基金记录；目录与进度独立显示。` :
            `已载入 ${new Set(data.funds.map(fund => fund.code)).size} 只基金；重新载入只读取最新已生成结果。`);
        }
      } catch (error) {
        if (request !== state.request) return;
        state.failed = true;
        const reason = error.message === 'PERIOD_MISMATCH' ? '返回数据与所选季度不符，未载入' : error.message === 'NOT_GENERATED' ? `${period.label} 公开数据尚未生成` : error.message === 'EMPTY_REPLACEMENT' ? '新结果为空，未替换现有记录' : error.message === 'INVALID_DATASET' || error instanceof SyntaxError ? '数据格式不符合约定' : '公开数据未能载入';
        updateObservation(`${reason}${state.data ? '；继续显示原有数据与观测日期。' : '。可稍后重载，或导入有效结果 JSON。'}`);
        if (!state.data) { $('fund-results').replaceChildren(emptyState(`${period.label} 尚无可用数据`, '等待本季度公开数据生成后点击「重新载入」。其他季度可在顶部选择，不会自动代替当前季度。')); $('result-count').textContent = '本季度尚未载入'; }
      } finally {
        win.clearTimeout(timer);
        if (request === state.request) $('reload').disabled = false;
      }
    }

    $('reload').addEventListener('click', reload);
    $('quarter-select').addEventListener('change', event => chooseQuarter(event.target.value));
    for (const [key, input] of Object.entries(inputs)) input.addEventListener(key === 'query' ? 'input' : 'change', () => {
      state.limit = 30;
      if (key === 'company' && state.manifest) { inputs.query.value = ''; inputs.manager.value = ''; inputs.change.value = ''; loadCompany(input.value.replace(/^code:/, ''), 0); }
      else renderResults();
    });
    $('company-search').addEventListener('input', renderDirectory);
    $('browse-companies').addEventListener('click', () => { inputs.query.value = ''; inputs.manager.value = ''; inputs.change.value = ''; inputs.period.value = 'latest'; loadCompany('', 0); renderDirectory(); });
    $('company-prev').addEventListener('click', () => loadCompany(state.companyCode, state.pageIndex - 1));
    $('company-next').addEventListener('click', () => loadCompany(state.companyCode, state.pageIndex + 1));
    $('reset-filters').addEventListener('click', () => { Object.entries(inputs).forEach(([key, input]) => { if (key !== 'company' || !state.manifest) input.value = key === 'period' ? 'latest' : ''; }); state.limit = 30; renderResults(); });
    $('import-file').addEventListener('change', async event => {
      const file = event.target.files[0]; if (!file) return;
      const request = ++state.request;
      try {
        if (file.size > MAX_DATA_BYTES) throw new Error('INVALID_DATASET');
        const data = validateDataset(JSON.parse(await file.text()));
        if (request !== state.request) return;
        if (state.data?.funds.length && !data.funds.length) throw new Error('EMPTY_REPLACEMENT');
        state.companyRequest++; state.directoryRequest++; state.catalog = null; state.progress = null; state.directoryFailures = [];
        state.manifest = null; state.loadingCompany = false; state.data = data; state.mode = 'imported'; state.failed = false; updateDataset(); updatePagination(); renderDirectory();
        win.RecordAnalysis?.reset('当前为本地导入记录，未附带跨基金汇总分析。');
        $('quarter-title').textContent = '本地导入'; $('quarter-description').textContent = `导入报告期 ${data.targetPeriodEnd || data.coverage.targetPeriodEnd || '见各基金记录'}，观察截至 ${data.asOf}。`;
        updateObservation('正在查看本地导入结果；文件不会上传，原始报告期与采集日期保持不变。');
      } catch (_) {
        if (request !== state.request) return;
        state.failed = true; updateObservation(`导入失败：文件格式无效、过大或没有可替换记录${state.data ? '；原有数据与观测日期已保留。' : '。请使用数据流程生成的结果 JSON。'}`);
      } finally { event.target.value = ''; if (request === state.request) $('reload').disabled = false; }
    });
    $('export-csv').addEventListener('click', () => {
      if (!state.data || !state.filtered.length) return;
      const blob = new win.Blob([exportCsv(state.filtered)], { type: 'text/csv;charset=utf-8' });
      const url = win.URL.createObjectURL(blob); const link = el('a');
      link.href = url; link.download = `fund-holdings-${state.data.asOf}.csv`;
      doc.body.append(link); link.click(); link.remove(); win.setTimeout(() => win.URL.revokeObjectURL(url), 1000);
    });
    doc.addEventListener('keydown', event => {
      if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !['INPUT', 'TEXTAREA', 'SELECT'].includes(doc.activeElement?.tagName) && !doc.activeElement?.isContentEditable) {
        event.preventDefault(); inputs.query.focus();
      }
    });
    initializePeriods();
  }

  return { validateDataset, validateCatalog, validateProgress, validatePeriods, selectQuarter, periodDataPath, validatePeriodDataset, responseText, researchOverview, notApplicableBreakdown, retryState, formatRetryAt, fundQuality, catalogComplete, catalogFreshness, companyDirectoryLabel, safeCompanyPath, companyPages, companyRecordSummaries, catalogIndex, companyCoverage, coverageOverview, safeHttpUrl, validEmptyStockEvidence, managerNames, managerAttributionLabel, managerTermLabel, reportPeriod, changeGroup, latestFunds, filterFunds,
    strategyEvidence, universeDescription, formatNumber, formatMoney, signed, holdingValue, priceDescription, priceConflictSources, csvCell, exportCsv, observationState, mount };
});
