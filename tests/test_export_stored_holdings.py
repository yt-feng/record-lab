"""Reconcile complete stored exports without collecting or changing sources."""
import csv
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('holdings_export', ROOT / 'scripts/export-stored-holdings.py')
exporter = importlib.util.module_from_spec(spec); spec.loader.exec_module(exporter)

FIXTURE = r"""
const fs=require('node:fs'),path=require('node:path');
const {buildSnapshot}=require('./scripts/build-data');
const {analyzeQuarter}=require('./scripts/analyze-data');
const root=process.argv[1];
const h=(stockCode,shares)=>({stockCode,stockName:'合成证券'+stockCode,market:'CN',currency:'CNY',shares,weightPct:shares/10,marketValueYuan:shares*10});
for(const quarter of ['2026Q2','2026Q3']){
 const q3=quarter==='2026Q3',target=q3?'2026-09-30':'2026-06-30',baseline=q3?'2026-06-30':'2026-03-31';
 const report=(code,date,holdings)=>({fundCode:code,kind:'quarterly',coverage:'top10',navYuan:10000,
  sourceUrl:`https://example.org/${code}-${date}.pdf`,periodEnd:date,publishedAt:date==='2026-09-30'?'2026-10-20':date==='2026-06-30'?'2026-07-20':'2026-04-20',holdings});
 const prior=[h('600001',q3?100:50),h('600003',20)];
 const funds=[{code:'000001',name:'合成基金A',company:'合成公司',companyId:'co1',sourcePortfolioCode:'000001',
  portfolioSourceUrl:'https://example.org/identity.pdf',shareClassCodes:['000001'],reports:[report('000001',target,[h('600001',q3?150:100),h(q3?'600002':'600003',q3?7:20)]),report('000001',baseline,prior)]},
 {code:'000003',name:'=文字基金',company:'合成公司',companyId:'co1',reports:[report('000003',target,[h('600001',11)])]}];
 const snapshot=buildSnapshot({asOf:'2026-11-11',retrievedAt:'2026-11-11T01:00:00Z',errors:[],barsByStock:{},funds},
  {now:'2026-11-11T01:00:00Z',config:{funds:[{code:'000001'},{code:'000003'}],targetPeriodEnd:target,baselinePeriodEnd:baseline}});
 const folder=path.join(root,'data/periods',quarter);fs.mkdirSync(path.join(folder,'companies'),{recursive:true});fs.mkdirSync(path.join(folder,'analysis'));
 const manifest={...snapshot,funds:[],companyFiles:[{path:'./data/companies/co1.json'}]};
 const index=analyzeQuarter(manifest,()=>snapshot,{now:'2026-11-11T01:00:00Z',writePart:(file,value)=>fs.writeFileSync(path.join(folder,file.slice(2)),JSON.stringify(value))});
 for(const [file,value] of Object.entries({'latest.json':manifest,'analysis.json':index,'companies/co1.json':snapshot,
  'catalog.json':{funds:[{code:'000001',name:'合成基金A'},{code:'000003',name:'文字基金'},{code:'009999',name:'未采集基金'}]},
  'progress.json':{funds:[{code:'000001',status:'complete',quality:{fields:{pdf:'complete',managers:'missing'}}},{code:'000003',status:'complete'},{code:'009999',status:'pending'}]}})){
  fs.writeFileSync(path.join(folder,file),JSON.stringify(value));
 }
 const old=structuredClone(snapshot);old.funds[0].retrievedAt='2026-10-21T00:00:00Z';
 fs.writeFileSync(path.join(folder,'companies/history-r01.json'),JSON.stringify(old));
}
"""


class ExportTests(unittest.TestCase):
    def fixture(self, folder):
        subprocess.run(['node', '-e', FIXTURE, str(folder)], cwd=ROOT, check=True, capture_output=True)

    def test_complete_export_retains_history_reconciles_counts_and_preserves_missing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root, out = Path(directory)/'web', Path(directory)/'export'; self.fixture(root)
            before = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            summary = exporter.export(root, out)
            self.assertEqual(before, {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()})
            self.assertEqual(summary['sourceInventory']['jsonFilesParsed'],len(before))
            self.assertEqual(summary['sourceInventory']['jsonBytesParsed'],sum(map(len,before.values())))
            self.assertFalse(summary['quarters']['2026Q3']['collectionComplete'])
            self.assertEqual(summary['quarters']['2026Q3']['portfolioRecords'],2)
            self.assertEqual(summary['quarters']['2026Q3']['exactBaselineReportRecords'],1)
            self.assertEqual(summary['quarters']['2026Q3']['catalogShareCodes'],3)
            archive=next(out.glob('*.zip'))
            with zipfile.ZipFile(archive) as bundle:
                def rows(prefix):
                    name=next(n for n in bundle.namelist() if n.startswith(prefix))
                    return list(csv.DictReader(io.StringIO(bundle.read(name).decode('utf-8-sig'))))
                changes=rows('2026Q3-持仓变化')
                self.assertEqual(len(changes),summary['quarters']['2026Q3']['exportedComparisonRows'])
                increase=next(r for r in changes if r['代表基金代码']=='000001' and r['证券代码']=='600001')
                self.assertEqual(increase['股数差'],'50'); self.assertEqual(increase['变化类别'],'持股增加')
                self.assertEqual(increase['基期截止日'],'2026-06-30')
                self.assertIn('000001-2026-06-30.pdf',increase['基期报告链接'])
                for row in changes:
                    self.assertEqual(row['是否证明实际交易'],'False')
                    if row['变化类别'] in ('进入披露名单','退出披露名单','基期报告缺失'):
                        self.assertEqual(row['股数差'],'')
                        self.assertTrue(row['本期股数']=='' or row['基期股数']=='')
                self.assertEqual(next(r for r in changes if r['代表基金代码']=='000003')['基金名称'],"'=文字基金")
                gaps=rows('2026Q3-全目录覆盖与缺口')
                self.assertEqual(next(r for r in gaps if r['目录基金代码']=='009999')['处理状态'],'pending')
                inventory=rows('全部JSON解析清单')
                self.assertTrue(any(r['当前季度清单引用']=='False' and 'history-' in r['仓库内相对路径'] for r in inventory))
                holdings=rows('全部Q2Q3持仓观测')
                self.assertTrue(any(r['当前清单选中']=='0' for r in holdings))

    def test_mismatched_generation_or_control_count_fails_without_an_archive(self):
        for field,value in [('generation','wrong-generation'),('contributionCount',999999)]:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as directory:
                root,out=Path(directory)/'web',Path(directory)/'export';self.fixture(root)
                file=root/'data/periods/2026Q3/analysis.json';body=json.loads(file.read_text())
                if field=='generation':body[field]=value
                else:body['coverage'][field]=value
                file.write_text(json.dumps(body))
                with self.assertRaisesRegex(ValueError,'EXPORT_'):exporter.export(root,out)
                self.assertFalse(list(out.glob('*.zip')))

    def test_csv_split_keeps_headers_identifiers_and_numeric_negative_values(self):
        with tempfile.TemporaryDirectory() as directory:
            tables=exporter.Tables(Path(directory),max_rows=2)
            tables.write('rows',['code','number','label'],[['000001',-2,'=1+1'],['000002',0,None],['000003',3,'text']])
            self.assertEqual([x['rows'] for x in tables.inventory],[2,1])
            with (Path(directory)/'rows-001.csv').open(encoding='utf-8-sig',newline='') as stream:
                rows=list(csv.DictReader(stream));self.assertEqual(rows[0],{'code':'000001','number':'-2','label':"'=1+1"})


if __name__=='__main__':unittest.main()
