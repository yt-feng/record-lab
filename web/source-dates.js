(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.RecordLabSourceDates = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  // Publication/founding/as-of dates use China time. Keep original timestamp
  // strings and instant ordering; exchange calendars and retry clocks differ.
  const dateOnly = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
  const stamp = value => typeof value === 'string'
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    && dateOnly(value.slice(0, 10)) && Number.isFinite(Date.parse(value));
  const formatter = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });
  function chinaDate(value) {
    const instant = value instanceof Date ? value : stamp(value) ? new Date(value) : null;
    if (!instant || !Number.isFinite(instant.getTime())) return null;
    const parts = Object.fromEntries(formatter.formatToParts(instant).map(part => [part.type, part.value]));
    return `${parts.year.padStart(4, '0')}-${parts.month}-${parts.day}`;
  }
  function chinaDayEnd(value) {
    // All supported reporting windows are in modern China (UTC+08:00).
    return dateOnly(value) ? Date.parse(value + 'T23:59:59.999+08:00') : NaN;
  }
  return { chinaDate, chinaDayEnd };
});
