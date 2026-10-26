(function (root, factory) {
  'use strict';
  const api = factory(typeof module === 'object' && module.exports ? require('./strategy-sections') : root.RecordLabStrategySections);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) root.RecordAnalysis = api.mount(root.document, root);
})(typeof globalThis !== 'undefined' ? globalThis : this, function (strategySections) {
  'use strict';
  const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const count = x => Number.isInteger(x) && x >= 0;
  const STRATEGY_CATEGORIES = { 'market-review': '市场回顾', 'portfolio-operations': '组合操作表述', outlook: '后市展望', unclassified: '其他说明' };
  function strategyEvidence(strategy, target, asOf = null) {
    const proof = strategySections?.strategySectionCoverageFor(strategy?.sectionCoverage, target);
    const start = `${target?.slice(0, 4)}-${String(Number(target?.slice(5, 7)) - 2).padStart(2, '0')}-01`;
    const current = proof?.kind === 'quarterly' && proof.periodStart === start;
    const observedAt = strategy?.evidenceRetrievedAt;
    const observed = typeof asOf === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(asOf)
      && generationValue(observedAt) && observedAt.slice(0, 10) <= asOf && proof?.publishedAt <= observedAt.slice(0, 10);
    return { proof, analysis: current && observed ? strategySections.strategyAnalysisFor(strategy?.analysis, proof) : null,
      complete: Boolean(current && observed && proof.complete) };
  }
  const generationValue = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value) && Number.isFinite(Date.parse(value));
  const money = x => finite(x) ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(x / 1e8) + ' 亿元' : '未取得';
  const priceNumber = x => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: Math.abs(x) < 0.01 ? 6 : 4 }).format(x);
  const url = value => { try { const u = new URL(value); return u.protocol === 'https:' && !u.username && !u.password ? u.href : null; } catch { return null; } };
  const quarter = value => typeof value === 'string' && /^2026Q[23]$/.test(value);
  const partPath = value => typeof value === 'string' && /^\.\/analysis\/details-(?:portfolios-)?\d+\.json(?:\.gz)?$/.test(value);
  const safePath = (id, file) => {
    if (!quarter(id) || !(file === 'analysis.json' || partPath(file))) throw new Error('INVALID_ANALYSIS_PATH');
    return `./data/periods/${id}/` + file.replace(/^\.\//, '');
  };
  function validate(data, id) {
    const end = id === '2026Q2' ? '2026-06-30' : '2026-09-30';
    const baseline = id === '2026Q2' ? '2026-03-31' : '2026-06-30';
    if (!quarter(id) || !object(data) || data.schemaVersion !== 1 || data.kind !== 'quarter-analysis'
      || data.targetPeriodEnd !== end || data.baselinePeriodEnd !== baseline || !generationValue(data.generation) || !object(data.coverage)
      || !Array.isArray(data.stocks) || data.stocks.length > 30000 || !Array.isArray(data.portfolioFiles)
      || !Array.isArray(data.themes)) throw new Error('INVALID_ANALYSIS');
    if (data.coverage.status === 'awaiting-data' && (data.asOf !== null || data.retrievedAt !== null
      || data.priceUpdatedAt != null || data.coverage.complete !== false
      || !['inputRecords', 'analyzedRecords', 'stockCount', 'contributionCount'].every(key => data.coverage[key] === 0)
      || data.stocks.length || data.portfolioFiles.length || data.themes.length
      || !Array.isArray(data.topStockKeys) || data.topStockKeys.length)) throw new Error('INVALID_ANALYSIS');
    const seen = new Set();
    for (const stock of data.stocks) {
      if (!object(stock) || typeof stock.key !== 'string' || seen.has(stock.key) || typeof stock.stockName !== 'string'
        || typeof stock.stockCode !== 'string' || !count(stock.currentPortfolioCount)
        || !count(stock.identityUnresolvedCount) || !object(stock.changes)
        || !Array.isArray(stock.details) || !stock.details.every(partPath)
        || ![stock.disclosedValueYuan, stock.navWeightEstimateYuan].every(value => value === null || finite(value) && value >= 0)) throw new Error('INVALID_ANALYSIS');
      seen.add(stock.key);
    }
    if (data.portfolioFiles.some(file => !object(file) || !partPath(file.path) || !Array.isArray(file.ids)
      || !file.ids.every(value => typeof value === 'string'))) throw new Error('INVALID_ANALYSIS');
    return data;
  }
  function validatePart(part, kind, snapshot) {
    if (!object(part) || part.schemaVersion !== 1 || part.kind !== kind
      || part.targetPeriodEnd !== snapshot.targetPeriodEnd || part.baselinePeriodEnd !== snapshot.baselinePeriodEnd
      || part.generation !== snapshot.generation) throw new Error('INVALID_ANALYSIS_PART');
    if (kind === 'stock-contributions') {
      if (!Array.isArray(part.stocks) || part.stocks.some(stock => !object(stock) || typeof stock.key !== 'string'
        || !Array.isArray(stock.contributions) || stock.contributions.some(item => !object(item)
          || typeof item.portfolioId !== 'string' || typeof item.change !== 'string'))) throw new Error('INVALID_ANALYSIS_PART');
    } else if (kind === 'portfolio-evidence') {
      if (!Array.isArray(part.portfolios) || part.portfolios.some(portfolio => !object(portfolio)
        || typeof portfolio.id !== 'string' || !object(portfolio.report) || portfolio.report.periodEnd !== snapshot.targetPeriodEnd
        || !url(portfolio.report.sourceUrl) || !object(portfolio.strategy) || !Array.isArray(portfolio.strategy.excerpts)
        || portfolio.strategy.excerpts.some(item => !object(item) || typeof item.text !== 'string'
          || item.periodEnd !== snapshot.targetPeriodEnd || !url(item.sourceUrl)))) throw new Error('INVALID_ANALYSIS_PART');
      for (const portfolio of part.portfolios) {
        if (!portfolio.strategy.sectionCoverage) continue;
        const value = strategyEvidence(portfolio.strategy, snapshot.targetPeriodEnd, snapshot.asOf);
        if (!value.proof || value.proof.publishedAt > snapshot.asOf
          || portfolio.strategy.analysis && !value.analysis) throw new Error('INVALID_ANALYSIS_PART');
      }
    } else throw new Error('INVALID_ANALYSIS_PART');
    return part;
  }
  function selectStocks(stocks, query = '', order = 'value') {
    const tokens = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const key = { value: 'disclosedValueYuan', estimate: 'navWeightEstimateYuan', holders: 'currentPortfolioCount' }[order];
    const metric = stock => key ? stock[key] ?? -1 : stock.changes?.[order] ?? -1;
    return stocks.filter(stock => tokens.every(token => `${stock.stockCode} ${stock.stockName} ${stock.market}`.toLocaleLowerCase().includes(token)))
      .slice().sort((a, b) => metric(b) - metric(a) || a.key.localeCompare(b.key));
  }
  function csv(stocks) {
    const cell = value => { let text = String(value ?? ''); if (/^[\s]*[=+@-]/.test(text)) text = "'" + text; return '"' + text.replaceAll('"', '""') + '"'; };
    const rows = [['市场', '证券代码', '证券名称', '持有记录数', '身份待核验数', '已核验组合披露市值合计(元)', '净资产乘权重合计(元)', '持股数量增加', '持股数量减少', '进入披露名单', '离开披露名单']];
    stocks.forEach(stock => rows.push([stock.market, stock.stockCode, stock.stockName, stock.currentPortfolioCount,
      stock.identityUnresolvedCount, stock.disclosedValueYuan, stock.navWeightEstimateYuan,
      stock.changes.increase, stock.changes.decrease, stock.changes.enteredDisclosure, stock.changes.leftDisclosure]));
    return '\uFEFF' + rows.map(row => row.map(cell).join(',')).join('\r\n');
  }
  function mount(doc, win) {
    const $ = id => doc.getElementById(id);
    let data = null, id = null, request = 0, detailRequest = 0, limit = 50;
    const cache = new Map();
    const element = (tag, text, className) => { const e = doc.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; };
    const link = (text, source) => { const a = element('a', text); const href = url(source); if (href) { a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; } return a; };
    async function read(file, signal, period) {
      const path = safePath(period, file);
      const response = await win.fetch(path, { signal, cache: 'no-store', credentials: 'omit' });
      if (!response.ok) throw new Error('ANALYSIS_UNAVAILABLE');
      let text;
      if (path.endsWith('.gz') && !/gzip/i.test(response.headers.get('content-encoding') || '')) {
        if (!response.body || !win.DecompressionStream) throw new Error('ANALYSIS_UNAVAILABLE');
        const reader = response.body.pipeThrough(new win.DecompressionStream('gzip')).getReader();
        const decoder = new win.TextDecoder(); const chunks = []; let size = 0;
        try { while (true) { const item = await reader.read(); if (item.done) break; size += item.value.byteLength; if (size > 25_000_000) throw new Error('ANALYSIS_TOO_LARGE'); chunks.push(decoder.decode(item.value, { stream: true })); } text = chunks.join('') + decoder.decode(); }
        finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      } else text = await response.text();
      if (text.length > 25_000_000) throw new Error('ANALYSIS_TOO_LARGE');
      return JSON.parse(text);
    }
    function reset(message = '等待本季度分析结果') {
      request++; detailRequest++; data = null; cache.clear(); limit = 50;
      $('analysis-status').textContent = message; $('analysis-results').replaceChildren(); $('analysis-detail').replaceChildren();
      $('analysis-result-count').textContent = '本季度分析尚未载入';
      $('analysis-themes').replaceChildren(); $('analysis-export').disabled = true; $('analysis-more').hidden = true;
    }
    function render() {
      if (!data) return;
      const stocks = selectStocks(data.stocks, $('analysis-search').value, $('analysis-sort').value);
      $('analysis-result-count').textContent = `${stocks.length.toLocaleString()} / ${data.stocks.length.toLocaleString()} 只证券 · 展示前 ${Math.min(limit, stocks.length)}`;
      const table = element('table', undefined, 'holdings-table'); const head = element('tr');
      ['证券', '持有记录 / 身份待核验', '已核验组合披露市值合计', '净资产 × 比重估算合计', '股数增加 / 减少', '进入 / 离开披露名单', '季度价格区间 · 非成本'].forEach(label => head.append(element('th', label)));
      const thead = element('thead'); thead.append(head); table.append(thead); const body = element('tbody');
      for (const stock of stocks.slice(0, limit)) {
        const row = element('tr'); const first = element('td'); const button = element('button', `${stock.stockName} ${stock.stockCode}`, 'text-button');
        button.addEventListener('click', () => showStock(stock)); first.append(button, element('small', stock.market, 'cell-sub')); row.append(first);
        const prices = stock.priceRange;
        const priceCoverage = prices?.coverageVersion === 2 && prices?.completeness === 'complete'
          ? '交易日完整' : '已观测，完整区间待补全';
        const interval = prices?.status === 'observed' && finite(prices.low) && finite(prices.high)
          ? `${priceNumber(prices.low)} — ${priceNumber(prices.high)} ${prices.currency} · ${priceCoverage}` : '未取得可比较区间';
        [`${stock.currentPortfolioCount} / ${stock.identityUnresolvedCount}`, money(stock.disclosedValueYuan), money(stock.navWeightEstimateYuan),
          `${stock.changes.increase || 0} / ${stock.changes.decrease || 0}`, `${stock.changes.enteredDisclosure || 0} / ${stock.changes.leftDisclosure || 0}`, interval].forEach(value => row.append(element('td', value)));
        body.append(row);
      }
      table.append(body); const scroll = element('div', undefined, 'table-scroll'); scroll.append(table);
      $('analysis-results').replaceChildren(scroll); $('analysis-more').hidden = stocks.length <= limit; $('analysis-export').disabled = !stocks.length;
    }
    async function showStock(stock) {
      const generation = ++detailRequest, current = request, period = id, snapshot = data;
      const target = $('analysis-detail'); target.replaceChildren(element('h3', `${stock.stockName} · 基金持仓与报告观点`), element('p', '正在载入该证券的基金明细…'));
      const controller = new win.AbortController(); const timer = win.setTimeout(() => controller.abort(), 45000);
      try {
        async function part(path, kind) {
          if (generation !== detailRequest || current !== request) throw new Error('STALE_ANALYSIS');
          const cacheKey = `${period}:${path}`;
          if (!cache.has(cacheKey)) cache.set(cacheKey, read(path, controller.signal, period));
          const part = await cache.get(cacheKey);
          if (generation !== detailRequest || current !== request) throw new Error('STALE_ANALYSIS');
          return validatePart(part, kind, snapshot);
        }
        const contributions = [];
        for (const path of stock.details) {
          const value = await part(path, 'stock-contributions');
          if (!Array.isArray(value.stocks)) throw new Error('INVALID_ANALYSIS_PART');
          for (const item of value.stocks) if (item.key === stock.key && Array.isArray(item.contributions)) contributions.push(...item.contributions);
        }
        const ids = new Set(contributions.map(item => item.portfolioId)); const portfolios = new Map();
        const expected = Object.values(stock.changes).reduce((sum, value) => sum + (count(value) ? value : 0), 0);
        if (contributions.length !== expected || ids.size !== contributions.length) throw new Error('INCOMPLETE_ANALYSIS_PART');
        for (const file of snapshot.portfolioFiles.filter(file => file.ids.some(key => ids.has(key)))) {
          const value = await part(file.path, 'portfolio-evidence');
          if (!Array.isArray(value.portfolios)) throw new Error('INVALID_ANALYSIS_PART');
          for (const item of value.portfolios) portfolios.set(item.id, item);
        }
        if (generation !== detailRequest || current !== request) return;
        target.replaceChildren(element('h3', `${stock.stockName} · ${contributions.length} 条基金持仓对比记录`), element('p', '下列观点来自基金报告，不能据此确定该股票的具体买卖理由。身份未核验的记录不计入上方金额合计。', 'filter-tip'));
        const labels = { increase: '持股数增加', decrease: '持股数减少', unchanged: '持股数未变', enteredDisclosure: '进入披露名单', leftDisclosure: '离开披露名单', noBaseline: '缺少上期基准', notComparable: '股数不可比较' };
        for (const item of contributions) {
          const portfolio = portfolios.get(item.portfolioId); if (!portfolio) throw new Error('MISSING_PORTFOLIO');
          const detail = element('details', undefined, 'analysis-fund');
          const managerList = portfolio.reportManagers?.length ? portfolio.reportManagers : portfolio.managers || [];
          const managers = managerList.map(manager => typeof manager === 'string' ? manager : manager.name).filter(Boolean).join(' / ');
          detail.append(element('summary', `${portfolio.name} ${portfolio.code} · ${portfolio.company || ''} · ${managers || '经理待核验'} · ${labels[item.change] || item.change || '变化待核验'}`));
          const report = portfolio.report || {};
          detail.append(element('p', `披露市值 ${money(item.disclosedValueYuan)} · 规模乘比重 ${money(item.navWeightEstimateYuan)} · ${item.aggregationEligible ? '身份可归并' : '身份待核验，金额未合计'}`));
          const number = value => finite(value) ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(value) : '未披露';
          detail.append(element('p', `本期持股 ${number(item.current?.shares)} · 上期持股 ${number(item.previous?.shares)} · 股数差 ${number(item.sharesDelta)} · 本期净资产占比 ${finite(item.current?.weightPct) ? number(item.current.weightPct) + '%' : '未披露'}`));
          const attribution = { report: portfolio.managerCoverageStatus === 'complete' ? '报告期内经理，经理表完整' : '报告期内已核验经理，经理表待补',
            'tenure-overlap': '任期重叠，报告经理表尚未核验', 'current-profile': '当前公开资料，非报告期确认' }[portfolio.managerAttribution] || '待核验';
          detail.append(element('p', `报告期 ${report.periodEnd || '待核验'} · 净资产 ${money(report.navYuan)} · 经理归属：${attribution}`, 'narrative-meta'));
          if (portfolio.managerAttribution === 'report') detail.append(element('p', '报告期内名单可能包含已离任经理，不能将每位经理都视为期末组合的管理人。', 'narrative-meta'));
          for (const manager of managerList) {
            if (!manager || typeof manager === 'string' || portfolio.managerAttribution !== 'report' || !manager.evidence) continue;
            const term = manager.endDate && manager.endDate <= report.periodEnd ? '期内离任' : '期末在任';
            detail.append(element('p', `${manager.name} · 任职 ${manager.startDate || '待核验'} 至 ${manager.endDate || '未披露离任日'} · ${term}`, 'narrative-meta'));
            detail.append(link(`经理表原文 · 第 ${manager.evidence.page} 页 ↗`, manager.evidence.sourceUrl));
          }
          detail.append(link('持仓报告 ↗', report.sourceUrl));
          const strategy = strategyEvidence(portfolio.strategy, snapshot.targetPeriodEnd, snapshot.asOf);
          detail.append(element('p', strategy.complete ? '本季度策略章节已完整提取；以下为选摘。' : '本季度策略章节仍待完整核验；已有引文继续保留。', 'narrative-meta'));
          if (strategy.analysis) {
            const analysis = strategy.analysis;
            const counts = Object.entries(analysis.categorySentenceCounts).map(([key, value]) => `${STRATEGY_CATEGORIES[key]} ${value} 句`).join(' · ');
            detail.append(element('p', `${counts}。原章节 ${analysis.sentenceCount} 句，选摘 ${analysis.selectedSentenceCount} 条；分类可重叠，不代表全文语义分析已完成。`, 'narrative-meta'));
          }
          const excerpts = strategy.analysis?.evidence.length ? strategy.analysis.evidence
            : (portfolio.strategy?.excerpts || []).map(item => ({ text: item.text, sourceUrl: item.sourceUrl, page: item.page }));
          for (const quote of excerpts) {
            if (quote.categories) detail.append(element('p', quote.categories.map(key => STRATEGY_CATEGORIES[key]).filter(Boolean).join(' · '), 'narrative-meta'));
            detail.append(element('p', quote.text, 'narrative-text'));
            if (quote.securityMentions?.some(item => item.market === stock.market && item.stockCode === stock.stockCode)) detail.append(element('p', '原文明确提及该证券；未据此认定具体交易或原因。', 'narrative-meta'));
            if (quote.quoteTruncated) detail.append(element('p', '该句仅展示短引文，完整表述见原报告。', 'narrative-meta'));
            detail.append(link(`报告原文${quote.page ? ' · 第 ' + quote.page + ' 页' : ''} ↗`, quote.sourceUrl));
          }
          if (!excerpts.length) detail.append(element('p', '本季度经理观点尚未提取齐全。', 'narrative-meta'));
          target.append(detail);
        }
      } catch (_) {
        if (generation === detailRequest && current === request) { cache.clear(); target.replaceChildren(element('p', '该证券分析明细未能完整载入，或页面已发布了更新批次。请点击顶部「重新载入」后再查看；未显示混合季度或混合批次的结果。')); }
      } finally { win.clearTimeout(timer); }
    }
    async function loadQuarter(period) {
      reset('正在载入跨基金个股分析…'); id = period?.id;
      if (!quarter(id)) { reset('当前视图没有可用的季度汇总分析。'); return; }
      const current = request, selected = id;
      const controller = new win.AbortController(); const timer = win.setTimeout(() => controller.abort(), 30000);
      try {
        const value = validate(await read('analysis.json', controller.signal, selected), selected);
        if (current !== request) return;
        if (value.coverage.status === 'awaiting-data') {
          reset(`${selected} · 等待有效报告数据，尚无可分析记录。采集未完成，暂未形成报告或行情观测时间。`);
          $('analysis-result-count').textContent = '0 条已取得记录 · 等待数据';
          return;
        }
        data = value;
        $('analysis-status').textContent = `${selected} · ${data.stocks.length.toLocaleString()} 只证券已建立分析索引 · 观察截至 ${data.asOf || data.retrievedAt || '见来源'}。经理表完整 ${data.coverage.reportManagerRecords || 0} 条组合记录，部分 ${data.coverage.reportManagerPartialRecords || 0} 条，待补总计 ${data.coverage.managerEvidenceMissingRecords || 0} 条。当前为已采集样本，完整度以本季度覆盖清单为准。金额以人民币计，价格保留原币种。`;
        const themes = $('analysis-themes'); themes.replaceChildren(element('h3', '基金报告中的策略主题'));
        if (generationValue(data.priceUpdatedAt)) themes.append(element('p', `行情单独更新 ${data.priceUpdatedAt}；报告原有观测日期保留。`, 'narrative-meta'));
        themes.append(element('p', `策略章节完整提取 ${data.coverage.strategySectionCompleteRecords || 0} 条组合记录，待完整核验 ${data.coverage.strategySectionIncompleteRecords ?? data.coverage.analyzedRecords ?? 0} 条；已有摘录 ${data.coverage.strategyRecords || 0} 条。摘录可用与整节完整分开计数。`, 'narrative-meta'));
        themes.append(element('p', '报告期历史产品名单尚未完整核验；本页汇总当前已取得的记录，不能据此视为全行业完整结果。', 'narrative-meta'));
        themes.append(element('p', '主题按本季度报告原文规则归类；计数为有对应表述的组合，不能解释为全行业结论或单只股票交易动机。', 'filter-tip'));
        const list = element('div', undefined, 'analysis-theme-list');
        for (const theme of data.themes) if (typeof theme.name === 'string' && count(theme.portfolioCount)) list.append(element('span', `${theme.name} · ${theme.portfolioCount} 个组合`, 'badge'));
        themes.append(list); render();
      } catch (_) { if (current === request) reset('本季度跨基金分析尚未发布或暂时无法载入；公司明细仍可在下方查看。'); }
      finally { win.clearTimeout(timer); }
    }
    $('analysis-search').addEventListener('input', () => { limit = 50; render(); });
    $('analysis-sort').addEventListener('change', () => { limit = 50; render(); });
    $('analysis-more').addEventListener('click', () => { limit += 50; render(); });
    $('analysis-export').addEventListener('click', () => {
      if (!data) return;
      const file = new win.Blob([csv(selectStocks(data.stocks, $('analysis-search').value, $('analysis-sort').value))], { type: 'text/csv;charset=utf-8' });
      const source = win.URL.createObjectURL(file), a = element('a'); a.href = source; a.download = `stock-analysis-${id}.csv`; doc.body.append(a); a.click(); a.remove(); win.setTimeout(() => win.URL.revokeObjectURL(source), 1000);
    });
    return { loadQuarter, reset };
  }
  return { validate, validatePart, safePath, selectStocks, strategyEvidence, csv, mount };
});
