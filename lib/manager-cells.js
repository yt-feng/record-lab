'use strict';
const crypto = require('node:crypto');
const FIELDS = ['name', 'role', 'start', 'end', 'experience', 'description'], IDENTITY = FIELDS.slice(0, 4), TOLERANCE = 1.2;
const COUNTS = ['candidateRows', 'parsedRows', 'ignoredKnownRoleRows', 'excludedRows', 'outsidePeriodRows', 'duplicateRows'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const compact = value => value.replace(/\s/g, '');
const digest = value => crypto.createHash('sha256').update(compact(value)).digest('hex');
const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const keys = (value, expected) => object(value) && same(Object.keys(value).sort(), [...expected].sort());
const bounds = (value, length) => Array.isArray(value) && value.length === length && value.every(Number.isFinite)
  && (length === 2 ? value[0] < value[1] : value[0] < value[2] && value[1] < value[3]);
const columnMatches = (box, column) => Math.abs(box[0] - column[0]) <= TOLERANCE && Math.abs(box[2] - column[1]) <= TOLERANCE;
function date(value) {
  if (typeof value !== 'string') return null;
  const numbers = value.match(/\d+/g);
  if (!numbers || numbers.length !== 3) return null;
  const [year, month, day] = numbers.map(Number), result = new Date(Date.UTC(year, month - 1, day));
  return year >= 1900 && result.getUTCFullYear() === year && result.getUTCMonth() === month-1 && result.getUTCDate() === day
    ? result.toISOString().slice(0, 10) : null;
}
function roleAllowed(value) {
  const role = compact(value);
  return (role.match(/基金经理/g) || []).length === 1 && !role.includes('基金经理助理')
    && !/曾|历任|担任|毕业|加入|学历|学位|硕士|博士|学士|就职|从事/.test(role) && !role.includes('证券投资基金') && role.length <= 60
    && (/本基金(?:的)?基金经理/.test(role) || role.startsWith('基金经理') || /(?:部|中心|团队)[-－—/、兼]基金经理$/.test(role));
}
function normalizeGeometryEvidence(raw, coverage) {
  if (!object(raw) || raw.schemaVersion !== 1 || !object(coverage)
    || ['sourceUrl', 'periodEnd', 'sourceTextHash'].some(key => raw[key] !== coverage[key]) || !hash(raw.pdfSha256)
    || !Array.isArray(raw.tables) || !raw.tables.length || raw.tables.length > 30
    || raw.tables.length !== coverage.tables?.length) return null;
  const total = { physicalRowCount: 0, logicalRowCount: 0, emptyRowCount: 0, continuationCount: 0, unresolvedRows: 0 };
  const pages = new Set(), outputKeys = new Set(), globalKeys = new Set(), tables = [];
  for (let index = 0; index < raw.tables.length; index++) {
    const table = raw.tables[index], source = coverage.tables[index], columns = table?.columns, header = table?.header;
    if (!object(table) || table.section !== source.section || !keys(columns, FIELDS)
      || FIELDS.some(field => !bounds(columns[field], 2)) || !Array.isArray(table.rows) || table.rows.length > 100
      || FIELDS.slice(1).some((field, index) => columns[FIELDS[index]][1] > columns[field][0] + TOLERANCE)
      || !object(header) || header.page !== source.startPage || ['cells', 'cellText', 'cellTextHashes'].some(key => !keys(header[key], FIELDS))) return null;
    const labels = { name: '姓名', role: '职务', start: '任职日期', end: '离任日期', description: '说明' };
    for (const field of FIELDS) {
      const text = header.cellText[field], box = header.cells[field];
      if (typeof text !== 'string' || text.length > 30 || digest(text) !== header.cellTextHashes[field]
        || (labels[field] ? text !== labels[field] : !text.startsWith('证券从业') || !text.includes('年限'))
        || !bounds(box, 4) || !columnMatches(box, columns[field])) return null;
    }
    if (FIELDS.some(field => header.cells[field][1] < header.cells.name[1] - TOLERANCE
      || header.cells[field][3] > header.cells.name[3] + TOLERANCE)) return null;
    const counts = Object.fromEntries(COUNTS.map(key => [key, 0])), rows = [];
    for (const row of table.rows) {
      const classification = row?.classification, fragments = row?.fragments, values = Object.fromEntries(IDENTITY.map(key => [key, '']));
      if (!['manager', 'assistant', 'outside-period', 'unresolved', 'empty'].includes(classification)
        || !Array.isArray(fragments) || !fragments.length || fragments.length > 4) return null;
      const cleanedFragments = [];
      for (let at = 0; at < fragments.length; at++) {
        const fragment = fragments[at], page = fragment?.page, box = fragment?.pageBox;
        const cells = fragment?.cells, hashes = fragment?.cellTextHashes, texts = fragment?.cellText;
        const descriptionOnly = fragment?.continuationType === 'description-only';
        if (!object(fragment) || !Number.isInteger(page) || page < source.startPage || page > source.endPage
          || at && page !== fragments[at-1].page + 1 || !bounds(box, 4) || !object(cells)
          || Object.keys(cells).some(field => !FIELDS.includes(field)) || classification !== 'unresolved' && !keys(cells, FIELDS) && !descriptionOnly
          || !keys(hashes, Object.keys(cells)) || !keys(texts, Object.keys(cells).filter(field => IDENTITY.includes(field)))) return null;
        if (descriptionOnly) {
          const region = fragment.emptyIdentityRegion, cell = cells.description;
          if (!at || !keys(cells, ['description']) || !object(region) || region.spanCount !== 0 || region.textHash !== digest('')
            || !bounds(cell, 4) || !same(region.rect, [columns.name[0], cell[1], columns.description[0], cell[3]])) return null;
        }
        for (const [field, cell] of Object.entries(cells)) {
          if (!bounds(cell, 4) || !columnMatches(cell, columns[field]) || cell[0] < box[0]-TOLERANCE || cell[1] < box[1]-TOLERANCE
            || cell[2] > box[2]+TOLERANCE || cell[3] > box[3]+TOLERANCE || !hash(hashes[field])) return null;
          if (IDENTITY.includes(field)) {
            if (typeof texts[field] !== 'string' || texts[field].length > (field === 'role' ? 120 : 32)
              || digest(texts[field]) !== hashes[field]) return null;
            values[field] += texts[field];
          }
        }
        if (classification === 'empty' && Object.values(hashes).some(value => value !== digest(''))) return null;
        const coordinates = Object.values(cells);
        if (classification !== 'unresolved' && (Math.max(...coordinates.map(v => v[1])) - Math.min(...coordinates.map(v => v[1])) > TOLERANCE
          || Math.max(...coordinates.map(v => v[3])) - Math.min(...coordinates.map(v => v[3])) > TOLERANCE)) return null;
        if (at) {
          const previous = fragments[at-1];
          if (['name', 'start', 'end'].some(field => texts[field]) || !descriptionOnly && !keys(cells, FIELDS) || !keys(fragments[0].cells, FIELDS)
            || Math.min(...Object.values(previous.cells).map(v => v[1])) > previous.pageBox[1]+120
            || Math.max(...coordinates.map(v => v[3])) < box[3]-120) return null;
        }
        pages.add(page);
        cleanedFragments.push({ page, pageBox: [...box], cells: Object.fromEntries(Object.entries(cells).map(([k,v]) => [k,[...v]])),
          cellTextHashes: { ...hashes }, cellText: { ...texts }, ...(descriptionOnly ? {
            continuationType: 'description-only', emptyIdentityRegion: { rect: [...fragment.emptyIdentityRegion.rect], spanCount: 0, textHash: digest('') }
          } : {}) });
      }
      total.physicalRowCount += fragments.length; total.continuationCount += fragments.length-1;
      const cleanRow = { classification, rowKey: null, fragments: cleanedFragments }; rows.push(cleanRow);
      if (classification === 'empty') { total.emptyRowCount++; continue; }
      total.logicalRowCount++; counts.candidateRows++;
      if (classification === 'unresolved') { total.unresolvedRows++; counts.excludedRows++; continue; }
      let key; try { key = JSON.parse(row.rowKey); } catch { return null; }
      if (!same(key, [values.name, date(values.start), date(values.end), values.role])
        || !/^[\u3400-\u9fff]{2,6}(?:[·•][\u3400-\u9fff]{1,6})?$/.test(values.name) || !values.role.length || values.role.length > 60) return null;
      cleanRow.rowKey = row.rowKey;
      if (classification === 'assistant') {
        if ((values.role.match(/基金经理/g) || []).length !== 1 || !values.role.includes('基金经理助理') || /曾|历任|毕业|加入|担任/.test(values.role)) return null;
        counts.ignoredKnownRoleRows++; continue;
      }
      if (!date(key[1]) || key[2] !== null && (!date(key[2]) || key[2] < key[1]) || !roleAllowed(key[3])
        || !['-', '—', '－', '–', '至今', '在任'].includes(values.end) && !date(values.end)) return null;
      counts.parsedRows++;
      const outside = key[1] > coverage.periodEnd || key[2] !== null && key[2] < coverage.periodStart;
      if (classification === 'outside-period') {
        if (!outside) return null;
        counts.outsidePeriodRows++;
      } else {
        if (outside) return null;
        if (globalKeys.has(row.rowKey)) counts.duplicateRows++;
        globalKeys.add(row.rowKey); outputKeys.add(row.rowKey);
      }
    }
    if (COUNTS.some(key => counts[key] !== source[key])) return null;
    tables.push({ section: table.section, columns: Object.fromEntries(FIELDS.map(k => [k,[...columns[k]]])),
      header: { page: header.page, cells: Object.fromEntries(FIELDS.map(k => [k,[...header.cells[k]]])),
        cellText: { ...header.cellText }, cellTextHashes: { ...header.cellTextHashes } }, rows });
  }
  if (Object.entries(total).some(([key,value]) => !Number.isInteger(raw[key]) || raw[key] !== value)
    || !same(raw.tablePages, [...pages].sort((a,b) => a-b)) || !same([...outputKeys].sort(), [...coverage.managerKeys].sort())) return null;
  return { schemaVersion: 1, pdfSha256: raw.pdfSha256, sourceUrl: raw.sourceUrl, periodEnd: raw.periodEnd,
    sourceTextHash: raw.sourceTextHash, tablePages: [...pages].sort((a,b) => a-b), ...total, tables };
}
module.exports = { normalizeGeometryEvidence };
