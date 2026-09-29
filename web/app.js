(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) api.mount(root.document, root);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
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
  const MAX_DATA_BYTES = 80 * 1024 * 1024;
  function safeCompanyPath(path) { return typeof path === 'string' && /^\.\/data\/companies\/[A-Za-z0-9_-]+\.json$/.test(path) ? path : null; }
  function companyPages(manifest, code) { return (manifest?.companyFiles || []).filter(file => file.companyCode === code).slice().sort((a, b) => (a.partIndex || 1) - (b.partIndex || 1)); }
  function catalogComplete(catalog) { return Boolean(catalog && catalog.coverage.complete !== false && catalog.coverage.directoryComplete !== false && !['bootstrap', 'partial-directory', 'limited-probe', 'unavailable'].includes(catalog.coverage.status)); }

  function safeHttpUrl(value) {
    if (typeof value !== 'string' || /[\u0000-\u001f\u007f]/.test(value)) return null;
    try {
      const url = new URL(value);
      const credentialQuery = [...url.searchParams.keys()].some(key => /(?:token|secret|password|passwd|authorization|signature|credential|api[-_]?key|^key$|^auth$)/i.test(key));
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password && !credentialQuery ? url.href : null;
    } catch (_) { return null; }
  }

  function validateDataset(data) {
    function fail() { throw new Error('INVALID_DATASET'); }
    if (!isRecord(data) || data.schemaVersion !== 1 || !dateOnly(data.asOf) || !validTimestamp(data.retrievedAt) ||
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
      if (fund.status === 'no-equities') {
        const evidence = fund.noEquitiesEvidence || fund.selectedReport?.noEquitiesEvidence;
        if (!isRecord(evidence) || evidence.confirmed !== true || !safeHttpUrl(evidence.sourceUrl) || evidence.periodEnd !== fund.selectedReport.periodEnd || fund.rows.length) fail();
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
      codes.add(company.code);
    }
    const fundCodes = new Set();
    for (const fund of data.funds) {
      if (!isRecord(fund) || typeof fund.code !== 'string' || !/^\d{6}$/.test(fund.code) || typeof fund.name !== 'string' ||
        !nullableText(fund.companyCode) || !nullableText(fund.companyName) || (fund.companyCode !== null && !codes.has(fund.companyCode)) || fundCodes.has(fund.code)) fail();
      fundCodes.add(fund.code);
    }
    if (!countValue(data.coverage.companyCount) || !countValue(data.coverage.fundCount)) fail();
    return data;
  }

  function validateProgress(data) {
    const fail = () => { throw new Error('INVALID_PROGRESS'); };
    if (!isRecord(data) || data.schemaVersion !== 1 || !validTimestamp(data.updatedAt) || !Array.isArray(data.companies) || !Array.isArray(data.funds) ||
      ['totalCompanies', 'totalFunds', 'processedFunds', 'completedCompanies', 'pendingFunds'].some(key => !countValue(data[key]))) fail();
    if (data.completedCompanies > data.totalCompanies || data.processedFunds > data.totalFunds || data.pendingFunds > data.totalFunds) fail();
    const companies = new Set();
    for (const company of data.companies) {
      if (!isRecord(company) || typeof company.code !== 'string' || typeof company.name !== 'string' ||
        !(company.totalFunds === null || countValue(company.totalFunds)) || ['processedFunds', 'withHoldings', 'withoutEquities', 'failed', 'pending'].some(key => !countValue(company[key])) || companies.has(company.code)) fail();
      companies.add(company.code);
    }
    const funds = new Set();
    for (const fund of data.funds) {
      if (!isRecord(fund) || typeof fund.code !== 'string' || !nullableText(fund.companyCode) ||
        !['complete', 'no-equities', 'failed', 'pending'].includes(fund.status) || funds.has(fund.code)) fail();
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
      return { ...company, totalFunds: company.fundCount ?? company.totalFunds ?? null,
        processedFunds: entry?.processedFunds ?? null, withHoldings: entry?.withHoldings ?? null,
        withoutEquities: entry?.withoutEquities ?? null, failed: entry?.failed ?? null, pending: entry?.pending ?? null,
        lastAttemptAt: entry?.lastAttemptAt || null };
    });
  }

  function managerNames(fund) {
    const managers = ['report', 'tenure-overlap'].includes(fund.managerAttribution) && fund.reportManagers?.length ? fund.reportManagers : fund.managers;
    return managers.map(manager => typeof manager === 'string' ? manager : manager.name).filter(Boolean);
  }
  function managerAttributionLabel(fund) {
    if (fund.managerAttribution === 'report' && fund.reportManagers?.length) return '报告披露经理';
    if (fund.managerAttribution === 'tenure-overlap' && fund.reportManagers?.length) return '任期覆盖报告期';
    return '当前经理概况';
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
    return { value: `${formatNumber(range.low, 3)} – ${formatNumber(range.high, 3)} ${textValue(range.currency)}`.trim(),
      note: `${range.observedStart || '日期未提供'} → ${range.observedEnd || '日期未提供'} · ${isFiniteNumber(range.observedCount) ? range.observedCount : '—'} 条行情`, observed: true };
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
      '该基金观察截至', '该基金采集时间', '该基金数据状态', '行情采集时间', '行情数据状态']];
    for (const fund of funds) for (const row of fund.visibleRows || fund.rows) {
      const price = row.priceRange || {};
      const validPrice = priceDescription(price).observed;
      lines.push([fund.code, fund.name, fund.company, managerNames(fund).join(' / '), reportPeriod(fund), fund.previousReport?.periodEnd,
        row.stockCode, row.stockName, row.market, row.currency, row.change.label || row.change.kind, row.change.reason,
        row.current?.shares, row.previous?.shares, row.change.sharesDelta, row.current?.weightPct, row.previous?.weightPct, row.change.weightDeltaPp,
        row.position.disclosedYuan, row.position.estimatedYuan, validPrice ? price.low : null, validPrice ? price.high : null,
        price.label || price.status, price.priceBasis, price.rangeStart, price.rangeEnd, price.observedStart, price.observedEnd,
        price.observedCount, '未知；区间价格不代表持有成本', safeHttpUrl(fund.selectedReport?.sourceUrl), safeHttpUrl(price.sourceUrl),
        fund.narrative ? `${fund.narrative.periodStart || ''} / ${fund.narrative.periodEnd || ''}` : '', safeHttpUrl(fund.narrative?.sourceUrl),
        safeHttpUrl(fund.selectedReport?.holdingsSourceUrl), safeHttpUrl(fund.selectedReport?.navSourceUrl), fund.managersAsOf,
        managerAttributionLabel(fund), safeHttpUrl(fund.managerSourceUrl || fund.metadataSourceUrl),
        fund.asOf, fund.retrievedAt, fund.dataStatus, price.fetchedAt, price.dataStatus]);
    }
    return '\ufeff' + lines.map(line => line.map(csvCell).join(',')).join('\r\n');
  }

  function observationState(data, mode, failed) {
    if (!data) return { badge: failed ? '尚无可用数据' : '正在载入', className: 'badge warning', dates: '等待首份有效公开数据' };
    const badge = mode === 'imported' ? '本地导入' : data.sourceMode === 'cached' ? '保留的有效数据' : '公开数据快照';
    return { badge: failed ? `${badge} · 载入未成功` : badge,
      className: `badge ${failed || data.sourceMode === 'cached' ? 'warning' : mode === 'imported' ? 'imported' : 'success'}`,
      dates: `观察截至 ${data.asOf} · 采集 ${data.retrievedAt.replace('T', ' ').replace(/\.\d+Z$/, ' UTC').replace(/Z$/, ' UTC')}` };
  }

  function mount(doc, win) {
    if (!doc.getElementById('fund-results')) return;
    const $ = id => doc.getElementById(id);
    const state = { data: null, manifest: null, companyCode: '', pageIndex: 0, companyRequest: 0, loadingCompany: false, catalog: null, progress: null, directoryRequest: 0, directoryFailures: [], mode: 'remote', failed: false, filtered: [], limit: 30, request: 0 };
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
      const entries = [['', state.manifest ? '选择公司，逐家查看' : state.catalog ? '全部目录公司' : '全部已收录公司']];
      const index = catalogIndex(state.catalog);
      for (const company of state.catalog?.companies || state.progress?.companies || []) entries.push([`code:${company.code}`, company.name]);
      for (const file of state.manifest?.companyFiles || []) if (!entries.some(([value]) => value === `code:${file.companyCode}`)) entries.push([`code:${file.companyCode}`, file.companyName]);
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
      $('stat-scope').textContent = overview.totalFunds !== null ? '目录按份额计数；同组合持仓不重复合计' : '全量目录生成中，分母尚未确认';
      $('stat-company-note').textContent = overview.totalCompanies !== null ? '目录公司均可查询处理状态' : '全量目录生成中，已有报告不代表全量';
      const global = state.manifest?.coverage;
      if (global) {
        $('stat-period').textContent = state.manifest.targetPeriodEnd || global.targetPeriodEnd || global.latestPeriod || '2026-06-30';
        $('stat-prices').textContent = `${formatNumber(global.priceAvailable || 0, 0)} / ${formatNumber(global.holdingCount || 0, 0)}`;
        $('stat-prices-note').textContent = '全局已有行情 / 持仓条目；缺失保留';
      }
    }
    function renderDirectory() {
      updateCompanyOptions(); updateStatistics();
      const overview = coverageOverview(state.catalog, state.progress, state.data);
      const incomplete = !catalogComplete(state.catalog);
      $('catalog-badge').textContent = incomplete ? `全量目录生成中${state.catalog ? ` · 已发现 ${state.catalog.companies.length} 家` : ''}` : `${state.catalog.companies.length} 家目录公司`;
      $('catalog-badge').className = `badge ${incomplete ? 'warning' : 'success'}`;
      $('progress-count').textContent = state.progress ? `${incomplete ? '当前已发现目录' : ''}已处理 ${formatNumber(state.progress.processedFunds, 0)} / ${formatNumber(state.progress.totalFunds, 0)} 只基金份额` : '处理进度待生成';
      $('progress-detail').textContent = state.progress ? `${state.progress.completedCompanies} 家公司处理完成 · ${formatNumber(state.progress.pendingFunds, 0)} 只待处理` : '已有报告与遍历完成是两项独立计数。';
      $('progress-percent').textContent = overview.percent === null ? '—' : `${formatNumber(overview.percent, 1)}%`;
      $('coverage-progress').value = overview.percent || 0;
      $('catalog-dates').textContent = [state.catalog ? `目录采集 ${state.catalog.retrievedAt}` : '目录待生成',
        state.progress ? `进度更新 ${state.progress.updatedAt}` : '进度待生成'].join(' · ');
      const companies = companyCoverage(state.catalog, state.progress, $('company-search').value);
      $('catalog-status').textContent = state.directoryFailures.length ? `${state.directoryFailures.join('；')}。已有目录与进度保留原时点。` :
        `显示 ${companies.length} 家公司。点击公司名称查看当前已收录报告；“未取得”表示本次未完成报告或数据获取。`;
      const container = $('company-coverage'); container.replaceChildren();
      if (!companies.length) {
        container.append(emptyState(state.catalog || state.progress ? '没有匹配的公司' : '全量目录待生成',
          state.catalog || state.progress ? '请尝试公司名称或目录代码；这项搜索覆盖全部已载入公司。' : '等待目录与进度文件生成。下方已有报告仍可独立查看，不代表全量公司已遍历。', true));
        return;
      }
      const table = el('table', 'company-table'); const thead = el('thead'); const head = el('tr');
      ['基金公司', '目录份额', '已处理', '有股票持仓', '无股票持仓', '报告 / 数据未取得', '待处理', '来源'].forEach(label => {
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
        if (company.lastAttemptAt) identity.append(el('small', '', `最近处理 ${company.lastAttemptAt}`));
        else identity.append(el('small', '', '尚无处理时点记录'));
        tr.append(identity);
        for (const key of ['totalFunds', 'processedFunds', 'withHoldings', 'withoutEquities', 'failed', 'pending']) {
          tr.append(el('td', key === 'failed' ? 'count-failed' : key === 'pending' ? 'count-pending' : '', company[key] === null ? '—' : formatNumber(company[key], 0)));
        }
        const source = el('td'); if (!addLink(source, '目录 ↗', company.sourceUrl)) source.textContent = '—'; tr.append(source); tbody.append(tr);
      }
      table.append(thead, tbody); container.append(table);
    }
    async function loadDirectory() {
      const request = ++state.directoryRequest;
      const tasks = [
        { key: 'catalog', path: './data/catalog.json', label: '目录', validate: validateCatalog },
        { key: 'progress', path: './data/progress.json', label: '进度', validate: validateProgress }
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
        $('stat-prices').textContent = `${rows.filter(row => priceDescription(row.priceRange).observed).length} / ${rows.length}`;
        $('stat-prices-note').textContent = '本期披露条目 · 行情覆盖未核验完整性';
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
      if (price.observed) priceCell.append(el('span', 'cell-sub', `口径 ${row.priceRange.priceBasis || '未提供'} · 完整性未核验`));
      if (row.priceRange.fetchedAt) priceCell.append(el('span', 'cell-sub', `${row.priceRange.dataStatus === 'cached' ? '保留行情 · ' : ''}采集 ${row.priceRange.fetchedAt}`));
      const priceSource = el('span', 'cell-sub'); if (addLink(priceSource, '行情来源 ↗', row.priceRange.sourceUrl)) priceCell.append(priceSource);
      tr.append(priceCell); return tr;
    }

    function fundCard(fund, open) {
      const card = el('details', 'fund-card'); card.open = open;
      const summary = el('summary');
      const heading = el('div');
      const title = el('div', 'fund-summary-title'); title.append(el('h3', '', fund.name || '名称未提供'), el('span', 'fund-code', fund.code));
      if (fund.status === 'unavailable') title.append(el('span', 'badge warning', '报告暂缺'));
      if (fund.status === 'no-equities') title.append(el('span', 'badge success', '报告确认期末未持股'));
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
      if (fund.asOf) addMetadata(metadata, '该基金观察截至', fund.asOf);
      if (fund.retrievedAt) addMetadata(metadata, '该基金采集', fund.retrievedAt);
      addMetadata(metadata, '期末净资产', formatMoney(fund.selectedReport?.navYuan));
      addMetadata(metadata, '公告日', fund.selectedReport?.publishedAt);
      addMetadata(metadata, '报告范围', { top10: '前十大重仓股', full: '完整持仓', unknown: '未提供' }[fund.selectedReport?.coverage]);
      addMetadata(metadata, '经理归属观测日', fund.managersAsOf || fund.managerObservedAt || fund.managerAsOf || fund.managerSource?.observedAt);
      addMetadata(metadata, '经理归属口径', `${managerAttributionLabel(fund)}${managerAttributionLabel(fund) === '当前经理概况' ? '，历史归属须查对应报告' : ''}`);
      if (fund.interval) addMetadata(metadata, '比较窗口', fund.interval.label || `${fund.interval.start || '—'} → ${fund.interval.end || '—'}`);
      if (fund.shareClassCodes?.length > 1) addMetadata(metadata, '份额类别', fund.shareClassCodes.join(' / '));
      addLink(metadata, '本期原报告 ↗', fund.selectedReport?.sourceUrl); addLink(metadata, '上期原报告 ↗', fund.previousReport?.sourceUrl);
      addLink(metadata, '持仓数据来源 ↗', fund.selectedReport?.holdingsSourceUrl); addLink(metadata, '净资产数据来源 ↗', fund.selectedReport?.navSourceUrl);
      addLink(metadata, '经理归属来源 ↗', fund.managerSourceUrl || fund.metadataSourceUrl);
      body.append(metadata);
      const warnings = [...(fund.warnings || [])];
      if (fund.dataStatus === 'cached') warnings.unshift('本轮未取得完整更新，继续保留这只基金之前的有效记录；观测截止与采集时间见上方。');
      if (fund.interval?.adjacent === false) warnings.unshift('比较报告期不相邻；持仓差异不能视为单季交易。');
      if (warnings.length) { const alerts = el('div', 'fund-alerts'); warnings.forEach(message => alerts.append(el('p', '', message))); body.append(alerts); }
      if (fund.status === 'no-equities') {
        const evidence = fund.noEquitiesEvidence || fund.selectedReport?.noEquitiesEvidence;
        const note = emptyState('报告确认本期期末未持有股票', evidence?.text || '按报告证据记录本期期末无股票持仓，不按基金类型推断。', true);
        addLink(note, '无股票持仓披露来源 ↗', evidence?.sourceUrl || fund.selectedReport?.sourceUrl); body.append(note);
      } else if (!fund.visibleRows.length) body.append(emptyState('暂无可展示持仓', '这只基金尚无可用的持仓披露明细；报告与经理信息仅按已提供字段展示。', true));
      else {
        const scroll = el('div', 'table-scroll'); scroll.tabIndex = 0; scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', `${fund.name}持仓表，可横向滚动`);
        const table = el('table', 'holdings-table'); const thead = el('thead'); const head = el('tr');
        const labels = ['证券', '披露变化 / 股数差', '本期 / 上期持股数', '净资产占比 / 变动', '本期披露市值', '持仓体量估算', '期间最低 — 最高价 · 非成本'];
        labels.forEach((label, index) => { const th = el('th', index >= 2 && index <= 5 ? 'number' : '', label); th.scope = 'col'; head.append(th); });
        thead.append(head); const tbody = el('tbody'); fund.visibleRows.forEach(row => tbody.append(holdingRow(row))); table.append(thead, tbody); scroll.append(table); body.append(scroll);
      }
      const narrative = el('section', 'narrative'); const narrativeTitle = el('div', 'narrative-title'); narrativeTitle.append(el('h4', '', '报告中的运作说明'));
      addLink(narrativeTitle, '阅读说明来源 ↗', fund.narrative?.sourceUrl); narrative.append(narrativeTitle);
      if (fund.narrative?.text) {
        const period = `${fund.narrative.periodStart || '起始日未提供'} → ${fund.narrative.periodEnd || '结束日未提供'}`;
        const mismatch = fund.narrative.periodEnd && reportPeriod(fund) && fund.narrative.periodEnd !== reportPeriod(fund);
        narrative.append(el('p', 'narrative-meta', `说明期间 ${period}${fund.narrative.page ? ` · 第 ${fund.narrative.page} 页` : ''}${mismatch ? ' · 与当前持仓期不同' : ''}`));
        narrative.append(el('p', 'narrative-text', fund.narrative.text));
        const selectionNote = { 'operation-sentences': '按运作相关关键词选取原文句子。', 'section-opening': '摘录运作分析章节开头。' }[fund.narrative.excerptSelection] || '';
        narrative.append(el('p', 'narrative-note', `${selectionNote}${fund.narrative.truncated ? '当前显示短摘录，请前往来源阅读完整内容。' : ''}报告层面的原文摘录；不能据此确定每只证券的买卖动机。`));
      } else narrative.append(el('p', 'narrative-meta', '尚未提取到可核验的经理运作说明，请查看原报告；不自动推断换仓逻辑。'));
      body.append(narrative); card.append(body); return card;
    }

    function updatePagination() {
      const pages = companyPages(state.manifest, state.companyCode);
      $('company-navigation').hidden = !state.manifest;
      $('company-page-status').textContent = pages.length ? `${pages[0].companyName} · 第 ${state.pageIndex + 1} / ${pages.length} 页 · 公司共 ${pages[0].companyFundCount ?? pages.reduce((sum, page) => sum + page.fundCount, 0)} 份记录${state.loadingCompany ? ' · 正在载入' : ''}` : '这家公司尚无可载入记录，处理状态保留在上方目录中';
      $('company-prev').disabled = state.loadingCompany || state.pageIndex <= 0;
      $('company-next').disabled = state.loadingCompany || state.pageIndex + 1 >= pages.length;
    }
    async function loadCompany(code, pageIndex = 0) {
      const request = ++state.companyRequest;
      const samePage = state.companyCode === code && state.pageIndex === pageIndex;
      const pages = companyPages(state.manifest, code);
      state.companyCode = code; state.pageIndex = pageIndex; state.loadingCompany = Boolean(pages[pageIndex]);
      if (!samePage || !pages[pageIndex]) state.data = null;
      inputs.company.value = `code:${code}`;
      updatePagination(); renderResults();
      if (!pages[pageIndex]) return;
      const controller = new win.AbortController(); const timer = win.setTimeout(() => controller.abort(), 20000);
      try {
        const response = await win.fetch(safeCompanyPath(pages[pageIndex].path), { cache: 'no-store', credentials: 'omit', signal: controller.signal });
        if (!response.ok) throw new Error('COMPANY_LOAD_FAILED');
        const raw = await response.text(); if (raw.length > MAX_DATA_BYTES) throw new Error('INVALID_DATASET');
        const data = validateDataset(JSON.parse(raw));
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
      $('result-count').textContent = `${new Set(state.filtered.map(fund => fund.code)).size} 只基金 · ${state.filtered.length} 份记录 · ${rowCount} 条持仓`;
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
      const request = ++state.request;
      loadDirectory();
      const controller = new win.AbortController();
      const timer = win.setTimeout(() => controller.abort(), 20000);
      $('reload').disabled = true;
      updateObservation(state.data ? '正在重载；当前数据与原始观测日期继续保留。' : '正在读取公开数据文件…');
      try {
        const response = await win.fetch('./data/latest.json', { cache: 'no-store', signal: controller.signal, credentials: 'omit' });
        if (!response.ok) throw new Error(response.status === 404 ? 'NOT_GENERATED' : 'LOAD_FAILED');
        const raw = await response.text();
        if (raw.length > MAX_DATA_BYTES) throw new Error('INVALID_DATASET');
        const data = validateDataset(JSON.parse(raw));
        if (request !== state.request) return;
        if (state.data?.funds.length && !data.funds.length && !data.companyFiles?.length) throw new Error('EMPTY_REPLACEMENT');
        state.failed = false; state.mode = 'remote';
        if (data.companyFiles?.length) {
          state.manifest = data; updateCompanyOptions(); updateStatistics();
          const code = inputs.company.value.startsWith('code:') ? inputs.company.value.slice(5) : data.companyFiles[0].companyCode;
          await loadCompany(code, 0);
        } else {
          state.manifest = null; state.data = data; updateDataset(); updatePagination();
          updateObservation(`已载入 ${new Set(data.funds.map(fund => fund.code)).size} 只基金；重新载入只读取最新已生成结果。`);
        }
      } catch (error) {
        if (request !== state.request) return;
        state.failed = true;
        const reason = error.message === 'NOT_GENERATED' ? '公开数据尚未生成' : error.message === 'EMPTY_REPLACEMENT' ? '新结果为空，未替换现有记录' : error.message === 'INVALID_DATASET' || error instanceof SyntaxError ? '数据格式不符合约定' : '公开数据未能载入';
        updateObservation(`${reason}${state.data ? '；继续显示原有数据与观测日期。' : '。可稍后重载，或导入有效结果 JSON。'}`);
        if (!state.data) { $('fund-results').replaceChildren(emptyState('尚无可用数据', '等待公开数据生成后点击「重新载入」，或导入数据流程导出的 JSON 文件。这里不会以示例持仓代替真实记录。')); $('result-count').textContent = '尚未载入'; }
      } finally {
        win.clearTimeout(timer);
        if (request === state.request) $('reload').disabled = false;
      }
    }

    $('reload').addEventListener('click', reload);
    for (const [key, input] of Object.entries(inputs)) input.addEventListener(key === 'query' ? 'input' : 'change', () => {
      state.limit = 30;
      if (key === 'company' && state.manifest) { inputs.query.value = ''; inputs.manager.value = ''; inputs.change.value = ''; loadCompany(input.value.replace(/^code:/, ''), 0); }
      else renderResults();
    });
    $('company-search').addEventListener('input', renderDirectory);
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
        state.companyRequest++; state.manifest = null; state.loadingCompany = false; state.data = data; state.mode = 'imported'; state.failed = false; updateDataset(); updatePagination();
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
    reload();
  }

  return { validateDataset, validateCatalog, validateProgress, catalogComplete, safeCompanyPath, companyPages, catalogIndex, companyCoverage, coverageOverview, safeHttpUrl, managerNames, managerAttributionLabel, reportPeriod, changeGroup, latestFunds, filterFunds,
    formatNumber, formatMoney, signed, holdingValue, priceDescription, csvCell, exportCsv, observationState, mount };
});
