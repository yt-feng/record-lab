'use strict';

const SOURCE = 'https://www.csrc.gov.cn/csrc/c101900/c1029655/1029655/files/公募基金产品索引（截至20260630）.xlsx';
const HASH = '0f6ac234b792bb6334c0c94ec37fa6c0548363c80017c946d11a5c7e95d3a9e0';
const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;

function normalizeInception(raw, fund, now = new Date()) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || raw.schemaVersion !== 1
    || !fund || !/^[0-9]{6}$/.test(fund.code) || raw.fundCode !== fund.code
    || raw.periodEnd !== '2026-06-30' || raw.sourceId !== 'csrc-fund-products-20260630'
    || raw.sourceUrl !== SOURCE || raw.contentHash !== HASH || raw.sheet !== 'Sheet1'
    || !Number.isInteger(raw.row) || raw.row < 3 || raw.row > 14172
    || !date(raw.date) || raw.date > raw.periodEnd || raw.date !== fund.inceptionDate || raw.sourceUrl !== fund.inceptionSourceUrl
    || typeof raw.retrievedAt !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(raw.retrievedAt)
    || !date(raw.retrievedAt.slice(0, 10)) || !Number.isFinite(Date.parse(raw.retrievedAt))
    || raw.retrievedAt.slice(0, 10) < raw.periodEnd || Date.parse(raw.retrievedAt) > now.getTime() + 300000) return null;
  return Object.fromEntries(['schemaVersion', 'fundCode', 'periodEnd', 'date', 'sourceId', 'sourceUrl',
    'contentHash', 'sheet', 'row', 'retrievedAt'].map(key => [key, raw[key]]));
}

module.exports = { normalizeInception };
