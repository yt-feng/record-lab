"""Manager appointments from bounded original PDF clipping cells.

The description column is evidence of table geometry, not a source of manager
candidates. Missing cells and ambiguous continuation rows remain unresolved.
"""
from __future__ import annotations
import copy
import hashlib
import io
import json
import math
import re

from foreign_holdings import extract_grid_page, compact
from report_managers import (ManagerRows, PARSER_VERSION, _COUNT_FIELDS, _HEADERS, _date,
                             _report_interval, _role_allowed, manager_key, parse_report_managers)

FIELDS = ('name', 'role', 'start', 'end', 'experience', 'description')
IDENTITY = FIELDS[:4]
TOLERANCE = 1.2
METHOD = 'pdf-clipping-manager-table'
EMPTY_HASH = hashlib.sha256(b'').hexdigest()


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def box(cell):
    return [round(value, 2) for value in (cell.x0, cell.y0, cell.x1, cell.y1)]


def _same_column(cell, bounds):
    return abs(cell.x0 - bounds[0]) <= TOLERANCE and abs(cell.x1 - bounds[1]) <= TOLERANCE


def _unique_cell(matches):
    """Coalesce duplicate clipping paths at the same hundredth-point footprint.

    Word-generated PDFs may clip one text draw through two near-identical
    rectangles. The existing reader assigns the text to the smaller one. Only
    empty duplicates or exact same text are equivalent; conflicting text fails.
    """
    if not matches: return None
    if len(matches) == 1: return matches[0]
    first = box(matches[0])
    if any(any(abs(a - b) > .05 for a, b in zip(first, box(cell))) for cell in matches): return None
    values = {compact(cell.text) for cell in matches if compact(cell.text)}
    if len(values) > 1: return None
    return next((cell for cell in matches if compact(cell.text)), matches[0])


def _header(page):
    labels = {'name': lambda x: x == '姓名', 'role': lambda x: x == '职务',
              'start': lambda x: x == '任职日期', 'end': lambda x: x == '离任日期',
              'experience': lambda x: x.startswith('证券从业') and '年限' in x,
              'description': lambda x: x == '说明'}
    for anchor in page.cells:
        if compact(anchor.text) != '姓名': continue
        headers = {}
        for key, test in labels.items():
            matches = [cell for cell in page.cells if test(compact(cell.text))
                       and cell.y0 >= anchor.y0 - TOLERANCE and cell.y1 <= anchor.y1 + TOLERANCE]
            if len(matches) != 1: break
            headers[key] = matches[0]
        if len(headers) != len(FIELDS): continue
        columns = {key: [headers[key].x0, headers[key].x1] for key in FIELDS}
        if any(columns[a][1] > columns[b][0] + TOLERANCE for a, b in zip(FIELDS, FIELDS[1:])): continue
        return columns, min(cell.y0 for cell in headers.values()), headers
    return None


def _line_y(page, predicate, top=math.inf):
    return max((y for y, line in page.lines if y < top and predicate(compact(line))), default=None)


def _physical_rows(page, columns, top, bottom):
    scoped = [cell for cell in page.cells if cell.y1 <= top + TOLERANCE and cell.y0 >= bottom - TOLERANCE
              and any(_same_column(cell, columns[field]) for field in FIELDS)]
    anchors = []
    for cell in sorted(scoped, key=lambda item: (-item.y1, item.x0)):
        if not any(_same_column(cell, columns[field]) for field in IDENTITY): continue
        if not any(abs(cell.y0 - a.y0) <= TOLERANCE and abs(cell.y1 - a.y1) <= TOLERANCE for a in anchors): anchors.append(cell)
    result = []
    for anchor in anchors:
        cells = {}
        for key in FIELDS:
            matches = [cell for cell in scoped if _same_column(cell, columns[key])
                       and abs(cell.y0 - anchor.y0) <= TOLERANCE and abs(cell.y1 - anchor.y1) <= TOLERANCE]
            unique = _unique_cell(matches)
            if unique is not None: cells[key] = unique
        result.append({'page': page.number, 'pageBox': list(getattr(page, 'page_box', [0, 0, 0, 0])),
                       'cells': cells, 'top': anchor.y1, 'bottom': anchor.y0})
    # A description/experience row with no identity-column rectangle is also an
    # unresolved physical row; otherwise an unsupported merged cell could hide it.
    for cell in scoped:
        if not any(abs(cell.y0 - row['bottom']) <= TOLERANCE and abs(cell.y1 - row['top']) <= TOLERANCE for row in result):
            result.append({'page': page.number, 'pageBox': list(getattr(page, 'page_box', [0, 0, 0, 0])),
                           'cells': {key: cell for key in FIELDS if _same_column(cell, columns[key])},
                           'top': cell.y1, 'bottom': cell.y0})
    for row in result:
        if set(row['cells']) == {'description'}:
            region = [columns['name'][0], row['bottom'], columns['description'][0], row['top']]
            spans = [value for x, y, value in page.spans if region[0] <= x < region[2] and region[1] <= y <= region[3] and compact(value)]
            if not spans:
                row['emptyIdentityRegion'] = {'rect': region, 'spanCount': 0, 'textHash': EMPTY_HASH}
        elif set(row['cells']) == {'role', 'description'}:
            # Some original PDFs carry just the unfinished role and biography
            # cells onto the next page. Missing identity rectangles are not
            # enough: the corresponding page regions must contain no text.
            regions = [[columns['name'][0], row['bottom'], columns['role'][0], row['top']],
                       [columns['start'][0], row['bottom'], columns['description'][0], row['top']]]
            if all(not any(left <= x < right and bottom <= y <= top and compact(value)
                           for x, y, value in page.spans) for left, bottom, right, top in regions):
                row['emptyIdentityRegions'] = [{'rect': region, 'spanCount': 0, 'textHash': EMPTY_HASH} for region in regions]
    return sorted(result, key=lambda row: -row['top'])[:101]


def _fragment(row):
    return {'page': row['page'], 'pageBox': row['pageBox'],
            'cells': {key: box(cell) for key, cell in row['cells'].items()},
            'cellTextHashes': {key: digest(cell.text) for key, cell in row['cells'].items()},
            'cellText': {key: compact(row['cells'][key].text) for key in IDENTITY if key in row['cells']},
            **({'continuationType': 'description-only', 'emptyIdentityRegion': row['emptyIdentityRegion']}
               if row.get('acceptedDescriptionContinuation') else {}),
            **({'continuationType': 'role-description', 'emptyIdentityRegions': row['emptyIdentityRegions']}
               if row.get('acceptedRoleDescriptionContinuation') else {})}


def _continuation(previous, current, *, first_on_page):
    if not first_on_page or not previous or previous[-1]['page'] + 1 != current['page']: return False
    prior = previous[-1]
    if set(previous[0]['cells']) != set(FIELDS): return False
    description_only = set(current['cells']) == {'description'} and current.get('emptyIdentityRegion')
    role_description = set(current['cells']) == {'role', 'description'} and current.get('emptyIdentityRegions')
    if not description_only and not role_description and set(current['cells']) != set(FIELDS): return False
    if any(compact(current['cells'][key].text) for key in ('name', 'start', 'end') if key in current['cells']): return False
    # Original rectangular cells must reach both adjacent page edges. Merely
    # seeing a blank name in another row is not continuation evidence.
    accepted = (prior['pageBox'][3] > prior['pageBox'][1] and current['pageBox'][3] > current['pageBox'][1]
            and prior['bottom'] <= prior['pageBox'][1] + 120
            and current['top'] >= current['pageBox'][3] - 120)
    if accepted and description_only: current['acceptedDescriptionContinuation'] = True
    if accepted and role_description: current['acceptedRoleDescriptionContinuation'] = True
    return accepted


def _logical(rows):
    logical = []
    for first_on_page, row in rows:
        if logical and _continuation(logical[-1], row, first_on_page=first_on_page):
            logical[-1].append(row)
        else: logical.append([row])
    return logical


def _identity(fragments):
    values = {key: ''.join(compact(row['cells'][key].text) for row in fragments if key in row['cells']) for key in IDENTITY}
    name, role = values['name'], values['role']
    start, end = _date(values['start']), _date(values['end'])
    valid = (all(set(row['cells']) == set(FIELDS) or row.get('acceptedDescriptionContinuation')
                 or row.get('acceptedRoleDescriptionContinuation') for row in fragments)
             and re.fullmatch(r'[\u3400-\u9fff]{2,6}(?:[·•][\u3400-\u9fff]{1,6})?', name)
             and 0 < len(role) <= 60 and len(fragments) <= 4
             and all(len(compact(row['cells'][key].text)) <= (120 if key == 'role' else 32) for row in fragments for key in IDENTITY if key in row['cells']))
    if not valid: return values, None, None, 'unresolved'
    if role.count('基金经理') == 1 and '基金经理助理' in role and not any(word in role for word in ('曾', '历任', '毕业', '加入', '担任')):
        return values, start, end, 'assistant'
    if not _role_allowed(role) or not start or values['end'] not in ('-', '—', '－', '–', '至今', '在任') and not end or end and end < start:
        return values, start, end, 'unresolved'
    return values, start, end, 'manager'


def parse_manager_grids(grids, pages, report, *, pdf_sha256, baseline=None):
    baseline = baseline if baseline is not None else parse_report_managers(pages, report)
    coverage = copy.deepcopy(baseline.coverage)
    interval = _report_interval(report)
    if not interval or not coverage.get('tables'): return None
    by_page = {page.number: page for page in grids}
    geometry_tables = []; output = {}; geometry_available = False
    for table in coverage['tables']:
        for key in _COUNT_FIELDS: table[key] = 0
        table['ignoredRoles'] = []
        first, last = table['startPage'], table['endPage']
        if type(first) is not int or type(last) is not int or not 1 <= first <= last < first + 6 or first not in by_page: return None
        found = _header(by_page[first])
        if not found: return None
        columns, header_bottom, headers = found; geometry_available = True
        physical = []; scope_failed = False; notes_seen = False
        for number in range(first, last + 1):
            if number not in by_page:
                scope_failed = True; continue
            page = by_page[number]
            top = header_bottom if number == first else getattr(page, 'page_box', [0, 0, 0, 842])[3] - 40
            if number != first and (repeated := _header(page)):
                if any(abs(repeated[0][field][side] - columns[field][side]) > TOLERANCE for field in FIELDS for side in (0, 1)):
                    scope_failed = True
                else: top = repeated[1]
            bottom = -math.inf
            if number == last and table.get('endSection'):
                section = table['endSection']
                end_y = _line_y(page, lambda line: line.startswith(section) and bool(re.match(re.escape(section) + r'(?![0-9.])', line)), top)
                if end_y is None: scope_failed = True
                else: bottom = end_y
            note_y = _line_y(page, lambda line: bool(re.match(r'注[:：]', line)), top)
            if note_y is not None: bottom = max(bottom, note_y); notes_seen = True
            page_rows = _physical_rows(page, columns, top, bottom)
            if any(compact(cell.text) and cell.y1 <= top + TOLERANCE and cell.y0 >= bottom - TOLERANCE
                   and cell.x0 >= columns['name'][0] - TOLERANCE and cell.x1 <= columns['description'][1] + TOLERANCE
                   and not any(_same_column(cell, columns[field]) for field in FIELDS) for cell in page.cells):
                scope_failed = True
            if len(page_rows) > 100: scope_failed = True
            physical.extend((index == 0, row) for index, row in enumerate(page_rows))
            if notes_seen: break
        table_rows = []
        for fragments in _logical(physical):
            empty = all(set(row['cells']) == set(FIELDS) and all(not compact(cell.text) for cell in row['cells'].values()) for row in fragments)
            item = {'classification': 'empty' if empty else 'unresolved', 'rowKey': None, 'fragments': [_fragment(row) for row in fragments]}
            table_rows.append(item)
            if empty: continue
            table['candidateRows'] += 1
            values, appointment, departure, classification = _identity(fragments)
            item['classification'] = classification
            if classification == 'unresolved': table['excludedRows'] += 1; continue
            item['rowKey'] = json.dumps([values['name'], appointment, departure, values['role']], ensure_ascii=False, separators=(',', ':'))
            if classification == 'assistant':
                table['ignoredKnownRoleRows'] += 1
                table['ignoredRoles'].append({'name': values['name'], 'role': values['role'], 'page': fragments[0]['page'],
                    'text': (values['name'] + ' ' + values['role'])[:240]})
                continue
            table['parsedRows'] += 1
            if appointment > interval[1] or departure and departure < interval[0]:
                table['outsidePeriodRows'] += 1; item['classification'] = 'outside-period'; continue
            manager = {'name': values['name'], 'startDate': appointment, 'endDate': departure, 'role': values['role'],
                       'periodStart': interval[0], 'periodEnd': interval[1], 'activeAtPeriodEnd': departure is None or departure >= interval[1],
                       'evidence': {'sourceUrl': report['sourceUrl'], 'page': fragments[0]['page'],
                           'pages': [row['page'] for row in fragments], 'tablePage': first, 'section': table['section'],
                           'text': ' '.join(values[key] for key in IDENTITY), 'method': 'explicit-report-manager-table', 'periodEnd': interval[1]}}
            key = manager_key(manager)
            if key in output: table['duplicateRows'] += 1
            output.setdefault(key, manager)
        if scope_failed or not physical:
            table['sectionBounded'] = False
        geometry_tables.append({'section': table['section'], 'columns': columns,
            'header': {'page': first, 'pageBox': list(getattr(by_page[first], 'page_box', [0, 0, 0, 0])),
                       'cells': {key: box(cell) for key, cell in headers.items()},
                       'cellText': {key: compact(cell.text) for key, cell in headers.items()},
                       'cellTextHashes': {key: digest(cell.text) for key, cell in headers.items()}}, 'rows': table_rows})
    if not geometry_available: return None
    for key in _COUNT_FIELDS: coverage[key] = sum(table[key] for table in coverage['tables'])
    rows = list(output.values())[:30]
    coverage.update(parserVersion=PARSER_VERSION, extractionMethod=METHOD, outputManagerCount=len(rows), managerKeys=sorted(manager_key(row) for row in rows))
    all_rows = [row for table in geometry_tables for row in table['rows']]
    coverage['geometryEvidence'] = {'schemaVersion': 1, 'pdfSha256': pdf_sha256, 'sourceUrl': report['sourceUrl'],
        'periodEnd': report['periodEnd'], 'sourceTextHash': coverage['sourceTextHash'],
        'tablePages': sorted({fragment['page'] for row in all_rows for fragment in row['fragments']}),
        'physicalRowCount': sum(len(row['fragments']) for row in all_rows),
        'logicalRowCount': sum(row['classification'] != 'empty' for row in all_rows),
        'emptyRowCount': sum(row['classification'] == 'empty' for row in all_rows),
        'continuationCount': sum(len(row['fragments']) - 1 for row in all_rows),
        'unresolvedRows': sum(row['classification'] == 'unresolved' for row in all_rows), 'tables': geometry_tables}
    published = _date(coverage.get('publishedAt'))
    coverage['complete'] = bool(published and published >= interval[1] and rows and len(output) <= 30 and coverage['excludedRows'] == 0
        and all(table['sectionBounded'] and table['pageNumbersVerified'] and not table['pageLimitReached'] for table in coverage['tables'])
        and normalize_geometry_evidence(coverage['geometryEvidence'], coverage) is not None)
    coverage['status'] = 'complete' if coverage['complete'] else 'partial'
    return ManagerRows(rows, coverage)


def normalize_geometry_evidence(raw, coverage):
    if not isinstance(raw, dict) or raw.get('schemaVersion') != 1: return None
    for key in ('sourceUrl', 'periodEnd', 'sourceTextHash'):
        if raw.get(key) != coverage.get(key): return None
    if not re.fullmatch(r'[a-f0-9]{64}', str(raw.get('pdfSha256', ''))): return None
    tables = raw.get('tables')
    if not isinstance(tables, list) or not 1 <= len(tables) <= 30 or len(tables) != len(coverage.get('tables', [])): return None
    counter = {'physicalRowCount': 0, 'logicalRowCount': 0, 'emptyRowCount': 0, 'continuationCount': 0, 'unresolvedRows': 0}
    pages = set(); keys = []; globally_seen = set()
    for table, source_table in zip(tables, coverage['tables']):
        columns = table.get('columns'); rows = table.get('rows')
        if table.get('section') != source_table.get('section') or not isinstance(columns, dict) or set(columns) != set(FIELDS) or not isinstance(rows, list) or len(rows) > 100: return None
        if any(not isinstance(bounds, list) or len(bounds) != 2 or any(type(n) not in (float, int) or not math.isfinite(n) for n in bounds) or bounds[0] >= bounds[1] for bounds in columns.values()): return None
        if any(columns[a][1] > columns[b][0] + TOLERANCE for a, b in zip(FIELDS, FIELDS[1:])): return None
        header = table.get('header')
        if not isinstance(header, dict) or header.get('page') != source_table['startPage']: return None
        if any(not isinstance(header.get(key), dict) or set(header[key]) != set(FIELDS) for key in ('cells', 'cellText', 'cellTextHashes')): return None
        labels = {'name': '姓名', 'role': '职务', 'start': '任职日期', 'end': '离任日期', 'description': '说明'}
        for field in FIELDS:
            text = header['cellText'][field]; bounds = header['cells'][field]
            if not isinstance(text, str) or len(text) > 30 or digest(text) != header['cellTextHashes'][field]: return None
            if field in labels and text != labels[field] or field == 'experience' and not (text.startswith('证券从业') and '年限' in text): return None
            if not isinstance(bounds, list) or len(bounds) != 4 or any(type(n) not in (int, float) or not math.isfinite(n) for n in bounds) or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]: return None
            if abs(bounds[0] - columns[field][0]) > TOLERANCE or abs(bounds[2] - columns[field][1]) > TOLERANCE: return None
        header_name = header['cells']['name']
        if any(bounds[1] < header_name[1] - TOLERANCE or bounds[3] > header_name[3] + TOLERANCE for bounds in header['cells'].values()): return None
        table_counts = dict.fromkeys(_COUNT_FIELDS, 0)
        for row in rows:
            classification = row.get('classification'); fragments = row.get('fragments')
            if classification not in ('manager', 'assistant', 'outside-period', 'unresolved', 'empty') or not isinstance(fragments, list) or not 1 <= len(fragments) <= 4: return None
            values = dict.fromkeys(IDENTITY, '')
            for index, fragment in enumerate(fragments):
                page = fragment.get('page'); page_box = fragment.get('pageBox'); cells = fragment.get('cells'); hashes = fragment.get('cellTextHashes'); texts = fragment.get('cellText')
                if type(page) is not int or not source_table['startPage'] <= page <= source_table['endPage'] or index and page != fragments[index - 1]['page'] + 1: return None
                if not isinstance(page_box, list) or len(page_box) != 4 or any(type(n) not in (float, int) or not math.isfinite(n) for n in page_box) or page_box[0] >= page_box[2] or page_box[1] >= page_box[3]: return None
                description_only = fragment.get('continuationType') == 'description-only'
                role_description = fragment.get('continuationType') == 'role-description'
                if not isinstance(cells, dict) or not set(cells) <= set(FIELDS) or classification != 'unresolved' and set(cells) != set(FIELDS) and not description_only and not role_description: return None
                if description_only:
                    region = fragment.get('emptyIdentityRegion'); bounds = cells.get('description')
                    if not index or set(cells) != {'description'} or not isinstance(region, dict) or region.get('spanCount') != 0 or region.get('textHash') != EMPTY_HASH: return None
                    if not isinstance(bounds, list) or len(bounds) != 4 or region.get('rect') != [columns['name'][0], bounds[1], columns['description'][0], bounds[3]]: return None
                if role_description:
                    regions = fragment.get('emptyIdentityRegions'); bounds = cells.get('role')
                    if not index or set(cells) != {'role', 'description'} or not isinstance(regions, list) or len(regions) != 2 or not isinstance(bounds, list) or len(bounds) != 4: return None
                    expected = [[columns['name'][0], bounds[1], columns['role'][0], bounds[3]],
                                [columns['start'][0], bounds[1], columns['description'][0], bounds[3]]]
                    if any(not isinstance(region, dict) or region.get('spanCount') != 0 or region.get('textHash') != EMPTY_HASH
                           or region.get('rect') != rect for region, rect in zip(regions, expected)): return None
                if not isinstance(hashes, dict) or set(hashes) != set(cells) or not isinstance(texts, dict) or set(texts) != set(cells) & set(IDENTITY): return None
                for field, bounds in cells.items():
                    if not isinstance(bounds, list) or len(bounds) != 4 or any(type(n) not in (int, float) or not math.isfinite(n) for n in bounds) or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]: return None
                    if abs(bounds[0] - columns[field][0]) > TOLERANCE or abs(bounds[2] - columns[field][1]) > TOLERANCE: return None
                    if bounds[0] < page_box[0] - TOLERANCE or bounds[1] < page_box[1] - TOLERANCE or bounds[2] > page_box[2] + TOLERANCE or bounds[3] > page_box[3] + TOLERANCE: return None
                    if not re.fullmatch(r'[a-f0-9]{64}', str(hashes[field])): return None
                    if field in IDENTITY:
                        text = texts[field]
                        if not isinstance(text, str) or len(text) > (120 if field == 'role' else 32) or digest(text) != hashes[field]: return None
                        values[field] += text
                if classification == 'empty' and any(value != EMPTY_HASH for value in hashes.values()): return None
                if classification != 'unresolved' and (max(bound[1] for bound in cells.values()) - min(bound[1] for bound in cells.values()) > TOLERANCE
                        or max(bound[3] for bound in cells.values()) - min(bound[3] for bound in cells.values()) > TOLERANCE): return None
                if index:
                    previous = fragments[index - 1]
                    if any(texts.get(field) for field in ('name', 'start', 'end')) or not description_only and not role_description and set(cells) != set(FIELDS) or set(fragments[0]['cells']) != set(FIELDS): return None
                    if min(bound[1] for bound in previous['cells'].values()) > previous['pageBox'][1] + 120 or max(bound[3] for bound in cells.values()) < page_box[3] - 120: return None
                pages.add(page)
            counter['physicalRowCount'] += len(fragments); counter['continuationCount'] += len(fragments) - 1
            if classification == 'empty': counter['emptyRowCount'] += 1; continue
            counter['logicalRowCount'] += 1; table_counts['candidateRows'] += 1
            if classification == 'unresolved': counter['unresolvedRows'] += 1; table_counts['excludedRows'] += 1; continue
            try: key = json.loads(row['rowKey'])
            except (KeyError, TypeError, ValueError): return None
            if key != [values['name'], _date(values['start']), _date(values['end']), values['role']]: return None
            if not re.fullmatch(r'[\u3400-\u9fff]{2,6}(?:[·•][\u3400-\u9fff]{1,6})?', values['name']) or not 0 < len(values['role']) <= 60: return None
            if classification == 'assistant':
                if values['role'].count('基金经理') != 1 or '基金经理助理' not in values['role'] or any(word in values['role'] for word in ('曾', '历任', '毕业', '加入', '担任')): return None
                table_counts['ignoredKnownRoleRows'] += 1; continue
            if not _date(key[1]) or key[2] is not None and (not _date(key[2]) or key[2] < key[1]) or not _role_allowed(key[3]): return None
            if values['end'] not in ('-', '—', '－', '–', '至今', '在任') and not _date(values['end']): return None
            table_counts['parsedRows'] += 1
            outside = key[1] > coverage['periodEnd'] or key[2] is not None and key[2] < coverage['periodStart']
            if classification == 'outside-period':
                if not outside: return None
                table_counts['outsidePeriodRows'] += 1
            else:
                if outside: return None
                if row['rowKey'] in globally_seen: table_counts['duplicateRows'] += 1
                globally_seen.add(row['rowKey']); keys.append(row['rowKey'])
        if any(table_counts[field] != source_table[field] for field in _COUNT_FIELDS): return None
    if any(type(raw.get(key)) is not int or raw[key] != value for key, value in counter.items()): return None
    if raw.get('tablePages') != sorted(pages) or sorted(set(keys)) != sorted(coverage['managerKeys']): return None
    return copy.deepcopy(raw)


def parse_manager_pdf(data, report, *, pages, baseline=None):
    """Read only relevant manager pages using the existing clipping-grid reader."""
    baseline = baseline if baseline is not None else parse_report_managers(pages, report)
    numbers = sorted({number for table in baseline.coverage.get('tables', [])
                      if type(table.get('startPage')) is int and type(table.get('endPage')) is int
                      and 0 <= table['endPage'] - table['startPage'] < 6
                      for number in range(table['startPage'], table['endPage'] + 1)})
    if not numbers or len(numbers) > 18: return None
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data)); grids = []
    for number in numbers:
        if not 1 <= number <= len(reader.pages): return None
        page = reader.pages[number - 1]
        grid = extract_grid_page(page, number)
        grid.page_box = [float(value) for value in page.mediabox]
        grids.append(grid)
    return parse_manager_grids(grids, pages, report, pdf_sha256=hashlib.sha256(data).hexdigest(), baseline=baseline)


def select_manager_rows(baseline, geometry):
    """Prefer original cells while retaining any independently parsed names.

    A cell-derived continuation may correct the same person's split role.
    An additional valid plain-text appointment cannot be silently discarded;
    its retention makes the geometry's whole-roster claim partial.
    """
    if geometry is None: return baseline
    rows = list(geometry)
    identities = {(row['name'], row['startDate'], row['endDate']) for row in rows}
    extra = [row for row in baseline if (row['name'], row['startDate'], row['endDate']) not in identities]
    if extra:
        proof = copy.deepcopy(geometry.coverage)
        proof['complete'] = False; proof['status'] = 'partial'
        return ManagerRows(rows + extra, proof)
    return geometry


def enrich_manager_details(result, data, report, pages):
    baseline = ManagerRows(result.get('reportManagers', []), result.get('managerTableCoverage'))
    try:
        geometry = parse_manager_pdf(data, report, pages=pages, baseline=baseline)
        selected = select_manager_rows(baseline, geometry)
        result['reportManagers'] = selected
        result['managerTableCoverage'] = selected.coverage
    except Exception:
        # An unsupported original grid cannot invalidate already evidenced
        # appointments or manufacture a complete table from a partial one.
        result['managerCellErrorCode'] = 'MANAGER_CELLS_UNAVAILABLE'
    return result
