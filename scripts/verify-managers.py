#!/usr/bin/env python3
"""Actions-only diagnosis of five fixed manager tables; no production writes.

Run the current manager parser unchanged. A temporary Python trace observes
its actual exclusion branches; it does not substitute a second parser or
relax the whole-table completion condition. Only short excerpts are emitted.
"""
import ast
from collections import Counter
import datetime as dt
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import re
import sys
import textwrap

from publication_guard import inspect_content
import report_managers as managers

SAMPLES = (
    {'code': '005892', 'publishedAt': '2026-07-21', 'sourceUrl': 'https://pdf.dfcfw.com/pdf/H2_AN202607211827188285_1.pdf', 'case': 'biography-candidates'},
    {'code': '017052', 'publishedAt': '2026-07-20', 'sourceUrl': 'https://pdf.dfcfw.com/pdf/H2_AN202607201827128068_1.pdf', 'case': 'many-excluded-candidates'},
    {'code': '004845', 'publishedAt': '2026-07-21', 'sourceUrl': 'https://pdf.dfcfw.com/pdf/H2_AN202607211827187317_1.pdf', 'case': 'missing-quarter-proof'},
    {'code': '023937', 'publishedAt': '2026-07-21', 'sourceUrl': 'https://pdf.dfcfw.com/pdf/H2_AN202607211827193628_1.pdf', 'case': 'zero-parsed-rows'},
    {'code': '009817', 'publishedAt': '2026-07-21', 'sourceUrl': 'https://pdf.dfcfw.com/pdf/H2_AN202607211827192463_1.pdf', 'case': 'complete-control'},
)
OUTPUT = Path('.cache/manager-verification/result.json')
MAX_CONTEXT_CHARACTERS = 800
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_PAGES = 40
MAX_OUTPUT_BYTES = 96_000
COUNTS = ('candidateRows', 'parsedRows', 'ignoredKnownRoleRows', 'excludedRows', 'outsidePeriodRows', 'duplicateRows', 'outputManagerCount')
ERRORS = {'ACTIONS_REQUIRED', 'HTTP_REJECTED', 'HTTP_ERROR', 'TLS_FAILED', 'TIMEOUT', 'NETWORK_ERROR',
          'INVALID_PDF', 'EMPTY_RESPONSE', 'RESPONSE_TOO_LARGE', 'PDF_PARSE_FAILED', 'PDF_TEXT_EMPTY',
          'PUBLICATION_REJECTED', 'DIAGNOSTIC_TOO_LARGE', 'PARSER_TRACE_MISMATCH', 'MANAGER_DIAGNOSTIC_FAILED'}


class DiagnosticError(Exception):
    def __init__(self, code):
        self.code = code if code in ERRORS else 'MANAGER_DIAGNOSTIC_FAILED'
        super().__init__(self.code)


def trace_points(function=managers.parse_report_managers):
    """Locate actual counter branches from this parser version's AST, not line numbers."""
    source_lines, start_line = inspect.getsourcelines(function)
    source = textwrap.dedent(''.join(source_lines))
    tree = ast.parse(source); points = {}
    rules = [('not match', 'ROW_LAYOUT_NOT_MATCHED'), ('_role_allowed', 'ROLE_NOT_ALLOWED'),
             ('not appointment', 'INVALID_APPOINTMENT_DATE'), ('departure < appointment', 'DEPARTURE_BEFORE_APPOINTMENT'),
             ('type(page)', 'PAGE_NUMBER_UNVERIFIED'), ('type(table_page)', 'TABLE_PAGE_MAPPING_UNVERIFIED'),
             ('row_pages', 'ROW_PAGE_MAPPING_UNVERIFIED'), ('len(managers)', 'MANAGER_LIMIT')]
    def visit(node, conditions=()):
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            for child in node.body: visit(child, (*conditions, test))
            for child in node.orelse: visit(child, (*conditions, 'not (' + test + ')'))
            return
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Subscript):
            target = ast.unparse(node.target)
            reason = None
            if target == "table['excludedRows']":
                reason = 'UNSCOPED_MANAGER_MENTION'
                for condition in reversed(conditions):
                    found = next((label for fragment, label in rules if fragment in condition), None)
                    if found:
                        reason = found
                        break
            elif target == "table['ignoredKnownRoleRows']": reason = 'KNOWN_ROLE_IGNORED'
            elif target == "table['parsedRows']": reason = 'PARSED_ROW'
            if reason: points[start_line + node.lineno - 1] = reason
        if isinstance(node, ast.Continue) and any('not all(header_matches)' in condition for condition in conditions):
            points[start_line + node.lineno - 1] = 'TABLE_HEADERS_NOT_MATCHED'
        for child in ast.iter_child_nodes(node): visit(child, conditions)
    visit(tree)
    return points, hashlib.sha256(source.encode()).hexdigest()


def event_context(reason, local):
    offsets = local.get('offsets', [])
    first = local.get('first_page', 0)
    numbers = local.get('numbers', [])
    position = None; context = ''; name = None
    if reason == 'TABLE_HEADERS_NOT_MATCHED':
        context = local.get('body', '')[:160]
    elif reason == 'UNSCOPED_MANAGER_MENTION':
        position = local.get('position')
        scope = local.get('scope', '')
        if isinstance(position, int): context = scope[max(0, position - 30):position + 50]
    else:
        match = local.get('match') if reason == 'PARSED_ROW' else local.get('candidate')
        if hasattr(match, 'group'):
            position = match.start(); context = match.group()[:160]
            try: name = match['name']
            except (IndexError, KeyError): pass
    index = next((index for offset, index in reversed(offsets) if isinstance(position, int) and offset <= position), first)
    page = numbers[index] if isinstance(index, int) and 0 <= index < len(numbers) else None
    if type(page) is not int or page <= 0: page = None
    return {'reason': reason, 'page': page, 'section': local.get('section'),
            'nameCandidate': name, 'context': re.sub(r'\s+', ' ', context).strip()}


def trace_parser(pages, report, *, page_numbers=None):
    points, parser_hash = trace_points(); counts = Counter(); examples = {}
    target = managers.parse_report_managers.__code__
    previous = sys.gettrace()
    def tracer(frame, event, _arg):
        if frame.f_code is not target: return None
        if event == 'line' and frame.f_lineno in points:
            reason = points[frame.f_lineno]; counts[reason] += 1
            group = examples.setdefault(reason, [])
            if len(group) < 8: group.append(event_context(reason, frame.f_locals))
        return tracer
    try:
        sys.settrace(tracer)
        rows = managers.parse_report_managers(pages, report, page_numbers=page_numbers)
    finally:
        sys.settrace(previous)
    excluded = sum(count for reason, count in counts.items() if reason not in ('PARSED_ROW', 'KNOWN_ROLE_IGNORED', 'TABLE_HEADERS_NOT_MATCHED'))
    coverage = rows.coverage
    aligned = (excluded == coverage.get('excludedRows', 0)
               and counts['PARSED_ROW'] == coverage.get('parsedRows', 0)
               and counts['KNOWN_ROLE_IGNORED'] == coverage.get('ignoredKnownRoleRows', 0))
    return rows, {'parserHash': parser_hash, 'counts': dict(counts), 'examples': examples, 'counterAlignmentVerified': aligned}


class ExcerptBudget:
    def __init__(self, maximum=MAX_CONTEXT_CHARACTERS):
        self.remaining = maximum
        self.used = 0
    def take(self, value, limit):
        value = re.sub(r'\s+', ' ', str(value or '')).strip()
        value = value[:min(self.remaining, limit)]
        self.used += len(value); self.remaining -= len(value)
        return value


def diagnose_pages(pages, sample, *, pdf_sha256, total_pages=None):
    """Pure diagnostic for synthetic tests or pages already obtained on Actions."""
    report = {**sample, 'kind': 'quarterly', 'periodEnd': '2026-06-30'}
    rows, trace = trace_parser(pages, report)
    if not trace['counterAlignmentVerified']: raise DiagnosticError('PARSER_TRACE_MISMATCH')
    # Reserve 200 characters for a previously missing manager recovered by the
    # cell parser. The total original-source budget remains 800 characters.
    coverage = rows.coverage; budget = ExcerptBudget(MAX_CONTEXT_CHARACTERS - 200)
    cleaned = [managers._clean_page(page) for page in pages]
    headings = []
    for index, page in enumerate(cleaned):
        for heading in managers._TITLE.finditer(page):
            if len(headings) < 4:
                headings.append({'page': index + 1, 'text': budget.take(heading.group(), 55)})
    parsed = [{'name': budget.take(row['name'], 12), 'role': budget.take(row['role'], 60), 'startDate': row['startDate'], 'endDate': row['endDate'],
               'pages': row['evidence']['pages'], 'tablePage': row['evidence']['tablePage'], 'section': row['evidence']['section']}
              for row in rows]
    tables = []
    for table in coverage.get('tables', []):
        tables.append({key: table.get(key) for key in ('section', 'startPage', 'endPage', 'endSection', 'sectionBounded',
            'pageNumbersVerified', 'pageLimitReached', *COUNTS[:-1])} | {'endHeading': budget.take(table.get('endHeading'), 55)})
    examples = []
    # Round-robin categories so one noisy biography does not consume all text.
    groups = [group for reason, group in sorted(trace['examples'].items()) if reason != 'PARSED_ROW']
    for index in range(8):
        for group in groups:
            if index >= len(group) or budget.remaining <= 0: continue
            item = group[index]
            examples.append({key: item[key] for key in ('reason', 'page', 'section')}
                            | {'nameCandidate': budget.take(item.get('nameCandidate'), 16),
                               'context': budget.take(item['context'], 60)})
    return {'code': sample['code'], 'case': sample['case'], 'sourceUrl': sample['sourceUrl'], 'kind': 'quarterly',
            'periodEnd': '2026-06-30', 'publishedAt': sample['publishedAt'], 'pdfSha256': pdf_sha256,
            'parserHash': trace['parserHash'], 'sourceTextHash': coverage.get('sourceTextHash'),
            'physicalPagesRead': len(pages), 'totalPdfPages': total_pages if total_pages is not None else len(pages),
            'pageReadLimitReached': total_pages is not None and total_pages > len(pages),
            'titlePeriodMatches': managers._title_matches_period(cleaned, '2026-06-30'),
            'managerHeadingCount': sum(len(list(managers._TITLE.finditer(page))) for page in cleaned),
            'headings': headings, 'parsedManagers': parsed, 'tables': tables,
            'coverageStatus': coverage.get('status'), 'tableCoverageComplete': coverage.get('complete') is True,
            'counts': {key: coverage.get(key, 0) for key in COUNTS}, 'branchCounts': trace['counts'],
            'counterAlignmentVerified': trace['counterAlignmentVerified'], 'branchExamples': examples,
            'sourceContextCharacters': budget.used, 'sourceContextCharacterLimit': MAX_CONTEXT_CHARACTERS,
            'diagnosticOnly': True, 'productionDataModified': False}


def layout_structure(text, page):
    """Character columns from pypdf layout mode, without publishing cell text.

    These are diagnostic column offsets, not validated PDF cell coordinates.
    No manager row or completeness proof is derived from this representation.
    """
    lines = str(text).splitlines()
    heading = next((index for index, line in enumerate(lines) if managers._TITLE.search(line)), None)
    start = heading + 1 if heading is not None else 0
    rows = []
    for index in range(start, len(lines)):
        if not lines[index].strip(): continue
        cells = []
        for match in re.finditer(r'\S(?:.*?\S)?(?= {2,}|$)', lines[index]):
            value = match.group(); compact = re.sub(r'\s+', '', value)
            labels = [label for label in managers._HEADERS if label in compact]
            kind = 'header' if labels else 'date' if re.search(managers._DATE, value) else 'manager-role' if '基金经理' in compact else 'text'
            cells.append({'column': match.start(), 'characters': len(value), 'kind': kind,
                          **({'headerLabels': labels} if labels else {})})
            if len(cells) >= 8: break
        rows.append({'layoutLine': index + 1, 'cells': cells})
        if len(rows) >= 18: break
    return {'page': page, 'coordinateKind': 'pypdf-layout-character-columns',
            'validatedTableGeometry': False, 'managerHeadingLine': heading + 1 if heading is not None else None,
            'rows': rows, 'rowsAreBoundedSample': True, 'fullPageTextPublished': False}


def add_layout_diagnostics(result, data):
    from pypdf import PdfReader
    table_pages = sorted({table.get(key) for table in result['tables'] for key in ('startPage', 'endPage')
                          if type(table.get(key)) is int})
    page_numbers = table_pages + [heading['page'] for heading in result['headings'] if heading['page'] not in table_pages]
    reader = PdfReader(io.BytesIO(data)); summaries = []
    for number in page_numbers[:2]:
        if not 1 <= number <= min(len(reader.pages), MAX_PAGES): continue
        text = reader.pages[number - 1].extract_text(extraction_mode='layout') or ''
        summaries.append(layout_structure(text, number))
    result['layoutDiagnostics'] = summaries
    result['layoutPageLimitReached'] = len(page_numbers) > 2


def normalized_manager_check(sample, details, now):
    """Exercise the production JS normalizer and Python completeness gate."""
    from verify_repairs import normalized_snapshot
    from traversal import valid_report_managers
    metadata = {**sample, 'kind': 'quarterly', 'periodEnd': '2026-06-30',
                'title': '公开基金2026年第2季度报告', 'check': 'managers'}
    fund = normalized_snapshot(metadata, details, now)
    proof = fund.get('managerTableCoverage') or {}
    return {'managerCount': len(fund.get('reportManagers', [])), 'parserVersion': proof.get('parserVersion'),
            'extractionMethod': proof.get('extractionMethod'),
            'complete': valid_report_managers(fund, fund.get('selectedReport') or {}, {'targetPeriodEnd': '2026-06-30'}),
            'geometryPreserved': bool(proof.get('geometryEvidence')),
            'sourcePreserved': proof.get('sourceUrl') == sample['sourceUrl']}


def manager_details_from_pages(pages, sample):
    """Keep the production report-only PDF evidence beside the manager table.

    These are the pages just extracted from this PDF, not inferred metadata.
    The production builder must still reject a table without its PDF facts.
    """
    from report_extract import _details_for_pages
    details = _details_for_pages(pages, sample['sourceUrl'], '2026-06-30', 'quarterly',
        lambda *_: {}, total_pages=pages.total_pages, published_at=sample['publishedAt'])
    details.update(parseMethod='pypdf', parseErrorCode=None)
    return details


def normalization_error(error):
    # Fixed categories only: exception text can contain local paths or source
    # content and must never enter the public diagnostic artifact.
    if isinstance(error, ImportError): return 'NORMALIZATION_DEPENDENCY_MISSING'
    code = getattr(error, 'code', None)
    return code if code in {'SNAPSHOT_BRIDGE_FAILED', 'SNAPSHOT_REJECTED'} else 'NORMALIZATION_CHECK_FAILED'


def run_sample(sample):
    if os.environ.get('GITHUB_ACTIONS') != 'true': raise DiagnosticError('ACTIONS_REQUIRED')
    if sample not in SAMPLES: raise DiagnosticError('MANAGER_DIAGNOSTIC_FAILED')
    from providers import PublicProvider
    from report_extract import _pypdf_pages
    data = PublicProvider(timeout=18, attempts=1).get(sample['sourceUrl'], binary=True)
    if len(data) > MAX_PDF_BYTES: raise DiagnosticError('RESPONSE_TOO_LARGE')
    if not data.startswith(b'%PDF'): raise DiagnosticError('INVALID_PDF')
    pages = _pypdf_pages(data, MAX_PAGES)
    if not any(page.strip() for page in pages): raise DiagnosticError('PDF_TEXT_EMPTY')
    result = diagnose_pages(pages, sample, pdf_sha256=hashlib.sha256(data).hexdigest(), total_pages=pages.total_pages)
    from manager_cells import enrich_manager_details
    details = manager_details_from_pages(pages, sample)
    enrich_manager_details(details, data, dict(sample, kind='quarterly', periodEnd='2026-06-30'), pages)
    final = details['reportManagers']; proof = details['managerTableCoverage']
    # Replace the old row list instead of publishing a second copy of quotes;
    # keep the original branch counts as the before-cells diagnosis.
    budget = ExcerptBudget(MAX_CONTEXT_CHARACTERS - result['sourceContextCharacters'])
    prior_text = sum(len(row.get(key, '')) for row in result['parsedManagers'] for key in ('name', 'role'))
    budget.remaining += prior_text; budget.used = -prior_text
    final_rows = [{'name': budget.take(row['name'], 12), 'role': budget.take(row['role'], 60),
                   'startDate': row['startDate'], 'endDate': row['endDate'], 'pages': row['evidence']['pages'],
                   'tablePage': row['evidence']['tablePage'], 'section': row['evidence']['section']} for row in final]
    result['sourceContextCharacters'] += budget.used
    result.update(parsedManagers=final_rows, baselineTableCoverageComplete=result['tableCoverageComplete'],
                  tableCoverageComplete=proof.get('complete') is True, coverageStatus=proof.get('status'),
                  effectiveCounts={key: proof.get(key, 0) for key in COUNTS},
                  managerExtractionMethod=proof.get('extractionMethod'), managerParserVersion=proof.get('parserVersion'),
                  managerCellErrorCode=details.get('managerCellErrorCode'))
    geometry = proof.get('geometryEvidence')
    if geometry:
        result['geometrySummary'] = {key: geometry[key] for key in ('pdfSha256', 'sourceTextHash', 'tablePages',
            'physicalRowCount', 'logicalRowCount', 'continuationCount', 'emptyRowCount', 'unresolvedRows')}
        result['geometrySummary']['tables'] = [{'section': table['section'], 'columns': table['columns'],
            'rows': [{'classification': row['classification'], 'fragments': [{key: fragment[key] for key in
                    ('page', 'pageBox', 'cells', 'cellTextHashes', 'continuationType', 'emptyIdentityRegion', 'emptyIdentityRegions') if key in fragment}
                    for fragment in row['fragments']]} for row in table['rows']]}
            for table in geometry['tables']]
    try:
        result['normalizedManagerCheck'] = normalized_manager_check(sample, details, dt.datetime.now(dt.timezone.utc))
    except Exception as error:
        result['normalizedManagerCheck'] = {'complete': False, 'errorCode': normalization_error(error)}
    try:
        add_layout_diagnostics(result, data)
    except Exception:
        result['layoutDiagnosticError'] = 'LAYOUT_EXTRACTION_FAILED'
    return result


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print(json.dumps({'status': 'failed', 'errorCode': 'ACTIONS_REQUIRED'})); return 2
    results = []
    for sample in SAMPLES:
        try:
            result = run_sample(sample)
            result['status'] = 'diagnosed'
        except Exception as error:
            result = {'code': sample['code'], 'sourceUrl': sample['sourceUrl'], 'case': sample['case'],
                      'status': 'failed', 'errorCode': DiagnosticError(getattr(error, 'code', '')).code}
        results.append(result)
    control = next(result for result in results if result['code'] == '009817')
    passed = all(result['status'] == 'diagnosed' and not result['pageReadLimitReached'] for result in results)
    control_passed = control.get('tableCoverageComplete') is True and (control.get('normalizedManagerCheck') or {}).get('complete') is True
    result = {'schemaVersion': 1, 'status': 'diagnosed' if passed and control_passed else 'partial',
              'observedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'controlPassed': control_passed,
              'productionDataModified': False, 'results': results}
    raw = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    error = 'DIAGNOSTIC_TOO_LARGE' if len(raw) > MAX_OUTPUT_BYTES else 'PUBLICATION_REJECTED' if inspect_content(raw) else None
    if error:
        raw = json.dumps({'status': 'failed', 'errorCode': error, 'productionDataModified': False}).encode()
        passed = False
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix('.tmp'); temporary.write_bytes(raw); temporary.replace(OUTPUT)
    print(json.dumps({'status': 'diagnosed' if passed and control_passed else 'partial', 'controlPassed': control_passed}))
    return 0 if passed and control_passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
