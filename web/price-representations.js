(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) root.RecordPriceRepresentations = api.mount(root.document, root);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const KEY = 'CN|000636|CNY|unadjusted|2026-07-01|2026-09-30|exchange-sessions-2026-q2-q3-v1';
  const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
  const count = value => Number.isSafeInteger(value) && value >= 0;
  function entries(value, quarter) {
    if (quarter !== '2026Q3' || value?.schemaVersion !== 1 || value.kind !== 'price-representations'
      || value.quarter !== quarter || value.targetPeriodEnd !== '2026-09-30'
      || !Array.isArray(value.records) || value.records.length > 8) return [];
    return value.records.filter(row => row?.kind === 'price-representation-classification' && row.key === KEY
      && row.eligible === true && row.classification === 'stored-fact-representation-compatible'
      && row.scope === 'stored-facts-only' && row.method === 'exact-or-yahoo-equals-binary32-eastmoney'
      && hash(row.classificationDigest) && hash(row.rawRegistryDigest) && hash(row.proof?.evidenceSha256)
      && typeof row.sourceDataCommit === 'string' && /^[a-f0-9]{40}$/.test(row.sourceDataCommit)
      && [row.observationCount, row.mathematicallyClassifiedObservationCount, row.directlyBoundRecheckObservationCount,
        row.uncoveredObservationCount].every(count) && row.observationCount > 0
      && row.factCount === 2 && row.mathematicallyClassifiedObservationCount === row.observationCount
      && row.directlyBoundRecheckObservationCount + row.uncoveredObservationCount === row.observationCount
      && row.directlyBoundRecheckObservationCount <= 2 && row.automaticResolution === false
      && row.quotesModified === false && row.rawConflictModified === false && row.ledgerModified === false
      && row.marketDataRequests === 0);
  }
  function summary(row) {
    return `000636 · 2026 年三季度：${row.mathematicallyClassifiedObservationCount}/${row.observationCount} 条已存价格观测的数值差异符合浮点表示关系。`
      + `现有行情复查直接绑定 ${row.directlyBoundRecheckObservationCount}/${row.observationCount} 条原观察；其余 ${row.uncoveredObservationCount} 条仅按相同存储事实分类，未逐次复查原响应。`;
  }
  function mount(doc, win) {
    const panel = doc.getElementById('price-representation-panel');
    let request = 0, controller = null;
    function reset() {
      request++; controller?.abort(); controller = null;
      if (panel) { panel.replaceChildren(); panel.hidden = true; }
    }
    async function loadQuarter(period) {
      reset();
      if (!panel || period?.id !== '2026Q3') return;
      const token = request, path = './data/periods/2026Q3/price-representations.json';
      const requestController = new win.AbortController(); controller = requestController;
      const signal = requestController.signal;
      const timer = win.setTimeout(() => requestController.abort(), 10000);
      try {
        const response = await win.fetch(path, { credentials: 'omit', cache: 'no-store', signal });
        if (!response.ok) return;
        const raw = await response.text(); if (raw.length > 512000 || token !== request) return;
        const records = entries(JSON.parse(raw), period.id); if (!records.length) return;
        const heading = doc.createElement('h3'); heading.textContent = '价格来源数值核验'; panel.append(heading);
        for (const record of records) {
          const text = doc.createElement('p'); text.textContent = summary(record); panel.append(text);
          const scope = doc.createElement('p');
          scope.textContent = `适用历史快照：${record.sourceDataCommit.slice(0, 12)}。原价格、来源链接和采集时间均保留；这项分类不改变其他证券或全站采集完成度。`;
          panel.append(scope);
        }
        const link = doc.createElement('a'); link.href = path; link.textContent = '查看逐日证据、来源与原始时间';
        link.target = '_blank'; link.rel = 'noopener noreferrer'; panel.append(link); panel.hidden = false;
      } catch { /* Optional evidence is shown only after its own publication. */ }
      finally { win.clearTimeout(timer); }
    }
    return { reset, loadQuarter };
  }
  // These view helpers never change a price or determine price completeness.
  return { entries, summary, mount };
});
