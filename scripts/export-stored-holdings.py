"""Extract every stored JSON file and export source-linked quarterly holdings."""
import argparse
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from urllib.parse import urlsplit
import zipfile

from publication_guard import content_limit

QUARTERS = {'2026Q2': ('2026-06-30', '2026-03-31'), '2026Q3': ('2026-09-30', '2026-06-30')}
LABELS = {'increase': '持股增加', 'decrease': '持股减少', 'unchanged': '股数不变',
          'enteredDisclosure': '进入披露名单', 'leftDisclosure': '退出披露名单',
          'noBaseline': '基期报告缺失', 'notComparable': '数量不可比'}


def require(ok, code):
    if not ok: raise ValueError(code)


def load(file):
    require(file.is_file() and not file.is_symlink(), 'EXPORT_INVALID_FILE')
    return json.loads(file.read_bytes())


def source_url(value):
    if not isinstance(value, str): return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password: return ''
        if re.search(r'(?:[?&])(?:token|key|secret|password|signature|authorization)=', value, re.I): return ''
        return value
    except ValueError:
        return ''


def cell(value):
    if value is None: return ''
    if isinstance(value, (list, dict)): value = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if isinstance(value, str) and re.match(r'^\s*[=+@-]', value): return "'" + value
    return value


class Tables:
    def __init__(self, root, max_rows=250_000):
        self.root, self.max_rows, self.inventory = root, max_rows, []

    def write(self, name, header, rows):
        part, count, stream, writer = 0, 0, None, None
        try:
            for row in rows:
                if stream is None or count == self.max_rows:
                    if stream: stream.close()
                    part += 1; count = 0
                    path = self.root / f'{name}-{part:03d}.csv'
                    stream = path.open('w', encoding='utf-8-sig', newline='')
                    writer = csv.writer(stream); writer.writerow(header)
                    self.inventory.append({'file': path.name, 'rows': 0, 'columns': len(header)})
                require(len(row) == len(header), 'EXPORT_COLUMN_MISMATCH')
                writer.writerow([cell(value) for value in row]); count += 1
                self.inventory[-1]['rows'] += 1
            if not part:
                path = self.root / f'{name}-001.csv'
                with path.open('w', encoding='utf-8-sig', newline='') as output: csv.writer(output).writerow(header)
                self.inventory.append({'file': path.name, 'rows': 0, 'columns': len(header)})
        finally:
            if stream: stream.close()


def part_path(folder, value):
    require(isinstance(value, str) and re.fullmatch(r'\./analysis/details-(?:portfolios-)?\d+\.json', value), 'EXPORT_INVALID_PART_PATH')
    return folder / value[2:]


def audit_sources(root, db, current):
    db.execute('CREATE TABLE reports (digest TEXT PRIMARY KEY, code TEXT, period TEXT, kind TEXT, scope TEXT, source TEXT, holdings INTEGER, first_file TEXT, copies INTEGER, current_source INTEGER)')
    db.execute('CREATE TABLE holdings (digest TEXT PRIMARY KEY, payload TEXT, copies INTEGER, current_source INTEGER)')
    audit = []; total_bytes = 0; report_count = 0; holding_count = 0
    for file in sorted(root.rglob('*.json')):
        relative = file.relative_to(root).as_posix()
        require(not file.is_symlink() and file.stat().st_size <= content_limit('web/' + relative), 'EXPORT_INVALID_FILE')
        raw = file.read_bytes(); body = json.loads(raw); total_bytes += len(raw)
        funds = body.get('funds', []) if isinstance(body, dict) else []
        is_company = file.parent.name == 'companies'
        audit.append([relative, len(raw), hashlib.sha256(raw).hexdigest(), body.get('kind') if isinstance(body, dict) else '',
                      len(funds) if isinstance(funds, list) else 0, relative in current, '已解析'])
        if not is_company: continue
        require(isinstance(funds, list), 'EXPORT_INVALID_COMPANY')
        for fund in funds:
            require(isinstance(fund, dict), 'EXPORT_INVALID_FUND')
            reports = [fund.get('selectedReport'), fund.get('previousReport'), *(fund.get('reports') or [])]
            seen = set()
            for report in reports:
                if not isinstance(report, dict): continue
                period = report.get('periodEnd')
                if period not in ('2026-06-30', '2026-09-30'): continue
                code, url = str(fund.get('code', '')), source_url(report.get('sourceUrl'))
                digest = hashlib.sha256((code + '\0' + json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':'))).encode()).hexdigest()
                if digest in seen: continue
                seen.add(digest); report_count += 1
                selected = relative in current and report is fund.get('selectedReport')
                rows = report.get('holdings') or []
                require(isinstance(rows, list), 'EXPORT_INVALID_HOLDINGS')
                db.execute('INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,1,?) ON CONFLICT(digest) DO UPDATE SET copies=copies+1,current_source=MAX(current_source,excluded.current_source)',
                           (digest, code, period, report.get('kind'), report.get('coverage'), url, len(rows), relative, int(selected)))
                for row in rows:
                    if not isinstance(row, dict): continue
                    values = [code, fund.get('name'), fund.get('company'), period, report.get('kind'), report.get('coverage'),
                              row.get('market'), row.get('stockCode'), row.get('stockName'), row.get('currency'),
                              row.get('shares'), row.get('weightPct'), row.get('marketValueYuan'), url, report.get('publishedAt'),
                              fund.get('retrievedAt'), relative]
                    # Retain distinct source/observation variants, rather than overwriting a history row.
                    key = hashlib.sha256(json.dumps(values[:-1], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
                    db.execute('INSERT INTO holdings VALUES (?,?,1,?) ON CONFLICT(digest) DO UPDATE SET copies=copies+1,current_source=MAX(current_source,excluded.current_source)',
                               (key, json.dumps(values, ensure_ascii=False, separators=(',', ':')), int(selected)))
                    holding_count += 1
        if len(audit) % 100 == 0:
            db.commit(); print(json.dumps({'stage': 'source_inventory', 'files': len(audit), 'bytes': total_bytes}), flush=True)
    db.commit()
    return audit, {'jsonFilesParsed': len(audit), 'jsonBytesParsed': total_bytes, 'reportAppearances': report_count,
                   'holdingAppearances': holding_count, 'uniqueReportVersions': db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],
                   'uniqueHoldingObservations': db.execute('SELECT COUNT(*) FROM holdings').fetchone()[0]}


def export_quarter(root, quarter, tables, db):
    target, baseline = QUARTERS[quarter]; folder = root / 'data/periods' / quarter
    manifest, index = load(folder / 'latest.json'), load(folder / 'analysis.json')
    require(index.get('targetPeriodEnd') == target and index.get('baselinePeriodEnd') == baseline
            and index.get('retrievedAt') == manifest.get('retrievedAt')
            and index.get('priceUpdatedAt') == manifest.get('priceUpdatedAt'), 'EXPORT_STALE_ANALYSIS')
    portfolios = {}; aliases = {}; metrics = {}; counts = Counter()
    for entry in index['portfolioFiles']:
        part = load(part_path(folder, entry['path']))
        require(part.get('generation') == index['generation'] and part.get('kind') == 'portfolio-evidence', 'EXPORT_GENERATION_MISMATCH')
        for portfolio in part['portfolios']:
            key = portfolio['id']; require(key not in portfolios, 'EXPORT_DUPLICATE_PORTFOLIO')
            portfolios[key] = portfolio; metrics[key] = Counter()
            for code in set([portfolio['code'], *(portfolio.get('shareClassCodes') or [])]):
                require(code not in aliases or aliases[code] == key, 'EXPORT_AMBIGUOUS_SHARE_CLASS')
                aliases[code] = key
    stock_map = {stock['key']: stock for stock in index['stocks']}
    details = sorted({value for stock in index['stocks'] for value in stock['details']})
    db.execute('CREATE TABLE IF NOT EXISTS comparisons (quarter TEXT, stock TEXT, portfolio TEXT, PRIMARY KEY(quarter,stock,portfolio))')

    def change_rows():
        for name in details:
            part = load(part_path(folder, name))
            require(part.get('generation') == index['generation'] and part.get('kind') == 'stock-contributions', 'EXPORT_GENERATION_MISMATCH')
            for stock in part['stocks']:
                meta = stock_map[stock['key']]
                for row in stock['contributions']:
                    p = portfolios[row['portfolioId']]; report, prior = p['report'], p.get('previousReport') or {}
                    require(report.get('periodEnd') == target, 'EXPORT_REPORT_PERIOD_MISMATCH')
                    require(not prior or prior.get('periodEnd') == baseline, 'EXPORT_BASELINE_PERIOD_MISMATCH')
                    db.execute('INSERT INTO comparisons VALUES (?,?,?)', (quarter, stock['key'], p['id']))
                    current, previous = row.get('current') or {}, row.get('previous') or {}
                    kind = row['change']; require(kind in LABELS, 'EXPORT_UNKNOWN_CHANGE')
                    require(row.get('provesTrade') is False, 'EXPORT_TRADE_ASSERTION')
                    if kind in ('enteredDisclosure', 'leftDisclosure', 'noBaseline'):
                        require(row.get('sharesDelta') is None, 'EXPORT_MISSING_AS_ZERO')
                    counts[kind] += 1; counts['rows'] += 1
                    metrics[p['id']][kind] += 1
                    metrics[p['id']]['currentRows'] += int(bool(current)); metrics[p['id']]['previousRows'] += int(bool(previous))
                    yield [quarter, baseline, target, p['id'], p['code'], p.get('shareClassCodes'), p['name'], p['company'],
                           meta['market'], meta['stockCode'], meta['stockName'], meta['currency'], LABELS[kind],
                           previous.get('shares'), current.get('shares'), row.get('sharesDelta'), previous.get('weightPct'),
                           current.get('weightPct'), row.get('weightDeltaPp'), previous.get('marketValueYuan'),
                           current.get('marketValueYuan'), report.get('coverage'), prior.get('coverage'),
                           row.get('aggregationEligible'), source_url(prior.get('sourceUrl')), source_url(report.get('sourceUrl')),
                           p.get('retrievedAt'), False]
    tables.write(quarter + '-持仓变化', ['季度','基期截止日','本期截止日','组合或记录ID','代表基金代码','份额代码','基金名称','基金公司',
                 '市场','证券代码','证券名称','证券币种','变化类别','基期股数','本期股数','股数差','基期权重百分数','本期权重百分数',
                 '权重差百分点','基期披露市值元','本期披露市值元','本期披露范围','基期披露范围','身份可用于组合汇总',
                 '基期报告链接','本期报告链接','原观测时间','是否证明实际交易'], change_rows())
    require(counts['rows'] == index['coverage']['contributionCount'], 'EXPORT_CONTRIBUTION_COUNT_MISMATCH')
    require(len(portfolios) == index['coverage']['analyzedRecords'], 'EXPORT_PORTFOLIO_COUNT_MISMATCH')
    require(sum(m['currentRows'] > 0 for m in metrics.values()) == index['coverage']['currentHoldingRecords'], 'EXPORT_HOLDING_COUNT_MISMATCH')

    def fund_rows():
        for key, p in sorted(portfolios.items()):
            m = metrics[key]; report = p['report']; strategy = p.get('strategy') or {}
            yield [quarter, p['id'], p['code'], p.get('shareClassCodes'), p['name'], p['company'], p.get('aggregationEligible'),
                   bool(p.get('previousReport')), report.get('coverage'), report.get('reportOnly'), m['currentRows'], m['previousRows'],
                   *[m[name] for name in LABELS], p.get('managerCoverageStatus'), p.get('reportManagers'),
                   strategy.get('sectionStatus'), strategy.get('semanticAnalysisComplete'), source_url(report.get('sourceUrl')), p.get('retrievedAt')]
    tables.write(quarter + '-基金与报告', ['季度','组合或记录ID','代表基金代码','份额代码','基金名称','基金公司','身份可用于组合汇总',
                 '有精确基期报告','披露范围','持仓尚未解析','本期持仓行数','基期持仓行数', *LABELS.values(), '报告经理覆盖','报告经理证据',
                 '策略章节覆盖','策略全文语义完成','报告链接','原观测时间'], fund_rows())
    tables.write(quarter + '-证券汇总', ['季度','市场','证券代码','证券名称','币种','本期持有记录数','基期持有记录数','身份待核验数',
                 *LABELS.values(), '已核验组合披露市值元','已核验组合股数差','披露市值汇总口径'],
                 ([quarter,s['market'],s['stockCode'],s['stockName'],s['currency'],s['currentPortfolioCount'],s['previousPortfolioCount'],
                   s['identityUnresolvedCount'], *[s['changes'].get(name,0) for name in LABELS], s.get('disclosedValueYuan'),
                   s.get('comparableSharesDelta'),'仅来源核验组合；记录数可能含身份待核验记录'] for s in index['stocks']))

    progress = load(folder / 'progress.json'); catalog = load(folder / 'catalog.json')
    progress_map = {f['code']: f for f in progress.get('funds', [])}
    def gap_rows():
        for fund in catalog.get('funds', []):
            code = fund['code']; state = progress_map.get(code, {}); p = portfolios.get(aliases.get(code), {}); m = metrics.get(p.get('id'), {})
            yield [quarter, code, fund.get('name'), fund.get('companyCode'), state.get('status','未记录处理状态'), bool(p),
                   bool(m.get('currentRows')), bool(p.get('previousReport')), p.get('managerCoverageStatus'),
                   (p.get('strategy') or {}).get('sectionStatus'), state.get('quality'), state.get('errorCode'),
                   state.get('lastAttemptAt'), state.get('nextRetryAt')]
    tables.write(quarter + '-全目录覆盖与缺口', ['季度','目录基金代码','名称','公司代码','处理状态','有已解析报告记录','有股票持仓记录',
                 '有精确基期报告','报告经理覆盖','策略章节覆盖','原始质量字段状态','错误类别','上次尝试时间','下次复查时间'], gap_rows())
    db.commit()
    return {'asOf': manifest.get('asOf'), 'retrievedAt': manifest.get('retrievedAt'), 'snapshotCoverage': manifest['coverage'],
            'analysisCoverage': index['coverage'], 'exportedComparisonRows': counts['rows'], 'changeCounts': dict(counts),
            'portfolioRecords': len(portfolios), 'exactBaselineReportRecords': sum(bool(p.get('previousReport')) for p in portfolios.values()),
            'catalogShareCodes': len(catalog.get('funds', [])),
            'collectionComplete': index['coverage'].get('collectionEvidenceComplete') is True}


def export(root='web', output='.cache/holdings-export'):
    root, output = Path(root), Path(output)
    require(root.is_dir() and not root.is_symlink() and not output.exists(), 'EXPORT_INVALID_OUTPUT')
    require(not output.resolve().is_relative_to(root.resolve()), 'EXPORT_OUTPUT_INSIDE_SOURCE')
    output.mkdir(parents=True); files = output / 'files'; files.mkdir(); tables = Tables(files)
    db = sqlite3.connect(output / 'inventory.sqlite'); db.execute('PRAGMA journal_mode=OFF')
    current = set()
    for quarter in QUARTERS:
        folder = root / 'data/periods' / quarter
        for entry in load(folder / 'latest.json')['companyFiles']:
            value = entry['path']; require(re.fullmatch(r'\./data/companies/[A-Za-z0-9_-]{1,64}\.json',value), 'EXPORT_INVALID_COMPANY_PATH')
            current.add((folder / value[len('./data/'):]).relative_to(root).as_posix())
    audit, inventory = audit_sources(root, db, current)
    tables.write('全部JSON解析清单', ['仓库内相对路径','字节数','SHA256','文档类别','基金记录数','当前季度清单引用','解析状态'], audit)
    tables.write('全部Q2Q3报告版本', ['基金代码','报告截止日','报告类别','披露范围','报告来源','持仓行数','首个存储文件','存储出现次数','当前清单选中'],
                 db.execute('SELECT code,period,kind,scope,source,holdings,first_file,copies,current_source FROM reports ORDER BY period,code,digest'))
    tables.write('全部Q2Q3持仓观测', ['基金代码','基金名称','基金公司','报告截止日','报告类别','披露范围','市场','证券代码','证券名称','币种',
                 '披露股数','权重百分数','披露市值元','原报告链接','报告发布日期','原观测时间','首个存储文件','存储出现次数','当前清单选中'],
                 (json.loads(payload)+[copies,current_source] for payload,copies,current_source in db.execute('SELECT payload,copies,current_source FROM holdings ORDER BY digest')))
    summary = {'schemaVersion':1,'status':'parsed-existing-snapshot','sourceInventory':inventory,'quarters':{}}
    for quarter in QUARTERS: summary['quarters'][quarter] = export_quarter(root, quarter, tables, db)
    try: summary['sourceCommit'] = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    except subprocess.CalledProcessError: summary['sourceCommit'] = None
    summary['tables'] = tables.inventory
    summary['definitions'] = ['Q3持仓变化为2026-09-30对比2026-06-30；Q2变化为2026-06-30对比2026-03-31。',
        '股数差仅在两期均披露且数量可比时计算。未披露和报告缺失保留为空，不按零处理。',
        '进入或退出披露名单不证明买入或清仓；股数变化可能包含送转股等公司行为。',
        '历史观测全部导出并标注当前清单是否选中。正式季度比较使用已核验的当前分析分片。',
        '基金目录代码、份额代码、基金记录与来源核验组合是不同口径。身份未核验记录不加入金额汇总。',
        '数据仍为部分覆盖；目录缺口、报告经理与策略章节状态单列。代码字段导入表格时应设为文本。']
    (files / 'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (files / '阅读说明.txt').write_text('\n'.join(summary['definitions'])+'\n',encoding='utf-8')
    archive = output / '2026Q2-Q3-全部已存基金持仓解析.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for file in sorted(files.iterdir()): bundle.write(file,file.name)
    db.close(); print(json.dumps({'status':'exported','jsonFiles':inventory['jsonFilesParsed'],
            'jsonBytes':inventory['jsonBytesParsed'],'tables':len(tables.inventory),'archiveBytes':archive.stat().st_size}),flush=True)
    return summary


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default='web');parser.add_argument('--output',default='.cache/holdings-export')
    args=parser.parse_args()
    try: export(args.root,args.output);return 0
    except Exception as error:
        code=str(error) if type(error) is ValueError and re.fullmatch(r'EXPORT_[A-Z_]+',str(error)) else 'EXPORT_FAILED'
        print(json.dumps({'status':'failed','errorCode':code}),flush=True);return 1


if __name__ == '__main__': raise SystemExit(main())
